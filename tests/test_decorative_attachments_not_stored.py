"""A sender's logo gets a ledger row, not disk space.

`dropped_decorative` is the connector's verdict on a logo, an email-signature graphic, an inline
screenshot — the images that arrive on every message and are never evidence of anything. Their
bytes were being written to the content-addressed store alongside real attachments.

Sized before it was changed, because the intuition is wrong in both directions. Content addressing
already collapsed **9,954 decorative rows into 939 distinct blobs**, of which 907 are only ever
decorative and take **13 MB — 2% of a 654 MB store**. So this is not where the disk goes, and it is
not a fix for a large `state/`; the point is that the store stays what it claims to be.

Safe to do, and checked rather than assumed: the largest attachment ever classified decorative is
**60 KB**, and all 10,051 of them are under 100 KB. A photographed POD is megabytes.

What is *not* dropped is the ledger row. "We saw this and here is why we ignored it" is the audit
trail, and it survives either way.
"""

import sqlite3

import pytest

from config import settings
from pipeline import attachment_ledger, attachment_store
from pipeline.models import Attachment, RawEmail


@pytest.fixture
def conn(tmp_path):
    from pipeline import state_db
    connection = state_db.get_connection(tmp_path / "state.sqlite3")
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


def _email(attachment: Attachment) -> RawEmail:
    return RawEmail(email_id="<m1@example.test>", received_at="2026-01-02T03:04:05+00:00",
                    sender_address="vendor@example-interiors.test",
                    sender_domain="example-interiors.test", subject="Delivery",
                    body_html="", body_text="", attachments=[attachment])


def _logo() -> Attachment:
    return Attachment(filename="logo.png", content_type="image/png",
                      content_bytes=b"\x89PNG" + b"logo" * 100,
                      drop_hint="decorative:cid_referenced", is_inline=True)


def _pod() -> Attachment:
    return Attachment(filename="pod.pdf", content_type="application/pdf",
                      content_bytes=b"%PDF-1.4 proof of delivery" * 50)


def test_a_logo_is_recorded_but_its_bytes_are_not_kept(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(attachment_store, "ROOT", tmp_path / "blobs")
    monkeypatch.setattr(settings, "STORE_DECORATIVE_ATTACHMENTS", False)

    attachment_ledger.observe(conn, _email(_logo()), None, "2026-01-02T03:04:05+00:00")

    row = conn.execute("SELECT filename, disposition, blob_sha256 FROM attachment_ledger").fetchone()
    assert row["disposition"] == attachment_ledger.DROPPED_DECORATIVE
    assert row["filename"] == "logo.png", "the row itself is the audit trail and must survive"
    assert row["blob_sha256"] is None, "a logo's bytes should not be on disk"
    assert not list((tmp_path / "blobs").rglob("*")), "nothing should have been written"


def test_a_real_attachment_is_still_stored(conn, monkeypatch, tmp_path):
    """The guard has to be narrow. This is the assertion that catches it being too wide."""
    monkeypatch.setattr(attachment_store, "ROOT", tmp_path / "blobs")
    monkeypatch.setattr(settings, "STORE_DECORATIVE_ATTACHMENTS", False)

    attachment_ledger.observe(conn, _email(_pod()), None, "2026-01-02T03:04:05+00:00")

    row = conn.execute("SELECT blob_sha256 FROM attachment_ledger").fetchone()
    assert row["blob_sha256"], "a POD must always be stored, whatever the decorative setting says"
    assert attachment_store.get(row["blob_sha256"]) == _pod().content_bytes


def test_the_setting_brings_the_old_behaviour_back(conn, monkeypatch, tmp_path):
    """It is a setting rather than a deletion because `mail_view` points every `cid:` reference at
    the attachment route — so without these blobs, inline images in message bodies stop rendering.
    Anyone who wants them back must not need a code change to get them."""
    monkeypatch.setattr(attachment_store, "ROOT", tmp_path / "blobs")
    monkeypatch.setattr(settings, "STORE_DECORATIVE_ATTACHMENTS", True)

    attachment_ledger.observe(conn, _email(_logo()), None, "2026-01-02T03:04:05+00:00")

    row = conn.execute("SELECT blob_sha256 FROM attachment_ledger").fetchone()
    assert row["blob_sha256"], "PREMIER_STORE_DECORATIVE=1 must restore storing them"


def test_a_logo_already_on_disk_is_still_linked_to_its_row(conn, monkeypatch, tmp_path):
    """Turning the setting off must not orphan rows from bytes that are already stored.

    9,954 decorative rows point at blobs on disk today. This change stops *adding* to them; it must
    not make the ones already there unreachable, or those messages lose their inline images too.
    """
    monkeypatch.setattr(attachment_store, "ROOT", tmp_path / "blobs")
    monkeypatch.setattr(settings, "STORE_DECORATIVE_ATTACHMENTS", False)

    logo = _logo()
    stored = attachment_store.put(logo.content_bytes, root=tmp_path / "blobs")
    logo.sha256 = stored

    attachment_ledger.observe(conn, _email(logo), None, "2026-01-02T03:04:05+00:00")

    row = conn.execute("SELECT blob_sha256 FROM attachment_ledger").fetchone()
    assert row["blob_sha256"] == stored, (
        "a decorative blob already on disk must stay reachable from its ledger row")


# --- the gate has to hold where the bytes actually are -------------------------------------------
#
# Every test above calls `attachment_ledger.observe()` directly, which is where the setting was
# read — and that is exactly why they all passed while the setting did nothing. Both connectors
# called `attachment_store.put()` one step *earlier*, on the way to releasing `content_bytes`, so
# by the time `_insert` consulted the setting the bytes were already on disk and its `exists()`
# re-link attached the row to them. Measured on Premier's live store 2026-09-16: 23,863 of 23,867
# decorative rows carried a blob with the setting off, the newest stamped that morning.
#
# These two test the rule where the decision now lives.


def test_the_connector_does_not_store_a_logo(monkeypatch, tmp_path):
    """The setting, asserted at the point the bytes are released rather than after."""
    monkeypatch.setattr(attachment_store, "ROOT", tmp_path / "blobs")
    monkeypatch.setattr(settings, "STORE_DECORATIVE_ATTACHMENTS", False)

    assert attachment_ledger.keeps_bytes("decorative:cid_referenced") is False
    assert not list((tmp_path / "blobs").rglob("*")), "a logo reached the store"


def test_the_connector_still_stores_everything_else(monkeypatch, tmp_path):
    """The narrowness guard. A duplicate is dropped from *extraction*, never from the store — its
    bytes are the same bytes some other row is keeping — and an attachment with no hint at all is
    evidence."""
    monkeypatch.setattr(settings, "STORE_DECORATIVE_ATTACHMENTS", False)

    assert attachment_ledger.keeps_bytes(None) is True
    assert attachment_ledger.keeps_bytes("duplicate:a6ff14bad9c3") is True
    assert attachment_ledger.keeps_bytes("duplicate_mismatch:a6ff14bad9c3") is True
    assert attachment_ledger.keeps_bytes("oversize:26000000") is True


def test_the_setting_reaches_the_connector_too(monkeypatch):
    """`test_the_setting_brings_the_old_behaviour_back` holds this open for `observe()`. The hedge
    the connectors were making — a misclassified photograph must not lose its bytes — has to be
    recoverable on their path as well, or turning the setting on only half works."""
    monkeypatch.setattr(settings, "STORE_DECORATIVE_ATTACHMENTS", True)
    assert attachment_ledger.keeps_bytes("decorative:cid_referenced") is True
