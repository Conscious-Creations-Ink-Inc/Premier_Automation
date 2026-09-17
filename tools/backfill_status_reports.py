"""Flag the spreadsheets already in the store as status reports, using bytes we already hold.

`grid_reader` now keeps what `receipt.classify_grid` decides about every tabular grid it reads, and
the dispatcher writes it onto `attachment_ledger.is_status_report`. That only helps mail that
arrives *after* the change. This is the same judgement applied to the spreadsheets already here.

    python -m tools.backfill_status_reports --dry-run     # what it would flag, and why
    python -m tools.backfill_status_reports --yes         # write it

**Free, and offline.** Bytes come from `attachment_store` by the digest the ledger already holds —
never Graph, never Azure, never the mailbox. A workbook is opened, its sheets are read, and the
verdict is the one `pipeline/parsing/receipt.py` would reach on the same header.

**It flags; it never sets mail aside.** Writing `is_status_report` puts a chip on the queue and a
sentence on the row. Whether a message is a delivery stays a person's decision, recorded in
`mail_overrides` by whoever presses the button — the two are deliberately different questions, for
the same reason `mail_overrides.CONFIRMED` is not folded into `DELIVERY`.

A file whose workbook holds a genuine `delivery_document` sheet is never flagged, however many
trackers sit beside it. See `attachment_ledger.status_report_verdict`.
"""

import argparse
import io
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone

from config import settings
from pipeline import attachment_ledger, attachment_store, state_db
from pipeline.stage3_extract import grid_reader

SPREADSHEET_SUFFIXES = (".xlsx", ".xlsm", ".xls")


def _verdicts_for(data: bytes, filename: str):
    """`(sheet_label, kind, reason)` per sheet, exactly as `ExcelAdapter` would produce them.

    Deliberately not a second implementation of the reader: the sheet is turned into rows the same
    way, the header is found by `grid_reader.find_header_row`, and the judgement is
    `receipt.classify_grid`. A private copy here would drift from the live path and this tool would
    quietly start disagreeing with ingest.
    """
    verdicts = []
    if filename.lower().endswith(".xls"):
        import xlrd
        book = xlrd.open_workbook(file_contents=data)
        try:
            for sheet in book.sheets():
                rows = [[str(sheet.cell(r, c).value) for c in range(sheet.ncols)]
                        for r in range(sheet.nrows)]
                verdicts.extend(_judge(rows, f"xls:{sheet.name}"))
        finally:
            book.release_resources()
        return verdicts

    import openpyxl
    workbook = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    try:
        for sheet in workbook.worksheets:
            rows = [grid_reader.as_strings(row) for row in sheet.iter_rows(values_only=True)]
            verdicts.extend(_judge(rows, f"excel:{sheet.title}"))
    finally:
        workbook.close()
    return verdicts


def _judge(rows, label):
    from pipeline.parsing import receipt
    if not rows:
        return []
    index = grid_reader.find_header_row(rows)
    if index is None:
        return []
    kind, reason = receipt.classify_grid(list(rows[index]))
    return [(label, kind, reason)]


def _bytes_for(row) -> bytes:
    for column in ("blob_sha256", "sha256"):
        digest = row[column]
        if digest:
            content = attachment_store.get(digest)
            if content:
                return content
    return b""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be flagged and change nothing (the default)")
    parser.add_argument("--yes", action="store_true", help="required to actually write")
    parser.add_argument("--limit", type=int, default=0, help="stop after this many attachments")
    args = parser.parse_args()

    writing = args.yes and not args.dry_run
    db_path = settings.PIPELINE_STATE_DB_PATH
    conn = state_db.get_connection(db_path)
    conn.row_factory = sqlite3.Row

    like = " OR ".join("lower(filename) LIKE ?" for _ in SPREADSHEET_SUFFIXES)
    rows = conn.execute(
        f"SELECT id, email_id, filename, sha256, blob_sha256 FROM attachment_ledger "
        f"WHERE {like} ORDER BY id",
        tuple(f"%{suffix}" for suffix in SPREADSHEET_SUFFIXES),
    ).fetchall()
    if args.limit:
        rows = rows[:args.limit]

    print(f"database   {db_path}")
    print(f"mode       {'WRITING' if writing else 'dry run — nothing will be written'}")
    print(f"candidates {len(rows)} spreadsheet attachment(s)\n")

    flagged, skipped, unreadable, missing = [], [], [], []
    for row in rows:
        data = _bytes_for(row)
        if not data:
            missing.append(row)
            continue
        try:
            verdicts = _verdicts_for(data, row["filename"] or "")
        except Exception as error:                                     # noqa: BLE001
            unreadable.append((row, type(error).__name__))
            continue
        reason = attachment_ledger.status_report_verdict(verdicts)
        (flagged if reason else skipped).append((row, reason, verdicts))

    for row, reason, _verdicts in flagged:
        print(f"  FLAG  {(row['filename'] or '')[:62]:<62}")
        print(f"        {reason[:110]}")

    kept = [r for r, _reason, verdicts in skipped
            if any(k == "delivery_document" for _l, k, _x in verdicts)]
    if kept:
        print("\n  left alone — these workbooks record a receipt:")
        for row in kept:
            print(f"        {(row['filename'] or '')[:70]}")

    print(f"\nflagged    {len(flagged)} attachment(s) on "
          f"{len({r['email_id'] for r, _x, _y in flagged})} message(s)")
    print(f"unflagged  {len(skipped)}   (of which {len(kept)} record a receipt)")
    if unreadable:
        print(f"unreadable {len(unreadable)}   {Counter(n for _r, n in unreadable).most_common()}")
    if missing:
        print(f"no bytes   {len(missing)}")

    if not writing:
        print("\nNothing was written. Re-run with --yes to apply.")
        return 0

    backup = db_path.with_name(f"{db_path.name}.bak-{datetime.now():%Y%m%d_%H%M%S}")
    shutil.copy2(db_path, backup)
    print(f"\nbackup     {backup.name}")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    for row, reason, _verdicts in flagged:
        conn.execute(
            "UPDATE attachment_ledger SET is_status_report = 1, status_report_reason = ?, "
            "last_updated_at = ? WHERE id = ?",
            (reason, now, row["id"]),
        )
    conn.commit()
    print(f"written    {len(flagged)} row(s) flagged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
