"""The demo dataset — that it is self-consistent, complete and reproducible."""
from api import db
from api.demo import catalog
from api.demo import seed as demo_seed
from api.stores import po_lines_store, reconciliation_store


def test_seed_produces_the_documented_dataset(conn):
    counts = demo_seed.summary(conn)

    assert counts["po_lines"] == len(catalog.PO_LINES)
    assert counts["emails"] == len(catalog.EMAILS)
    assert counts["extracted_records"] == len(catalog.CLEAN_RECORDS) + len(catalog.FLAGGED_RECORDS)
    assert counts["matches"] == counts["extracted_records"]


def test_every_clean_record_settles_and_every_flagged_one_queues(conn):
    """The fixtures are meant to split exactly this way — if scoring drifts, this fails."""
    counts = demo_seed.summary(conn)

    assert counts["auto_approved"] == len(catalog.CLEAN_RECORDS)
    assert counts["exception_queue"] == len(catalog.FLAGGED_RECORDS)
    assert counts["staged_receipts"] == len(catalog.CLEAN_RECORDS)


def test_the_queue_is_never_empty(conn):
    """A demo with nothing to review would not show the feature it exists to show."""
    assert len(reconciliation_store.list_exception_queue(conn)) > 0


def test_auto_approved_receipts_moved_quantity_onto_their_lines(conn):
    settled = reconciliation_store.list_matches(
        conn, review_status=reconciliation_store.REVIEW_AUTO_APPROVED
    )

    assert settled, "expected the automation to settle some records"
    for match in settled:
        line = po_lines_store.get(conn, match.po_line_id).line
        assert line.qty_received > 0


def test_the_queue_covers_a_range_of_reasons(conn):
    """Each flagged fixture exercises a different failure mode; a reviewer should meet them
    all, not eight copies of one."""
    reasons = " | ".join(m.flag_reason for m in reconciliation_store.list_exception_queue(conn))

    assert "missing quantity" in reasons
    assert "missing POD date" in reasons
    assert "missing spec ID" in reasons
    assert "no PO line matched" in reasons
    assert "outstanding" in reasons          # the over-receipt case


def test_seeding_is_deterministic():
    """Two fresh databases must come out identical — the demo has to look the same every time
    it is rebuilt."""
    first, second = db.get_demo_connection(":memory:"), db.get_demo_connection(":memory:")
    try:
        demo_seed.seed_demo(first)
        demo_seed.seed_demo(second)

        keys_first = [row.line.line_key for row in po_lines_store.list_all(first)]
        keys_second = [row.line.line_key for row in po_lines_store.list_all(second)]

        assert keys_first == keys_second
        assert demo_seed.summary(first) == demo_seed.summary(second)
    finally:
        first.close()
        second.close()


def test_reset_returns_to_the_baseline(conn):
    baseline = demo_seed.summary(conn)
    conn.execute("DELETE FROM match_results")
    conn.commit()

    assert demo_seed.seed_demo(conn, reset_first=True) == baseline
