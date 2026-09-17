"""Mark staged receipts that no evidence supports, so a person decides rather than the pipeline.

Records staged from a confirmation grid before the receipt gate existed carry a quantity taken from
the grid's `Qty` column — the **ordered** figure — with nothing anywhere saying the goods arrived.
On the ATTIC STOCK thread that produced three of them, one for a line the property had said in the
same email had never been received.

Nothing is deleted or voided. Each record gets a `manual_note` saying what is wrong with it, and
`status` is left exactly as it was: these are somebody's to judge, not this tool's.

New mail needs none of this — `confirmation.records_from_grid` now refuses to call an unevidenced
quantity received. This is only for rows already in the store.

    python -m tools.flag_unevidenced_receipts [--dry-run] [--db PATH]
"""

import argparse
import sqlite3
import sys

from pipeline import state_db

_NOTE = ("staged before the receipt gate existed: the quantity is the ORDERED figure from a "
         "request grid and no proof of delivery is linked to this record — confirm receipt before "
         "posting")


def _candidates(conn: sqlite3.Connection):
    """Auto-staged confirmation-grid rows carrying a quantity with no POD behind them.

    `origin = 'auto'` so a record a person built by hand is never second-guessed, and
    `pod_ledger_id IS NULL` because a row that does point at its proof is exactly the row this is
    not about.
    """
    return conn.execute("""
        SELECT id, po_number, spec_code, quantity_received, extraction_confidence,
               COALESCE(manual_note, '') AS manual_note
          FROM extracted_records
         WHERE origin = 'auto'
           AND status = 'pending'
           AND extraction_source LIKE '%confirmation_grid%'
           AND quantity_received IS NOT NULL
           AND pod_ledger_id IS NULL
         ORDER BY id
    """).fetchall()


def run(dry_run: bool = False, db_path=None) -> int:
    conn = state_db.get_connection(db_path) if db_path else state_db.get_connection()
    conn.row_factory = sqlite3.Row

    flagged = 0
    for row in _candidates(conn):
        if _NOTE in row["manual_note"]:
            continue   # already flagged by an earlier run
        print(f"  record #{row['id']}: PO {row['po_number']} {row['spec_code']} "
              f"qty {row['quantity_received']} (confidence {row['extraction_confidence']})")
        if not dry_run:
            note = "; ".join(part for part in (row["manual_note"], _NOTE) if part)
            conn.execute("UPDATE extracted_records SET manual_note = ? WHERE id = ?",
                         (note, row["id"]))
        flagged += 1

    if not dry_run:
        conn.commit()
    print(f"\n{'would flag' if dry_run else 'flagged'} {flagged} record(s) for review")
    return flagged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report and change nothing")
    parser.add_argument("--db", default=None, help="state DB path (defaults to the live store)")
    args = parser.parse_args()
    run(dry_run=args.dry_run, db_path=args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
