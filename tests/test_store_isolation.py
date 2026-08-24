"""Sample data must never appear in a live receiver report.

The guarantee is structural — two database files rather than a `source` column — so these tests
assert the structure rather than the queries. `pipeline/read_views.py` and
`pipeline/receipt_log.py` are deliberately unfiltered whole-table reads; that is safe only for as
long as a connection can see exactly one store, which is what is checked here.
"""

import sqlite3

import pytest

from config import settings
from pipeline import receipt_log
from pipeline import extracted_records_store, read_views, state_db
from tests.test_read_views import make_record   # every field defaulted; one helper, not two


def _store(path, po: str, spec: str):
    conn = state_db.get_connection(path)
    conn.row_factory = sqlite3.Row
    record = make_record(po_number=po, spec_code=spec, source_email_id=f"<{po}@example.test>")
    extracted_records_store.write_pending(conn, record, "2026-08-08T00:00:00Z")
    return conn


def test_path_for_maps_the_two_sources():
    assert state_db.path_for("mailbox") == settings.PIPELINE_STATE_DB_PATH
    assert state_db.path_for("sample") == settings.SAMPLE_STATE_DB_PATH
    assert state_db.path_for("mailbox") != state_db.path_for("sample")


@pytest.mark.parametrize("bad", ["", "Sample", "live", "corpus", None])
def test_path_for_refuses_anything_else(bad):
    """It must raise rather than fall back. A typo resolving to the live store by default is the
    exact failure the split exists to prevent."""
    with pytest.raises(ValueError):
        state_db.path_for(bad)


def test_neither_store_can_see_the_other(tmp_path):
    live = _store(tmp_path / "live.sqlite3", "212456", "STE-402")
    sample = _store(tmp_path / "sample.sqlite3", "999999", "SAMPLE-1")
    try:
        live_report = receipt_log.build(live)
        sample_report = receipt_log.build(sample)

        assert [po.po_number for po in live_report.purchase_orders] == ["212456"]
        assert [po.po_number for po in sample_report.purchase_orders] == ["999999"]

        # The report query has no source predicate and is not meant to grow one.
        assert "999999" not in str(receipt_log.to_html(live_report))
        assert "212456" not in str(receipt_log.to_html(sample_report))

        assert read_views.summary(live).records_total == 1
        assert read_views.summary(sample).records_total == 1
    finally:
        live.close()
        sample.close()


def test_corpus_tool_defaults_to_the_sample_store():
    """A bare `python -m tools.ingest_corpus` must not be able to reach Premier's real mail."""
    from tools import ingest_corpus, ingest_mailbox

    assert ingest_corpus.DEFAULT_DB_PATH == settings.SAMPLE_STATE_DB_PATH
    assert ingest_mailbox.DEFAULT_DB_PATH == settings.PIPELINE_STATE_DB_PATH


def test_orchestrator_refuses_to_guess_a_store():
    """`conn` used to default to the live store, so a caller that forgot wrote sample rows into
    Premier's mail. It must now fail loudly instead."""
    from pipeline import ingest_orchestrator, stage1_ingest

    with pytest.raises(ValueError, match="conn is required"):
        ingest_orchestrator.process_new_mail(mailbox=None, conn=None)
    with pytest.raises(ValueError, match="conn is required"):
        stage1_ingest.fetch_new_emails(mailbox=None, conn=None)
