"""Tests for `state_db.forget_emails` — the scoped purge behind `--reset`.

The failure this guards against is not subtle but it is silent: the reset used to be
`DELETE FROM` six tables, which was indistinguishable from "forget the corpus" only for as long
as the corpus was the only thing in the database. It no longer is — `tools/ingest_mailbox.py`
writes mail read from Premier's live mailbox to the same file, and the dashboard's one button
calls that reset.
"""
import pytest

from pipeline import email_log, mail_overrides, state_db


@pytest.fixture
def conn():
    return state_db.get_connection(":memory:")


def record_email(conn, email_id: str, *, folder="Processed", category="surface"):
    email_log.record(
        conn, email_id=email_id, subject="", sender="", origin_sender=None, email_date="",
        notification_type=None, category=category, matched_rule="", reason="", po_hints="",
        shipment_hint=None, notification_number=None, attachment_count=0, ocr_attempted=0,
        folder=folder, error_type=None, processed_at="2026-08-05T00:00:00Z",
    )
    state_db.mark_seen(conn, email_id, "2026-08-05T00:00:00Z")


def accumulate(conn, email_id: str, po_number: str, shipment_number):
    conn.execute(
        "INSERT INTO accumulation (po_number, shipment_number, email_id, notification_type, "
        "category, received_at, payload_json) VALUES (?, ?, ?, 'inbound', 'surface', ?, '{}')",
        (po_number, shipment_number, email_id, "2026-08-05T00:00:00Z"),
    )
    conn.commit()


def release(conn, po_number: str, shipment_number):
    conn.execute(
        "INSERT INTO released_events (po_number, shipment_number, released_at, release_reason) "
        "VALUES (?, ?, ?, 'test')",
        (po_number, shipment_number, "2026-08-05T00:00:00Z"),
    )
    conn.commit()


def ledger_row(conn, email_id: str):
    conn.execute(
        "INSERT INTO attachment_ledger (email_id, sniffed_kind, disposition, first_seen_at, "
        "last_updated_at) VALUES (?, 'pdf', 'extracted', ?, ?)",
        (email_id, "2026-08-05T00:00:00Z", "2026-08-05T00:00:00Z"),
    )
    conn.commit()


def extracted_row(conn, email_id: str):
    conn.execute(
        "INSERT INTO extracted_records (source_email_id, email_date, extraction_source, "
        "extraction_confidence, status, created_at) VALUES (?, ?, 'test', 1.0, 'pending', ?)",
        (email_id, "2026-08-05T00:00:00Z", "2026-08-05T00:00:00Z"),
    )
    conn.commit()


def count(conn, table: str, column: str, email_id: str) -> int:
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (email_id,)
    ).fetchone()[0]


def test_only_the_named_emails_are_forgotten(conn):
    """The whole point: re-running the corpus must leave live-mailbox mail alone."""
    for email_id in ("corpus-1", "<live@example-pm.test>"):
        record_email(conn, email_id)
        ledger_row(conn, email_id)
        extracted_row(conn, email_id)
        accumulate(conn, email_id, "908491", "90052 : 1")

    state_db.forget_emails(conn, ["corpus-1"])

    assert not state_db.has_seen(conn, "corpus-1")
    assert count(conn, "email_log", "email_id", "corpus-1") == 0
    assert count(conn, "attachment_ledger", "email_id", "corpus-1") == 0
    assert count(conn, "extracted_records", "source_email_id", "corpus-1") == 0
    assert count(conn, "accumulation", "email_id", "corpus-1") == 0

    assert state_db.has_seen(conn, "<live@example-pm.test>")
    assert count(conn, "email_log", "email_id", "<live@example-pm.test>") == 1
    assert count(conn, "attachment_ledger", "email_id", "<live@example-pm.test>") == 1
    assert count(conn, "extracted_records", "source_email_id", "<live@example-pm.test>") == 1
    assert count(conn, "accumulation", "email_id", "<live@example-pm.test>") == 1


def test_released_events_is_cleared_so_the_delivery_can_fire_again(conn):
    """`released_events` has no email column, so it is the one table a naive scoped delete misses.
    Leave the row behind and the re-run looks like it worked — Stage 2 just logs "duplicate notice
    for an already-released delivery" and stages nothing."""
    record_email(conn, "corpus-1")
    accumulate(conn, "corpus-1", "908491", "90052 : 1")
    release(conn, "908491", "90052 : 1")

    state_db.forget_emails(conn, ["corpus-1"])

    assert conn.execute("SELECT COUNT(*) FROM released_events").fetchone()[0] == 0


def test_a_release_another_email_still_claims_is_left_alone(conn):
    """One delivery, several emails — forgetting one of them must not un-release the delivery the
    others still account for."""
    for email_id in ("corpus-1", "corpus-2"):
        record_email(conn, email_id)
        accumulate(conn, email_id, "908491", "90052 : 1")
    release(conn, "908491", "90052 : 1")

    state_db.forget_emails(conn, ["corpus-1"])

    assert conn.execute("SELECT COUNT(*) FROM released_events").fetchone()[0] == 1


def test_a_null_shipment_number_still_matches(conn):
    """Shipment number is nullable, and `= NULL` matches nothing in SQL — the delete has to use
    `IS`, or every PO-only delivery silently keeps its release row."""
    record_email(conn, "corpus-1")
    accumulate(conn, "corpus-1", "908491", None)
    release(conn, "908491", None)

    state_db.forget_emails(conn, ["corpus-1"])

    assert conn.execute("SELECT COUNT(*) FROM released_events").fetchone()[0] == 0


def test_it_reports_what_it_deleted(conn):
    record_email(conn, "corpus-1")
    ledger_row(conn, "corpus-1")

    deleted = state_db.forget_emails(conn, ["corpus-1"])

    assert deleted["email_log"] == 1
    assert deleted["attachment_ledger"] == 1
    assert deleted["seen_message_ids"] == 1
    assert deleted["extracted_records"] == 0


def test_a_human_verdict_goes_with_the_mail_it_was_about(conn):
    """The `_EMAIL_KEYED_TABLES` decision, asserted where it was made.

    A person flags a message "not a delivery" because a triage rule got it wrong. Fixing that rule
    and reprocessing is exactly the case where the flag must not survive: it would go on hiding a
    real delivery for ever, silently, after the thing that caused it had been fixed.
    """
    record_email(conn, "corpus-1")
    record_email(conn, "<live@example-pm.test>")
    for email_id in ("corpus-1", "<live@example-pm.test>"):
        mail_overrides.set_verdict(conn, email_id=email_id, verdict=mail_overrides.NOT_DELIVERY,
                                   decided_by="Reviewer", at="2026-08-05T00:00:00Z")

    deleted = state_db.forget_emails(conn, ["corpus-1"])

    assert deleted["mail_overrides"] == 1
    assert mail_overrides.get(conn, "corpus-1") is None
    assert mail_overrides.get(conn, "<live@example-pm.test>") is not None


def test_forgetting_nothing_is_a_no_op(conn):
    record_email(conn, "corpus-1")
    assert state_db.forget_emails(conn, []) == {}
    assert state_db.has_seen(conn, "corpus-1")


def test_a_repeated_id_is_only_counted_once(conn):
    record_email(conn, "corpus-1")
    deleted = state_db.forget_emails(conn, ["corpus-1", "corpus-1"])
    assert deleted["email_log"] == 1


# --- Reprocessing after a rule changes ----------------------------------------

def test_forgetting_mail_without_rewinding_the_watermark_would_hide_it():
    """The interaction `tools.reprocess_mail` exists to get right.

    `forget_emails` clears the seen-marker so a message can be read again — but the next poll now
    also sends `$filter receivedDateTime ge <watermark>`, and the watermark sits at the *newest*
    mail settled. Forget an old email without rewinding and the server never lists it again: it is
    forgotten, un-ingested, and invisible.
    """
    conn = state_db.get_connection(":memory:")
    state_db.set_ingest_state(conn, state_db.WATERMARK_KEY, "2026-08-12T10:00:00Z")
    state_db.mark_seen(conn, "msg-old", "2026-08-01T09:00:00Z")

    state_db.forget_emails(conn, ["msg-old"])
    assert state_db.has_seen(conn, "msg-old") is False
    assert state_db.get_watermark(conn) == "2026-08-12T10:00:00Z", "forget must not move it on its own"

    # What the tool then does, explicitly, with the date it read before deleting the row.
    state_db.set_ingest_state(conn, state_db.WATERMARK_KEY, "2026-08-01T09:00:00Z")
    assert state_db.get_watermark(conn) == "2026-08-01T09:00:00Z"


def test_the_watermark_survives_a_reopen():
    """It is per-store state, so it has to be in the file rather than in the process."""
    conn = state_db.get_connection(":memory:")
    state_db.advance_watermark(conn, "2026-08-12T10:00:00Z")
    assert state_db.get_watermark(conn) == "2026-08-12T10:00:00Z"


def test_advancing_with_nothing_is_a_no_op():
    conn = state_db.get_connection(":memory:")
    state_db.advance_watermark(conn, None)
    assert state_db.get_watermark(conn) is None
