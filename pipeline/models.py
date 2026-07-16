from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


# --- Stage 1: Ingest & Triage -------------------------------------------

class NotificationType(str, Enum):
    WAREHOUSE_INBOUND = "warehouse_inbound"
    DELIVERED_SHIPPED = "delivered_shipped"
    INBOUND_NOTIFICATION = "inbound_notification"
    PROPERTY_CONFIRMATION = "property_confirmation"
    VENDOR_CONFIRMATION = "vendor_confirmation"
    ORDER_CANCELLATION = "order_cancellation"
    UNKNOWN = "unknown"


class TriageCategory(str, Enum):
    HIDE = "hide"
    SURFACE = "surface"
    HOLD = "hold"
    ROUTE = "route"


@dataclass
class Attachment:
    filename: str
    content_type: str
    content_bytes: bytes


@dataclass
class RawEmail:
    email_id: str
    received_at: str
    sender_address: str
    sender_domain: str
    subject: str
    body_html: Optional[str]
    body_text: Optional[str]
    attachments: List[Attachment] = field(default_factory=list)


@dataclass
class TriagedEmail:
    email: RawEmail
    notification_type: NotificationType
    category: TriageCategory
    matched_rule: str
    extracted_po_hints: List[str]
    extracted_shipment_hint: Optional[str] = None   # None for property/vendor confirmations — no shipment number
    reason: str = ""


# --- Stage 2: Accumulate -------------------------------------------------

@dataclass(frozen=True)
class AccumulationKey:
    po_number: str
    shipment_number: Optional[str]


@dataclass
class DeliveryEvent:
    key: AccumulationKey
    trigger_notification_type: NotificationType
    emails: List[TriagedEmail]
    released_at: str
    release_reason: str


# --- Stage 3: Extract -----------------------------------------------------

@dataclass
class ExtractedRecord:
    source_email_id: str
    po_number: str
    shipment_number: Optional[str]
    spec_code: Optional[str]
    parent_spec_code: Optional[str]
    sub_spec_suffix: Optional[str]
    item_description: Optional[str]
    vendor_name: Optional[str]
    carrier_name: Optional[str]
    tracking_number: Optional[str]
    quantity_received: Optional[float]
    unit_of_measure: Optional[str]
    pod_stated_date: Optional[str]
    email_date: str
    delivery_location: Optional[str]
    comments: Optional[str]
    extraction_source: str
    extraction_confidence: float
    raw_snippet: str


@dataclass
class ExtractedRecordRow:
    """One row of the `extracted_records` staging table — the Stage 3/4 hand-off (see ORCHESTRATOR_DESIGN.md)."""
    id: int
    record: ExtractedRecord
    status: str   # "pending" | "matched" | "failed" | "routed"
    created_at: str
    updated_at: Optional[str] = None


# --- Stage 4: Reconcile & Match --------------------------------------------

@dataclass
class POLine:
    po_number: str
    line_number: int
    line_key: str
    spec_code: str
    description: str
    vendor_name: str
    unit_of_measure: str
    qty_ordered: float
    qty_received: float
    cost_code: str
    project_code: str
    project_name: str
    line_status: str   # "Open" | "Closed" | "Cancelled"
    expected_date: Optional[str]
    ship_to: Optional[str]
    assigned_agent: Optional[str]
    pay_terms: Optional[str] = None   # "Net 30" | "CBD" | "ADR" — needed by Stage 5; checklist #14


@dataclass
class ScoredCandidate:
    line: POLine
    signals_matched: int   # 0-3


@dataclass
class MatchResult:
    extracted: ExtractedRecord
    po_line: Optional[POLine]
    signals_matched: int
    confidence: str   # "high" | "medium" | "low" | "none"
    notes: str


# --- Stage 5: Verify ---------------------------------------------------------

@dataclass
class PodEvidence:
    file: Optional[bytes]
    confirmation_text: Optional[str]
    confirmation_email_date: Optional[str]
    carrier_tracking_date: Optional[str]
    pod_stated_date: Optional[str]
    email_date: str
    extracted_description: Optional[str]


@dataclass
class VerifyResult:
    match: MatchResult
    passed: bool
    reason: str
    resolved_received_date: Optional[str]
    requires_email_confirmation: bool
    in_scope: bool


# --- Stage 6: Build & Log -------------------------------------------------

@dataclass
class StagedPOReceipt:
    shipment_number: Optional[str]
    purchase_order: str
    item_number: str
    item_description: str
    vendor: str
    carrier_name: Optional[str]
    pro_number: Optional[str]
    quantity: float
    quantity_types: str
    est_ship_date: Optional[str]
    sch_pickup_date: Optional[str]
    act_pickup_date: Optional[str]
    qty_shipped: float
    qty_left_to_ship: Optional[float]
    act_delivery_date: str
    sch_delivery_date: Optional[str]
    delivery_location: Optional[str]
    comments: Optional[str]
    created_by: str


@dataclass
class StagedPODDocument:
    shipment_number: Optional[str]
    purchase_order: str
    document_type: str   # e.g. "CARRIER_POD" | "EMAIL_CONFIRMATION"
    document_name: str
    document_content: bytes
    created_by: str


# --- Stage 7: Route & Audit -----------------------------------------------

class RouteTarget(str, Enum):
    AUTO_APPROVED = "auto_approved"
    PURCHASING_AGENT = "purchasing_agent"
    FIXED_ASSET_ACCOUNTING = "fixed_asset_accounting"
    EXCEPTION_QUEUE = "exception_queue"
    NOT_APPLICABLE = "not_applicable"


@dataclass
class RoutingDecision:
    source_stage: str
    reference: Dict
    route_to: RouteTarget
    reason: str
    logged_at: str
