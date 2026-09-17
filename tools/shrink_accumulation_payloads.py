"""Drop the duplicated attachment bytes out of `accumulation.payload_json`, once.

**This is a migration. It is written to be read and run by a human against a backed-up database
(CLAUDE.md §4). It does nothing without `--apply`.**

## What it changes

`accumulation.payload_json` carried every attachment's bytes base64'd inline. Measured on the live
store on 2026-09-03: **1,870 MB of a 1,930 MB database** — 98% of the payload, median 921 KB a row,
43 MB at worst. Rows are keyed per (PO, shipment), so an email touching three POs embedded its
attachments three times: the largest 40 rows alone held 520 MB of copies of 242 MB of content, and
one 9.3 MB file appeared twelve times.

`pipeline/stage2_accumulate.py` no longer writes that field, reading the bytes back from
`attachment_store` instead. This rewrites the rows already on disk to match. Expect roughly
1.93 GB -> 60 MB, and the same reduction in every future `.bak` copy (`state/` was holding ~7.6 GB
of them).

## What it will not do

**A byte is only ever dropped once its replacement is confirmed present.** Each attachment's
`content_b64` is removed only where `attachment_store.exists(sha256)` is true and the stored blob
re-hashes to that same sha256. Anything else — no sha, a blob the store never took, a blob whose
content no longer matches — keeps its inline copy and is reported. Nothing outside
`accumulation.payload_json` is touched: no ledger row, no file in `state/attachments`, no other
table.

Idempotent: a second run finds nothing left to do.

## How to reverse it

Restore the database copy taken in step 1 below. The change is confined to one column of one
table, and the code reads the inline copy first where it still exists, so a restored database
works against either version of `stage2_accumulate`.

## Running it

    python -m tools.shrink_accumulation_payloads                  # report only; changes nothing
    python -m tools.shrink_accumulation_payloads --apply          # rewrite the rows
    python -m tools.shrink_accumulation_payloads --apply --vacuum # and reclaim the file space

`--vacuum` is separate because VACUUM rewrites the whole database and needs free disk space equal
to its size. Without it the rows shrink but the file does not.

Suggested order, all of it deliberate:

1. Stop the console, so nothing writes while this runs.
2. Copy `state/pipeline_state.sqlite3` to a dated `.bak` and confirm the copy's size.
3. Run without `--apply` and read the report.
4. Run with `--apply --vacuum`.
5. Re-run without `--apply`; it should report nothing left to change.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
from typing import Optional, Tuple

from config import settings
from pipeline import attachment_store, state_db


def _blob_is_trustworthy(sha256: str) -> bool:
    """True only if the store holds this content *and* it still hashes to its own name.

    The existence check alone would be enough for a cache. This is not a cache — it is the last
    remaining copy once the payload is rewritten — so the content is verified before its duplicate
    is discarded. A truncated or replaced blob is exactly the case where dropping the inline copy
    would lose an attachment for good, and it is cheap to rule out.
    """
    if not sha256:
        return False
    data = attachment_store.get(sha256)
    if data is None:
        return False
    return hashlib.sha256(data).hexdigest() == sha256


def _rewrite(payload: str) -> Tuple[Optional[str], int, int, int]:
    """Returns (new payload or None if unchanged, bytes freed, dropped count, kept count)."""
    try:
        document = json.loads(payload)
    except (ValueError, TypeError):
        return None, 0, 0, 0

    attachments = (document.get("email") or {}).get("attachments") or []
    dropped = kept = freed = 0

    for attachment in attachments:
        inline = attachment.get("content_b64") or ""
        if not inline:
            continue
        if _blob_is_trustworthy(attachment.get("sha256") or ""):
            attachment["content_b64"] = ""
            freed += len(inline)
            dropped += 1
        else:
            kept += 1

    if not dropped:
        return None, 0, 0, kept
    return json.dumps(document), freed, dropped, kept


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="write the rewritten rows; without it nothing is changed")
    parser.add_argument("--vacuum", action="store_true",
                        help="VACUUM afterwards to return the freed space to the filesystem")
    parser.add_argument("--limit", type=int, default=0, help="stop after this many rows")
    # So this can be rehearsed on a copy before it is run on the live store. It had no such flag,
    # while `tools/dedupe_accumulation_payloads.py` -- the migration this one is supposed to run
    # *before* -- has always had one, so the pair could not be practised together.
    parser.add_argument("--db", default=None,
                        help="database path (defaults to the live store)")
    args = parser.parse_args(argv)

    conn = state_db.get_connection(args.db or settings.PIPELINE_STATE_DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT rowid, LENGTH(payload_json) AS size FROM accumulation ORDER BY size DESC"
    ).fetchall()
    if args.limit:
        rows = rows[:args.limit]

    total_freed = total_dropped = total_kept = changed = 0

    for row in rows:
        payload = conn.execute(
            "SELECT payload_json FROM accumulation WHERE rowid = ?", (row["rowid"],)
        ).fetchone()["payload_json"]

        rewritten, freed, dropped, kept = _rewrite(payload)
        total_kept += kept
        if rewritten is None:
            continue

        changed += 1
        total_freed += freed
        total_dropped += dropped
        if args.apply:
            conn.execute("UPDATE accumulation SET payload_json = ? WHERE rowid = ?",
                         (rewritten, row["rowid"]))

    if args.apply:
        conn.commit()

    verb = "freed" if args.apply else "would free"
    print(f"rows examined      : {len(rows)}")
    print(f"rows to rewrite    : {changed}")
    print(f"attachments unlinked: {total_dropped}")
    print(f"payload {verb:<12}: {total_freed / 1e6:.0f} MB")
    if total_kept:
        print(f"\nkept inline (store cannot vouch for the bytes): {total_kept}")
        print("  These are NOT a failure — they are attachments whose only copy is still the one")
        print("  in the payload, so it was left there. Re-run after `tools.backfill_attachments`")
        print("  if you want them unlinked too.")

    if args.vacuum:
        if not args.apply:
            print("\n--vacuum ignored: nothing was written.")
        else:
            print("\nVACUUM (rewrites the whole file; this takes a while)...")
            conn.isolation_level = None
            conn.execute("VACUUM")
            print(f"done: {settings.PIPELINE_STATE_DB_PATH.stat().st_size / 1e9:.2f} GB")

    if not args.apply:
        print("\nNothing was changed. Re-run with --apply once a backup exists.")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
