from datetime import datetime, timezone

from rapidfuzz import fuzz
from typing import Callable, Dict, List, Optional, Tuple

from config import settings
from connectors.mailbox import Mailbox
from pipeline import (
    attachment_ledger, email_log, evidence, extracted_records_store, mail_arrivals,
    stage2_accumulate, state_db,
)
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
from pipeline.stage3_extract.ocr_adapter import DocumentIntelligenceClient, OcrAdapter, build_client
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

    `ocr_client` swaps an explicit client into both OcrAdapter and DocxAdapter's embedded-image
    fallback. When it is None the client is *resolved*, not defaulted to the mock: `build_client`
    honours PREMIER_OCR_CLIENT and picks Azure Document Intelligence when credentials are
    configured. Without this the adapters silently mocked every photographed POD even with a
    provisioned resource sitting in `.env` — OCR looked wired and read nothing.

    `UnsupportedFormatAdapter` is deliberately last. It claims the kinds we recognise but do not
    read (.doc, .pptx, .7z, .rar, and anything unidentified), so those end with a stated reason
    on the exception queue instead of being indistinguishable from a coverage gap.
    """
    client = ocr_client or build_client()
    return [
        HtmlAdapter(), PdfAdapter(), DocxAdapter(ocr_client=client),
        OcrAdapter(client=client), ExcelAdapter(), TextAdapter(),
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


def _log_email(conn, email, triaged, folder: str, now: str, *, evidence=None, error=None, note="") -> None:
    """Record this email's verdict, on every exit path including the failure one.

    `triaged` is None when triage never returned — either it raised, or the mail was quarantined
    before it ran. The row is still written: subject, sender, attachment count and the exception
    text are exactly what a person needs to see, and an email that produced nothing at all is
    the case this table exists for.

    Never raises. An audit write must not cost us the mail, and raising from inside the caller's
    `except` block would abort the whole run rather than the one email. That does re-create a
    silent-loss path for the log itself, so the runner asserts row count == emails processed.
    """
    try:
        reason = " — ".join(part for part in (
            note,
            triaged.reason if triaged else "",
            f"{type(error).__name__}: {error}" if error else "",
        ) if part)
        email_log.record(
            conn,
            email_id=email.email_id,
            subject=email.subject or "",
            sender=email.sender_address or "",
            origin_sender=triaged.origin_sender_address if triaged else None,
            email_date=email.received_at or "",
            origin_sent_at=triaged.origin_sent_at if triaged else None,
            notification_type=triaged.notification_type.value if triaged else None,
            category=triaged.category.value if triaged else email_log.CATEGORY_ERROR,
            matched_rule=triaged.matched_rule if triaged else "",
            reason=reason,
            po_hints=", ".join(triaged.extracted_po_hints) if triaged else "",
            shipment_hint=triaged.extracted_shipment_hint if triaged else None,
            notification_number=triaged.notification_number if triaged else None,
            attachment_count=len(email.attachments),
            ocr_attempted=evidence.ocr_attempted if evidence else 0,
            folder=folder,
            error_type=type(error).__name__ if error else None,
            processed_at=now,
        )
    except Exception as e:
        _log(f"email_log write failed for {email.email_id}: {e}")


def _settle(mailbox, conn, email, triaged, folder: str, now: str, **log_kwargs) -> None:
    """The one exit path every email takes: record the verdict, mark it processed, move the mail.

    All three in one helper so no branch can do two of the three. In particular the seen-marker
    is written *here* rather than while fetching, so an email is only ever marked processed after
    it has been — see `state_db.mark_seen`. Order matters: the audit row first, because a folder
    move can fail and an unrecorded email cannot.
    """
    _log_email(conn, email, triaged, folder, now, **log_kwargs)
    state_db.mark_seen(conn, email.email_id, now)
    # The arrival watch may have shown this message on screen seconds after it landed, badged as
    # not yet read. This is the moment that stops being true, and stamping it here — on the one
    # exit path every email takes — is what keeps the badge honest on every branch, including the
    # failure one. A no-op when nothing is watching: corpus runs and mail that predates the watch
    # have no arrival row, and `mark_enriched` deliberately does not create one.
    _mark_arrival_enriched(conn, email.email_id, now)
    _mark_processed_safely(mailbox, email, folder)


def _mark_arrival_enriched(conn, email_id: str, now: str) -> None:
    """Never raises. Clearing a badge must not cost us the email, for the same reason `_log_email`
    swallows: this runs inside the one path that must always complete."""
    try:
        mail_arrivals.mark_enriched(conn, email_id, now)
    except Exception as e:                                     # noqa: BLE001
        _log(f"arrival enrich stamp failed for {email_id}: {e}")


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
    should_stop: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[str, int, int], None]] = None,
) -> int:
    """Runs Stages 1->2->3 end to end over every new email, writes results to
    extracted_records. Returns the number of records staged. One bad email/source is logged
    and skipped, never aborts the rest of the run — see ORCHESTRATOR_DESIGN.md.

    `conn` is required: it names which store this run writes to, and there are two of them
    (Premier's live mail and the test corpus). Defaulting it would mean a caller that forgot
    silently wrote sample data into the live store.

    `should_stop` is the operator's kill switch. It is polled between emails — never mid-email —
    so a stop always lands on a boundary where the previous email is fully settled and committed.

    `on_progress(phase, done, total)` reports where the run has got to, for a console that would
    otherwise show a spinner meaning nothing. Two phases, because they fail differently and take
    very different amounts of time: `"reading"` is the mailbox fetch, which is one long call with
    no interior milestones, and `"processing"` walks the mail one at a time with a real
    denominator. Never let a progress callback break a run — it is decoration, and the run is not.
    """
    if conn is None:
        raise ValueError(
            "conn is required: process_new_mail must be told which store to write to "
            "(state_db.path_for('mailbox') or path_for('sample'))"
        )
    adapters = build_default_adapters(ocr_client=ocr_client)
    evidence_cache = evidence.EvidenceCache()
    staged_count = 0
    released_events: List[DeliveryEvent] = []
    newest_settled: Optional[str] = None
    """Newest `receivedDateTime` this run actually settled, for the watermark at the end."""

    def progress(phase: str, done: int, total: int, note: str = "") -> None:
        if on_progress is None:
            return
        try:
            on_progress(phase, done, total, note)
        except Exception:                                       # noqa: BLE001
            _logger.debug("progress callback failed; continuing", exc_info=True)

    # Before the fetch, not after: against a live mailbox this call is most of the elapsed time,
    # and a console that only starts reporting once it returns spends that whole period looking
    # like nothing is happening.
    progress("reading", 0, 0)
    new_mail = fetch_new_emails(mailbox, conn=conn)
    total = len(new_mail)
    progress("processing", 0, total)

    for index, email in enumerate(new_mail, start=1):
        # Named before the work, not after. One email carrying ten photographed attachments takes
        # most of a minute on its own, and a counter reading "0 of 3" for that whole time is
        # indistinguishable from a stall. The subject is what tells a watcher it is alive.
        progress("processing", index - 1, total, (email.subject or "")[:60])
        if should_stop is not None and should_stop():
            # The previous email is fully settled and committed at this point, so stopping
            # here leaves no half-written record. Unprocessed mail simply stays unseen and is
            # picked up by the next run — `seen_message_ids` is only written on settle.
            _log("stopped by operator — remaining mail left for the next run")
            break

        # Pre-bound so the `except` path below can still log. If triage() itself raises,
        # `triaged` would otherwise be unbound and the audit write would die with a
        # NameError — losing exactly the email that most needed recording.
        triaged = None
        email_evidence = None
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

            # Every branch below logs *before* moving the mail: a folder-move hiccup is
            # survivable, an email that leaves no record is not.
            if oversize:
                _log(f"quarantined {email.email_id}: {oversize}")
                attachment_ledger.close_undispatched(conn, email, _now_iso())
                _settle(mailbox, conn, email, triaged, settings.MAILBOX_FOLDER_QUARANTINE,
                        _now_iso(), evidence=email_evidence, note=f"quarantined: {oversize}")
                continue
            if triaged.category == TriageCategory.ROUTE:
                _log(f"routed, no accumulation: {email.email_id} — {triaged.reason}")
                attachment_ledger.close_undispatched(conn, email, _now_iso())
                _settle(mailbox, conn, email, triaged, settings.MAILBOX_FOLDER_ROUTED,
                        _now_iso(), evidence=email_evidence)
                continue
            if triaged.category == TriageCategory.HIDE:
                # discarded — Stage 1's job is done, nothing more happens with it
                attachment_ledger.close_undispatched(conn, email, _now_iso())
                _settle(mailbox, conn, email, triaged, settings.MAILBOX_FOLDER_HIDDEN,
                        _now_iso(), evidence=email_evidence)
                continue
            released_events.extend(stage2_accumulate.process_triaged_email(conn, triaged, _now_iso()))
            _settle(mailbox, conn, email, triaged, settings.MAILBOX_FOLDER_PROCESSED,
                    _now_iso(), evidence=email_evidence)
        except Exception as e:
            _log(f"failed to triage/accumulate {email.email_id}: {e}")
            _settle(mailbox, conn, email, triaged, settings.MAILBOX_FOLDER_ERRORS,
                    _now_iso(), evidence=email_evidence, error=e)
            continue
        finally:
            # In `finally`, so an email that failed still counts as dealt with. Otherwise the
            # count silently stalls on the one message that went wrong — the exact moment someone
            # is watching the screen to find out what is happening.
            progress("processing", index, total)
            # Every path above ends in `_settle`, including the error one, so reaching here means
            # this email has a verdict in `email_log` and its id in `seen_message_ids`. If `_settle`
            # itself raised, the exception leaves this function and the watermark below is never
            # written — which is what makes "advance only on a clean run" true.
            if email.received_at and (newest_settled is None or email.received_at > newest_settled):
                newest_settled = email.received_at

    released_events.extend(stage2_accumulate.sweep_stale_holds(conn, datetime.now(timezone.utc)))

    if released_events:
        progress("extracting", 0, len(released_events))

    for event_index, event in enumerate(released_events, start=1):
        if should_stop is not None and should_stop():
            _log("stopped by operator — remaining deliveries left for the next run")
            break
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
        finally:
            progress("extracting", event_index, len(released_events))

    # Anything still open belongs to a HOLD whose event has not fired. Settling it here is
    # what keeps attachment_ledger.orphans() meaningful as a leak detector.
    attachment_ledger.close_open_rows(conn, _now_iso())

    # Last, and only here. Reaching this line means the whole run completed without an exception
    # escaping, so the next poll may safely ask the server for a narrower window. A run that died
    # part way leaves the watermark where it was and re-reads the same window — costing a second
    # listing, which is the cheap half of the trade.
    state_db.advance_watermark(conn, newest_settled)

    progress("done", 0, 0)
    return staged_count
