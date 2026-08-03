from datetime import datetime, timezone

from rapidfuzz import fuzz
from typing import Dict, List, Optional, Tuple

from config import settings
from connectors.mailbox import Mailbox
from pipeline import attachment_ledger, evidence, extracted_records_store, stage2_accumulate, state_db
from pipeline.models import DeliveryEvent, ExtractedRecord, TriageCategory
from pipeline.stage1_ingest import fetch_new_emails
from pipeline.parsing import text
from pipeline.stage1_triage import triage
from pipeline.stage3_extract import containers, dispatch
from pipeline.stage3_extract.base import ExtractionAdapter, ExtractionSource
from pipeline.stage3_extract.docx_adapter import DocxAdapter
from pipeline.stage3_extract.excel_adapter import ExcelAdapter
from pipeline.stage3_extract.freetext_adapter import FreetextAdapter
from pipeline.stage3_extract.html_adapter import HtmlAdapter
from pipeline.stage3_extract.ocr_adapter import DocumentIntelligenceClient, OcrAdapter
from pipeline.stage3_extract.pdf_adapter import PdfAdapter
from pipeline.stage3_extract.text_adapter import TextAdapter
from pipeline.stage3_extract.unsupported_adapter import UnsupportedFormatAdapter


def _log(message: str) -> None:
    print(f"[ingest_orchestrator] {message}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mark_processed_safely(mailbox: Mailbox, email, folder: str) -> None:
    """A folder-move failure (transient Graph hiccup, etc.) must never crash the run — the email
    is already durably captured in our own state by the time this is called; worst case it just
    sits in Inbox and gets looked at again next poll (seen_message_ids still skips reprocessing it)."""
    handle = getattr(email, "provider_message_id", None) or getattr(email, "email_id", email)
    try:
        mailbox.mark_processed(handle, folder)
    except Exception as e:
        _log(f"failed to move {handle} to '{folder}': {e}")


def build_default_adapters(ocr_client: Optional[DocumentIntelligenceClient] = None) -> List[ExtractionAdapter]:
    """The dispatch cascade, most specific first.

    `ocr_client` swaps in Azure AI Vision or Tesseract in place of the default mock, for both
    OcrAdapter and DocxAdapter's embedded-image fallback.

    `UnsupportedFormatAdapter` is deliberately last. It claims the kinds we recognise but do not
    read (.doc, .pptx, .7z, .rar, and anything unidentified), so those end with a stated reason
    on the exception queue instead of being indistinguishable from a coverage gap.
    """
    return [
        HtmlAdapter(), PdfAdapter(), DocxAdapter(ocr_client=ocr_client),
        OcrAdapter(client=ocr_client), ExcelAdapter(), TextAdapter(),
        FreetextAdapter(), UnsupportedFormatAdapter(),
    ]


def _stamp_triage_category(conn, email, category: str) -> None:
    """Backfill the triage verdict onto rows written before triage ran.

    The ledger is populated first precisely so routed and hidden mail is recorded at all; the
    verdict simply is not known yet at that moment."""
    for attachment in email.attachments:
        if attachment.ledger_id is not None:
            conn.execute(
                "UPDATE attachment_ledger SET triage_category = ? WHERE id = ?",
                (category, attachment.ledger_id),
            )
    conn.commit()


def _enforce_attachment_limits(email) -> Optional[str]:
    """Flag limit breaches, marking the offending attachments rather than removing them.

    Returns a reason when the email as a whole must be quarantined. There were no limits at all
    before this: a 200 MB attachment was read into memory, base64-encoded, and written into a
    SQLite TEXT column.
    """
    live = [a for a in email.attachments if not a.drop_hint]
    total = sum(a.size_bytes or len(a.content_bytes or b"") for a in live)

    for attachment in live:
        size = attachment.size_bytes or len(attachment.content_bytes or b"")
        if size > settings.MAX_ATTACHMENT_BYTES:
            attachment.drop_hint = f"oversize:{size} bytes exceeds {settings.MAX_ATTACHMENT_BYTES}"
            attachment.content_bytes = b""

    if len(live) > settings.MAX_ATTACHMENTS_PER_EMAIL:
        return f"{len(live)} attachments exceeds {settings.MAX_ATTACHMENTS_PER_EMAIL}"
    if total > settings.MAX_EMAIL_ATTACHMENT_BYTES:
        return f"{total} total bytes exceeds {settings.MAX_EMAIL_ATTACHMENT_BYTES}"
    return None


def run_adapters(source: ExtractionSource, adapters: Optional[List[ExtractionAdapter]] = None) -> List[ExtractedRecord]:
    """Walk the cascade until an adapter both claims the source and returns something.

    A failing adapter is logged and the cascade *continues* to the next one. The previous
    version returned `[]` on the first exception, which aborted the rest of the cascade and
    contradicted its own docstring (finding C10) — a PDF whose table pass raised would never
    reach the text pass that could have read it.

    An adapter that claims a source and legitimately finds nothing also falls through, so a
    body with an unrecognised table shape still gets a look from the free-text adapter.
    """
    claimed_by = None
    for adapter in adapters or build_default_adapters():
        try:
            if not adapter.can_handle(source):
                continue
            claimed_by = adapter.__class__.__name__
            records = adapter.extract(source)
            if records:
                return records
        except Exception as e:
            _log(f"{adapter.__class__.__name__} failed on {source.source_email_id}: {type(e).__name__}: {e}")
            continue

    if claimed_by is None:
        _log(
            f"no adapter recognized source for {source.source_email_id} "
            f"(source_type={source.source_type}, filename={source.filename}, content_type={source.content_type})"
        )
    return []


def _sources_for_delivery_event(event: DeliveryEvent) -> List[ExtractionSource]:
    """Body + every attachment, independently — each bundled email in the event contributes
    its own sources (see ORCHESTRATOR_DESIGN.md).

    Every source carries the event's PO in `only_po`, plus the email's sender and subject. The
    PO filter is what stops a multi-PO document being staged once per PO: notice 239260 covers
    206725 and 207665, releases as two events, and without the filter each event staged all
    thirteen of its lines (finding C1). Sender and subject are what let a vendor parser
    recognise the format at all.
    """
    sources = []
    for te in event.emails:
        email = te.email
        common = {
            "source_email_id": email.email_id,
            "email_date": email.received_at,
            "sender_address": te.origin_sender_address or email.sender_address,
            "subject": email.subject,
            "only_po": event.key.po_number,
        }
        if email.body_html or email.body_text:
            sources.append(ExtractionSource(
                source_type="body", body_html=email.body_html, body_text=email.body_text, **common,
            ))
        for att in email.attachments:
            if att.drop_hint:
                continue   # already ledgered with a terminal disposition at ingest
            sources.append(ExtractionSource(
                source_type="attachment", filename=att.filename,
                content_type=att.content_type, content_bytes=att.content_bytes,
                ledger_id=att.ledger_id, container_path=att.container_path, **common,
            ))
    return sources


def _belongs_to_event(record: ExtractedRecord, event: DeliveryEvent) -> bool:
    """Second line of defence behind `only_po`.

    Adapters that cannot filter by PO themselves — a carrier POD names a PO but a free-text
    pass may name none — still hand back records that must not be attributed to the wrong
    delivery. A record with no PO is adopted by the event (it is evidence *for* this delivery,
    which is why it was in the bundle) and stamped with the event's PO; a record naming a
    different PO is dropped, because that PO has its own event.
    """
    if not record.po_number:
        record.po_number = event.key.po_number
        return True
    return record.po_number == event.key.po_number


DESCRIPTION_MATCH_THRESHOLD = 85   # rapidfuzz token_set_ratio


def _group_records(records: List[ExtractedRecord]) -> Dict[Tuple, List[ExtractedRecord]]:
    """Group records that describe the same PO line, across sources of differing richness.

    The two Authority notices for one delivery state different things about the same goods: the
    Inbound gives a spec *and* a Spitfire line number, the Delivered gives a spec only. So the
    spec is the primary key — using the line number first would leave the two ungrouped and
    stage every line twice.

    Lines with no spec at all are the awkward case: Authority puts prose in `Part #`
    ("Accessory Pocket", "Toe Kick Planter"), and two such rows on one PO are different items.
    They are separated by line number where stated, and otherwise matched on description, which
    is how the Delivered notice's `Custom Accessory Pocket` finds the Inbound row it belongs to.
    """
    groups: Dict[Tuple, List[ExtractedRecord]] = {}
    for record in records:
        # The *full* spec, sub-part suffix included — never the parent. A lamp arrives as
        # STE-402-LT-B (11 bases) and STE-402-LT-SH (12 shades), which are Spitfire lines 300
        # and 301: separate receivable lines that legitimately have different quantities.
        # Grouping them by their shared parent STE-402-LT merged two real lines into one and
        # reported the difference as a quantity conflict. The parent is for Stage 4's match
        # lookup; it is not an identity.
        spec = record.spec_code or record.parent_spec_code
        if spec:
            groups.setdefault(("spec", record.po_number, spec), []).append(record)
            continue

        description = (record.item_description or "").strip().lower()
        placed = False
        for key, members in groups.items():
            if key[0] not in ("line", "desc") or key[1] != record.po_number:
                continue
            if record.po_line_number is not None and key[0] == "line" and key[2] != record.po_line_number:
                continue   # the source told us these are different lines
            if description and any(
                fuzz.token_set_ratio(description, (m.item_description or "").lower()) >= DESCRIPTION_MATCH_THRESHOLD
                for m in members
            ):
                members.append(record)
                placed = True
                break
        if placed:
            continue

        if record.po_line_number is not None:
            key = ("line", record.po_number, record.po_line_number)
        else:
            key = ("desc", record.po_number, description[:120])
        groups.setdefault(key, []).append(record)
    return groups


def reconcile_cross_source_duplicates(records: List[ExtractedRecord]) -> List[ExtractedRecord]:
    """Agreeing quantities (or one missing) -> keep only the highest-confidence record, log the
    rest as discarded duplicates. Disagreeing quantities -> keep all, flagged
    +quantity_conflict, never silently pick one. See ORCHESTRATOR_DESIGN.md.

    This is what collapses a Delivered notice and its matching Inbound into one set of records.
    Both describe the same goods; the Inbound wins on confidence because it states the Spitfire
    line number and the Delivered does not.
    """
    groups = _group_records(records)

    result = []
    for key, group in groups.items():
        if len(group) == 1:
            result.append(group[0])
            continue
        quantities = {r.quantity_received for r in group if r.quantity_received is not None}
        if len(quantities) <= 1:
            best = max(group, key=lambda r: r.extraction_confidence)
            discarded = [r for r in group if r is not best]
            if discarded:
                _log(f"discarded {len(discarded)} duplicate record(s) for {key}, kept source={best.extraction_source}")
            result.append(best)
        else:
            _log(f"quantity conflict across sources for {key}: {sorted(quantities)} — routing all, not guessing")
            for r in group:
                r.extraction_source = r.extraction_source + "+quantity_conflict"
            result.extend(group)
    return result


def process_new_mail(
    mailbox: Mailbox,
    ocr_client: Optional[DocumentIntelligenceClient] = None,
    conn=None,
) -> int:
    """Runs Stages 1->2->3 end to end over every new email, writes results to
    extracted_records. Returns the number of records staged. One bad email/source is logged
    and skipped, never aborts the rest of the run — see ORCHESTRATOR_DESIGN.md.

    `conn` is injectable (an existing sqlite3 connection, e.g. ":memory:" with the schema
    already created) so tests never have to touch the real on-disk pipeline_state.sqlite3.
    """
    owns_connection = conn is None
    if conn is None:
        conn = state_db.get_connection()
    adapters = build_default_adapters(ocr_client=ocr_client)
    evidence_cache = evidence.EvidenceCache()
    staged_count = 0
    try:
        released_events: List[DeliveryEvent] = []

        for email in fetch_new_emails(mailbox, conn=conn):
            try:
                oversize = _enforce_attachment_limits(email)

                # Ledger every attachment *before* anything branches. ROUTE and HIDE mail exits
                # without reaching extraction, so anything recorded further down structurally
                # cannot see it — and ROUTE is where the interesting failures land, since a
                # photographed POD with no readable text ends up there.
                attachment_ledger.observe(conn, email, None, _now_iso())

                # Then read the attachments, and only then decide whether the mail matters.
                # Two corpus threads carry no PO in any body because every PO is inside an
                # attached spreadsheet; triaging before opening it can only ever be a guess.
                email_evidence = evidence.gather(
                    conn, email, text.body_text_of(email), adapters, now=_now_iso(),
                ) if not oversize else evidence.EmailEvidence(email_id=email.email_id)
                evidence_cache.put(email_evidence)

                triaged = triage(email, evidence=email_evidence)
                _stamp_triage_category(conn, email, triaged.category.value)

                if oversize:
                    _log(f"quarantined {email.email_id}: {oversize}")
                    attachment_ledger.close_undispatched(conn, email, _now_iso())
                    _mark_processed_safely(mailbox, email, settings.MAILBOX_FOLDER_QUARANTINE)
                    continue
                if triaged.category == TriageCategory.ROUTE:
                    _log(f"routed, no accumulation: {email.email_id} — {triaged.reason}")
                    attachment_ledger.close_undispatched(conn, email, _now_iso())
                    _mark_processed_safely(mailbox, email, settings.MAILBOX_FOLDER_ROUTED)
                    continue
                if triaged.category == TriageCategory.HIDE:
                    # discarded — Stage 1's job is done, nothing more happens with it
                    attachment_ledger.close_undispatched(conn, email, _now_iso())
                    _mark_processed_safely(mailbox, email, settings.MAILBOX_FOLDER_HIDDEN)
                    continue
                released_events.extend(stage2_accumulate.process_triaged_email(conn, triaged, _now_iso()))
                _mark_processed_safely(mailbox, email, settings.MAILBOX_FOLDER_PROCESSED)
            except Exception as e:
                _log(f"failed to triage/accumulate {email.email_id}: {e}")
                _mark_processed_safely(mailbox, email, settings.MAILBOX_FOLDER_ERRORS)
                continue

        released_events.extend(stage2_accumulate.sweep_stale_holds(conn, datetime.now(timezone.utc)))

        for event in released_events:
            try:
                raw_records: List[ExtractedRecord] = []
                for src in _sources_for_delivery_event(event):
                    if src.source_type == "attachment":
                        # Already read during the evidence pass, before triage — re-reading it
                        # would duplicate the work and, for images, the OCR spend.
                        produced = evidence_cache.records_for(src.source_email_id, event.key.po_number)
                    else:
                        produced = dispatch.dispatch_source(
                            conn, src, adapters, budget=containers.Budget.fresh(), now=_now_iso(),
                        )
                    for record in produced:
                        if not _belongs_to_event(record, event):
                            continue
                        record.shipment_number = record.shipment_number or event.key.shipment_number
                        raw_records.append(record)

                for record in reconcile_cross_source_duplicates(raw_records):
                    extracted_records_store.write_pending(conn, record, _now_iso())
                    staged_count += 1
            except Exception as e:
                _log(f"failed to extract for delivery {event.key}: {e}")
                continue

        # Anything still open belongs to a HOLD whose event has not fired. Settling it here is
        # what keeps attachment_ledger.orphans() meaningful as a leak detector.
        attachment_ledger.close_open_rows(conn, _now_iso())
    finally:
        if owns_connection:
            conn.close()
    return staged_count
