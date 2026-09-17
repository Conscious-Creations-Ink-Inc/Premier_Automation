import io
import os

import docx
import pytest
from openpyxl import Workbook
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.platypus import Image as PdfImage
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

from config import settings
from pipeline.stage3_extract import ai_fallback
from pipeline.stage3_extract.base import ExtractionSource, PartialFields
from pipeline.stage3_extract.docx_adapter import DOCX_CONTENT_TYPE, DocxAdapter
from pipeline.stage3_extract.excel_adapter import EXCEL_CONTENT_TYPE, ExcelAdapter
from pipeline.stage3_extract.freetext_adapter import FreetextAdapter
from pipeline.stage3_extract.html_adapter import HtmlAdapter
from pipeline.stage3_extract.ocr_adapter import (
    MockDocumentIntelligenceClient,
    OcrAdapter,
    OcrResult,
    OcrServiceUnavailable,
    TesseractDocumentIntelligenceClient,
)
from pipeline.stage3_extract.pdf_adapter import PdfAdapter, _has_text_layer

TESSERACT_AVAILABLE = os.path.exists(settings.TESSERACT_CMD_PATH)


def source(**overrides):
    defaults = dict(source_email_id="msg-1", email_date="2026-06-08T14:00:00Z", source_type="body")
    defaults.update(overrides)
    return ExtractionSource(**defaults)


def make_pdf_bytes(rows):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter)
    table = Table(rows)
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    doc.build([table])
    return buf.getvalue()


def make_pdf_with_embedded_image_bytes(image_bytes, width=400, height=200):
    """A PDF with a raster image drawn on the page and no real text layer — the 'photographed
    POD saved/printed as PDF' shape, as opposed to make_pdf_bytes' native vector-text table."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter)
    doc.build([PdfImage(io.BytesIO(image_bytes), width=width, height=height)])
    return buf.getvalue()


def make_docx_table_bytes(rows):
    document = docx.Document()
    table = document.add_table(rows=len(rows), cols=len(rows[0]))
    for i, row in enumerate(rows):
        for j, cell_text in enumerate(row):
            table.cell(i, j).text = str(cell_text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def make_docx_with_image_bytes(image_bytes, paragraph_text="Delivery photo attached"):
    document = docx.Document()
    document.add_paragraph(paragraph_text)
    document.add_picture(io.BytesIO(image_bytes))
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def make_docx_text_bytes(text):
    document = docx.Document()
    document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def make_xlsx_bytes(rows):
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --- Shared regex helpers (base.py) — hardened after real dummy test documents -------------

def test_quantity_regex_does_not_misread_a_date_as_a_fraction():
    # Regression guard: real PDF samples were browser prints of scanned PODs, and the print
    # header's date ("7/18/26") was being misread as a quantity fraction ("7 of 18"). A date has
    # three slash-separated numbers; a real quantity fraction only ever has two.
    from pipeline.stage3_extract.base import regex_extract_fields
    assert regex_extract_fields("7/18/26, 1:27 AM").quantity_received is None
    assert regex_extract_fields("received 11 of 12 chairs").quantity_received == 11.0
    assert regex_extract_fields("shipped 11/12 units").quantity_received == 11.0


def test_spec_regex_matches_a_letter_fused_onto_the_numeric_segment():
    # Regression guard: real sample raw_sample_03 has spec code "GR-350a-WTF" — a real spec code
    # whose numeric segment has a trailing letter, which the original digits-only pattern missed.
    from pipeline.stage3_extract.base import regex_extract_fields
    assert regex_extract_fields("Main Drapery Fabric GR-350a-WTF POD").spec_code == "GR-350a-WTF"
    assert regex_extract_fields("spec LI-12, nothing else").spec_code == "LI-12"


def test_strip_print_chrome_removes_boilerplate_but_keeps_real_content():
    from pipeline.stage3_extract.base import strip_print_chrome
    raw = "7/18/26, 1:27 AM\nfile:///C:/fake/pdf_sample_01.html 1/1\nPO 913987 Spec LI-12 Qty 1"
    cleaned = strip_print_chrome(raw)
    assert "7/18/26" not in cleaned
    assert "file:///" not in cleaned
    assert "PO 913987 Spec LI-12 Qty 1" in cleaned


# --- HtmlAdapter -----------------------------------------------------------

def test_html_clean_warehouse_table_full_confidence():
    html = (
        "<table><tr><th>PO</th><th>Spec</th><th>Description</th><th>Qty</th></tr>"
        "<tr><td>913987</td><td>LI-12</td><td>Lyla Medium Convertible Chandelier</td><td>1</td></tr></table>"
    )
    records = HtmlAdapter().extract(source(body_html=html))
    assert len(records) == 1
    r = records[0]
    assert r.po_number == "913987"
    assert r.spec_code == "LI-12"
    assert r.quantity_received == 1.0
    assert r.extraction_confidence == 1.0
    assert r.extraction_source == "html"


def test_html_split_shipment_sub_specs():
    html_base = "<table><tr><th>PO</th><th>Spec</th><th>Qty</th></tr><tr><td>908491</td><td>STE-402-LT-B</td><td>11</td></tr></table>"
    html_shade = "<table><tr><th>PO</th><th>Spec</th><th>Qty</th></tr><tr><td>908491</td><td>STE-402-LT-SH</td><td>12</td></tr></table>"
    base_records = HtmlAdapter().extract(source(body_html=html_base))
    shade_records = HtmlAdapter().extract(source(body_html=html_shade))
    assert base_records[0].parent_spec_code == "STE-402-LT"
    assert base_records[0].sub_spec_suffix == "B"
    assert shade_records[0].parent_spec_code == "STE-402-LT"
    assert shade_records[0].sub_spec_suffix == "SH"


def test_html_fuzzy_header_matching():
    html = "<table><tr><th>PO</th><th>Spec</th><th>Qty Rcvd</th></tr><tr><td>908491</td><td>LI-1</td><td>4</td></tr></table>"
    records = HtmlAdapter().extract(source(body_html=html))
    assert records[0].quantity_received == 4.0


# --- PdfAdapter --------------------------------------------------------------

def test_pdf_native_table_extraction():
    pdf_bytes = make_pdf_bytes([["PO", "Spec", "Qty"], ["908491", "LI-1", "1"]])
    records = PdfAdapter().extract(source(source_type="attachment", content_type="application/pdf", content_bytes=pdf_bytes))
    assert len(records) == 1
    assert records[0].po_number == "908491"
    assert records[0].extraction_source == "pdf"


def test_pdf_browser_print_chrome_is_not_meaningful_text():
    # Regression guard for a real bug found via real dummy test documents: every real PDF
    # sample was a browser "print to PDF" of a scanned/photographed POD, and its only text was
    # the browser's own print header (a timestamp line, and a file:// path + page number line —
    # the exact shape found in the real samples). That trivial boilerplate was making
    # _has_text_layer wrongly report True, so PdfAdapter grabbed it as "content" instead of
    # correctly declining and routing to OcrAdapter.
    from pipeline.stage3_extract.base import is_meaningful_text
    chrome_text = "7/18/26, 1:27 AM\nfile:///C:/Users/DELL/AppData/Local/Temp/pwrap/pdf_sample_01.html 1/1"
    assert not is_meaningful_text(chrome_text)
    assert is_meaningful_text("PO 913987 Spec LI-12 Qty 1")


def test_pdf_with_only_print_chrome_and_an_embedded_image_routes_to_ocr():
    # End-to-end version of the same regression: a PDF whose only vector text is browser print
    # chrome, with the real content as an embedded raster image, must be declined by PdfAdapter
    # so OcrAdapter (not PdfAdapter) is the one that ends up reading the actual image.
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas as pdf_canvas
    image_bytes = make_pod_image_bytes(["Delivery Confirmation", "PO 913987", "Spec LI-12", "Qty received: 1"])
    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=letter)
    c.drawString(50, 800, "7/18/26, 1:27 AM")
    c.drawString(50, 785, "file:///C:/fake/path/pdf_sample_01.html 1/1")
    c.drawImage(ImageReader(io.BytesIO(image_bytes)), 50, 400, width=400, height=200)
    c.save()
    pdf_bytes = buf.getvalue()

    assert not _has_text_layer(pdf_bytes)
    assert not PdfAdapter().can_handle(
        source(source_type="attachment", content_type="application/pdf", content_bytes=pdf_bytes)
    )


# --- FreetextAdapter ----------------------------------------------------------

def test_freetext_po_only_yields_no_record_at_all():
    # A bare PO mention with nothing else behind it used to be staged with its confidence floored
    # to zero. Flooring it was the right instinct and the wrong remedy: the row still reached the
    # manual queue, where nobody could finish it, because it names no item and no quantity.
    # 151 such records are in the live store, every one at confidence 0.0 and none completable.
    # `states_a_delivery` now needs two of {purchase order, item, quantity}, so this states none.
    assert FreetextAdapter().extract(source(body_text="the mirrors arrived today, PO 912448")) == []


def test_freetext_po_with_a_quantity_is_a_delivery():
    # The counterpart, so the gate is pinned from both sides: say how many arrived and it reads.
    records = FreetextAdapter().extract(
        source(body_text="the mirrors arrived today, PO 912448 - 11 EA"))
    assert len(records) == 1
    assert records[0].po_number == "912448"
    assert records[0].quantity_received == 11.0


def test_freetext_po_and_spec_found_survives_the_floor():
    # Once spec_code is *also* found, the floor's condition (spec AND qty AND description
    # all None) is no longer true, so the freetext baseline confidence stands.
    records = FreetextAdapter().extract(source(body_text="PO 912448, spec LI-12, nothing else confirmed"))
    r = records[0]
    assert r.po_number == "912448"
    assert r.spec_code == "LI-12"
    assert r.extraction_confidence == 0.3


def test_freetext_ai_fallback_does_not_invent_fields(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "ENABLE_AI_FALLBACK", True)
    monkeypatch.setattr(ai_fallback, "propose_fields", lambda text: PartialFields(po_number="912448"))

    # A proposal of a purchase order and nothing else is not a delivery, whoever made it. The
    # fallback is held to exactly the same standard as the regex pass it stands in for.
    assert FreetextAdapter().extract(source(body_text="the mirrors arrived today, PO 912448")) == []

    monkeypatch.setattr(ai_fallback, "propose_fields",
                        lambda text: PartialFields(po_number="912448", quantity_received=11.0))
    records = FreetextAdapter().extract(source(body_text="the mirrors arrived today, PO 912448"))
    r = records[0]
    assert r.extraction_source == "freetext+ai"
    assert r.spec_code is None, "the fallback must not invent an item it was not given"
    assert r.extraction_confidence <= 0.5


# --- OcrAdapter (mock client) ---------------------------------------------------

def test_ocr_mock_client_returns_table():
    fixture = OcrResult(tables=[[["PO", "Spec", "Qty"], ["908491", "LI-1", "2"]]])
    client = MockDocumentIntelligenceClient(fixture=fixture)
    records = OcrAdapter(client=client).extract(
        source(source_type="attachment", content_type="image/jpeg", content_bytes=b"fake-image-bytes")
    )
    assert len(records) == 1
    assert records[0].po_number == "908491"
    assert records[0].extraction_source == "ocr"
    assert records[0].extraction_confidence <= 0.85


def test_ocr_mock_client_returns_only_unstructured_text():
    # States a quantity as well as the purchase order: a PO mentioned alone is not a delivery,
    # and `states_a_delivery` refuses it — see `test_freetext_po_only_yields_no_record_at_all`.
    fixture = OcrResult(raw_text="PO 912448 received, 11 EA all good")
    client = MockDocumentIntelligenceClient(fixture=fixture)
    records = OcrAdapter(client=client).extract(
        source(source_type="attachment", content_type="image/jpeg", content_bytes=b"fake-image-bytes")
    )
    assert len(records) == 1
    assert records[0].po_number == "912448"
    assert records[0].extraction_source == "ocr"


def test_ocr_client_failure_raises_rather_than_inventing_a_record():
    """An outage must reach `dispatch`, which is the only thing that can record it truthfully.

    This used to assert the opposite — that `extract` answered with a placeholder record carrying
    `comments="OCR service unavailable"`. That one-element list is truthy, so
    `dispatch._read_leaf`'s `if records:` wrote `disposition='extracted'`, `records_extracted=1`,
    `error_type=NULL`. Every image that failed during the Azure 401 outage was filed as a success,
    and after the key was fixed nothing could name what to re-run.
    """
    client = MockDocumentIntelligenceClient(raise_error=True)
    with pytest.raises(OcrServiceUnavailable) as caught:
        OcrAdapter(client=client).extract(
            source(source_type="attachment", content_type="image/jpeg",
                   content_bytes=b"fake-image-bytes")
        )
    # The reason travels with it — the ledger row is written from these two fields.
    assert caught.value.service == "MockDocumentIntelligenceClient"
    assert caught.value.detail


def test_an_ocr_outage_is_recorded_as_service_unavailable_not_extracted(tmp_path):
    """End to end: a failing OCR client leaves a ledger row a person can find and a tool can select.

    The three properties that were all false before, and that together make an outage recoverable:
    the disposition names the cause, `error_type` is set, and the row reaches the manual queue.
    """
    import sqlite3
    from pipeline import attachment_ledger, state_db
    from pipeline.models import Attachment, RawEmail
    from pipeline.stage3_extract import dispatch

    # Real PNG bytes: `OcrAdapter.can_handle` claims by byte sniff, not by declared content type,
    # so a placeholder string would simply not be claimed and the row would read `no_adapter`.
    png = make_pod_image_bytes(["POD"])

    conn = state_db.get_connection(tmp_path / "s.sqlite3")
    email = RawEmail(email_id="<outage@x>", received_at="2026-08-25T00:00:00Z",
                     sender_address="a@b.com", sender_domain="b.com", subject="photo",
                     body_html=None, body_text=None,
                     attachments=[Attachment(filename="pod.png", content_type="image/png",
                                             content_bytes=png, size_bytes=len(png))])
    attachment_ledger.observe(conn, email, None, "2026-08-25T00:00:00Z")
    ledger_id = email.attachments[0].ledger_id

    src = ExtractionSource(source_email_id=email.email_id, email_date=email.received_at,
                           source_type="attachment", filename="pod.png",
                           content_type="image/png", content_bytes=png,
                           ledger_id=ledger_id)
    records = dispatch.dispatch_source(
        conn, src, [OcrAdapter(client=MockDocumentIntelligenceClient(raise_error=True))],
        now="2026-08-25T00:00:00Z")

    assert records == [], "a failed read must not manufacture a record"

    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM attachment_ledger WHERE id = ?", (ledger_id,)).fetchone()
    assert row["disposition"] == attachment_ledger.SERVICE_UNAVAILABLE
    assert row["records_extracted"] == 0
    # The *specific* failure, not the wrapper class. Every row used to read "OcrServiceUnavailable"
    # whatever went wrong, so a throttle could not be told from a rejected file without reading a
    # hundred detail strings by eye.
    assert row["error_type"] == "RuntimeError"
    assert "MockDocumentIntelligenceClient" in row["disposition_detail"]
    # `NEEDS_ATTENTION` membership is what puts it in front of a person and stamps review_status.
    assert row["review_status"] == attachment_ledger.REVIEW_PENDING
    assert ledger_id in [r.id for r in attachment_ledger.list_needing_attention(conn)]
    conn.close()


# --- OcrAdapter (real Tesseract client, dev/test convenience only) -----------

def make_pod_image_bytes(lines, font_size=28):
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.truetype("arial.ttf", font_size)
    except OSError:
        font = ImageFont.load_default()
    img = Image.new("RGB", (900, 80 + 60 * len(lines)), color="white")
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((20, 20 + 60 * i), line, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.skipif(not TESSERACT_AVAILABLE, reason="Tesseract not installed on this machine")
def test_tesseract_client_reads_a_real_rendered_image():
    # Local Tesseract has no table-structure detection (see ocr_adapter.py), so a clean POD
    # photo falls through OCR -> raw text -> FreetextAdapter, same as a real photographed
    # delivery note would. This is a dev/test convenience, never the production OCR path
    # (that's Azure Document Intelligence, RealDocumentIntelligenceClient) — see STAGE_3_EXTRACT.md.
    image_bytes = make_pod_image_bytes(["Delivery Confirmation", "PO 913987", "Spec LI-12", "Qty received: 1"])
    source = ExtractionSource(
        source_email_id="msg-ocr-tesseract", email_date="2026-06-08T14:00:00Z",
        source_type="attachment", content_type="image/png", content_bytes=image_bytes,
    )
    records = OcrAdapter(client=TesseractDocumentIntelligenceClient()).extract(source)
    assert len(records) == 1
    r = records[0]
    assert r.po_number == "913987"
    assert r.spec_code == "LI-12"
    assert r.extraction_source == "ocr"


@pytest.mark.skipif(not TESSERACT_AVAILABLE, reason="Tesseract not installed on this machine")
def test_tesseract_client_reads_a_scanned_pdf_via_rasterization():
    # A "photographed POD saved as PDF" (real dummy test samples arrived in exactly this shape:
    # a raster image dropped onto a page, no vector text) has no text layer, so PdfAdapter
    # correctly declines it and OcrAdapter takes over — but the raw PDF bytes aren't an image
    # pytesseract/PIL can open directly, so the client must rasterize each page first.
    image_bytes = make_pod_image_bytes(["Delivery Confirmation", "PO 913987", "Spec LI-12", "Qty received: 1"])
    pdf_bytes = make_pdf_with_embedded_image_bytes(image_bytes)
    assert not _has_text_layer(pdf_bytes)

    records = OcrAdapter(client=TesseractDocumentIntelligenceClient()).extract(
        source(source_type="attachment", content_type="application/pdf", content_bytes=pdf_bytes)
    )
    assert len(records) == 1
    r = records[0]
    assert r.po_number == "913987"
    assert r.spec_code == "LI-12"
    assert r.extraction_source == "ocr"


# --- DocxAdapter (real Word-doc format, found via real dummy test samples) ---

def test_docx_native_table_extraction():
    docx_bytes = make_docx_table_bytes([["PO", "Spec", "Qty"], ["913987", "LI-12", "1"]])
    records = DocxAdapter().extract(
        source(source_type="attachment", content_type=DOCX_CONTENT_TYPE, content_bytes=docx_bytes)
    )
    assert len(records) == 1
    r = records[0]
    assert r.po_number == "913987"
    assert r.spec_code == "LI-12"
    assert r.quantity_received == 1.0
    assert r.extraction_source == "docx"


def test_docx_embedded_image_routes_through_mock_ocr():
    # A photographed POD pasted directly into a Word doc — the shape every real docx sample
    # turned out to be. The image's own content doesn't matter here (the mock client ignores
    # it), only that python-docx can embed and later recover a real image from word/media/.
    image_bytes = make_pod_image_bytes(["irrelevant to the mock"])
    docx_bytes = make_docx_with_image_bytes(image_bytes)
    fixture = OcrResult(tables=[[["PO", "Spec", "Qty"], ["908491", "LI-1", "2"]]])

    records = DocxAdapter(ocr_client=MockDocumentIntelligenceClient(fixture=fixture)).extract(
        source(source_type="attachment", content_type=DOCX_CONTENT_TYPE, content_bytes=docx_bytes)
    )
    assert len(records) == 1
    assert records[0].po_number == "908491"
    assert records[0].extraction_source == "docx"


def test_docx_plain_text_fallback_when_no_table_or_image():
    docx_bytes = make_docx_text_bytes("the mirrors arrived today, PO 912448 - 11 EA")
    records = DocxAdapter().extract(
        source(source_type="attachment", content_type=DOCX_CONTENT_TYPE, content_bytes=docx_bytes)
    )
    assert len(records) == 1
    assert records[0].po_number == "912448"
    assert records[0].extraction_source == "docx"


@pytest.mark.skipif(not TESSERACT_AVAILABLE, reason="Tesseract not installed on this machine")
def test_docx_embedded_image_via_real_tesseract():
    image_bytes = make_pod_image_bytes(["Delivery Confirmation", "PO 913987", "Spec LI-12", "Qty received: 1"])
    docx_bytes = make_docx_with_image_bytes(image_bytes)
    records = DocxAdapter(ocr_client=TesseractDocumentIntelligenceClient()).extract(
        source(source_type="attachment", content_type=DOCX_CONTENT_TYPE, content_bytes=docx_bytes)
    )
    assert len(records) == 1
    r = records[0]
    assert r.po_number == "913987"
    assert r.spec_code == "LI-12"
    assert r.extraction_source == "docx"


# --- FreetextAdapter must not claim attachments (see ingest_orchestrator.run_adapters) -------

def test_freetext_adapter_does_not_claim_attachments():
    # Regression guard: FreetextAdapter used to return can_handle=True unconditionally, so an
    # attachment type no adapter recognized was silently mislabeled as an empty extraction
    # (po_number="", extraction_source="freetext") instead of correctly yielding nothing.
    unrecognized = source(source_type="attachment", content_type="application/zip", content_bytes=b"whatever")
    assert FreetextAdapter().can_handle(unrecognized) is False
    assert FreetextAdapter().can_handle(source(body_text="hello")) is True


# --- ExcelAdapter ------------------------------------------------------------

def test_excel_tracker_with_confirmed_column():
    xlsx_bytes = make_xlsx_bytes([
        ["PO", "Spec", "Qty", "Confirmed Y/N"],
        ["912448", "UNI-100", "5", "Y"],
    ])
    records = ExcelAdapter().extract(
        source(source_type="attachment", content_type=EXCEL_CONTENT_TYPE, content_bytes=xlsx_bytes)
    )
    assert len(records) == 1
    r = records[0]
    assert r.po_number == "912448"
    assert r.quantity_received == 5.0
    # A confirmation column routes the sheet through the confirmation-grid path, which records
    # the answer in words rather than echoing the raw cell — "Y", "yes" and "Confirmed" all
    # normalise to the same statement, and a "no" is recorded just as explicitly.
    assert r.comments == "property/vendor confirmed receipt"
    assert r.extraction_confidence == 0.85
    assert r.extraction_source.startswith("excel:")   # suffixed with the sheet name


# --- The whole parse is kept, not just the fields we happen to map today -----

def test_the_full_parse_is_stored_beside_the_records(tmp_path):
    """A reader's output must survive being narrowed to `ExtractedRecord`.

    Measured on an Atlas Logistics Warehouse Receiving Report: Azure returned nine clean tables
    carrying `Customer PO # 913994`, `Received by T.Reed`, `BOL/PRO ED6 96925779`,
    `Total Received 2 pallets` and a 22-row line-item grid. `build_record_from_row` mapped one
    table onto six fields and dropped the other eight, so the email reached a person as "nothing
    extractable" while the data sat in a response we had already paid for and thrown away.
    """
    import sqlite3
    from pipeline import attachment_ledger, parsed_documents, state_db
    from pipeline.models import Attachment, RawEmail
    from pipeline.stage3_extract import dispatch

    png = make_pod_image_bytes(["PO 912448"])
    fixture = OcrResult(
        tables=[[["Customer PO #", "Received by"], ["913994", "T.Reed"]],
                [["Qty", "Description"], ["7", "SORRENTO LOUNGE"]]],
        raw_text="WAREHOUSE RECEIVING REPORT\nRR 911798-5\nCustomer PO # 913994",
        confidence=0.9)

    conn = state_db.get_connection(tmp_path / "s.sqlite3")
    email = RawEmail(email_id="<rr@x>", received_at="2026-08-25T00:00:00Z",
                     sender_address="a@b.com", sender_domain="b.com", subject="RR 911798",
                     body_html=None, body_text=None,
                     attachments=[Attachment(filename="RR.png", content_type="image/png",
                                             content_bytes=png, size_bytes=len(png))])
    attachment_ledger.observe(conn, email, None, "2026-08-25T00:00:00Z")
    ledger_id = email.attachments[0].ledger_id

    src = ExtractionSource(source_email_id=email.email_id, email_date=email.received_at,
                           source_type="attachment", filename="RR.png", content_type="image/png",
                           content_bytes=png, ledger_id=ledger_id)
    dispatch.dispatch_source(
        conn, src, [OcrAdapter(client=MockDocumentIntelligenceClient(fixture=fixture))],
        now="2026-08-25T00:00:00Z")

    stored = parsed_documents.get(conn, ledger_id)
    assert stored is not None, "the parse was not kept"
    assert stored.table_count == 2, "every table survives, not only the ones we map"
    assert "RR 911798-5" in stored.raw_text

    # The point of keeping it: a field no extractor claims today is still answerable.
    cells = stored.cells()
    assert cells["Customer PO #"] == "913994"
    assert cells["Received by"] == "T.Reed"

    # And it is JSON, for whatever asks next.
    import json
    assert json.loads(json.dumps(stored.payload()))["tables"][0][1] == ["913994", "T.Reed"]
    conn.close()


def test_storing_the_parse_never_costs_us_the_extraction():
    """Evidence is worth a lot, but not the read it was captured alongside."""
    from pipeline.stage3_extract import dispatch

    src = ExtractionSource(source_email_id="m1", email_date="2026-08-25T00:00:00Z",
                           source_type="attachment", ledger_id=1)
    src.parsed_text = "something"

    class Exploding:
        def execute(self, *a, **k):
            raise RuntimeError("disk on fire")
        def commit(self):
            raise RuntimeError("disk on fire")

    dispatch._keep_the_parse(Exploding(), src, "PdfAdapter", "2026-08-25T00:00:00Z")
