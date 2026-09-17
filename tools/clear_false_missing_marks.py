"""Un-mark arrivals wrongly recorded as "no longer in the mailbox", once.

**This is a migration. It is written to be read and run by a human against a backed-up database
(CLAUDE.md §4). It does nothing without `--apply`.**

## What it changes

`mail_arrivals.recovery_missing_at` records that a by-id fetch went looking for a message and the
mailbox did not have it. For 26 rows on the live store that is false, and provably so: they are
sitting in the Inbox and the pipeline settled them days earlier.

They were condemned by `connectors.mailbox.fetch_by_ids` asking Graph
`internetMessageId eq '<id>'` with an id the arrival watch had recorded — and Graph truncates
`internetMessageId` at 255 characters unless `$select` asks for the message body, which the watch
deliberately never does (see `pipeline.mail_arrivals.ID_MATCH_LEN`). A truncated id matches nothing,
`fetch_by_ids` reported the message absent, and `mark_missing` wrote the stamp.

Both halves of that are already fixed: `fetch_by_ids` now matches a truncated id by prefix, and
`_PENDING_WHERE` compares the two id shapes on their first 255 characters. This only clears the
stamps left behind.

## Why it is optional

Nothing reads `recovery_missing_at` on its own — both readers (`recoverable_count` and
`unreachable`) combine it with `_PENDING_WHERE`, and with the join fixed these rows are no longer
pending, so the stamp is already inert. It is run because a column that says "this message is gone"
about mail in the Inbox is false data, and false data is read as true by whoever finds it next.

## What it will not do

Nothing outside `mail_arrivals.recovery_missing_at`, and only on rows that have a verdict in
`email_log`. **No `email_id` is rewritten** — promoting a truncated id to its full form would look
tidier and would break `record()`, which upserts `ON CONFLICT(email_id)`: the next metadata listing
would not conflict and would insert a second row for the same message.

A row with no verdict keeps its stamp. Those are the genuinely missing ones — on the live store, 3
messages including an urgent purchase-order email — and they are the reason the count on the Mail
page is not zero. Clearing them would be inventing an answer this tool has no evidence for.

Idempotent: a second run finds nothing to clear.

## How to reverse it

Restore the database copy taken in step 2. The change is one column of one table, and the stamp is
re-derivable — a later run of the recovery will re-stamp anything genuinely absent.

## Running it

    python -m tools.clear_false_missing_marks              # report only; changes nothing
    python -m tools.clear_false_missing_marks --apply      # clear the stamps

Suggested order:

1. Stop the console, so the fifteen-second watch is not writing while this runs.
2. Copy `state/pipeline_state.sqlite3` to a dated `.bak`.
3. Run without `--apply` and read the report.
4. Run with `--apply`.
5. Re-run without `--apply`; it should report nothing left to clear.
"""

import argparse
import sqlite3

from config import settings
from pipeline import mail_arrivals, state_db


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="clear the stamps; without it nothing is changed")
    args = parser.parse_args(argv)

    conn = state_db.get_connection(settings.PIPELINE_STATE_DB_PATH)
    conn.row_factory = sqlite3.Row

    # Marked missing, but `email_log` holds a verdict for it — matched the way everything else
    # matches these two tables, on the first `ID_MATCH_LEN` characters.
    wrong = conn.execute(
        f"""
        SELECT a.email_id, a.subject, a.recovery_missing_at, e.processed_at, e.category
          FROM mail_arrivals a
          JOIN email_log e
            ON substr(e.email_id, 1, {mail_arrivals.ID_MATCH_LEN})
             = substr(a.email_id, 1, {mail_arrivals.ID_MATCH_LEN})
         WHERE a.recovery_missing_at IS NOT NULL
         ORDER BY a.received_at DESC
        """
    ).fetchall()

    # The other half of the same column, reported so the difference is on screen rather than
    # inferred: these have no verdict and the stamp is the truth about them.
    genuinely = conn.execute(
        f"""
        SELECT COUNT(*)
          FROM mail_arrivals a
          LEFT JOIN email_log e
            ON substr(e.email_id, 1, {mail_arrivals.ID_MATCH_LEN})
             = substr(a.email_id, 1, {mail_arrivals.ID_MATCH_LEN})
         WHERE a.recovery_missing_at IS NOT NULL AND e.email_id IS NULL
        """
    ).fetchone()[0]

    print(f"marked 'not in the mailbox' but settled by the pipeline: {len(wrong)}")
    for row in wrong[:10]:
        print(f"  marked {row['recovery_missing_at'][:10]}  "
              f"settled {str(row['processed_at'])[:10]} as {row['category']:8} "
              f"{(row['subject'] or '')[:48]}")
    if len(wrong) > 10:
        print(f"  … and {len(wrong) - 10} more")
    print(f"marked 'not in the mailbox' and genuinely never read: {genuinely}  (left alone)")

    if not wrong:
        print("nothing to clear.")
        return 0
    if not args.apply:
        print("\nreport only — re-run with --apply to clear them.")
        return 0

    conn.executemany(
        "UPDATE mail_arrivals SET recovery_missing_at = NULL WHERE email_id = ?",
        [(row["email_id"],) for row in wrong],
    )
    conn.commit()
    print(f"\ncleared {len(wrong)} stamp(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
