"""Premier's POD-date rule, as stated on 2026-09-03.

    If the document gives a delivery date, that is the date.
    If it does not, the date the mail was received is the date.

Two halves, tested separately because they live in different places: the first is `stage_records`
declining to touch a record that already has a date or a linked POD, the second is
`_fallback_delivery_date`.

The rule matters because of how rarely the first half applies. Of 2,332 records with no POD date,
**only 2** came from an email that carried a recognised POD — across the whole store just 8 records
ever got a date from an actual POD document. Vendors do not send them. So the fallback is not an
edge case here; it is the mechanism, and getting it wrong is wrong on thousands of rows.

`_fallback_delivery_date` used to prefer `origin_sent_at` — when the message was *written* — over
the received date, on the reasoning that a forwarded thread still describes the delivery its author
saw. On **48 of 1,642** messages those fall on different days, and on each of those it stamped a
date Premier had not asked for. That is what `test_the_received_date_wins_when_they_differ` holds.
"""

import sqlite3

import pytest

from pipeline import state_db
from pipeline.ingest_orchestrator import _fallback_delivery_date


@pytest.fixture
def conn(tmp_path):
    connection = state_db.get_connection(tmp_path / "state.sqlite3")
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


def _logged(conn, email_id: str, *, received: str, origin_sent: str = None) -> None:
    from pipeline import email_log

    email_log.record(
        conn, email_id=email_id, subject="Delivery", sender="v@example-tile.test",
        email_date=received, origin_sent_at=origin_sent, category="hold",
        folder="Processed", processed_at="2026-09-03T12:00:00Z",
    )


def test_the_received_date_is_used_when_the_document_states_nothing(conn):
    """The stated rule, in its ordinary case."""
    _logged(conn, "<a@example-tile.test>", received="2026-09-01T08:30:00+00:00")

    assert _fallback_delivery_date(conn, "<a@example-tile.test>") == (
        "2026-09-01", "email_received_date")


def test_the_received_date_wins_when_they_differ(conn):
    """The regression this rule exists to prevent, and the assertion that failed before it.

    A message written on the 25th and received on the 2nd used to be stamped 2026-08-25. Premier's
    rule says the received date, so it is 2026-09-02 — and `pod_source` must say so too, or the
    receiver report cannot tell which date it is looking at.
    """
    _logged(conn, "<fwd@example-tile.test>",
            received="2026-09-02T09:00:00+00:00", origin_sent="2026-08-25T14:00:00+00:00")

    date, source = _fallback_delivery_date(conn, "<fwd@example-tile.test>")

    assert date == "2026-09-02", "the received date is the rule; the sent date is not"
    assert source == "email_received_date"


def test_the_sent_date_is_never_used(conn):
    """Belt and braces on the same point, stated as an absolute.

    `email_sent_date` was a real `pod_source` value and 1 record still carries it. Nothing may
    produce a new one — a rule that varies by message is not a rule.
    """
    _logged(conn, "<b@example-tile.test>",
            received="2026-09-02T09:00:00+00:00", origin_sent="2026-08-25T14:00:00+00:00")

    assert _fallback_delivery_date(conn, "<b@example-tile.test>")[1] != "email_sent_date"


def test_no_date_at_all_yields_nothing_rather_than_a_guess(conn):
    """`None`, not today's date. An invented delivery date is worse than an absent one: it reads
    as fact on the receiver report and nothing downstream can tell it was made up."""
    _logged(conn, "<c@example-tile.test>", received="")

    assert _fallback_delivery_date(conn, "<c@example-tile.test>") is None


def test_an_unknown_email_yields_nothing(conn):
    assert _fallback_delivery_date(conn, "<never-seen@example-tile.test>") is None


def test_the_date_is_a_day_not_an_instant(conn):
    """A delivery is reported on a day. Keeping the time would make two records of the same
    delivery, stamped from two messages minutes apart, look like different dates."""
    _logged(conn, "<d@example-tile.test>", received="2026-09-01T23:59:59+00:00")

    date, _ = _fallback_delivery_date(conn, "<d@example-tile.test>")

    assert date == "2026-09-01" and len(date) == 10


def test_a_stated_document_date_is_never_overwritten():
    """The first half of the rule, held at its call site rather than here.

    `stage_records` only consults the fallback when `pod_stated_date` is empty *and* no POD is
    linked. If that guard ever loosened, a carrier's real date would be replaced by an email
    timestamp — the one outcome that would make the receiver report actively wrong rather than
    incomplete.
    """
    import inspect

    from pipeline import ingest_orchestrator

    source = inspect.getsource(ingest_orchestrator.stage_records)

    assert "if not record.pod_stated_date and pod_ledger_id is None:" in source, (
        "the fallback must stay behind both guards: a stated date and a linked POD each win")


# --- the backfill ----------------------------------------------------------
#
# It exists because 2,332 records were staged before the rule did. Its guarantees matter more than
# its effect: it writes to a financial system of record's source data, so the ways it must NOT
# behave are the ones worth holding.

def _record(conn, *, email_id, pod_date="", pod_ledger_id=None) -> int:
    cur = conn.execute(
        "INSERT INTO extracted_records (source_email_id, po_number, pod_stated_date, "
        "pod_ledger_id, email_date, extraction_source, extraction_confidence, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'test', 1.0, 'pending', ?)",
        (email_id, "210634", pod_date, pod_ledger_id, "2026-09-01", "2026-09-01T00:00:00Z"))
    conn.commit()
    return cur.lastrowid


class _KeepOpen:
    """The test's connection, with `close()` disarmed.

    `main` closes what it opens, which is right for a tool and inconvenient here — the assertions
    run afterwards on the same in-memory-backed file. A sqlite3.Connection rejects `setattr`, so
    this delegates rather than patches.
    """

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


def _run_backfill(conn, monkeypatch, apply=True):
    from tools import backfill_pod_fallback

    monkeypatch.setattr(backfill_pod_fallback.state_db, "get_connection",
                        lambda *a, **k: _KeepOpen(conn))
    return backfill_pod_fallback.main(["--apply"] if apply else [])


def test_the_backfill_dates_a_record_that_has_none(conn, monkeypatch):
    _logged(conn, "<x@example-tile.test>", received="2026-09-01T08:30:00+00:00")
    rid = _record(conn, email_id="<x@example-tile.test>")

    _run_backfill(conn, monkeypatch)

    row = conn.execute("SELECT pod_stated_date, pod_source FROM extracted_records WHERE id=?",
                       (rid,)).fetchone()
    assert row["pod_stated_date"] == "2026-09-01"
    assert row["pod_source"] == "email_received_date"


def test_the_backfill_never_overwrites_a_stated_date(conn, monkeypatch):
    """The first half of the rule. A date the document gave always wins — overwriting it with an
    email timestamp would make the receiver report actively wrong, not merely incomplete."""
    _logged(conn, "<y@example-tile.test>", received="2026-09-01T08:30:00+00:00")
    rid = _record(conn, email_id="<y@example-tile.test>", pod_date="2026-08-20")

    _run_backfill(conn, monkeypatch)

    assert conn.execute("SELECT pod_stated_date FROM extracted_records WHERE id=?",
                        (rid,)).fetchone()[0] == "2026-08-20"


def test_the_backfill_skips_a_record_with_a_linked_pod(conn, monkeypatch):
    """A linked POD owns `pod_source`. Stamping over it would lose which file proved the receipt —
    the same guard `stage_records` applies."""
    _logged(conn, "<z@example-tile.test>", received="2026-09-01T08:30:00+00:00")
    rid = _record(conn, email_id="<z@example-tile.test>", pod_ledger_id=42)

    _run_backfill(conn, monkeypatch)

    row = conn.execute("SELECT pod_stated_date, pod_source FROM extracted_records WHERE id=?",
                       (rid,)).fetchone()
    assert not row["pod_stated_date"], "a record with a real POD linked must be left alone"
    assert row["pod_source"] is None


def test_the_backfill_invents_nothing_when_there_is_no_date(conn, monkeypatch):
    """An invented delivery date reads as fact on the report and nothing downstream can tell."""
    _logged(conn, "<w@example-tile.test>", received="")
    rid = _record(conn, email_id="<w@example-tile.test>")

    _run_backfill(conn, monkeypatch)

    assert not conn.execute("SELECT pod_stated_date FROM extracted_records WHERE id=?",
                            (rid,)).fetchone()[0]


def test_the_backfill_is_idempotent(conn, monkeypatch):
    """It will be run more than once — dry, then applied, then checked. A second pass must be a
    no-op rather than re-stamping rows."""
    _logged(conn, "<r@example-tile.test>", received="2026-09-01T08:30:00+00:00")
    rid = _record(conn, email_id="<r@example-tile.test>")

    _run_backfill(conn, monkeypatch)
    first = conn.execute("SELECT pod_stated_date FROM extracted_records WHERE id=?",
                         (rid,)).fetchone()[0]
    _run_backfill(conn, monkeypatch)
    assert conn.execute("SELECT pod_stated_date FROM extracted_records WHERE id=?",
                        (rid,)).fetchone()[0] == first


def test_a_dry_run_changes_nothing(conn, monkeypatch):
    """The default. This writes to the source data of a financial system of record, so doing
    nothing has to be what happens when nobody asked for a write."""
    _logged(conn, "<d@example-tile.test>", received="2026-09-01T08:30:00+00:00")
    rid = _record(conn, email_id="<d@example-tile.test>")

    _run_backfill(conn, monkeypatch, apply=False)

    assert not conn.execute("SELECT pod_stated_date FROM extracted_records WHERE id=?",
                            (rid,)).fetchone()[0]
