"""The dedupe layers, exercised through a real ingest run rather than in isolation.

`test_dedupe.py` holds the hashes. This holds the thing that actually costs Premier money: the same
notification arriving twice under two message ids, accumulating twice, and becoming two receipts.
That is the failure that ended their previous attempt at this, so it is worth an integration test
and not only a unit one.
"""

import json
import shutil

import pytest

from connectors.mailbox import LocalFolderMailbox
from pipeline import dedupe, ingest_orchestrator, state_db

SAMPLE = "warehouse_inbound_239336.json"


@pytest.fixture
def mailbox_dir(tmp_path):
    """One real sample email, copied out so a second copy can be added to it."""
    root = tmp_path / "emails"
    root.mkdir()
    shutil.copy(_sample_dir() / SAMPLE, root / SAMPLE)
    return root


def _sample_dir():
    from pathlib import Path
    return Path(__file__).resolve().parent.parent / "sample_data" / "emails"


def resend(mailbox_dir, new_id: str):
    """The same notification again, under a different message id.

    This is what a vendor re-send looks like, and what a second expeditor forwarding the same mail
    to the receiving inbox looks like. Every byte of the content is identical; only the envelope
    identity differs — which is precisely why `seen_message_ids` cannot catch it.

    Read from the pristine sample rather than from `mailbox_dir`: `LocalFolderMailbox.mark_processed`
    moves a file into a subfolder once it has been read, so the original is no longer where it was
    put after the first run.
    """
    payload = json.loads((_sample_dir() / SAMPLE).read_text(encoding="utf-8"))
    payload["email_id"] = new_id
    (mailbox_dir / f"{new_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def run(conn, mailbox_dir):
    return ingest_orchestrator.process_new_mail(LocalFolderMailbox(mailbox_dir), conn=conn)


def counts(conn):
    return (conn.execute("SELECT COUNT(*) FROM extracted_records").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM email_log").fetchone()[0])


def test_a_resend_under_a_new_id_does_not_stage_a_second_time(tmp_path, mailbox_dir):
    """The one that matters. `seen_message_ids` keys on `internetMessageId`, which a re-send does
    not share, so without a content fingerprint this notification accumulates twice and every line
    on it is staged twice."""
    conn = state_db.get_connection(tmp_path / "one.sqlite3")
    run(conn, mailbox_dir)
    records_after_one, _ = counts(conn)
    assert records_after_one > 0, "the fixture must actually stage something"

    resend(mailbox_dir, "resent-copy")
    run(conn, mailbox_dir)

    records_after_two, emails = counts(conn)
    assert records_after_two == records_after_one, "the re-send staged records a second time"
    assert emails == 2, "both messages must still be recorded — nothing is deleted"


def test_the_second_copy_says_what_it_duplicates(tmp_path, mailbox_dir):
    """Linked, not dropped. A suppression nobody can inspect is a suppression nobody can trust, and
    the failure it would hide is a real delivery that silently never arrived."""
    conn = state_db.get_connection(tmp_path / "two.sqlite3")
    run(conn, mailbox_dir)
    resend(mailbox_dir, "resent-copy")
    run(conn, mailbox_dir)

    import sqlite3
    conn.row_factory = sqlite3.Row
    copy = conn.execute("SELECT * FROM email_log WHERE email_id = 'resent-copy'").fetchone()

    assert copy is not None
    assert copy["duplicate_of"] not in (None, "")
    assert copy["fingerprint"]
    assert "not counted twice" in (copy["reason"] or "")


def test_the_first_message_is_not_marked_as_a_duplicate_of_anything(tmp_path, mailbox_dir):
    conn = state_db.get_connection(tmp_path / "three.sqlite3")
    run(conn, mailbox_dir)
    resend(mailbox_dir, "resent-copy")
    run(conn, mailbox_dir)

    original = conn.execute(
        "SELECT duplicate_of FROM email_log WHERE email_id != 'resent-copy'").fetchone()[0]
    assert original in (None, "")


def test_every_message_carries_a_fingerprint_whether_or_not_it_duplicates(tmp_path, mailbox_dir):
    """So "which messages share this fingerprint" is answerable from the table rather than by
    recomputing over a whole mailbox."""
    conn = state_db.get_connection(tmp_path / "four.sqlite3")
    run(conn, mailbox_dir)

    fingerprints = [row[0] for row in conn.execute("SELECT fingerprint FROM email_log")]
    assert fingerprints and all(fingerprints)


def test_records_carry_the_delivery_key_they_were_deduped_on(tmp_path, mailbox_dir):
    """Written at stage time, so a record a person made and a record extraction staged are
    comparable. Two different notions of "the same delivery" would agree on nothing."""
    conn = state_db.get_connection(tmp_path / "five.sqlite3")
    run(conn, mailbox_dir)

    keys = [row[0] for row in conn.execute("SELECT delivery_key FROM extracted_records")]
    assert keys and all(keys)


def test_a_message_a_person_finished_stops_extraction_staging_more(tmp_path, mailbox_dir):
    """Otherwise the Records page shows two rows for one delivery: the one somebody entered, and
    the one the next run extracted from the same mail."""
    conn = state_db.get_connection(tmp_path / "six.sqlite3")
    run(conn, mailbox_dir)
    staged_first = counts(conn)[0]

    # A person records this delivery by hand, and the mail is re-presented under a new id — a
    # forward, say — so triage and accumulation run again on the same content.
    conn.execute("UPDATE email_log SET handled_manually = 1")
    conn.execute("DELETE FROM extracted_records")
    conn.commit()

    resend(mailbox_dir, "forwarded-again")
    run(conn, mailbox_dir)

    assert counts(conn)[0] == 0, "extraction staged from a message a person had finished"
    assert staged_first > 0


def _shared_record(ledger_id: int = 4242):
    """One extracted line, stating no delivery date of its own.

    `source_ledger_id` is set because that is what makes `_are_siblings_of_one_grid` treat a group
    as rows of one document rather than as competing claims — the path that lets the same instance
    through `reconcile_cross_source_duplicates` twice.
    """
    from pipeline.models import ExtractedRecord
    return ExtractedRecord(
        source_email_id="msg-shared", po_number="212696", shipment_number=None,
        spec_code="STE-405-LT", parent_spec_code="STE-405-LT", sub_spec_suffix=None,
        item_description="Table Lamp at End Table", vendor_name=None, carrier_name=None,
        tracking_number=None, quantity_received=20.0, unit_of_measure="EA",
        pod_stated_date=None, email_date="", delivery_location=None, comments=None,
        extraction_source="docx", extraction_confidence=0.9, raw_snippet="20.00 | EA | Table Lamp",
        source_ledger_id=ledger_id)


def test_the_stored_delivery_key_describes_the_record_it_is_on(tmp_path):
    """The invariant the duplicate bug broke, on the one record shape that broke it.

    `delivery_key` used to be minted before `_fallback_delivery_date` filled in a missing
    `pod_stated_date` — a field the key hashes — so the row was written carrying a key that
    described a version of itself that no longer existed. Nothing could match it again, which is
    how 160 duplicate records reached the live store across 43 purchase orders.

    Staged singly, so this fails on the stale key itself rather than on the duplicate it goes on
    to cause. A stored key that does not reproduce from its own row is the defect.
    """
    import sqlite3
    conn = state_db.get_connection(tmp_path / "invariant.sqlite3")
    conn.execute(
        "INSERT INTO email_log (email_id, category, folder, processed_at, email_date) "
        "VALUES ('msg-shared', 'surface', 'Inbox', '2026-09-05T00:00:00', '2026-09-05T09:00:00Z')")
    conn.commit()

    staged = ingest_orchestrator.stage_records(
        conn, [_shared_record()], po_number="212696", delivery_ref="message:msg-shared",
        delivery_rung="message", now="2026-09-05T00:00:00")
    assert len(staged) == 1

    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM extracted_records").fetchone()
    assert row["pod_source"] == "email_received_date", "the fallback has to have fired"
    assert dedupe.key_for_row(row) == row["delivery_key"], (
        "delivery_key does not reproduce from its own row — it was minted before "
        "pod_stated_date was filled in")


def test_every_staged_record_carries_a_key_that_reproduces(tmp_path, mailbox_dir):
    """The same invariant swept across a real ingest run, so a future source that mutates a
    record late is caught here rather than in Premier's duplicate count."""
    import sqlite3
    conn = state_db.get_connection(tmp_path / "sweep.sqlite3")
    run(conn, mailbox_dir)

    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM extracted_records").fetchall()
    assert rows, "the fixture must actually stage something"

    stale = [(r["id"], r["pod_source"]) for r in rows
             if dedupe.key_for_row(r) != r["delivery_key"]]
    assert not stale, f"delivery_key does not reproduce from its own row: {stale}"


def test_one_record_instance_staged_twice_becomes_one_row(tmp_path):
    """The exact shape that produced the duplicates.

    `evidence_cache.records_for` is keyed on the email, not the attachment, so every attachment
    source on one message handed back the *same* `ExtractedRecord` instances — and this loop saw
    the same object twice. That alone is harmless; it only staged a second row because the first
    pass mutated the object's `pod_stated_date` after minting its key, so the second pass hashed
    different content and the guard missed it.

    The email_log row is what makes the fallback fire. Without a date to find there is no
    mutation, the two passes agree, and this would pass even against the bug.
    """
    conn = state_db.get_connection(tmp_path / "shared.sqlite3")
    conn.execute(
        "INSERT INTO email_log (email_id, category, folder, processed_at, email_date) "
        "VALUES ('msg-shared', 'surface', 'Inbox', '2026-09-05T00:00:00', '2026-09-05T09:00:00Z')")
    conn.commit()

    record = _shared_record()
    assert record.pod_stated_date is None, "the fallback has to have something to do"

    staged = ingest_orchestrator.stage_records(
        conn, [record, record], po_number="212696", delivery_ref="message:msg-shared",
        delivery_rung="message", now="2026-09-05T00:00:00")

    assert len(staged) == 1, "the same record instance was staged twice"
    assert conn.execute("SELECT COUNT(*) FROM extracted_records").fetchone()[0] == 1
    # And the row that did land carries the fallback date, not a blank one.
    row = conn.execute(
        "SELECT pod_stated_date, pod_source FROM extracted_records").fetchone()
    assert row[0] == "2026-09-05"
    assert row[1] == "email_received_date"
