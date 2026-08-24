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
from pipeline import ingest_orchestrator, state_db

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
