import io
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

from config import settings
from pipeline.models import ExtractedRecord
from pipeline.stage3_extract.base import ExtractionAdapter, ExtractionSource, build_record_from_row, map_headers
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
        if source.source_type != "attachment" or source.content_bytes is None:
            return False
        if source.content_type in IMAGE_CONTENT_TYPES:
            return True
        if source.content_type == "application/pdf":
            from pipeline.stage3_extract.pdf_adapter import _has_text_layer
            return not _has_text_layer(source.content_bytes)  # only if PdfAdapter found nothing
        return False

    def extract(self, source: ExtractionSource) -> List[ExtractedRecord]:
        result = self._analyze_with_retry(source.content_bytes, source.source_email_id)
        if result is None:
            return [_empty_ocr_failure_record(source)]

        records = []
        for table in result.tables:
            if not table or len(table) < 2:
                continue
            column_map = map_headers(table[0])
            if not column_map:
                continue
            for row in table[1:]:
                records.append(build_record_from_row(source, row, column_map, "ocr"))

        if not records:
            # Azure/OCR responded fine, it just found no recognizable table — a genuinely
            # different case from "the service was unavailable," so it must never reuse that
            # reason. Falls through to free-text regex over whatever raw text exists (often
            # nothing at all for a true photographed image, per STAGE_3_EXTRACT.md).
            text_source = ExtractionSource(
                source_email_id=source.source_email_id, email_date=source.email_date,
                source_type="body", body_text=result.raw_text,
            )
            records = FreetextAdapter().extract(text_source)
            for r in records:
                r.extraction_source = "ocr"

        for r in records:
            r.extraction_confidence = min(r.extraction_confidence, settings.AZURE_DOC_INTELLIGENCE_CONFIDENCE_CAP)

        return records

    def _analyze_with_retry(self, content_bytes: bytes, email_id: str) -> Optional[OcrResult]:
        attempts = settings.AZURE_OCR_RETRY_COUNT + 1
        for attempt in range(attempts):
            try:
                return self.client.analyze(content_bytes)
            except Exception as e:
                if attempt < attempts - 1:
                    time.sleep(settings.AZURE_OCR_RETRY_BACKOFF_SECONDS)
                    continue
                _log(f"OCR service unavailable for {email_id} after {attempts} attempts: {e}")
                return None
        return None
