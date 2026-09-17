"""One place that answers "give me this attachment's bytes", for everything that needs them.

There were two answers before this, and they disagreed. `mail_view._with_content` — the
server-side preview path — read the click cache and then fell back to the content-addressed store.
`GET /ui/mail/attachment` — the route that serves every image, every PDF, every inline `cid:`
reference and every Download link — read the click cache **and stopped**, returning 404 whenever
that row held no bytes.

Measured against Premier's live store the day this was written: **18 of 35 cached attachments had
no `content`, so they 404'd — and all 18 were sitting on disk in `attachment_store`.** Half the
live attachments could not be opened or downloaded, and nothing had been lost. A spreadsheet
rendered while the image beside it reported itself missing, because the two went looking in
different places.

The resolution order below is the union of what both callers used to do, plus one addition:

  1. `mail_cache` — the click cache, when it actually holds content. Fastest, and it is the only
     source for a message recovered live that ingest never saw.
  2. the ledger's `blob_sha256` → `attachment_store`.
  3. the ledger's `sha256` → `attachment_store`. **This is the addition.** All 72 ledger rows in
     `sample_state.sqlite3` have `blob_sha256` NULL while their bytes are on disk under the plain
     content hash, so the previous `WHERE blob_sha256 IS NOT NULL` filter skipped every one of them.

A miss is still a miss. Decorative signature images and duplicates are dropped at ingest on
purpose and genuinely have no bytes; the caller's job is to say so rather than to invent them.
"""
import sqlite3
from dataclasses import dataclass
from typing import Optional

from pipeline import attachment_store, mail_cache


@dataclass(frozen=True)
class ResolvedAttachment:
    content: bytes
    filename: str
    content_type: str
    kind: str
    source: str
    """Which of the three places answered — `cache`, `blob_sha256` or `sha256`.

    Carried so a caller can say where the bytes came from, and so the tests can assert that the
    fallbacks are really being exercised rather than the cache quietly covering for them.
    """


def resolve(conn: sqlite3.Connection, email_id: str, ordinal: int,
            filename: str = "") -> Optional[ResolvedAttachment]:
    """This attachment's bytes with the metadata that describes them, or None if truly absent.

    `filename` is optional and only widens the ledger match: a container child's ordinal is its
    position inside its parent, so the filename is the only handle that identifies it.
    """
    cached = mail_cache.cached_attachment_bytes(conn, email_id, ordinal)
    if cached is not None and cached["content"]:
        return ResolvedAttachment(
            content=cached["content"],
            filename=cached["filename"] or filename or "attachment",
            content_type=cached["content_type"] or "",
            kind=cached["kind"] or "",
            source="cache",
        )

    # The cache's bytes, by hash rather than by blob. Before the ledger on purpose: a message
    # recovered live from Graph has a cache row and may have no ledger row at all, and the ledger
    # fallback below matches on *ordinal*, which drifts if a connector skipped a failed attachment.
    if cached is not None and cached["content_sha256"]:
        content = attachment_store.get(str(cached["content_sha256"]))
        if content:
            return ResolvedAttachment(
                content=content,
                filename=cached["filename"] or filename or "attachment",
                content_type=cached["content_type"] or "",
                kind=cached["kind"] or "",
                source="cache_store",
            )

    row = _ledger_row(conn, email_id, ordinal, filename or (cached["filename"] if cached else ""))
    if row is None:
        return None

    for column in ("blob_sha256", "sha256"):
        digest = row[column]
        if not digest:
            continue
        content = attachment_store.get(digest)
        if content:
            return ResolvedAttachment(
                content=content,
                filename=row["filename"] or filename or "attachment",
                content_type=(cached["content_type"] if cached else "")
                or row["declared_content_type"] or "",
                kind=row["sniffed_kind"] or (cached["kind"] if cached else "") or "",
                source=column,
            )
    return None


def _ledger_row(conn: sqlite3.Connection, email_id: str, ordinal: int,
                filename: str) -> Optional[sqlite3.Row]:
    """The ledger row for this attachment, preferring an ordinal match over a filename one.

    Both are needed. The ordinal is what the cache and the ledger agree on for a top-level
    attachment; the filename is the only thing that identifies a container child, whose ordinal
    counts within its parent rather than within the email.
    """
    prior = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT filename, sha256, blob_sha256, declared_content_type, sniffed_kind, "
            "       disposition, COALESCE(is_inline, 0) AS is_inline "
            "  FROM attachment_ledger "
            " WHERE email_id = ? AND (ordinal = ? OR (? <> '' AND filename = ?)) "
            " ORDER BY CASE WHEN ordinal = ? THEN 0 ELSE 1 END, depth, id LIMIT 1",
            (email_id, ordinal, filename or "", filename or "", ordinal),
        ).fetchone()
    finally:
        conn.row_factory = prior


def missing_reason(conn: sqlite3.Connection, email_id: str, ordinal: int,
                   filename: str = "") -> Optional[tuple]:
    """`(disposition, is_inline)` for an attachment that resolved to nothing, or None.

    For a caller deciding *how* to say there are no bytes. It never invents any, and it is a
    separate function from `resolve` on purpose: resolving is the thing the POD upload does, and
    nothing on that path should be able to reach a branch about presentation.

    Reuses `_ledger_row` rather than adding a fourth copy of the ordinal-or-filename rule --
    `routes._attachment_verdict` and `record_create` are already copies two and three.
    """
    row = _ledger_row(conn, email_id, ordinal, filename)
    if row is None:
        return None
    return (row["disposition"] or "", bool(row["is_inline"]))
