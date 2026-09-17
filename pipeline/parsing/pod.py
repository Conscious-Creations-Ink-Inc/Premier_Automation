"""Carrier proof-of-delivery parsing.

A POD is the strongest evidence Stage 5 can hold: it states a delivery date, a signature and a
reference field, and it comes from the carrier rather than from anyone with an interest in the
answer. The corpus has FedEx Freight PODs; the field grammar below is written from a real one:

    The following is the proof-of-delivery for tracking number: 7497809572
    Status: Delivered        Delivery date: Sep 10, 2025 10:18
    Signed for by: U ALI
    Service type: FedEx Freight Priority
    Tracking number: 7497809572     Ship Date: Sep 5, 2025
    Weight: 392.0 LB/177.97 KG
    Purchase Order 91457971,910634,99985 : 1

That last line is the trap and the prize. It is labelled "Purchase Order" but holds three
different identifiers comma-joined — the carrier's own reference, the **PO** (910634), and the
Authority shipment number with its leg suffix (99985 : 1). Taking the field at its label gives
`91457971` as the PO; taking the six-digit token gives the right answer *and* the shipment
number that ties this POD to the notification that announced it.

Values sit on the same line as their label and several labels share a line, so each field is
matched up to the next known label rather than to end-of-line.
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional

from pipeline.parsing import tokens

# Every label that can terminate another label's value.
_LABELS = [
    "status", "delivery date", "delivered on", "signed for by", "signed by", "service type",
    "special handling", "tracking number", "reference", "ship date", "shipped date", "weight",
    "recipient", "shipper", "purchase order", "po number", "bill of lading", "bol", "pro number",
    "pieces", "dimensions",
]
_TERMINATOR = r"(?=\s*(?:" + "|".join(re.escape(label) for label in _LABELS) + r")\s*[:#]|\n|$)"

_POD_MARKER_RE = re.compile(
    r"proof[\s\-]?of[\s\-]?delivery|\bPOD\b|delivery\s+information|signed\s+for\s+by",
    re.IGNORECASE,
)

_CARRIER_PATTERNS = [
    (re.compile(r"\bfedex\b", re.IGNORECASE), "FedEx"),
    (re.compile(r"\bups\b", re.IGNORECASE), "UPS"),
    (re.compile(r"\bdhl\b", re.IGNORECASE), "DHL"),
    (re.compile(r"\bxpo\b", re.IGNORECASE), "XPO"),
    (re.compile(r"\bold\s*dominion\b", re.IGNORECASE), "Old Dominion"),
    (re.compile(r"\bestes\b", re.IGNORECASE), "Estes"),
    (re.compile(r"\bglobaltranz\b", re.IGNORECASE), "GlobalTranz"),
    (re.compile(r"\bnolan\s+transportation\b", re.IGNORECASE), "Example Freight"),
]


@dataclass
class PodDocument:
    delivery_date: Optional[str] = None
    signed_for_by: Optional[str] = None
    tracking_numbers: List[str] = field(default_factory=list)
    carrier_name: Optional[str] = None
    service_type: Optional[str] = None
    ship_date: Optional[str] = None
    weight: Optional[str] = None
    po_numbers: List[str] = field(default_factory=list)
    other_references: List[str] = field(default_factory=list)
    recipient: Optional[str] = None
    raw_text: str = ""

    @property
    def is_delivered(self) -> bool:
        return self.delivery_date is not None


def detect_carrier(text: str) -> Optional[str]:
    """Carrier name from any text that might mention one — a POD body, or a tracker's
    `Tracking` cell (`"FedEx 476858924781"`, `"Shipped via Truck"`)."""
    for pattern, name in _CARRIER_PATTERNS:
        if pattern.search(text or ""):
            return name
    return None


def looks_like_pod(text: str) -> bool:
    """Cheap gate so a packing slip or an invoice isn't run through the POD grammar."""
    return bool(text) and bool(_POD_MARKER_RE.search(text))


def _field(text: str, label: str) -> str:
    pattern = re.compile(
        rf"\b{re.escape(label)}\s*[:#]?\s*(?P<value>.+?){_TERMINATOR}",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    return re.sub(r"\s+", " ", match.group("value")).strip() if match else ""


def parse_pod(text: str) -> Optional[PodDocument]:
    """Parse POD text (from a PDF text layer or from OCR). None when it isn't a POD."""
    if not looks_like_pod(text):
        return None

    document = PodDocument(raw_text=text[:4000])
    document.delivery_date = tokens.normalize_date(_field(text, "delivery date")) or \
        tokens.normalize_date(_field(text, "delivered on"))
    document.signed_for_by = _field(text, "signed for by") or _field(text, "signed by") or None
    document.service_type = _field(text, "service type") or None
    document.ship_date = tokens.normalize_date(_field(text, "ship date")) or \
        tokens.normalize_date(_field(text, "shipped date"))
    document.weight = _field(text, "weight") or None
    document.recipient = _field(text, "recipient") or None

    tracking = _field(text, "tracking number")
    document.tracking_numbers = tokens.parse_tracking_numbers(tracking)

    # "Purchase Order 91457971,910634,99985 : 1" — split rather than trusted whole.
    reference = _field(text, "purchase order") or _field(text, "reference") or _field(text, "po number")
    document.po_numbers, document.other_references = tokens.split_reference_field(reference)

    document.carrier_name = detect_carrier(text)
    return document
