import re
from typing import List, Optional

from bs4 import BeautifulSoup

from config import settings
from pipeline.models import NotificationType, RawEmail, TriageCategory, TriagedEmail

PO_TOKEN_RE = re.compile(settings.PO_TOKEN_REGEX)
SHIPMENT_TOKEN_RE = re.compile(settings.SHIPMENT_TOKEN_REGEX, re.IGNORECASE)
STATUS_WORDS_RE = re.compile(r"\b(delivered|shipped|picked up)\b", re.IGNORECASE)
INVENTORY_WORDS_RE = re.compile(r"\b(inventory|warehouse receipt)\b", re.IGNORECASE)
CANCELLATION_RE = re.compile(settings.CANCELLATION_KEYWORDS_REGEX, re.IGNORECASE)


def _extract_po_hints(email: RawEmail) -> List[str]:
    text = " ".join(t for t in [email.subject, email.body_text, email.body_html] if t)
    seen = []
    for match in PO_TOKEN_RE.findall(text):
        if match not in seen:
            seen.append(match)
    return seen


def _extract_shipment_hint(email: RawEmail) -> Optional[str]:
    text = " ".join(t for t in [email.subject, email.body_text, email.body_html] if t)
    match = SHIPMENT_TOKEN_RE.search(text)
    return match.group(1) if match else None


def _has_table(body_html: Optional[str]) -> bool:
    if not body_html:
        return False
    return BeautifulSoup(body_html, "lxml").find("table") is not None


def _word_count(email: RawEmail) -> int:
    if email.body_text:
        text = email.body_text
    elif email.body_html:
        text = BeautifulSoup(email.body_html, "lxml").get_text()
    else:
        text = ""
    return len(text.split())


def triage(email: RawEmail) -> TriagedEmail:
    """The Stage 1 rule chain — first match wins. See BuildPlan/STAGE_1_INGEST_AND_TRIAGE.md.

    Note: the vendor-confirmation check runs ahead of the generic property-reply fallback (a
    correction versus the doc's original 4-then-5 prose ordering) — otherwise a known vendor
    domain would always be caught by the generic "short reply" rule first and never reach its
    own, more specific rule. Rule numbers in `matched_rule` keep the doc's original numbering
    for traceability even though the code checks them in this corrected order.
    """
    po_hints = _extract_po_hints(email)
    shipment_hint = _extract_shipment_hint(email)
    combined_text = " ".join(t for t in [email.subject, email.body_text] if t)

    if CANCELLATION_RE.search(combined_text):
        return TriagedEmail(
            email=email, notification_type=NotificationType.ORDER_CANCELLATION,
            category=TriageCategory.ROUTE, matched_rule="rule_0_order_cancellation",
            extracted_po_hints=po_hints, extracted_shipment_hint=shipment_hint,
            reason="order cancellation notice — requires manual PO update in Spitfire, not a delivery event",
        )

    if email.sender_domain in settings.WAREHOUSE_SENDER_DOMAINS and _has_table(email.body_html):
        return TriagedEmail(
            email=email, notification_type=NotificationType.WAREHOUSE_INBOUND,
            category=TriageCategory.SURFACE, matched_rule="rule_1_warehouse_table",
            extracted_po_hints=po_hints, extracted_shipment_hint=shipment_hint,
        )

    if (email.sender_domain in settings.FREIGHT_SENDER_DOMAINS
            and STATUS_WORDS_RE.search(combined_text)
            and not INVENTORY_WORDS_RE.search(combined_text)):
        return TriagedEmail(
            email=email, notification_type=NotificationType.DELIVERED_SHIPPED,
            category=TriageCategory.HIDE, matched_rule="rule_2_freight_status",
            extracted_po_hints=po_hints, extracted_shipment_hint=shipment_hint,
            reason="intermediate freight status notice, not a true receiving event",
        )

    if email.sender_domain in settings.WAREHOUSE_SENDER_DOMAINS and po_hints:
        return TriagedEmail(
            email=email, notification_type=NotificationType.INBOUND_NOTIFICATION,
            category=TriageCategory.SURFACE, matched_rule="rule_3_warehouse_no_table",
            extracted_po_hints=po_hints, extracted_shipment_hint=shipment_hint,
        )

    if email.sender_domain in settings.VENDOR_CONFIRMATION_DOMAINS and po_hints:
        return TriagedEmail(
            email=email, notification_type=NotificationType.VENDOR_CONFIRMATION,
            category=TriageCategory.HOLD, matched_rule="rule_5_vendor_confirmation",
            extracted_po_hints=po_hints, extracted_shipment_hint=shipment_hint,
        )

    if po_hints and _word_count(email) <= settings.PROPERTY_REPLY_MAX_WORDS and not _has_table(email.body_html):
        return TriagedEmail(
            email=email, notification_type=NotificationType.PROPERTY_CONFIRMATION,
            category=TriageCategory.HOLD, matched_rule="rule_4_property_reply",
            extracted_po_hints=po_hints, extracted_shipment_hint=shipment_hint,
        )

    return TriagedEmail(
        email=email, notification_type=NotificationType.UNKNOWN,
        category=TriageCategory.ROUTE, matched_rule="rule_6_unknown",
        extracted_po_hints=po_hints, extracted_shipment_hint=shipment_hint,
        reason="no PO reference found — cannot resolve automatically",
    )
