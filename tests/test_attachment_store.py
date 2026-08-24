"""The attachment store, and the guarantee it exists to provide.

Before it, ingest kept no bytes at all: the ledger held a filename and a hash, the accumulation
payload's `content_bytes` was empty on every live row, and `mail_attachment` only held messages a
human had clicked. The single durable copy of every POD was Premier's Outlook mailbox.
"""

import hashlib

import pytest

from pipeline import attachment_ledger, attachment_store, state_db
from pipeline.models import Attachment, RawEmail

POD = b"%PDF-1.4 proof of delivery, signed U ALI"
NOW = "2026-08-12T12:00:00Z"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Redirect the store at a temp directory, so no test writes into state/attachments."""
    monkeypatch.setattr(attachment_store, "ROOT", tmp_path / "attachments")
    return tmp_path / "attachments"


def test_bytes_come_back_exactly(store):
    digest = attachment_store.put(POD)
    assert attachment_store.get(digest) == POD


def test_the_name_is_the_checksum(store):
    assert attachment_store.put(POD) == hashlib.sha256(POD).hexdigest()


def test_identical_content_under_two_names_is_stored_once(store):
    """Premier really does send byte-identical PODs under different filenames — the 5-Star thread
    carries two, 20,535 bytes each."""
    first = attachment_store.put(POD)
    second = attachment_store.put(POD)

    assert first == second
    assert len(list(store.rglob("*"))) == 1 + 1   # one shard directory, one file


def test_storing_twice_is_idempotent(store):
    attachment_store.put(POD)
    attachment_store.put(POD)
    assert attachment_store.get(hashlib.sha256(POD).hexdigest()) == POD


def test_empty_content_is_not_stored(store):
    assert attachment_store.put(b"") is None
    assert attachment_store.put(None) is None


def test_asking_for_something_never_stored_is_not_an_error(store):
    assert attachment_store.get("0" * 64) is None
    assert attachment_store.exists("0" * 64) is False
    assert attachment_store.path_for("") is None


def test_a_failed_write_does_not_take_the_run_with_it(store, monkeypatch):
    """The ledger row, the triage verdict and the records are all still correct without the blob,
    and `verify_attachments` reports the gap. Losing the email because the disk was full would be
    the worse trade."""
    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(attachment_store.os, "replace", explode)
    assert attachment_store.put(POD) is None


def test_no_half_written_blob_is_left_behind(store, monkeypatch):
    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(attachment_store.os, "replace", explode)
    attachment_store.put(POD)

    assert not list(store.rglob("*.part")), "a temp file survived a failed write"
    assert attachment_store.get(hashlib.sha256(POD).hexdigest()) is None


# --- Through the ledger -------------------------------------------------------

def _email(attachments):
    return RawEmail(email_id="msg-1", received_at=NOW, sender_address="a@b.com",
                    sender_domain="b.com", subject="Delivery", body_html=None,
                    body_text=None, attachments=attachments)


def test_the_ledger_records_where_the_bytes_went(store):
    conn = state_db.get_connection(":memory:")
    attachment_ledger.observe(conn, _email([
        Attachment(filename="POD.pdf", content_type="application/pdf", content_bytes=POD)]), None, NOW)

    row = conn.execute("SELECT blob_sha256, blob_stored_at FROM attachment_ledger").fetchone()
    assert row[0] == hashlib.sha256(POD).hexdigest()
    assert row[1] == NOW
    assert attachment_store.get(row[0]) == POD


def test_an_attachment_already_dropped_still_reaches_its_blob(store):
    """The connector clears a dropped attachment's bytes, having stored them first. The ledger has
    no bytes left to store, so it must recognise the content it already holds — otherwise thirty
    of Premier's forty-six rows would record no blob at all."""
    attachment_store.put(POD)                       # what the connector did before releasing
    dropped = Attachment(filename="image001.png", content_type="image/png", content_bytes=b"",
                         sha256=hashlib.sha256(POD).hexdigest(), size_bytes=len(POD),
                         drop_hint="decorative:known_hash")

    conn = state_db.get_connection(":memory:")
    attachment_ledger.observe(conn, _email([dropped]), None, NOW)

    stored = conn.execute("SELECT blob_sha256 FROM attachment_ledger").fetchone()[0]
    assert stored == hashlib.sha256(POD).hexdigest()


def test_a_decorative_drop_no_longer_destroys_the_evidence(store):
    """`mail_view` says in as many words that "a pasted photograph is often the proof of delivery
    itself", and thirty live ledger rows are `dropped_decorative`. A misclassification must cost a
    thumbnail, not the POD."""
    photo = b"\xff\xd8\xff" + b"pallet photograph" * 40
    attachment_store.put(photo)

    assert attachment_store.get(hashlib.sha256(photo).hexdigest()) == photo
