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


# --- HtmlAdapter -----------------------------------------------------------

def test_html_clean_warehouse_table_full_confidence():
    html = (
        "<table><tr><th>PO</th><th>Spec</th><th>Description</th><th>Qty</th></tr>"
        "<tr><td>213987</td><td>LI-12</td><td>Lyla Medium Convertible Chandelier</td><td>1</td></tr></table>"
    )
    records = HtmlAdapter().extract(source(body_html=html))
    assert len(records) == 1
    r = records[0]
    assert r.po_number == "213987"
    assert r.spec_code == "LI-12"
    assert r.quantity_received == 1.0
    assert r.extraction_confidence == 1.0
    assert r.extraction_source == "html"


def test_html_split_shipment_sub_specs():
    html_base = "<table><tr><th>PO</th><th>Spec</th><th>Qty</th></tr><tr><td>208491</td><td>STE-402-LT-B</td><td>11</td></tr></table>"
    html_shade = "<table><tr><th>PO</th><th>Spec</th><th>Qty</th></tr><tr><td>208491</td><td>STE-402-LT-SH</td><td>12</td></tr></table>"
    base_records = HtmlAdapter().extract(source(body_html=html_base))
    shade_records = HtmlAdapter().extract(source(body_html=html_shade))
    assert base_records[0].parent_spec_code == "STE-402-LT"
    assert base_records[0].sub_spec_suffix == "B"
    assert shade_records[0].parent_spec_code == "STE-402-LT"
    assert shade_records[0].sub_spec_suffix == "SH"


def test_html_fuzzy_header_matching():
    html = "<table><tr><th>PO</th><th>Spec</th><th>Qty Rcvd</th></tr><tr><td>208491</td><td>LI-1</td><td>4</td></tr></table>"
    records = HtmlAdapter().extract(source(body_html=html))
    assert records[0].quantity_received == 4.0


# --- PdfAdapter --------------------------------------------------------------

def test_pdf_native_table_extraction():
    pdf_bytes = make_pdf_bytes([["PO", "Spec", "Qty"], ["208491", "LI-1", "1"]])
    records = PdfAdapter().extract(source(source_type="attachment", content_type="application/pdf", content_bytes=pdf_bytes))
    assert len(records) == 1
    assert records[0].po_number == "208491"
    assert records[0].extraction_source == "pdf"


# --- FreetextAdapter ----------------------------------------------------------

def test_freetext_po_only_confidence_floored_to_zero():
    # A bare PO mention with nothing else behind it is exactly what the POD-evidence
    # confidence floor exists to catch (see STAGE_3_EXTRACT.md) — supersedes the pipeline's
    # earlier, less careful expectation of a nonzero "low confidence" for this exact case.
    records = FreetextAdapter().extract(source(body_text="the mirrors arrived today, PO 212448"))
    r = records[0]
    assert r.po_number == "212448"
    assert r.spec_code is None
    assert r.quantity_received is None
    assert r.extraction_confidence == 0.0


def test_freetext_po_and_spec_found_survives_the_floor():
    # Once spec_code is *also* found, the floor's condition (spec AND qty AND description
    # all None) is no longer true, so the freetext baseline confidence stands.
    records = FreetextAdapter().extract(source(body_text="PO 212448, spec LI-12, nothing else confirmed"))
    r = records[0]
    assert r.po_number == "212448"
    assert r.spec_code == "LI-12"
    assert r.extraction_confidence == 0.3


def test_freetext_ai_fallback_does_not_invent_fields(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "ENABLE_AI_FALLBACK", True)
    monkeypatch.setattr(ai_fallback, "propose_fields", lambda text: PartialFields(po_number="212448"))

    records = FreetextAdapter().extract(source(body_text="the mirrors arrived today, PO 212448"))
    r = records[0]
    assert r.extraction_source == "freetext+ai"
    assert r.spec_code is None
    assert r.quantity_received is None
    assert r.extraction_confidence <= 0.5


# --- OcrAdapter (mock client) ---------------------------------------------------

def test_ocr_mock_client_returns_table():
    fixture = OcrResult(tables=[[["PO", "Spec", "Qty"], ["208491", "LI-1", "2"]]])
    client = MockDocumentIntelligenceClient(fixture=fixture)
    records = OcrAdapter(client=client).extract(
        source(source_type="attachment", content_type="image/jpeg", content_bytes=b"fake-image-bytes")
    )
    assert len(records) == 1
    assert records[0].po_number == "208491"
    assert records[0].extraction_source == "ocr"
    assert records[0].extraction_confidence <= 0.85


def test_ocr_mock_client_returns_only_unstructured_text():
    fixture = OcrResult(raw_text="PO 212448 received, all good")
    client = MockDocumentIntelligenceClient(fixture=fixture)
    records = OcrAdapter(client=client).extract(
        source(source_type="attachment", content_type="image/jpeg", content_bytes=b"fake-image-bytes")
    )
    assert len(records) == 1
    assert records[0].po_number == "212448"
    assert records[0].extraction_source == "ocr"


def test_ocr_client_failure_both_attempts_logs_service_unavailable():
    client = MockDocumentIntelligenceClient(raise_error=True)
    records = OcrAdapter(client=client).extract(
        source(source_type="attachment", content_type="image/jpeg", content_bytes=b"fake-image-bytes")
    )
    assert len(records) == 1
    assert records[0].comments == "OCR service unavailable"
    assert records[0].extraction_confidence == 0.0


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
    image_bytes = make_pod_image_bytes(["Delivery Confirmation", "PO 213987", "Spec LI-12", "Qty received: 1"])
    source = ExtractionSource(
        source_email_id="msg-ocr-tesseract", email_date="2026-06-08T14:00:00Z",
        source_type="attachment", content_type="image/png", content_bytes=image_bytes,
    )
    records = OcrAdapter(client=TesseractDocumentIntelligenceClient()).extract(source)
    assert len(records) == 1
    r = records[0]
    assert r.po_number == "213987"
    assert r.spec_code == "LI-12"
    assert r.extraction_source == "ocr"


@pytest.mark.skipif(not TESSERACT_AVAILABLE, reason="Tesseract not installed on this machine")
def test_tesseract_client_reads_a_scanned_pdf_via_rasterization():
    # A "photographed POD saved as PDF" (real dummy test samples arrived in exactly this shape:
    # a raster image dropped onto a page, no vector text) has no text layer, so PdfAdapter
    # correctly declines it and OcrAdapter takes over — but the raw PDF bytes aren't an image
    # pytesseract/PIL can open directly, so the client must rasterize each page first.
    image_bytes = make_pod_image_bytes(["Delivery Confirmation", "PO 213987", "Spec LI-12", "Qty received: 1"])
    pdf_bytes = make_pdf_with_embedded_image_bytes(image_bytes)
    assert not _has_text_layer(pdf_bytes)

    records = OcrAdapter(client=TesseractDocumentIntelligenceClient()).extract(
        source(source_type="attachment", content_type="application/pdf", content_bytes=pdf_bytes)
    )
    assert len(records) == 1
    r = records[0]
    assert r.po_number == "213987"
    assert r.spec_code == "LI-12"
    assert r.extraction_source == "ocr"


# --- DocxAdapter (real Word-doc format, found via real dummy test samples) ---

def test_docx_native_table_extraction():
    docx_bytes = make_docx_table_bytes([["PO", "Spec", "Qty"], ["213987", "LI-12", "1"]])
    records = DocxAdapter().extract(
        source(source_type="attachment", content_type=DOCX_CONTENT_TYPE, content_bytes=docx_bytes)
    )
    assert len(records) == 1
    r = records[0]
    assert r.po_number == "213987"
    assert r.spec_code == "LI-12"
    assert r.quantity_received == 1.0
    assert r.extraction_source == "docx"


def test_docx_embedded_image_routes_through_mock_ocr():
    # A photographed POD pasted directly into a Word doc — the shape every real docx sample
    # turned out to be. The image's own content doesn't matter here (the mock client ignores
    # it), only that python-docx can embed and later recover a real image from word/media/.
    image_bytes = make_pod_image_bytes(["irrelevant to the mock"])
    docx_bytes = make_docx_with_image_bytes(image_bytes)
    fixture = OcrResult(tables=[[["PO", "Spec", "Qty"], ["208491", "LI-1", "2"]]])

    records = DocxAdapter(ocr_client=MockDocumentIntelligenceClient(fixture=fixture)).extract(
        source(source_type="attachment", content_type=DOCX_CONTENT_TYPE, content_bytes=docx_bytes)
    )
    assert len(records) == 1
    assert records[0].po_number == "208491"
    assert records[0].extraction_source == "docx"


def test_docx_plain_text_fallback_when_no_table_or_image():
    docx_bytes = make_docx_text_bytes("the mirrors arrived today, PO 212448")
    records = DocxAdapter().extract(
        source(source_type="attachment", content_type=DOCX_CONTENT_TYPE, content_bytes=docx_bytes)
    )
    assert len(records) == 1
    assert records[0].po_number == "212448"
    assert records[0].extraction_source == "docx"


@pytest.mark.skipif(not TESSERACT_AVAILABLE, reason="Tesseract not installed on this machine")
def test_docx_embedded_image_via_real_tesseract():
    image_bytes = make_pod_image_bytes(["Delivery Confirmation", "PO 213987", "Spec LI-12", "Qty received: 1"])
    docx_bytes = make_docx_with_image_bytes(image_bytes)
    records = DocxAdapter(ocr_client=TesseractDocumentIntelligenceClient()).extract(
        source(source_type="attachment", content_type=DOCX_CONTENT_TYPE, content_bytes=docx_bytes)
    )
    assert len(records) == 1
    r = records[0]
    assert r.po_number == "213987"
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
        ["212448", "UNI-100", "5", "Y"],
    ])
    records = ExcelAdapter().extract(
        source(source_type="attachment", content_type=EXCEL_CONTENT_TYPE, content_bytes=xlsx_bytes)
    )
    assert len(records) == 1
    r = records[0]
    assert r.po_number == "212448"
    assert r.quantity_received == 5.0
    assert r.comments == "Confirmed: Y"
    assert r.extraction_source == "excel"
