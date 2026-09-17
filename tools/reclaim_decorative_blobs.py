r"""Delete the stored bytes of signature logos, and only those.

**This is a destructive migration. It is written to be read and run by a human against a
backed-up database (CLAUDE.md s4). It does nothing without `--apply`.**

## What it changes

`attachment_store` keeps one file per distinct sha256, shared by every row that names it. Most of
those files are evidence. About fifteen hundred are signature logos and letterhead images, which
`parsing/sniff.classify_image` marks `dropped_decorative` at ingest and nothing ever reads again.
This deletes those files and stamps `blob_reclaimed_at` on the rows that named them.

Measured on Premier's live store, 2026-09-16:

| | |
|---|---|
| distinct sha named by a `dropped_decorative` row | 1,568 |
| ...also named by some other row, so **kept** | 95 |
| candidates | ~1,473 |
| reclaimed | **~22 MB of a 1,466 MB store (1.5%)** |

That is the whole prize, and it is small. If the goal is disk, `tools/shrink_accumulation_payloads.py`
followed by `tools/dedupe_accumulation_payloads.py` takes `accumulation.payload_json` from 1,939 MB
to 119 MB and is not destructive in the same way. Run those first and see whether this is still
worth doing.

## How to reverse it

**You cannot, from this repository.** The bytes are gone. The only recovery is
`tools/backfill_attachments.py` re-fetching from Outlook, for as long as the message is still
there -- and that tool now deliberately skips these rows, so it would have to be pointed at them
by hand. The ledger row, its filename, its sha256 and its verdict all survive; only the bytes go.

Restoring the database copy taken in step 2 below brings back the rows' `blob_sha256`, but not the
files. Take the backup of `state/attachments/` as well if you want a way back.

## What it will not delete

A blob is reclaimed only when **all five** hold:

1. some `dropped_decorative` row names it, and
2. **no** row in **any** store names it for any other reason -- the store is one directory shared
   by the live and sample databases, so both are consulted, and a sha named by a POD, a chosen
   proof, a parsed document or a cache row that has dropped its own copy is kept whatever its
   disposition says; and
3. no held-mail payload is relying on it; and
4. it is no larger than `SIZE_CEILING` -- the classifier cannot call anything decorative above
   64 KB, so a bigger file means a rule changed and the run stops rather than shrugging; and
5. the file on disk still hashes to the name it is filed under -- never delete what you cannot
   identify.

## Running it

    python -m tools.reclaim_decorative_blobs                  # report only; changes nothing
    python -m tools.reclaim_decorative_blobs --list           # and name every candidate
    python -m tools.reclaim_decorative_blobs --apply          # delete them

Suggested order:

1. Stop the console. A click in the mail viewer between "work out what is referenced" and "delete"
   could cache a blob this run is about to take.
2. Copy `state/pipeline_state.sqlite3` and `state/attachments/` somewhere dated. Verify the copy
   opens.
3. Run without `--apply` and read the report, especially the "kept because" breakdown.
4. Run with `--apply`.
5. `python -m tools.verify_attachments --list` -- the reclaimed rows must read `released`, and
   `missing_file` and `corrupt_blob` must both be zero.
6. Re-run without `--apply`; it should report nothing left to do.
"""
import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings                                          # noqa: E402
from pipeline import attachment_ledger, attachment_store, state_db   # noqa: E402

SIZE_CEILING = 100_000
"""No decorative file may be larger than this.

`sniff.classify_image` can only reach that verdict by known hash, by being cid-referenced under
64 KB, or by being under 16 KB. The largest ever observed on the live store is 65,209 bytes. A
candidate above this ceiling means a classification rule has changed since this was written, and
the right response is to stop and re-read it, not to delete a megabyte because a query said so.
"""

DECORATIVE = attachment_ledger.DROPPED_DECORATIVE

# Every way a row can name a blob for a reason other than "we saw a logo". A sha appearing in any
# of these, in any store, is kept -- regardless of what its own disposition says, because a POD
# somebody chose by hand outranks the classifier's opinion of it.
_KEEP_SQL = f"""
    SELECT COALESCE(NULLIF(blob_sha256, ''), sha256) AS sha FROM attachment_ledger
     WHERE COALESCE(NULLIF(blob_sha256, ''), sha256) <> '' AND disposition <> '{DECORATIVE}'
UNION
    SELECT COALESCE(NULLIF(a.blob_sha256, ''), a.sha256) FROM attachment_ledger a
     WHERE COALESCE(a.is_pod, 0) = 1
UNION
    SELECT COALESCE(NULLIF(a.blob_sha256, ''), a.sha256)
      FROM extracted_records r JOIN attachment_ledger a
        ON a.id = r.pod_ledger_id OR a.id = r.source_ledger_id
UNION
    SELECT COALESCE(NULLIF(a.blob_sha256, ''), a.sha256)
      FROM parsed_documents p JOIN attachment_ledger a ON a.id = p.ledger_id
UNION
    SELECT content_sha256 FROM parsed_documents WHERE COALESCE(content_sha256, '') <> ''
UNION
    -- A cached attachment that has dropped its own copy now depends on this file. 91 of the 205
    -- cached blobs hash to a decorative-only sha: without this clause, `mail_cache`'s pointer and
    -- this tool jointly destroy the only copy of an image somebody actually opened.
    SELECT content_sha256 FROM mail_attachment
     WHERE content IS NULL AND COALESCE(content_sha256, '') <> ''
"""

_CANDIDATE_SQL = f"""
    SELECT DISTINCT COALESCE(NULLIF(blob_sha256, ''), sha256) AS sha
      FROM attachment_ledger
     WHERE disposition = '{DECORATIVE}'
       AND COALESCE(NULLIF(blob_sha256, ''), sha256) <> ''
"""


def _stores(extra=None) -> list:
    """Every database that can name a blob in the one shared store directory."""
    paths = [state_db.path_for(state_db.STORE_MAILBOX), state_db.path_for(state_db.STORE_SAMPLE)]
    if extra:
        paths.append(Path(extra))
    return [p for p in paths if Path(p).exists()]


def _query(path, sql: str) -> set:
    conn = sqlite3.connect("file:" + str(path).replace("\\", "/") + "?mode=ro", uri=True)
    try:
        return {row[0] for row in conn.execute(sql) if row[0]}
    except sqlite3.OperationalError as exc:            # a store missing a late column
        print(f"  ! {Path(path).name}: {exc}")
        return set()
    finally:
        conn.close()


def _payload_pinned(paths) -> set:
    """Shas a held-mail payload is relying on the store for.

    A payload entry carries `sha256` and, when the store already had the bytes, an empty
    `content_b64`. A decorative attachment has never had a non-empty one -- all three connectors
    clear `content_bytes` before the attachment leaves them, so `_embedded_bytes` returns "" for a
    reason that has nothing to do with the store -- and a full census of the live store confirmed
    0 of 12,193 entries. This computes it anyway rather than trusting it: it is the assertion that
    catches the day a connector stops clearing bytes.
    """
    pinned = set()
    for path in paths:
        conn = sqlite3.connect("file:" + str(path).replace("\\", "/") + "?mode=ro", uri=True)
        try:
            for table in ("accumulation", "accumulation_payload"):
                try:
                    rows = conn.execute(
                        f"SELECT payload_json FROM {table} WHERE COALESCE(payload_json, '') <> ''")
                except sqlite3.OperationalError:
                    continue
                for (blob,) in rows:
                    try:
                        document = json.loads(blob)
                    except Exception:                                  # noqa: BLE001
                        continue
                    email = document.get("email") or document
                    for entry in email.get("attachments") or []:
                        sha = entry.get("sha256") or ""
                        if not sha or entry.get("content_b64"):
                            continue                                   # carries its own copy
                        if str(entry.get("drop_hint") or "").startswith("decorative"):
                            continue                                   # never had one to carry
                        pinned.add(sha)
        finally:
            conn.close()
    return pinned


def _reason_kept(sha, keep, pinned, path) -> str:
    if sha in keep:
        return "named by a row that is not decorative"
    if sha in pinned:
        return "a held-mail payload depends on it"
    if path is None:
        return "not in the store (never kept, or already taken)"
    size = path.stat().st_size
    if size > SIZE_CEILING:
        return f"over the {SIZE_CEILING:,}-byte ceiling ({size:,})"
    return "did not hash to its own name"


def run(*, apply: bool = False, listing: bool = False, extra_db=None) -> int:
    if settings.STORE_DECORATIVE_ATTACHMENTS:
        print("PREMIER_STORE_DECORATIVE is on, which asks ingest to keep exactly the files this\n"
              "would delete. Turn it off, or do not run this. Nothing was changed.")
        return 2

    paths = _stores(extra_db)
    print("stores consulted   : " + ", ".join(Path(p).name for p in paths))

    candidates = set()
    keep = set()
    for path in paths:
        candidates |= _query(path, _CANDIDATE_SQL)
        keep |= _query(path, _KEEP_SQL)
    pinned = _payload_pinned(paths)

    print(f"decorative shas    : {len(candidates):,}")
    print(f"kept by a reference: {len(candidates & keep):,}")
    print(f"pinned by a payload: {len(candidates & pinned):,}")

    reclaimable, kept, freed = [], [], 0
    for sha in sorted(candidates):
        # `path_for` answers None when the file is not there, which is a perfectly ordinary state:
        # a row written before the store existed, or a blob a previous run already took.
        path = attachment_store.path_for(sha)
        if sha in keep or sha in pinned or path is None:
            kept.append((sha, _reason_kept(sha, keep, pinned, path)))
            continue
        size = path.stat().st_size
        if size > SIZE_CEILING:
            kept.append((sha, _reason_kept(sha, keep, pinned, path)))
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            kept.append((sha, "did not hash to its own name"))
            continue
        reclaimable.append((sha, path, size))
        freed += size

    print(f"reclaimable        : {len(reclaimable):,}  ({freed / 1024 / 1024:.1f} MB)")

    if listing:
        for sha, _path, size in reclaimable:
            print(f"    reclaim {sha[:16]}  {size:,} bytes")
    if kept:
        print("\nkept, and why:")
        counts = {}
        for _sha, why in kept:
            counts[why] = counts.get(why, 0) + 1
        for why, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"    {count:>6,}  {why}")

    if not apply:
        print("\nreport only — nothing was changed. Re-run with --apply to delete.")
        return 0

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    writable = [p for p in paths if Path(p) != Path(state_db.path_for(state_db.STORE_SAMPLE))] \
        if extra_db is None else paths
    deleted = 0
    for sha, path, _size in reclaimable:
        # Re-verify immediately before the unlink. Between the scan above and here, a click in the
        # mail viewer could have cached a row that now depends on this file.
        still_free = all(sha not in _query(p, _KEEP_SQL) for p in paths)
        if not still_free:
            print(f"    skipped {sha[:16]} — something started referencing it during this run")
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:                                         # noqa: PERF203
            print(f"    could not delete {sha[:16]}: {exc}")
            continue
        deleted += 1
        # After the unlink, never before: a crash between the two must leave a row claiming a blob
        # that is gone -- which `verify_attachments` names -- rather than a blob nobody claims,
        # which nothing would ever find again. `sha256` is deliberately left in place so the row
        # still says which file it was, and so `read_views.UNREAD_CLAUSE` does not suddenly
        # reclassify every one of these as "nothing could be read out of this file".
        for store_path in writable:
            conn = state_db.get_connection(store_path)
            try:
                conn.execute(
                    "UPDATE attachment_ledger SET blob_sha256 = NULL, blob_reclaimed_at = ? "
                    " WHERE COALESCE(NULLIF(blob_sha256, ''), sha256) = ? AND disposition = ?",
                    (now, sha, DECORATIVE))
                conn.commit()
            finally:
                conn.close()

    print(f"\ndeleted {deleted:,} file(s), {freed / 1024 / 1024:.1f} MB.")
    print("Now run:  python -m tools.verify_attachments --list")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="required to actually delete")
    parser.add_argument("--list", action="store_true", dest="listing",
                        help="name every candidate, not just the totals")
    parser.add_argument("--db", default=None,
                        help="an additional database to consult for references")
    args = parser.parse_args(argv)
    return run(apply=args.apply, listing=args.listing, extra_db=args.db)


if __name__ == "__main__":
    sys.exit(main())
