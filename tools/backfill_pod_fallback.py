"""Apply Premier's POD-date rule to records staged before it existed.

**This is a data backfill. It is written to be read and run by a human against a backed-up database
(CLAUDE.md §1 and §4). It does nothing without `--apply`.**

## The rule

    If the document gives a delivery date, that is the date.
    If it does not, the date the mail was received is the date.

Stated by Premier on 2026-09-03. It is live for newly staged records — `stage_records` consults
`_fallback_delivery_date` whenever a record has no `pod_stated_date` and no linked POD — and the
effect is visible in the data: records staged on 03 Sep carry a delivery date 99% of the time
(162 of 163), against 1-4% for 28 Aug to 02 Sep. This brings the earlier records into line.

## Why it is worth doing

2,332 records have no delivery date. Only **2** of them came from an email that carried a
recognised proof of delivery; 2,256 had attachments where none was a POD, and 74 had no attachment
at all. Across the whole store just 8 records ever got a date from an actual POD document — the
documents do not state one, so no amount of parsing will find it.

Of those 2,332, **690 are blocked on the delivery date and nothing else**: they complete the moment
this runs. Another ~1,039 are missing the date plus something else, so this removes half of what
stands in their way.

## What it will not do

* **Never overwrites a stated date.** Only records with an empty `pod_stated_date` are touched, so
  the first half of the rule always wins.
* **Never overrides a real proof of delivery.** A record with a `pod_ledger_id` is skipped, exactly
  as `stage_records` skips it — a linked POD owns `pod_source`, and overwriting it would lose which
  file proved the receipt.
* **Never invents a date.** A record whose email carries no received date is left alone and
  reported. An invented delivery date reads as fact on the receiver report and nothing downstream
  can tell it was made up.
* Touches only `pod_stated_date` and `pod_source` on `extracted_records`. No other column, no other
  table, no row deleted.

The rule itself is not restated here: this calls `ingest_orchestrator._fallback_delivery_date`, the
same function the pipeline uses. A second copy would drift, and historical rows disagreeing with
current behaviour is the entire problem being fixed.

`pod_source` is stamped `email_received_date`, so a fallback date stays distinguishable from a
carrier's proof on the report and at posting time. Posting still requires a linked POD, a
`pod_waived_by` waiver, or body evidence — this cannot push anything into Spitfire on its own.

Idempotent: a second run finds nothing left to do.

## How to reverse it

Restore the database copy taken in step 2. Every row this touches has `pod_source =
'email_received_date'` and can also be cleared with:

    UPDATE extracted_records SET pod_stated_date = '', pod_source = NULL
     WHERE pod_source = 'email_received_date' AND pod_ledger_id IS NULL;

but note that would also clear rows the *pipeline* stamped the same way during normal running, so
the backup is the cleaner revert.

## Running it

    python -m tools.backfill_pod_fallback              # report only; changes nothing
    python -m tools.backfill_pod_fallback --apply      # write the dates

1. Stop the console, so no run is staging while this works.
2. Copy `state/pipeline_state.sqlite3` to a dated `.bak`.
3. Run without `--apply` and read the report, especially the skipped counts.
4. Run with `--apply`.
5. Re-run without `--apply`; it should report nothing left to do.
"""

import argparse
import sqlite3
import sys

from config import settings
from pipeline import state_db
from pipeline.ingest_orchestrator import _fallback_delivery_date


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="write the dates; without it nothing is changed")
    parser.add_argument("--limit", type=int, default=0, help="stop after this many records")
    args = parser.parse_args(argv)

    conn = state_db.get_connection(settings.PIPELINE_STATE_DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT id, source_email_id, pod_ledger_id
          FROM extracted_records
         WHERE COALESCE(pod_stated_date, '') = ''
      ORDER BY id""").fetchall()
    if args.limit:
        rows = rows[:args.limit]

    updates = []
    skipped_linked_pod = 0
    skipped_no_date = []

    for row in rows:
        # The same guard `stage_records` applies. A linked POD owns the date and the source.
        if row["pod_ledger_id"] is not None:
            skipped_linked_pod += 1
            continue

        fallback = _fallback_delivery_date(conn, row["source_email_id"])
        if not fallback:
            skipped_no_date.append(row["id"])
            continue

        date, source = fallback
        updates.append((date, source, row["id"]))

    if args.apply and updates:
        conn.executemany(
            "UPDATE extracted_records SET pod_stated_date = ?, pod_source = ? WHERE id = ?",
            updates)
        conn.commit()

    verb = "dated" if args.apply else "would be dated"
    print(f"records with no delivery date : {len(rows):,}")
    print(f"{verb:<30}: {len(updates):,}")
    print(f"skipped, a real POD is linked : {skipped_linked_pod:,}")
    print(f"skipped, no date to fall back : {len(skipped_no_date):,}")

    if skipped_no_date:
        print("\n  Their emails carry no received date, so there is nothing to use. Left alone "
              "rather than guessed at. Record ids:")
        print("  " + ", ".join(str(i) for i in skipped_no_date[:40])
              + (" ..." if len(skipped_no_date) > 40 else ""))

    sources = {source for _, source, _ in updates}
    if sources:
        print(f"\npod_source stamped: {', '.join(sorted(sources))}")
        if sources - {"email_received_date"}:
            print("  WARNING: something other than the received date was used. Premier's rule is "
                  "the received date; check _fallback_delivery_date before applying.")

    if not args.apply:
        print("\nNothing was changed. Re-run with --apply once a backup exists.")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
