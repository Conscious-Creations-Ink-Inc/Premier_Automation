"""Rewrite `mail_arrivals.received_at` into one timestamp format, once.

**This is a migration. It is written to be read and run by a human against a backed-up database
(CLAUDE.md §4). It does nothing without `--apply`.**

## What it changes

The column accumulated two shapes: space-separated to minute precision (`2026-07-13 13:40`, 1,606
rows on the live store) and ISO with a Z (`2026-08-28T15:47:18Z`, 11 rows). Mixed, string
comparison inverts, because a space sorts before a `T`:

    '2026-09-03 12:08' < '2026-09-03T11:04:44Z'   ->   True

12:08 is the later instant and compares as earlier. That is not hypothetical: it skewed the first
measurement of how much mail had fallen behind the ingest listing window, on this exact column.
Every query that orders or filters this column assumes it sorts lexicographically, and until now
it did not.

`pipeline.mail_arrivals.normalise_instant` is the single definition of the target format
(`%Y-%m-%dT%H:%M:%SZ`, what the arrivals watermark already parses), and the writer now calls it —
so new rows are already correct. This brings the existing rows into line.

## What it will not do

Nothing outside `mail_arrivals.received_at`. No verdict, no ledger row, no other column, and no
row is deleted. A value it cannot parse is **left exactly as it is** and reported: a timestamp we
do not understand is still evidence of when something arrived, and inventing one is worse than
leaving it odd. Minute-precision values gain `:00` seconds — the precision was never there, and
this does not pretend otherwise.

Idempotent: normalising an already-normal value returns it unchanged, so a second run reports
nothing to do.

## How to reverse it

Restore the database copy taken in step 2. The change is confined to one column of one table, and
the reader parses both shapes, so a restored database works against either version of the code.

## Running it

    python -m tools.normalise_arrival_instants                 # report only; changes nothing
    python -m tools.normalise_arrival_instants --apply         # rewrite the rows

Suggested order:

1. Stop the console, so the fifteen-second watch is not writing while this runs.
2. Copy `state/pipeline_state.sqlite3` to a dated `.bak`.
3. Run without `--apply` and read the report — in particular the unparseable list.
4. Run with `--apply`.
5. Re-run without `--apply`; it should report nothing left to change.
"""

import argparse
import sqlite3
import sys

from config import settings
from pipeline import mail_arrivals, state_db


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="write the rewritten rows; without it nothing is changed")
    args = parser.parse_args(argv)

    conn = state_db.get_connection(settings.PIPELINE_STATE_DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT email_id, received_at FROM mail_arrivals WHERE COALESCE(received_at,'') <> ''"
    ).fetchall()

    changed, already, unparseable = [], 0, []

    for row in rows:
        current = row["received_at"]
        target = mail_arrivals.normalise_instant(current)
        if target == current:
            already += 1
            # Distinguishable from "already normal": an unparseable value is returned unchanged,
            # so the only way to tell them apart is to check the shape we were aiming for.
            if not (len(current) == 20 and current.endswith("Z") and current[10] == "T"):
                unparseable.append((row["email_id"], current))
            continue
        changed.append((target, row["email_id"]))

    if args.apply and changed:
        conn.executemany(
            "UPDATE mail_arrivals SET received_at = ? WHERE email_id = ?", changed)
        conn.commit()

    verb = "rewritten" if args.apply else "would be rewritten"
    print(f"rows with a timestamp : {len(rows)}")
    print(f"already normal        : {already - len(unparseable)}")
    print(f"{verb:<22}: {len(changed)}")

    if unparseable:
        print(f"\nleft alone, could not be parsed: {len(unparseable)}")
        for email_id, value in unparseable[:20]:
            print(f"  {value!r}  {email_id}")
        print("  These keep their original value. Decide by hand whether they mean anything.")

    if not args.apply:
        print("\nNothing was changed. Re-run with --apply once a backup exists.")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
