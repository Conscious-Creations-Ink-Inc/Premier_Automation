"""Approve and cancel — the human decisions, and the guarantees around them."""
import pytest

from api.services import decisions
from api.stores import extracted_store, po_lines_store, reconciliation_store

NOW = "2026-07-21T00:00:00+00:00"


def _find(conn, needle: str):
    """Pick the queued item flagged for a particular reason."""
    return next(
        match for match in reconciliation_store.list_exception_queue(conn)
        if needle in match.flag_reason
    )


def test_approve_is_blocked_until_the_gap_is_filled(conn):
    match = _find(conn, "missing quantity")

    with pytest.raises(decisions.DecisionError, match="Still missing"):
        decisions.approve(conn, match.id, "tester", NOW)


def test_approve_stages_a_receipt_and_moves_the_quantity(conn):
    match = _find(conn, "missing quantity")
    before = po_lines_store.get(conn, match.po_line_id).line.qty_received

    outcome = decisions.approve(
        conn, match.id, "tester", NOW, filled_fields={"quantity_received": 18}, note="phoned vendor"
    )

    assert outcome.review_status == reconciliation_store.REVIEW_APPROVED
    assert outcome.extracted_status == "matched"
    assert outcome.receipt_id is not None
    assert po_lines_store.get(conn, match.po_line_id).line.qty_received == before + 18
    assert extracted_store.get(conn, match.extracted_record_id).status == "matched"

    audit = reconciliation_store.decisions_for_match(conn, match.id)
    assert len(audit) == 1
    assert (audit[0].decision, audit[0].decided_by, audit[0].reason) == (
        "approve", "tester", "phoned vendor",
    )


def test_approving_twice_neither_double_counts_nor_double_receipts(conn):
    match = _find(conn, "missing quantity")
    decisions.approve(conn, match.id, "tester", NOW, filled_fields={"quantity_received": 18})
    qty_after_first = po_lines_store.get(conn, match.po_line_id).line.qty_received
    receipts_after_first = reconciliation_store.count_receipts(conn)

    again = decisions.approve(conn, match.id, "tester", NOW)

    assert again.already_applied is True
    assert po_lines_store.get(conn, match.po_line_id).line.qty_received == qty_after_first
    assert reconciliation_store.count_receipts(conn) == receipts_after_first


def test_approve_needs_a_line_when_the_automation_found_none(conn):
    match = _find(conn, "no PO line matched")

    with pytest.raises(decisions.DecisionError, match="No PO line is selected"):
        decisions.approve(conn, match.id, "tester", NOW)


def test_reviewer_can_resolve_an_unknown_po_by_choosing_the_line(conn):
    """The hardest case in the queue: nothing matched, so the reviewer supplies both the line
    and the missing spec."""
    match = _find(conn, "no PO line matched")
    line = po_lines_store.list_all(conn)[0]

    outcome = decisions.approve(
        conn, match.id, "tester", NOW,
        po_line_id=line.id, filled_fields={"spec_code": line.line.spec_code},
    )

    assert outcome.po_line_id == line.id
    assert outcome.extracted_status == "matched"


def test_cancel_requires_a_reason(conn):
    match = reconciliation_store.list_exception_queue(conn)[0]

    with pytest.raises(decisions.DecisionError, match="reason is required"):
        decisions.cancel(conn, match.id, "tester", "   ", NOW)


def test_cancel_marks_the_record_failed_and_keeps_the_reason_on_it(conn):
    """`mark_failed` appends to the record's own comments, so the reason travels with the
    record rather than living only in the audit table."""
    match = reconciliation_store.list_exception_queue(conn)[0]

    outcome = decisions.cancel(conn, match.id, "tester", "duplicate of an earlier receipt", NOW)

    assert outcome.extracted_status == "failed"
    row = extracted_store.get(conn, match.extracted_record_id)
    assert row.status == "failed"
    assert "duplicate of an earlier receipt" in (row.record.comments or "")


def test_cannot_cancel_something_already_approved(conn):
    match = _find(conn, "missing quantity")
    decisions.approve(conn, match.id, "tester", NOW, filled_fields={"quantity_received": 18})

    with pytest.raises(decisions.DecisionError, match="already approved"):
        decisions.cancel(conn, match.id, "tester", "changed my mind", NOW)


def test_decisions_shrink_the_queue(conn):
    before = len(reconciliation_store.list_exception_queue(conn))
    match = _find(conn, "missing quantity")

    decisions.approve(conn, match.id, "tester", NOW, filled_fields={"quantity_received": 18})

    assert len(reconciliation_store.list_exception_queue(conn)) == before - 1
