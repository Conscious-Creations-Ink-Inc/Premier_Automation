"""Give a delivery document's second purchase order the records it already proves.

A receiving report may cover several purchase orders, printing each line's order in its own `PO #`
column. Until 2026-09-17 `stage3_extract/base.py` blanked any such cell naming an order the local
Spitfire mirror had never been pulled, and `ingest_orchestrator` then stamped the event's order onto
the blanked line. The measured case: ten lines, seven belonging to one order and three to another,
all ten recorded against the second — and the first received **nothing**.

That is fixed for mail arriving from now on. This repairs what is already in the store, and one
thing has to happen before `tools/replay_parse.py` can do it:

    keys = _delivery_keys_for(conn, email_id)      # the email's accumulation rows
    if not po_number or po_number not in keys:
        continue                                   # ...every line of the missing order drops here

Stage 2 loops the triage hints, the hints come from the purchase orders the *records* named, and the
blanking bug kept the second order out of them. So the message accumulated one order, and a replay
alone has nowhere to put the other one's lines. This seeds the delivery Stage 2 would have created —
sharing the `delivery_ref` the message already has, which is what makes it one shipment with one
delivery per order — and then hands the staging to `replay_parse._write` so the records are written
by exactly the code the live path uses.

    python -m tools.repair_multi_po_documents --dry-run      # what would change, writes nothing
    python -m tools.repair_multi_po_documents --email '<id@mail.dll>' --dry-run
    python -m tools.repair_multi_po_documents --yes          # for real, after a backup

**Never run automatically.** CLAUDE.md §4: a backfill is written, explained and run by a person
against a backed-up database. `--yes` takes its own copy first regardless.

What it will not touch, borrowed whole from `replay_parse`: any message where somebody built a
record by hand, waived a proof of delivery or chose which file the proof is, and any record already
pushed to Spitfire. Those outrank any rule change, this one included.
"""

import argparse
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from typing import Dict, List, Set

from pipeline import evidence as evidence_mod
from pipeline import state_db
from tools import replay_parse

REPAIR_REASON = "backfilled by tools/repair_multi_po_documents — the document names this PO"


def _documents_pos(conn: sqlite3.Connection, email_id: str, known_pos) -> Dict[str, Set[str]]:
    """Every purchase order this message's stored parses name, under today's rules.

    Keyed by ledger id so the report can say which document named what — a person deciding whether
    to accept a backfill wants the file's name, not just a number.
    """
    found: Dict[str, Set[str]] = {}
    parses = conn.execute(
        "SELECT ledger_id, email_id, filename, container_path, adapter, raw_text, tables_json "
        "  FROM parsed_documents WHERE email_id = ? ORDER BY ledger_id", (email_id,)).fetchall()
    for parse in parses:
        try:
            _source, records = replay_parse._replay_one(parse, known_pos, "")
        except Exception as e:                                      # noqa: BLE001
            print(f"    ! {parse['filename']}: {type(e).__name__}: {e}")
            continue
        pos = {r.po_number for r in records if r.po_number}
        if pos:
            found[f"{parse['ledger_id']}:{parse['filename']}"] = pos
    return found


def _corroborated_missing(named: Dict[str, Set[str]], keys: Set[str]) -> Set[str]:
    """Purchase orders worth seeding: named by a document that also names one we already have.

    The same rule `base.po_column_is_corroborated` applies inside a grid, applied here to a whole
    document. A receiving report that names the order this message already accumulated **and**
    another one is describing a two-order shipment; that second order is real and its lines are
    being dropped for want of a delivery.

    A document that names only orders we have never seen is not evidence of anything. `COTA 211798
    RR#3.pdf` is the case that forced this: its `Customer PO #` band holds `453295`, which is the
    **bill of lading** (`Bill Of Lading Number: 4532957` further down the same page), while the
    order it is actually against — 213993, printed twice elsewhere — already has its delivery.
    Seeding a delivery for a BOL would invent a purchase order that does not exist, which is the
    exact failure the per-cell mirror check was written to prevent.
    """
    worth = set()
    for pos in named.values():
        if pos & keys:                      # this document names something we already believe
            worth |= pos - keys
    return worth


def _seed_delivery(conn: sqlite3.Connection, email_id: str, po_number: str, now: str) -> bool:
    """The accumulation and release rows Stage 2 would have written for this purchase order.

    Copied from the row the message already has, so the new delivery carries the *same*
    `delivery_ref` — `deliveries` is keyed `UNIQUE(po_number, delivery_ref)`, so one shipment
    becomes one delivery per purchase order rather than two unrelated deliveries. Only the order
    number changes.

    `release_reason` says a repair did this. A backfilled row that reads like something Stage 2
    decided is a row nobody can audit later.
    """
    template = conn.execute(
        "SELECT * FROM accumulation WHERE email_id = ? ORDER BY rowid LIMIT 1", (email_id,)
    ).fetchone()
    if template is None:
        return False

    conn.execute(
        "INSERT OR IGNORE INTO accumulation "
        "(po_number, shipment_number, email_id, notification_type, category, received_at, "
        " payload_json, delivery_ref, delivery_rung) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (po_number, template["shipment_number"], email_id, template["notification_type"],
         template["category"], now, "", template["delivery_ref"], template["delivery_rung"]))
    conn.execute(
        "INSERT OR IGNORE INTO released_events "
        "(po_number, shipment_number, released_at, release_reason, delivery_ref, delivery_rung) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (po_number, template["shipment_number"], now, REPAIR_REASON,
         template["delivery_ref"], template["delivery_rung"]))
    return True


def _candidates(conn: sqlite3.Connection, email_id: str = "", limit: int = 0) -> List[str]:
    """Messages holding a stored parse, newest first — the same population `replay_parse` works on."""
    return replay_parse._replayable_emails(conn, email_id, limit)


def run(*, dry_run: bool = True, email_id: str = "", limit: int = 0, db_path=None) -> int:
    db_path = db_path or state_db.path_for("mailbox")
    conn = state_db.get_connection(db_path)
    conn.row_factory = sqlite3.Row

    emails = _candidates(conn, email_id, limit)
    print(f"selected   {len(emails)} message(s) with a stored parse")
    known_pos = evidence_mod._known_po_numbers(conn)
    print(f"known POs  {len(known_pos or ())} from the mirror\n")

    affected = []
    for email in emails:
        keys = replay_parse._delivery_keys_for(conn, email)
        if not keys:
            continue                       # never accumulated: routed, or still on hold
        named = _documents_pos(conn, email, known_pos)
        missing = sorted(_corroborated_missing(named, set(keys)))
        if not missing:
            continue
        reason = replay_parse._protected(conn, email)
        affected.append((email, keys, named, missing, reason))

    if not affected:
        print("no message names a purchase order it has no delivery for. nothing to do.")
        return 0

    print(f"{len(affected)} message(s) name a purchase order with no delivery of its own:\n")
    for email, keys, named, missing, reason in affected:
        print(f"  {email}")
        for where, pos in named.items():
            print(f"      {where[:66]}  names {sorted(pos)}")
        print(f"      has deliveries for {sorted(keys)}; missing {missing}")
        if reason:
            print(f"      SKIPPED — {reason}")
    print()

    if dry_run:
        print("nothing was written. Add --yes to seed these deliveries and re-stage the records.")
        return 0

    backup = db_path.with_name(f"{db_path.name}.bak-repairpo-{datetime.now():%Y%m%d_%H%M%S}")
    shutil.copy2(db_path, backup)
    print(f"backup     {backup.name}\n")

    now = datetime.now().isoformat()
    seeded = staged_total = 0
    per_po: Counter = Counter()
    for email, _keys, _named, missing, reason in affected:
        if reason:
            continue
        for po_number in missing:
            if _seed_delivery(conn, email, po_number, now):
                seeded += 1
                per_po[po_number] += 1

        # Re-derive and stage through the live path's own code. `_write` deletes only the records
        # it re-derives, keeps anything a person made, and leaves posted records alone.
        parses = conn.execute(
            "SELECT ledger_id, email_id, filename, container_path, adapter, raw_text, tables_json "
            "  FROM parsed_documents WHERE email_id = ? ORDER BY ledger_id", (email,)).fetchall()
        email_date = replay_parse._email_date_for(conn, email)
        produced = []
        covered = set()
        for parse in parses:
            try:
                _source, records = replay_parse._replay_one(parse, known_pos, email_date)
            except Exception as e:                                  # noqa: BLE001
                print(f"  ! {email} / {parse['filename']}: {type(e).__name__}: {e}")
                continue
            produced.extend(records)
            covered.update(replay_parse.family_of(r.extraction_source) for r in records)
        staged = replay_parse._write(conn, email, produced, covered, now)
        staged_total += staged
        print(f"  {email}  seeded {missing} — {staged} record(s) staged")
    conn.commit()

    print(f"\ndeliveries seeded    {seeded}  ({', '.join(f'{po}×{n}' for po, n in per_po.items())})")
    print(f"records staged       {staged_total}")
    print(f"backup kept at       {backup}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", default="", help="one internetMessageId")
    parser.add_argument("--limit", type=int, default=0, help="stop after this many messages")
    parser.add_argument("--db", default="", help="a copy of the store, for rehearsing the repair")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change and write nothing")
    parser.add_argument("--yes", action="store_true", help="required to actually write")
    args = parser.parse_args()

    if not args.yes and not args.dry_run:
        print("Refusing to write without --yes. Showing a dry run instead.\n")
    from pathlib import Path
    return run(dry_run=not args.yes, email_id=args.email, limit=args.limit,
               db_path=Path(args.db) if args.db else None)


if __name__ == "__main__":
    sys.exit(main())
