"""Re-read attachments that failed on something other than their own contents.

The tool for "the OCR service was down; now it is fixed; start again from where it broke". It
selects attachments the ledger has already recorded as failures, resolves their bytes **from local
storage**, and runs the real extraction cascade over them again.

**It deletes nothing.** That is the whole reason it exists beside `tools/reprocess_mail.py`, which
erases `email_log`, `attachment_ledger`, `accumulation`, `extracted_records` and `deliveries` for
the selected mail and re-downloads it from Graph. That tool destroys, with no `origin` filter:
records a person built by hand, their `pod_waived_by` waivers — the only thing that lets a POD-less
record reach Spitfire — and their choice of which file is the POD. It also deletes the `email_log`
row that carries `handled_manually`, which is what `dedupe.is_handled_manually` reads, so the guard
against overwriting a person's work returns False immediately afterwards. None of that is
acceptable for recovering from somebody else's outage.

This corrects the ledger row **in place** instead, so nothing else has to be rebuilt.

    python -m tools.reextract --dry-run                  # what would be re-read, and what it costs
    python -m tools.reextract --since 2026-08-24T12:00Z  # everything that failed since then
    python -m tools.reextract --ocr azure --yes          # for real, with the paid client

**Read-only against Graph and Spitfire.** Bytes come from `attachment_store` via
`attachment_bytes.resolve`, which no reprocess ever deletes, so the mailbox is never touched.
"""

import argparse
import io
import shutil
import sqlite3
import sys
from datetime import datetime

from config import settings
from pipeline import attachment_bytes, attachment_ledger, dedupe, evidence as evidence_mod, state_db
from pipeline.stage3_extract import dispatch
from pipeline.stage3_extract.base import ExtractionSource
from pipeline.stage3_extract.ocr_adapter import CachingOcrClient
from tools.ingest_corpus import BudgetedOcrClient

# What this tool is for. `service_unavailable` is the disposition an external-dependency failure
# now leaves; the other two are read failures that a newer adapter or a fixed library may well get
# further with, and re-reading them is free unless they are images.
RERUNNABLE = (
    attachment_ledger.SERVICE_UNAVAILABLE,
    attachment_ledger.UNREADABLE,
    attachment_ledger.CORRUPT,
)


# Kinds whose text can be read locally for nothing. An image or a scanned PDF costs a paid OCR
# call per page, which a backfill across the whole history has no business spending — the same
# rule `tools/backfill_pod_flags.py` follows, and for the same reason.
FREE_TO_READ = ("pdf", "html", "xlsx", "xls", "docx", "text", "csv", "msg")


def _needs_ocr(kind: str, content: bytes) -> bool:
    """Whether reading this costs a paid call.

    `sniffed_kind` alone is not enough. A scanned Warehouse Receiving Report sniffs as `pdf` and is
    every bit as much an OCR job as a photograph — `PdfAdapter.can_handle` declines it and
    `OcrAdapter` picks it up. Checking the text layer is local and free, so `--free-only` can mean
    what it says instead of meaning "probably free".
    """
    if kind == "image":
        return True
    if kind == "pdf":
        from pipeline.stage3_extract.pdf_adapter import _has_text_layer
        try:
            return not _has_text_layer(content)
        except Exception:                                          # noqa: BLE001
            return True
    return False


def _email_date_for(conn: sqlite3.Connection, email_id: str) -> str:
    """When this message arrived, for the source an adapter reads `email_date` off.

    It was hardcoded empty, so nothing re-read through this tool carried a date at all — which
    also left the delivery-date fallback in `stage_records` with nothing to fall back to.
    """
    row = conn.execute("SELECT COALESCE(email_date, '') FROM email_log WHERE email_id = ?",
                       (email_id,)).fetchone()
    return (row[0] if row else "") or ""


def _delivery_keys_for(conn: sqlite3.Connection, email_id: str) -> dict:
    """`po_number -> (delivery_ref, delivery_rung)` for this message, from `accumulation`.

    Stage 2 already resolved which physical delivery each of this email's purchase orders belongs
    to and stored it; recomputing the ladder here would be a second opinion on a question that has
    an answer on disk, and the two could disagree.

    An empty dict means the mail never accumulated — it was routed to a person, or is still on
    hold. Its attachments are still worth reading (the ledger keeps the POD verdict, which is what
    a held event will need when it releases) but there is no delivery to hang a record on.
    """
    prior = getattr(conn, "row_factory", None)
    try:
        conn.row_factory = None
        rows = conn.execute(
            "SELECT po_number, COALESCE(delivery_ref, ''), COALESCE(delivery_rung, '') "
            "  FROM accumulation WHERE email_id = ?", (email_id,)).fetchall()
    finally:
        conn.row_factory = prior
    return {po: (ref, rung) for po, ref, rung in rows if po}


def _pod_ledger_ids_for(conn: sqlite3.Connection, email_id: str) -> dict:
    """`po_number -> attachment_ledger.id` of the proof naming it, read off the ledger verdicts.

    Reuses `evidence._read_pod_verdicts` so a re-read links a record to its POD by exactly the
    rule the live path uses, including its shape check on the purchase order and its exclusion of
    inline images.
    """
    bundle = evidence_mod.EmailEvidence(email_id=email_id)
    evidence_mod._read_pod_verdicts(conn, bundle)
    return dict(bundle.pod_ledger_ids)


def _too_small_to_be_a_document(content: bytes, min_pixels: int) -> bool:
    """Whether this image is too small to be a photographed or scanned page.

    A packing slip photographed on a phone is measured in megapixels; an email signature logo is
    a few thousand. Below the floor there is nothing to read, and reading it anyway is what spent
    Premier's OCR quota on 244 banners while the documents behind them were refused.
    """
    if min_pixels <= 0:
        return False
    try:
        from PIL import Image
        with Image.open(io.BytesIO(content)) as image:
            width, height = image.size
    except Exception:                                              # noqa: BLE001
        return False        # unreadable here is not a reason to skip — let the adapter decide
    return width * height < min_pixels


def _stage(conn, records, row, now: str) -> tuple:
    """Persist what a re-read produced. Returns `(staged_ids, unattributed)`.

    This is the step `dispatch` does not do: it returns records and stamps the ledger, and every
    record this tool read used to be counted and then dropped on the floor. Staging goes through
    `ingest_orchestrator.stage_records`, so the dedupe guards, the deferred delivery row and the
    POD link are the live path's, not a second copy of them.
    """
    from pipeline.ingest_orchestrator import stage_records

    keys = _delivery_keys_for(conn, row["email_id"])
    if not keys:
        return [], len(records)

    pod_ids = _pod_ledger_ids_for(conn, row["email_id"])
    only_po = next(iter(keys)) if len(keys) == 1 else None

    by_po: dict = {}
    unattributed = 0
    for record in records:
        po_number = record.po_number or only_po
        if not po_number or po_number not in keys:
            # Either the record names no purchase order and the message covers several, so
            # guessing would attribute goods to the wrong one, or it names a PO this message never
            # accumulated. Both are for a person, not for a default.
            unattributed += 1
            continue
        record.po_number = po_number
        by_po.setdefault(po_number, []).append(record)

    staged: list = []
    for po_number, group in by_po.items():
        delivery_ref, delivery_rung = keys[po_number]
        staged.extend(stage_records(
            conn, group, po_number=po_number, delivery_ref=delivery_ref,
            delivery_rung=delivery_rung, now=now,
            pod_ledger_id_of=lambda r: pod_ids.get(r.po_number),
        ))
    return staged, unattributed


def select(conn: sqlite3.Connection, *, disposition=None, since="", email_id="",
           claimed_by="", free_only=False, limit=0) -> list:
    """Ledger rows worth re-reading, oldest first.

    Selected by *what failed*, not by which email — one message can carry twenty-three images of
    which four failed, and re-running the message would re-read and re-bill the nineteen that
    worked.

    `claimed_by` exists for the outage that prompted this tool. Attachments that failed *before*
    `service_unavailable` existed are recorded as `extracted` with `records_extracted=1`, because
    `OcrAdapter` answered a 401 with a placeholder record and `dispatch` found that list truthy.
    Those rows cannot be found by disposition — they claim to have succeeded — so the only handle
    left is "OcrAdapter touched it during the window":

        --disposition extracted --claimed-by OcrAdapter --since 2026-08-24T12:00:00

    Going forward the default selection finds them by disposition and this flag is not needed.
    """
    where = ["disposition IN (%s)" % ",".join("?" * len(RERUNNABLE))]
    params = list(RERUNNABLE)
    if disposition:
        where = ["disposition = ?"]
        params = [disposition]
    if claimed_by:
        where.append("claimed_by = ?")
        params.append(claimed_by)
    if since:
        where.append("COALESCE(last_updated_at, first_seen_at) >= ?")
        params.append(since)
    if email_id:
        where.append("email_id = ?")
        params.append(email_id)
    if free_only:
        where.append("sniffed_kind IN (%s)" % ",".join("?" * len(FREE_TO_READ)))
        params.extend(FREE_TO_READ)

    sql = ("SELECT id, email_id, ordinal, filename, declared_content_type, sniffed_kind, "
           "       disposition, disposition_detail, container_path, claimed_by, "
           "       records_extracted, sha256 "
           "  FROM attachment_ledger WHERE " + " AND ".join(where) +
           # Real attachments before inline chrome, for the same reason `evidence.gather` orders
           # them that way: the OCR quota is finite, whichever runs first gets it, and a backlog
           # that spends it on signature logos leaves the documents behind them refused.
           "  ORDER BY COALESCE(is_inline, 0), first_seen_at, id")
    if limit:
        sql += f" LIMIT {int(limit)}"
    conn.row_factory = sqlite3.Row
    return conn.execute(sql, params).fetchall()


def _protected(conn: sqlite3.Connection, email_id: str) -> str:
    """Why this email's records must not be touched, or "" if they may be.

    Checked per email rather than per attachment because the thing being protected is the *record*
    a person made from the message. This guard is only meaningful on a path that leaves `email_log`
    intact — which is exactly what `reprocess_mail` does not do.
    """
    if dedupe.is_handled_manually(conn, email_id):
        return "a person already built a record from this message by hand"
    row = conn.execute(
        """SELECT COUNT(*) FROM extracted_records
            WHERE source_email_id = ?
              AND (origin = 'manual' OR COALESCE(pod_waived_by, '') <> ''
                   OR pod_ledger_id IS NOT NULL)""", (email_id,)).fetchone()
    if row and row[0]:
        return f"{row[0]} record(s) here carry manual work, a POD waiver or a chosen POD"
    return ""


def _posted_pos(conn: sqlite3.Connection) -> set:
    """`(po, line)` pairs already sent to Spitfire.

    A re-read POD hashes differently from the one that posted, so `post_ledger.find_delivery`'s
    `(PO, line, evidence hash)` idempotency key will *not* recognise a re-extraction as the same
    delivery — the failure that let PO 212559 collect eight receipts for one delivery. Nothing here
    posts, but the census names these so a reviewer does not press Post on a rebuilt twin.
    """
    return {(r[0], r[1]) for r in conn.execute(
        "SELECT po_number, line_number FROM spitfire_post "
        " WHERE state IN ('posted', 'pod_posted', 'partial')")}


def _restore(conn: sqlite3.Connection, row, now: str) -> None:
    """Put back the verdict this row had before the re-read overwrote it."""
    conn.execute(
        """UPDATE attachment_ledger
              SET disposition = ?, disposition_detail = ?, claimed_by = ?,
                  records_extracted = ?, error_type = NULL, last_updated_at = ?
            WHERE id = ?""",
        (row["disposition"], row["disposition_detail"] or "", row["claimed_by"],
         row["records_extracted"] or 0, now, row["id"]))
    conn.commit()


def run(*, dry_run: bool = True, since: str = "", email_id: str = "", disposition: str = "",
        claimed_by: str = "", free_only: bool = False, ocr: str = "mock",
        max_ocr_pages: int = settings.OCR_PAGES_PER_RUN, limit: int = 0,
        min_image_pixels: int = 0, db_path=None) -> int:
    db_path = db_path or state_db.path_for("mailbox")
    conn = state_db.get_connection(db_path)
    rows = select(conn, disposition=disposition, since=since, email_id=email_id,
                  claimed_by=claimed_by, free_only=free_only, limit=limit)

    print(f"selected   {len(rows)} attachment(s) to re-read")
    if not rows:
        print("nothing to do.")
        return 0

    by_disposition: dict = {}
    for row in rows:
        by_disposition[row["disposition"]] = by_disposition.get(row["disposition"], 0) + 1
    for name, count in sorted(by_disposition.items()):
        print(f"           {count:>4}  {name}")
    images = sum(1 for r in rows if r["sniffed_kind"] == "image")
    print(f"           {images:>4}  of them are images (each one a page of OCR)")
    # Counted before anything is sent, because it is the number that decides whether a run fits
    # inside the budget — and it is less than half the row count on this ledger.
    distinct = len({(r["sha256"] or "").strip() or f"row-{r['id']}" for r in rows})
    print(f"           {distinct:>4}  distinct document(s) among them; the rest are repeats "
          f"the cache serves for nothing")
    print(f"ocr client {ocr}, budget {max_ocr_pages} page(s)")
    if dry_run and ocr != "mock":
        # Said plainly, because "dry run" reads as "free" and here it is not: the cascade really
        # runs, so the paid calls really happen. Only the database writes are withheld.
        print("           NOTE: a dry run with a real client still bills — only writes are skipped")

    if not dry_run:
        # Before any write, and named for when it happened. `backfill_deliveries` does the same,
        # and this tool writes to the same live store.
        backup = db_path.with_name(f"{db_path.name}.bak-{datetime.now():%Y%m%d_%H%M%S}")
        shutil.copy2(db_path, backup)
        print(f"backup     {backup.name}")

    from pipeline.ingest_orchestrator import build_default_adapters
    from pipeline.stage3_extract.ocr_adapter import build_client

    # Cache inside the budget, so a repeat costs neither a call nor a page. The other order would
    # meter cache hits as spending and stop a run that had not spent anything.
    cache = CachingOcrClient(build_client(ocr))
    client = BudgetedOcrClient(cache, max_pages=max_ocr_pages)
    adapters = build_default_adapters(ocr_client=client)
    now = datetime.now().isoformat()

    read = still_failing = no_bytes = protected = needs_ocr = 0
    produced = staged_total = unattributed_total = too_small = 0
    skipped_emails: dict = {}

    for index, row in enumerate(rows, start=1):
        email_id_of_row = row["email_id"]
        reason = skipped_emails.get(email_id_of_row)
        if reason is None:
            reason = _protected(conn, email_id_of_row)
            skipped_emails[email_id_of_row] = reason
        if reason:
            protected += 1
            print(f"  [{index}/{len(rows)}] skipped {row['filename']!r} — {reason}")
            continue

        resolved = attachment_bytes.resolve(conn, email_id_of_row, row["ordinal"],
                                            row["filename"] or "")
        if not (resolved and resolved.content):
            no_bytes += 1
            print(f"  [{index}/{len(rows)}] no stored bytes for {row['filename']!r}")
            continue

        if (row["sniffed_kind"] == "image"
                and _too_small_to_be_a_document(resolved.content, min_image_pixels)):
            # Skipped without touching the ledger row, so lowering the floor later still
            # finds it exactly where it was.
            too_small += 1
            print(f"  [{index}/{len(rows)}] skipped {row['filename']!r} — below the "
                  f"{min_image_pixels}px floor, not a document")
            continue

        if free_only and _needs_ocr(row["sniffed_kind"], resolved.content):
            needs_ocr += 1
            print(f"  [{index}/{len(rows)}] skipped {row['filename']!r} — needs OCR, and "
                  f"--free-only was asked for")
            continue

        source = ExtractionSource(
            source_email_id=email_id_of_row,
            email_date=_email_date_for(conn, email_id_of_row),
            source_type="attachment",
            filename=row["filename"] or "",
            content_type=row["declared_content_type"] or "",
            content_bytes=resolved.content,
            # None on a dry run: `dispatch._record` is a no-op without a ledger id, which makes the
            # preview exact rather than approximate — the same cascade, writing nothing.
            ledger_id=None if dry_run else row["id"],
            container_path=row["container_path"] or "",
        )
        try:
            records = dispatch.dispatch_source(
                None if dry_run else conn, source, adapters, now=now)
        except Exception as e:                                     # noqa: BLE001
            still_failing += 1
            print(f"  [{index}/{len(rows)}] {row['filename']!r} still failing: "
                  f"{type(e).__name__}: {e}")
            continue

        if records:
            read += 1
            produced += len(records)
            staged, unattributed = ([], 0) if dry_run else _stage(conn, records, row, now)
            staged_total += len(staged)
            unattributed_total += unattributed
            note = f"{len(records)} record(s)"
            if not dry_run:
                note += f", staged {len(staged)}"
                if unattributed:
                    note += f", {unattributed} with no delivery to attach to"
            print(f"  [{index}/{len(rows)}] read {row['filename']!r} -> {note}")
        else:
            still_failing += 1
            if not dry_run and row["disposition"] == attachment_ledger.EXTRACTED:
                # A re-read that finds nothing is not evidence that the earlier read was wrong. A
                # weaker client — the mock, or Tesseract standing in for Azure — returns nothing on
                # a document a paid reader handled fine, and `dispatch` will have just overwritten
                # `extracted` with `empty`. Put the earlier verdict back: this tool exists to
                # recover failures, never to manufacture them.
                _restore(conn, row, now)
                print(f"  [{index}/{len(rows)}] {row['filename']!r} read nothing this time — "
                      f"kept the earlier 'extracted' verdict")
            else:
                print(f"  [{index}/{len(rows)}] {row['filename']!r} read, nothing extractable")

    print()
    print(f"re-read successfully   {read}   ({produced} record(s) read)")
    if not dry_run:
        print(f"records STAGED         {staged_total}")
        print(f"read but not staged    {unattributed_total}   <- no accumulated delivery "
              f"for their PO; the ledger keeps the POD verdict")
    print(f"still nothing          {still_failing}")
    print(f"no stored bytes        {no_bytes}   <- these need their mail re-fetched from Graph")
    print(f"skipped, human work    {protected}")
    print(f"skipped, needs OCR     {needs_ocr}")
    print(f"skipped, too small     {too_small}")
    print(f"ocr pages billed       {client.calls} (refused {client.refused})")
    print(f"ocr cache              {cache.hits} repeat(s) served without a call, "
          f"{cache.misses} distinct document(s) read")

    posted = _posted_pos(conn)
    if posted and not dry_run:
        print(f"\n{len(posted)} PO/line pair(s) already posted to Spitfire. A re-read POD hashes "
              f"differently, so the duplicate guard will not recognise a rebuilt twin — check "
              f"before posting anything from this run.")

    if dry_run:
        print("\nnothing was written. Add --yes to re-read these for real.")
    else:
        conn.commit()
    conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", default="",
                        help="only attachments last touched at or after this ISO instant")
    parser.add_argument("--email", default="", help="one internetMessageId")
    parser.add_argument("--disposition", default="",
                        help=f"one disposition instead of the default {list(RERUNNABLE)}")
    parser.add_argument("--claimed-by", default="",
                        help="only rows this adapter claimed, e.g. OcrAdapter — for finding "
                             "failures recorded before `service_unavailable` existed")
    parser.add_argument("--free-only", action="store_true",
                        help="only kinds readable locally for nothing — skips images and scans, "
                             "so a whole-history backfill costs no OCR")
    parser.add_argument("--limit", type=int, default=0, help="stop after this many")
    parser.add_argument("--min-image-pixels", type=int, default=0,
                        help="skip images smaller than this many pixels (width*height) "
                             "without billing for them — 307200 is 640x480, below which a "
                             "photographed page does not exist and a signature logo does")
    parser.add_argument("--ocr", default="mock", choices=["mock", "azure", "vision", "tesseract", "auto"])
    # `settings.OCR_PAGES_PER_RUN`, never `ingest_corpus.DEFAULT_MAX_OCR_PAGES`. That constant is 15
    # — a tool default written for driving a 14-message corpus by hand — and taking it here is the
    # same leak `settings.py` records as having stranded 104 live attachments at "OCR page budget of
    # 15 exhausted" while the credentials were valid. A recovery run over this mailbox has ~489
    # documents to read; stopping after fifteen pages would look like the quota problem it is meant
    # to clear.
    parser.add_argument("--max-ocr-pages", type=int, default=settings.OCR_PAGES_PER_RUN)
    parser.add_argument("--dry-run", action="store_true",
                        help="change nothing in the database. NOTE: with --ocr azure this still "
                             "calls Azure and still bills, because it runs the real cascade; only "
                             "the writes are skipped. Use the default mock client for a free "
                             "preview of what would be selected")
    parser.add_argument("--yes", action="store_true", help="required to actually write")
    args = parser.parse_args()

    if not args.yes and not args.dry_run:
        print("Refusing to write without --yes. Showing a dry run instead.\n")
    return run(dry_run=args.dry_run or not args.yes, since=args.since, email_id=args.email,
               disposition=args.disposition, claimed_by=args.claimed_by,
               free_only=args.free_only, ocr=args.ocr,
               max_ocr_pages=args.max_ocr_pages, limit=args.limit,
               min_image_pixels=args.min_image_pixels)


if __name__ == "__main__":
    raise SystemExit(main())
