import io
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
        import requests

        if len(content_bytes) > settings.OCR_MAX_IMAGE_BYTES:
            raise ValueError(
                f"document is {len(content_bytes)} bytes, above the "
                f"{settings.OCR_MAX_IMAGE_BYTES}-byte limit"
            )

        params = {"api-version": settings.AZURE_DOC_INTELLIGENCE_API_VERSION}
        if content_bytes[:4] == b"%PDF":
            # Caps billed pages on a long scan. Images are always one page, so the parameter is
            # only meaningful — and only accepted without complaint — for PDFs.
            params["pages"] = f"1-{settings.OCR_MAX_PAGES}"

        response = requests.post(
            f"{self.endpoint}/documentintelligence/documentModels/{self.model}:analyze",
            params=params,
            headers={
                "Ocp-Apim-Subscription-Key": self.api_key,
                "Content-Type": "application/octet-stream",
            },
            data=content_bytes,
            timeout=self.timeout,
        )
        response.raise_for_status()
        operation_url = response.headers.get("operation-location")
        if not operation_url:
            raise RuntimeError("Document Intelligence accepted the document but returned no "
                               "operation-location to poll")
        return _ocr_result_from_docint(self._poll(operation_url))

    def _poll(self, operation_url: str) -> dict:
        import requests

        deadline = time.monotonic() + settings.AZURE_DOC_INTELLIGENCE_POLL_TIMEOUT_SECONDS
        while True:
            result = requests.get(
                operation_url,
                headers={"Ocp-Apim-Subscription-Key": self.api_key},
                timeout=self.timeout,
            )
            result.raise_for_status()
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

    return OcrResult(tables=[t for t in tables if t], raw_text=raw_text, confidence=confidence)


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
        grid[row][column] = (cell.get("content") or "").strip()
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
