"""Stamp `extracted_records.source_ledger_id` on records staged before the column existed.

New records carry it from the adapter that read them. Old ones lost it, because the store used to
drop the field. Without it `spitfire_post._pod_for` rule 3 — attach the document a record was read
from — has nothing to attach.

The link is recovered, never guessed:

1. same email, and an attachment the matching adapter actually extracted from
   (`extraction_source` `pdf…` → `PdfAdapter`, `excel:…` → `ExcelAdapter`, and so on)
2. exactly one such attachment → that one
3. several → the one whose parsed text contains the record's `raw_snippet`, if exactly one does
4. otherwise left NULL. A wrong document attached to a receipt is worse than none, and a person can
   still choose the file on `/ui/deliveries/{id}/proof`.

Records read from the email body (`freetext`, `html`, `authority_*`, `manual`) are not touched.
Only writes `extracted_records.source_ledger_id`, and only where it is NULL.

    python -m tools.backfill_source_ledger            # dry run: counts only
    python -m tools.backfill_source_ledger --apply
"""

import argparse
import sqlite3
import sys
from collections import Counter, defaultdict

from pipeline import state_db

# Leading token of `extraction_source` (before ':' or '+') → the adapter that claims the file.
_ADAPTER = {
    "pdf": "PdfAdapter",
    "ocr": "OcrAdapter",
    "docx": "DocxAdapter",
    "excel": "ExcelAdapter",
    "text": "TextAdapter",
}


def _adapter_for(extraction_source: str) -> str:
    head = str(extraction_source or "").split("+", 1)[0].split(":", 1)[0].strip().lower()
    return _ADAPTER.get(head, "")


def _snippet_key(raw_snippet: str) -> str:
    return " ".join(str(raw_snippet or "").split())[:60]


def run(apply: bool = False) -> int:
    conn = state_db.get_connection()
    conn.row_factory = sqlite3.Row

    candidates = defaultdict(list)          # (email_id, adapter) -> [ledger id]
    for a in conn.execute(
            """SELECT id, email_id, claimed_by FROM attachment_ledger
                WHERE disposition = 'extracted' AND COALESCE(is_inline, 0) = 0
                  AND COALESCE(records_extracted, 0) > 0"""):
        candidates[(a["email_id"], a["claimed_by"])].append(a["id"])

    texts = {}                              # ledger id -> whitespace-normalised parsed text
    for p in conn.execute("SELECT ledger_id, raw_text, tables_json FROM parsed_documents"):
        texts[p["ledger_id"]] = " ".join(
            f"{p['raw_text'] or ''} {p['tables_json'] or ''}".split())

    outcome = Counter()
    updates = []
    for r in conn.execute(
            """SELECT id, source_email_id, extraction_source, raw_snippet FROM extracted_records
                WHERE source_ledger_id IS NULL"""):
        adapter = _adapter_for(r["extraction_source"])
        if not adapter:
            outcome["body or unknown source (skipped)"] += 1
            continue
        ids = candidates.get((r["source_email_id"], adapter), [])
        if len(ids) == 1:
            updates.append((ids[0], r["id"]))
            outcome["mapped: one document"] += 1
            continue
        if not ids:
            outcome["no matching document"] += 1
            continue
        key = _snippet_key(r["raw_snippet"])
        hits = [i for i in ids if key and key in texts.get(i, "")]
        if len(hits) == 1:
            updates.append((hits[0], r["id"]))
            outcome["mapped: snippet picked one"] += 1
        else:
            outcome["ambiguous (left NULL)"] += 1

    for name, n in sorted(outcome.items()):
        print(f"{n:6d}  {name}")
    print(f"{len(updates):6d}  total to write")

    if apply and updates:
        conn.executemany(
            "UPDATE extracted_records SET source_ledger_id = ? "
            " WHERE id = ? AND source_ledger_id IS NULL", updates)
        conn.commit()
        print("written")
    elif not apply:
        print("dry run — pass --apply to write")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the links (default: dry run)")
    return run(apply=parser.parse_args().apply)


if __name__ == "__main__":
    sys.exit(main())
