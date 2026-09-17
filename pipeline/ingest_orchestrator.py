import re
from dataclasses import replace
from datetime import datetime, timezone

from rapidfuzz import fuzz
from typing import Callable, Dict, List, Optional, Set, Tuple

from config import settings
from connectors.mailbox import Mailbox
from pipeline import (
    attachment_ledger, dedupe, deliveries_store, email_log, evidence, extracted_records_store,
    mail_arrivals,
    stage2_accumulate, state_db)
from pipeline.models import DeliveryEvent, ExtractedRecord, TriageCategory
from pipeline.stage1_ingest import fetch_new_emails
from pipeline.parsing import confirmation, items, text
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
            source_folder=getattr(email, "source_folder", "") or "",
            not_a_delivery=bool(triaged.not_a_delivery) if triaged else False,
        )
    except Exception as e:
        _log(f"email_log write failed for {email.email_id}: {e}")


def _identify(conn, email, triaged):
    """This message's content fingerprint, and the earlier message it duplicates if any.

    Reads only. The stamp is written by `_settle`, *after* the `email_log` row exists —
    `email_log.record` writes or overwrites the whole row, so anything stamped ahead of it is
    erased, which left every message carrying `fingerprint = NULL` and made the lookup match
    nothing.

    Never raises. A fingerprint is bookkeeping about an email; failing to take one must not cost us
    the email, and treating an error as "this is a duplicate" would silently drop a real delivery —
    so the failure direction is deliberately towards processing it again rather than not at all.
    """
    try:
        value = dedupe.fingerprint(
            subject=email.subject,
            origin_sender=triaged.origin_sender_address if triaged else None,
            origin_sent_at=triaged.origin_sent_at if triaged else None,
            attachment_hashes=dedupe.hashes_for_email(conn, email.email_id))
        earlier = dedupe.find_by_fingerprint(conn, value, exclude_email_id=email.email_id)
        return value, (str(earlier["email_id"]) if earlier is not None else None)
    except Exception as e:                                     # noqa: BLE001
        _log(f"fingerprint failed for {email.email_id}: {e}")
        return "", None


def _settle(mailbox, conn, email, triaged, folder: str, now: str,
            fingerprint: str = "", duplicate_of=None, **log_kwargs) -> None:
    """The one exit path every email takes: record the verdict, mark it processed, move the mail.

    All three in one helper so no branch can do two of the three. In particular the seen-marker
    is written *here* rather than while fetching, so an email is only ever marked processed after
    it has been — see `state_db.mark_seen`. Order matters: the audit row first, because a folder
    move can fail and an unrecorded email cannot.

    The fingerprint is stamped here for the same reason, and *after* the audit row: `email_log`
    writes the whole row, so a stamp applied earlier is overwritten. Doing it on this one path is
    what gives every message a fingerprint rather than only the ones that reached accumulation.
    """
    _log_email(conn, email, triaged, folder, now, **log_kwargs)
    dedupe.stamp(conn, email.email_id, fingerprint, duplicate_of)
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


def _sources_for_delivery_event(event: DeliveryEvent, evidence_cache=None) -> List[ExtractionSource]:
    """Body + every attachment, independently — each bundled email in the event contributes
    its own sources (see ORCHESTRATOR_DESIGN.md).

    Every source carries the event's PO in `only_po`, plus the email's sender and subject. The
    PO filter is what stops a multi-PO document being staged once per PO: notice 939260 covers
    906725 and 907665, releases as two events, and without the filter each event staged all
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
            # The body is where confirmation grids live, and a grid row may only be called received
            # when something outside the grid says so. The orchestrator is the one layer holding
            # both the message and its attachment ledger, so it is where "a proof of delivery names
            # this PO" can be answered; the adapter reads the thread's own sentences itself.
            sources.append(ExtractionSource(
                source_type="body", body_html=email.body_html, body_text=email.body_text,
                receipt_evidence=_receipt_evidence_for(evidence_cache, email.email_id), **common,
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


def _receipt_evidence_for(evidence_cache, email_id: str):
    """The proof-of-delivery verdicts already recorded for this message, as `ReceiptEvidence`.

    `None` when there is no cache to ask — which the grid reader treats as "nothing is proven",
    not as "anything goes". Under-claiming queues a mail for a person; over-claiming posts a
    receiver for goods nobody received, and only one of those is recoverable.
    """
    bundle = evidence_cache.get(email_id) if evidence_cache is not None else None
    if bundle is None:
        return None
    return confirmation.ReceiptEvidence(
        pod_po_numbers=frozenset(bundle.pod_po_numbers or ()),
    )


def _pod_ledger_id_for(evidence_cache, record: ExtractedRecord):
    """The `attachment_ledger` row of the proof of delivery naming this record's purchase order.

    Read off the verdict the extraction pass already wrote, so the POD that identified the
    delivery and the POD linked to its receipt are the same file by construction. `None` when
    nothing on the message read as proof — which is a real and common state, and one the page must
    be able to show as itself.
    """
    if evidence_cache is None or not record.po_number:
        return None
    bundle = evidence_cache.get(record.source_email_id)
    if bundle is None:
        return None
    return bundle.pod_ledger_ids.get(record.po_number)


def _candidate_pos_for_event(event: DeliveryEvent, evidence_cache) -> Set[str]:
    """Every purchase order the messages behind this event could be talking about.

    Both halves matter. The triage hints are what Stage 2 released events for; the purchase orders
    the extracted records themselves name are what the *documents* say, and the two disagree
    exactly when this goes wrong — the receiving report for RR 211373-29 prints two orders and its
    covering mail hinted at one.
    """
    candidates: Set[str] = {event.key.po_number} if event.key.po_number else set()
    for triaged in event.emails or []:
        for hint in getattr(triaged, "extracted_po_hints", None) or []:
            if hint:
                candidates.add(hint)
        email_id = getattr(getattr(triaged, "email", None), "email_id", None)
        bundle = evidence_cache.get(email_id) if (evidence_cache and email_id) else None
        for record in (getattr(bundle, "records", None) or []):
            if record.po_number:
                candidates.add(record.po_number)
    return candidates


def _record_for_event(record: ExtractedRecord, event: DeliveryEvent,
                      candidate_pos: Set[str]) -> Optional[ExtractedRecord]:
    """The record as it should be staged against this delivery, or None if it is not ours.

    Second line of defence behind `only_po`. Adapters that cannot filter by PO themselves — a
    carrier POD names a PO but a free-text pass may name none — still hand back records that must
    not be attributed to the wrong delivery. A record naming a different PO is dropped, because
    that PO has its own event.

    **A record naming no PO is adopted only when the message has exactly one purchase order to
    adopt it.** With two, adoption is a coin toss dressed up as a fact: the first event to run
    claimed every unattributed line, so attribution depended on iteration order. On the receiving
    report for RR 211373-29 that put seven item lines belonging to one order onto another — and
    because the losing order never had an event at all, it received nothing. `tools/reextract.py`
    and `tools/replay_parse.py` have always refused to guess here; this is the same rule, finally
    in the live path. A refused line is left unstaged and surfaces on the queue as a message that
    yielded nothing, which is recoverable; a receipt against the wrong purchase order is not.

    Returns a **copy** when it stamps a PO. The evidence cache hands the same `ExtractedRecord`
    instances to every event, so mutating one here rewrote what the next event would see.
    """
    if record.po_number:
        return record if record.po_number == event.key.po_number else None
    if len(candidate_pos) > 1:
        return None
    return replace(record, po_number=event.key.po_number)


DESCRIPTION_MATCH_THRESHOLD = 85   # rapidfuzz token_set_ratio

# Deciding two rows of ONE document are different items is a different question from the one above
# and uses `parsing/items.py`, which owns that comparison. `record_completion` resolves a purchase
# order line with the same helper, and the two must agree: a pair kept apart here and then resolved
# onto one line afterwards would produce the double receipt this grouping exists to prevent.
#
# The threshold above keeps `token_set_ratio` on purpose — there the question is whether a
# Delivered notice's terser wording names the Inbound's item, and "Custom Accessory Pocket" must
# still find "Accessory Pocket" (100 by set, only 82 by sort).


def _descriptions_conflict(left: ExtractedRecord, right: ExtractedRecord) -> bool:
    """True only when one document names two different items under the same spec.

    Two conditions, and both matter for a different reason.

    **Same document.** Differing wording only means differing lines when it comes from one
    document. Across sources it means nothing: the Authority notice calls line STE-402-LT-B
    "BASE, Floor Lamp 2 (Linen Drum Shade) at Sectional" while the tracker spreadsheet beside it
    calls the same line "Throw Pillow". Splitting on that would stage one physical line as two
    receipts, which is the failure this whole grouping exists to prevent.

    **Both stated.** A Delivered notice frequently names no item at all and must still collapse
    onto the Inbound that does, so a missing description never separates anything.

    Two distinct documents of the same kind share an `extraction_source` and so are treated as one
    here. That errs toward routing to a person rather than toward silently merging two lines.
    """
    if (left.extraction_source or "") != (right.extraction_source or ""):
        return False
    if not items.normalise(left.item_description) or not items.normalise(right.item_description):
        return False
    return not items.same_item(left.item_description, right.item_description)


def _place_in_spec_group(groups: Dict[Tuple, List[ExtractedRecord]],
                         record: ExtractedRecord, spec: str) -> None:
    """File a record under its spec, splitting the spec when descriptions say it is several lines.

    A spec code is not always a line identity. PO 907514 carries **29 lines that all read
    `LOB-900-SI`** — a signage package whose lines differ only by description ("Restroom Door
    Placard", "Common Room ID", "Exit"). Grouping on `(PO, spec)` alone put all 23 delivered items
    in one group, and `reconcile_cross_source_duplicates` — correctly refusing to choose between 12
    different quantities for what it believed was one line — flagged every one of them
    `+quantity_conflict`. Those 23, plus 21 more on PO 907249, were 35% of every record in the
    store and the largest single category of manual work.

    Measured against the mirror afterwards, 22 of those 23 matched a distinct Spitfire line by
    description at a score of 100 *and* agreed with that line's ordered quantity. The refusal was
    right; the grouping that provoked it was not.

    A record joins the first sub-group holding nothing it contradicts, so records that genuinely
    describe one line still meet. A record naming no item joins the first sub-group — it cannot
    discriminate, and collapsing is the behaviour worth preserving when there is no evidence
    either way.
    """
    ordinal = 0
    while True:
        key = ("spec", record.po_number, spec, ordinal)
        members = groups.get(key)
        if members is None:
            groups[key] = [record]
            return
        if not any(_descriptions_conflict(record, member) for member in members):
            members.append(record)
            return
        ordinal += 1


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
            _place_in_spec_group(groups, record, spec)
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
        if _are_siblings_of_one_grid(group):
            # Rows of one grid, not claims about one line. An Atlas receiving report lists
            # GR-905-EQ three times — 210, 75 and 65 — because the item arrived on three skids;
            # the fourth row is STE-901-EQ at 23, and the four sum to 373, exactly what the packing
            # slip bound in behind them states was delivered. Reading those three numbers as a
            # disagreement and flagging all of them was wrong twice over: nothing disagreed, and it
            # put three rows in front of a person who had nothing to decide. 58 of the 66 flagged
            # groups in the live store are one document like this.
            #
            # They are kept apart rather than summed. Whether three skids of one spec are one
            # receipt line or three is a question about the receipt, not about the document, and it
            # is settled downstream where the purchase-order line is known.
            result.extend(group)
            continue
        quantities = {r.quantity_received for r in group if r.quantity_received is not None}
        if len(quantities) <= 1:
            best = max(group, key=lambda r: r.extraction_confidence)
            discarded = [r for r in group if r is not best]
            if discarded:
                _log(f"discarded {len(discarded)} duplicate record(s) for {key}, kept source={best.extraction_source}")
            result.append(best)
        else:
            # Disagreement. Where one side is delivery evidence and the other is a body grid, the
            # evidence wins — but the row is still flagged, because the disagreement is the whole
            # point. PO 910634 was staged at 196 from Premier's request table while the carrier's
            # POD and the Authority notice both said 202; the thread itself explains the gap as
            # "6 yards of overage". Keeping 196 silently is how a receiver goes out wrong, and
            # keeping both rows puts two receipts on the page for one delivery.
            evidenced = [r for r in group if _is_delivery_evidence(r)]
            grid_only = [r for r in group if _is_confirmation_grid(r)]
            if evidenced and grid_only:
                best = max(evidenced, key=lambda r: r.extraction_confidence)
                others = sorted({r.quantity_received for r in grid_only
                                 if r.quantity_received is not None})
                _log(f"quantity conflict for {key}: evidence says {best.quantity_received}, "
                     f"grid says {others} — keeping the evidence, flagged for review")
                best.extraction_source = best.extraction_source + "+quantity_conflict"
                best.comments = "; ".join(part for part in (
                    best.comments,
                    f"quantity conflict: the request grid states {', '.join(str(q) for q in others)}"
                ) if part)
                result.append(best)
                continue
            _log(f"quantity conflict across sources for {key}: {sorted(quantities)} — routing all, not guessing")
            for r in group:
                # Appended at most once. `EvidenceCache.records_for` hands back the *same* record
                # objects every time it is asked, so an email whose mail fires several delivery
                # events is reconciled several times over one object — and an unguarded concatenation
                # wrote `ocr+quantity_conflict+quantity_conflict+quantity_conflict+quantity_conflict`
                # into the live store. Every reader uses `in`, so it never changed a verdict, but it
                # is a record of how many times a thing happened that only happened once.
                if "quantity_conflict" not in (r.extraction_source or ""):
                    r.extraction_source = r.extraction_source + "+quantity_conflict"
            result.extend(group)
    return _fold_pod_only_records(result)


def _are_siblings_of_one_grid(group: List[ExtractedRecord]) -> bool:
    """Whether these records are rows of a single grid on a single document.

    Two records disagree only if they are two *claims*. Rows of one table are not claims about
    each other — they are a list — and `reconcile_cross_source_duplicates` exists to settle
    disagreement between sources, not to audit a document against itself.

    `source_ledger_id` is what makes the distinction available: it names the attachment a record
    was read from, so records sharing one came off the same read of the same file. A group where
    it is unknown (a body grid, a hand-built record) is not treated as siblings — silence is not
    evidence of sameness, and the cautious answer is the existing one.
    """
    ledger_ids = {r.source_ledger_id for r in group}
    return len(ledger_ids) == 1 and None not in ledger_ids


def _is_pod_only(record: ExtractedRecord) -> bool:
    """A record that proves a delivery happened but names no line on it.

    `records_from_pod` builds exactly this: a POD states a date, a signature, a carrier and a
    tracking number, and deliberately no spec and no quantity, because it is evidence about a
    *delivery* and not about which purchase-order lines were on the truck.
    """
    return (record.spec_code is None and record.quantity_received is None
            and not (record.item_description or "").strip())


_POD_EVIDENCE_FIELDS = ("pod_stated_date", "received_by", "carrier_name", "tracking_number",
                        "delivery_location")


def _fold_pod_only_records(records: List[ExtractedRecord]) -> List[ExtractedRecord]:
    """Move a POD's evidence onto the lines it is evidence *for*, instead of staging it beside them.

    A notification email carrying its own POD produced two records for one delivery: the line
    (spec, quantity, description — complete, postable) and a POD-only twin with those three fields
    null. `_group_records` keys the first on its spec and the second on an empty description, so
    they never met, and the twin could never be completed from the document it came from.

    The visible cost was worse than a spare row. The twin's gap list —
    `missing: spec code, description, quantity` — was rendered against the *message* on the manual
    queue, so PO 908705 read as unparsed on screen while its real record sat in Spitfire as a
    posted receipt. Somebody looking at that page would reasonably conclude the parser cannot read
    an Authority line table, which it does perfectly.

    So the evidence fields are filled into the lines that lack them and the twin is dropped —
    **only** when a line for the same PO actually exists. A POD arriving with no line to attach to
    is the sole evidence there is and still stages on its own.
    """
    lines_by_po: Dict[str, List[ExtractedRecord]] = {}
    for record in records:
        if not _is_pod_only(record):
            lines_by_po.setdefault(record.po_number or "", []).append(record)

    kept: List[ExtractedRecord] = []
    for record in records:
        lines = lines_by_po.get(record.po_number or "") if _is_pod_only(record) else None
        if not lines:
            kept.append(record)
            continue
        for line in lines:
            for field_name in _POD_EVIDENCE_FIELDS:
                if not getattr(line, field_name, None):
                    setattr(line, field_name, getattr(record, field_name, None))
        _log(f"folded POD evidence for {record.po_number} into {len(lines)} line record(s) "
             f"instead of staging a line-less twin")
    return kept


_DELIVERY_EVIDENCE_SOURCES = ("authority_", "pdf:carrier_pod", "ocr")
"""Sources that report what a carrier or the originating warehouse actually handled."""


def _is_delivery_evidence(record: ExtractedRecord) -> bool:
    source = (record.extraction_source or "").lower()
    return any(source.startswith(prefix) for prefix in _DELIVERY_EVIDENCE_SOURCES)


def _is_confirmation_grid(record: ExtractedRecord) -> bool:
    """A row read out of a request/tracker grid — what somebody *wrote down*, not what shipped.

    Deliberately narrow. Only this loses to delivery evidence, because only this is known to
    carry an ordered quantity in a column a reader takes for a received one. A packing slip, a
    generic table, an OCR'd BOL are all real shipping paperwork, and where one of those disagrees
    with a notice the difference is a genuine fact — overage, a split part shipment — that must be
    staged and looked at, never resolved by preferring one document a priori.
    """
    return "confirmation_grid" in (record.extraction_source or "").lower()


def _fallback_delivery_date(conn, email_id: str):
    """`(date, source)` to stand in for a delivery date the document never stated, else `None`.

    **Premier's rule, stated 2026-09-03: if the document gives a delivery date, that is the date.
    If it does not, the date the mail was received is the date.** Nothing else. The caller applies
    the first half by only reaching here when `pod_stated_date` is empty and no POD is linked; this
    function is the second half.

    `pod_stated_date` is one of the five fields `completeness` requires and the only one no other
    system holds, so a document that omits it strands an otherwise complete receipt. The mail
    bounds when the delivery happened: it cannot have been reported before it occurred.

    This previously preferred `origin_sent_at` — when the message was *written* — on the reasoning
    that a thread forwarded to Premier days later still describes the delivery its author saw, so
    the received date would push that delivery late. It is a good argument and it is not the rule
    we were given; on 48 of 1,642 messages the two fall on different days, and on every one of
    those it stamped a date Premier did not ask for. Do not reinstate it without Premier changing
    the rule: the point of a stated business rule is that it does not quietly vary by message.

    This is an estimate standing in for a fact, so the caller records which one it used
    (`pod_source`), and posting still wants a linked proof of delivery, a `pod_waived_by` waiver,
    or body evidence — a fallback date cannot smuggle anything into Spitfire on its own.
    """
    row = conn.execute(
        "SELECT COALESCE(email_date, '') FROM email_log WHERE email_id = ?", (email_id,)).fetchone()
    if not row:
        return None
    received = (row[0] or "").strip()
    if received:
        return received[:10], "email_received_date"
    return None


def stage_records(
    conn,
    records: List[ExtractedRecord],
    *,
    po_number: str,
    delivery_ref: str,
    delivery_rung: str,
    now: str,
    pod_ledger_id_of=None,
) -> List[int]:
    """Stage extracted records against the delivery they describe. Returns the new record ids.

    The staging half of `process_new_mail`, lifted out so anything that re-reads an attachment
    after the fact — `tools/reextract.py` recovering from an OCR outage — stages through exactly
    these guards rather than its own copy of them. A second implementation would drift, and the
    two rules below are the ones that stop a delivery being counted twice.

    `pod_ledger_id_of` resolves the proof-of-delivery row for a record; the orchestrator answers it
    from the evidence cache it already built, a re-read answers it from the ledger. `None` means
    nothing on the message read as proof, which is a real and common state.
    """
    delivery_id = None
    staged_ids: List[int] = []

    # Records read from a less accurate copy of a document another attachment states better. They
    # are kept — the file may still be the proof of delivery, and what it said is evidence — but
    # they are not work, so they never reach reconciliation, never claim a delivery key, and never
    # create a delivery row. Reconciling them against the good read is precisely the mistake this
    # avoids: two copies of one report disagree in the way only OCR disagrees, and every such
    # disagreement would come back as a quantity conflict for a person to settle by hand.
    superseded = [r for r in records if r.superseded_by_ledger_id is not None]
    records = [r for r in records if r.superseded_by_ledger_id is None]

    for record in superseded:
        if dedupe.is_handled_manually(conn, record.source_email_id):
            continue
        extracted_records_store.write_pending(
            conn, record, now, status=extracted_records_store.SUPERSEDED)

    for record in reconcile_cross_source_duplicates(records):
        # A person already built a record from this message, so staging more from it would
        # put two rows on the page for one delivery. Scoped to the one message — never to
        # its thread or its purchase order, because the next mail on the same thread may be
        # a genuinely separate delivery and suppressing that would hide a real receipt.
        if dedupe.is_handled_manually(conn, record.source_email_id):
            _log(f"skipped {record.po_number}: a person already recorded "
                 f"{record.source_email_id} by hand")
            continue

        # Point the record at the file that proves it, where one exists. Records used to
        # carry a POD's date and signature with no link back to the document they came
        # from, which made "backed by a carrier's proof" and "nobody has confirmed this"
        # look identical on the page — records 131-133 all showed a POD date and all had
        # `pod_ledger_id` null, and only one of the three had any proof at all.
        pod_fields: Dict[str, object] = {}
        pod_ledger_id = pod_ledger_id_of(record) if pod_ledger_id_of is not None else None
        if pod_ledger_id is not None:
            pod_fields["pod_ledger_id"] = pod_ledger_id
            pod_fields["pod_source"] = "attachment"

        # Only once the document has had its say, and never over a real proof of delivery: a
        # linked POD already owns `pod_source`, and overwriting it would lose which file proved
        # the receipt.
        #
        # **Before the delivery key is minted, not after.** This line mutates
        # `record.pod_stated_date`, which `dedupe.key_for_row` hashes. Minting the key first and
        # filling the date in afterwards wrote a row whose stored `delivery_key` described a
        # version of itself that no longer existed — so the guard below could never match it
        # again. Combined with one record instance reaching this loop twice, that staged 160
        # duplicate records across 43 purchase orders, every one of them a line that then
        # collided with its own twin in `spitfire_post._refuse_line_collisions` and could never
        # post. The key must be the last thing computed before the write.
        if not record.pod_stated_date and pod_ledger_id is None:
            fallback = _fallback_delivery_date(conn, record.source_email_id)
            if fallback:
                record.pod_stated_date, pod_fields["pod_source"] = fallback

        # And the same delivery read twice from two different messages. Deliberately after
        # `reconcile_cross_source_duplicates`, which dedupes *within* one email; this asks
        # the store, so it also catches a re-extraction after a reprocess.
        key = dedupe.key_for_row(record)
        existing = dedupe.find_by_delivery_key(conn, key)
        if existing:
            _log(f"skipped {record.po_number}: already staged as record "
                 f"#{existing[0]['id']}")
            continue

        if delivery_id is None:
            # Deferred to the first line that actually survives the guards above. Creating
            # it eagerly would leave an empty delivery behind every time an event turned
            # out to be entirely duplicate — a row asserting goods arrived, with nothing
            # under it saying what.
            delivery_id = deliveries_store.upsert(
                conn, po_number=po_number, delivery_ref=delivery_ref,
                delivery_rung=delivery_rung, now=now, facts=record)

        extra = {"delivery_key": key, "delivery_id": delivery_id, **pod_fields}
        staged_ids.append(extracted_records_store.write_pending(conn, record, now, extra=extra))

    if delivery_id is not None:
        _log(f"delivery #{delivery_id}: PO {po_number} "
             f"({delivery_ref}) carrying {len(staged_ids)} item line(s)")
    return staged_ids


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

            # Computed once for every message, whatever triage decided, so the fingerprint column
            # is answerable across the whole mailbox rather than only for delivery mail. It is only
            # *acted on* in the accumulate branch below — chatter and routed mail never reach the
            # step that would double-count a delivery, so suppressing them would buy nothing and
            # would hide a second copy of something a person still needs to read.
            fingerprint, duplicate_of = _identify(conn, email, triaged)
            settle = dict(fingerprint=fingerprint, duplicate_of=duplicate_of)

            # Every branch below logs *before* moving the mail: a folder-move hiccup is
            # survivable, an email that leaves no record is not.
            if oversize:
                _log(f"quarantined {email.email_id}: {oversize}")
                attachment_ledger.close_undispatched(conn, email, _now_iso())
                _settle(mailbox, conn, email, triaged, settings.MAILBOX_FOLDER_QUARANTINE,
                        _now_iso(), evidence=email_evidence, note=f"quarantined: {oversize}",
                        **settle)
                continue
            if triaged.category == TriageCategory.ROUTE:
                _log(f"routed, no accumulation: {email.email_id} — {triaged.reason}")
                attachment_ledger.close_undispatched(conn, email, _now_iso())
                _settle(mailbox, conn, email, triaged, settings.MAILBOX_FOLDER_ROUTED,
                        _now_iso(), evidence=email_evidence, **settle)
                continue
            if triaged.category == TriageCategory.HIDE:
                # discarded — Stage 1's job is done, nothing more happens with it
                attachment_ledger.close_undispatched(conn, email, _now_iso())
                _settle(mailbox, conn, email, triaged, settings.MAILBOX_FOLDER_HIDDEN,
                        _now_iso(), evidence=email_evidence, **settle)
                continue
            # Has this exact notification already been through, under a different message id?
            # `seen_message_ids` cannot answer that — a vendor re-send and a second expeditor's
            # forward each get their own `internetMessageId` — so the same delivery accumulates
            # twice and stages two records. Asked here, after triage (which is what recovers the
            # originating sender and send time from the quoted headers) and before accumulation,
            # which is the step that would double-count it.
            if duplicate_of is not None:
                _log(f"duplicate of {duplicate_of}: {email.email_id} — not accumulated")
                attachment_ledger.close_undispatched(conn, email, _now_iso())
                _settle(mailbox, conn, email, triaged, settings.MAILBOX_FOLDER_PROCESSED,
                        _now_iso(), evidence=email_evidence,
                        note="already received as another message; not counted twice", **settle)
                continue

            released_events.extend(stage2_accumulate.process_triaged_email(conn, triaged, _now_iso()))
            _settle(mailbox, conn, email, triaged, settings.MAILBOX_FOLDER_PROCESSED,
                    _now_iso(), evidence=email_evidence, **settle)
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
            # `_sources_for_delivery_event` yields one source per attachment, but
            # `evidence_cache.records_for` is keyed on the *email* — it ignores `src` entirely
            # and hands back the same `ExtractedRecord` instances for every attachment on that
            # message. Appending them once per source staged each line as many times as the
            # message had attachments. Identity, not equality: these are literally the same
            # objects, and two genuine rows that merely look alike must still both be kept.
            seen_records: Set[int] = set()
            # Asked once per event: how many purchase orders could an unattributed line on this
            # message belong to? More than one and nothing is adopted — see `_record_for_event`.
            candidate_pos = _candidate_pos_for_event(event, evidence_cache)
            for src in _sources_for_delivery_event(event, evidence_cache):
                if src.source_type == "attachment":
                    # Already read during the evidence pass, before triage — re-reading it
                    # would duplicate the work and, for images, the OCR spend.
                    produced = evidence_cache.records_for(src.source_email_id, event.key.po_number)
                else:
                    produced = dispatch.dispatch_source(
                        conn, src, adapters, budget=containers.Budget.fresh(), now=_now_iso(),
                    )
                for record in produced:
                    # Identity is checked on the instance the cache handed back, before
                    # `_record_for_event` may hand us a copy of it — otherwise a copied record
                    # carries a new id and the same line stages once per attachment again.
                    if id(record) in seen_records:
                        continue
                    seen_records.add(id(record))
                    mine = _record_for_event(record, event, candidate_pos)
                    if mine is None:
                        continue
                    mine.shipment_number = mine.shipment_number or event.key.shipment_number
                    raw_records.append(mine)

            # The delivery these lines arrived on, created once per event rather than implied N
            # times by N rows that happen to share a purchase order. `event.key` already carries
            # the ref Stage 2 accumulated under, so the parent and the accumulation agree on what
            # "this delivery" means by construction rather than by a second guess.
            staged_ids = stage_records(
                conn, raw_records,
                po_number=event.key.po_number,
                delivery_ref=event.key.delivery_ref,
                delivery_rung=event.key.delivery_rung,
                now=_now_iso(),
                pod_ledger_id_of=lambda r: _pod_ledger_id_for(evidence_cache, r),
            )
            staged_count += len(staged_ids)
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
