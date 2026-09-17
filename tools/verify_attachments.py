"""Prove the attachment store actually holds what the ledger says it holds.

"Keep everything" is only worth saying if it can be checked, and a store that silently lost a
POD looks exactly like one that never had it. This walks every ledger row and answers three
questions per row: is there a blob recorded, is the file there, and do its bytes still hash to
their own name.

Read-only. It opens the database, reads files, and writes nothing anywhere.

    python -m tools.verify_attachments
    python -m tools.verify_attachments --source sample
"""

import argparse
import hashlib
import sys
from collections import Counter

from pipeline import attachment_store, state_db

OK = "ok"
NOT_STORED = "not_stored"            # no blob recorded — ingested before the store, or a backfill gap
MISSING_FILE = "missing_file"        # the ledger names a blob that is not on disk
CORRUPT_BLOB = "corrupt_blob"        # the file is there and its bytes do not match its name
NO_BYTES = "no_bytes"                # nothing to store: a cloud link, or a zero-byte attachment
RELEASED = "released"                # the bytes were dropped on purpose, and this says by whom


def check_row(row) -> str:
    if not row["sha256"] or not row["size_bytes"]:
        return NO_BYTES
    # Released on purpose is not the same fact as never stored, and neither is the same fact as
    # lost. `tools/reclaim_decorative_blobs.py` stamps `blob_reclaimed_at` as it deletes, so this
    # can tell them apart -- without it, the first run of that tool makes this one report ~23,900
    # `missing_file` rows and advise a backfill that would undo the whole thing.
    if _column(row, "blob_reclaimed_at"):
        return RELEASED
    blob = row["blob_sha256"]
    if not blob:
        return NOT_STORED
    data = attachment_store.get(blob)
    if data is None:
        return MISSING_FILE
    # The name is the checksum, so verification is just a re-read. Cheap insurance against a
    # half-written file or a disk that lied about a flush.
    return OK if hashlib.sha256(data).hexdigest() == blob else CORRUPT_BLOB


def _column(row, name: str):
    """A column that may predate this build. `sqlite3.Row` raises IndexError for an unknown key,
    and a verifier must not be the thing that crashes on an older database."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=state_db.STORE_MAILBOX,
                        choices=[state_db.STORE_MAILBOX, state_db.STORE_SAMPLE])
    parser.add_argument("--list", action="store_true",
                        help="name every row that is not ok, not just the totals")
    args = parser.parse_args(argv)

    conn = state_db.get_connection(state_db.path_for(args.source))
    conn.row_factory = __import__("sqlite3").Row
    try:
        rows = conn.execute(
            "SELECT id, email_id, filename, sha256, size_bytes, blob_sha256, disposition, "
            "       blob_reclaimed_at "
            "  FROM attachment_ledger ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    verdicts = Counter()
    problems = []
    for row in rows:
        verdict = check_row(row)
        verdicts[verdict] += 1
        if verdict not in (OK, NO_BYTES, RELEASED):
            problems.append((verdict, row))

    print(f"{len(rows)} ledger row(s) in {args.source}")
    for verdict in (OK, NO_BYTES, RELEASED, NOT_STORED, MISSING_FILE, CORRUPT_BLOB):
        if verdicts[verdict]:
            print(f"  {verdict:14s} {verdicts[verdict]}")

    if args.list:
        for verdict, row in problems:
            print(f"    [{verdict}] #{row['id']} {row['filename'][:60]} "
                  f"({row['size_bytes']} bytes, {row['disposition']})")

    # A missing or corrupt blob is a real loss and fails the check. `not_stored` is reported but
    # does not fail on its own: rows written before the store existed are expected until
    # `tools.backfill_attachments` has been run, and failing on them would make this useless as a
    # routine check.
    lost = verdicts[MISSING_FILE] + verdicts[CORRUPT_BLOB]
    if lost:
        print(f"\n{lost} attachment(s) the ledger claims we kept and we do not have.")
        return 1
    if verdicts[NOT_STORED]:
        print(f"\n{verdicts[NOT_STORED]} row(s) predate the store — "
              f"run `python -m tools.backfill_attachments` while the mail is still in Outlook.")
    else:
        print("\nEvery attachment with bytes is stored and verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
