"""The delivery lifecycle vocabulary — the words, in one place.

The 8 August meeting agreed six states: open → shipped / in transit → at the partnered warehouse →
delivered → POD submitted → pushed to Spitfire.

Two different derivations exist, because two databases carry different evidence:

* `api/services/po_status.py` infers status over the **demo** database — seeded PO lines, staged
  receipts, synthetic mail — for the React dashboard.
* `pipeline/read_views.po_delivery_status` infers it over the **pipeline** store — real
  notification types accumulated from Premier's actual mail — for the `/ui` pages.

The derivations must differ; the vocabulary must not. Keeping `LIFECYCLE` and `STATUS_LABELS` here,
imported by both, is what stops two copies drifting into two different sets of words for the same
six states — the exact failure `api/schemas.py` and the frontend's `badges.tsx` each warn about
separately. It lives in `pipeline/` rather than `api/` because `pipeline/` never imports `api/`.

M1 will replace both derivations with persisted statuses. It should keep these constants.
"""

from typing import Dict, Iterable, Optional, Sequence

OPEN = "open"
IN_TRANSIT = "in_transit"
AT_WAREHOUSE = "at_warehouse"
DELIVERED = "delivered"
POD_SUBMITTED = "pod_submitted"
PUSHED_TO_SPITFIRE = "pushed_to_spitfire"

# Ordered least- to most-advanced. The order is load-bearing twice: a derivation uses the index to
# pick the highest status a line has evidence for, and `rollup` uses it to pick the lowest across a
# purchase order's lines. Reordering this tuple silently changes both.
LIFECYCLE: Sequence[str] = (
    OPEN, IN_TRANSIT, AT_WAREHOUSE, DELIVERED, POD_SUBMITTED, PUSHED_TO_SPITFIRE,
)

# Outcomes that end a delivery without completing the ladder. Kept OUT of `LIFECYCLE` deliberately:
# `rank()` and `rollup()` index into that tuple, and a cancellation is not a position on the way to
# delivery — it is the journey stopping. Giving it an index would make "least advanced" arithmetic
# treat it as progress.
CANCELLED = "cancelled"
LOSS_OR_CLAIM = "loss_or_claim"

TERMINAL_BRANCHES: Sequence[str] = (CANCELLED, LOSS_OR_CLAIM)
"""Loss/claim is deliberately not folded into cancelled. An order that was cancelled and goods that
were lost or damaged are different events with different consequences — one needs a PO update in
Spitfire, the other needs a claim filed against the carrier. A page that reported "cancelled" for a
lost pallet would send someone to do the wrong thing."""

STATUS_LABELS: Dict[str, str] = {
    OPEN: "Ordered",
    IN_TRANSIT: "In transit",
    AT_WAREHOUSE: "At partnered warehouse",
    DELIVERED: "Delivered",
    POD_SUBMITTED: "Receipt staged",
    PUSHED_TO_SPITFIRE: "In Spitfire",
    CANCELLED: "Cancelled",
    LOSS_OR_CLAIM: "Loss or claim",
}

# `at_warehouse` is deliberately its own state rather than a flavour of `in_transit`. Vendor →
# Premier's partnered warehouse → onward to the property is a real and distinct path, and
# collapsing it makes two physical receipts look like one — the failure that ended Premier's
# previous attempt at this.


def rank(status: str) -> int:
    return LIFECYCLE.index(status)


def rollup(statuses: Iterable[str]) -> Optional[str]:
    """A purchase order's status: the **least advanced** of its lines, unless something ended it.

    A PO is not delivered while any line on it is still open. Taking the most advanced line instead
    — the tempting reading of "has this PO arrived?" — reports a PO as delivered the moment one of
    several lines lands, which is the over-optimistic number that makes a lifecycle view
    untrustworthy.

    A terminal branch wins outright, and does not participate in that comparison. A cancelled PO is
    not "less advanced than in transit"; it is finished. Where both terminals are present, cancelled
    wins — an order called off is settled, while a claim on part of it is still open business.

    Returns None for a PO with nothing at all; the caller decides whether that is an absence or a 404.
    """
    known = list(statuses)
    if CANCELLED in known:
        return CANCELLED
    if LOSS_OR_CLAIM in known:
        return LOSS_OR_CLAIM
    on_ladder = [s for s in known if s in LIFECYCLE]
    return min(on_ladder, key=rank) if on_ladder else None


def count_by_status(statuses: Iterable[str]) -> Dict[str, int]:
    """Per-status counts, every key present even at zero.

    A view renders "3 of 5 delivered" from this, so a missing key would be a KeyError on a page
    rather than a missing number.
    """
    counts = {status: 0 for status in tuple(LIFECYCLE) + tuple(TERMINAL_BRANCHES)}
    for status in statuses:
        counts[status] += 1
    return counts
