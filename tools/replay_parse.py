"""Re-derive records from parses we already have on disk, and show what changes.

Every attachment the pipeline has read is stored whole in `parsed_documents` — the text it held and
the tables it held, keyed on its `attachment_ledger` row. That is enough to run today's extraction
rules over yesterday's reads **without touching the mailbox and without paying for OCR a second
time**, which is the difference between "we could see what the new rules do" and "we can see what
the new rules do".

    python -m tools.replay_parse --email '<id@mail.dll>'          # one message, changes nothing
    python -m tools.replay_parse --dry-run                        # the whole backlog, as a diff
    python -m tools.replay_parse --yes                            # write it

**Free, always.** It reads `parsed_documents`, never the original bytes, so a scanned receiving
report costs nothing to replay. Of the record-producing attachments in the live store, 775 of 861
have a stored parse and every one of them is a delivery document — spreadsheets store no parse, so
the scope this tool works on is exactly the one it should.

**It is not `tools/reextract.py`.** That tool re-reads *files* whose extraction failed on something
external, and it will call Azure. This one re-reads *parses* whose extraction succeeded and whose
rules have since changed. Neither deletes anything.

What it will not touch, borrowed whole from `reextract._protected`: any message where a person has
built a record by hand, waived a proof of delivery, or chosen which file the proof is. Their work
outranks any rule change.
"""

import argparse
import json
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional

from config import settings
from pipeline import evidence as evidence_mod
from pipeline import extracted_records_store, state_db
from pipeline.models import ExtractedRecord
from pipeline.stage3_extract.base import ExtractionSource
from pipeline.stage3_extract.ocr_adapter import OcrResult, records_from_ocr_result
from tools.reextract import _delivery_keys_for, _email_date_for, _pod_ledger_ids_for, _protected

# Which adapter's stored parse becomes which `extraction_source`, and whether its tables came from
# a reader or from a text layer. The second half matters: a native table read is worth full
# confidence and an OCR read is not, and `records_from_ocr_result` caps by default.
_ADAPTERS = {
    "PdfAdapter":   ("pdf", None),
    "HtmlAdapter":  ("html", None),
    "DocxAdapter":  ("docx", None),
    "TextAdapter":  ("text", None),
    "ExcelAdapter": ("excel", None),
    "OcrAdapter":   ("ocr", "ocr"),
}


def _replay_one(row: sqlite3.Row, known_pos, email_date: str):
    """Today's rules over one stored parse. Returns `(source, records)`."""
    extraction_source, cap_kind = _ADAPTERS.get(row["adapter"], ("ocr", "ocr"))
    source = ExtractionSource(
        source_email_id=row["email_id"],
        email_date=email_date,
        source_type="attachment",
        filename=row["filename"] or "",
        ledger_id=row["ledger_id"],
        container_path=row["container_path"] or "",
        known_po_numbers=known_pos,
    )
    result = OcrResult(
        tables=json.loads(row["tables_json"] or "[]"),
        raw_text=row["raw_text"] or "",
        confidence=0.8,
    )
    records = records_from_ocr_result(
        source, result, extraction_source=extraction_source,
        # Lifted for a text-layer parse: those tables are what the document says, not what a reader
        # made of a picture of it, and capping them would quietly downgrade every clean PDF.
        confidence_cap=settings.AZURE_DOC_INTELLIGENCE_CONFIDENCE_CAP if cap_kind == "ocr" else None,
    )
    return source, records


def family_of(extraction_source: str) -> str:
    """The adapter family a record came from — `pdf:text+quantity_conflict` -> `pdf`.

    What a replay may replace is exactly what it re-derived. Deleting a message's records wholesale
    and re-staging would drop any source this tool does not reproduce, so the families are compared
    rather than the whole string.
    """
    return (extraction_source or "").split("+")[0].split(":")[0]


def _replay_body(conn: sqlite3.Connection, email_id: str, known_pos, email_date: str,
                 had_freetext: bool) -> List[ExtractedRecord]:
    """The message body, re-read, but only where a text-only pass is what read it the first time.

    `parsed_documents` holds attachments; a body is in `mail_body`. 194 records in the live store
    come from a text pass over a body and would not be created under today's rules, so leaving
    bodies out would leave the largest class of them untouched.

    **Gated on the message already having freetext records, deliberately.** In the live cascade a
    body goes to `HtmlAdapter` first and only falls through to the text pass when that finds
    nothing. Running the text pass unconditionally here would invent records on every message whose
    body was read by the grid reader — which is not a replay, it is a second, worse extraction.
    """
    if not had_freetext:
        return []
    from pipeline.stage3_extract.freetext_adapter import FreetextAdapter

    body = conn.execute(
        "SELECT COALESCE(body_text, '') , COALESCE(body_html, '') FROM mail_body WHERE email_id = ?",
        (email_id,)).fetchone()
    if not body or not (body[0] or body[1]):
        return []
    return FreetextAdapter().extract(ExtractionSource(
        source_email_id=email_id, email_date=email_date, source_type="body",
        body_text=body[0] or None, body_html=body[1] or None, known_po_numbers=known_pos,
    ))


def _existing(conn: sqlite3.Connection, email_id: str) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT id, spec_code, quantity_received, item_description, pod_stated_date, "
        "       extraction_source, status "
        "  FROM extracted_records WHERE source_email_id = ? AND origin = 'auto' ORDER BY id",
        (email_id,)).fetchall()


def _describe(record) -> str:
    spec = record["spec_code"] if isinstance(record, sqlite3.Row) else record.spec_code
    qty = record["quantity_received"] if isinstance(record, sqlite3.Row) else record.quantity_received
    desc = record["item_description"] if isinstance(record, sqlite3.Row) else record.item_description
    date = record["pod_stated_date"] if isinstance(record, sqlite3.Row) else record.pod_stated_date
    return f"spec={spec!r} qty={qty!r} desc={(desc or '')[:26]!r} date={date!r}"


def _replayable_emails(conn: sqlite3.Connection, email_id: str = "", limit: int = 0) -> List[str]:
    """Messages this tool can re-derive: one with a stored attachment parse, or one whose records
    came from a text pass over its body — `mail_body` is the stored input for those.

    Both halves are needed. 75 messages carry no attachment parse at all and hold 194 text-derived
    records between them, which is the largest single class the new rules reject.
    """
    sql = ("SELECT email_id FROM parsed_documents WHERE email_id <> '' "
           " UNION "
           "SELECT r.source_email_id FROM extracted_records r "
           "  JOIN mail_body b ON b.email_id = r.source_email_id "
           " WHERE r.origin = 'auto' AND r.extraction_source LIKE 'freetext%'")
    rows = [r[0] for r in conn.execute(sql).fetchall()]
    if email_id:
        rows = [r for r in rows if r == email_id]
    rows.sort()
    return rows[:limit] if limit else rows


def run(*, dry_run: bool = True, email_id: str = "", limit: int = 0, verbose: bool = False,
        db_path=None) -> int:
    db_path = db_path or state_db.path_for("mailbox")
    conn = state_db.get_connection(db_path)
    conn.row_factory = sqlite3.Row

    emails = _replayable_emails(conn, email_id, limit)
    print(f"selected   {len(emails)} message(s) that can be re-derived from stored input")
    if not emails:
        print("nothing to do.")
        return 0

    if not dry_run:
        backup = db_path.with_name(f"{db_path.name}.bak-replay-{datetime.now():%Y%m%d_%H%M%S}")
        shutil.copy2(db_path, backup)
        print(f"backup     {backup.name}")

    known_pos = evidence_mod._known_po_numbers(conn)
    print(f"known POs  {len(known_pos or ())} from the mirror\n")

    before_total = after_total = superseded_total = 0
    protected = staged_total = 0
    changed_emails = 0
    reasons: Counter = Counter()
    now = datetime.now().isoformat()

    for email in emails:
        reason = _protected(conn, email)
        if reason:
            protected += 1
            if verbose:
                print(f"  skipped {email} — {reason}")
            continue

        parses = conn.execute(
            "SELECT ledger_id, email_id, filename, container_path, adapter, raw_text, tables_json "
            "  FROM parsed_documents WHERE email_id = ? ORDER BY ledger_id", (email,)).fetchall()
        email_date = _email_date_for(conn, email)

        reads: List[evidence_mod._DocumentRead] = []
        produced: List[ExtractedRecord] = []
        for parse in parses:
            try:
                source, records = _replay_one(parse, known_pos, email_date)
            except Exception as e:                                  # noqa: BLE001
                # One unreadable parse must not cost the message the rest of its attachments —
                # the same rule `evidence.gather` follows for one unreadable attachment.
                print(f"  ! {parse['filename']!r}: {type(e).__name__}: {e}")
                continue
            produced.extend(records)
            reads.append(evidence_mod._DocumentRead(
                ledger_id=parse["ledger_id"], filename=parse["filename"] or "",
                key=evidence_mod._document_key(source),
                fidelity=evidence_mod._fidelity_of(records), records=records))

        superseded_total += evidence_mod.supersede_duplicate_documents(reads)

        before = _existing(conn, email)
        had_freetext = any(family_of(r["extraction_source"]) == "freetext" for r in before)
        produced.extend(_replay_body(conn, email, known_pos, email_date, had_freetext))
        # What this replay re-derived, and so what it is entitled to replace.
        covered = {_ADAPTERS.get(p["adapter"], ("ocr", None))[0] for p in parses}
        if had_freetext:
            covered.add("freetext")

        before_total += len(before)
        after_total += len(produced)
        for record in produced:
            reasons[record.extraction_source] += 1

        if len(before) != len(produced) or verbose:
            changed_emails += 1
            subject = conn.execute(
                "SELECT COALESCE(subject, '') FROM email_log WHERE email_id = ?", (email,)).fetchone()
            print(f"  {email}")
            print(f"    {(subject[0] if subject else '')[:78]!r}")
            print(f"    before {len(before):>3} record(s), after {len(produced):>3}")
            if verbose:
                for row in before:
                    print(f"       - #{row['id']} {_describe(row)} [{row['extraction_source']}]")
                for record in produced:
                    mark = "SUPERSEDED" if record.superseded_by_ledger_id else "         "
                    print(f"       + {mark} {_describe(record)} [{record.extraction_source}]")

        if not dry_run:
            staged_total += _write(conn, email, produced, covered, now)

    print()
    print(f"messages replayed      {len(emails) - protected}")
    print(f"skipped, human work    {protected}")
    print(f"messages that change   {changed_emails}")
    print(f"records before         {before_total}")
    print(f"records after          {after_total}   "
          f"({after_total - before_total:+d}, of which {superseded_total} superseded)")

    # What this tool cannot reach, said plainly. `mail_body` is a cache, not an archive — it holds
    # 79 of the thousands of messages processed — so a record read from a body whose text was never
    # stored has no local input to re-derive it from. Those need the mail fetching again, which is
    # `tools/reprocess_mail.py`, which deletes far more than this does. Naming the number here is
    # the difference between "the backlog is clean" and "the backlog is clean except for these".
    stranded = conn.execute(
        """SELECT COUNT(*) FROM extracted_records r
            WHERE r.origin = 'auto' AND r.status IN ('pending', 'failed')
              AND r.extraction_source LIKE 'freetext%'
              AND NOT EXISTS (SELECT 1 FROM mail_body b WHERE b.email_id = r.source_email_id)"""
    ).fetchone()[0]
    if stranded:
        print(f"out of reach           {stranded}   <- read from a message body that was never "
              f"stored; re-fetching the mail is the only way to re-derive them")
    print("by source:")
    for name, count in reasons.most_common():
        print(f"   {count:>5}  {name}")

    if dry_run:
        print("\nnothing was written. Add --yes to replace these records for real.")
    else:
        print(f"\nrecords staged         {staged_total}")
        conn.commit()
    conn.close()
    return 0


def _write(conn: sqlite3.Connection, email_id: str, records: List[ExtractedRecord],
           covered, now: str) -> int:
    """Replace this message's replayed records with the ones today's rules produce.

    Deliberately a replacement, not an addition. A replay is a **correction** of what the same
    document already said — `parsed_documents.save` treats a re-read the same way, "a correction,
    not a second opinion" — and appending instead would leave every message holding both the record
    a person is complaining about and its fixed twin.

    `covered` is what makes that safe: only records from a source this replay actually re-derived
    are removed. A message whose body was read by the confirmation-grid reader keeps those records
    untouched, because nothing here re-read them and deleting what you cannot rebuild is not a
    correction.

    Only `origin = 'auto'` rows are in scope, and only for a message `_protected` has already
    cleared, so nothing a person made or chose is touched. Records already sent to Spitfire stay
    exactly where they are: whatever the rules say now, that receipt exists.
    """
    from pipeline.ingest_orchestrator import stage_records

    doomed = [row[0] for row in conn.execute(
        "SELECT id, extraction_source FROM extracted_records "
        " WHERE source_email_id = ? AND origin = 'auto' AND status <> 'pushed_to_spitfire'",
        (email_id,)).fetchall() if family_of(row[1]) in covered]
    conn.executemany("DELETE FROM extracted_records WHERE id = ?", [(i,) for i in doomed])

    keys = _delivery_keys_for(conn, email_id)
    if not keys:
        # The message never accumulated — routed to a person, or still on hold. There is no
        # delivery to hang a record on, and inventing one here would be a second opinion about
        # something Stage 2 already decided.
        return 0

    pod_ids = _pod_ledger_ids_for(conn, email_id)
    only_po = next(iter(keys)) if len(keys) == 1 else None

    by_po: Dict[str, List[ExtractedRecord]] = {}
    for record in records:
        po_number = record.po_number or only_po
        if not po_number or po_number not in keys:
            continue
        record.po_number = po_number
        by_po.setdefault(po_number, []).append(record)

    staged = 0
    for po_number, group in by_po.items():
        delivery_ref, delivery_rung = keys[po_number]
        staged += len(stage_records(
            conn, group, po_number=po_number, delivery_ref=delivery_ref,
            delivery_rung=delivery_rung, now=now,
            pod_ledger_id_of=lambda r: pod_ids.get(r.po_number)))
    return staged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", default="", help="one internetMessageId")
    parser.add_argument("--limit", type=int, default=0, help="stop after this many messages")
    parser.add_argument("--verbose", action="store_true",
                        help="print every record before and after, not just the counts")
    parser.add_argument("--dry-run", action="store_true",
                        help="change nothing. This tool makes no external calls either way, so a "
                             "dry run here really is free")
    parser.add_argument("--yes", action="store_true", help="required to actually write")
    args = parser.parse_args()

    if not args.yes and not args.dry_run:
        print("Refusing to write without --yes. Showing a dry run instead.\n")
    return run(dry_run=args.dry_run or not args.yes, email_id=args.email,
               limit=args.limit, verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
