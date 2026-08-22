"""What a record still lacks before it can become a receiver line.

One definition, used by three screens that were each answering the question differently — the
Records page ("Ready to process further"), the manual queue ("needs a human"), and the Receipt Log
itself. Before this, `read_views._READY_CLAUSE` was the only gate and it asked for a PO number, a
confidence above zero, and no quantity conflict. Nothing else. Against Premier's live mailbox that
let three records through labelled *ready* while carrying **no proof of delivery at all** — no
delivery date, no carrier, no tracking, no signature, no PO line number. They cannot be compared
against a purchase order, which is the entire point of extracting them.

`REQUIRED` is what only the email can tell us, and nothing else can supply. Everything a purchase
order already knows was taken out of it on 2026-08-21 and moved to `DERIVED` — see below — because
demanding a vendor name from a delivery notification is asking a person to retype a fact Spitfire
is holding, and getting it wrong is then possible where before it was not.

Carrier and tracking are `ADVISORY` on purpose. A warehouse Inbound notification legitimately
carries neither — the goods moved inside the 3PL's own network — so demanding them would park
correct records in a human queue for ever.

**Nothing here blocks.** Gaps are reported, not enforced: an incomplete record still reaches the
Records page, flagged. Whoever builds the write path must refuse a record whose `is_complete` is
False, or **a receiver with no delivery date reaches Premier's ERP** — that is the one this list
still exists to prevent, and `post_decision` honours it.

A *missing POD file* is no longer part of that judgement. It is a separate gate, in
`post_decision`, because it has a separate remedy: a person may waive it for a delivery stated
entirely in the email body, and nothing else here is waivable.
"""

from dataclasses import dataclass, field
from typing import Any, List, Mapping, Sequence

REQUIRED: Sequence[str] = (
    "po_number",           # DocNo — nothing downstream can match without it
    "spec_code",           # what was actually delivered, as Premier names it
    "item_description",    # Description
    "quantity_received",   # Received — a receipt line without one cannot be created at all
    "pod_stated_date",     # proof it arrived, and *when*. The one fact no other system holds.
)
"""The five facts only the delivery notification can supply. All five, or the record is incomplete."""

DERIVED: Sequence[str] = (
    "vendor_name",         # spitfire_po_lines.vendor_name, on the matched line
    "unit_of_measure",     # spitfire_po_lines.unit_of_measure — post_decision already reads it there
    "po_line_number",      # resolved by spec match, or chosen by a reviewer from the PO's own lines
    "received_by",         # the POD's `Signed for by:`, else the reviewer who accepted the record
)
"""Fields a record needs but a person must never be asked to type.

Each has a source that already holds the authoritative value, so requiring them of the *email* was
requiring the wrong thing. Measured on the 94 live records: `received_by` alone blocked 22 of them
and appeared in the gap set of 71.

They are listed rather than deleted so the reason survives — and because `receipt_log` fills the
first two from `spitfire_po_lines` at build time, which only makes sense if you can see here that
their absence from the record is expected rather than a defect.

`post_decision` does not read this tuple: it already derives what it needs from the matched PO line
(`check.unit_of_measure`, `_cost_code_of`) and takes the line number from the record itself. The
tuple is documentation and is what the UI uses to label a cell "from the purchase order".
"""

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
