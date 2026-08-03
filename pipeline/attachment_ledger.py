"""The attachment ledger — one durable row per attachment, always, with a terminal verdict.

The problem this solves: before it existed, an attachment nobody could read produced a single
`print()` to a console that, in an unattended run, nobody is watching. It never reached the
exception queue either, because that queue is keyed on `match_results`, and a file that was
never read produces no extracted record and therefore no match row. Five of the fifteen sniffed
kinds had no reader at all, and four further drops inside the intake connector were bare
`continue` statements. The system could lose a proof of delivery and show no sign of it.

Two design points carry the weight:

* **Rows are written before triage branches.** `process_new_mail` exits early for ROUTE and HIDE
  mail, so anything recorded during extraction structurally cannot see those messages — and
  ROUTE is exactly where the interesting failures land, since that is where a photographed POD
  with no readable text ends up.
* **`orphans()` must always return nothing.** A row still sitting at `OBSERVED` after a run means
  an attachment escaped the dispatcher. That is the bug class this module exists to make
  impossible to hide, so it is queryable rather than merely hoped for.
"""

import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from pipeline.models import Attachment, RawEmail
from pipeline.parsing import sniff

# --- Dispositions -----------------------------------------------------------

OBSERVED = "observed"
"""Transient — recorded at ingest, not yet dispatched. Never a resting state."""

NOT_DISPATCHED = "not_dispatched"     # the email was ROUTE/HIDE, so Stage 3 never ran on it
AWAITING_RELEASE = "awaiting_release"
"""On a HOLD email whose delivery event has not fired yet. A resting state, not a loss — the
attachment is read when the partner notice arrives or the grace sweep releases it."""
CONTAINER_EXPANDED = "container_expanded"   # a zip/.msg/.eml; its members have their own rows
EXTRACTED = "extracted"               # an adapter claimed it and produced records
EMPTY = "empty"                       # an adapter claimed it, ran clean, found nothing
UNREADABLE = "unreadable"             # an adapter claimed it and raised
ENCRYPTED = "encrypted"               # password- or DRM-protected
CORRUPT = "corrupt"                   # structurally broken, and not encrypted
NO_ADAPTER = "no_adapter"             # nothing claimed it — a real coverage gap
UNSUPPORTED_FORMAT = "unsupported_format"   # recognised, deliberately no reader (.doc, .pptx)
DROPPED_DECORATIVE = "dropped_decorative"
DROPPED_DUPLICATE = "dropped_duplicate"
DROPPED_OVERSIZE = "dropped_oversize"
DROPPED_DEPTH = "dropped_depth"
DROPPED_COUNT = "dropped_count"
DROPPED_EMPTY = "dropped_empty"       # zero bytes

ALL_DISPOSITIONS = frozenset({
    OBSERVED, NOT_DISPATCHED, AWAITING_RELEASE, CONTAINER_EXPANDED, EXTRACTED, EMPTY, UNREADABLE, ENCRYPTED,
    CORRUPT, NO_ADAPTER, UNSUPPORTED_FORMAT, DROPPED_DECORATIVE, DROPPED_DUPLICATE,
    DROPPED_OVERSIZE, DROPPED_DEPTH, DROPPED_COUNT, DROPPED_EMPTY,
})
TERMINAL = ALL_DISPOSITIONS - {OBSERVED}

NEEDS_ATTENTION = frozenset({
    UNREADABLE, ENCRYPTED, CORRUPT, NO_ADAPTER, UNSUPPORTED_FORMAT,
    DROPPED_OVERSIZE, DROPPED_DEPTH, DROPPED_COUNT,
})
"""Dispositions a person should see. Deliberately excludes the decorative/duplicate drops,
which are routine and correct, and `empty`, which means we read it and it genuinely held
nothing."""

REVIEW_NONE = "none"
REVIEW_PENDING = "pending_review"
REVIEW_ACKNOWLEDGED = "acknowledged"
REVIEW_REPROCESS = "reprocess_requested"

_COLUMNS = (
    "email_id", "parent_id", "depth", "ordinal", "container_path", "filename",
    "declared_content_type", "sniffed_kind", "sniff_reason", "sha256", "size_bytes",
    "content_id", "is_inline", "triage_category", "claimed_by", "records_extracted",
    "disposition", "disposition_detail", "error_type", "review_status",
    "first_seen_at", "last_updated_at",
)


@dataclass
class LedgerRow:
    id: int
    email_id: str
    filename: str
    sniffed_kind: str
    sha256: str
    size_bytes: int
    disposition: str
    disposition_detail: str
    container_path: str = ""
    depth: int = 0
    claimed_by: Optional[str] = None
    records_extracted: int = 0
    error_type: Optional[str] = None
    triage_category: Optional[str] = None
    review_status: str = REVIEW_NONE
    parent_id: Optional[int] = None


def _row(record: sqlite3.Row) -> LedgerRow:
    return LedgerRow(
        id=record["id"], email_id=record["email_id"], filename=record["filename"],
        sniffed_kind=record["sniffed_kind"], sha256=record["sha256"],
        size_bytes=record["size_bytes"], disposition=record["disposition"],
        disposition_detail=record["disposition_detail"], container_path=record["container_path"],
        depth=record["depth"], claimed_by=record["claimed_by"],
        records_extracted=record["records_extracted"], error_type=record["error_type"],
        triage_category=record["triage_category"], review_status=record["review_status"],
        parent_id=record["parent_id"],
    )


# --- Writing ----------------------------------------------------------------


def observe(
    conn: sqlite3.Connection,
    email: RawEmail,
    triage_category: Optional[str],
    now: str,
) -> None:
    """Record every attachment on this email and stamp each one's `ledger_id` in place.

    Attachments the connector already decided against still get a row — carrying their
    `drop_hint` as a terminal disposition — because "we dropped this and here is why" is the
    whole point. Idempotent on the unique key, so a re-run adds nothing.
    """
    for ordinal, attachment in enumerate(email.attachments):
        disposition, detail = _disposition_for_hint(attachment.drop_hint)
        attachment.ledger_id = _insert(
            conn,
            email_id=email.email_id,
            parent_id=None,
            depth=0,
            ordinal=ordinal,
            container_path=attachment.container_path or attachment.filename,
            attachment=attachment,
            triage_category=triage_category,
            disposition=disposition,
            detail=detail,
            now=now,
        )


def add_child(
    conn: sqlite3.Connection,
    parent_id: Optional[int],
    email_id: str,
    attachment: Attachment,
    depth: int,
    ordinal: int,
    triage_category: Optional[str],
    now: str,
) -> int:
    """Record one member of an expanded container, parented to it."""
    disposition, detail = _disposition_for_hint(attachment.drop_hint)
    attachment.ledger_id = _insert(
        conn, email_id=email_id, parent_id=parent_id, depth=depth, ordinal=ordinal,
        container_path=attachment.container_path or attachment.filename,
        attachment=attachment, triage_category=triage_category,
        disposition=disposition, detail=detail, now=now,
    )
    return attachment.ledger_id


def _disposition_for_hint(drop_hint: Optional[str]) -> tuple:
    """A connector's `drop_hint` becomes a terminal disposition; no hint means still in flight."""
    if not drop_hint:
        return OBSERVED, ""
    head = drop_hint.split(":", 1)[0]
    mapping = {
        "decorative": DROPPED_DECORATIVE,
        "duplicate": DROPPED_DUPLICATE,
        "oversize": DROPPED_OVERSIZE,
        "empty": DROPPED_EMPTY,
        "depth": DROPPED_DEPTH,
        "count": DROPPED_COUNT,
    }
    return mapping.get(head, NO_ADAPTER), drop_hint


def _insert(
    conn: sqlite3.Connection,
    *,
    email_id: str,
    parent_id: Optional[int],
    depth: int,
    ordinal: int,
    container_path: str,
    attachment: Attachment,
    triage_category: Optional[str],
    disposition: str,
    detail: str,
    now: str,
) -> Optional[int]:
    result = sniff.sniff(attachment.content_bytes, attachment.filename, attachment.content_type or "")
    # A dropped attachment has had its bytes cleared, so re-sniffing returns `unknown`. The kind
    # recorded at ingest, while the bytes were still present, is the truthful one.
    kind = attachment.sniffed_kind or result.kind
    reason = result.reason if kind == result.kind else "recorded at ingest, before the bytes were released"
    values = (
        email_id, parent_id, depth, ordinal, container_path, attachment.filename,
        attachment.content_type, kind, reason,
        attachment.sha256 or result.sha256,
        attachment.size_bytes or len(attachment.content_bytes or b""),
        attachment.content_id, 1 if attachment.is_inline else 0,
        triage_category, None, 0, disposition, detail, None, REVIEW_NONE, now, None,
    )
    placeholders = ", ".join(["?"] * len(_COLUMNS))
    cursor = conn.execute(
        f"INSERT OR IGNORE INTO attachment_ledger ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
        values,
    )
    conn.commit()
    if cursor.lastrowid:
        return cursor.lastrowid
    existing = conn.execute(
        "SELECT id FROM attachment_ledger WHERE email_id = ? AND depth = ? AND ordinal = ? AND sha256 = ?",
        (email_id, depth, ordinal, attachment.sha256 or result.sha256),
    ).fetchone()
    return existing[0] if existing else None


def record_outcome(
    conn: sqlite3.Connection,
    ledger_id: Optional[int],
    disposition: str,
    now: str,
    *,
    claimed_by: Optional[str] = None,
    records_extracted: int = 0,
    detail: str = "",
    error_type: Optional[str] = None,
) -> None:
    """Close a row with what actually happened. A no-op for `ledger_id=None` so callers that
    don't have one (unit tests, direct adapter calls) need no special-casing."""
    if ledger_id is None:
        return
    if disposition not in ALL_DISPOSITIONS:
        raise ValueError(f"unknown disposition {disposition!r}")
    review = REVIEW_PENDING if disposition in NEEDS_ATTENTION else REVIEW_NONE
    conn.execute(
        """UPDATE attachment_ledger
              SET disposition = ?, disposition_detail = ?, claimed_by = ?,
                  records_extracted = ?, error_type = ?, review_status = ?, last_updated_at = ?
            WHERE id = ?""",
        (disposition, detail, claimed_by, records_extracted, error_type, review, now, ledger_id),
    )
    conn.commit()


def close_undispatched(conn: sqlite3.Connection, email: RawEmail, now: str) -> None:
    """Mark this email's still-open rows as never dispatched — used on the ROUTE/HIDE paths,
    where Stage 3 legitimately never runs but the attachments must still be accounted for."""
    for attachment in email.attachments:
        if attachment.ledger_id is None:
            continue
        conn.execute(
            """UPDATE attachment_ledger
                  SET disposition = ?, disposition_detail = ?, last_updated_at = ?
                WHERE id = ? AND disposition = ?""",
            (NOT_DISPATCHED, "email was routed or hidden before extraction", now,
             attachment.ledger_id, OBSERVED),
        )
    conn.commit()


def close_open_rows(conn: sqlite3.Connection, now: str) -> int:
    """Settle anything still `observed` at the end of a pass.

    These are attachments on HOLD mail whose delivery event has not fired — they are waiting,
    not lost. Closing them explicitly is what lets `orphans()` mean "something escaped the
    dispatcher" rather than "something is pending", which is the only way that check is useful.
    """
    cursor = conn.execute(
        """UPDATE attachment_ledger
              SET disposition = ?, disposition_detail = ?, last_updated_at = ?
            WHERE disposition = ?""",
        (AWAITING_RELEASE, "held pending the delivery event that will release it", now, OBSERVED),
    )
    conn.commit()
    return cursor.rowcount


def set_review_status(conn: sqlite3.Connection, ledger_id: int, status: str, now: str) -> None:
    conn.execute(
        "UPDATE attachment_ledger SET review_status = ?, last_updated_at = ? WHERE id = ?",
        (status, now, ledger_id),
    )
    conn.commit()


# --- Reading ----------------------------------------------------------------


def _query(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> List[LedgerRow]:
    prior = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return [_row(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.row_factory = prior


def for_email(conn: sqlite3.Connection, email_id: str) -> List[LedgerRow]:
    return _query(conn, "SELECT * FROM attachment_ledger WHERE email_id = ? ORDER BY depth, ordinal, id", (email_id,))


def list_needing_attention(conn: sqlite3.Connection) -> List[LedgerRow]:
    marks = ", ".join(["?"] * len(NEEDS_ATTENTION))
    return _query(
        conn,
        f"SELECT * FROM attachment_ledger WHERE disposition IN ({marks}) ORDER BY first_seen_at DESC",
        tuple(sorted(NEEDS_ATTENTION)),
    )


def orphans(conn: sqlite3.Connection) -> List[LedgerRow]:
    """Rows still at `OBSERVED`. Always empty in a correct run — a non-empty result means an
    attachment slipped past the dispatcher without a verdict."""
    return _query(conn, "SELECT * FROM attachment_ledger WHERE disposition = ?", (OBSERVED,))


def counts_by_disposition(conn: sqlite3.Connection) -> Dict[str, int]:
    return {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT disposition, COUNT(*) FROM attachment_ledger GROUP BY disposition"
        ).fetchall()
    }


def counts_by_kind(conn: sqlite3.Connection) -> Dict[str, int]:
    return {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT sniffed_kind, COUNT(*) FROM attachment_ledger GROUP BY sniffed_kind"
        ).fetchall()
    }
