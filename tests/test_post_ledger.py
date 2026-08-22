"""The duplicate guard.

This table is the only thing standing between a re-run and a second receipt on Premier's ERP.
Spitfire offers nothing to lean on: the catalog does not deduplicate identical bytes, attaching
twice creates two rows, and `ReceiptInProgressUnits` reads 0.0 against an unapproved receipt — so
every check has to happen here or not at all.
"""

import pytest

from pipeline import post_ledger as pl
from pipeline import state_db


@pytest.fixture
def conn():
    return state_db.get_connection(":memory:")


def test_the_same_delivery_cannot_be_claimed_twice(conn):
    first = pl.claim(conn, record_id=1, po_number="212614", line_number=1, pod_md5="ABC")
    assert first is not None and first.state == pl.CLAIMED

    assert pl.claim(conn, record_id=1, po_number="212614", line_number=1, pod_md5="ABC") is None


def test_the_race_is_settled_by_the_index_not_by_a_prior_read(conn):
    """Two operators pressing Post at the same moment both see "not yet claimed" if the check is a
    separate SELECT. The UNIQUE constraint is what actually decides, and `claim` returns None on
    the IntegrityError rather than letting it escape."""
    pl.claim(conn, record_id=2, po_number="212614", line_number=1, pod_md5="ABC")
    # No intervening read — straight into the second insert, as a racing request would.
    assert pl.claim(conn, record_id=2, po_number="212614", line_number=1, pod_md5="ABC") is None


def test_a_different_pod_on_the_same_line_is_a_different_delivery(conn):
    """A second genuine shipment against the same PO line must not be blocked by the first. The
    POD's content hash is what tells them apart — same paperwork means same delivery, new
    paperwork means new goods."""
    pl.claim(conn, record_id=3, po_number="212614", line_number=1, pod_md5="FIRST")
    assert pl.claim(conn, record_id=3, po_number="212614", line_number=1, pod_md5="SECOND")


def test_a_failed_attempt_may_be_retried_but_a_partial_one_may_not(conn):
    """The distinction is the reason both states exist. FAILED created nothing, so the delivery is
    free. PARTIAL left a real document on Premier's instance, and a retry would put a second one
    beside it."""
    failed = pl.claim(conn, record_id=4, po_number="212614", line_number=1, pod_md5="A")
    pl.settle(conn, failed.idempotency_key, pl.FAILED, "cookie expired before anything was made")
    assert not pl.find(conn, failed.idempotency_key).is_blocking

    partial = pl.claim(conn, record_id=5, po_number="212614", line_number=1, pod_md5="B")
    pl.settle(conn, partial.idempotency_key, pl.PARTIAL, "receipt exists, POD never attached")
    assert pl.find(conn, partial.idempotency_key).is_blocking


def test_a_claim_is_visible_before_the_work_finishes(conn):
    """Written before the first call, not after the last: a crash mid-chain must leave evidence
    that a receipt may exist, not silence."""
    pl.claim(conn, record_id=6, po_number="212614", line_number=1, pod_md5="A")
    stranded = pl.stranded(conn)
    assert len(stranded) == 1 and stranded[0].record_id == 6


def test_nothing_is_stranded_once_settled(conn):
    attempt = pl.claim(conn, record_id=7, po_number="212614", line_number=1, pod_md5="A")
    pl.settle(conn, attempt.idempotency_key, pl.POSTED, "receipt 0007")
    assert pl.stranded(conn) == []


def test_the_receipt_key_is_recorded_before_the_files_are_touched(conn):
    """Without it a crashed post says only that "a receipt might exist somewhere on training",
    with no way to find it."""
    attempt = pl.claim(conn, record_id=8, po_number="212614", line_number=1, pod_md5="A")
    pl.record_receipt(conn, attempt.idempotency_key, receipt_key="abc-123", receipt_doc_no="0009")
    stored = pl.find(conn, attempt.idempotency_key)
    assert stored.receipt_key == "abc-123" and stored.receipt_doc_no == "0009"
    assert stored.state == pl.CLAIMED, "recording progress must not settle the attempt"


def test_a_non_terminal_settle_is_rejected(conn):
    attempt = pl.claim(conn, record_id=9, po_number="212614", line_number=1, pod_md5="A")
    with pytest.raises(ValueError):
        pl.settle(conn, attempt.idempotency_key, pl.CLAIMED)


def test_latest_by_record_returns_the_newest_attempt_per_record(conn):
    """The Records page renders one row per record and must not show a stale first attempt beside
    a later one. Two claims in the same second are the double-submit case, so this cannot order by
    timestamp."""
    first = pl.claim(conn, record_id=10, po_number="212614", line_number=1, pod_md5="A")
    pl.settle(conn, first.idempotency_key, pl.FAILED, "first try")
    second = pl.claim(conn, record_id=10, po_number="212614", line_number=1, pod_md5="B")
    pl.settle(conn, second.idempotency_key, pl.POSTED, "second try")

    latest = {a.record_id: a for a in pl.latest_by_record(conn)}
    assert latest[10].state == pl.POSTED


def test_a_refusal_is_recorded_with_its_reason(conn):
    """The reason used to live only in the dialog the reviewer closed, so the next person clicked
    Post to rediscover it — at the cost of a live purchase-order read each time."""
    pl.record_refusal(conn, record_id=20, po_number="212559", line_number=1, pod_md5="A",
                      reason="the record is incomplete — missing: received-by")
    attempt = pl.find(conn, pl.idempotency_key(20, "212559", 1, "A"))
    assert attempt.state == pl.FLAGGED
    assert "received-by" in attempt.detail
    assert attempt.attempts == 1


def test_a_refusal_does_not_block_a_later_post(conn):
    """The whole point of the state. Records are refused for things that get fixed, and the moment
    one is corrected it must post with nothing to clear by hand — a flag that had to be dismissed
    would become a second queue nobody tends."""
    pl.record_refusal(conn, record_id=21, po_number="212559", line_number=1, pod_md5="A",
                      reason="quantities disagree")
    assert not pl.find(conn, pl.idempotency_key(21, "212559", 1, "A")).is_blocking
    assert pl.claim(conn, record_id=21, po_number="212559", line_number=1, pod_md5="A") is not None


def test_repeated_refusals_do_not_multiply_rows(conn):
    """Five clicks on an unchanged record are one fact and a count, not five events. Five rows of
    the same sentence would bury the twenty other records blocked for twenty other reasons."""
    for _ in range(5):
        pl.record_refusal(conn, record_id=22, po_number="212559", line_number=1, pod_md5="A",
                          reason="missing: received-by")
    rows = conn.execute("SELECT COUNT(*) FROM spitfire_post WHERE record_id = 22").fetchone()[0]
    assert rows == 1
    assert pl.find(conn, pl.idempotency_key(22, "212559", 1, "A")).attempts == 5


def test_a_refusal_never_overwrites_a_real_receipt(conn):
    """Turning the record of a posted receipt into a flag would lose the receipt number, and the
    receipt would still be sitting on Premier's instance."""
    attempt = pl.claim(conn, record_id=23, po_number="212559", line_number=1, pod_md5="A")
    pl.record_receipt(conn, attempt.idempotency_key, receipt_key="abc", receipt_doc_no="0004")
    pl.settle(conn, attempt.idempotency_key, pl.POSTED, "done")

    pl.record_refusal(conn, record_id=23, po_number="212559", line_number=1, pod_md5="A",
                      reason="already posted")
    stored = pl.find(conn, attempt.idempotency_key)
    assert stored.state == pl.POSTED and stored.receipt_doc_no == "0004"


def test_blocked_lists_only_the_flagged(conn):
    pl.record_refusal(conn, record_id=24, po_number="212559", line_number=1, pod_md5="A",
                      reason="missing: received-by")
    done = pl.claim(conn, record_id=25, po_number="212559", line_number=2, pod_md5="B")
    pl.settle(conn, done.idempotency_key, pl.POSTED, "receipt 0005")

    blocked = pl.blocked(conn)
    assert [b.record_id for b in blocked] == [24]


def test_the_audit_log_is_persisted_because_spitfire_cannot_tell_operators_apart(conn):
    """Every write lands as api@consciouscreations.ai regardless of who triggered it, so the ERP's
    own history cannot answer "who posted this"."""
    attempt = pl.claim(conn, record_id=11, po_number="212614", line_number=1, pod_md5="A",
                       actor="ashford")
    pl.settle(conn, attempt.idempotency_key, pl.POSTED, "done",
              audit=[{"method": "POST", "path": "/api/catalog/upload", "status": 200}])
    row = conn.execute("SELECT actor, audit_json FROM spitfire_post WHERE idempotency_key = ?",
                       (attempt.idempotency_key,)).fetchone()
    assert row[0] == "ashford"
    assert "/api/catalog/upload" in row[1]
