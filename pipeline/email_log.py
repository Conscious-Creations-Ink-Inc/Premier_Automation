"""One durable row per email, carrying the verdict Stage 1 reached and where the mail was filed.

The gap this closes: a ROUTE or HIDE email left no trace anywhere. Its triage reason was
`print()`ed and discarded, and an email with no attachments creates no `attachment_ledger` row
either — so the two categories that most need a person to look were the two that were
structurally invisible in the database. `attachment_ledger.orphans()` proved no attachment went
missing; nothing proved no *email* did.

Current verdict per email, not history: `UNIQUE(email_id)` plus `INSERT OR REPLACE` means a
reprocess after `state_db.forget_message` overwrites rather than accumulating. Anything that
wants "what did we decide last Tuesday" needs a separate events table, not this one.
"""
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional

CATEGORY_ERROR = "error"
"""Not a `TriageCategory`. Recorded when triage never ran or raised, so the row still exists —
an email that failed to process is exactly the kind a person needs to see."""

_COLUMNS = (
    "email_id", "subject", "sender", "origin_sender", "email_date", "origin_sent_at",
    "notification_type",
    "category", "matched_rule", "reason", "po_hints", "shipment_hint", "notification_number",
    "attachment_count", "ocr_attempted", "folder", "error_type", "processed_at",
    # Where the message was read *from*, as against `folder`, which is where it was put afterwards.
    "source_folder",
    # Positively not a delivery notification, as against merely unrecognised.
    "not_a_delivery",
)


@dataclass
class EmailLogRow:
    id: int
    email_id: str
    subject: str
    sender: str
    origin_sender: Optional[str]
    email_date: str
    origin_sent_at: Optional[str]
    notification_type: Optional[str]
    category: str
    matched_rule: str
    reason: str
    po_hints: str
    shipment_hint: Optional[str]
    notification_number: Optional[str]
    attachment_count: int
    ocr_attempted: int
    folder: str
    error_type: Optional[str]
    processed_at: str
    source_folder: str = ""
    """The mailbox folder this message was read from, as against `folder`, which is where the
    pipeline filed it afterwards. Defaulted so rows written before the column still load."""
    not_a_delivery: int = 0
    """1 when triage positively identified this as not a delivery notification."""


def record(
    conn: sqlite3.Connection,
    *,
    email_id: str,
    subject: str = "",
    sender: str = "",
    origin_sender: Optional[str] = None,
    email_date: str = "",
    origin_sent_at: Optional[str] = None,
    notification_type: Optional[str] = None,
    category: str,
    matched_rule: str = "",
    reason: str = "",
    po_hints: str = "",
    shipment_hint: Optional[str] = None,
    notification_number: Optional[str] = None,
    attachment_count: int = 0,
    ocr_attempted: int = 0,
    folder: str,
    error_type: Optional[str] = None,
    processed_at: str,
    source_folder: str = "",
    not_a_delivery: bool = False,
) -> None:
    """Write (or overwrite) this email's verdict."""
    values = (
        email_id, subject or "", sender or "", origin_sender, email_date or "", origin_sent_at,
        notification_type,
        category, matched_rule or "", reason or "", po_hints or "", shipment_hint,
        notification_number, attachment_count, ocr_attempted, folder, error_type, processed_at,
        source_folder or "", 1 if not_a_delivery else 0,
    )
    placeholders = ", ".join(["?"] * len(_COLUMNS))
    conn.execute(
        f"INSERT OR REPLACE INTO email_log ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
        values,
    )
    conn.commit()


def _query(conn: sqlite3.Connection, sql: str, params=()) -> List[sqlite3.Row]:
    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.row_factory = prior_factory


def _to_row(row: sqlite3.Row) -> EmailLogRow:
    return EmailLogRow(id=row["id"], **{col: row[col] for col in _COLUMNS})


def list_all(conn: sqlite3.Connection) -> List[EmailLogRow]:
    """Newest first — the run you just did is the one you want to look at."""
    return [_to_row(r) for r in _query(
        conn, "SELECT * FROM email_log ORDER BY processed_at DESC, id DESC"
    )]


def get(conn: sqlite3.Connection, email_id: str) -> Optional[EmailLogRow]:
    rows = _query(conn, "SELECT * FROM email_log WHERE email_id = ?", (email_id,))
    return _to_row(rows[0]) if rows else None


def counts_by_category(conn: sqlite3.Connection) -> Dict[str, int]:
    return {r["category"]: r["n"] for r in _query(
        conn, "SELECT category, COUNT(*) AS n FROM email_log GROUP BY category"
    )}


def subjects_by_email_id(conn: sqlite3.Connection) -> Dict[str, str]:
    """Lookup used to give an attachment or a record its email's subject — the only thing that
    makes a row on the manual queue identifiable to a person."""
    return {r["email_id"]: r["subject"] for r in _query(
        conn, "SELECT email_id, subject FROM email_log"
    )}


def envelope_by_email_id(conn: sqlite3.Connection) -> Dict[str, Dict[str, str]]:
    """Sender and folder per message, for queue rows that are not themselves email rows.

    `subjects_by_email_id` above already covers the subject. These two are the rest of what the
    manual queue shows about a message, and an attachment row carries neither: 57 of the messages
    on that queue are there *only* because of a bad attachment, so without this lookup they render
    with no sender and no folder — the two columns that say whose problem it is and where it went.
    """
    return {r["email_id"]: {"sender": r["sender"] or "", "folder": r["folder"] or ""}
            for r in _query(conn, "SELECT email_id, sender, folder FROM email_log")}


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM email_log").fetchone()[0]
