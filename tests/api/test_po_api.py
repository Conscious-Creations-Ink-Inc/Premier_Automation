"""The `/api/po` delivery-status view.

Two things are pinned here above all: that the PO rollup takes the **least** advanced line rather
than the most, and that the two statuses this build cannot derive are reported as unreachable
instead of being quietly folded into a neighbour. Both are places where a wrong answer still looks
like a working page, which is what makes them worth a test rather than a code review.
"""

import pytest

from api.services import po_status
from api.stores.emails_store import DemoEmail
from tests.api.conftest import make_line

SEEDED_POS = {"912456", "912547", "906534", "998033", "913987", "914902"}


def _email(status_keyword: str, po_number: str = "912456") -> DemoEmail:
    return DemoEmail(
        email_id=f"em-{status_keyword}", received_at="2026-07-20T08:30:00+00:00",
        sender_address="warehouse@example-logistics.test", sender_domain="example-logistics.test",
        subject="Delivery notification", body_snippet=None,
        notification_type="delivered_shipped", triage_category="hide", matched_rule="rule",
        reason="", status_keyword=status_keyword, po_number=po_number, has_attachment=False,
        proposed_folder="Hidden", keep_in_inbox=False, organized_folder=None, moved_at=None,
    )


# --- the derived ladder -------------------------------------------------------


def test_a_line_with_nothing_against_it_is_open():
    assert po_status.derive_line_status(make_line()).status == po_status.OPEN


def test_quantity_outranks_an_email():
    """A delivery email is a claim; received quantity is the ledger. When they disagree the ledger
    wins, and the stated reason has to name the numbers so a reviewer can see which was used."""
    line = make_line(qty_ordered=12.0, qty_received=12.0)
    result = po_status.derive_line_status(line, [_email("out_for_delivery")])
    assert result.status == po_status.DELIVERED
    assert "12" in result.reason


def test_an_email_can_never_mark_a_line_delivered():
    """An email names a PO and no line — that is why a matching stage exists at all. So delivery
    mail lifts a line off `open` and no further; only received quantity settles it.

    Without this the rollup is worthless: one carrier notification would mark every line on the PO
    delivered, including lines with nothing received."""
    line = make_line(qty_ordered=12.0, qty_received=0.0)
    result = po_status.derive_line_status(line, [_email("delivered")])
    assert result.status == po_status.IN_TRANSIT
    assert "names no line" in result.reason


def test_unapproved_receipt_reads_as_in_transit():
    """qty_in_transit is Spitfire's ReceiptInProgressUnits — a receipt raised but not approved."""
    line = make_line(qty_ordered=12.0, qty_received=0.0, qty_in_transit=5.0)
    assert po_status.derive_line_status(line).status == po_status.IN_TRANSIT


def test_staged_receipt_beats_everything_below_it():
    line = make_line(qty_ordered=12.0, qty_received=12.0)
    result = po_status.derive_line_status(line, [_email("delivered")], has_staged_receipt=True)
    assert result.status == po_status.POD_SUBMITTED


def test_a_zero_quantity_line_is_not_delivered_by_default():
    """`qty_received >= qty_ordered` is true for 0 >= 0. A line ordering nothing must not report
    itself delivered — tax and freight lines arrive with ContractUnits 0.0."""
    assert po_status.derive_line_status(make_line(qty_ordered=0.0, qty_received=0.0)).status \
        == po_status.OPEN


def test_every_status_has_a_label():
    """Superset, not equality: `STATUS_LABELS` also names the terminal branches (cancelled,
    loss/claim), which are deliberately outside `LIFECYCLE` because they are not positions on the
    way to delivery. Every rung still has to be named."""
    assert set(po_status.LIFECYCLE) <= set(po_status.STATUS_LABELS)


# --- the rollup ---------------------------------------------------------------


def test_rollup_takes_the_least_advanced_line():
    """The whole design decision. Taking the *most* advanced line would report a PO as delivered
    the moment one of its lines arrived — the over-optimistic number that makes a lifecycle
    dashboard untrustworthy."""
    statuses = [
        po_status.derive_line_status(make_line(line_id=1, qty_ordered=12.0, qty_received=12.0)),
        po_status.derive_line_status(make_line(line_id=2, qty_ordered=4.0, qty_received=0.0)),
    ]
    assert [s.status for s in statuses] == [po_status.DELIVERED, po_status.OPEN]
    assert po_status.rollup(statuses) == po_status.OPEN


def test_rollup_of_nothing_is_none():
    assert po_status.rollup([]) is None


def test_counts_cover_every_status_even_at_zero():
    """The UI renders "3 of 5 delivered" from this; a missing key would be a KeyError on a page,
    not a missing number."""
    counts = po_status.count_by_status([po_status.derive_line_status(make_line())])
    assert set(po_status.LIFECYCLE) <= set(counts)
    assert counts[po_status.OPEN] == 1
    assert counts[po_status.DELIVERED] == 0


# --- the endpoints ------------------------------------------------------------


def test_list_returns_every_seeded_po(client):
    body = client.get("/api/po").json()
    assert {row["po_number"] for row in body["rows"]} == SEEDED_POS
    assert body["derived"] is True


def test_list_declares_the_statuses_it_cannot_produce(client):
    """Not "none currently have this" — cannot be produced at all. The partnered-warehouse leg has
    no signal in the data, and nothing writes to Spitfire in this phase."""
    body = client.get("/api/po").json()
    assert set(body["unreachable_statuses"]) == {
        po_status.AT_WAREHOUSE, po_status.PUSHED_TO_SPITFIRE,
    }
    assert all(row["status"] not in body["unreachable_statuses"] for row in body["rows"])


def test_po_212456_does_not_read_as_delivered_while_two_lines_are_unreceived(client):
    """The seeded case that proves the rollup rule. PO 912456 has three lines: em-1001 settled
    line 1 (auto-approved, receipt staged), and lines 2 and 3 have had nothing. It also carries two
    carrier emails, one of them `delivered`.

    So the PO must report the least-advanced line — in transit, because the carrier mail proves
    *something* moved — and never `delivered`, which is both wrong and entirely plausible-looking."""
    row = next(r for r in client.get("/api/po").json()["rows"] if r["po_number"] == "912456")
    assert row["line_count"] == 3
    assert row["status"] == po_status.IN_TRANSIT
    assert row["status"] != po_status.DELIVERED
    assert row["counts_by_status"][po_status.POD_SUBMITTED] == 1    # line 1
    assert row["counts_by_status"][po_status.IN_TRANSIT] == 2       # lines 2 and 3


def test_detail_returns_lines_and_the_email_audit_trail(client):
    body = client.get("/api/po/912456").json()
    assert len(body["lines"]) == 3
    assert all(line["reason"] for line in body["lines"]), "every status must state its evidence"
    assert body["email_count"] == len(body["emails"])
    assert {e["po_number"] for e in body["emails"]} == {"912456"}


def test_detail_404s_for_a_po_that_is_not_in_the_catalogue(client):
    """PO 999111 appears on a seeded extracted record but has no PO lines — the "nothing to match"
    case. A 200 with an empty list would read as "this PO has no lines on it"."""
    response = client.get("/api/po/999111")
    assert response.status_code == 404
    assert "999111" in response.json()["detail"]


def test_quantities_roll_up_across_lines(client):
    """PO 912456 orders 12 + 4 + 24."""
    row = next(r for r in client.get("/api/po").json()["rows"] if r["po_number"] == "912456")
    assert row["qty_ordered"] == 40.0
    assert row["qty_outstanding"] == row["qty_ordered"] - row["qty_received"] - row["qty_in_transit"]


def test_the_view_reports_no_money(client):
    """The meeting asked for dollars by status. A PO line carries no rate or amount until M4, so
    this page shows quantities — and must not grow a money-shaped field that is silently zero."""
    row = client.get("/api/po").json()["rows"][0]
    assert not [key for key in row if any(word in key for word in ("amount", "value", "cost_total"))]
