"""Close post attempts that died mid-chain, settling each as whatever Spitfire actually holds.

A claim is written to `spitfire_post` *before* the first call to Spitfire, so that a receipt can
never exist without a row naming it. The cost is that a process killed between the claim and the
settle leaves the row at `CLAIMED` — and `CLAIMED` is in `BLOCKING`, so every route out is closed
at once: the Post button is not drawn, posting again answers "still in flight", that refusal cannot
even be recorded, `claim()` will not take the row over, and `post_report` and `verify_pod` both
report that nothing was posted. The record is wedged, and the only sign of it anywhere in the
product is the word "Posting…" in one table cell.

`post_ledger.stranded()` was written for exactly this, with a docstring saying the list "must be
empty between runs". It had no callers. This is the thing that calls it.

**Nothing is inferred from the ledger — the ledger is what is in doubt.** For each stranded row,
`spitfire_post.evidenced_state` reads the receipt back and settles it as what is actually on it:
`POSTED` if the report is there, `POD_POSTED` if the proof of delivery is there and its catalog
hash still matches our own bytes, `PARTIAL` if a receipt exists without it, `FAILED` if no receipt
was ever created.

**A claim that cannot be checked is left alone.** If Spitfire cannot be read, the row stays
`CLAIMED` and the reason is printed. That is the conservative direction on purpose: settling a
claim to `FAILED` on a guess would unblock a second post against a receipt that already exists,
and duplicate receipts in Premier's ERP are the failure this whole ledger exists to prevent.

Reads and settles only. It never posts, never creates a receipt, and never touches
`extracted_records`. Finishing a recovered `POD_POSTED` record is a separate, human step — the
"Post report" button, which the settle makes available again.

    python -m tools.settle_stranded_posts [--dry-run]
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from pipeline import post_ledger, spitfire_post, state_db

REPORT_DIR = Path(__file__).resolve().parents[2] / "dev_reports"


def _record_row(conn: sqlite3.Connection, record_id: int):
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM extracted_records WHERE id = ?", (record_id,)).fetchone()


def _report(lines, dry_run: bool) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"stranded-posts-settled-{stamp}.md"
    header = [
        f"# Stranded post attempts settled against Spitfire — {stamp}",
        "",
        ("**Dry run — no ledger row was changed.**" if dry_run else
         "Each row below was settled to the state Spitfire's own copy of the receipt supports."),
        "",
        "| record | PO | receipt | settled as | what Spitfire holds |",
        "|---|---|---|---|---|",
    ]
    path.write_text("\n".join(header + lines) + "\n", encoding="utf-8")
    return path


def run(dry_run: bool = False) -> int:
    conn = state_db.get_connection()
    conn.row_factory = sqlite3.Row
    stranded = post_ledger.stranded(conn)
    if not stranded:
        print("no stranded post attempts — every claim has been settled")
        conn.close()
        return 0

    print(f"reading {len(stranded)} stranded claim(s) back from Spitfire...\n")
    report_lines = []
    settled = unchecked = 0

    for attempt in stranded:
        row = _record_row(conn, attempt.record_id)
        if row is None:
            # The claim names a record that no longer exists. Not settleable from evidence about a
            # delivery nobody can look up, and not this tool's business to invent one.
            print(f"  skip   #{attempt.record_id:<5} PO {attempt.po_number:<9} "
                  f"the record it was claimed for is no longer in the store")
            unchecked += 1
            continue

        result = spitfire_post.evidenced_state(conn, attempt, row)
        label = attempt.receipt_doc_no or (attempt.receipt_key[:8] if attempt.receipt_key else "-")

        if result.state == post_ledger.CLAIMED:
            unchecked += 1
            print(f"  keep   #{attempt.record_id:<5} PO {attempt.po_number:<9} rcpt {label:<6} "
                  f"still unknown — Spitfire could not be read: {result.message}")
            report_lines.append(
                f"| {attempt.record_id} | {attempt.po_number} | {label} | left claimed | "
                f"could not be read: {result.message} |")
            continue

        settled += 1
        print(f"  settle #{attempt.record_id:<5} PO {attempt.po_number:<9} rcpt {label:<6} "
              f"-> {result.state}: {result.message}")
        for step in result.steps:
            print(f"           {step}")
        report_lines.append(
            f"| {attempt.record_id} | {attempt.po_number} | {label} | `{result.state}` | "
            f"{result.message} |")

        if not dry_run:
            post_ledger.settle(conn, attempt.idempotency_key, result.state, result.message)

    conn.close()
    path = _report(report_lines, dry_run)
    print(f"\nconsidered {len(stranded)} stranded claim(s)")
    print(f"  {'would settle' if dry_run else 'settled':<15} {settled}"
          f"{'  (dry run — nothing written)' if dry_run else ''}")
    print(f"  {'left claimed':<15} {unchecked}   <- still blocking, on purpose")
    print(f"\nreport: {path}")
    return settled


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report without settling anything")
    sys.exit(0 if run(parser.parse_args().dry_run) >= 0 else 1)
