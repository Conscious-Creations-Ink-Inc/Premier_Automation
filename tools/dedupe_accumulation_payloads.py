"""Move each message's accumulation payload into `accumulation_payload`, once.

**This is a migration. It is written to be read and run by a human against a backed-up database
(CLAUDE.md §4). It does nothing without `--apply`.**

## What it changes

`accumulation` is keyed per (po_number, shipment_number, email_id), and it carried the serialized
message on every one of those rows. A message naming N purchase orders therefore stored its whole
body N times. Measured on Premier's live store on 2026-09-07: one expediting report naming 74
purchase orders held **173 MB by itself**, and the top rows were byte-identical copies — the rows
differ only in which delivery they belong to.

`pipeline/stage2_accumulate.py` now writes the payload once into `accumulation_payload`, keyed on
`email_id`, and leaves `accumulation.payload_json` empty. This rewrites the rows already on disk to
match: one copy per message, kept in the new table, and the per-row column emptied.

## Run the other migration first

`tools/shrink_accumulation_payloads.py` is the larger win and is independent of this one. It drops
the base64 attachment bytes out of the same column, which new writes already exclude. Measured over
the same 1,773 rows:

| | payload on disk |
|---|---|
| today | 1,939 MB |
| after `shrink_accumulation_payloads --apply` | 322 MB |
| and then after this migration | **119 MB** |

Running this one first would work, and would waste effort: it would carefully de-duplicate 1.9 GB
of attachment bytes that the other migration is about to delete outright.

## What it will not do

Nothing is dropped that cannot be read back. A row is only emptied once its payload is confirmed
present in `accumulation_payload` and byte-identical to what the row held. Where two rows for the
same message somehow disagree — which should not happen and is reported if it does — both keep
their inline copy and neither is touched. No other table, ledger row or file in `state/` is read or
written.

Idempotent: a second run finds nothing left to do.

## How to reverse it

Restore the database copy taken in step 2 below. The change is confined to one column of one table
plus rows added to `accumulation_payload`, and `stage2_accumulate._bundle_for_key` coalesces the
two homes — so a restored database works against either version of the code, and so does a
half-migrated one.

## Running it

    python -m tools.dedupe_accumulation_payloads                  # report only; changes nothing
    python -m tools.dedupe_accumulation_payloads --apply          # rewrite the rows
    python -m tools.dedupe_accumulation_payloads --apply --vacuum # and reclaim the file space

`--vacuum` is separate because VACUUM rewrites the whole database and needs free disk space equal
to its size. Without it the rows shrink but the file does not.

Suggested order:

1. Stop the console, so nothing writes while this runs.
2. Copy `state/pipeline_state.sqlite3` to a dated `.bak` and confirm the copy's size.
3. Run `tools/shrink_accumulation_payloads.py` first — see above.
4. Run this without `--apply` and read the report.
5. Run with `--apply --vacuum`.
6. Re-run without `--apply`; it should report nothing left to change.
"""

import argparse
import sqlite3
import sys
from collections import defaultdict

from pipeline import state_db


def _plan(conn: sqlite3.Connection):
    """What would move, and what is in the way.

    Returns `(movable, conflicted, already_done, bytes_before, bytes_after)`. `movable` maps
    email_id to the single payload every one of its rows agrees on; `conflicted` maps email_id to
    the number of distinct payloads found, which must be resolved by a person rather than by
    picking one.
    """
    by_email = defaultdict(set)
    bytes_before = 0
    already_done = 0
    for email_id, payload in conn.execute(
            "SELECT email_id, payload_json FROM accumulation"):
        if not payload:
            already_done += 1
            continue
        bytes_before += len(payload)
        by_email[email_id].add(payload)

    movable, conflicted = {}, {}
    for email_id, payloads in by_email.items():
        if len(payloads) == 1:
            movable[email_id] = next(iter(payloads))
        else:
            conflicted[email_id] = len(payloads)

    bytes_after = sum(len(p) for p in movable.values())
    return movable, conflicted, already_done, bytes_before, bytes_after


def _mb(n: int) -> str:
    return f"{n / 1048576:,.0f} MB"


def run(*, apply: bool = False, vacuum: bool = False, db_path=None) -> int:
    db_path = db_path or state_db.path_for("mailbox")
    conn = state_db.get_connection(db_path)
    try:
        movable, conflicted, already_done, before, after = _plan(conn)

        print(f"database            {db_path}")
        print(f"rows already moved  {already_done}")
        print(f"messages to move    {len(movable)}")
        print(f"payload on disk     {_mb(before)} -> {_mb(after)}"
              f"   (saves {_mb(before - after)})")
        if conflicted:
            # Not an error to stop on, but never resolved automatically: two rows for one message
            # holding different payloads means something wrote them at different times, and picking
            # one would silently choose which version of the message survives.
            print(f"\nmessages whose rows disagree, left untouched: {len(conflicted)}")
            for email_id, n in sorted(conflicted.items())[:20]:
                print(f"   {n} distinct payloads   {email_id}")

        if not apply:
            print("\nreport only — nothing was changed. Re-run with --apply to write.")
            return 0

        for email_id, payload in movable.items():
            conn.execute(
                "INSERT OR IGNORE INTO accumulation_payload (email_id, payload_json) VALUES (?, ?)",
                (email_id, payload))
            # Only after the replacement is confirmed present and identical. `INSERT OR IGNORE`
            # above leaves an existing row alone, so this re-reads rather than assuming.
            stored = conn.execute(
                "SELECT payload_json FROM accumulation_payload WHERE email_id = ?",
                (email_id,)).fetchone()
            if stored is None or stored[0] != payload:
                print(f"   SKIPPED {email_id}: stored copy differs from the row's payload")
                continue
            conn.execute(
                "UPDATE accumulation SET payload_json = '' WHERE email_id = ?", (email_id,))
        conn.commit()
        print(f"\nmoved {len(movable)} message payload(s)")

        if vacuum:
            print("vacuuming — this rewrites the whole file and needs free space equal to its size")
            conn.execute("VACUUM")
            print("done")
        else:
            print("rows are smaller; the file is not. Re-run with --vacuum to reclaim it.")
        return 0
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="required to actually write")
    parser.add_argument("--vacuum", action="store_true",
                        help="reclaim the file space afterwards; rewrites the whole database")
    parser.add_argument("--db", default=None, help="database path (defaults to the live store)")
    args = parser.parse_args(argv)
    return run(apply=args.apply, vacuum=args.vacuum, db_path=args.db)


if __name__ == "__main__":
    sys.exit(main())
