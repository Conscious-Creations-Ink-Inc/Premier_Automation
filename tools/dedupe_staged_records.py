"""Retire the duplicate records staged by the stale-`delivery_key` bug, and repair the keys.

Background. `stage_records` used to mint a record's `delivery_key` *before*
`_fallback_delivery_date` filled in a missing `pod_stated_date` — a field the key hashes. The row
was then written carrying a key that no longer described it, so `dedupe.find_by_delivery_key`
could never match it again. On its own that is only a stale key. It became duplicate *records*
because `evidence_cache.records_for` is keyed on the email rather than the attachment, so one
`ExtractedRecord` instance reached that loop once per attachment on the message: the first pass
mutated the date and minted a key from the blank one, the second pass saw the mutated object,
minted a different key, and staged a second row. Both fixes are in
`pipeline/ingest_orchestrator.py`.

This repairs what the bug already wrote. Measured on Premier's live store 2026-09-15: 159 duplicate
groups, 160 extra records, 43 purchase orders — every one of them a line that then collided with
its own twin in `spitfire_post._refuse_line_collisions` and could never post. PO 212696 and 212685
could post nothing at all.

**How a duplicate is proved, without reading the source document.** Members of a group are compared
column by column. They must differ in *nothing* except `id`, `pod_source`, `delivery_key` and the
timestamps — and the pattern must be the bug's own signature: the lowest-id row carries
`pod_source = 'email_received_date'` (the pass that ran the fallback) and every later row carries
`pod_source IS NULL` (the pass that found the date already set). That signature cannot be produced
any other way. Two *distinct* records with identical content would both have entered the loop with
a blank date, both would have minted the same key, and the second would have been skipped — so a
second row on disk with a null `pod_source` can only be the same object written twice.

Anything that does not match is left alone and reported, including a group whose members genuinely
differ. On the live store that is one group; it is a real collision and needs a person.

What is written, and how to reverse it:

  * `extracted_records.delivery_key` is recomputed from each pending row's own content. The
    previous value is printed for every row changed, and a key is derived data — nothing reads it
    but the duplicate guard, which wants the recomputed one.
  * `extracted_records.status` moves `'pending'` -> `'superseded'` on the retired copies only.
    Reverse with `UPDATE extracted_records SET status = 'pending' WHERE id IN (...)`; the ids are
    printed at the end of the run. Nothing is deleted.

`superseded` is chosen because it already exists, already drops out of `read_views._READY_CLAUSE`
(which requires `'pending'`) and out of `dedupe.BLOCKING_STATUSES`, so the surviving copy still
blocks a re-staging of the same delivery.

Run the code fix first. Against an unfixed pipeline the next ingest simply re-creates these.

    python -m tools.dedupe_staged_records                    # dry run, writes nothing
    python -m tools.dedupe_staged_records --apply --i-understand-this-writes
"""

import argparse
import sqlite3
import sys
from typing import Dict, List, Sequence

from pipeline import dedupe, state_db

# Columns two writes of one object may legitimately differ in. Anything else differing means the
# rows are not the same record, and this tool must not touch them.
_MAY_DIFFER = {"id", "pod_source", "delivery_key", "created_at", "updated_at"}

# The natural identity of a staged line — what "the same row of the same document" means.
_IDENTITY = ("source_email_id", "source_ledger_id", "po_number", "spec_code",
             "item_description", "quantity_received", "po_line_number")

_FALLBACK = "email_received_date"


def _groups(conn: sqlite3.Connection) -> List[List[sqlite3.Row]]:
    """Pending records sharing one natural identity, oldest first within each group."""
    cols = ", ".join(_IDENTITY)
    rows = conn.execute(
        "SELECT GROUP_CONCAT(id) AS ids FROM extracted_records "
        " WHERE status = 'pending' AND source_ledger_id IS NOT NULL "
        f" GROUP BY {cols} HAVING COUNT(*) > 1"
    ).fetchall()
    groups = []
    for row in rows:
        ids = sorted(int(x) for x in str(row["ids"]).split(","))
        groups.append([conn.execute("SELECT * FROM extracted_records WHERE id = ?",
                                    (i,)).fetchone() for i in ids])
    return groups


def _why_not(group: Sequence[sqlite3.Row], columns: Sequence[str]) -> str:
    """Empty when this group is provably the bug, else the reason it is being left alone."""
    differing = sorted({c for c in columns if len({r[c] for r in group}) > 1} - _MAY_DIFFER)
    if differing:
        return f"members differ in {', '.join(differing)} — not the same record"
    if group[0]["pod_source"] != _FALLBACK:
        return (f"the oldest row's pod_source is {group[0]['pod_source']!r}, not "
                f"{_FALLBACK!r} — the fallback did not mint its key, so this is not the "
                "bug's signature")
    later = [r["pod_source"] for r in group[1:]]
    if any(source is not None for source in later):
        return f"later rows carry pod_source {later!r}, expected all NULL"
    return ""


def run(apply: bool = False) -> int:
    conn = state_db.get_connection()
    conn.row_factory = sqlite3.Row
    columns = [c[1] for c in conn.execute("PRAGMA table_info(extracted_records)")]

    # 1. Repair every stale key first, so the duplicate guard matches on content from now on.
    repaired = 0
    pending = conn.execute("SELECT * FROM extracted_records WHERE status = 'pending'").fetchall()
    for row in pending:
        fresh = dedupe.key_for_row(row)
        if fresh == row["delivery_key"]:
            continue
        repaired += 1
        print(f"  key   #{row['id']:<7} {str(row['delivery_key'])[:16]} -> {fresh[:16]}"
              f"  (pod_source={row['pod_source']})")
        if apply:
            conn.execute("UPDATE extracted_records SET delivery_key = ? WHERE id = ?",
                         (fresh, row["id"]))

    # 2. Decide each duplicate group on its own evidence.
    retired: List[int] = []
    left: List[str] = []
    by_po: Dict[str, int] = {}
    for group in _groups(conn):
        ids = [r["id"] for r in group]
        reason = _why_not(group, columns)
        if reason:
            left.append(f"  LEFT  {ids}  PO {group[0]['po_number']}: {reason}")
            continue
        keep, extras = group[0], group[1:]
        po_number = str(keep["po_number"])
        by_po[po_number] = by_po.get(po_number, 0) + len(extras)
        print(f"  dupe  PO {po_number:<9} keep #{keep['id']:<7} "
              f"retire {[r['id'] for r in extras]}  "
              f"{str(keep['spec_code'])[:22]:<24} qty={keep['quantity_received']}")
        for extra in extras:
            retired.append(extra["id"])
            if apply:
                conn.execute("UPDATE extracted_records SET status = 'superseded' WHERE id = ?",
                             (extra["id"],))

    if apply:
        conn.commit()
    conn.close()

    note = "" if apply else "   (dry run — nothing written)"
    print(f"\nstale delivery_key repaired   {repaired}{note}")
    print(f"duplicate records retired     {len(retired)}{note}")
    if by_po:
        print("  by purchase order: " + ", ".join(
            f"{po} ({n})" for po, n in sorted(by_po.items(), key=lambda kv: -kv[1])))
    print(f"groups left for a person      {len(left)}")
    for line in left:
        print(line)
    if retired:
        print("\nto reverse the retirement:")
        print("  UPDATE extracted_records SET status = 'pending' WHERE id IN "
              f"({', '.join(str(i) for i in retired)});")
    return len(retired)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument("--i-understand-this-writes", action="store_true",
                        help="required alongside --apply; typed in full or nothing is written")
    args = parser.parse_args()
    if args.apply and not args.i_understand_this_writes:
        print("refusing: --apply needs --i-understand-this-writes as well")
        sys.exit(2)
    run(apply=args.apply)
