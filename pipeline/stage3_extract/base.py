import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from rapidfuzz import fuzz

from config import settings
from pipeline.models import ExtractedRecord
from pipeline.parsing import tokens

SUB_SPEC_PATTERN = re.compile(r"^(?P<parent>[A-Z0-9]+-\d+-[A-Z]+)-(?P<suffix>[A-Z]+)$")

# A browser "print to PDF" of a scanned/photographed POD (or a photo's OCR text, since OCR
# rasterizes the whole page including this chrome) leaves behind its own print header/footer —
# a timestamp line, and a file:// path + optional page-number line. Found via real dummy test
# documents: every one of them had exactly this shape, and it was polluting quantity/spec-code
# extraction with false positives (a date's "M/D" or a page number's "N/M" misread as a
# quantity fraction). Shared here so both PdfAdapter's text-layer check and any OCR raw-text
# fallback (see ocr_adapter.records_from_ocr_result) strip/ignore it identically.
_PRINT_CHROME_LINE_RE = re.compile(
    r"^\d{1,2}/\d{1,2}/\d{2,4},?\s+\d{1,2}:\d{2}\s*(AM|PM)$"
    r"|^file:///\S+(\s+\d+/\d+)?$"
    # "Page 2 of 2" is a footer, not a quantity — but `QTY_TOKEN_REGEX` matches `N of M`, so every
    # page of a multi-page form produced a record whose only field was a quantity read off the page
    # numbering. Four such records came from four Atlas receiving reports.
    r"|^page\s+\d+\s+of\s+\d+$",
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

    known_po_numbers: Optional[frozenset] = None
    """Purchase orders this message is already known to be about, when the caller can say.

    A six-digit number in a column headed something like "order number" is not necessarily a
    purchase order. The Peerless packing slip on PO 212749 heads two columns `Customer Order
    Number` and `Order Number` over `125609` and `SO127023`: `SO127023` fails `is_po_number`, but
    **`125609` passes it** — six digits — and would be staged as the purchase order for goods
    ordered on 212749. Checked against the mirror, `125609` is not a Spitfire PO at all.

    So a PO read off a document is accepted only if it is one the system already knows. `None`
    means the caller cannot say, and then only the shape test applies — which is exactly the
    behaviour every caller had before this existed."""

    ledger_id: Optional[int] = None
    container_path: str = ""
    """Which `attachment_ledger` row this source is, and where it sits inside any containers.
    The dispatcher writes the outcome back against `ledger_id`, which is what guarantees every
    attachment ends with a recorded verdict."""

    receipt_evidence: Any = None
    """`confirmation.ReceiptEvidence` — what is independently known to have arrived, per PO.

    A confirmation grid states what somebody wants to know; whether it happened is a different
    question, and one the grid cannot answer about itself. This carries the answer in: the purchase
    orders a proof of delivery names, and what the newest hop of the thread said about each spec.

    `None` means nothing is known, and the grid reader treats that as "nothing is proven" rather
    than "anything goes" — a caller that forgets to set this under-claims, which queues work for a
    person, instead of over-claiming, which stages a receiver for goods nobody received.
    """

    pod_document: Any = None
    """The parsed proof of delivery, when whichever adapter read this attachment recognised one.

    Set by `records_from_pod`, which every POD path funnels through — a PDF's text layer, an
    OCR'd photograph, a scan lifted out of a .docx. Read by the dispatcher, which is the one
    place holding both a database connection and this source's `ledger_id`, and written onto the
    ledger row so that "is this file the proof?" is answered once, at ingest, for any file type.

    Carried on the source rather than returned because the adapter contract is
    `extract() -> List[ExtractedRecord]`, and widening that for one verdict would touch every
    adapter to serve none of them."""

    parsed_text: Optional[str] = None
    parsed_tables: Optional[list] = None
    """Everything the reader saw, before it was narrowed to `ExtractedRecord` fields.

    `ExtractedRecord` is nine useful columns wide, and a real document is not. An Atlas Logistics
    Warehouse Receiving Report OCRs into nine clean tables carrying `Customer PO # 913994`,
    `Received by T.Reed`, `BOL/PRO ED6 96925779`, `Total Received 2 pallets`, a 22-row line-item
    grid with part numbers, weights and dimensions — and the pipeline kept none of it, because
    `build_record_from_row` maps one table onto six fields and drops the rest. The parse
    succeeded and the data was discarded, which is indistinguishable from never having read it.

    So the reader's whole output is carried here and persisted by the dispatcher into
    `parsed_documents`, keyed on the attachment. It costs one JSON blob per document and means a
    field we do not extract yet is still recoverable — without re-reading the file and, for a
    scan, without paying for OCR twice.

    `parsed_tables` is a list of tables, each a list of rows, each row a list of cell strings —
    the shape every adapter here already produces internally."""

    grid_verdicts: Optional[list] = None
    """What `receipt.classify_grid` made of each tabular grid read out of this source:
    `(sheet_label, kind, reason)` per grid, in the order they were read.

    `grid_reader` has always computed this and thrown it away — the verdict decided whether rows
    could become receipts and then went nowhere, so a message carrying Premier's own expediting
    report looked, on the queue, exactly like a message nobody could read. The reason strings it
    produces are specific enough to show a person ("a status tracker — 22 lifecycle columns
    including Estimated Delivery (Date)"), and that is the whole point of keeping them.

    Recorded for **every** grid, including the ones that do produce records. A workbook is judged
    sheet by sheet: `ANS-026 … Expediting Report.xlsx` yields 1,122 records from its confirmation
    grid while its `Expediting` sheet is a Spitfire export, and a verdict kept only on the
    record-less path would miss exactly the largest reports.

    Carried on the source for the same reason as `pod_document` — the adapter contract is
    `extract() -> List[ExtractedRecord]`, and the dispatcher is the one place holding both a
    connection and this source's `ledger_id`."""


class ExtractionAdapter(ABC):
    @abstractmethod
    def can_handle(self, source: ExtractionSource) -> bool: ...

    @abstractmethod
    def extract(self, source: ExtractionSource) -> List[ExtractedRecord]: ...


PACKAGE_MARKING_RE = re.compile(
    r"^(?P<spec>.+?)[-\s]*(?P<index>\d+)\s*/\s*(?P<of>\d+)\s+of\s+(?P<total>\d+)\s*$",
    re.IGNORECASE,
)


def strip_package_marking(spec_code: Optional[str]):
    """`'LOB-400-LT-1/1 of 2'` -> `('LOB-400-LT', '1/1 of 2')`. No marking -> `(spec, None)`.

    Atlas heads this column "Carton Markings", and what it holds is the spec code with the carton's
    own numbering fused onto the end. Spitfire's line carries the bare spec, so the two never
    compare equal and the line never resolves.

    Two shapes, because the warehouse writes both. The first is the carton numbering above. The
    second is a note about how the goods are stacked, appended with no separator this pattern
    recognises — `GR-905-EQ////105 PER SKID` on PO 212749, which is Spitfire line 3 and matched
    nothing at all. For that one the rule is positional: what a cell **starts** with is the spec,
    and whatever trails it is the marking. Anchoring at the start is what keeps prose out — a
    description cell reading `TV Mount Tilt: ST650` begins with no spec and is left alone.

    **The original is never discarded.** `spec_code` keeps exactly what the document said; the
    cleaned value goes to `parent_spec_code`, which `po_verify._line_check` already tries as a
    fallback and reports as `matched_on_parent`. That way a wrong guess here is visible and
    reversible rather than silently rewriting what the warehouse wrote.
    """
    if not spec_code:
        return spec_code, None
    text = spec_code.strip()

    match = PACKAGE_MARKING_RE.match(text)
    if match:
        cleaned = match.group("spec").strip(" -/")
        if cleaned:
            return cleaned, match.group(0)[len(match.group("spec")):].strip(" -")

    # `tokens.SPEC_RE` rather than a second grammar here: it is already the repository's definition
    # of a spec code, written from every spec in the corpus, and a private copy would drift from it.
    leading = tokens.SPEC_RE.match(text)
    if leading:
        marking = text[leading.end():].strip(" -/")
        if marking:
            return leading.group(1), marking

    return spec_code, None


def split_sub_spec(spec_code: Optional[str]):
    """'STE-402-LT-B' -> ('STE-402-LT', 'B'); 'STE-402-LT' -> ('STE-402-LT', None)."""
    if not spec_code:
        return None, None
    match = SUB_SPEC_PATTERN.match(spec_code.strip())
    if match:
        return match.group("parent"), match.group("suffix")
    return spec_code, None


def _normalise_header(text: Optional[str]) -> str:
    """Collapse newlines and runs of whitespace to single spaces, then lower.

    Header cells routinely arrive wrapped — `pdfplumber` hands back
    `'Description \\n(such as Desk, Chair, Lamp)'` — and `.strip()` alone leaves the newline in the
    middle, where it depresses every similarity score against a single-line synonym.
    """
    return " ".join((text or "").split()).lower()


def match_header(header_text: str, field_name: str) -> bool:
    """Fuzzy match one header cell against the synonym list for one field (shared by every
    document-shaped adapter — Html/Pdf/Ocr/Excel — so header spelling variance is handled once.

    Whole-cell comparison only. `match_header_embedded` handles the other shape.
    """
    synonyms = settings.COLUMN_SYNONYMS.get(field_name, [])
    header_norm = _normalise_header(header_text)
    if not header_norm:
        return False
    return any(fuzz.ratio(header_norm, syn.lower()) >= settings.HEADER_MATCH_THRESHOLD for syn in synonyms)


def _embedded_pattern(synonym: str) -> re.Pattern:
    """A synonym as a whole word inside a longer header, tolerant of how it is punctuated.

    `\\b` is no use here because most of these synonyms end in `#`: in `item#/mfg#` there is no
    word boundary between `#` and `/`, so `\\bitem#\\b` never matches. Alphanumeric lookaround does
    the job, and the separator inside a synonym is made flexible so `Item #` also finds `item#`.
    """
    parts = [re.escape(p) for p in re.split(r"[^0-9a-z]+", synonym.lower()) if p]
    if not parts:
        return re.compile(r"(?!)")
    return re.compile(r"(?<![0-9a-z])" + r"[^0-9a-z]*".join(parts) + r"(?![0-9a-z])")


def match_header_embedded(header_text: str, field_name: str) -> bool:
    """True when a synonym appears *inside* a longer header cell.

    Atlas heads its two most important columns `Description (such as Desk, Chair, Lamp)` and
    `Carton Markings (Item#/Mfg#/Serial#/Model#/Roll#)`. Both name the field unmistakably and both
    score 44-55 against a bare synonym, well under the threshold — so the grid was recognised on PO
    and Qty alone, `map_headers` returned a mapping too thin to build a record from, and the whole
    table fell through to the regex fallback, which read the Job # as a spec code and no PO at all.
    """
    header_norm = _normalise_header(header_text)
    if not header_norm:
        return False
    for synonym in settings.COLUMN_SYNONYMS.get(field_name, []):
        if len(synonym.strip()) < settings.HEADER_EMBEDDED_MIN_LENGTH:
            continue    # too short to look for inside a longer phrase without false positives
        if _embedded_pattern(synonym).search(header_norm):
            return True
    return False


def map_headers(headers: List[str]) -> Dict[str, int]:
    """Given a table's header row, return {field_name: column_index} for every field recognized.

    Two passes, and the order matters. A cell that *is* a synonym always beats one that merely
    contains it, so whole-cell matching runs first and unchanged; only fields still unmapped go
    looking inside longer headers, and never at a column already claimed. Without that ordering
    `Item` would claim `Item Description` and the spec would be read from the description column.
    """
    mapping: Dict[str, int] = {}
    for field_name in settings.COLUMN_SYNONYMS:
        for idx, header in enumerate(headers):
            if match_header(header, field_name):
                mapping[field_name] = idx
                break

    taken = set(mapping.values())
    for field_name in settings.COLUMN_SYNONYMS:
        if field_name in mapping:
            continue
        for idx, header in enumerate(headers):
            if idx in taken:
                continue
            if match_header_embedded(header, field_name):
                mapping[field_name] = idx
                taken.add(idx)
                break
    return mapping


def is_usable_column_map(column_map: Dict[str, int]) -> bool:
    """A grid worth building records from names an item and says something about it.

    `map_headers` returning *anything* is not enough. An Atlas receiving report carries an
    "Hourly Charges" grid headed `Work Description (Open and Inspect)`, which legitimately maps to
    a description and nothing else; reading rows off it produces records describing labour, not
    deliveries. Requiring an identity column — a PO or a spec — plus one more field is what
    separates a line-item table from a table that merely has words in it.
    """
    has_identity = "po_number" in column_map or "spec_code" in column_map
    return has_identity and len(column_map) >= 2


def find_header_row(table: List[List[str]]) -> Optional[tuple]:
    """Locate the header row inside a table, returning `(index, column_map)`, or None.

    Every adapter used to assume the header was row 0. On a form-shaped PDF it is not: Atlas's
    warehouse receiving report comes back from `pdfplumber` as one 18-row table whose row 0 is the
    title `WAREHOUSE RECEIVING REPORT` and whose item header — `PO # | Description | Carton
    Markings | Qty | ...` — sits at **row 13**, under the address and carrier blocks. Row 0 mapped
    to nothing, so the grid was skipped entirely and the page fell through to the regex fallback,
    which found no PO at all and read the Job # `CTP-025-NA-1-00001` as a spec code.

    The widest usable mapping wins; ties keep the earliest row, so a genuine header beats a data
    row further down that happens to contain matching words.
    """
    best_score = 0
    best: Optional[tuple] = None
    for index, row in enumerate(table[:-1]):    # a header needs at least one row beneath it
        column_map = map_headers([c or "" for c in row])
        if not is_usable_column_map(column_map):
            continue
        if len(column_map) > best_score:
            best_score, best = len(column_map), (index, column_map)
    return best


def harvest_document_fields(tables: List[List[List[str]]]) -> Dict[str, str]:
    """Shipment-level facts from a form's labelled bands: `{field_name: value}`.

    A receiving report states the carrier, the tracking number, the delivery date and who signed
    for it **once**, above the item grid, as a label row with its values on the row beneath. None
    of that is per-line, so `build_record_from_row` never sees it — and `pod_stated_date` is one of
    the five fields `completeness` requires, so every line came out incomplete over a date printed
    on the page.

    Earlier bands win: these forms put the delivery band above the grid, and a later row repeating
    a label is more often a totals or continuation line than a correction.
    """
    found: Dict[str, str] = {}
    for table in tables:
        for index, row in enumerate(table[:-1]):
            values = table[index + 1]
            for field_name, labels in settings.DOCUMENT_FIELD_LABELS.items():
                if field_name in found:
                    continue
                for position, cell_text in enumerate(row):
                    if position >= len(values):
                        continue
                    label = _normalise_header(cell_text)
                    if not label:
                        continue
                    if not any(fuzz.ratio(label, l.lower()) >= settings.HEADER_MATCH_THRESHOLD
                               for l in labels):
                        continue
                    value = " ".join((values[position] or "").split())
                    # A blank value means the band is a template nobody filled in; a value that is
                    # itself a label means the two rows are both headers.
                    if value and not any(fuzz.ratio(value.lower(), l.lower()) >= settings.HEADER_MATCH_THRESHOLD
                                         for l in labels):
                        found[field_name] = value
                        break
    return found


def apply_document_fields(record: ExtractedRecord, fields: Dict[str, str]) -> ExtractedRecord:
    """Stamp shipment-level facts onto a line record, **never overwriting** what the line said.

    A value read off the line is more specific than one read off the form's header, and if Stage 3
    already found one it is what a reviewer has been looking at.
    """
    if not fields:
        return record
    for field_name, value in fields.items():
        if getattr(record, field_name, None):
            continue
        if field_name == "pod_stated_date":
            value = tokens.normalize_date(value) or None
            if not value:
                continue
        setattr(record, field_name, value)
    return record


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

_LABELLED_QTY_RE = re.compile(
    r"\b(?:qty|quantity)\b(?:\s+(?:received|rcvd|shipped|delivered))?\s*[:#]?\s*(\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)
"""A quantity that says it is one — `Qty: 11`, `Quantity received 202`."""

_RECEIPT_VERB_RE = re.compile(
    r"\b(?:receiv(?:ed|ing)|deliver(?:ed|ies|y)|shipp?(?:ed)?|arriv(?:ed|al)|accepted|"
    r"signed\s+for)\b",
    re.IGNORECASE,
)
_RECEIPT_VERB_LOOKBACK = 40
"""How far in front of a bare `N of M` to look for a word saying goods arrived. Deliberately short
— a verb two sentences away says nothing about this number."""


def quantity_from_text(text: Optional[str]) -> Optional[float]:
    """A delivered quantity from running prose, or None.

    Prose is full of numbers that are not quantities, and `QTY_TOKEN_REGEX` — which takes the
    numerator of any `N of M` or `N/M` — read a great many of them as one. Measured over the live
    store, 22 free-text quantities are provably that: `Page: 1 of 4` became a quantity of 1,
    `Page 2 of 2` a quantity of 2, `Issue Date: 10/22` a quantity of 10, and the drawing scale
    `1:8 (1-1/2'=1' 0")` a quantity of 1. On PO 212749 the tag reference `WRR- 17 / PH 3/4 + 4/ 4.`
    became a delivery of 3 against a line the report itself says received 23.

    So a number only counts here when the text says what it is: labelled `Qty`/`Quantity`, or
    carrying a unit of measure. Package units (`9 PLT`, `41 CTN`) are deliberately *not* returned —
    they are `package_quantity`, and receiving a carton count against a PO line is the failure that
    split those two fields apart in the first place.

    `settings.QTY_TOKEN_REGEX` stays where it is: `strip_package_marking` and the carton-marking
    grammar still want the `N of M` form, in a cell where it genuinely means one.
    """
    if not text:
        return None
    labelled = _LABELLED_QTY_RE.search(text)
    if labelled:
        return float(labelled.group(1))
    for value, uom in tokens.QTY_UOM_RE.findall(text):
        quantity = tokens.parse_item_quantity(f"{value} {uom}")
        if quantity is not None:
            return quantity.value
    # Last: the `N of M` form, and only where the sentence says goods arrived. "received 11 of 12
    # chairs" is a delivery; "Page 2 of 2", "Issue Date: 10/22", the drawing scale in
    # `1:8 (1-1/2'=1' 0")` and the tag reference `WRR- 17 / PH 3/4` are not, and every one of those
    # was staged as a quantity. A receipt verb close in front of the number is what separates them.
    for match in QTY_TOKEN_RE.finditer(text):
        if _RECEIPT_VERB_RE.search(text[max(0, match.start() - _RECEIPT_VERB_LOOKBACK):match.start()]):
            return float(match.group(1))
    return None


def clean_spec_cell(text: Optional[str]) -> Optional[str]:
    """A spec cell as the document meant it, with OCR's leavings removed.

    Handwriting read off a scan arrives with the reader's own uncertainty attached — the Atlas
    receiving report's Carton Markings column came back as `PGR-905-EQ ?`, `? STE-901-EQ?` and
    `PO#212749.` — and a newline where a checkbox sat. None of that is part of a spec code, and a
    spec carrying it matches no purchase order line and never will.

    Only wrapping punctuation is stripped. The interior is left exactly as written, so
    `GR-905-EQ////105 PER SKID` still reaches `strip_package_marking` whole.
    """
    if text is None:
        return None
    collapsed = " ".join(str(text).split())
    return collapsed.strip(" ?.,;:#*|-") or None


def states_a_line_item(record: ExtractedRecord) -> bool:
    """Whether a grid row says enough to be a delivered line: how many, or what.

    An item grid runs on past its last item — into totals, continuation lines, and on a scan into
    marginal notes that Document Intelligence returns as rows of their own. A row naming neither a
    quantity nor a description describes no delivery; it can never be completed, and all it does is
    occupy a line on somebody's queue. 89 such records are in the live store and not one of them
    has ever been complete.
    """
    return record.quantity_received is not None or bool((record.item_description or "").strip())


def kept_despite_a_misread_spec(record: ExtractedRecord, cells: Sequence[Any],
                                column_map: Dict[str, int]) -> bool:
    """A line that lost a misread spec, and must not be dropped for having lost it.

    The table readers drop a record naming neither a PO nor a spec, because such a row usually
    identifies nothing. `Alarm Clocks | 9` used to survive that guard only because a quantity had been
    misread into its spec field (`1`); with the misread cleared, the same guard would delete a
    delivered line instead of leaving it for a person to name.

    **Deliberately narrow: only rows whose spec column held a value that was rejected.** Keeping every
    line with a description and a quantity was measured first and would have staged 628 records
    across 77 documents that were never staged before — service parts, rugs, adhesive, most of them
    repeated across forwarded copies. This keeps exactly the rows that survived before, minus their
    wrong spec, and adds none.

    A description **or** a quantity is enough, because that is what those rows had: 394 of them
    (`Corridor Broadloom` over a vendor number) had a description and no quantity. Deleting junk
    lines is a separate decision with its own measurement; this rule only stops a field correction
    from deleting lines as a side effect.
    """
    index = column_map.get("spec_code")
    if index is None or index >= len(cells) or clean_spec_cell(cells[index]) is None:
        return False
    return bool((record.item_description or "").strip()) or record.quantity_received is not None


_SPEC_IN_CELL_RE = re.compile(tokens.SPEC_RE.pattern, re.IGNORECASE)
"""Premier's spec shape, case-insensitive. `tokens.SPEC_RE` is upper-case only because free text is
matched against it; a cell a person typed into a spreadsheet as `lob-105-cg` is still that spec."""

_TOTAL_ROW_RE = re.compile(r"^\s*(?:grand\s+|sub\s*-?\s*)?totals?\b", re.IGNORECASE)
_BLOCK_ROW_RE = re.compile(
    r"^\s*(?:ship(?:ping)?\s*(?:to|address)|bill(?:ing)?\s*(?:to|address)|sold\s+to|remit\s+to"
    r"|order\s+information|delivery\s+instructions|shipping\s+instructions)\b",
    re.IGNORECASE,
)
_LABEL_CELL_RE = re.compile(r"^\s*[A-Za-z][A-Za-z .#/&()'-]{0,40}:")


def is_item_row(cells: Sequence[Any]) -> bool:
    """Whether a table body row can be a delivered item at all, judged before it becomes a record.

    Three shapes that are not, each staged as goods on the live store before this existed (G14,
    case C — 100 records):

    * **a total** — `Grand Total | 22 | 2 | $12,639`, `Subtotal`, `Total:`
    * **an address or order-information block** — `Shipping Address: …`, `Ship To:`, `Order
      Information`
    * **a label row naming no spec** — `Item Description: Area Name: | Curved Sofas…`. A label is
      only disqualifying when nothing in the row is a spec: `PO#: 900101 | LOB-105-CG | 2` is an
      item line whose first cell happens to carry its own caption.

    An empty row is not an item either. What a row *says* about quantity and description is
    `states_a_line_item`'s question, asked after the record is built.
    """
    texts = [str(c).strip() for c in cells if c is not None]
    filled = [t for t in texts if t]
    if not filled:
        return False
    first = filled[0]
    if _TOTAL_ROW_RE.match(first) or any(re.fullmatch(r"(?i)(?:grand\s+|sub\s*)?totals?\s*:?", t)
                                          for t in filled):
        return False
    if _BLOCK_ROW_RE.match(first):
        return False
    if _LABEL_CELL_RE.match(first) and not any(_SPEC_IN_CELL_RE.search(t) for t in filled):
        return False
    return True


_DOCUMENT_NUMBER_RE = re.compile(r"(?:RR|WRR|PO|SO|INV|BOL|PRO|REF|ORD)\s*[-#]?\s*\d+", re.IGNORECASE)
"""A bare document reference — `RR-28`, `SO-5`, `INV-1042`. It has a spec's shape and is not one.

Only the *bare* form: letters, a separator, digits, nothing after. A real spec that happens to share
the letters carries more — `RR-701-MR` is a restroom item — and is still read."""


_NOT_A_NAME_RE = re.compile(
    r"(?:tbd|tba|n/?a|none|null|each|ea|pcs?|pieces?|units?|window|sets?|pairs?|lots?|boxes?|"
    r"cartons?|ctns?|rolls?|yards?|yds?|feet|ft|sq\s*ft|sf|lf|ly|sy)",
    re.IGNORECASE,
)


def _reads_as_an_item_name(text: Optional[str]) -> bool:
    """Whether a spec cell reads as the item's *name* rather than any kind of code.

    Two or more real words, because one is how plenty of genuine specs are written: the mirror holds
    `Photo 1`, `Dryer 01`, `Leather` and `Collateral` as spec codes on live purchase order lines.
    `24424 *EXAMPLE DESK 130lbs` is prose by that test and `Photo 1` is not, which is the line this
    has to draw — a name moved into the description is a name taken off the spec.
    """
    value = (text or "").strip()
    if not value or _NOT_A_NAME_RE.fullmatch(value):
        return False
    return len(re.findall(r"[A-Za-z]{3,}", value)) >= 2


def _is_certainly_not_a_spec(text: str) -> bool:
    """Whether a cell can be ruled out as a spec code outright.

    Deliberately a list of what is *wrong* rather than a test of what is right. Requiring the
    familiar `ABC-123` shape looked equivalent and was not: 122 of the 1,386 spec codes on mirrored
    purchase order lines — 8.8% — do not have it (`IT-EQ`, `P-01`, `Photo 1`, `Dryer 01`,
    `HydroKit-01`, `STE-201Ra-SGF`), and a real receipt for 373 `ST650` mounts lost its spec to that
    rule. An unfamiliar code is kept; only these three are cleared.
    """
    value = (text or "").strip()
    if not value:
        return False
    # A quantity or a bare figure. `1`, `2.0`, `300101` — a number identifies no line item.
    if re.fullmatch(r"[\d.,]+", value):
        return True
    # A unit of measure or a placeholder: `Each`, `EA`, `Window`, `TBD`, `N/A`.
    if _NOT_A_NAME_RE.fullmatch(value):
        return True
    # The document's own number, which is about the paperwork rather than the goods: `RR-28`.
    if _DOCUMENT_NUMBER_RE.fullmatch(value):
        return True
    # Prose. Two or more real words is the item's name written into the wrong column, not a code —
    # see `_reads_as_an_item_name`, which is also what moves it across into the description.
    return _reads_as_an_item_name(value)


def _spec_elsewhere_in_row(cells: Sequence[Any], skip: Optional[int]) -> Optional[str]:
    """The first spec-shaped value in any other cell of the row, or None.

    Document numbers are skipped. On the live store the first version of this took a receiving
    report's `RR-28` for the spec of a pallet line, replacing one wrong spec with another.
    """
    for index, value in enumerate(cells):
        if index == skip or value is None:
            continue
        for found in _SPEC_IN_CELL_RE.finditer(str(value)):
            if not _DOCUMENT_NUMBER_RE.fullmatch(found.group(1)):
                return found.group(1)
    return None


def regex_extract_fields(text: Optional[str]) -> PartialFields:
    """Regex-only extraction. The lowest-level building block — used directly by
    FreetextAdapter, and as a same-row fallback by every other document-shaped adapter
    when a table cell didn't match cleanly. Lives here (not in freetext_adapter.py) so
    this module has no dependency on the other adapter modules — they depend on it."""
    if not text:
        return PartialFields()
    po_match = PO_TOKEN_RE.search(text)
    spec_match = SPEC_TOKEN_RE.search(text)
    return PartialFields(
        po_number=po_match.group(1) if po_match else None,
        spec_code=spec_match.group(1) if spec_match else None,
        quantity_received=quantity_from_text(text),
    )


def po_column_is_corroborated(
    rows: Iterable[List[str]],
    column_map: Dict[str, int],
    known_po_numbers: Optional[Set[str]],
) -> bool:
    """Whether this table's PO column can be believed for purchase orders we have never pulled.

    The `known_po_numbers` guard in `build_record_from_row` exists because a mapped "PO" column is
    sometimes not one — a packing slip's `Customer Order Number` over `125609`. Checking each cell
    against the mirror catches that, but it also throws away a **real** purchase order printed on a
    real document merely because nobody has pulled it into the mirror yet.

    The two cases are told apart by the column as a whole, not by the cell. A genuine PO column on
    a multi-PO document carries some orders we know and some we do not; a mismapped column carries
    none we know at all. So: one recognised purchase order anywhere in the column vouches for the
    rest of it.

    Measured on the live store, this is exactly the Warehouse Receiving Report for RR 211373-29 —
    ten item lines, three on a purchase order the mirror holds and seven on one it does not. Those
    seven had their PO cell blanked and were then stamped with the *other* order's number, so seven
    item lines were recorded against goods they had nothing to do with.
    """
    if not known_po_numbers:
        return False
    index = column_map.get("po_number")
    if index is None:
        return False
    for cells in rows:
        if index >= len(cells):
            continue
        value = (cells[index] or "").strip()
        if value in known_po_numbers:
            return True
    return False


def build_record_from_row(
    source: ExtractionSource,
    cells: List[str],
    column_map: Dict[str, int],
    extraction_source: str,
    po_column_verified: bool = False,
) -> ExtractedRecord:
    """Shared by HtmlAdapter, PdfAdapter, and ExcelAdapter — one table-row/column-map pair
    becomes one ExtractedRecord, with the same fallback-to-regex and confidence-floor rules
    applied identically regardless of which document format it came from.

    `po_column_verified` says the caller has already established that this table's PO column really
    holds purchase orders — see `po_column_is_corroborated`. It relaxes the mirror check below, and
    only that check; every other test a cell must pass is unchanged. It defaults to False so a
    caller that has not looked keeps the stricter behaviour.
    """
    def cell(field: str) -> Optional[str]:
        idx = column_map.get(field)
        return cells[idx] if idx is not None and idx < len(cells) and cells[idx] else None

    po_number = cell("po_number")
    if po_number is not None and not tokens.is_po_number(po_number):
        # The mapped column said "PO" but the cell is not one. `map_headers` matches headers
        # fuzzily against short synonyms, so on a document nobody has a parser for it lands on
        # whatever column looked closest — on an Atlas "Warehouse Receiving Report" that was an
        # address block, and the resulting records carried a phone number as their purchase order.
        # Dropping it lets the regex fallback below have a go at the row instead.
        po_number = None
    if (po_number is not None and source.known_po_numbers and not po_column_verified
            and po_number not in source.known_po_numbers):
        # The right shape, the wrong number. A packing slip heads a column `Customer Order Number`
        # over `125609` — six digits, so the shape test above passes it — on a delivery against
        # purchase order 212749. Taking it would attribute the goods to a purchase order that does
        # not exist. Dropped like any other bad cell, which leaves the record's PO to be settled by
        # the delivery event it belongs to. See `ExtractionSource.known_po_numbers`.
        #
        # **Unless the column has already vouched for itself.** A purchase order we have not pulled
        # is not a purchase order that does not exist, and blanking it here is what let the delivery
        # event stamp a different order's number onto the line — see `po_column_is_corroborated`.
        po_number = None
    spec_code = clean_spec_cell(cell("spec_code"))
    rejected_spec_text: Optional[str] = None
    # A document number is checked even though it *has* the usual shape: `RR-28` is `[A-Z]{2}-\d{2}`
    # to the eye and to the regex, and it is the receiving report's own number. `RR-701-MR` is a
    # restroom item and does not fullmatch, so real specs carrying those letters are untouched.
    if spec_code is not None and (not _SPEC_IN_CELL_RE.search(spec_code)
                                  or _DOCUMENT_NUMBER_RE.fullmatch(spec_code)):
        # The mapped column said "spec" and the cell does not carry one of the familiar shape. That
        # is a reason to look harder, not a reason to throw the cell away — see
        # `_is_certainly_not_a_spec` for the 122 real spec codes this used to delete.
        #
        # `.search`, not `.fullmatch`, above: a cell that *contains* a spec is left exactly as it
        # is. `LOB-400-LT-1/1 of 2` is a carton marking `strip_package_marking` reads below, and a
        # spec with words around it is a later step's to split, not this one's to discard.
        elsewhere = _spec_elsewhere_in_row(cells, column_map.get("spec_code"))
        if elsewhere is not None:
            # Another cell of the same row holds a spec of Premier's usual shape, so the mapped one
            # was the vendor's own code beside it. On the live store this is what put `LOB-400-LT`
            # back on lines whose spec column held `VND_SKU_01`-shaped text.
            rejected_spec_text = spec_code
            spec_code = elsewhere
        elif _is_certainly_not_a_spec(spec_code):
            # A quantity, a unit word or the document's own number. Nothing in the row to put in its
            # place, and keeping it guarantees a value that can never match a purchase order line.
            rejected_spec_text = spec_code
            spec_code = None
        # Anything else stays exactly as it was read. An unfamiliar code is far more often a real
        # spec this reader has not seen the shape of than it is noise.
    description = cell("item_description")
    quantity = parse_quantity(cell("quantity_received"))
    if (not (description or "").strip() and quantity is not None
            and _reads_as_an_item_name(rejected_spec_text)):
        # The rejected cell was the item's *name*, read into the wrong column: 256 lines on the
        # live store carried `24424 *ARISTOTLE DESK`-shaped text in the spec and nothing in the
        # description. Moved across rather than discarded, and only into an empty description.
        #
        # Only on a row that states a quantity. Without one, the row named a "spec" and nothing
        # else, so it was never staged; filling its description made it pass `states_a_line_item`
        # and staged 142 new records store-wide — `via email`, a mill's name, a plan reference.
        description = rejected_spec_text
    vendor = cell("vendor_name")
    uom = cell("unit_of_measure")

    found = sum(1 for f in (po_number, spec_code, quantity) if f is not None)
    confidence = 1.0 if found == 3 else 0.6

    if found < 3:
        fallback = regex_extract_fields(" ".join(cells))
        po_number = po_number or fallback.po_number
        # The same exclusion `_spec_elsewhere_in_row` makes. This fallback scans the joined row too,
        # and a receiving report's `RR-28` has a spec's shape.
        if not (fallback.spec_code and _DOCUMENT_NUMBER_RE.fullmatch(fallback.spec_code)):
            spec_code = spec_code or fallback.spec_code
        quantity = quantity if quantity is not None else fallback.quantity_received

    # A carton marking is stripped for matching only — `spec_code` below still carries what the
    # document actually said, and the cleaned form travels as the parent.
    cleaned_spec, _package_marking = strip_package_marking(spec_code)
    parent_spec, sub_spec = split_sub_spec(cleaned_spec)

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
        source_ledger_id=source.ledger_id,
    )
    return apply_confidence_floor(record)
