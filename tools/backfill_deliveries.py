"""Group records written before deliveries existed, and give them the keys the guards need.

Three columns arrived after most of this store was written, so on real data they are empty and the
code that depends on them is inert:

* `extracted_records.delivery_id` — which physical delivery an item line arrived on. Without it a
  purchase order that took one truck reads as six unrelated rows, each of which would create its
  own Spitfire receipt.
* `extracted_records.delivery_key` — the staging-time duplicate guard. `find_by_delivery_key`
  matches nothing when it is NULL, so the manual create form would happily accept a second copy of
  a record that already exists.

Reads nothing from the network and re-extracts nothing: every value is derived from columns the
rows already carry, plus the attachment ledger. Run `--dry-run` first; it prints exactly what would
change and writes nothing.

**Records already posted to Spitfire are grouped but never otherwise touched.** Their status and
every `spitfire_post` ledger row are left exactly as they are — history has to keep saying what was
actually sent, whatever shape we would send it in today.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import dedupe, deliveries_store, state_db   # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ref_for(conn: sqlite3.Connection, row: sqlite3.Row):
    """The delivery this row belongs to, by the same ladder Stage 2 uses on live mail.

    `dedupe.ref_for_email` reads rungs 3 and 4 off the attachment ledger, so a record whose proof
    of delivery was parsed at ingest is identified by that proof here too — the backfill and the
    pipeline cannot disagree about what one delivery is.
    """
    return dedupe.ref_for_email(
        conn, str(row["source_email_id"] or ""),
        shipment_number=row["shipment_number"],
        notification_number=row["notification_number"])


def plan(conn: sqlite3.Connection):
    """What would change. Groups rows by (purchase order, delivery ref) without writing."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM extracted_records WHERE delivery_id IS NULL ORDER BY id").fetchall()

    groups = defaultdict(list)
    for row in rows:
        po_number = str(row["po_number"] or "").strip()
        if not po_number:
            continue          # nothing to group on; left alone and reported
        ref, rung = _ref_for(conn, row)
        groups[(po_number, ref, rung)].append(row)

    missing_key = conn.execute(
        "SELECT COUNT(*) FROM extracted_records WHERE COALESCE(delivery_key,'') = ''"
    ).fetchone()[0]
    no_po = sum(1 for r in rows if not str(r["po_number"] or "").strip())
    return groups, rows, missing_key, no_po


def apply(conn: sqlite3.Connection, groups) -> dict:
    counts = {"deliveries": 0, "attached": 0, "keys": 0}
    for (po_number, ref, rung), members in groups.items():
        # The first member supplies the delivery-level facts; `upsert` fills only what is still
        # blank, so a later row stating a carrier the first one omitted still contributes it.
        delivery_id = deliveries_store.upsert(
            conn, po_number=po_number, delivery_ref=ref, delivery_rung=rung,
            now=_now(), facts=members[0])
        for member in members[1:]:
            deliveries_store.upsert(conn, po_number=po_number, delivery_ref=ref,
                                    delivery_rung=rung, now=_now(), facts=member)
        counts["deliveries"] += 1
        counts["attached"] += deliveries_store.attach(
            conn, delivery_id, [int(m["id"]) for m in members])

    conn.row_factory = sqlite3.Row
    for row in conn.execute(
            "SELECT * FROM extracted_records WHERE COALESCE(delivery_key,'') = ''").fetchall():
        key = dedupe.key_for_row(row)
        conn.execute("UPDATE extracted_records SET delivery_key = ? WHERE id = ?",
                     (key, int(row["id"])))
        counts["keys"] += 1
    conn.commit()
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(state_db.path_for("mailbox")))
    ap.add_argument("--dry-run", action="store_true", help="print what would change, write nothing")
    args = ap.parse_args()

    db_path = Path(args.db)
    conn = state_db.get_connection(db_path)
    groups, rows, missing_key, no_po = plan(conn)

    print(f"store: {db_path}")
    print(f"  item lines with no delivery : {len(rows)}")
    print(f"  they group into             : {len(groups)} deliveries")
    print(f"  rows with no delivery_key   : {missing_key}")
    if no_po:
        print(f"  rows with no PO (left alone): {no_po}")
    print()
    by_rung = defaultdict(int)
    for (_po, _ref, rung) in groups:
        by_rung[rung] += 1
    print("  identified by:", dict(by_rung) or "—")
    print()
    for (po_number, ref, _rung), members in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:10]:
        print(f"    PO {po_number:<8} {ref[:34]:<34} {len(members):>3} item line(s)")

    if args.dry_run:
        print("\ndry run — nothing written")
        return 0

    backup = db_path.with_name(db_path.name + f".bak-{datetime.now():%Y%m%d_%H%M%S}")
    shutil.copy2(db_path, backup)
    print(f"\nbackup: {backup.name}")
    counts = apply(conn, groups)
    print(f"created {counts['deliveries']} deliveries, attached {counts['attached']} item lines, "
          f"wrote {counts['keys']} delivery keys")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
