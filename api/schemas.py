"""Pydantic mirrors of the pipeline dataclasses, plus the request/response shapes.

Field names and the enum vocabularies (`confidence`, `route_target`) match
`pipeline.models` exactly, so the badges in the React app are keyed off the same strings the
pipeline emits and can never drift. The frontend's TypeScript types are generated from the
OpenAPI schema this module produces.
"""
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Confidence = Literal["high", "medium", "low", "none"]
ReviewStatus = Literal["auto_approved", "pending_review", "approved", "cancelled"]


# --- Core records --------------------------------------------------------------


class POLineSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    po_number: str
    line_number: int
    line_key: str
    spec_code: str
    description: str
    vendor_name: str
    unit_of_measure: str
    qty_ordered: float
    qty_received: float
    qty_in_transit: float = 0.0   # unapproved receipts; qty_outstanding already nets these off
    qty_outstanding: float
    cost_code: str
    project_code: str
    project_name: str
    line_status: str
    expected_date: Optional[str] = None
    ship_to: Optional[str] = None
    assigned_agent: Optional[str] = None
    pay_terms: Optional[str] = None

    @classmethod
    def from_row(cls, row) -> "POLineSchema":
        return cls(id=row.id, qty_outstanding=row.qty_outstanding, **vars(row.line))


class ExtractedRecordSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source_email_id: str
    po_number: Optional[str] = None
    shipment_number: Optional[str] = None
    spec_code: Optional[str] = None
    parent_spec_code: Optional[str] = None
    sub_spec_suffix: Optional[str] = None
    item_description: Optional[str] = None
    vendor_name: Optional[str] = None
    carrier_name: Optional[str] = None
    tracking_number: Optional[str] = None
    quantity_received: Optional[float] = None
    unit_of_measure: Optional[str] = None
    pod_stated_date: Optional[str] = None
    email_date: str
    delivery_location: Optional[str] = None
    comments: Optional[str] = None
    extraction_source: str
    extraction_confidence: float
    raw_snippet: Optional[str] = None

    # These five exist on `ExtractedRecord` and in the `extracted_records` table but were never
    # added here, so the API and the review UI could not see them. `po_line_number` is the worst
    # of the five to lose: it is the Spitfire line number stated outright by the Authority Inbound
    # (`908491 : 300`), and it is what turns a match from a fuzzy description search into an exact
    # lookup — precisely the field a human completing a match by hand most needs shown.
    po_line_number: Optional[int] = None
    received_by: Optional[str] = None
    package_quantity: Optional[float] = None
    package_uom: Optional[str] = None
    notification_number: Optional[str] = None


class ExtractedRecordRowSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    created_at: str
    updated_at: Optional[str] = None
    record: ExtractedRecordSchema


class EmailContextSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    email_id: str
    subject: str
    sender_address: str
    received_at: str
    notification_type: str
    triage_category: str
    status_keyword: str
    has_attachment: bool


# --- Reconciliation ------------------------------------------------------------


class MatchSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    extracted_record_id: int
    po_line_id: Optional[int] = None
    po_signal: bool
    spec_signal: bool
    desc_signal: bool
    desc_score: float
    signals_matched: int
    confidence: Confidence
    missing_fields: List[str]
    flagged: bool
    flag_reason: str
    route_target: str
    review_status: ReviewStatus
    notes: str
    created_at: str
    updated_at: Optional[str] = None


class CandidateSchema(BaseModel):
    """A PO line scored against the record, with the per-signal breakdown the review drawer
    shows so a reviewer can see why it ranked where it did."""
    po_line: POLineSchema
    po_signal: bool
    spec_signal: bool
    desc_signal: bool
    desc_score: float
    signals_matched: int
    confidence: Confidence

    @classmethod
    def from_candidate(cls, candidate) -> "CandidateSchema":
        return cls(
            po_line=POLineSchema.from_row(candidate.po_line),
            po_signal=candidate.po_signal, spec_signal=candidate.spec_signal,
            desc_signal=candidate.desc_signal, desc_score=candidate.desc_score,
            signals_matched=candidate.signals_matched, confidence=candidate.confidence,
        )


class DecisionSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    decision: str
    decided_by: str
    decided_at: str
    reason: str
    resulting_status: str
    po_line_id: Optional[int] = None
    receipt_id: Optional[int] = None


class ReconciliationRowSchema(BaseModel):
    match: MatchSchema
    record: ExtractedRecordRowSchema
    po_line: Optional[POLineSchema] = None
    email: Optional[EmailContextSchema] = None


class ExtractedRow(BaseModel):
    """The parse/extract view. `match` is optional here because a record exists the moment it is
    extracted, before reconciliation has run against it."""
    record: ExtractedRecordRowSchema
    match: Optional[MatchSchema] = None
    po_line: Optional[POLineSchema] = None
    email: Optional[EmailContextSchema] = None


class ReconciliationDetailSchema(ReconciliationRowSchema):
    candidates: List[CandidateSchema] = Field(default_factory=list)
    decisions: List[DecisionSchema] = Field(default_factory=list)
    receipt: Optional[Dict] = None
    editable_fields: List[str] = Field(default_factory=list)


# --- Decisions -----------------------------------------------------------------


class ApproveRequest(BaseModel):
    po_line_id: Optional[int] = Field(
        default=None, description="Line the reviewer picked; defaults to the automation's best match."
    )
    filled_fields: Optional[Dict] = Field(
        default=None, description="Corrections for missing fields, e.g. {'spec_code': 'STE-402-LT'}."
    )
    note: str = ""
    operator: Optional[str] = None


class CancelRequest(BaseModel):
    reason: str = Field(min_length=1, description="Why this item is being rejected. Required.")
    operator: Optional[str] = None


class DecisionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    match_id: int
    extracted_record_id: int
    decision: str
    review_status: str
    extracted_status: str
    decided_by: str
    decided_at: str
    reason: str = ""
    po_line_id: Optional[int] = None
    receipt_id: Optional[int] = None
    already_applied: bool = False


# --- Dashboard -----------------------------------------------------------------


class FunnelStage(BaseModel):
    key: str
    label: str
    count: int


class DashboardSummary(BaseModel):
    generated_at: str
    totals: Dict[str, int]
    funnel: List[FunnelStage]
    by_status: Dict[str, int]
    by_confidence: Dict[str, int]
    by_review_status: Dict[str, int]
    by_route_target: Dict[str, int]
    exception_queue_size: int


# --- Delivery report -----------------------------------------------------------


class DeliveryRow(BaseModel):
    email_id: str
    received_at: str
    subject: str
    sender_address: str
    po_number: Optional[str] = None
    notification_type: str
    triage_category: str
    status_keyword: str
    proposed_folder: str
    has_attachment: bool


class DeliveryBucket(BaseModel):
    key: str
    label: str
    count: int
    rows: List[DeliveryRow]


class DeliveryReport(BaseModel):
    total: int
    buckets: List[DeliveryBucket]


# --- Inbox organizer -----------------------------------------------------------


# --- Delivery status per purchase order ---------------------------------------
# The lifecycle vocabulary is `api.services.po_status.LIFECYCLE`, not a Literal here: the statuses
# are derived today and become M1's persisted values later, and a Literal in two files is how the
# pipeline and the UI start disagreeing about what a status is called.


class PoLineStatus(BaseModel):
    line: POLineSchema
    status: str
    label: str
    reason: str
    """Why this status was inferred. Every status on this screen is a guess until M1 lands, and a
    guess a reviewer cannot interrogate is worse than showing nothing."""


class PoStatusRow(BaseModel):
    po_number: str
    vendor_name: str
    project_code: str
    project_name: str
    status: str
    label: str
    line_count: int
    counts_by_status: Dict[str, int]
    qty_ordered: float
    qty_received: float
    qty_in_transit: float
    qty_outstanding: float
    last_email_at: Optional[str] = None
    email_count: int = 0


class PoDetail(PoStatusRow):
    lines: List[PoLineStatus] = Field(default_factory=list)
    emails: List[DeliveryRow] = Field(default_factory=list)


class PoStatusReport(BaseModel):
    generated_at: str
    derived: bool = True
    """False once M1 replaces the inference with persisted statuses. The UI says so on the page —
    a client should never mistake an inferred lifecycle for a recorded one."""
    unreachable_statuses: List[str] = Field(default_factory=list)
    """Statuses this build cannot produce at all: the partnered-warehouse leg has no signal in the
    data, and nothing writes to Spitfire. Listed so the UI labels them rather than a viewer
    concluding the pipeline never gets that far."""
    status_labels: Dict[str, str] = Field(default_factory=dict)
    rows: List[PoStatusRow] = Field(default_factory=list)


class InboxRow(BaseModel):
    email_id: str
    received_at: str
    subject: str
    sender_address: str
    notification_type: str
    triage_category: str
    reason: str
    proposed_folder: str
    keep_in_inbox: bool
    organized_folder: Optional[str] = None
    moved_at: Optional[str] = None


class InboxPreview(BaseModel):
    rows: List[InboxRow]
    pending_move: int
    kept_in_inbox: int
    already_filed: int
    by_folder: Dict[str, int]


class OrganizeRequest(BaseModel):
    email_ids: Optional[List[str]] = Field(
        default=None, description="Omit to file everything currently pending."
    )


class OrganizeResponse(BaseModel):
    moved: int
    remaining: int
    by_folder: Dict[str, int]


# --- Vendor template -----------------------------------------------------------


class VendorTemplateSchema(BaseModel):
    subject: str
    body: str
    schedule_hours: int
    enabled: bool
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None
    placeholders: List[str] = Field(default_factory=list)


class VendorTemplateUpdate(BaseModel):
    subject: Optional[str] = None
    body: Optional[str] = None
    schedule_hours: Optional[int] = Field(default=None, ge=1, le=720)
    enabled: Optional[bool] = None


class VendorSendRequest(BaseModel):
    extracted_record_id: Optional[int] = None
    to: Optional[str] = None


class VendorSendResponse(BaseModel):
    sent_to: str
    subject: str
    rendered_body: str
    sent_at: str
    status: str
    po_number: Optional[str] = None


class ScheduleRequest(BaseModel):
    every_n_hours: int = Field(ge=1, le=720)
    enabled: bool


class ScheduleResponse(BaseModel):
    enabled: bool
    every_n_hours: int
    next_run_at: Optional[str] = None


# --- Admin ---------------------------------------------------------------------


class SeedResponse(BaseModel):
    seeded: bool
    counts: Dict[str, int]


class HealthResponse(BaseModel):
    status: str
    database: str
    seeded: bool
    counts: Dict[str, int]
