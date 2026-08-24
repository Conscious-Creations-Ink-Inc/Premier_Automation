"""Cached copies of recovered messages, so one is fetched from its source once rather than per click.

Lifted out of `console/store.py` when the mail viewer became shared: two interfaces now open
messages, and a cache belonging to one of them would leave the other rescanning the `.msg` folder on
every click, or reaching across into a database it has no business reading.

The tables live in the pipeline store (`pipeline/state_db.py`), which puts each cached message in the
same file as the mail it was recovered from. Everywhere else in this system sample and live mail are
kept apart by file rather than by a column, and a single shared cache would be the one place that
stopped being true.

**Nothing here prunes.** `content` holds full attachment bytes, so the cache grows with every message
opened and never shrinks. That is tolerable for a corpus of fourteen and is not tolerable against a
live mailbox indefinitely — it is the same unbounded-retention problem already logged against
`accumulation.payload_json`, and it wants the same answer.
"""

import sqlite3
from typing import List, Optional


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
        "SELECT filename, content_type, kind, content FROM mail_attachment "
        "WHERE email_id = ? AND ordinal = ?",
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
        "size_bytes, content_id, is_inline, content) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (email_id, a["ordinal"], a["filename"], a["content_type"], a["kind"],
             a["size_bytes"], a.get("content_id"), 1 if a.get("is_inline") else 0, a.get("content"))
            for a in attachments
        ],
    )
    conn.commit()
