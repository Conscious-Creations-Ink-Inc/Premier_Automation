"""Cached copies of recovered messages, so one is fetched from its source once rather than per click.

Lifted out of `console/store.py` when the mail viewer became shared: two interfaces now open
messages, and a cache belonging to one of them would leave the other rescanning the `.msg` folder on
every click, or reaching across into a database it has no business reading.

The tables live in the pipeline store (`pipeline/state_db.py`), which puts each cached message in the
same file as the mail it was recovered from. Everywhere else in this system sample and live mail are
kept apart by file rather than by a column, and a single shared cache would be the one place that
stopped being true.

`content` no longer holds a second copy of bytes the content-addressed store already has. A row
records `content_sha256` and leaves `content` NULL whenever `attachment_store` is confirmed to hold
that digest — the same trade `stage2_accumulate._embedded_bytes` makes, under the same rule: **a
byte is only dropped once its replacement is confirmed present.**

It only ever *points at* the store; it never puts anything there. Ingest decides what is worth
keeping, and since `attachment_ledger.keeps_bytes` started declining signature logos, a cache that
stored what it was handed would put every logo back the first time somebody opened the message.
Anything the store does not already hold keeps its inline copy, which is also what makes a message
recovered live from Graph — one ingest never saw — safe to cache.
"""

import hashlib
import sqlite3
from typing import List, Optional

from pipeline import attachment_store


def get_cached_mail(conn: sqlite3.Connection, email_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM mail_body WHERE email_id = ?", (email_id,)).fetchone()


def cached_attachments(conn: sqlite3.Connection, email_id: str) -> List[sqlite3.Row]:
    """Metadata only — deliberately not the `content` blob.

    Images and PDFs stream from the attachment route one at a time; selecting every blob here would
    load an entire message's attachments into memory just to render a list of filenames.
    """
    return conn.execute(
        "SELECT ordinal, filename, content_type, kind, size_bytes, content_id, is_inline "
        "FROM mail_attachment WHERE email_id = ? ORDER BY ordinal",
        (email_id,),
    ).fetchall()


def cached_attachment_bytes(conn: sqlite3.Connection, email_id: str, ordinal: int):
    return conn.execute(
        "SELECT filename, content_type, kind, content, "
        "       COALESCE(content_sha256, '') AS content_sha256 "
        "  FROM mail_attachment WHERE email_id = ? AND ordinal = ?",
        (email_id, ordinal),
    ).fetchone()


def cache_mail(conn: sqlite3.Connection, *, email_id: str, subject: str, sender: str,
               received_at: str, body_html: Optional[str], body_text: Optional[str],
               source: str, attachments: list, cached_at: str) -> None:
    """`attachments` is a list of dicts carrying the `mail_attachment` columns.

    Attachments are deleted and reinserted rather than merged: a message re-recovered from a
    different source can legitimately have a different set, and a merge would leave rows from the
    previous read behind under ordinals that no longer mean the same thing.
    """
    conn.execute(
        "INSERT INTO mail_body (email_id, subject, sender, received_at, body_html, body_text, "
        "source, cached_at) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(email_id) DO UPDATE SET subject=excluded.subject, sender=excluded.sender, "
        "received_at=excluded.received_at, body_html=excluded.body_html, "
        "body_text=excluded.body_text, source=excluded.source, cached_at=excluded.cached_at",
        (email_id, subject, sender, received_at, body_html, body_text, source, cached_at),
    )
    conn.execute("DELETE FROM mail_attachment WHERE email_id = ?", (email_id,))
    conn.executemany(
        "INSERT INTO mail_attachment (email_id, ordinal, filename, content_type, kind, "
        "size_bytes, content_id, is_inline, content, content_sha256) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (email_id, a["ordinal"], a["filename"], a["content_type"], a["kind"],
             a["size_bytes"], a.get("content_id"), 1 if a.get("is_inline") else 0,
             *_bytes_or_pointer(a.get("content")))
            for a in attachments
        ],
    )
    conn.commit()


def _bytes_or_pointer(blob) -> tuple:
    """`(content, content_sha256)` — the bytes, or a pointer to where they already are.

    Asks the store whether it holds these bytes; never hands it any. If it does, the row keeps the
    digest and drops its copy. If it does not — a decorative image ingest declined, an attachment
    from a message recovered live that ingest never saw — the row keeps the bytes, because a byte
    is only dropped once its replacement is confirmed present.

    The digest is recorded either way. It is what lets a later sweep drop the blob safely, and what
    tells `tools/reclaim_decorative_blobs.py` that this row is relying on a file it must not
    delete.
    """
    if not blob:
        return (blob, None)
    digest = hashlib.sha256(blob).hexdigest()
    if attachment_store.exists(digest):
        return (None, digest)
    return (blob, digest)
