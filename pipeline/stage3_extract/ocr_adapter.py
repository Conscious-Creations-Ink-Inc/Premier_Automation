import io
import time
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
    build_record_from_row,
    map_headers,
    strip_print_chrome,
)
from pipeline.stage3_extract.freetext_adapter import FreetextAdapter

IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/tiff", "image/bmp"}


def _log(message: str) -> None:
    print(f"[ocr_adapter] {message}")


@dataclass
class OcrResult:
    tables: List[List[List[str]]] = field(default_factory=list)  # each table: list of rows, each row a list of cells
    raw_text: str = ""
    confidence: float = 0.0


class DocumentIntelligenceClient(ABC):
    @abstractmethod
    def analyze(self, content_bytes: bytes) -> OcrResult: ...


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


class RealDocumentIntelligenceClient(DocumentIntelligenceClient):
    """Actual Azure AI Document Intelligence call — not implemented until Premier's Azure
    resource/key exist. See ourDocs/JOE_FOLLOWUPS_CHECKLIST.md."""

    def __init__(self, endpoint: Optional[str] = None, api_key: Optional[str] = None):
        self.endpoint = endpoint or settings.AZURE_DOC_INTELLIGENCE_ENDPOINT
        self.api_key = api_key or settings.AZURE_DOC_INTELLIGENCE_KEY

    def analyze(self, content_bytes: bytes) -> OcrResult:
        raise NotImplementedError(
            "RealDocumentIntelligenceClient requires a provisioned Azure Document Intelligence "
            "resource endpoint + key — see ourDocs/JOE_FOLLOWUPS_CHECKLIST.md."
        )


def _empty_ocr_failure_record(source: ExtractionSource) -> ExtractedRecord:
    return ExtractedRecord(
        source_email_id=source.source_email_id,
        po_number="", shipment_number=None, spec_code=None, parent_spec_code=None,
        sub_spec_suffix=None, item_description=None, vendor_name=None, carrier_name=None,
        tracking_number=None, quantity_received=None, unit_of_measure=None, pod_stated_date=None,
        email_date=source.email_date, delivery_location=None,
        comments="OCR service unavailable", extraction_source="ocr",
        extraction_confidence=0.0, raw_snippet="OCR service unavailable",
    )


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
        result = self._analyze_with_retry(source.content_bytes, source.source_email_id)
        if result is None:
            return [_empty_ocr_failure_record(source)]
        return records_from_ocr_result(source, result, extraction_source="ocr")

    def _analyze_with_retry(self, content_bytes: bytes, email_id: str) -> Optional[OcrResult]:
        return analyze_with_retry(self.client, content_bytes, email_id)


def analyze_with_retry(
    client: DocumentIntelligenceClient, content_bytes: bytes, email_id: str
) -> Optional[OcrResult]:
    """Shared retry wrapper so any adapter that needs to OCR embedded image content (not just
    OcrAdapter's own direct attachments — see DocxAdapter) gets the same retry/give-up behavior."""
    attempts = settings.AZURE_OCR_RETRY_COUNT + 1
    for attempt in range(attempts):
        try:
            return client.analyze(content_bytes)
        except Exception as e:
            if attempt < attempts - 1:
                time.sleep(settings.AZURE_OCR_RETRY_BACKOFF_SECONDS)
                continue
            _log(f"OCR service unavailable for {email_id} after {attempts} attempts: {e}")
            return None
    return None


def records_from_ocr_result(
    source: ExtractionSource, result: OcrResult, extraction_source: str = "ocr"
) -> List[ExtractedRecord]:
    """Turns one OcrResult into ExtractedRecords — shared so DocxAdapter's embedded-image path
    gets identical table/fallback/confidence-cap handling to OcrAdapter's own attachment path."""
    records = []
    for table in result.tables:
        if not table or len(table) < 2:
            continue
        column_map = map_headers(table[0])
        if not column_map:
            continue
        for row in table[1:]:
            records.append(build_record_from_row(source, row, column_map, extraction_source))

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
        )
        records = FreetextAdapter().extract(text_source)
        for r in records:
            r.extraction_source = extraction_source

    for r in records:
        r.extraction_confidence = min(r.extraction_confidence, settings.AZURE_DOC_INTELLIGENCE_CONFIDENCE_CAP)

    return records
