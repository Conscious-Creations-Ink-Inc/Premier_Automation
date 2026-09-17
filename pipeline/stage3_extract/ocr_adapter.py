import io
import re
import time
from pathlib import Path
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

from config import settings
from pipeline.models import ExtractedRecord
from pipeline.parsing import pod as pod_parser
from pipeline.parsing import sniff
from pipeline.stage3_extract.base import (
    ExtractionAdapter,
    ExtractionSource,
    apply_document_fields,
    build_record_from_row,
    po_column_is_corroborated,
    is_item_row,
    kept_despite_a_misread_spec,
    find_header_row,
    harvest_document_fields,
    states_a_line_item,
    strip_print_chrome,
)
from pipeline.stage3_extract.freetext_adapter import FreetextAdapter

IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/tiff", "image/bmp"}

# Statuses worth trying again. Everything else is the service telling us something about the
# request that a second identical request cannot change — see `_is_retryable`.
RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

class OcrQuotaExhausted(RuntimeError):
    """Azure's own limit: the account has no call volume left this period.

    Deliberately not an `AzureOcrError`. That class means "this request failed and here is what the
    service said"; this one means "the service will not answer any request until the subscription
    changes", which is a different fact with a different remedy — and, like `OcrBudgetExhausted`,
    one that no retry can help with. `_is_retryable` treats both the same for that reason.

    Raised only by `BudgetedOcrClient` once it has latched, so the ledger row for the page that
    actually discovered the exhaustion still carries Azure's own wording.
    """


class OcrBudgetExhausted(RuntimeError):
    """Our own per-run page ceiling, not a failure of Azure's.

    Its own class so `_is_retryable` can tell it from every other `RuntimeError` without matching on
    the message. Retrying it would be asking our spend guard to change its mind; classing *all*
    `RuntimeError` as permanent to achieve that would have silently stopped retrying genuine
    transient faults, which is the opposite of the fix.

    Subclasses `RuntimeError` so `BudgetedOcrClient`'s existing callers keep working unchanged.
    """


_LAST_CALL_AT = [0.0]
"""When the last analyze POST went out, so calls can be paced across every caller.

Module-level rather than per-client because the pacing that matters is against the *resource*'s
calls-per-minute, and `build_default_adapters` hands the same client to several adapters while
`reextract` builds its own. A per-instance clock would let two of them outrun the tier together.
"""


def _log(message: str) -> None:
    print(f"[ocr_adapter] {message}")


class AzureOcrError(Exception):
    """An HTTP failure from Document Intelligence, carrying what Azure actually said.

    `raise_for_status()` produces `"400 Client Error: Bad Request for url: …"` and nothing else,
    and that is all the ledger held for five identical failures. Azure had answered:

        {"error": {"code": "InvalidRequest", "innererror":
                   {"code": "InvalidContentLength",
                    "message": "The input image is too large."}}}

    The file was 4.67 MB against this resource's 4 MB body cap. One line of the response body is
    the difference between "OCR is broken" and "shrink the image", so it is read before the status
    is raised on, and kept here.
    """

    def __init__(self, status: int, code: str, message: str, url: str = "",
                 retry_after: Optional[str] = None):
        self.status = status
        self.code = code
        self.message = message
        self.url = url
        self.retry_after = retry_after
        """Whatever Azure put in the `Retry-After` header, unparsed. It is the service saying how
        long its own throttle lasts, which beats any interval we would invent."""
        detail = f"HTTP {status}"
        if code:
            detail += f" {code}"
        if message:
            detail += f": {message}"
        super().__init__(detail)


def _raise_for_status(response, url: str = "") -> None:
    """Turn a non-2xx into an `AzureOcrError` that has read the body first."""
    if response.status_code < 400:
        return
    code = message = ""
    try:
        error = (response.json() or {}).get("error") or {}
        inner = error.get("innererror") or {}
        # The inner code is the specific one — `InvalidContentLength` rather than `InvalidRequest`
        # — and the inner message is the sentence a person can act on.
        code = inner.get("code") or error.get("code") or ""
        message = inner.get("message") or error.get("message") or ""
    except ValueError:
        message = (response.text or "")[:200]
    raise AzureOcrError(response.status_code, code, message,
                        url or getattr(response, "url", "") or "",
                        (response.headers or {}).get("Retry-After"))


def is_quota_exhausted(exc: Exception) -> bool:
    """Whether this failure means the OCR account has no call volume left this period.

    Distinct from every other failure here, because it is the only one that is true of the
    *account* rather than of the document: retrying it costs a round trip and cannot succeed, and
    neither can the next document, or the six hundredth.

    That distinction was missing, and it cost the ledger 580 rows. Each attachment was tried,
    refused with `HTTP 403: Out of call volume quota`, and filed individually as
    `service_unavailable` — so an exhausted subscription looked like 580 separately broken
    attachments rather than one account-level fact with a date on it.

    Matched on the message as well as the status because 403 alone is ambiguous: a wrong key or a
    firewalled endpoint is also 403, and those are worth retrying against the next document.
    """
    if not isinstance(exc, AzureOcrError):
        return False
    if exc.status != 403:
        return False
    return "out of call volume quota" in f"{exc.code} {exc}".lower()


def _is_retryable(exc: Exception) -> bool:
    """Whether trying the identical request again could plausibly succeed.

    The retry used to catch bare `Exception`, so a 400 was re-sent exactly like a 429 — re-uploading
    4.67 MB for a guaranteed second refusal, and spending one of only two attempts to learn nothing.
    A budget stop was retried too, which is our own guard being asked to change its mind.
    """
    if isinstance(exc, AzureOcrError):
        return exc.status in RETRYABLE_STATUSES
    # A local refusal: a file too big to send that could not be shrunk, or our own spend ceiling.
    # Neither is Azure's to change its mind about. Deliberately narrow — every other `RuntimeError`
    # stays retryable, because "the service misbehaved in a way we did not anticipate" is exactly
    # the case a retry exists for.
    # `OcrQuotaExhausted` belongs here for the same reason as the budget, one step further out:
    # it is raised without contacting Azure at all, so a retry re-asks a decision already taken
    # locally. Left retryable it would multiply the very round trips the latch exists to stop.
    if isinstance(exc, (ValueError, OcrBudgetExhausted, OcrQuotaExhausted)):
        return False
    # Timeouts, connection resets and the SSL EOF that dropped an 11.6 MB upload mid-flight.
    return True


def _retry_after_seconds(exc: Exception, attempt: int) -> float:
    """How long to wait, preferring what Azure asked for over what we guessed.

    `connectors/mailbox.py` has kept this contract for Graph since the day a 429 cost a whole
    cycle there; its docstring calls obeying `Retry-After` "the difference between a two-second
    pause and a lost cycle". The OCR path never did, and 77% of its failures are 429s.
    """
    header = getattr(exc, "retry_after", None)
    if header:
        try:
            return min(float(header), settings.AZURE_OCR_RETRY_MAX_SECONDS)
        except (TypeError, ValueError):
            pass
    # Exponential, with jitter so several callers backing off together do not resynchronise.
    import random

    base = settings.AZURE_OCR_RETRY_BACKOFF_SECONDS * (2 ** attempt)
    return min(base + random.uniform(0, base / 2), settings.AZURE_OCR_RETRY_MAX_SECONDS)


def _open_image(content_bytes: bytes):
    """The image behind these bytes, or None if Pillow cannot read them as one.

    A PDF, a corrupt file and a Word document all land here on the way to OCR; none of them is
    something to re-encode, and all of them must come back out untouched for the caller to refuse
    or forward honestly.
    """
    try:
        from PIL import Image
    except ImportError:                                            # pragma: no cover
        return None
    try:
        image = Image.open(io.BytesIO(content_bytes))
        image.load()
        return image
    except Exception:                                              # noqa: BLE001 — not an image
        return None


def _clamp_to_dimension_range(image):
    """Scale an image into Document Intelligence's 50-10000 pixel range, or return it unchanged.

    Shared by `fit_for_upload` and the PDF page split, because the second needed it and did not
    have it: three large-format submittals render at 8398x11198 at 200 DPI, and every page of all
    three came back `400 InvalidContentDimensions` — the same refusal the whole-file path had just
    been taught to prevent, reintroduced one level down.

    Both bounds are checked after scaling for the first, so squaring up a long thin page cannot
    push its short side back under the floor.
    """
    width, height = image.size
    smallest, largest = min(width, height), max(width, height)
    if smallest >= settings.OCR_MIN_IMAGE_PIXELS and largest <= settings.OCR_MAX_IMAGE_PIXELS:
        return image

    from PIL import Image

    scale = 1.0
    if smallest < settings.OCR_MIN_IMAGE_PIXELS:
        scale = settings.OCR_MIN_IMAGE_PIXELS / smallest
    if largest * scale > settings.OCR_MAX_IMAGE_PIXELS:
        scale = settings.OCR_MAX_IMAGE_PIXELS / largest
    resized = image.resize(
        (max(settings.OCR_MIN_IMAGE_PIXELS, round(width * scale)),
         max(settings.OCR_MIN_IMAGE_PIXELS, round(height * scale))),
        Image.LANCZOS)
    _log(f"scaled {width}x{height} to {resized.width}x{resized.height} into the "
         f"{settings.OCR_MIN_IMAGE_PIXELS}-{settings.OCR_MAX_IMAGE_PIXELS}px range")
    return resized


def _encode(image, limit: int) -> bytes:
    """The smallest acceptable encoding of `image` that fits under `limit`.

    PNG first, because it is lossless and every image small enough for it to win is one where the
    text is thin — a screenshot, a pasted table, a logo — and JPEG ringing on thin text is exactly
    what an OCR pass then has to read through. JPEG only once PNG will not fit, stepping the scale
    and the quality down together the way the old loop did.
    """
    from PIL import Image

    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    if buffer.tell() <= limit:
        return buffer.getvalue()

    for scale in (1.0, 0.75, 0.5, 0.35, 0.25):
        candidate = image
        if scale < 1.0:
            candidate = image.resize(
                (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                Image.LANCZOS)
        for quality in (85, 70, 55):
            buffer = io.BytesIO()
            candidate.save(buffer, format="JPEG", quality=quality, optimize=True)
            if buffer.tell() <= limit:
                _log(f"downscaled to {buffer.tell()} bytes "
                     f"({candidate.width}x{candidate.height}, q{quality}) to fit the upload cap")
                return buffer.getvalue()
    return b""


def fit_for_upload(content_bytes: bytes, limit: Optional[int] = None) -> bytes:
    """Make an image something Document Intelligence will look at, or return it unchanged.

    Three separate refusals, all of which describe the *request* rather than the file, and all of
    which are correctable here:

    1. **Too large.** The resource caps a request body at 4 MB. A phone photo of a signed POD is
       routinely larger — the one on PO 914711 is 4.67 MB at 5712x4284 — and refusing it loses the
       only evidence that delivery has. Halving the linear size costs nothing OCR can read.
    2. **Unsupported container** (`400 InvalidContent`). GIF is not on the service's list, and
       Outlook writes GIFs for signature banners; twenty-one reached OCR and all came back as
       "corrupted or format is unsupported". Only the first frame is a document, so that is what
       gets re-encoded. This also normalises `MPO` — an iPhone multi-picture JPEG carrying extra
       frames after the primary one, likewise not on the list, and a second independent reason
       PO 914711's photo was refused.
    3. **Out of dimension range** (`400 InvalidContentDimensions`). The service reads between 50
       and 10000 pixels a side. Both ends are scaled into range rather than refused, so what
       decides the disposition is the image's contents and not a request never looked at.

    Anything Pillow cannot open — a PDF above the cap — comes back untouched, for the caller to
    handle. There is no lossless way to shrink it here; `AzureDocumentIntelligenceClient` splits
    those by page instead.

    Returning the bytes unchanged where nothing needed doing is deliberate: the overwhelming
    majority of attachments are already acceptable, and re-encoding them would spend CPU to make
    the OCR result marginally worse.
    """
    limit = limit or settings.OCR_MAX_UPLOAD_BYTES
    image = _open_image(content_bytes)
    if image is None:
        return content_bytes

    fmt = (image.format or "").upper()
    width, height = image.size
    too_big = len(content_bytes) > limit
    # Deliberately not "has more than one frame". A multi-page TIFF is a supported container and
    # Document Intelligence reads every page of it; collapsing it to the first frame here would
    # silently discard pages two onward of a faxed POD. Only an unsupported container is rewritten,
    # and `MPO` — the iPhone multi-picture JPEG — is unsupported by name, so it is already covered.
    wrong_format = fmt not in settings.OCR_UPLOAD_FORMATS
    smallest, largest = min(width, height), max(width, height)
    too_small = smallest < settings.OCR_MIN_IMAGE_PIXELS
    too_wide = largest > settings.OCR_MAX_IMAGE_PIXELS

    if not (too_big or wrong_format or too_small or too_wide):
        return content_bytes

    if wrong_format and getattr(image, "n_frames", 1) > 1:
        # An animated GIF signature banner: the first frame carries whatever text there is and the
        # rest are the animation. Seeking is also what makes a GIF's palette resolve before the
        # conversion below. Guarded on `wrong_format` so a multi-page TIFF never reaches here.
        image.seek(0)

    image = _clamp_to_dimension_range(image)

    prepared = _encode(image, limit)
    if not prepared:
        # Every scale and quality step still overshot. The original is no smaller, but it is at
        # least the file the sender actually sent, and the caller refuses it by size with a
        # message naming the real number.
        return content_bytes
    if wrong_format:
        _log(f"re-encoded {fmt or 'unknown'} to a supported container for upload")
    return prepared


@dataclass
class OcrResult:
    tables: List[List[List[str]]] = field(default_factory=list)  # each table: list of rows, each row a list of cells
    raw_text: str = ""
    confidence: float = 0.0
    pages: int = 1
    """How many pages the service was billed for producing this.

    Document Intelligence charges per page, and `BudgetedOcrClient` counted one per `analyze()`
    call — so a 10-page scanned bill of lading drew ten pages of Premier's allowance and one unit
    of ours. That undercount is how a 150-page budget could walk into the tier's monthly ceiling
    with the meter reading a third full.

    One for an image, which is always a single page, and the real count for a PDF.
    """


class DocumentIntelligenceClient(ABC):
    @abstractmethod
    def analyze(self, content_bytes: bytes) -> OcrResult: ...


class CachingOcrClient(DocumentIntelligenceClient):
    """Reads each distinct document once, however many attachments carry it.

    Mail is enormously repetitive and the ledger says so plainly: the 533 attachments waiting on
    OCR are **227 distinct blobs**. One signature banner accounts for thirty of them, one EMCO
    bill of lading for two. Reading by ledger row therefore paid Azure 2.3 times over for the same
    pages — and, because the page budget is finite, a run that spent itself on repeats left
    genuinely unread documents still unread behind it.

    Keyed on the SHA-256 of the bytes as submitted, so it is a statement about the document rather
    than about the attachment: the same photograph forwarded through four hops of a thread hits the
    cache three times, and two different files never share an entry.

    **Failures are cached too, and deliberately.** A GIF the service refuses as an unsupported
    container will be refused identically the next 29 times; re-asking spends a round trip and the
    tier's rate allowance to be told the same thing. What is *not* cached is the account-level
    quota failure — `BudgetedOcrClient` latches that separately, and it is not a fact about any
    document.

    In memory, for the life of one run. A cache that survived runs would need a table, and a
    table is a schema change; this is where the repetition actually is.
    """

    def __init__(self, inner: DocumentIntelligenceClient):
        self.inner = inner
        self.hits = 0
        self.misses = 0
        self._results: dict = {}
        self._failures: dict = {}

    def analyze(self, content_bytes: bytes) -> OcrResult:
        import hashlib

        key = hashlib.sha256(content_bytes).hexdigest()
        if key in self._results:
            self.hits += 1
            return self._results[key]
        if key in self._failures:
            self.hits += 1
            raise self._failures[key]

        try:
            result = self.inner.analyze(content_bytes)
        except (OcrQuotaExhausted, OcrBudgetExhausted):
            # Neither is a fact about this document — one is the subscription and one is our own
            # ceiling — so neither is cached against it. Raising a budget stop back at a blob whose
            # turn came after the budget closed would make the stop permanent for the whole run,
            # and it would still be there after the budget was raised.
            raise
        except Exception as exc:                                   # noqa: BLE001
            self.misses += 1
            self._failures[key] = exc
            raise
        self.misses += 1
        self._results[key] = result
        return result


class MockDocumentIntelligenceClient(DocumentIntelligenceClient):
    """Reads a pre-set fixture response instead of calling anything real — fast, deterministic
    unit tests. Set `.fixture` (an OcrResult) or `.raise_error=True` to simulate a failure."""

    def __init__(self, fixture: Optional[OcrResult] = None, raise_error: bool = False):
        self.fixture = fixture if fixture is not None else OcrResult()
        self.raise_error = raise_error

    def analyze(self, content_bytes: bytes) -> OcrResult:
        if self.raise_error:
            raise RuntimeError("Simulated Azure Document Intelligence failure")
        return self.fixture


class TesseractDocumentIntelligenceClient(DocumentIntelligenceClient):
    """Runs local Tesseract OCR against a real image — a dev/test convenience to try the
    pipeline against real sample content before Azure credentials exist. NOT the production
    path (that's RealDocumentIntelligenceClient / Azure, per Premier's confirmation) — purely
    a local, fully-under-our-control testing aid. See STAGE_3_EXTRACT.md."""

    def __init__(self):
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = settings.TESSERACT_CMD_PATH
        self._pytesseract = pytesseract

    def analyze(self, content_bytes: bytes) -> OcrResult:
        from PIL import Image

        if content_bytes[:4] == b"%PDF":
            # A scanned/photographed POD saved as PDF has no text layer (that's exactly why
            # OcrAdapter routed it here — see _has_text_layer) but pdfplumber can still
            # rasterize each page to a real image for Tesseract, same as Azure Document
            # Intelligence would read the PDF's pages directly in production.
            import pdfplumber
            texts = []
            with pdfplumber.open(io.BytesIO(content_bytes)) as pdf:
                for page in pdf.pages:
                    image = page.to_image(resolution=200).original
                    texts.append(self._pytesseract.image_to_string(image))
            text = "\n".join(texts)
        else:
            image = Image.open(io.BytesIO(content_bytes))
            text = self._pytesseract.image_to_string(image)
        # Tesseract has no real table-structure detection — deliberately returns no tables,
        # so OcrAdapter correctly falls through to free-text extraction over the raw OCR text,
        # exercising that path with genuinely messy real text instead of a hand-typed stand-in.
        return OcrResult(tables=[], raw_text=text, confidence=0.5)


class AzureVisionClient(DocumentIntelligenceClient):
    """Azure AI Vision — the Image Analysis `read` feature, called over REST.

    Complete and ready; it needs only `AZURE_VISION_ENDPOINT` and `AZURE_VISION_KEY`, which are
    blank in `.env` until Premier provisions the resource. While they are blank `build_client`
    resolves to the mock, so nothing here runs and photographed PODs route to a person.

    Deliberately no SDK: one authenticated POST against `/computervision/imageanalysis:analyze`
    is the whole contract, and `requests` is already a dependency.

    **Known limit, and it is the reason to keep this swappable:** Vision's Read returns text
    lines with bounding boxes, *not* table structure. That is sufficient for a carrier POD —
    `parsing/pod.py` recovers delivery date, carrier, tracking and signature from running text —
    but a photographed packing-slip *table* comes back as loose lines with no column alignment.
    If those turn out to be common, Azure AI Document Intelligence's prebuilt-layout model is
    the better service and slots in behind this same interface.

    A PDF cannot be posted to the Image Analysis endpoint, so scanned PDFs are rasterised
    page-by-page first, exactly as the Tesseract client does.
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: Optional[int] = None,
    ):
        self.endpoint = (endpoint or settings.AZURE_VISION_ENDPOINT or "").rstrip("/")
        self.api_key = api_key or settings.AZURE_VISION_KEY
        self.timeout = timeout or settings.AZURE_VISION_TIMEOUT_SECONDS
        if not self.endpoint or not self.api_key:
            raise ValueError(
                "AzureVisionClient needs AZURE_VISION_ENDPOINT and AZURE_VISION_KEY. Both are "
                "blank until the Azure AI Vision resource is provisioned — set them in .env."
            )

    def analyze(self, content_bytes: bytes) -> OcrResult:
        if content_bytes[:4] == b"%PDF":
            return self._analyze_pdf(content_bytes)
        return OcrResult(tables=[], raw_text=self._read_image(content_bytes), confidence=0.8)

    def _analyze_pdf(self, content_bytes: bytes) -> OcrResult:
        import pdfplumber

        texts = []
        with pdfplumber.open(io.BytesIO(content_bytes)) as pdf:
            for page in pdf.pages[: settings.OCR_MAX_PAGES]:
                buffer = io.BytesIO()
                page.to_image(resolution=200).original.save(buffer, format="PNG")
                texts.append(self._read_image(buffer.getvalue()))
        return OcrResult(tables=[], raw_text="\n".join(texts), confidence=0.8)

    def _read_image(self, image_bytes: bytes) -> str:
        import requests

        if len(image_bytes) > settings.OCR_MAX_IMAGE_BYTES:
            raise ValueError(
                f"image is {len(image_bytes)} bytes, above Azure Vision's "
                f"{settings.OCR_MAX_IMAGE_BYTES}-byte limit"
            )
        response = requests.post(
            f"{self.endpoint}/computervision/imageanalysis:analyze",
            params={"api-version": settings.AZURE_VISION_API_VERSION, "features": "read"},
            headers={
                "Ocp-Apim-Subscription-Key": self.api_key,
                "Content-Type": "application/octet-stream",
            },
            data=image_bytes,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return _text_from_vision_response(response.json())


def _text_from_vision_response(payload: dict) -> str:
    """Flatten Vision's block/line structure to text, one line per line, blocks separated.

    Line order is reading order, which is what the POD grammar's label/value patterns assume.
    """
    blocks = (payload.get("readResult") or {}).get("blocks") or []
    chunks = []
    for block in blocks:
        lines = [line.get("text", "") for line in block.get("lines", [])]
        if lines:
            chunks.append("\n".join(lines))
    return "\n\n".join(chunks)


class AzureDocumentIntelligenceClient(DocumentIntelligenceClient):
    """Azure AI Document Intelligence — the production OCR path, called over REST.

    This is what Premier's "premier" resource (eastus) actually serves; a Vision Image Analysis
    call against the same endpoint 401s, which is why `AzureVisionClient` below is kept only for
    a genuine Vision resource. DocInt is the better service for this corpus regardless:

    - `prebuilt-layout` returns **table structure**, so a photographed packing slip's columns
      survive and `records_from_ocr_result` can take its table path instead of falling through
      to free text. Vision's Read could only ever return loose lines.
    - It accepts a **PDF directly** — no page-by-page rasterisation, so a scanned multi-page POD
      is one call, not one call per page.

    Analyze is asynchronous: POST returns 202 with an `operation-location`, which is polled until
    the status leaves `running`. Polling GETs are not billed; only the initial POST is, and it is
    billed per page — hence the `pages` cap below, which is the spend guard.

    Deliberately no SDK: two authenticated HTTP calls are the whole contract and `requests` is
    already a dependency.
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
    ):
        self.endpoint = (endpoint or settings.AZURE_DOC_INTELLIGENCE_ENDPOINT or "").rstrip("/")
        self.api_key = api_key or settings.AZURE_DOC_INTELLIGENCE_KEY
        self.model = model or settings.AZURE_DOC_INTELLIGENCE_MODEL
        self.timeout = timeout or settings.AZURE_VISION_TIMEOUT_SECONDS
        if not self.endpoint or not self.api_key:
            raise ValueError(
                "AzureDocumentIntelligenceClient needs AZURE_DOC_INTELLIGENCE_ENDPOINT and "
                "AZURE_DOC_INTELLIGENCE_KEY (or the AZURE_VISION_* pair they fall back to) — "
                "set them in .env."
            )

    def analyze(self, content_bytes: bytes) -> OcrResult:
        # Shrink before measuring: an oversized *image* is a thing we can fix, and refusing it
        # loses the delivery it is evidence of.
        content_bytes = fit_for_upload(content_bytes)
        if len(content_bytes) > settings.OCR_MAX_UPLOAD_BYTES:
            if content_bytes[:4] == b"%PDF":
                # A scan too large to post whole is still readable a page at a time, and these are
                # the documents most worth reading: of the fourteen refused on size, ten are EMCO
                # bills of lading and one is a warehouse receiving report's damage photographs.
                # Refusing them outright filed real delivery evidence as "the OCR service is down".
                return self._analyze_pdf_by_page(content_bytes)
            raise ValueError(
                f"document is {len(content_bytes)} bytes, above the "
                f"{settings.OCR_MAX_UPLOAD_BYTES}-byte upload limit, and is not an image that "
                f"could be scaled down to fit"
            )
        return self._analyze_whole(content_bytes)

    def _analyze_pdf_by_page(self, content_bytes: bytes) -> OcrResult:
        """Rasterise an oversized PDF and read it page by page, merging the results.

        Splitting rather than compressing, because the size is in the page images themselves —
        these are scans — so there is nothing to strip out without destroying what OCR must read.

        Billed per page either way: Document Intelligence charges by page, so N page-calls cost
        what the whole document would have. `OcrResult.pages` carries the count out so
        `BudgetedOcrClient` charges the budget the same N and the spend guard stays honest.

        `OCR_MAX_PAGES` caps the read at the same place the whole-document path caps it with its
        `pages=1-N` parameter, so a 23-page specification does not quietly become a 23-page bill.
        """
        try:
            import pypdfium2 as pdfium
        except ImportError:                                        # pragma: no cover
            raise ValueError(
                f"document is {len(content_bytes)} bytes, above the "
                f"{settings.OCR_MAX_UPLOAD_BYTES}-byte upload limit, and pypdfium2 is not "
                f"installed to split it by page"
            )

        try:
            document = pdfium.PdfDocument(content_bytes)
        except Exception as exc:                                   # noqa: BLE001
            # Too big to post and not a PDF anything can open. Refused by size, in the file's own
            # terms — the reason a reviewer needs is "this document is 11 MB", not "PDFium: Data
            # format error", and certainly not "the OCR service is unavailable".
            raise ValueError(
                f"document is {len(content_bytes)} bytes, above the "
                f"{settings.OCR_MAX_UPLOAD_BYTES}-byte upload limit, and could not be opened to "
                f"split by page ({type(exc).__name__})"
            )

        texts: List[str] = []
        tables: List[List[List[str]]] = []
        confidences: List[float] = []
        pages_read = 0
        try:
            count = min(len(document), settings.OCR_MAX_PAGES)
            _log(f"{len(content_bytes)} bytes is over the upload cap — reading "
                 f"{count} of {len(document)} page(s) individually")
            for index in range(count):
                page = document[index]
                # 200 DPI is what the Tesseract and Vision clients already rasterise at, and it is
                # the resolution a scanned delivery note's text survives; the scale factor is
                # relative to PDF's own 72 DPI user space.
                bitmap = page.render(scale=200 / 72)
                # Clamped, not just compressed. A large-format submittal renders to 8398x11198 at
                # 200 DPI — over the service's 10000-pixel ceiling — and three of them failed every
                # page with `InvalidContentDimensions` until this line existed.
                image = _clamp_to_dimension_range(bitmap.to_pil())
                prepared = _encode(image, settings.OCR_MAX_UPLOAD_BYTES)
                if not prepared:
                    _log(f"page {index + 1} would not fit the upload cap at any scale — skipped")
                    continue
                result = self._analyze_whole(prepared)
                pages_read += 1
                if result.raw_text:
                    texts.append(result.raw_text)
                tables.extend(result.tables)
                if result.confidence:
                    confidences.append(result.confidence)
        finally:
            document.close()

        return OcrResult(
            tables=tables,
            raw_text="\n".join(texts),
            confidence=(sum(confidences) / len(confidences)) if confidences else 0.0,
            pages=max(1, pages_read),
        )

    def _analyze_whole(self, content_bytes: bytes) -> OcrResult:
        """One document, one POST, polled to completion. The single billed call."""
        import requests

        params = {"api-version": settings.AZURE_DOC_INTELLIGENCE_API_VERSION}
        if content_bytes[:4] == b"%PDF":
            # Caps billed pages on a long scan. Images are always one page, so the parameter is
            # only meaningful — and only accepted without complaint — for PDFs.
            params["pages"] = f"1-{settings.OCR_MAX_PAGES}"

        url = f"{self.endpoint}/documentintelligence/documentModels/{self.model}:analyze"
        self._pace()
        response = requests.post(
            url,
            params=params,
            headers={
                "Ocp-Apim-Subscription-Key": self.api_key,
                "Content-Type": "application/octet-stream",
            },
            data=content_bytes,
            timeout=self.timeout,
        )
        _raise_for_status(response, url)
        operation_url = response.headers.get("operation-location")
        if not operation_url:
            raise RuntimeError("Document Intelligence accepted the document but returned no "
                               "operation-location to poll")
        return _ocr_result_from_docint(self._poll(operation_url))

    @staticmethod
    def _pace() -> None:
        """Hold the floor between submissions so a serial loop cannot outrun the tier.

        The calls were never concurrent — and still overran the quota, because a loop over an
        email's inline images fires them back to back and each analysis costs a POST plus several
        poll GETs against the same allowance. Rate limiting was 77% of every OCR failure recorded.
        """
        gap = settings.OCR_MIN_SECONDS_BETWEEN_CALLS
        if gap <= 0:
            return
        waited = time.monotonic() - _LAST_CALL_AT[0]
        if 0 <= waited < gap:
            time.sleep(gap - waited)
        _LAST_CALL_AT[0] = time.monotonic()

    def _poll(self, operation_url: str) -> dict:
        """Wait for the result, retrying the *same* operation rather than re-submitting.

        This is where twelve analyses were lost. A 429 on the poll used to propagate out of
        `analyze`, and the retry wrapper answered it by POSTing the whole document again — so
        Premier was billed for an analysis Azure had already accepted, the finished result was
        abandoned at a URL nobody went back to, and the load landed on the endpoint that had just
        asked us to slow down.

        The result is sitting at `operation_url` either way. Being throttled while collecting it
        is a reason to wait, never a reason to start again.
        """
        import requests

        deadline = time.monotonic() + settings.AZURE_DOC_INTELLIGENCE_POLL_TIMEOUT_SECONDS
        throttled = 0
        while True:
            try:
                result = requests.get(
                    operation_url,
                    headers={"Ocp-Apim-Subscription-Key": self.api_key},
                    timeout=self.timeout,
                )
                _raise_for_status(result, operation_url)
            except AzureOcrError as exc:
                if not _is_retryable(exc) or time.monotonic() >= deadline:
                    raise
                throttled += 1
                wait = _retry_after_seconds(exc, throttled)
                _log(f"poll throttled ({exc}); waiting {wait:.1f}s for the result already paid for")
                time.sleep(wait)
                continue

            payload = result.json()
            status = (payload.get("status") or "").lower()
            if status == "succeeded":
                return payload.get("analyzeResult") or {}
            if status == "failed":
                error = (payload.get("error") or {}).get("message", "no message")
                raise RuntimeError(f"Document Intelligence analysis failed: {error}")
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Document Intelligence still '{status}' after "
                    f"{settings.AZURE_DOC_INTELLIGENCE_POLL_TIMEOUT_SECONDS}s"
                )
            time.sleep(settings.AZURE_DOC_INTELLIGENCE_POLL_SECONDS)


SELECTION_MARK_RE = re.compile(r":(?:un)?selected:", re.IGNORECASE)
"""Document Intelligence renders every checkbox as a `:selected:` / `:unselected:` token,
inline in the cell beside it. On a receiving report the tick next to an item lands inside the
item, and `ST650` reached the store as the spec code `ST650
:selected:` — a value that
matches no purchase order line and never will. It carries nothing the record fields model, so
it comes out before anything reads them."""


def strip_selection_marks(text: str) -> str:
    """`text` with the checkbox tokens removed and the whitespace they left tidied."""
    return " ".join(SELECTION_MARK_RE.sub(" ", text or "").split())


def _ocr_result_from_docint(analyze_result: dict) -> OcrResult:
    """Flatten a DocInt analyzeResult into the OcrResult the adapters already understand.

    Text comes from `pages[].lines[]` rather than the flat `content` string, because the line
    breaks are what the POD grammar's label/value patterns key off — `content` on a
    multi-column form runs labels and values together.
    """
    pages = analyze_result.get("pages") or []

    chunks = []
    for page in pages:
        lines = [line.get("content", "") for line in (page.get("lines") or [])]
        if lines:
            chunks.append("\n".join(lines))
    raw_text = "\n\n".join(chunks) or (analyze_result.get("content") or "")

    tables = [_grid_from_docint_table(table) for table in (analyze_result.get("tables") or [])]

    confidences = [
        word["confidence"]
        for page in pages
        for word in (page.get("words") or [])
        if isinstance(word.get("confidence"), (int, float))
    ]
    mean = sum(confidences) / len(confidences) if confidences else 0.5
    confidence = min(mean, settings.AZURE_DOC_INTELLIGENCE_CONFIDENCE_CAP)

    return OcrResult(tables=[t for t in tables if t], raw_text=raw_text, confidence=confidence,
                     # What Azure says it read, which is what Azure bills for.
                     pages=max(1, len(pages)))


def _grid_from_docint_table(table: dict) -> List[List[str]]:
    """DocInt returns a table as a flat cell list carrying row/column indices; rebuild the grid.

    Cells that span rows or columns appear once, at their origin — the rest of the span stays
    empty, which is the honest representation: `map_headers` reads row 0 and a merged header
    genuinely does not label the columns it visually covers.
    """
    rows = table.get("rowCount") or 0
    columns = table.get("columnCount") or 0
    if not rows or not columns:
        return []
    grid = [["" for _ in range(columns)] for _ in range(rows)]
    for cell in table.get("cells") or []:
        row, column = cell.get("rowIndex"), cell.get("columnIndex")
        if row is None or column is None or row >= rows or column >= columns:
            continue
        grid[row][column] = strip_selection_marks(cell.get("content") or "")
    return grid


# Kept under its old name so existing imports and tests keep resolving. It now points at the
# Document Intelligence client, which is what the name always claimed.
RealDocumentIntelligenceClient = AzureDocumentIntelligenceClient


def build_client(preference: Optional[str] = None) -> DocumentIntelligenceClient:
    """Resolve the OCR client: `auto` | `azure` | `vision` | `tesseract` | `mock`.

    `azure` is Document Intelligence — the production path, and what Premier's resource serves.
    `vision` is the Image Analysis client, kept for a Vision-or-multi-service resource.

    `auto` prefers Document Intelligence when it is configured, falls back to Tesseract when the
    binary is present, and otherwise returns the mock — so a machine with no OCR at all still
    runs, and photographed evidence lands on the exception queue instead of failing the pass.
    """
    choice = (preference or settings.OCR_CLIENT or "auto").lower()

    if choice == "mock":
        return MockDocumentIntelligenceClient()
    if choice == "azure":
        return AzureDocumentIntelligenceClient()
    if choice == "vision":
        return AzureVisionClient()
    if choice == "tesseract":
        return TesseractDocumentIntelligenceClient()

    if settings.AZURE_DOC_INTELLIGENCE_ENDPOINT and settings.AZURE_DOC_INTELLIGENCE_KEY:
        try:
            return AzureDocumentIntelligenceClient()
        except Exception as e:
            _log(f"Azure Document Intelligence unavailable ({e}); falling back")
    try:
        client = TesseractDocumentIntelligenceClient()
        if Path(settings.TESSERACT_CMD_PATH).exists():
            return client
    except Exception:
        pass
    _log("no OCR client configured — images and scans will be routed for human review")
    return MockDocumentIntelligenceClient()


class OcrServiceUnavailable(Exception):
    """The OCR service could not be reached or refused us. The file is fine.

    Raised rather than swallowed, and raised rather than turned into a record, because both of
    those were tried and both hid an outage. Until 2026-08-25 `analyze_with_retry` returned `None`
    and `OcrAdapter.extract` answered with a one-element placeholder record; `dispatch`'s
    `if records:` found that truthy and wrote `disposition='extracted'`, so every image that failed
    during an Azure 401 outage was recorded as a successful extraction. Nothing could then name
    what to re-run.

    Returning `[]` instead would only have downgraded the lie to `empty` ("we read it and it held
    nothing"). An exception is the one answer `dispatch` already knows how to record faithfully.

    `service` and `detail` are carried so the ledger row says *which* dependency failed and why —
    that is what a person reads before deciding whether re-running is worth anything yet.
    """

    def __init__(self, service: str, detail: str, error_type: str = ""):
        self.service = service
        self.detail = detail
        self.error_type = error_type or "OcrServiceUnavailable"
        """What actually went wrong, for the ledger's queryable column — `TooManyRequests`,
        `InvalidContentLength`. Defaults to this class's own name, which is what every row used to
        say regardless of cause."""
        super().__init__(f"{service}: {detail}")


class OcrAdapter(ExtractionAdapter):
    def __init__(self, client: Optional[DocumentIntelligenceClient] = None):
        self.client = client or MockDocumentIntelligenceClient()

    def can_handle(self, source: ExtractionSource) -> bool:
        """Claim by byte sniff, not by declared content type.

        The corpus's five photographed PODs (`IMG_2479.jpeg` and siblings, ~3 MB each) arrive
        with `mimetype=None`, so the previous content-type allowlist rejected every one of them
        — the OCR path had never actually seen a real photo (finding C13). Sniffing also covers
        HEIC, which phones now produce by default.
        """
        if source.source_type != "attachment" or source.content_bytes is None:
            return False
        kind = sniff.sniff(source.content_bytes, source.filename or "", source.content_type or "").kind
        if kind == sniff.KIND_IMAGE:
            return True
        if kind == sniff.KIND_PDF:
            from pipeline.stage3_extract.pdf_adapter import _has_text_layer
            return not _has_text_layer(source.content_bytes)   # a scan, not a native PDF
        return False

    def extract(self, source: ExtractionSource) -> List[ExtractedRecord]:
        """Records from the OCR read. Raises `OcrServiceUnavailable` if the service would not
        answer — deliberately, so the ledger records a failure rather than a fabricated success."""
        result = self._analyze_with_retry(source.content_bytes, source.source_email_id)
        return records_from_ocr_result(source, result, extraction_source="ocr")

    def _analyze_with_retry(self, content_bytes: bytes, email_id: str) -> OcrResult:
        return analyze_with_retry(self.client, content_bytes, email_id)


def analyze_with_retry(
    client: DocumentIntelligenceClient, content_bytes: bytes, email_id: str
) -> OcrResult:
    """Shared retry wrapper so any adapter that needs to OCR embedded image content (not just
    OcrAdapter's own direct attachments — see DocxAdapter) gets the same retry/give-up behavior.

    **Raises `OcrServiceUnavailable` when every attempt fails.** It used to return `None` and log
    to stdout, which meant an outage left no trace anywhere a query could reach — see that
    exception's docstring. The caller is expected to let it propagate to `dispatch`, which records
    it against the attachment.
    """
    attempts = settings.AZURE_OCR_RETRY_COUNT + 1
    for attempt in range(attempts):
        try:
            return client.analyze(content_bytes)
        except Exception as e:
            # The *underlying* client, not whatever is wrapping it. `BudgetedOcrClient` sits in
            # front of the real one, and a ledger row reading "BudgetedOcrClient unavailable" names
            # our own spend guard rather than the dependency that actually failed.
            service = type(getattr(client, "inner", client)).__name__
            last = attempt >= attempts - 1

            # A refusal that a second identical request cannot change. Re-sending 4.67 MB to be
            # told "the input image is too large" a second time costs an attempt, a paid call and
            # the wait between them, and teaches nobody anything.
            if not _is_retryable(e):
                _log(f"OCR refused {email_id} and will not be retried: {e}")
                raise OcrServiceUnavailable(service, _detail_for(e), _error_type_for(e)) from e

            if not last:
                wait = _retry_after_seconds(e, attempt)
                _log(f"OCR attempt {attempt + 1}/{attempts} for {email_id} failed ({e}); "
                     f"retrying in {wait:.1f}s")
                time.sleep(wait)
                continue

            _log(f"OCR service unavailable for {email_id} after {attempts} attempts: {e}")
            raise OcrServiceUnavailable(service, _detail_for(e), _error_type_for(e)) from e
    raise OcrServiceUnavailable(
        type(getattr(client, "inner", client)).__name__, "no attempt was made")


def _detail_for(exc: Exception) -> str:
    """The sentence that lands on the ledger row.

    For an `AzureOcrError` that is Azure's own words — "InvalidContentLength: The input image is
    too large." — instead of the status line `raise_for_status` used to produce, which named the
    URL and nothing about what was wrong with the request.
    """
    if isinstance(exc, AzureOcrError):
        return str(exc)[:300]
    return f"{type(exc).__name__}: {exc}"[:300]


_STATUS_NAMES = {400: "BadRequest", 401: "Unauthorized", 403: "Forbidden", 413: "PayloadTooLarge",
                 415: "UnsupportedMediaType", 429: "TooManyRequests", 500: "ServerError",
                 503: "ServiceUnavailable"}


def _error_type_for(exc: Exception) -> str:
    """A short, queryable name for what went wrong."""
    if isinstance(exc, AzureOcrError):
        return exc.code or _STATUS_NAMES.get(exc.status) or f"HTTP{exc.status}"
    return type(exc).__name__


_USE_OCR_CONFIDENCE_CAP = object()


def records_from_ocr_result(
    source: ExtractionSource, result: OcrResult, extraction_source: str = "ocr",
    confidence_cap=_USE_OCR_CONFIDENCE_CAP,
) -> List[ExtractedRecord]:
    """Turns one OcrResult into ExtractedRecords — shared so DocxAdapter's embedded-image path
    gets identical table/fallback/confidence-cap handling to OcrAdapter's own attachment path.

    `confidence_cap` is the OCR ceiling by default, because every caller in the pipeline is
    reading a picture of a document and no such read deserves full confidence. `None` lifts it,
    for a caller replaying tables that came from a text layer rather than from a reader — see
    `tools/replay_parse.py`. Passing it explicitly keeps that a decision at the call site instead
    of something inferred from a label.
    """
    # Everything the reader saw, kept before this function narrows it to record fields. OCR is the
    # path where the loss was worst: Azure returns clean table structure and only the tables whose
    # headers `map_headers` recognises survive the loop below. See `ExtractionSource.parsed_text`.
    source.parsed_text = result.raw_text or ""
    source.parsed_tables = [[[str(c) for c in row] for row in table] for table in result.tables]

    # Read the form's labelled bands once, before the item grid, exactly as `PdfAdapter` does.
    # A receiving report states the delivery date, who signed for it, the carrier and the tracking
    # number **once**, above the items — so `build_record_from_row` never sees any of it. Skipping
    # this was why every OCR record came out missing `pod_stated_date`, one of the five fields
    # `completeness` requires, while `Date Received | 8/14/26` sat two tables up the same page.
    document_fields = harvest_document_fields(result.tables)

    records = []
    for table in result.tables:
        if not table or len(table) < 2:
            continue
        # `find_header_row` rather than `map_headers(table[0])`, for two reasons. A form's item
        # header is often not row 0, and — the sharper one — it applies `is_usable_column_map`,
        # which demands an identity column plus one more. Row 0 alone accepted a freight bill's
        # `Qty | Pkg | HM | Description | Weight` band and staged "2 PLT RACK MOUNTS AND
        # ACCESSORIES" as goods received, alongside a liftgate charge and a prepaid total.
        located = find_header_row(table)
        if located is None:
            continue
        header_index, column_map = located
        # Asked of the whole grid before any row is read — see `po_column_is_corroborated`. A scan
        # is where this matters most: OCR is the reason a mirrored purchase order can arrive with a
        # mangled digit, and blanking the cell hands the line to whichever order's event runs.
        po_column_verified = po_column_is_corroborated(
            [[strip_selection_marks(c) for c in r] for r in table[header_index + 1:]],
            column_map, source.known_po_numbers)
        for row in table[header_index + 1:]:
            # A total, an address block or a label row is not an item, whatever it contains —
            # `Shipping Address: … PO 900101 …` reads as a purchase order to the regex fallback
            # and would otherwise survive the "names a PO or a spec" guard below.
            if not is_item_row(row):
                continue
            # Stripped again here, not only in `_grid_from_docint_table`, so a grid that reached
            # us any other way — a parse replayed out of `parsed_documents`, a fixture, a client
            # that does its own flattening — is cleaned too. A tick fused onto a spec code splits
            # one delivered item into two records that no longer look like each other.
            cells = [strip_selection_marks(cell) for cell in row]
            record = build_record_from_row(source, cells, column_map, extraction_source,
                                           po_column_verified=po_column_verified)
            # A grid runs on past its last item into totals and continuation lines. A record
            # naming neither a purchase order nor a spec identifies nothing and can never become
            # a receipt, so it is dropped here — inside the adapter, before the delivery event
            # stamps its own PO onto it and makes the junk look attributable.
            if not (record.po_number or record.spec_code
                    or kept_despite_a_misread_spec(record, cells, column_map)):
                continue
            # A scan adds a failure mode a clean PDF does not have: the reader returns the
            # *margin* as rows of the grid. Handwritten queries beside the Atlas item table came
            # back as three rows reading `PGR-905-EQ ?`, `? STE-901-EQ?` and `PO#212749.` — each
            # one a spec-shaped cell with no quantity and no description beside it, and each one
            # staged as a delivered line against a real purchase order. A row that says neither
            # how many nor what is not a line item.
            if not states_a_line_item(record):
                continue
            records.append(apply_document_fields(record, document_fields))

    if not records and result.raw_text:
        # A photographed POD or BOL is a labelled form, not a table, so it never survives the
        # table pass. Reading it with the carrier-POD grammar recovers the delivery date,
        # signature and reference line that the free-text pass below would miss entirely.
        document = pod_parser.parse_pod(strip_print_chrome(result.raw_text))
        if document is not None and (document.po_numbers or document.delivery_date):
            from pipeline.stage3_extract.pdf_adapter import records_from_pod
            records = records_from_pod(document, source)
            for r in records:
                r.extraction_source = f"{extraction_source}:carrier_pod"

    if not records and not (result.raw_text or "").strip():
        # OCR responded and returned nothing at all — which is what the default Mock client
        # does, and what a real client does on an unreadable photo. There is nothing to
        # extract, so nothing is emitted. Fabricating an empty record here staged one bogus
        # row per pallet photo (six of them on one corpus message), each of which would have
        # to be dismissed by hand in the exception queue.
        _log(f"OCR returned no text and no tables for {source.source_email_id} ({source.filename})")
        return []

    if not records:
        # Azure/OCR responded fine, it just found no recognizable table — a genuinely
        # different case from "the service was unavailable," so it must never reuse that
        # reason. Falls through to free-text regex over whatever raw text exists (often
        # nothing at all for a true photographed image, per STAGE_3_EXTRACT.md).
        text_source = ExtractionSource(
            source_email_id=source.source_email_id, email_date=source.email_date,
            source_type="body", body_text=strip_print_chrome(result.raw_text),
            ledger_id=source.ledger_id, known_po_numbers=source.known_po_numbers,
        )
        records = FreetextAdapter().extract(text_source)
        for r in records:
            r.extraction_source = extraction_source

    if confidence_cap is _USE_OCR_CONFIDENCE_CAP:
        confidence_cap = settings.AZURE_DOC_INTELLIGENCE_CONFIDENCE_CAP
    if confidence_cap is not None:
        for r in records:
            r.extraction_confidence = min(r.extraction_confidence, confidence_cap)

    return records
