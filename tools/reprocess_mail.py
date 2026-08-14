"""Run already-ingested mail through the pipeline again, after a rule or a reader has changed.

A fix to triage or to a parser only ever applies to mail that arrives *next*. Everything already
in the store keeps the verdict it was given, because `seen_message_ids` exists precisely to stop
mail being processed twice — so after changing a rule the screen looks exactly as it did before,
and the change appears not to have worked.

This selects a set of emails, erases every trace of them (`state_db.forget_emails`, which also
un-releases delivery events keyed only to those emails) and rewinds the listing watermark far
enough that the next poll actually asks the server for them again. The next run then reads them
from Outlook as if for the first time.

Nothing is written to the mailbox. The mail is still in the Inbox because every run is
`read_only=True`, which is what makes this recoverable at all.

    python -m tools.reprocess_mail --rule rule_7_unknown --dry-run
    python -m tools.reprocess_mail --rule rule_7_unknown --yes
    python -m tools.reprocess_mail --all --yes
"""

import argparse
import sqlite3
import sys

from config import settings
from pipeline import state_db


def _select(conn, args) -> list:
    where, params = [], []
    if args.rule:
        where.append("matched_rule = ?")
        params.append(args.rule)
    if args.category:
        where.append("category = ?")
        params.append(args.category)
    if args.email:
        where.append("email_id = ?")
        params.append(args.email)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return conn.execute(
        f"SELECT email_id, subject, category, matched_rule, email_date FROM email_log{clause} "
        f"ORDER BY email_date", params).fetchall()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rule", help="only mail this triage rule matched, e.g. rule_7_unknown")
    parser.add_argument("--category", help="only mail in this category, e.g. route")
    parser.add_argument("--email", help="one internetMessageId")
    parser.add_argument("--all", action="store_true", help="every email in the store")
    parser.add_argument("--dry-run", action="store_true", help="list and change nothing")
    parser.add_argument("--yes", action="store_true", help="required to actually delete")
    args = parser.parse_args(argv)

    if not (args.rule or args.category or args.email or args.all):
        parser.error("choose what to reprocess: --rule, --category, --email or --all")

    conn = state_db.get_connection(settings.PIPELINE_STATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = _select(conn, args)
        if not rows:
            print("nothing matched")
            return 0

        print(f"{len(rows)} email(s) would be reprocessed:")
        for row in rows[:40]:
            print(f"  {(row['subject'] or row['email_id'])[:60]:60s} "
                  f"{row['category']:8s} {row['matched_rule']}")
        if len(rows) > 40:
            print(f"  ... and {len(rows) - 40} more")

        if args.dry_run or not args.yes:
            print("\nnothing changed. Add --yes to erase these and let the next run read them again.")
            return 0

        # Read before deleting: `email_date` is on the rows about to go, and the watermark has to
        # be rewound behind the oldest of them or the next poll's `$filter` will not list them.
        oldest = min((row["email_date"] or "") for row in rows)
        deleted = state_db.forget_emails(conn, [row["email_id"] for row in rows])
        if oldest:
            state_db.set_ingest_state(conn, state_db.WATERMARK_KEY, oldest)

        for table, count in deleted.items():
            if count:
                print(f"  cleared {count:4d} from {table}")
        print(f"\nwatermark rewound to {oldest or 'unchanged'}")
        print("Press “Run now” on /ui/automation, or run `python -m tools.ingest_mailbox`.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
