"""Delivery status per purchase-order line — **derived, and deliberately provisional.**

The 8 August meeting agreed a six-state lifecycle: open → shipped / in transit → at the partnered
warehouse → delivered → POD submitted → pushed to Spitfire. **M1 builds the real thing** — statuses
persisted against each line with a timestamped audit row per transition. This module is not that. It
infers a status from signals that already exist so the `/po` route can be built and looked at before
M1 lands, and it is meant to be deleted when M1 replaces it.

Two consequences are structural, not temporary gaps in the code:

* **`AT_WAREHOUSE` can never be returned.** Nothing in the data marks the vendor → partnered
  warehouse → property leg. That leg is a real and distinct path, and collapsing it into `IN_TRANSIT`
  is what makes two physical receipts look like one — the exact failure the third status was added to
  prevent. So it is reported as unreachable rather than guessed at.
* **`PUSHED_TO_SPITFIRE` can never be returned** either, because nothing writes to Spitfire in this
  phase. `connectors/spitfire.py` has no write methods at all.

Both are surfaced in `UNREACHABLE_STATUSES` so the UI can label them, instead of a viewer concluding
the pipeline simply never reaches those states.
"""

from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence

from api.stores.emails_store import DemoEmail
from api.stores.po_lines_store import POLineRow
from pipeline.delivery_status import (  # the vocabulary, shared with the pipeline-side derivation
    AT_WAREHOUSE,
    DELIVERED,
    IN_TRANSIT,
    LIFECYCLE,
    OPEN,
    POD_SUBMITTED,
    PUSHED_TO_SPITFIRE,
    STATUS_LABELS,
)
from pipeline import delivery_status

UNREACHABLE_STATUSES: Sequence[str] = (AT_WAREHOUSE, PUSHED_TO_SPITFIRE)
"""Not "no rows currently have this". Cannot be produced at all — see the module docstring.

Specific to *this* derivation. The pipeline-side one reaches `at_warehouse` (its mail carries a
`warehouse_inbound` notification type) but cannot reach `pod_submitted` (the pipeline store has no
staged receipts). Which statuses are unreachable is a property of the evidence available, not of
the vocabulary, so it does not belong in the shared module."""

# `demo_emails.status_keyword`. "shipped" is in the delivery report's in-transit bucket and is never
# emitted by the seed catalogue; it is honoured here anyway so the two agree if it ever appears.
DELIVERED_KEYWORDS = frozenset({"delivered", "received"})
IN_TRANSIT_KEYWORDS = frozenset({"out_for_delivery", "shipped"})


@dataclass
class LineStatus:
    """One PO line and the status inferred for it, with the evidence that produced it."""
    line: POLineRow
    status: str
    reason: str

    @property
    def label(self) -> str:
        return STATUS_LABELS[self.status]


def derive_line_status(
    line: POLineRow,
    emails: Sequence[DemoEmail] = (),
    has_staged_receipt: bool = False,
) -> LineStatus:
    """The most advanced status this line has evidence for, plus why.

    `reason` is not decoration. Every status shown to a person is a guess made by this function, and
    a guess a reviewer cannot interrogate is worse than no status — they have no way to tell an
    inference from a fact. It is carried through to the API and rendered in the drawer.
    """
    if has_staged_receipt:
        return LineStatus(line, POD_SUBMITTED, "a receipt has been staged for this line")

    # `delivered` needs LINE-level evidence, and only quantity provides it.
    #
    # An email carries a PO number and no line reference (`demo_emails` has no line column, and the
    # real mail usually names no line either — that is the entire reason a matching stage exists).
    # So "a delivery notice arrived for PO 212456" cannot settle any particular line. Letting it
    # try was a bug caught by test_po_212456_rolls_up_to_open_despite_a_delivered_line: one carrier
    # email marked all three lines delivered, including two with nothing received, which made the
    # least-advanced rollup meaningless — every PO with any delivery mail read as fully delivered.
    if line.line.qty_ordered > 0 and line.line.qty_received >= line.line.qty_ordered:
        return LineStatus(
            line, DELIVERED,
            f"received {line.line.qty_received:g} of {line.line.qty_ordered:g} ordered",
        )

    if line.line.qty_in_transit > 0:
        return LineStatus(
            line, IN_TRANSIT, f"{line.line.qty_in_transit:g} unapproved on an existing receipt",
        )

    # Email evidence is therefore a floor, never a ceiling: it can lift a line off `open`, because
    # goods demonstrably moved somewhere on this PO, but it can never mark one arrived.
    keywords = {e.status_keyword for e in emails}
    if keywords & (DELIVERED_KEYWORDS | IN_TRANSIT_KEYWORDS):
        return LineStatus(
            line, IN_TRANSIT,
            "a delivery or carrier notification arrived for this PO, but names no line",
        )

    return LineStatus(line, OPEN, "no delivery signal yet")


# Thin adapters: this module works in `LineStatus` objects, the shared module in plain status
# strings. The rules themselves — least-advanced wins, every key present — live in one place.

def rollup(line_statuses: Iterable[LineStatus]):
    return delivery_status.rollup(ls.status for ls in line_statuses)


def count_by_status(line_statuses: Iterable[LineStatus]) -> Dict[str, int]:
    return delivery_status.count_by_status(ls.status for ls in line_statuses)


def statuses_for_po(
    lines: Sequence[POLineRow],
    emails: Sequence[DemoEmail] = (),
    staged_line_ids: Sequence[int] = (),
) -> List[LineStatus]:
    staged = set(staged_line_ids)
    return [derive_line_status(line, emails, line.id in staged) for line in lines]
