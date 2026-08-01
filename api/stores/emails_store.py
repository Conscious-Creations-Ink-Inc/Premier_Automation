"""Query layer for the synthetic emails and the triage verdict each one received.

This is the read model behind three screens: the context shown beside an extracted record,
the delivery report's three status buckets, and the inbox organizer. `proposed_folder` is
computed at seed time from the pipeline's own folder map in `config.settings`, so the
organizer always agrees with what the real orchestrator would do.
"""
import sqlite3
from dataclasses import dataclass
from typing import List, Optional, Sequence

_COLUMNS = (
    "email_id", "received_at", "sender_address", "sender_domain", "subject", "body_snippet",
    "notification_type", "triage_category", "matched_rule", "reason", "status_keyword",
    "po_number", "has_attachment", "proposed_folder", "keep_in_inbox", "organized_folder",
    "moved_at",
)


@dataclass
class DemoEmail:
    email_id: str
    received_at: str
    sender_address: str
    sender_domain: str
    subject: str
    body_snippet: Optional[str]
    notification_type: str
    triage_category: str
    matched_rule: str
    reason: str
    status_keyword: str          # delivered | received | out_for_delivery | cancelled | none
    po_number: Optional[str]
    has_attachment: bool
    proposed_folder: str
    keep_in_inbox: bool
    organized_folder: Optional[str]
    moved_at: Optional[str]

    @property
    def is_organized(self) -> bool:
        return self.organized_folder is not None


def insert(conn: sqlite3.Connection, email: DemoEmail) -> None:
    placeholders = ", ".join(["?"] * len(_COLUMNS))
    conn.execute(
        f"INSERT INTO demo_emails ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
        tuple(_to_sql(getattr(email, column)) for column in _COLUMNS),
    )
    conn.commit()


def list_all(conn: sqlite3.Connection) -> List[DemoEmail]:
    return _query(conn, "SELECT * FROM demo_emails ORDER BY received_at DESC")


def get(conn: sqlite3.Connection, email_id: str) -> Optional[DemoEmail]:
    rows = _query(conn, "SELECT * FROM demo_emails WHERE email_id = ?", (email_id,))
    return rows[0] if rows else None


def list_pending_organize(conn: sqlite3.Connection) -> List[DemoEmail]:
    """Everything the organizer would still move — i.e. not already filed and not one of the
    mails we deliberately leave in the inbox (confirmations, cancellations, unclear)."""
    return _query(
        conn,
        "SELECT * FROM demo_emails WHERE organized_folder IS NULL AND keep_in_inbox = 0 "
        "ORDER BY received_at DESC",
    )


def mark_organized(conn: sqlite3.Connection, email_ids: Sequence[str], now: str) -> int:
    """Simulates the Graph folder move — records where each mail was filed without touching a
    real mailbox. Returns how many rows actually changed."""
    if not email_ids:
        return 0
    moved = 0
    for email_id in email_ids:
        cursor = conn.execute(
            "UPDATE demo_emails SET organized_folder = proposed_folder, moved_at = ? "
            "WHERE email_id = ? AND organized_folder IS NULL AND keep_in_inbox = 0",
            (now, email_id),
        )
        moved += cursor.rowcount
    conn.commit()
    return moved


def _to_sql(value):
    return int(value) if isinstance(value, bool) else value


def _query(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> List[DemoEmail]:
    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return [
            DemoEmail(
                **{
                    column: (bool(row[column]) if column in ("has_attachment", "keep_in_inbox")
                             else row[column])
                    for column in _COLUMNS
                }
            )
            for row in rows
        ]
    finally:
        conn.row_factory = prior_factory
