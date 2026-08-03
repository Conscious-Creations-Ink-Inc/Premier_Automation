"""Authority Logistics (ALS) notification parser — corpus classes A and B.

Two notices describe every warehouse delivery, and telling them apart is the whole job:

**Class A — Inbound Notification**, from `warehousing@authoritylogistics.com`. What the
warehouse actually booked in. This is the receiver trigger.

    [External] 239475 - Inbound Notification - 208491 - 2985 : LXR Cameo Beverly Hills (Guestrooms) - MRC Los Angeles CA
    Received Date: 10/09/2025 | Received at: Crown Worldwide ... | Received By: Miguel C.
    ALS Shipment #: <blank> | Carrier: Custom Companies | Tracking: 69942177 | Quantity: 1 CTN
    PO # / Line # | Supplier | Part # | Item                                   | Package        | Comments
    208491 : 300  | Light Annex | STE-402-LT-B | 1 EA - STE-402-LT-B - BASE, ... | 1 CTN - 50.00 lb | STE-402-LT

**Class B — Delivered Notification**, from `routing@authoritylogistics.com`. What the *carrier*
dropped at the warehouse door. Usually **not** a receiver — the Inbound for the same goods
follows, and creating a receiver from both is the double-count that killed Premier's previous
automation attempt.

    [External] 50009 - Delivered Notification -  - 206725, 207665
    Authority #: 50009 | Carrier: Nolan Transportation | Tracking: 8801592 | Delivered: 09/15/2025
    Package | PO # | Item | Supplier

The join that prevents the double count: **Class B's `Authority #` is Class A's
`ALS Shipment #`** — verified in the corpus (Delivered 50009 and Inbound 239260 are the same
goods: POs 206725/207665, tracking 8801592). `AuthorityNotice.shipment_number` returns that
number under either name, and Stage 2 keys on it.

Everything here is positional. The generic PO regex finds nothing in these emails — no "PO"
prefix appears anywhere near the numbers — and a bare `\\d{6}` would happily return the inbound
number 239336 as a PO. See `parsing/tokens.py`.
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from pipeline.models import ExtractedRecord
from pipeline.parsing import tables as tbl
from pipeline.parsing import text as txt
from pipeline.parsing import thread as thr
from pipeline.parsing import tokens as tok

AUTHORITY_DOMAIN = "authoritylogistics.com"

INBOUND_LOCAL_PARTS = {"warehousing"}
DELIVERED_LOCAL_PARTS = {"routing"}


class NoticeKind(str, Enum):
    INBOUND = "inbound"          # class A — the receiver trigger
    DELIVERED = "delivered"      # class B — carrier dropped at the warehouse
    STATUS_REPORT = "status_report"   # class E — periodic summary, never a receiver
    NOT_AUTHORITY = "not_authority"


# --- Subject grammars -------------------------------------------------------
# `{n} - Inbound Notification - {POs} - {project} : {name} - MRC {city}`. The project/name tail
# is optional: the shortest Delivered subject in the corpus stops right after the PO list.

_INBOUND_SUBJECT_RE = re.compile(
    r"^(?P<notice>\d{4,8})\s*-\s*Inbound\s+Notification\s*-\s*"
    r"(?P<pos>[\d,\s]+?)"
    r"(?:\s*-\s*(?P<project>\d{3,5})\s*[:\-]\s*(?P<project_name>.*))?$",
    re.IGNORECASE,
)

# Delivered subjects carry an empty slot between the label and the PO list — rendered as
# `-  -` or `- -` depending on how many spaces survived. That empty slot is load-bearing: it is
# what distinguishes the grammar from Inbound's, so it is matched, not skipped over.
_DELIVERED_SUBJECT_RE = re.compile(
    r"^(?P<notice>\d{4,8})\s*-\s*Delivered\s+Notification\s*-\s*-\s*"
    r"(?P<pos>[\d,\s]+?)"
    r"(?:\s*-\s*(?P<project>\d{3,5})\s*[:\-]\s*(?P<project_name>.*))?$",
    re.IGNORECASE,
)

_STATUS_REPORT_SUBJECT_RE = re.compile(r"purchase\s+order\s+status\s+report", re.IGNORECASE)

# --- Table header signatures ------------------------------------------------

INBOUND_LINE_HEADERS = ["PO # / Line #", "Supplier", "Part #", "Item", "Package", "Comments"]
DELIVERED_LINE_HEADERS = ["Package", "PO #", "Item", "Supplier"]

# --- Header (key/value) fields ----------------------------------------------
# Inbound renders these as a two-column table; Delivered renders them as label/value text. Both
# are read with the same label list, table first then text, so the two paths cannot drift.

_HEADER_LABELS = [
    "received date", "received at", "received by", "returned", "als shipment #", "authority #",
    "carrier", "tracking", "quantity", "weight", "from", "delivered", "signed by", "to",
]

_LABEL_VALUE_RE_CACHE: Dict[str, re.Pattern] = {}


def _label_value(text: str, label: str) -> str:
    """Read `Label: value` out of rendered text, tolerating the value landing on the next line
    (which is how Outlook renders these blocks) and stopping at the next known label."""
    if label not in _LABEL_VALUE_RE_CACHE:
        _LABEL_VALUE_RE_CACHE[label] = re.compile(
            rf"^[ \t]*{re.escape(label)}\s*:[ \t]*\n?(?P<value>(?:[^\n]*\n?){{0,3}}?)(?=\n\s*\n|\n[ \t]*[A-Z][\w #]{{2,20}}\s*:|\Z)",
            re.IGNORECASE | re.MULTILINE,
        )
    match = _LABEL_VALUE_RE_CACHE[label].search(text or "")
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group("value")).strip()


_INBOUND_ANCHOR_RE = re.compile(r"^[ \t]*Received\s+Date\s*:", re.IGNORECASE | re.MULTILINE)
_DELIVERED_ANCHOR_RE = re.compile(r"^[ \t]*Authority\s*#\s*:", re.IGNORECASE | re.MULTILINE)


def _notice_region(rendered: str, kind: NoticeKind) -> str:
    """Narrow the text to the notice body, discarding the forwarding chrome above it.

    A forwarded notice carries Outlook's own `From:` / `To:` / `Subject:` header block, and the
    notice itself has fields with those same names — `From: Tournesol Siteworks : 1540 Leader
    International Dr` and `To: Crown Worldwide ... Mira Loma`. Read against the whole message
    the `To:` label matches the *recipient list* first, so `delivery_location` came out as a
    string of premierpm.com addresses. Anchoring on the notice's first field fixes that at the
    source instead of trying to recognise a recipient list after the fact.
    """
    if not rendered:
        return ""
    anchor = _INBOUND_ANCHOR_RE if kind == NoticeKind.INBOUND else _DELIVERED_ANCHOR_RE
    match = anchor.search(rendered)
    return rendered[match.start():] if match else rendered


@dataclass
class AuthorityLine:
    """One row of the notice's line table."""
    po_number: str
    line_number: Optional[int]
    supplier: Optional[str]
    part_number: Optional[str]
    item_cell: str
    package_cell: str
    comments: Optional[str]

    quantity: Optional[float] = None
    unit_of_measure: Optional[str] = None
    package_quantity: Optional[float] = None
    package_uom: Optional[str] = None
    spec_code: Optional[str] = None
    parent_spec_code: Optional[str] = None
    sub_spec_suffix: Optional[str] = None
    description: Optional[str] = None


@dataclass
class AuthorityNotice:
    kind: NoticeKind
    notice_number: Optional[str] = None
    subject_po_numbers: List[str] = field(default_factory=list)
    project_code: Optional[str] = None
    project_name: Optional[str] = None

    received_date: Optional[str] = None
    received_at: Optional[str] = None
    received_by: Optional[str] = None
    returned: Optional[str] = None
    als_shipment_number: Optional[str] = None
    authority_number: Optional[str] = None
    carrier: Optional[str] = None
    tracking_numbers: List[str] = field(default_factory=list)
    header_quantity: Optional[float] = None
    header_uom: Optional[str] = None
    ship_from: Optional[str] = None
    ship_to: Optional[str] = None
    signed_by: Optional[str] = None

    lines: List[AuthorityLine] = field(default_factory=list)

    @property
    def shipment_number(self) -> Optional[str]:
        """The B<->A join key under whichever name this notice used it.

        Returns None rather than a substitute when an Inbound leaves `ALS Shipment #` blank
        (notice 239475 does). Falling back to the *inbound* number there would look like a key
        but join to nothing — every Delivered notice states an Authority number, never an inbound
        number — and would silently split one delivery into two accumulation buckets.
        """
        if self.kind == NoticeKind.DELIVERED:
            return self.authority_number or self.notice_number
        return self.als_shipment_number

    @property
    def is_receiver_trigger(self) -> bool:
        return self.kind == NoticeKind.INBOUND

    @property
    def po_numbers(self) -> List[str]:
        """Subject POs plus any that only appear in the line table, in first-seen order."""
        ordered = list(self.subject_po_numbers)
        for line in self.lines:
            if line.po_number and line.po_number not in ordered:
                ordered.append(line.po_number)
        return ordered


# --- Classification ---------------------------------------------------------


def classify(sender_address: str, subject: str) -> NoticeKind:
    """Kind from (sender local-part, subject token) — never from the domain alone.

    The weekly Purchase Order Status Report arrives from the *same* `warehousing@` address as the
    receiver trigger, so a domain- or even sender-only rule routes a summary report into the
    receiver path. Only the subject separates them (finding G3).
    """
    address = (sender_address or "").lower()
    clean_subject = thr.strip_forward_prefixes(subject or "")
    from_authority = AUTHORITY_DOMAIN in address

    # The subject grammar is specific enough to stand on its own — `{digits} - Inbound
    # Notification - {POs} - {project} : {name}` is not a shape other mail takes. That matters
    # for a notification that arrives as a nested attachment inside somebody else's thread: the
    # attachment carries the notice's subject but the enclosing email's sender, so requiring a
    # matching sender first dropped the notice to the generic adapter, which read the whole Item
    # cell as a spec code.
    if _INBOUND_SUBJECT_RE.match(clean_subject):
        return NoticeKind.INBOUND
    if _DELIVERED_SUBJECT_RE.match(clean_subject):
        return NoticeKind.DELIVERED

    if not from_authority:
        return NoticeKind.NOT_AUTHORITY

    if _STATUS_REPORT_SUBJECT_RE.search(clean_subject):
        return NoticeKind.STATUS_REPORT

    # Subject didn't parse — fall back to the local part so an unrecognised subject variant still
    # lands on the right side of the receiver/not-a-receiver line rather than being dropped.
    local_part = address.split("@")[0]
    if local_part in INBOUND_LOCAL_PARTS:
        return NoticeKind.INBOUND
    if local_part in DELIVERED_LOCAL_PARTS:
        return NoticeKind.DELIVERED
    return NoticeKind.NOT_AUTHORITY


# --- Parsing ----------------------------------------------------------------


def parse_authority_notice(
    sender_address: str,
    subject: str,
    body_html: Optional[str],
    body_text: Optional[str] = None,
) -> Optional[AuthorityNotice]:
    """Parse one notice, or None if this isn't an Authority notice we handle.

    A STATUS_REPORT returns a notice with no lines — recognised, deliberately empty, so the
    caller can route it to noise instead of leaving it to fall through to a generic adapter that
    would happily scrape PO-looking numbers out of a summary table.
    """
    kind = classify(sender_address, subject)
    if kind in (NoticeKind.NOT_AUTHORITY,):
        return None

    notice = AuthorityNotice(kind=kind)
    clean_subject = thr.strip_forward_prefixes(subject or "")

    subject_match = (_INBOUND_SUBJECT_RE.match(clean_subject) if kind == NoticeKind.INBOUND
                     else _DELIVERED_SUBJECT_RE.match(clean_subject) if kind == NoticeKind.DELIVERED
                     else None)
    if subject_match:
        notice.notice_number = subject_match.group("notice")
        notice.subject_po_numbers = tok.parse_po_list(subject_match.group("pos"))
        notice.project_code = subject_match.group("project")
        notice.project_name = (subject_match.group("project_name") or "").strip() or None

    if kind == NoticeKind.STATUS_REPORT:
        return notice

    rendered = body_text if (body_text and body_text.strip()) else txt.html_to_text(body_html)
    all_tables = tbl.extract_tables(body_html)

    _fill_header(notice, all_tables, _notice_region(rendered, kind))
    _fill_lines(notice, all_tables, kind)
    return notice


def _fill_header(notice: AuthorityNotice, all_tables: List[tbl.HtmlTable], rendered: str) -> None:
    """Header fields from the key/value table if there is one, else from label/value text.

    Both sources are consulted for every field: the Inbound header table can carry a blank cell
    (`ALS Shipment #:` is empty on the single-base notice) where the text rendering still has
    the label, and vice versa.
    """
    from_tables: Dict[str, str] = {}
    for table in all_tables:
        if table.width <= 3:
            for key, value in tbl.as_key_values(table).items():
                if key in _HEADER_LABELS and key not in from_tables:
                    from_tables[key] = value

    def get(label: str) -> str:
        return from_tables.get(label) or _label_value(rendered, label)

    notice.received_date = tok.normalize_date(get("received date")) or tok.normalize_date(get("delivered"))
    notice.received_at = get("received at") or get("to") or None
    notice.received_by = get("received by") or get("signed by") or None
    notice.returned = get("returned") or None
    notice.als_shipment_number = tok.parse_shipment_number(get("als shipment #"))
    notice.authority_number = tok.parse_shipment_number(get("authority #"))
    notice.carrier = get("carrier") or None
    notice.tracking_numbers = tok.parse_tracking_numbers(get("tracking"))
    notice.ship_from = get("from") or None
    notice.ship_to = get("to") or None
    notice.signed_by = get("signed by") or None

    header_quantity = tok.parse_leading_quantity(get("quantity"))
    if header_quantity:
        notice.header_quantity = header_quantity.value
        notice.header_uom = header_quantity.uom

    # A Delivered notice states no `ALS Shipment #`; its `Authority #` *is* that number, and the
    # subject repeats it. Recording it under both names is what lets Stage 2 recognise a
    # Delivered/Inbound pair as one delivery rather than two.
    if notice.kind == NoticeKind.DELIVERED:
        notice.authority_number = notice.authority_number or notice.notice_number
        notice.als_shipment_number = notice.als_shipment_number or notice.authority_number


def _fill_lines(notice: AuthorityNotice, all_tables: List[tbl.HtmlTable], kind: NoticeKind) -> None:
    expected = INBOUND_LINE_HEADERS if kind == NoticeKind.INBOUND else DELIVERED_LINE_HEADERS
    grid = tbl.find_grid(all_tables, expected)
    if grid is None:
        return

    header = grid.header
    idx_po_line = tbl.column_index(header, ["PO # / Line #", "PO #", "PO"])
    idx_supplier = tbl.column_index(header, ["Supplier", "Vendor"])
    idx_part = tbl.column_index(header, ["Part #", "Part Number", "Item Number"])
    idx_item = tbl.column_index(header, ["Item", "Item Description", "Description"])
    idx_package = tbl.column_index(header, ["Package"])
    idx_comments = tbl.column_index(header, ["Comments", "Notes"])

    # Delivered grids put Package first and PO second; Inbound has no standalone Package column
    # header collision. Resolving by name rather than position keeps one code path for both.
    carried_package = ""
    for row in grid.body_rows:
        po_cell = tbl.cell(row, idx_po_line).strip()
        if not po_cell:
            continue

        reference = tok.parse_po_line_ref(po_cell)
        if reference:
            po_number, line_number = reference.po_number, reference.line_number
        else:
            candidates = tok.parse_po_list(po_cell)
            if not candidates:
                continue   # not a data row (a spanning sub-header, or a spacer)
            po_number, line_number = candidates[0], None

        package_cell = tbl.cell(row, idx_package).strip()
        # Authority states the package figure once, on the first row of a multi-line shipment,
        # and leaves it blank on the rest. Carrying it forward keeps every row's provenance
        # complete without ever letting it be mistaken for the item quantity.
        if package_cell:
            carried_package = package_cell
        effective_package = package_cell or carried_package

        item_cell = tbl.cell(row, idx_item).strip()
        line = AuthorityLine(
            po_number=po_number,
            line_number=line_number,
            supplier=tbl.cell(row, idx_supplier).strip() or None,
            part_number=tbl.cell(row, idx_part).strip() or None,
            item_cell=item_cell,
            package_cell=effective_package,
            comments=tbl.cell(row, idx_comments).strip() or None,
        )

        item_quantity = tok.parse_item_quantity(item_cell)
        if item_quantity:
            line.quantity = item_quantity.value
            line.unit_of_measure = item_quantity.uom

        package_quantity = tok.parse_package_quantity(effective_package)
        if package_quantity:
            line.package_quantity = package_quantity.value
            line.package_uom = package_quantity.uom

        # Spec: the `Part #` column when the Inbound format supplies it, otherwise the first spec
        # token inside the Item text (which is all the Delivered format gives us). `Part #` is
        # not always a spec — "Accessory Pocket" and "Toe Kick Planter" appear there — so the
        # value is validated against the spec grammar before being trusted.
        spec = tok.normalize_spec(line.part_number or "") or tok.normalize_spec(_first_spec_in(item_cell) or "")
        if spec:
            line.spec_code = spec.full
            line.parent_spec_code = spec.parent
            line.sub_spec_suffix = spec.sub_part
        line.description = _description_of(item_cell, line.spec_code)

        notice.lines.append(line)


def _first_spec_in(text: str) -> Optional[str]:
    specs = tok.find_specs(text)
    return specs[0] if specs else None


def _description_of(item_cell: str, spec_code: Optional[str]) -> Optional[str]:
    """The human-readable part of an Item cell: leading quantity removed, and the spec code
    dropped where it is merely repeated (`"1 EA - STE-402-LT-B STE-402-LT-B - BASE, Floor Lamp"`
    is the same spec stated three times before the description begins)."""
    if not item_cell:
        return None
    text = re.sub(r"^\s*\d+(?:\.\d+)?\s*[A-Za-z]{2,7}\s*-\s*", "", item_cell).strip()
    if spec_code:
        text = re.sub(rf"^(?:{re.escape(spec_code)}[\s\-:]*)+", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"^(?:Item Number:\s*\S+\s*)?(?:Item Description:\s*)?", "", text, flags=re.IGNORECASE).strip()
    return text or None


# --- Record emission --------------------------------------------------------


def records_from_notice(
    notice: AuthorityNotice,
    source_email_id: str,
    email_date: str,
    only_po: Optional[str] = None,
) -> List[ExtractedRecord]:
    """One `ExtractedRecord` per line of the notice.

    `only_po` filters to a single PO. Multi-PO notices are the norm here (239260 covers 206725
    and 207665), and the orchestrator processes one `DeliveryEvent` per PO — without the filter
    every line would be staged once per PO on the notice, which is finding C1's duplicate
    receipts.
    """
    if notice.kind != NoticeKind.INBOUND and notice.kind != NoticeKind.DELIVERED:
        return []

    tracking = notice.tracking_numbers[0] if notice.tracking_numbers else None
    extraction_source = f"authority_{notice.kind.value}"
    records: List[ExtractedRecord] = []

    for line in notice.lines:
        if only_po and line.po_number != only_po:
            continue

        # Confidence reflects how much of the match key the source stated outright. An Inbound
        # line with an explicit Spitfire line number needs no fuzzy matching at all; a Delivered
        # line, which carries no line number, cannot claim the same.
        if line.line_number is not None and line.quantity is not None:
            confidence = 1.0
        elif line.spec_code and line.quantity is not None:
            confidence = 0.9
        elif line.spec_code or line.quantity is not None:
            confidence = 0.6
        else:
            confidence = 0.3

        records.append(ExtractedRecord(
            source_email_id=source_email_id,
            po_number=line.po_number,
            shipment_number=notice.shipment_number,
            spec_code=line.spec_code,
            parent_spec_code=line.parent_spec_code,
            sub_spec_suffix=line.sub_spec_suffix,
            item_description=line.description,
            vendor_name=line.supplier,
            carrier_name=notice.carrier,
            tracking_number=tracking,
            quantity_received=line.quantity,
            unit_of_measure=line.unit_of_measure,
            pod_stated_date=notice.received_date,
            email_date=email_date,
            delivery_location=notice.received_at,
            comments=line.comments,
            extraction_source=extraction_source,
            extraction_confidence=confidence,
            raw_snippet=" | ".join(filter(None, [
                f"{line.po_number} : {line.line_number}" if line.line_number is not None else line.po_number,
                line.supplier, line.part_number, line.item_cell, line.package_cell, line.comments,
            ]))[:1000],
            po_line_number=line.line_number,
            received_by=notice.received_by,
            package_quantity=line.package_quantity,
            package_uom=line.package_uom,
            notification_number=notice.notice_number,
        ))
    return records
