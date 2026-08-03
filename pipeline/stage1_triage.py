"""Stage 1 — Triage. First matching rule wins; every email leaves with a category and a reason.

Rewritten against the real June corpus. The previous version keyed on `sender_domain` alone,
which the corpus defeats three separate ways:

* Every sample arrives forwarded from `premierpm.com`, so the domain never identifies the
  originator — that has to be recovered from the quoted chain first.
* The receiver trigger (`warehousing@`) and the not-a-receiver notice (`routing@`) share a
  domain, so only the local part separates them.
* The weekly Purchase Order Status Report arrives from `warehousing@` too, so only the
  *subject* separates it from the trigger. A domain rule routes a summary report straight into
  the receiver path.

Hence the rule key is the pair **(sender local part, subject grammar)**, resolved on the origin
hop rather than the delivered envelope.

Categories: HIDE discards, SURFACE fires a delivery event, HOLD waits for a partner notice or
the grace sweep, ROUTE hands it to a person.
"""

import re
from typing import List, Optional, Tuple

from config import settings
from pipeline.models import NotificationType, RawEmail, TriageCategory, TriagedEmail
from pipeline.parsing import boilerplate, sniff, tables, text, thread, tokens
from pipeline.vendors import authority

# Class E: reads exactly like delivery mail but must never produce a receiver. From the corpus:
# "RES-100b-EQ for PO #208453 was lost by the warehouse... The new PO for the replacement is
# PO #211400" (finding G8).
LOSS_OR_CLAIM_RE = re.compile(
    r"\b(?:lost\s+by|was\s+lost|item\s+lost|missing\s+item|damaged\s+in\s+transit|"
    r"claim\s+(?:filed|number|#)|credit\s+memo|replacement\s+PO|short\s+ship(?:ped|ment)|"
    r"freight\s+claim|concealed\s+damage)\b",
    re.IGNORECASE,
)

CANCELLATION_RE = re.compile(settings.CANCELLATION_KEYWORDS_REGEX, re.IGNORECASE)

# Header signatures of the request/tracker tables Premier sends to properties and vendors. Their
# presence is what makes a thread a confirmation thread — and, crucially, the tables carry the
# PO/spec/qty that the human's one-line reply does not.
CONFIRMATION_TABLE_HEADERS = ["Description of Item", "SPEC # or Phase Code", "UOM", "Qty"]
TRACKER_TABLE_HEADERS = ["Vendor", "PO#", "Spec#", "QTY", "Item Description"]


def _thread_of(email: RawEmail):
    body = text.body_text_of(email)
    parsed = thread.split_thread(body, email.sender_address, email.subject)
    origin = thread.resolve_origin(email.sender_address, email.subject, parsed)
    return body, parsed, origin


def _po_hints_across_thread(email: RawEmail, body: str) -> List[str]:
    """Labelled POs from the subject and *every* hop, plus any PO column in a request table.

    Reading the whole thread is not optional: a property reply is often *"Yes ma'am, this was
    received!"* with the PO living in a table quoted three hops down (finding G4). Scanning only
    the newest hop finds nothing and the email falls through to the unknown rule.
    """
    hints: List[str] = []
    for candidate in tokens.find_po_numbers(f"{email.subject}\n{body}"):
        if candidate not in hints:
            hints.append(candidate)

    all_tables = tables.extract_tables(email.body_html)
    for header_set in (CONFIRMATION_TABLE_HEADERS, TRACKER_TABLE_HEADERS):
        for grid in tables.find_all_grids(all_tables, header_set):
            po_column = tables.column_index(grid.header, ["P.O.#", "PO#", "PO #", "Purchase Order"])
            if po_column is None:
                continue
            for row in grid.body_rows:
                for candidate in tokens.parse_po_list(tables.cell(row, po_column)):
                    if candidate not in hints:
                        hints.append(candidate)
    return hints


def _has_confirmation_table(email: RawEmail) -> bool:
    all_tables = tables.extract_tables(email.body_html)
    return bool(tables.find_all_grids(all_tables, CONFIRMATION_TABLE_HEADERS)
                or tables.find_all_grids(all_tables, TRACKER_TABLE_HEADERS))


def _newest_hop_word_count(parsed) -> int:
    """Words in the newest hop only, boilerplate removed.

    Both halves matter. Measuring the whole thread makes every reply long; leaving the
    signature and the six repeated confidentiality notices in makes a four-word reply measure
    in the hundreds.
    """
    newest = parsed.newest
    return boilerplate.significant_word_count(newest.body) if newest else 0


_DELIVERY_THREAD_RE = re.compile(
    r"\b(deliver(?:y|ed|ies)|receipt|receiv(?:e|ed|ing)|confirm(?:ation)?|BOL|packing\s+slip|POD|pallet|shipment)\b",
    re.IGNORECASE,
)

_READABLE_KINDS = {sniff.KIND_XLSX, sniff.KIND_DOCX, sniff.KIND_PDF, sniff.KIND_HTML, sniff.KIND_MSG}


def _attachment_kinds(email: RawEmail) -> Tuple[set, int]:
    """(machine-readable kinds present, count of photo-sized images).

    Decorative attachments are already dropped at ingest, so anything still here is either real
    evidence or something no adapter will claim — either way triage should know about it.
    """
    readable, image_count = set(), 0
    for attachment in email.attachments:
        result = sniff.sniff(attachment.content_bytes, attachment.filename, attachment.content_type or "")
        if result.kind in _READABLE_KINDS:
            readable.add(result.kind)
        elif result.is_image:
            image_count += 1
    return readable, image_count


def _looks_like_delivery_thread(subject: str, body: str) -> bool:
    """Guard on the rules that act on attachments alone, so an unrelated thread that happens to
    carry a spreadsheet is not pulled into the receiving pipeline."""
    return bool(_DELIVERY_THREAD_RE.search(f"{subject}\n{body[:4000]}"))


def _decide_authority(
    email: RawEmail, notice: authority.AuthorityNotice
) -> Tuple[NotificationType, TriageCategory, str, str]:
    if notice.kind == authority.NoticeKind.STATUS_REPORT:
        return (NotificationType.WAREHOUSE_STATUS_REPORT, TriageCategory.HIDE,
                "rule_1c_authority_status_report",
                "periodic PO status summary from the same sender as the receiver trigger — not a delivery event")

    if notice.kind == authority.NoticeKind.INBOUND:
        return (NotificationType.WAREHOUSE_INBOUND, TriageCategory.SURFACE,
                "rule_1a_authority_inbound",
                "warehouse booked the goods in — the receiver trigger")

    # Class B. Held rather than surfaced: the carrier delivering to the warehouse door is not a
    # receiving event, and the Inbound that follows is. Holding (instead of hiding) means a
    # shipment that only ever produces a Delivered notice still reaches a human via the grace
    # sweep instead of vanishing — the corpus contains exactly that case, annotated
    # "straightforward, WH rec'd", and Premier owes us a written rule on it.
    return (NotificationType.DELIVERED_SHIPPED, TriageCategory.HOLD,
            "rule_1b_authority_delivered",
            "carrier delivered to the warehouse; waiting on the matching Inbound notification")


def triage(email: RawEmail, evidence=None) -> TriagedEmail:
    """Decide what this email is.

    `evidence` is an `EmailEvidence` bundle from `pipeline/evidence.py` — what the attachments
    actually contained, parsed before this ran. It is optional so every existing caller and test
    keeps working, but the orchestrator always supplies it, and it is what turns two of the
    corpus's rules from guesses into readings: a thread whose POs exist only inside an attached
    spreadsheet now has those POs here, and a photographed-evidence email is only routed for OCR
    after OCR has genuinely been tried or declined.
    """
    body, parsed, origin = _thread_of(email)
    origin_subject = origin.subject or email.subject
    po_hints = _po_hints_across_thread(email, body)

    # POs read out of the attachments rank alongside those found in the text — an attachment is
    # not weaker evidence, it is usually the only evidence.
    for candidate in getattr(evidence, "po_numbers", []) or []:
        if candidate not in po_hints:
            po_hints.append(candidate)

    def result(
        notification_type: NotificationType,
        category: TriageCategory,
        matched_rule: str,
        reason: str = "",
        shipment_hint: Optional[str] = None,
        notification_number: Optional[str] = None,
    ) -> TriagedEmail:
        return TriagedEmail(
            email=email, notification_type=notification_type, category=category,
            matched_rule=matched_rule, extracted_po_hints=po_hints,
            extracted_shipment_hint=shipment_hint, reason=reason,
            origin_sender_address=origin.sender_address or email.sender_address,
            notification_number=notification_number,
        )

    # Rule 0 — cancellations and loss/claim threads leave first. Both read like delivery mail,
    # and both are out of scope; letting either reach the vendor parsers risks a receiver for
    # goods that were cancelled or never arrived.
    newest_text = boilerplate.strip_boilerplate(parsed.newest.body if parsed.newest else "")
    subject_and_newest = f"{email.subject}\n{newest_text}"

    if CANCELLATION_RE.search(subject_and_newest):
        return result(NotificationType.ORDER_CANCELLATION, TriageCategory.ROUTE,
                      "rule_0a_order_cancellation",
                      "order cancellation notice — needs a manual PO update in Spitfire, not a delivery event")

    if LOSS_OR_CLAIM_RE.search(f"{email.subject}\n{body}"):
        return result(NotificationType.LOSS_OR_CLAIM, TriageCategory.ROUTE,
                      "rule_0b_loss_or_claim",
                      "lost / damaged / claim / replacement-PO thread — out of Phase 1 scope, needs a person")

    # Rule 1 — Authority Logistics, by (local part, subject grammar).
    notice = authority.parse_authority_notice(origin.sender_address, origin_subject, email.body_html, body)
    if notice is not None:
        notification_type, category, matched_rule, reason = _decide_authority(email, notice)
        notice_pos = notice.po_numbers
        if notice_pos:
            # The vendor grammar is authoritative over the generic scan — it knows which of the
            # six-digit numbers in this mail are POs and which are the inbound number.
            po_hints = notice_pos
        return result(notification_type, category, matched_rule, reason,
                      shipment_hint=notice.shipment_number,
                      notification_number=notice.notice_number)

    # Rule 2 — carrier status mail (FedEx/UPS "shipped", "out for delivery"). An intermediate
    # movement notice, never a receiving event.
    if origin.sender_domain in settings.FREIGHT_SENDER_DOMAINS:
        return result(NotificationType.DELIVERED_SHIPPED, TriageCategory.HIDE,
                      "rule_2_freight_status",
                      "intermediate carrier status notice, not a receiving event")

    # Rule 3 — vendor/outside-warehouse confirmation (Class D). Recognised by the request table
    # Premier sent, not by the reply text, which is free-form and often a partial confirmation
    # ("we have only received the Sheer Fabric").
    if origin.sender_domain in settings.VENDOR_CONFIRMATION_DOMAINS and (po_hints or _has_confirmation_table(email)):
        return result(NotificationType.VENDOR_CONFIRMATION, TriageCategory.HOLD,
                      "rule_3_vendor_confirmation",
                      "outside-warehouse receipt verification — hold for the confirmation to be reconciled")

    # Rule 4 — property confirmation (Class C): a short human reply on a thread that carries a
    # confirmation table or a PO reference.
    if (po_hints or _has_confirmation_table(email)) and _newest_hop_word_count(parsed) <= settings.PROPERTY_REPLY_MAX_WORDS:
        return result(NotificationType.PROPERTY_CONFIRMATION, TriageCategory.HOLD,
                      "rule_4_property_reply",
                      "short human confirmation on a delivery thread — hold pending reconciliation")

    # Rule 5 — the identifying data is in an attachment, not the text. Two real corpus threads
    # carry no PO anywhere in their bodies because the POs live in an attached tracker
    # (`Cameo Receivers.xlsx`, columns Vendor/PO#/Spec#/QTY/Item Description). Falling through to
    # the unknown rule routes a fully-machine-readable spreadsheet to a human for retyping.
    readable, images = _attachment_kinds(email)
    if readable and _looks_like_delivery_thread(email.subject, body):
        detail = f"({', '.join(sorted(readable))})"
        if getattr(evidence, "has_records", False):
            # Read, not assumed. The reason string carries what was actually found, so the
            # operator sees evidence rather than a heuristic.
            detail = (f"({', '.join(sorted(readable))}) — read {len(evidence.records)} line(s)"
                      f" covering PO(s) {', '.join(evidence.po_numbers) or 'none stated'}")
        return result(NotificationType.PROPERTY_CONFIRMATION, TriageCategory.HOLD,
                      "rule_5a_tracker_attachment",
                      f"delivery thread whose identifying data is in an attachment {detail}")

    # Rule 5b — photographed PODs and BOLs. Reached only once the evidence pass has already
    # tried to read them, so this says "OCR found nothing", not "we never looked".
    if images and _looks_like_delivery_thread(email.subject, body):
        attempted = getattr(evidence, "ocr_attempted", 0)
        how = (f"OCR read {attempted} of them and recovered no PO"
               if attempted else "no OCR client is configured")
        return result(NotificationType.UNKNOWN, TriageCategory.ROUTE, "rule_5b_image_only_evidence",
                      f"delivery evidence is {images} photographed attachment(s) with no text layer — "
                      f"{how}; needs OCR or a person")

    # Rule 6 — a PO is referenced but nothing above recognised the shape. Better routed to a
    # person than guessed at.
    if po_hints:
        return result(NotificationType.UNKNOWN, TriageCategory.ROUTE, "rule_6_unrecognized_with_po",
                      "PO referenced but the message shape is unrecognized — needs a person")

    return result(NotificationType.UNKNOWN, TriageCategory.ROUTE, "rule_7_unknown",
                  "no PO reference found anywhere in the thread — cannot resolve automatically")
