"""Clear `+quantity_conflict` markers that the purchase order shows were never a conflict.

The marker means *two extraction sources stated different quantities for what the grouper believed
was one PO line, and nothing chose between them*. It never consulted Spitfire. It is a substring
appended to `extracted_records.extraction_source` once at ingest, read back with
`LIKE '%quantity_conflict%'`, and **never recomputed** — so a marker written under a grouping rule
that has since been fixed stays on the record for ever, holding it off the Records page.

That is what happened. `_group_records` keyed on `(PO, spec_code)`, and a spec code is not a line
identity: PO 207514 carries 29 lines that all read `LOB-900-SI`, PO 207505 carries 30 reading
`RES-950-EQ`. Every delivered item on such a PO landed in one group holding a dozen different
quantities, and the reconciler — correctly refusing to guess — flagged all of them.
`_place_in_spec_group` fixed the grouping. Nothing went back for the markers it had already written.

**This asks the purchase order, which is the thing that actually knows.** For each flagged record it
runs `po_verify.verify_records` — the same function behind the Verify button, so this tool and that
screen cannot answer differently — and clears the marker only where the PO resolves a line and that
line's ordered quantity matches the record's.

Reaching the line is the whole difficulty, and `(PO, spec)` cannot do it: 29 lines share the spec.
The description resolves it, Spitfire's `"Stair Evacuation Plan Item: ST-7"` against the mail's
`"Stair Evacuation Plan"`, and `po_verify` already does this correctly. It is not reimplemented here.

**Positive evidence is required to clear, never the absence of a complaint.** Four separate ways a
record is left alone even though nothing said "mismatch":

- no line resolved — including a PO that could not be read at all, which produces no comparison
  rather than a favourable one;
- nothing decided the match — several lines tied on signals, the unit separated none of them (all
  29 are `EA`) and the leaders drew on description too, so the head of the list was taken. Record
  178 "Accesible Lift" draws with line 0030 "Handicap Lift Accesible" exactly this way. A quantity
  agreeing with a line nobody resolved is a coincidence, not a verification;
- two records on one delivery resolved to the *same* line. Live example: record 165
  ("Main Elevator Lobby Directory") fuzzy-matched line 9 ("Elevator Evacuation Plan"), which record
  166 matched exactly — PO 207514 has no directory line at all, so 165's agreement is spurious;
- the quantities genuinely differ, which is the marker doing its job.

Never deletes a record and never edits a quantity. The only column written is `extraction_source`
— the same string surgery `record_edit` performs when a reviewer settles a conflict by hand, so
both paths leave identical values behind. A full per-record report is written to `dev_reports`
whether or not anything was written, because a bulk mutation with no trace of what it decided and
why is not one anybody can check afterwards.

`--dry-run` decides and reports without touching `extracted_records`. It is not, however, free of
side effects: reading a purchase order refreshes the local mirror of it, which is `verify_records`
doing its job and is the same thing pressing Verify does. Said here because "dry run" usually
promises more than that.

    python -m tools.settle_quantity_conflicts [--dry-run]
"""

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from pipeline import po_verify, state_db
from pipeline.record_edit import CONFLICT_MARKER

REPORT_DIR = Path(__file__).resolve().parents[2] / "dev_reports"

CLEARED = "cleared"
KEPT = "kept"


def _cleared_source(source: str) -> str:
    """Strip the marker exactly as `record_edit.apply` does, so a record settled here and one
    settled by a reviewer are indistinguishable in the store."""
    return source.replace(f"+{CONFLICT_MARKER}", "").replace(
        CONFLICT_MARKER, "").strip("+ ") or "manual"


def _normalised_source(source: str) -> str:
    """One marker, however many times it was written.

    Records ingested before the append was guarded carry it up to four times —
    `ocr+quantity_conflict+quantity_conflict+quantity_conflict+quantity_conflict` — because
    `EvidenceCache` handed the same object to each of an email's delivery events. Every reader tests
    with `in`, so this changes no verdict anywhere; it is done on the records this tool *keeps*
    because leaving them is not a reason to leave them unreadable.
    """
    if source.count(CONFLICT_MARKER) <= 1:
        return source
    return f"{_cleared_source(source)}+{CONFLICT_MARKER}"


def _flagged(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT * FROM extracted_records "
        " WHERE extraction_source LIKE ? ORDER BY po_number, id",
        (f"%{CONFLICT_MARKER}%",)).fetchall()


def _claim_key(row, result):
    """What this record says it is a receipt against. `None` when it identified no line.

    Scoped to the delivery rather than to the purchase order: two separate deliveries receiving
    against one line is an ordinary part shipment, while two lines of *one* delivery claiming the
    same line means at least one of them matched something it is not.
    """
    if result.matched is None or result.matched.line_number is None:
        return None
    return (row["delivery_id"], row["po_number"], result.matched.line_number)


def _verdict(row, result, contested):
    """`(CLEARED|KEPT, reason)` for one record. The reason is printed either way."""
    check = result.matched
    qty = row["quantity_received"]

    if check is None:
        if not result.po_found:
            why = f"purchase order {row['po_number']} could not be read"
            if result.error:
                why += f" ({result.error})"
            return KEPT, why
        return KEPT, (f"no line on purchase order {row['po_number']} resolved for "
                      f"{row['spec_code'] or row['item_description'] or 'this record'}")

    line = f"line {check.line_number:04d}" if check.line_number is not None else "an unnumbered line"

    if result.ambiguous:
        return KEPT, (f"nothing separated {line} from the other lines that matched; it was taken "
                      f"off the top of the list, and agreeing with a line nobody resolved is not "
                      f"a verification")

    if _claim_key(row, result) in contested:
        return KEPT, (f"another record on this delivery also resolved to {line}, so at least one "
                      f"of the two matched a line it is not")

    if qty is None:
        return KEPT, "the email stated no quantity, so there is nothing the purchase order can confirm"

    if check.qty_agrees:
        return CLEARED, (f"{po_verify.fmt_qty(qty)} {check.record_uom or ''}".strip()
                         + f" agrees with {line} ({po_verify.fmt_qty(check.qty_ordered)} "
                           f"{check.unit_of_measure} ordered)")

    return KEPT, (f"the email says {po_verify.fmt_qty(qty)} {check.record_uom or ''}; {line} is "
                  f"{po_verify.fmt_qty(check.qty_ordered)} {check.unit_of_measure} ordered"
                  ).replace("  ", " ")


def _report(lines, dry_run: bool) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"quantity-conflicts-settled-{stamp}.md"
    header = [
        f"# Quantity conflicts settled against the purchase order — {stamp}",
        "",
        ("**Dry run — no record was changed.** (Reading the purchase orders did refresh the "
         "local mirror of them.)" if dry_run else
         "Markers shown as *cleared* were removed from `extraction_source`."),
        "",
        "| record | PO | spec | email qty | outcome | why |",
        "|---|---|---|---|---|---|",
    ]
    path.write_text("\n".join(header + lines) + "\n", encoding="utf-8")
    return path


def run(dry_run: bool = False) -> int:
    conn = state_db.get_connection()
    conn.row_factory = sqlite3.Row
    rows = _flagged(conn)
    if not rows:
        print("no record carries a quantity conflict marker")
        conn.close()
        return 0

    print(f"verifying {len(rows)} flagged record(s) against Spitfire "
          f"({len({r['po_number'] for r in rows})} purchase order(s))...\n")
    results = po_verify.verify_records(conn, rows)

    # Every line claimed more than once by one delivery. Built before any verdict, because whether
    # a record is contested depends on what its neighbours resolved to.
    claims = defaultdict(list)
    for row, result in zip(rows, results):
        key = _claim_key(row, result)
        if key is not None:
            claims[key].append(row["id"])
    contested = {key for key, ids in claims.items() if len(ids) > 1}

    report_lines = []
    cleared = kept = 0
    for row, result in zip(rows, results):
        outcome, reason = _verdict(row, result, contested)
        print(f"  {'clear ' if outcome is CLEARED else 'keep  '} #{row['id']:<4} "
              f"PO {row['po_number']:<8} {str(row['spec_code'] or '-'):<14} {reason}")
        qty = row["quantity_received"]
        report_lines.append(
            f"| {row['id']} | {row['po_number']} | {row['spec_code'] or '—'} | "
            f"{po_verify.fmt_qty(qty) if qty is not None else '—'} | "
            f"{outcome} | {reason} |")

        if outcome is CLEARED:
            cleared += 1
            if not dry_run:
                conn.execute(
                    "UPDATE extracted_records SET extraction_source = ?, updated_at = ? "
                    " WHERE id = ?",
                    (_cleared_source(row["extraction_source"] or ""),
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S"), row["id"]))
        else:
            kept += 1
            tidy = _normalised_source(row["extraction_source"] or "")
            if tidy != (row["extraction_source"] or "") and not dry_run:
                conn.execute(
                    "UPDATE extracted_records SET extraction_source = ? WHERE id = ?",
                    (tidy, row["id"]))

    if not dry_run:
        conn.commit()
    conn.close()

    path = _report(report_lines, dry_run)
    print(f"\nconsidered {len(rows)} flagged record(s)")
    print(f"  {'would clear' if dry_run else 'cleared':<14} {cleared}"
          f"{'  (dry run — no record changed)' if dry_run else ''}")
    print(f"  {'left flagged':<14} {kept}   <- a real conflict, or not verifiable")
    print(f"\nreport: {path}")
    return cleared


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    sys.exit(0 if run(parser.parse_args().dry_run) >= 0 else 1)
