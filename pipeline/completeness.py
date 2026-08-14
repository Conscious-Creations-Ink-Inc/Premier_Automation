"""What a record still lacks before it can become a receiver line.

One definition, used by three screens that were each answering the question differently — the
Records page ("Ready to process further"), the manual queue ("needs a human"), and the Receipt Log
itself. Before this, `read_views._READY_CLAUSE` was the only gate and it asked for a PO number, a
confidence above zero, and no quantity conflict. Nothing else. Against Premier's live mailbox that
let three records through labelled *ready* while carrying **no proof of delivery at all** — no
delivery date, no carrier, no tracking, no signature, no PO line number. They cannot be compared
against a purchase order, which is the entire point of extracting them.

`REQUIRED` is not a taste call. It is `receipt_log._HEADERS` read backwards: DocNo, Vendor, Line,
Description, Order Qty, Received, Receiver. A record is complete exactly when the Receipt Log can
be built from it, so the two files cannot drift apart without a test noticing.

Carrier and tracking are `ADVISORY` on purpose. A warehouse Inbound notification legitimately
carries neither — the goods moved inside the 3PL's own network — so demanding them would park
correct records in a human queue for ever.

**Nothing here blocks.** Gaps are reported, not enforced: an incomplete record still reaches the
Records page, flagged. That is safe only while `stage4_match`-`stage7_route` remain stubs and
nothing posts to Spitfire. Whoever builds the write path must refuse a record whose
`is_complete` is False, or a receiver with no POD date reaches Premier's ERP.
"""

from dataclasses import dataclass, field
from typing import Any, List, Mapping, Sequence

REQUIRED: Sequence[str] = (
    "po_number",           # DocNo — nothing downstream can match without it
    "vendor_name",         # Vendor
    "po_line_number",      # Line
    "item_description",    # Description
    "quantity_received",   # Received
    "unit_of_measure",     # the quantity is meaningless without it
    "spec_code",           # what was actually delivered, as Premier names it
    "pod_stated_date",     # proof it arrived, and when
    "received_by",         # proof a person took it
)

ADVISORY: Sequence[str] = (
    "carrier_name",
    "tracking_number",
)

LABELS = {
    "po_number": "PO number",
    "vendor_name": "vendor",
    "po_line_number": "PO line #",
    "item_description": "description",
    "quantity_received": "quantity",
    "unit_of_measure": "UOM",
    "spec_code": "spec code",
    "pod_stated_date": "POD date",
    "received_by": "received-by",
    "carrier_name": "carrier",
    "tracking_number": "tracking #",
}


@dataclass(frozen=True)
class Gaps:
    missing_required: List[str] = field(default_factory=list)
    missing_advisory: List[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return not self.missing_required

    @property
    def count(self) -> int:
        return len(self.missing_required)

    def describe(self, advisory: bool = False) -> str:
        """`missing: POD date, received-by, PO line #` — the fields, never a score.

        A person fixing one of these needs to know which cells to fill. "confidence 0.0", which is
        what the queue said before, tells them only that something is wrong.
        """
        names = [LABELS.get(f, f) for f in self.missing_required]
        if advisory:
            names += [f"{LABELS.get(f, f)} (advisory)" for f in self.missing_advisory]
        return "missing: " + ", ".join(names) if names else ""


def _is_present(value: Any) -> bool:
    """Blank, whitespace and NULL are absent. Zero is **present**.

    A delivered quantity of 0 is a real statement — a shipment that arrived empty, or a line
    cancelled at the dock — and treating it as missing would hide exactly the case a person most
    needs to see. Only `po_line_number` 0 is suspect, and that is a matching problem, not a
    completeness one.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _get(row: Any, field_name: str) -> Any:
    """Read a field from a sqlite3.Row, a mapping, or an ExtractedRecord alike."""
    if isinstance(row, Mapping):
        return row.get(field_name)
    try:
        return row[field_name]                     # sqlite3.Row
    except (IndexError, KeyError, TypeError):
        return getattr(row, field_name, None)


def gaps(row: Any) -> Gaps:
    """Which required and advisory fields this record is missing."""
    return Gaps(
        missing_required=[f for f in REQUIRED if not _is_present(_get(row, f))],
        missing_advisory=[f for f in ADVISORY if not _is_present(_get(row, f))],
    )


def is_complete(row: Any) -> bool:
    return gaps(row).is_complete
