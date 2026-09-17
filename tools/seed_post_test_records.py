"""Seed three marked records so the Post button can be driven by hand, then remove them again.

**Why this is needed at all.** Nothing in the live store can be posted today: 27 of the 29 records
on `/ui/records` are missing `received_by`, because they come from an Excel tracker whose own
comments say Premier is still chasing it ("Checking with John for receipt"). The gate that stops
them is correct — `completeness.py` requires proof a person took delivery — so the answer is not
to weaken it but to add records that genuinely satisfy it and are obviously ours.

Each seeded record carries a different attachment kind, because "can we upload a POD" has three
answers and they are not the same code path:

    A  .pdf   the real FedEx POD bytes already in the store
    B  .png   an image POD, which is what a phone-photographed delivery note is
    C  .doc   legacy Word — refused outright until 2026-08-17, and the reason this file exists

They point at **PO 912559**, chosen because it is genuinely receivable: project PRJ001PB100003,
lines 1/2/4 ordering 4/4/2 Set with nothing received, cost code 102011215. Records 191-194 already
sit on those lines needing nothing but a receiver, so the seeds are those rows made complete
rather than fiction.

**Posting these creates real receipts on Premier's training instance.** They are titled
`CC-TEST … DO NOT PROCESS`, left In Process, and never routed. `--remove` clears the seeded
records; it cannot clear the receipts, which stay on training alongside the earlier probe
artefacts.

    python tools/seed_post_test_records.py            # seed
    python tools/seed_post_test_records.py --remove   # undo
    python tools/seed_post_test_records.py --status   # what is seeded, and what it posted
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import state_db  # noqa: E402

MARKER = "cc-test:seed"
"""`extraction_source` for every seeded row. One greppable string is what makes `--remove` exact
rather than a guess at which records were ours."""

PO = "912559"
PROJECT = "PRJ001PB100003"

# (label, PO line, quantity, spec, description, filename, kind, content-type)
# Quantities match the mirrored order exactly: the decision gate's tolerance is 0.0, so a seed
# that ordered 4 and received 3 would flag rather than post and prove nothing about the chain.
SEEDS = (
    ("A", 1, 4.0, "BRR-803-AC", "Amenity Tray at Ballroom Restrooms",
     "CC-TEST.POD.FedEx.pdf", "pdf", "application/pdf"),
    ("B", 2, 4.0, "FRR-803-AC", "Amenity Tray at Front Restrooms",
     "CC-TEST.POD.signed.png", "image", "image/png"),
    ("C", 4, 2.0, "PRR-803-AC", "Amenity Tray at Pool Restrooms",
     "CC-TEST.POD.delivery-note.doc", "doc", "application/msword"),
)

# A one-page PDF, a 1x1 PNG and an OLE2 header. Real bytes rather than placeholders because the
# upload is hash-verified against the server's own MD5 — `b"fake"` would prove the plumbing and
# not the format.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478"
    "9c6360000002000100fffe4fdb0000000049454e44ae426082")
_DOC_HEADER = bytes.fromhex("d0cf11e0a1b11ae1") + b"\x00" * 504


def _pdf_bytes(conn: sqlite3.Connection) -> bytes:
    """The real FedEx POD already in the store, or a minimal PDF if it has been cleared.

    Preferred because it is a genuine multi-kilobyte scanned document — the case the upload has to
    survive — rather than something contrived to be small.
    """
    from pipeline import attachment_store
    row = conn.execute(
        """SELECT blob_sha256, sha256 FROM attachment_ledger
            WHERE filename LIKE '%FedEx POD%' AND sniffed_kind = 'pdf' LIMIT 1""").fetchone()
    if row:
        for digest in (row[0], row[1]):
            if digest:
                content = attachment_store.get(digest)
                if content:
                    return content
    return (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
            b"trailer<</Root 1 0 R>>\n%%EOF\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def seed(conn: sqlite3.Connection) -> int:
    existing = conn.execute("SELECT COUNT(*) FROM extracted_records WHERE extraction_source = ?",
                            (MARKER,)).fetchone()[0]
    if existing:
        print(f"{existing} seeded record(s) already present — run --remove first")
        return 0

    line = conn.execute("SELECT COUNT(*) FROM spitfire_po_lines WHERE po_number = ?",
                        (PO,)).fetchone()[0]
    if not line:
        # Without the mirror the decision cannot find the project or the cost code, and the post
        # would flag for a reason that has nothing to do with what is being tested.
        print(f"PO {PO} is not in the local mirror. Open it once on /ui/records and press Verify, "
              f"then run this again.")
        return 0

    pdf = _pdf_bytes(conn)
    payloads = {"pdf": pdf, "image": _PNG, "doc": _DOC_HEADER}
    now = _now()
    made = 0

    for label, po_line, qty, spec, description, filename, kind, content_type in SEEDS:
        email_id = f"cc-test-seed-{label}@consciouscreations.ai"
        content = payloads[kind]

        conn.execute(
            """INSERT OR REPLACE INTO mail_body
               (email_id, subject, sender, received_at, body_html, body_text, source, cached_at)
               VALUES (?, ?, ?, ?, NULL, ?, 'seed', ?)""",
            (email_id, f"CC-TEST {label} — delivery for PO {PO} line {po_line}",
             "cc-test@consciouscreations.ai", now,
             f"Seeded by tools/seed_post_test_records.py to exercise the Post button. "
             f"Not a real delivery. Remove with --remove.", now))

        conn.execute(
            """INSERT OR REPLACE INTO mail_attachment
               (email_id, ordinal, filename, content_type, kind, size_bytes, content_id,
                is_inline, content)
               VALUES (?, 0, ?, ?, ?, ?, NULL, 0, ?)""",
            (email_id, filename, content_type, kind, len(content), content))

        # `is_pod` and `pod_po_numbers` are the ingest-time verdict — what a reader concluded when
        # it looked inside the file. `spitfire_post._pod_for` selects on those columns, not on the
        # extension, precisely so a tracker spreadsheet cannot be uploaded as proof of delivery.
        # Seeding them is simulating that reader, which is the only way an image or a `.doc` can
        # be tested at all: a PDF gets re-read on demand, an image would need OCR, and a `.doc`
        # has no reader in this codebase.
        conn.execute(
            """INSERT OR REPLACE INTO attachment_ledger
               (email_id, depth, ordinal, filename, declared_content_type, sniffed_kind,
                sha256, size_bytes, is_inline, disposition, first_seen_at,
                is_pod, pod_po_numbers, pod_delivery_date, pod_signed_by)
               VALUES (?, 0, 0, ?, ?, ?, ?, ?, 0, 'extracted', ?, 1, ?, ?, 'CC-TEST receiver')""",
            (email_id, filename, content_type, kind,
             hashlib.sha256(content).hexdigest(), len(content), now, PO, now[:10]))

        conn.execute(
            """INSERT INTO extracted_records
               (source_email_id, po_number, spec_code, item_description, vendor_name,
                quantity_received, unit_of_measure, pod_stated_date, email_date, received_by,
                po_line_number, extraction_source, extraction_confidence, status, created_at,
                comments)
               VALUES (?, ?, ?, ?, 'Pigeon & Poodle', ?, 'Set', ?, ?, 'CC-TEST receiver', ?, ?,
                       1.0, 'pending', ?, ?)""",
            (email_id, PO, spec, f"CC-TEST {label} — {description}", qty,
             now[:10], now[:10], po_line, MARKER, now,
             "Seeded to test the Post button. Not a real delivery."))
        made += 1
        print(f"  seeded {label}: PO {PO} line {po_line}, {qty:g} Set, "
              f"{filename} ({len(content):,} bytes, kind={kind})")

    conn.commit()
    return made


def remove(conn: sqlite3.Connection) -> int:
    """Drop the seeded records and everything they brought with them.

    The post ledger rows are left alone deliberately. They are the record that a receipt was
    created on Premier's training instance, which is still true after the seed is gone — deleting
    them would make the instance's CC-TEST receipts look like they came from nowhere.
    """
    emails = [r[0] for r in conn.execute(
        "SELECT DISTINCT source_email_id FROM extracted_records WHERE extraction_source = ?",
        (MARKER,))]
    if not emails:
        print("nothing seeded")
        return 0
    marks = ",".join("?" * len(emails))
    removed = conn.execute("DELETE FROM extracted_records WHERE extraction_source = ?",
                           (MARKER,)).rowcount
    conn.execute(f"DELETE FROM mail_attachment WHERE email_id IN ({marks})", emails)
    conn.execute(f"DELETE FROM attachment_ledger WHERE email_id IN ({marks})", emails)
    conn.execute(f"DELETE FROM mail_body WHERE email_id IN ({marks})", emails)
    conn.commit()
    print(f"removed {removed} seeded record(s) and {len(emails)} seeded email(s)")
    print("post-ledger rows kept: the receipts they created are still on training")
    return removed


def status(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, po_number, po_line_number, quantity_received, item_description "
        "FROM extracted_records WHERE extraction_source = ? ORDER BY id", (MARKER,)).fetchall()
    if not rows:
        print("nothing seeded")
    for r in rows:
        att = conn.execute(
            "SELECT filename, kind, LENGTH(content) FROM mail_attachment WHERE email_id = "
            "(SELECT source_email_id FROM extracted_records WHERE id = ?)", (r["id"],)).fetchone()
        print(f"  record {r['id']:<5} PO {r['po_number']} line {r['po_line_number']:<3} "
              f"{r['quantity_received']:g} Set   {att[0] if att else '(no attachment)'} "
              f"({att[1]}, {att[2]:,}b)" if att else "")

    print("\nwhat has been posted:")
    posted = conn.execute(
        """SELECT record_id, state, receipt_doc_no, receipt_key, pod_file_key, report_file_key
             FROM spitfire_post ORDER BY id""").fetchall()
    if not posted:
        print("  nothing yet")
    for p in posted:
        print(f"  record {p['record_id']:<5} {p['state']:<8} receipt {p['receipt_doc_no'] or '?':<6} "
              f"{p['receipt_key'][:8]}…  pod={bool(p['pod_file_key'])} "
              f"report={bool(p['report_file_key'])}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--remove", action="store_true", help="delete the seeded records")
    parser.add_argument("--status", action="store_true", help="show what is seeded and posted")
    args = parser.parse_args()

    conn = state_db.get_connection()
    if args.status:
        status(conn)
    elif args.remove:
        remove(conn)
    else:
        made = seed(conn)
        if made:
            print(f"\n{made} records seeded against PO {PO} in {PROJECT}.")
            print("Open http://127.0.0.1:8000/ui/records and look for the CC-TEST rows.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
