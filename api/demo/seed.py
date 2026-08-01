"""Builds the demo dataset.

Order matters and is deliberate: PO lines first, then records derived from them, then a match
verdict for every record, and only then the auto-approvals. Computing all verdicts before any
approval moves quantity keeps the over-receipt case honest — it is flagged against the line's
original outstanding quantity, not one already consumed by a different receipt.

Run standalone with:  python -m api.demo.seed --reset
"""
import argparse
import uuid
from datetime import datetime, timezone
from typing import Dict

from api import db as api_db
from api.demo import catalog
from api.services import decisions, reconcile
from api.stores import emails_store, po_lines_store, reconciliation_store, vendor_store
from config import settings
from pipeline import extracted_records_store
from pipeline.models import ExtractedRecord, POLine

# Stable namespace so a reseed reproduces identical line keys — the demo must look the same
# every time it is rebuilt.
_LINE_KEY_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "premier/spitfire/po-lines")

# The pipeline's own folder map, so the organizer proposes exactly what the real orchestrator
# would do with each triage category.
FOLDER_BY_CATEGORY = {
    "hide": settings.MAILBOX_FOLDER_HIDDEN,
    "route": settings.MAILBOX_FOLDER_ROUTED,
    "surface": settings.MAILBOX_FOLDER_PROCESSED,
    "hold": settings.MAILBOX_FOLDER_PROCESSED,
}

# Child rows first — plain deletes, no FK cascade to rely on.
_TABLES_TO_CLEAR = (
    "review_decisions", "staged_receipts", "match_results", "vendor_send_log",
    "demo_emails", "po_lines", "extracted_records", "vendor_template",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _line_key(po_number: str, line_number: int) -> str:
    return str(uuid.uuid5(_LINE_KEY_NAMESPACE, f"{po_number}/{line_number}"))


def reset(conn) -> None:
    """Empties every demo-owned table. Never touches pipeline_state.sqlite3 — this connection
    is the dashboard's own database."""
    for table in _TABLES_TO_CLEAR:
        conn.execute(f"DELETE FROM {table}")
    conn.commit()


def seed_demo(conn, reset_first: bool = False) -> Dict[str, int]:
    if reset_first:
        reset(conn)
    now = _now_iso()

    for spec in catalog.PO_LINES:
        po_lines_store.insert(conn, POLine(
            line_key=_line_key(spec["po_number"], spec["line_number"]), **spec
        ))

    for payload in catalog.EMAILS:
        emails_store.insert(conn, emails_store.DemoEmail(
            proposed_folder=FOLDER_BY_CATEGORY[payload["triage_category"]],
            keep_in_inbox=payload["notification_type"] in catalog.KEEP_IN_INBOX_TYPES,
            organized_folder=None,
            moved_at=None,
            **payload,
        ))

    po_lines = po_lines_store.list_all(conn)
    for payload in catalog.CLEAN_RECORDS + catalog.FLAGGED_RECORDS:
        record = ExtractedRecord(**payload)
        record_id = extracted_records_store.write_pending(conn, record, now)
        reconciliation_store.write(
            conn, reconcile.compute_match(record_id, record, po_lines, now), now
        )

    # Settle everything the automation was sure about, exactly as it would have.
    for match in reconciliation_store.list_matches(
        conn, review_status=reconciliation_store.REVIEW_AUTO_APPROVED
    ):
        decisions.auto_approve(conn, match.id, now)

    vendor_store.get_template(conn)   # materialise the default template row
    return summary(conn)


def seed_if_empty(conn) -> Dict[str, int]:
    """Used on API startup so a fresh checkout has something to show immediately."""
    if api_db.is_seeded(conn):
        return summary(conn)
    return seed_demo(conn)


def summary(conn) -> Dict[str, int]:
    def scalar(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    return {
        "po_lines": scalar("SELECT COUNT(*) FROM po_lines"),
        "emails": scalar("SELECT COUNT(*) FROM demo_emails"),
        "extracted_records": scalar("SELECT COUNT(*) FROM extracted_records"),
        "matches": scalar("SELECT COUNT(*) FROM match_results"),
        "flagged": scalar("SELECT COUNT(*) FROM match_results WHERE flagged = 1"),
        "exception_queue": scalar(
            "SELECT COUNT(*) FROM match_results WHERE flagged = 1 AND review_status = 'pending_review'"
        ),
        "auto_approved": scalar(
            "SELECT COUNT(*) FROM match_results WHERE review_status = 'auto_approved'"
        ),
        "staged_receipts": scalar("SELECT COUNT(*) FROM staged_receipts"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the reconciliation dashboard demo data.")
    parser.add_argument("--reset", action="store_true", help="clear existing demo rows first")
    args = parser.parse_args()

    conn = api_db.get_demo_connection()
    try:
        counts = seed_demo(conn, reset_first=args.reset)
    finally:
        conn.close()

    print(f"seeded {api_db.config.DEMO_DB_PATH}")
    for name, value in counts.items():
        print(f"  {name:<18} {value}")


if __name__ == "__main__":
    main()
