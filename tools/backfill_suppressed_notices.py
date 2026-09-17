"""Recover suppressions that Stage 2 made before it wrote them down.

Stage 2 correctly refuses to accumulate a notice whose delivery has already been released — that
refusal is the whole reason one physical delivery does not become two receivers. Until
`suppressed_notices` existed the refusal wrote only a log line, so the message ended with no
accumulation row and no record, which is indistinguishable from a message nothing could be read
from. `read_views.manual_queue` therefore showed it as

    "read as delivery mail but nothing was extracted from it — no PO, spec, quantity or POD was
     recovered from the body or any attachment"

which is false in every clause. New mail needs none of this: `stage2_accumulate` records the
suppression as it makes it. This is only for rows already in the store.

Conservative on purpose. A message is recovered only when **every** purchase order it names was
already released **before** that message was processed. A message with even one unreleased PO was
not suppressed for this reason, and guessing at it would replace a wrong label with a different
wrong label.

Read-only except for inserts into `suppressed_notices`; nothing existing is modified or deleted.

    python -m tools.backfill_suppressed_notices [--dry-run] [--db PATH]
"""

import argparse
import sqlite3
import sys

from pipeline import state_db

_REASON = ("the same delivery was already released and staged from an earlier message "
           "(recovered by backfill)")


def _candidates(conn: sqlite3.Connection):
    """Mail triaged as a delivery that produced nothing, and has no suppression row yet.

    Exactly the population `manual_queue` calls `nothing_extracted`, which is what makes this
    query the right one: whatever it recovers leaves that bucket, and whatever it leaves behind is
    a genuine read failure that deserves the label.
    """
    return conn.execute("""
        SELECT e.email_id, e.subject, e.po_hints, e.processed_at
          FROM email_log e
         WHERE e.category IN ('surface', 'hold')
           AND COALESCE(e.po_hints, '') <> ''
           AND NOT EXISTS (SELECT 1 FROM extracted_records x WHERE x.source_email_id = e.email_id)
           AND NOT EXISTS (SELECT 1 FROM accumulation a WHERE a.email_id = e.email_id)
           AND NOT EXISTS (SELECT 1 FROM suppressed_notices s WHERE s.email_id = e.email_id)
         ORDER BY e.processed_at
    """).fetchall()


def _release_before(conn: sqlite3.Connection, po_number: str, processed_at: str):
    """When this PO was released, if that happened before the message was processed.

    The ordering test is what makes the inference safe. A release *after* the message means the
    message cannot have been skipped for being late — something else produced nothing, and this
    tool must not claim otherwise.
    """
    row = conn.execute(
        "SELECT released_at, delivery_ref FROM released_events "
        "WHERE po_number = ? AND released_at <= ? ORDER BY released_at LIMIT 1",
        (po_number, processed_at or "9999"),
    ).fetchone()
    return (row[0], row[1] or "") if row else None


def run(dry_run: bool = False, db_path=None) -> int:
    conn = state_db.get_connection(db_path) if db_path else state_db.get_connection()
    conn.row_factory = None

    recovered = 0
    for email_id, subject, po_hints, processed_at in _candidates(conn):
        pos = [p.strip() for p in str(po_hints or "").split(",") if p.strip()]
        releases = {po: _release_before(conn, po, processed_at) for po in pos}
        if not pos or not all(releases.values()):
            unresolved = [po for po, rel in releases.items() if not rel]
            print(f"  skipped {subject[:58]!r}: PO {', '.join(unresolved)} was not already "
                  f"released — this message produced nothing for some other reason")
            continue

        for po, (released_at, delivery_ref) in releases.items():
            print(f"  {subject[:58]!r}: PO {po} released {released_at[:16]}")
            if not dry_run:
                conn.execute(
                    "INSERT OR IGNORE INTO suppressed_notices "
                    "(email_id, po_number, delivery_ref, released_at, suppressed_at, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (email_id, po, delivery_ref, released_at, processed_at, _REASON),
                )
            recovered += 1

    if not dry_run:
        conn.commit()
    print(f"\n{'would recover' if dry_run else 'recovered'} {recovered} suppression row(s)")
    return recovered


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report and change nothing")
    parser.add_argument("--db", default=None, help="state DB path (defaults to the live store)")
    args = parser.parse_args()
    run(dry_run=args.dry_run, db_path=args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
