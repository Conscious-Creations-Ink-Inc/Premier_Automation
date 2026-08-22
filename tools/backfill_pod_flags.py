"""Stamp `is_pod` on attachments that were ingested before the column existed.

New mail needs none of this — `stage3_extract` records the verdict as it reads each attachment,
whatever the file type. This is only for rows already in the store, so the Records page and
`spitfire_post._pod_for` see the same answer for old mail as for new.

**Anything needing OCR is skipped** — photographs, and .docx, whose text this project reaches only
by OCR'ing the images embedded in it. OCR is a paid call per image; the ingest pass pays it once on
the way in, and a backfill has no business spending it again across the whole history. Those are
decided the next time their mail is reprocessed. PDFs carry their own text layer and are free to
read, so they are decided here.

Read-only against Spitfire and the mailbox; the only writes are `is_pod`, `pod_po_numbers`,
`pod_delivery_date` and `pod_signed_by` on `attachment_ledger`.

    python -m tools.backfill_pod_flags [--dry-run]
"""

import argparse
import sqlite3
import sys

from pipeline import attachment_bytes, state_db
from pipeline.parsing import pod as pod_parser

# The only type whose text can be read locally, for nothing. Deliberately not a statement about
# what a POD may be — a POD may be any file at all — only about what can be decided here without
# spending money on OCR.
_FREE_TO_READ = ("pdf",)


def _text(kind: str, content: bytes) -> str:
    if kind == "pdf":
        from pipeline.stage3_extract.pdf_adapter import _page_texts
        return "\n".join(_page_texts(content))
    return ""


def run(dry_run: bool = False) -> int:
    conn = state_db.get_connection()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, email_id, ordinal, filename, sniffed_kind
             FROM attachment_ledger
            WHERE COALESCE(is_pod, 0) = 0 AND COALESCE(is_inline, 0) = 0
            ORDER BY id"""
    ).fetchall()

    stamped = skipped_image = unreadable = not_a_pod = 0
    for row in rows:
        kind = row["sniffed_kind"]
        if kind in ("image", "docx"):
            skipped_image += 1
            continue
        if kind not in _FREE_TO_READ:
            not_a_pod += 1
            continue
        resolved = attachment_bytes.resolve(conn, row["email_id"], row["ordinal"],
                                            row["filename"] or "")
        if not (resolved and resolved.content):
            unreadable += 1
            continue
        try:
            document = pod_parser.parse_pod(_text(kind, resolved.content))
        except Exception:                                          # noqa: BLE001
            unreadable += 1
            continue
        if document is None:
            not_a_pod += 1
            continue

        stamped += 1
        print(f"  POD  {row['filename'][:52]:<52} PO(s)={document.po_numbers or '-'} "
              f"date={document.delivery_date or '-'} by={document.signed_for_by or '-'}")
        if not dry_run:
            conn.execute(
                """UPDATE attachment_ledger
                      SET is_pod = 1, pod_po_numbers = ?, pod_delivery_date = ?, pod_signed_by = ?
                    WHERE id = ?""",
                (",".join(document.po_numbers or []), document.delivery_date,
                 document.signed_for_by, row["id"]))
    if not dry_run:
        conn.commit()
    conn.close()

    print(f"\nconsidered {len(rows)} unstamped attachment(s)")
    print(f"  stamped as POD        {stamped}{'  (dry run — nothing written)' if dry_run else ''}")
    print(f"  not a POD             {not_a_pod}")
    print(f"  need OCR, skipped     {skipped_image}   <- decided on the next ingest of their mail")
    print(f"  bytes unreadable      {unreadable}")
    return stamped


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    sys.exit(0 if run(parser.parse_args().dry_run) >= 0 else 1)
