from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from config import settings
from connectors.mailbox import Mailbox
from pipeline import extracted_records_store, stage2_accumulate, state_db
from pipeline.models import DeliveryEvent, ExtractedRecord, TriageCategory
from pipeline.stage1_ingest import fetch_new_emails
from pipeline.stage1_triage import triage
from pipeline.stage3_extract.base import ExtractionAdapter, ExtractionSource
from pipeline.stage3_extract.docx_adapter import DocxAdapter
from pipeline.stage3_extract.excel_adapter import ExcelAdapter
from pipeline.stage3_extract.freetext_adapter import FreetextAdapter
from pipeline.stage3_extract.html_adapter import HtmlAdapter
from pipeline.stage3_extract.ocr_adapter import DocumentIntelligenceClient, OcrAdapter
from pipeline.stage3_extract.pdf_adapter import PdfAdapter


def _log(message: str) -> None:
    print(f"[ingest_orchestrator] {message}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mark_processed_safely(mailbox: Mailbox, email_id: str, folder: str) -> None:
    """A folder-move failure (transient Graph hiccup, etc.) must never crash the run — the email
    is already durably captured in our own state by the time this is called; worst case it just
    sits in Inbox and gets looked at again next poll (seen_message_ids still skips reprocessing it)."""
    try:
        mailbox.mark_processed(email_id, folder)
    except Exception as e:
        _log(f"failed to move {email_id} to '{folder}': {e}")


def build_default_adapters(ocr_client: Optional[DocumentIntelligenceClient] = None) -> List[ExtractionAdapter]:
    """The fixed dispatch order from STAGE_3_EXTRACT.md, extended with DocxAdapter (discovered
    as a real, plausible attachment format via real dummy test documents — not in the original
    discovery-doc sample set). `ocr_client` lets callers swap in Tesseract or the real Azure
    client instead of the default fixture-based mock, for both OcrAdapter and DocxAdapter's own
    embedded-image fallback."""
    return [
        HtmlAdapter(), PdfAdapter(), DocxAdapter(ocr_client=ocr_client),
        OcrAdapter(client=ocr_client), ExcelAdapter(), FreetextAdapter(),
    ]


def run_adapters(source: ExtractionSource, adapters: Optional[List[ExtractionAdapter]] = None) -> List[ExtractedRecord]:
    """First adapter whose can_handle() returns True wins. One adapter failing is logged and
    treated as 'found nothing', never lets one bad source abort the rest of the delivery.
    An attachment type no adapter recognizes is also logged, not silently staged as an empty
    record — see FreetextAdapter, which intentionally does not claim attachments."""
    for adapter in adapters or build_default_adapters():
        try:
            if adapter.can_handle(source):
                return adapter.extract(source)
        except Exception as e:
            _log(f"{adapter.__class__.__name__} failed on {source.source_email_id}: {e}")
            return []
    _log(
        f"no adapter recognized source for {source.source_email_id} "
        f"(source_type={source.source_type}, filename={source.filename}, content_type={source.content_type})"
    )
    return []


def _sources_for_delivery_event(event: DeliveryEvent) -> List[ExtractionSource]:
    """Body + every attachment, independently — each bundled email in the event contributes
    its own sources (see ORCHESTRATOR_DESIGN.md)."""
    sources = []
    for te in event.emails:
        email = te.email
        if email.body_html or email.body_text:
            sources.append(ExtractionSource(
                source_email_id=email.email_id, email_date=email.received_at,
                source_type="body", body_html=email.body_html, body_text=email.body_text,
            ))
        for att in email.attachments:
            sources.append(ExtractionSource(
                source_email_id=email.email_id, email_date=email.received_at,
                source_type="attachment", filename=att.filename,
                content_type=att.content_type, content_bytes=att.content_bytes,
            ))
    return sources


def reconcile_cross_source_duplicates(records: List[ExtractedRecord]) -> List[ExtractedRecord]:
    """Groups by (po_number, parent_spec_code). Agreeing quantities (or one missing) -> keep
    only the highest-confidence record, log the rest as discarded duplicates. Disagreeing
    quantities -> keep all, flagged +quantity_conflict, never silently pick one. See
    ORCHESTRATOR_DESIGN.md."""
    groups: Dict[Tuple[str, Optional[str]], List[ExtractedRecord]] = {}
    for r in records:
        key = (r.po_number, r.parent_spec_code or r.spec_code)
        groups.setdefault(key, []).append(r)

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
    staged_count = 0
    try:
        released_events: List[DeliveryEvent] = []

        for email in fetch_new_emails(mailbox, conn=conn):
            try:
                triaged = triage(email)
                if triaged.category == TriageCategory.ROUTE:
                    _log(f"routed, no accumulation: {email.email_id} — {triaged.reason}")
                    _mark_processed_safely(mailbox, email.email_id, settings.MAILBOX_FOLDER_ROUTED)
                    continue
                if triaged.category == TriageCategory.HIDE:
                    # discarded — Stage 1's job is done, nothing more happens with it
                    _mark_processed_safely(mailbox, email.email_id, settings.MAILBOX_FOLDER_HIDDEN)
                    continue
                released_events.extend(stage2_accumulate.process_triaged_email(conn, triaged, _now_iso()))
                _mark_processed_safely(mailbox, email.email_id, settings.MAILBOX_FOLDER_PROCESSED)
            except Exception as e:
                _log(f"failed to triage/accumulate {email.email_id}: {e}")
                _mark_processed_safely(mailbox, email.email_id, settings.MAILBOX_FOLDER_ERRORS)
                continue

        released_events.extend(stage2_accumulate.sweep_stale_holds(conn, datetime.now(timezone.utc)))

        for event in released_events:
            try:
                raw_records: List[ExtractedRecord] = []
                for src in _sources_for_delivery_event(event):
                    for record in run_adapters(src, adapters):
                        record.shipment_number = event.key.shipment_number
                        raw_records.append(record)

                for record in reconcile_cross_source_duplicates(raw_records):
                    extracted_records_store.write_pending(conn, record, _now_iso())
                    staged_count += 1
            except Exception as e:
                _log(f"failed to extract for delivery {event.key}: {e}")
                continue
    finally:
        if owns_connection:
            conn.close()
    return staged_count
