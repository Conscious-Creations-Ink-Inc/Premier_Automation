"""What is sitting in the mailbox, as distinct from what has been read out of it.

`email_log` answers "what did the orchestrator decide about this email". This answers the cheaper
and much more urgent question "has anything arrived", and it is the whole of the real-time path:
a metadata-only Graph listing writes here in a few hundred milliseconds, the page's version token
changes, and the ten-second poller already in `api/ui/html.py` shows it. The expensive work —
attachments, sniffing, OCR, extraction — happens later on its own schedule and sets `enriched_at`.

Nothing here is a verdict. There is deliberately no category, no rule and no reason column: a row
in this table means "this message exists", and inventing a triage answer for mail nobody has read
is exactly the confusion the two-table split exists to prevent.

Keyed on `internetMessageId`, like every other identity in this system — Graph's own `id` changes
the moment a message is moved. See `connectors/mailbox.py`.
"""
import sqlite3
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

_COLUMNS = ("email_id", "received_at", "sender", "subject", "has_attachments",
            "first_seen_at", "enriched_at")


@dataclass(frozen=True)
class Arrival:
    email_id: str
    received_at: str
    sender: str
    subject: str
    has_attachments: bool
    first_seen_at: str
    enriched_at: Optional[str]

    @property
    def is_enriched(self) -> bool:
        return self.enriched_at is not None


def record(conn: sqlite3.Connection, arrivals: Iterable[Arrival], *, now: str) -> int:
    """Upsert a batch and return how many were genuinely new.

    One transaction for the whole batch: this runs every fifteen seconds against a database a
    forty-second ingest pass may be writing at the same time, and a commit per row would multiply
    the chances of colliding with it for no benefit.

    `first_seen_at` is preserved on conflict — it is when *we* first knew, which is the number that
    makes "how far behind is the automation" answerable, and re-stamping it on every poll would
    reset that to now for ever. `enriched_at` is likewise never cleared here; only the pipeline
    sets it, and only `forget` unsets it.
    """
    rows = [
        (a.email_id, a.received_at or "", a.sender or "", a.subject or "",
         1 if a.has_attachments else 0, a.first_seen_at or now)
        for a in arrivals if a.email_id
    ]
    if not rows:
        return 0
    known = {r[0] for r in conn.execute(
        "SELECT email_id FROM mail_arrivals WHERE email_id IN "
        f"({', '.join('?' * len(rows))})", [r[0] for r in rows]
    )}
    conn.executemany(
        """
        INSERT INTO mail_arrivals
              (email_id, received_at, sender, subject, has_attachments, first_seen_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(email_id) DO UPDATE SET
              received_at     = excluded.received_at,
              sender          = excluded.sender,
              subject         = excluded.subject,
              has_attachments = excluded.has_attachments
        """,
        rows,
    )
    conn.commit()
    return len([r for r in rows if r[0] not in known])


def mark_enriched(conn: sqlite3.Connection, email_id: str, when: str) -> None:
    """Stamp when we noticed the pipeline had read this message. Idempotent, never creates a row.

    An audit stamp, not the authority — see `_PENDING_WHERE`. Whether a message has been read is
    decided by whether `email_log` holds a verdict for it, so this being missing (mail processed
    before the table existed) or stale can never put a processed message back on screen as unread.

    Not creating a row matters: mail read from a source that is not the live Inbox has no arrival,
    and inventing one would claim the watch saw a message it never did.
    """
    conn.execute(
        "UPDATE mail_arrivals SET enriched_at = ? WHERE email_id = ? AND enriched_at IS NULL",
        (when, email_id),
    )
    conn.commit()


# "Has the pipeline read this?" is answered by asking `email_log`, not by trusting `enriched_at`.
#
# The stamp alone was wrong the first time this table was ever filled: the very first poll listed
# 22 messages the pipeline had already processed days earlier, and every one of them rendered
# "not read yet" — because `enriched_at` is only ever set by a *future* run, and those runs had
# already happened. A one-off backfill would have fixed that morning and nothing else; the same
# drift returns whenever a store is copied, an arrival is re-listed after a forget, or the watch
# is enabled on a mailbox with history.
#
# The join cannot drift, because it asks the table that actually holds the verdict. `enriched_at`
# is kept as the audit stamp of *when* we noticed — `email_log.processed_at` answers when it was
# processed, which is a different moment — but it is never the authority on whether.
_PENDING_WHERE = """
      FROM mail_arrivals a
 LEFT JOIN email_log e ON e.email_id = a.email_id
     WHERE e.email_id IS NULL
"""


def pending(conn: sqlite3.Connection) -> List[Arrival]:
    """In the mailbox, with no verdict recorded against it — newest first.

    This is the number the Mail page shows as "not processed yet", and it replaces a ~3.2s Graph
    round trip that used to run inside the page render.
    """
    return _query(conn, f"SELECT a.* {_PENDING_WHERE} ORDER BY a.received_at DESC")


def pending_count(conn: sqlite3.Connection) -> int:
    return conn.execute(f"SELECT COUNT(*) {_PENDING_WHERE}").fetchone()[0]


def version_signature(conn: sqlite3.Connection) -> str:
    """`count:newest-first-seen:pending` — what `/ui/version` folds in so the browser notices.

    All three parts earn their place. The count moves when mail arrives; `first_seen_at` moves when
    an arrival is *re*-seen, which the count alone would miss; and the pending count moves when the
    pipeline records a verdict, which is the second update a row makes and the one that clears its
    "not read yet" badge.
    """
    total, newest = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(first_seen_at), '') FROM mail_arrivals").fetchone()
    return f"{total}:{newest}:{pending_count(conn)}"


def get(conn: sqlite3.Connection, email_id: str) -> Optional[Arrival]:
    rows = _query(conn, "SELECT * FROM mail_arrivals WHERE email_id = ?", (email_id,))
    return rows[0] if rows else None


def clear_enrichment(conn: sqlite3.Connection, email_ids: Sequence[str],
                     *, commit: bool = True) -> int:
    """Un-stamp these arrivals: they are back to "in the mailbox, not read".

    The counterpart to `state_db.forget_emails`, and deliberately not a DELETE. Forgetting an email
    means it will be ingested again, so the true state afterwards is exactly what an un-enriched
    arrival says. Deleting the row instead would hide the message from the Mail page until some
    future poll re-listed it — and the arrivals watermark has usually moved past it by then, so for
    older mail that poll never comes and a reprocessed message simply disappears.

    `commit=False` is for `forget_emails`, which erases six tables and one release key as a single
    transaction. Committing in the middle of that would leave a half-forgotten email behind if a
    later step failed.
    """
    ids = list(dict.fromkeys(email_ids))
    if not ids:
        return 0
    cursor = conn.execute(
        f"UPDATE mail_arrivals SET enriched_at = NULL "
        f"WHERE email_id IN ({', '.join('?' * len(ids))})", ids,
    )
    if commit:
        conn.commit()
    return cursor.rowcount


def _query(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> List[Arrival]:
    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return [
            Arrival(
                email_id=row["email_id"],
                received_at=row["received_at"],
                sender=row["sender"],
                subject=row["subject"],
                has_attachments=bool(row["has_attachments"]),
                first_seen_at=row["first_seen_at"],
                enriched_at=row["enriched_at"],
            )
            for row in conn.execute(sql, params).fetchall()
        ]
    finally:
        conn.row_factory = prior_factory
