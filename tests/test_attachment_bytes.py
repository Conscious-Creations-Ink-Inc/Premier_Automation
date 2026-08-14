"""An attachment whose bytes exist must never report itself missing.

There were two answers to "give me this attachment's bytes" before `attachment_bytes`, and they
disagreed. The server-side preview path read the click cache and then fell back to the
content-addressed store; the HTTP route that serves every image, every PDF, every inline `cid:`
reference and every Download link read the click cache and stopped.

Measured against Premier's live store the day this was written: **18 of 35 cached attachments had
no `content` and so 404'd — and all 18 were on disk in `attachment_store` the whole time.** Half
the live attachments could not be opened or downloaded, and nothing had been lost. The symptom was
a spreadsheet rendering happily beside an image that reported itself gone.

These tests pin each rung of the ladder separately, because a cache hit will otherwise quietly
cover for a broken fallback — which is exactly how the fault went unnoticed.
"""
import sqlite3

import pytest

from pipeline import attachment_bytes, attachment_store, mail_cache, state_db

NOW = "2026-08-13T00:00:00Z"
CONTENT = b"%PDF-1.4 signed by Miguel C."
EMAIL = "<pod@premier>"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(attachment_store, "ROOT", tmp_path / "attachments")
    connection = state_db.get_connection(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def add_cache_row(conn, *, content):
    """A `mail_attachment` row, with or without bytes. `cache_mail` is the real writer, but it
    always stores content — the row this fixture needs is the one that does not."""
    conn.execute(
        "INSERT INTO mail_attachment (email_id, ordinal, filename, content_type, kind, "
        "size_bytes, content) VALUES (?, 0, 'pod.pdf', 'application/pdf', 'pdf', ?, ?)",
        (EMAIL, len(CONTENT), content),
    )
    conn.commit()


def add_ledger_row(conn, *, sha256="", blob_sha256=None, filename="pod.pdf", ordinal=0):
    conn.execute(
        "INSERT INTO attachment_ledger (email_id, depth, ordinal, container_path, filename, "
        "declared_content_type, sniffed_kind, sha256, blob_sha256, size_bytes, disposition, "
        "first_seen_at, last_updated_at) "
        "VALUES (?, 0, ?, ?, ?, 'application/pdf', 'pdf', ?, ?, ?, 'extracted', ?, ?)",
        (EMAIL, ordinal, filename, filename, sha256, blob_sha256, len(CONTENT), NOW, NOW),
    )
    conn.commit()


# --- The ladder ---------------------------------------------------------------

def test_the_cache_answers_when_it_holds_bytes(conn):
    add_cache_row(conn, content=CONTENT)
    found = attachment_bytes.resolve(conn, EMAIL, 0)
    assert found.content == CONTENT
    assert found.source == "cache"


def test_the_store_answers_when_the_cache_row_has_no_bytes(conn):
    """The 18. A cached row with `content IS NULL` used to be indistinguishable from an attachment
    that was never retained."""
    digest = attachment_store.put(CONTENT)
    add_cache_row(conn, content=None)
    add_ledger_row(conn, sha256=digest, blob_sha256=digest)

    found = attachment_bytes.resolve(conn, EMAIL, 0)
    assert found is not None, "an attachment on disk reported itself missing"
    assert found.content == CONTENT
    assert found.source == "blob_sha256"


def test_the_store_answers_when_there_is_no_cache_row_at_all(conn):
    """Ingest stores blobs for every message; the click cache only holds ones somebody has opened.
    For most mail the cache is empty and the store is the only source."""
    digest = attachment_store.put(CONTENT)
    add_ledger_row(conn, sha256=digest, blob_sha256=digest)

    found = attachment_bytes.resolve(conn, EMAIL, 0)
    assert found is not None and found.content == CONTENT


def test_a_plain_sha256_is_enough_when_blob_sha256_is_null(conn):
    """Every one of the 72 ledger rows in `sample_state.sqlite3` has `blob_sha256` NULL while its
    bytes sit on disk under the plain content hash. The old lookup filtered those rows out with
    `WHERE blob_sha256 IS NOT NULL`, so it found none of them."""
    digest = attachment_store.put(CONTENT)
    add_ledger_row(conn, sha256=digest, blob_sha256=None)

    found = attachment_bytes.resolve(conn, EMAIL, 0)
    assert found is not None, "a null blob_sha256 hid bytes that were on disk"
    assert found.source == "sha256"


def test_a_genuinely_dropped_attachment_still_resolves_to_nothing(conn):
    """Decorative logos and duplicates are dropped on purpose and have no bytes anywhere. The
    honest answer is still None — the fix was to stop guessing, not to start inventing."""
    add_cache_row(conn, content=None)
    add_ledger_row(conn, sha256="", blob_sha256=None)
    assert attachment_bytes.resolve(conn, EMAIL, 0) is None


def test_a_hash_recorded_but_never_written_resolves_to_nothing(conn):
    """`attachment_store.put` swallows a write failure so the run survives, which leaves a ledger
    row naming a blob that is not there."""
    add_ledger_row(conn, sha256="0" * 64, blob_sha256="0" * 64)
    assert attachment_bytes.resolve(conn, EMAIL, 0) is None


def test_nothing_at_all_resolves_to_nothing(conn):
    assert attachment_bytes.resolve(conn, "<unknown@x>", 0) is None


# --- Matching -----------------------------------------------------------------

def test_a_container_child_is_found_by_filename(conn):
    """A child's ordinal counts within its parent, not within the email, so the filename is the
    only handle that identifies it."""
    digest = attachment_store.put(CONTENT)
    add_ledger_row(conn, sha256=digest, blob_sha256=digest, filename="inner.pdf", ordinal=7)

    found = attachment_bytes.resolve(conn, EMAIL, 0, "inner.pdf")
    assert found is not None and found.content == CONTENT


def test_an_ordinal_match_wins_over_a_filename_match(conn):
    """Two attachments can share a name — the same POD forwarded twice. The ordinal is the
    stronger handle and must be preferred."""
    wanted, other = b"the right one", b"the wrong one"
    add_ledger_row(conn, sha256=attachment_store.put(other), filename="pod.pdf", ordinal=3)
    add_ledger_row(conn, sha256=attachment_store.put(wanted), filename="pod.pdf", ordinal=0)

    found = attachment_bytes.resolve(conn, EMAIL, 0, "pod.pdf")
    assert found.content == wanted


def test_an_empty_filename_does_not_match_every_row(conn):
    """`filename = ''` must not become a wildcard — the SQL guards it with `? <> ''`."""
    digest = attachment_store.put(CONTENT)
    add_ledger_row(conn, sha256=digest, blob_sha256=digest, filename="", ordinal=5)
    assert attachment_bytes.resolve(conn, EMAIL, 0, "") is None


def test_metadata_comes_back_with_the_bytes(conn):
    """The route decides inline-vs-download from this, so it has to describe the bytes actually
    being served rather than whatever the cache last recorded."""
    digest = attachment_store.put(CONTENT)
    add_ledger_row(conn, sha256=digest, blob_sha256=digest)
    found = attachment_bytes.resolve(conn, EMAIL, 0)
    assert found.filename == "pod.pdf"
    assert found.content_type == "application/pdf"
    assert found.kind == "pdf"


def test_the_row_factory_is_left_as_it_was_found(conn):
    """The ledger lookup needs `sqlite3.Row` and the callers share the connection."""
    conn.row_factory = None
    attachment_bytes.resolve(conn, EMAIL, 0)
    assert conn.row_factory is None
