"""What the OCR client does when Azure says no.

Every case here is one that actually happened. On 2026-08-27 the ledger held 26 unread attachments
against a working, credentialled Azure resource: 20 rate-limited, 5 refused for size, 1 dropped
mid-upload. The client had no handling for any of it — one flat 2s retry, `except Exception` for
every failure alike, and `raise_for_status()` discarding the response body that said what was
wrong.

Nothing here touches the network. `requests` is faked per test, so the branches are exercised
without billing a page.
"""

import io
import sys
import types

import pytest

from config import settings
from pipeline.stage3_extract import ocr_adapter
from pipeline.stage3_extract.ocr_adapter import (
    AzureDocumentIntelligenceClient,
    AzureOcrError,
    OcrServiceUnavailable,
    analyze_with_retry,
    fit_for_upload,
)


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text
        self.url = "https://premier.cognitiveservices.azure.com/fake"

    def json(self):
        return self._payload


@pytest.fixture
def client(monkeypatch):
    """A real client pointed at nothing, with pacing off so tests do not sleep."""
    monkeypatch.setattr(settings, "OCR_MIN_SECONDS_BETWEEN_CALLS", 0)
    monkeypatch.setattr(ocr_adapter.time, "sleep", lambda _s: None)
    return AzureDocumentIntelligenceClient(endpoint="https://x", api_key="k", model="m")


def install_requests(monkeypatch, *, post, get):
    """Swap the `requests` module both methods import locally."""
    fake = types.SimpleNamespace(post=post, get=get)
    monkeypatch.setitem(sys.modules, "requests", fake)
    return fake


def png_bytes(width: int, height: int) -> bytes:
    """Noise, not a blank canvas — a flat image compresses to almost nothing and would never
    exercise the size path it is here to test."""
    import os

    from PIL import Image

    image = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# --- what Azure said ----------------------------------------------------------------------------

def test_the_azure_error_body_is_read_before_the_status_is_raised_on():
    """The five identical 400s that started this. `raise_for_status()` produced
    "400 Client Error: Bad Request for url: …" and the ledger held that and nothing else — while
    Azure's own reply named the reason exactly."""
    response = FakeResponse(400, {
        "error": {"code": "InvalidRequest", "message": "Invalid request.",
                  "innererror": {"code": "InvalidContentLength",
                                 "message": "The input image is too large."}}})

    with pytest.raises(AzureOcrError) as caught:
        ocr_adapter._raise_for_status(response)

    assert caught.value.code == "InvalidContentLength", "the *inner* code is the specific one"
    assert "too large" in caught.value.message
    assert "InvalidContentLength: The input image is too large." in str(caught.value)


def test_a_body_that_is_not_json_still_produces_a_usable_error():
    with pytest.raises(AzureOcrError) as caught:
        ocr_adapter._raise_for_status(FakeResponse(502, None, text="<html>gateway</html>"))
    assert caught.value.status == 502


def test_the_specific_failure_reaches_the_ledger_as_a_queryable_type():
    """`error_type` was the literal "OcrServiceUnavailable" for every cause alike, so "show me the
    rate-limited ones" could not be asked of 108 rows at all."""
    assert ocr_adapter._error_type_for(
        AzureOcrError(400, "InvalidContentLength", "x")) == "InvalidContentLength"
    assert ocr_adapter._error_type_for(AzureOcrError(429, "", "")) == "TooManyRequests"


# --- what is worth trying again -----------------------------------------------------------------

def test_a_rejected_file_is_not_sent_a_second_time(client, monkeypatch):
    """A 400 is deterministic. Re-uploading 4.67 MB to be refused again spends an attempt, a paid
    call and the wait between them to learn nothing."""
    calls = []

    def post(url, **kw):
        calls.append(url)
        return FakeResponse(400, {"error": {"innererror": {
            "code": "InvalidContentLength", "message": "The input image is too large."}}})

    install_requests(monkeypatch, post=post, get=None)

    with pytest.raises(OcrServiceUnavailable) as caught:
        analyze_with_retry(client, b"x", "msg-1")

    assert len(calls) == 1, "a permanent refusal must not be retried"
    assert "InvalidContentLength" in caught.value.detail, "and must carry Azure's own words"
    assert caught.value.error_type == "InvalidContentLength"


def test_a_throttled_submit_is_retried_and_can_succeed(client, monkeypatch):
    """77% of every OCR failure recorded was a 429 — the one condition with no handling."""
    posts = []

    def post(url, **kw):
        posts.append(url)
        if len(posts) == 1:
            return FakeResponse(429, {}, headers={"Retry-After": "1"})
        return FakeResponse(202, {}, headers={"operation-location": "https://op/1"})

    def get(url, **kw):
        return FakeResponse(200, {"status": "succeeded", "analyzeResult": {"content": "ok"}})

    install_requests(monkeypatch, post=post, get=get)

    result = analyze_with_retry(client, b"x", "msg-1")

    assert len(posts) == 2
    assert result.raw_text == "ok"


def test_the_wait_prefers_the_retry_after_azure_sent():
    """Azure states how long its own throttle lasts. The old flat 2s landed inside the same window
    and gave up — `connectors/mailbox.py` has honoured this header for Graph all along."""
    assert ocr_adapter._retry_after_seconds(
        AzureOcrError(429, "", "", retry_after="17"), attempt=0) == 17

    # No header: exponential, and never below the configured base.
    assert ocr_adapter._retry_after_seconds(AzureOcrError(429, "", ""), attempt=2) >= (
        settings.AZURE_OCR_RETRY_BACKOFF_SECONDS * 4)


def test_our_own_spend_guard_is_not_argued_with(client):
    """`BudgetedOcrClient` stops the run when the page budget is gone. Retrying that is asking our
    own guard to change its mind.

    It has its own class precisely so this can be told apart from any other `RuntimeError`.
    Treating every `RuntimeError` as permanent would have bought this one case at the price of
    silently never retrying a genuine transient fault — the opposite of what this whole change is
    for, and the sort of fix that looks right until something stops being retried.
    """
    assert not ocr_adapter._is_retryable(ocr_adapter.OcrBudgetExhausted("budget of 15 exhausted"))
    assert not ocr_adapter._is_retryable(ValueError("document is too big"))
    assert ocr_adapter._is_retryable(TimeoutError("read timed out"))
    assert ocr_adapter._is_retryable(RuntimeError("something unanticipated")), (
        "an unexpected fault is exactly what a retry is for")


# --- the twelve that were paid for and thrown away -----------------------------------------------

def test_a_throttled_poll_waits_instead_of_resubmitting_the_document(client, monkeypatch):
    """The most expensive bug in this path.

    Twelve analyses were submitted, accepted and billed, and then abandoned: a 429 on the poll
    propagated out of `analyze`, and the retry answered it by POSTing the whole document again —
    adding load to the endpoint that had just asked us to slow down, and leaving the finished
    result at a URL nobody went back to. The result is at `operation-location` either way.
    """
    posts, gets = [], []

    def post(url, **kw):
        posts.append(url)
        return FakeResponse(202, {}, headers={"operation-location": "https://op/1"})

    def get(url, **kw):
        gets.append(url)
        if len(gets) < 3:
            return FakeResponse(429, {}, headers={"Retry-After": "1"})
        return FakeResponse(200, {"status": "succeeded", "analyzeResult": {"content": "recovered"}})

    install_requests(monkeypatch, post=post, get=get)

    result = analyze_with_retry(client, b"x", "msg-1")

    assert len(posts) == 1, "the document must be submitted exactly once"
    assert gets == ["https://op/1"] * 3, "and the same operation polled until it answers"
    assert result.raw_text == "recovered"


# --- fitting the image to the resource ------------------------------------------------------------

def test_an_oversized_image_is_scaled_to_fit_rather_than_refused():
    """The photo on PO 214711 — 4.67 MB, and the only evidence for a quantity conflict nobody can
    settle. Refusing it loses the delivery; shrinking it costs nothing OCR can read."""
    big = png_bytes(4000, 3000)
    assert len(big) > 200_000

    fitted = fit_for_upload(big, limit=100_000)

    assert len(fitted) <= 100_000
    from PIL import Image
    assert Image.open(io.BytesIO(fitted)).format == "JPEG", (
        "re-encoding also normalises MPO, which is not a format Document Intelligence accepts")


def test_a_file_already_within_the_cap_is_sent_untouched():
    """Nothing wrong with it, so nothing is done to it — not even a re-encode.

    Identity, not equality, on purpose: the overwhelming majority of attachments are already
    acceptable, and quietly re-encoding them would spend CPU to hand OCR a slightly worse image
    than the sender sent.

    200x200 rather than the 40x40 this used to use. Forty is below Document Intelligence's own
    50-pixel floor, so that fixture now describes a file the service refuses to look at — which
    `fit_for_upload` scales into range, correctly, and which is a different test's subject."""
    small = png_bytes(200, 200)
    assert fit_for_upload(small, limit=10_000_000) is small


def test_a_pdf_above_the_cap_is_refused_honestly_not_as_an_outage(client, monkeypatch):
    """An oversized PDF that cannot be opened is refused by *size*, never as an outage.

    There is no lossless way to shrink a PDF here, so `fit_for_upload` returns it unchanged. A
    readable one is then split by page (see the test below); this one is bytes that begin `%PDF`
    and are nothing else, so there are no pages to split. What matters is the wording it fails
    with: `service_unavailable` on a ledger row sends a reviewer to look for an outage, and the
    file being 5 KB over a 1 KB cap is not one."""
    monkeypatch.setattr(settings, "OCR_MAX_UPLOAD_BYTES", 1000)
    oversized = b"%PDF-1.4" + b"x" * 5000
    assert fit_for_upload(oversized, limit=1000) == oversized

    with pytest.raises(ValueError) as caught:
        client.analyze(oversized)
    assert "upload limit" in str(caught.value)


# ---------------------------------------------------------------------------
# Refusals that describe the request, not the file.
#
# Three of Document Intelligence's 400s are about the shape of what we sent, and every one of them
# reads on a ledger row as though the attachment were broken. On 2026-09-09 they accounted for 28
# of the 533 attachments waiting on OCR: 21 `InvalidContent`, 7 `InvalidContentDimensions`. Both
# codes were reproduced against the live resource before these tests were written, and both were
# fixed by changing the upload rather than the file.
# ---------------------------------------------------------------------------


def gif_bytes(width: int, height: int, frames: int = 3) -> bytes:
    """An animated GIF, which is what Outlook writes for a signature banner."""
    import os

    from PIL import Image

    images = [
        Image.frombytes("RGB", (width, height), os.urandom(width * height * 3)).convert("P")
        for _ in range(frames)
    ]
    buffer = io.BytesIO()
    images[0].save(buffer, format="GIF", save_all=True, append_images=images[1:])
    return buffer.getvalue()


def test_a_gif_is_re_encoded_into_a_container_the_service_accepts():
    """`400 InvalidContent: "The file is corrupted or format is unsupported."`

    Twenty-one attachments carried that, and not one of them was corrupt — GIF simply is not on
    Document Intelligence's list. Sending the first frame as PNG is the whole fix, and it was
    confirmed against the live resource: `image004.gif` failed as a GIF and returned text as a PNG.
    """
    from PIL import Image

    original = gif_bytes(300, 200)
    prepared = fit_for_upload(original, limit=10_000_000)

    assert prepared is not original, "a GIF must not be sent as-is; the service refuses it"
    with Image.open(io.BytesIO(prepared)) as image:
        assert image.format in settings.OCR_UPLOAD_FORMATS
        assert image.size == (300, 200), "re-encoding must not resize a correctly sized image"


def test_an_image_below_the_dimension_floor_is_scaled_up_rather_than_refused():
    """`400 InvalidContentDimensions` on a 32x32 icon, which the service will not even look at.

    Scaled up it comes back empty, and empty is the honest answer for an icon — the point is that
    the disposition is then decided by what the image contains rather than by a request that was
    never read.
    """
    from PIL import Image

    prepared = fit_for_upload(png_bytes(32, 32), limit=10_000_000)
    with Image.open(io.BytesIO(prepared)) as image:
        assert min(image.size) >= settings.OCR_MIN_IMAGE_PIXELS


def test_an_image_above_the_dimension_ceiling_is_scaled_down():
    """The same refusal from the other end. A 12000px-wide scan is in range at 10000."""
    from PIL import Image

    prepared = fit_for_upload(png_bytes(12000, 60), limit=50_000_000)
    with Image.open(io.BytesIO(prepared)) as image:
        assert max(image.size) <= settings.OCR_MAX_IMAGE_PIXELS
        assert min(image.size) >= settings.OCR_MIN_IMAGE_PIXELS, (
            "scaling the long side down must not push the short side under the floor")


def test_a_multi_page_tiff_keeps_every_page():
    """The one multi-frame image that must NOT be collapsed to its first frame.

    TIFF is a supported container and the service reads every page of it, so a faxed two-page POD
    goes up whole. Collapsing it the way an animated GIF is collapsed would silently discard the
    page carrying the signature.
    """
    import os

    from PIL import Image

    pages = [Image.frombytes("RGB", (300, 200), os.urandom(300 * 200 * 3)) for _ in range(2)]
    buffer = io.BytesIO()
    pages[0].save(buffer, format="TIFF", save_all=True, append_images=pages[1:])
    original = buffer.getvalue()

    prepared = fit_for_upload(original, limit=10_000_000)
    assert prepared is original, "a supported multi-page container is sent untouched"


def test_an_oversized_scan_is_read_page_by_page_instead_of_refused(client, monkeypatch):
    """Ten of the fourteen attachments refused on size are EMCO bills of lading.

    Refusing them filed real delivery evidence as `service_unavailable`. Split by page, each page
    fits the 4 MB body cap and the document is read — and `pages` carries the count out so
    `BudgetedOcrClient` charges the budget for what Azure actually billed.
    """
    pytest.importorskip("pypdfium2")
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument.new()
    for _ in range(3):
        document.new_page(300, 400)
    buffer = io.BytesIO()
    document.save(buffer)
    pdf = buffer.getvalue()

    monkeypatch.setattr(settings, "OCR_MAX_UPLOAD_BYTES", len(pdf) - 1)
    monkeypatch.setattr(settings, "OCR_MAX_PAGES", 10)

    sent = []
    monkeypatch.setattr(
        client, "_analyze_whole",
        lambda content: sent.append(content) or ocr_adapter.OcrResult(raw_text="PO 212696"))

    result = client.analyze(pdf)

    assert len(sent) == 3, "one call per page"
    assert all(page[:4] != b"%PDF" for page in sent), "pages go up as images, not as PDFs"
    assert result.raw_text.count("PO 212696") == 3
    assert result.pages == 3, "the budget must be charged for three pages, not one"


def test_the_page_split_respects_the_page_cap(client, monkeypatch):
    """`OCR_MAX_PAGES` is the same spend guard the whole-document path applies with `pages=1-N`.

    Without it a 23-page specification — there are two in the backlog — would quietly become a
    23-page bill the moment it crossed the size cap.
    """
    pytest.importorskip("pypdfium2")
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument.new()
    for _ in range(8):
        document.new_page(300, 400)
    buffer = io.BytesIO()
    document.save(buffer)
    pdf = buffer.getvalue()

    monkeypatch.setattr(settings, "OCR_MAX_UPLOAD_BYTES", len(pdf) - 1)
    monkeypatch.setattr(settings, "OCR_MAX_PAGES", 2)
    monkeypatch.setattr(client, "_analyze_whole",
                        lambda content: ocr_adapter.OcrResult(raw_text="x"))

    assert client.analyze(pdf).pages == 2


def test_a_large_format_page_is_clamped_before_it_is_sent(client, monkeypatch):
    """The bug the first live backfill found, and the reason the clamp is shared.

    `fit_for_upload` learned Document Intelligence's dimension range; the PDF page split, added in
    the same change, did not — it compressed each rendered page to fit the byte cap and sent it at
    whatever size 200 DPI produced. Three large-format submittals render to 8398x11198, so every
    page of all three came back `400 InvalidContentDimensions`: the same refusal, reintroduced one
    level down.
    """
    pytest.importorskip("pypdfium2")
    import pypdfium2 as pdfium
    from PIL import Image

    document = pdfium.PdfDocument.new()
    document.new_page(3000, 4000)          # points; at 200 DPI this is far over the ceiling
    buffer = io.BytesIO()
    document.save(buffer)
    pdf = buffer.getvalue()

    monkeypatch.setattr(settings, "OCR_MAX_PAGES", 10)

    sent = []
    monkeypatch.setattr(
        client, "_analyze_whole",
        lambda content: sent.append(content) or ocr_adapter.OcrResult(raw_text="x"))

    # The split is called directly rather than through `analyze`: reaching it that way needs a cap
    # below the PDF's own size, and a cap that small leaves no room for any page to encode under
    # it either. What is under test is what the split sends, not how it is entered.
    client._analyze_pdf_by_page(pdf)

    assert sent, "the page must be sent, not dropped"
    with Image.open(io.BytesIO(sent[0])) as page:
        assert max(page.size) <= settings.OCR_MAX_IMAGE_PIXELS
        assert min(page.size) >= settings.OCR_MIN_IMAGE_PIXELS
