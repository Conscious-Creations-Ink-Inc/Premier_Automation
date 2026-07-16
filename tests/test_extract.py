import io
import os

import pytest
from openpyxl import Workbook
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

from config import settings
from pipeline.stage3_extract import ai_fallback
from pipeline.stage3_extract.base import ExtractionSource, PartialFields
from pipeline.stage3_extract.excel_adapter import EXCEL_CONTENT_TYPE, ExcelAdapter
from pipeline.stage3_extract.freetext_adapter import FreetextAdapter
from pipeline.stage3_extract.html_adapter import HtmlAdapter
from pipeline.stage3_extract.ocr_adapter import (
    MockDocumentIntelligenceClient,
    OcrAdapter,
    OcrResult,
    TesseractDocumentIntelligenceClient,
)
from pipeline.stage3_extract.pdf_adapter import PdfAdapter

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
