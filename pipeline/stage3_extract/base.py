import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from rapidfuzz import fuzz

from config import settings
from pipeline.models import ExtractedRecord

SUB_SPEC_PATTERN = re.compile(r"^(?P<parent>[A-Z0-9]+-\d+-[A-Z]+)-(?P<suffix>[A-Z]+)$")

# A browser "print to PDF" of a scanned/photographed POD (or a photo's OCR text, since OCR
# rasterizes the whole page including this chrome) leaves behind its own print header/footer —
# a timestamp line, and a file:// path + optional page-number line. Found via real dummy test
# documents: every one of them had exactly this shape, and it was polluting quantity/spec-code
# extraction with false positives (a date's "M/D" or a page number's "N/M" misread as a
# quantity fraction). Shared here so both PdfAdapter's text-layer check and any OCR raw-text
# fallback (see ocr_adapter.records_from_ocr_result) strip/ignore it identically.
_PRINT_CHROME_LINE_RE = re.compile(
    r"^\d{1,2}/\d{1,2}/\d{2,4},?\s+\d{1,2}:\d{2}\s*(AM|PM)$|^file:///\S+(\s+\d+/\d+)?$",
    re.IGNORECASE,
)


def is_meaningful_text(text: str) -> bool:
    """True if `text` has any real content beyond recognized browser print-to-PDF chrome."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    real_lines = [ln for ln in lines if not _PRINT_CHROME_LINE_RE.match(ln)]
    return bool(" ".join(real_lines).strip())


def strip_print_chrome(text: str) -> str:
    """Removes recognized browser print-to-PDF chrome lines before regex field extraction runs
    over OCR'd/extracted text, so a leftover timestamp or page number can't be misread as a
    quantity or spec code."""
    lines = [ln for ln in text.splitlines() if not _PRINT_CHROME_LINE_RE.match(ln.strip())]
    return "\n".join(lines)


@dataclass
class PartialFields:
    """Whatever a text-based extraction pass could find — used by FreetextAdapter's own
    regex pass and by ai_fallback.py's proposal, so both speak the same shape."""
    po_number: Optional[str] = None
    spec_code: Optional[str] = None
    quantity_received: Optional[float] = None


@dataclass
class ExtractionSource:
    """One thing for an adapter to read — the email body, or exactly one attachment.
    The Ingest Orchestrator produces one of these per source in a DeliveryEvent (body +
    every attachment, independently) and runs the adapter cascade on each separately.
    """
    source_email_id: str
    email_date: str
    source_type: str                          # "body" | "attachment"
    body_html: Optional[str] = None
    body_text: Optional[str] = None
    filename: Optional[str] = None
    content_type: Optional[str] = None
    content_bytes: Optional[bytes] = None

    sender_address: Optional[str] = None
    subject: Optional[str] = None
    """Envelope sender and subject of the email this source came from. A vendor parser cannot
    identify a format without them — the Authority grammars key on (sender local part, subject),
    and an attachment carries neither on its own."""

    only_po: Optional[str] = None
    """Restrict extraction to one PO. The orchestrator runs one `DeliveryEvent` per PO, so a
    multi-PO source read without this filter stages every line once per PO on the document:
    one email covering two POs produced four records where two were correct (finding C1)."""

    ledger_id: Optional[int] = None
    container_path: str = ""
    """Which `attachment_ledger` row this source is, and where it sits inside any containers.
    The dispatcher writes the outcome back against `ledger_id`, which is what guarantees every
    attachment ends with a recorded verdict."""

    pod_document: Any = None
    """The parsed proof of delivery, when whichever adapter read this attachment recognised one.

    Set by `records_from_pod`, which every POD path funnels through — a PDF's text layer, an
    OCR'd photograph, a scan lifted out of a .docx. Read by the dispatcher, which is the one
    place holding both a database connection and this source's `ledger_id`, and written onto the
    ledger row so that "is this file the proof?" is answered once, at ingest, for any file type.

    Carried on the source rather than returned because the adapter contract is
    `extract() -> List[ExtractedRecord]`, and widening that for one verdict would touch every
    adapter to serve none of them."""


class ExtractionAdapter(ABC):
    @abstractmethod
    def can_handle(self, source: ExtractionSource) -> bool: ...

    @abstractmethod
    def extract(self, source: ExtractionSource) -> List[ExtractedRecord]: ...


def split_sub_spec(spec_code: Optional[str]):
    """'STE-402-LT-B' -> ('STE-402-LT', 'B'); 'STE-402-LT' -> ('STE-402-LT', None)."""
    if not spec_code:
        return None, None
    match = SUB_SPEC_PATTERN.match(spec_code.strip())
    if match:
        return match.group("parent"), match.group("suffix")
    return spec_code, None


def match_header(header_text: str, field_name: str) -> bool:
    """Fuzzy match one header cell against the synonym list for one field (shared by every
    document-shaped adapter — Html/Pdf/Ocr/Excel — so header spelling variance is handled once."""
    synonyms = settings.COLUMN_SYNONYMS.get(field_name, [])
    header_norm = (header_text or "").strip().lower()
    if not header_norm:
        return False
    return any(fuzz.ratio(header_norm, syn.lower()) >= settings.HEADER_MATCH_THRESHOLD for syn in synonyms)


def map_headers(headers: List[str]) -> Dict[str, int]:
    """Given a table's header row, return {field_name: column_index} for every field recognized."""
    mapping = {}
    for field_name in settings.COLUMN_SYNONYMS:
        for idx, header in enumerate(headers):
            if match_header(header, field_name):
                mapping[field_name] = idx
                break
    return mapping


def apply_confidence_floor(record: ExtractedRecord) -> ExtractedRecord:
    """No spec, no quantity, no description => zero real evidence => confidence forced to 0.0.
    Applied centrally so every adapter benefits identically — see STAGE_3_EXTRACT.md."""
    if record.spec_code is None and record.quantity_received is None and record.item_description is None:
        record.extraction_confidence = 0.0
    return record


def parse_quantity(text: Optional[str]) -> Optional[float]:
    """Parses a quantity cell/snippet like '1', '11 CTN', '12' into a float, or None."""
    if not text:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    return float(match.group(1)) if match else None


PO_TOKEN_RE = re.compile(settings.PO_TOKEN_REGEX)
SPEC_TOKEN_RE = re.compile(settings.SPEC_TOKEN_REGEX)
QTY_TOKEN_RE = re.compile(settings.QTY_TOKEN_REGEX)


def regex_extract_fields(text: Optional[str]) -> PartialFields:
    """Regex-only extraction. The lowest-level building block — used directly by
    FreetextAdapter, and as a same-row fallback by every other document-shaped adapter
    when a table cell didn't match cleanly. Lives here (not in freetext_adapter.py) so
    this module has no dependency on the other adapter modules — they depend on it."""
    if not text:
        return PartialFields()
    po_match = PO_TOKEN_RE.search(text)
    spec_match = SPEC_TOKEN_RE.search(text)
    qty_match = QTY_TOKEN_RE.search(text)
    return PartialFields(
        po_number=po_match.group(1) if po_match else None,
        spec_code=spec_match.group(1) if spec_match else None,
        quantity_received=float(qty_match.group(1)) if qty_match else None,
    )


def build_record_from_row(
    source: ExtractionSource,
    cells: List[str],
    column_map: Dict[str, int],
    extraction_source: str,
) -> ExtractedRecord:
    """Shared by HtmlAdapter, PdfAdapter, and ExcelAdapter — one table-row/column-map pair
    becomes one ExtractedRecord, with the same fallback-to-regex and confidence-floor rules
    applied identically regardless of which document format it came from."""
    def cell(field: str) -> Optional[str]:
        idx = column_map.get(field)
        return cells[idx] if idx is not None and idx < len(cells) and cells[idx] else None

    po_number = cell("po_number")
    spec_code = cell("spec_code")
    description = cell("item_description")
    quantity = parse_quantity(cell("quantity_received"))
    vendor = cell("vendor_name")
    uom = cell("unit_of_measure")

    found = sum(1 for f in (po_number, spec_code, quantity) if f is not None)
    confidence = 1.0 if found == 3 else 0.6

    if found < 3:
        fallback = regex_extract_fields(" ".join(cells))
        po_number = po_number or fallback.po_number
        spec_code = spec_code or fallback.spec_code
        quantity = quantity if quantity is not None else fallback.quantity_received

    parent_spec, sub_spec = split_sub_spec(spec_code)

    record = ExtractedRecord(
        source_email_id=source.source_email_id,
        po_number=po_number or "",
        shipment_number=None,   # captured at the email level by Stage 1, not per-row here
        spec_code=spec_code,
        parent_spec_code=parent_spec,
        sub_spec_suffix=sub_spec,
        item_description=description,
        vendor_name=vendor,
        carrier_name=None,
        tracking_number=None,
        quantity_received=quantity,
        unit_of_measure=uom,
        pod_stated_date=None,
        email_date=source.email_date,
        delivery_location=None,
        comments=None,
        extraction_source=extraction_source,
        extraction_confidence=confidence,
        raw_snippet=" | ".join(cells),
    )
    return apply_confidence_floor(record)
