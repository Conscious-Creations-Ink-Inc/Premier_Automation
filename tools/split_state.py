"""One-time migration: pull the .msg corpus out of the live store into its own database.

Until now both `tools/ingest_corpus.py` and `tools/ingest_mailbox.py` wrote
`state/pipeline_state.sqlite3`, so it holds Premier's real mail and 14 sample emails in the same
tables. Every read view is an unfiltered whole-table scan, which is why the console reported "14
need a person" when 12 of those were live-mailbox rows and 2 were samples.

This splits them: the live store keeps only what came from Graph, and a new
`state/sample_state.sqlite3` keeps only the corpus.

    python -m tools.split_state              # dry run — prints the plan, changes nothing
    python -m tools.split_state --apply

**Premier's mailbox is never re-read.** The corpus email ids are derived locally by re-opening
the .msg folder — the same stable ids `--reset` already uses — and everything not in that set is
live by definition. `seen_message_ids` for the live mail is therefore never in a delete list,
which matters: that table is the only thing standing between us and re-downloading 12 messages
and their attachments from Graph.

Deliberately *not* discriminating by `processed_at` date or by id shape. Both would work on
today's data by luck (one ingest each, on different days) and neither is a property of the data —
both stores' ids are ordinary `…@…prod.outlook.com` Message-IDs.

Copy-then-prune rather than re-running the corpus: it is symmetric, uses one already-tested
primitive (`state_db.forget_emails`, which handles `released_events`' missing email column) in
both directions, and preserves the sample rows exactly as they were produced.
"""

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

from config import settings
from pipeline import state_db

TABLES = ("seen_message_ids", "email_log", "extracted_records",
          "accumulation", "released_events", "attachment_ledger")


def corpus_email_ids() -> set:
    """The ids of the 14 sample messages, read from the .msg files themselves."""
    from connectors.msg_file import MsgFileMailbox
    from tools.ingest_corpus import DEFAULT_CORPUS

    folder = Path(DEFAULT_CORPUS)
    if not folder.exists():
        raise SystemExit(
            f"corpus folder not found: {folder}\n"
            "Without it the sample ids cannot be derived, and guessing them would risk deleting "
            "live mail. Nothing was changed."
        )
    # read_only is not optional: this is Premier's own sample data, in their own folder.
    mailbox = MsgFileMailbox(folder, read_only=True)
    return {email.email_id for email in mailbox.fetch_new()}


def all_email_ids(conn) -> set:
    """Union of both id-bearing tables. An id can be marked seen without ever reaching the log —
    a run that died mid-email — and missing those would strand them in the wrong store."""
    ids = {r[0] for r in conn.execute("SELECT email_id FROM email_log")}
    ids |= {r[0] for r in conn.execute("SELECT email_id FROM seen_message_ids")}
    return ids


def counts(conn) -> dict:
    out = {}
    for table in TABLES:
        try:
            out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except Exception:                                      # noqa: BLE001
            out[table] = 0
    return out


def _print_counts(label: str, values: dict) -> None:
    print(f"  {label}")
    for table in TABLES:
        print(f"    {table:<20} {values.get(table, 0)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="actually perform the split. Without it nothing is written.")
    args = parser.parse_args()

    live_path = Path(settings.PIPELINE_STATE_DB_PATH)
    sample_path = Path(settings.SAMPLE_STATE_DB_PATH)

    if not live_path.exists():
        print(f"nothing to split: {live_path} does not exist.")
        return 0
    if sample_path.exists():
        # Existence alone is not evidence the split has run: `get_connection` creates the file
        # and its schema the first time anything opens it, so merely starting the console or the
        # test suite leaves an empty one behind. Refuse on *rows*, which only a real run or a
        # previous split can produce.
        probe = state_db.get_connection(sample_path)
        try:
            existing = sum(counts(probe).values())
        finally:
            probe.close()
        if existing:
            print(f"refusing to run: {sample_path} already holds {existing} row(s).\n"
                  "The split has been done, or a corpus run has written there. Delete that file "
                  "first if you genuinely want to redo it.")
            return 1
        sample_path.unlink()   # empty shell; the copy below replaces it wholesale

    corpus_ids = corpus_email_ids()
    conn = state_db.get_connection(live_path)
    try:
        before = counts(conn)
        present = all_email_ids(conn)
    finally:
        conn.close()

    sample_ids = present & corpus_ids
    live_ids = present - corpus_ids
    missing = corpus_ids - present   # in the folder but never ingested; nothing to move

    print(f"live store   {live_path}")
    print(f"sample store {sample_path}   (will be created)")
    print()
    _print_counts("current row counts", before)
    print()
    print(f"  emails in the store        {len(present)}")
    print(f"  -> stay live               {len(live_ids)}")
    print(f"  -> move to sample          {len(sample_ids)}")
    if missing:
        print(f"  corpus files never ingested {len(missing)} (ignored)")
    print()

    if not sample_ids:
        print("no corpus rows found in the live store — nothing to move.")
        return 0
    if not args.apply:
        print("DRY RUN. Nothing was changed. Re-run with --apply to perform the split.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = live_path.with_name(f"{live_path.name}.bak-{stamp}")
    shutil.copy2(live_path, backup)
    print(f"backed up to {backup}")

    shutil.copy2(live_path, sample_path)     # both files now hold everything
    print(f"created {sample_path}")

    sample_conn = state_db.get_connection(sample_path)
    live_conn = state_db.get_connection(live_path)
    try:
        # Each store forgets what does not belong to it. Note the *complementary* id sets:
        # nothing is deleted from both, so no row can be lost by this pair of calls.
        state_db.forget_emails(sample_conn, sorted(live_ids))
        state_db.forget_emails(live_conn, sorted(sample_ids))
        after_live = counts(live_conn)
        after_sample = counts(sample_conn)
    finally:
        sample_conn.close()
        live_conn.close()

    print()
    _print_counts("live store, after", after_live)
    print()
    _print_counts("sample store, after", after_sample)
    print()
    for table in TABLES:
        total = after_live[table] + after_sample[table]
        flag = "" if total == before[table] else f"   <-- MISMATCH, was {before[table]}"
        print(f"  {table:<20} {after_live[table]} + {after_sample[table]} = {total}{flag}")
    print("\nDone. Premier's mailbox was not contacted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
