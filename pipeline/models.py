from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

# Safe at module scope: `dedupe` imports only the standard library, so there is no cycle back here.
from pipeline import dedupe


# --- Stage 1: Ingest & Triage -------------------------------------------

class NotificationType(str, Enum):
    WAREHOUSE_INBOUND = "warehouse_inbound"
    DELIVERED_SHIPPED = "delivered_shipped"
    INBOUND_NOTIFICATION = "inbound_notification"
    PROPERTY_CONFIRMATION = "property_confirmation"
    VENDOR_CONFIRMATION = "vendor_confirmation"
    ORDER_CANCELLATION = "order_cancellation"
    WAREHOUSE_STATUS_REPORT = "warehouse_status_report"
    """The periodic Purchase Order Status Report — same sender as the receiver trigger, told
    apart only by its subject. Recognised so it can be discarded rather than scraped."""
    LOSS_OR_CLAIM = "loss_or_claim"
    """Lost/damaged goods, claims, credit memos, replacement POs. Reads like delivery mail and
    is out of Phase 1 scope, so it must be recognised and handed to a person, never processed."""
    VERIFICATION_REQUEST = "verification_request"
    """Mail that *asks* whether goods arrived, rather than reporting that they did.

    Premier drives property and vendor confirmations with a request table, and the reply carries
    that table quoted underneath it. Both halves are full of delivery vocabulary, so a message
    still awaiting its answer was indistinguishable from the answer — `Could you please verify
    whether the fabrics listed below were received for Attic Stock?` was read as a confirmation
    and staged three receipts, one for goods the property later said had never arrived. A question
    needs a person, never a receiver."""
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

    # --- Provenance. All defaulted, so every existing positional constructor still works.

    content_id: Optional[str] = None
    is_inline: bool = False
    """From the mail source. Together with the body's `cid:` references these are what tell a
    signature logo apart from a photograph someone attached — see parsing/sniff.classify_image."""

    sha256: str = ""
    size_bytes: int = 0
    sniffed_kind: str = ""
    """Recorded at ingest, while the bytes are still in hand. A dropped attachment has its
    `content_bytes` cleared to avoid carrying a logo or a duplicate around, so re-sniffing it
    later yields `unknown` — which would make the ledger useless for exactly the rows a person
    most needs to understand."""

    drop_hint: Optional[str] = None
    """Set when the connector decided not to hand this on: `"decorative:tiny"`,
    `"duplicate:<sha>"`, `"oversize"`, `"empty"`. The attachment is still carried, so the
    orchestrator can record *why* it was dropped. Silently discarding one at ingest is how a
    photographed POD used to disappear without trace."""

    ledger_id: Optional[int] = None
    container_path: str = ""
    """Position inside nested containers, e.g. `"outer.msg!/inner.zip!/pod.pdf"`, so a record
    extracted four levels down can still be traced back to the file it came from."""


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

    provider_message_id: Optional[str] = None
    """The mail provider's own handle for this message, when it differs from `email_id`.

    Graph's folder-scoped `id` changes the moment a message is moved — and this pipeline moves
    every message it processes — so it cannot be the dedupe key; `internetMessageId` is. But
    Graph's /move endpoint only accepts its own id, so both have to be carried (finding C5)."""

    source_folder: str = ""
    """Which mailbox folder this message was listed from — a name in
    `settings.MAILBOX_SOURCE_FOLDERS`, so `inbox` or `junkemail`.

    Carried so "Exchange junked a delivery notification" is a fact on the screen rather than
    something that has to be re-discovered by querying Graph by hand, which is how it was found.
    Empty for every connector that has no folders — `LocalFolderMailbox`, `MsgFileMailbox` and the
    test fakes — which is why it is a default rather than a required field: the `Mailbox` ABC does
    not change and nothing else had to."""


@dataclass
class TriagedEmail:
    email: RawEmail
    notification_type: NotificationType
    category: TriageCategory
    matched_rule: str
    extracted_po_hints: List[str]
    extracted_shipment_hint: Optional[str] = None   # None for property/vendor confirmations — no shipment number
    reason: str = ""

    origin_sender_address: Optional[str] = None
    """Who actually sent the payload, recovered from the quoted chain. Every message Premier
    handed over is a `Fw:` from an internal expeditor, so `email.sender_address` is
    `example-pm.test` on all of them and useless for routing (see parsing/thread.py)."""

    origin_sent_at: Optional[str] = None
    """When that payload was actually sent, `YYYY-MM-DD`, from the same quoted header block.

    The counterpart to `origin_sender_address` and for the same reason: the envelope date of a
    forward is the day it was forwarded. On the corpus that is 2026-06-06 for twelve of fourteen
    messages, which is why every stage of every delivery timeline once showed one date. None when
    the mail arrived direct, or when the header is in a shape `parsing.thread.parse_sent` cannot
    read — in both cases the envelope date is the better answer."""

    notification_number: Optional[str] = None
    """The originator's own reference (Authority inbound # / Authority #). Two files in the
    corpus are the same notice 939336 — one direct, one forwarded — arriving under different
    Message-IDs; this is what lets Stage 2 see them as one event."""

    not_a_delivery: bool = False
    """This message is positively not a delivery notification — not merely unrecognised.

    Derived from the rule that matched, never guessed at the call site: see
    `stage1_triage.NOT_A_DELIVERY_RULES`. It separates "we know this is not a receipt" from "we
    could not tell", which is the distinction the manual queue could not draw. 91% of all records
    came in through one catch-all rule and 86% of those were incomplete, because a scheduled report
    and a genuine two-line property confirmation reached it by the same door.

    Deliberately false for cancellations and loss/claim notices. Those are not deliveries either,
    but they are real work — a PO needs updating in Spitfire — and filing them under a heading that
    reads "no action" would bury them."""


# --- Stage 2: Accumulate -------------------------------------------------

@dataclass(frozen=True)
class AccumulationKey:
    """Which delivery, on which purchase order.

    `shipment_number` is kept because it is what the mail states and what a person reads, but it is
    no longer the key: nine of the fourteen corpus emails state none, and a nullable key column
    collapsed every such delivery on a PO into one. `delivery_ref` is what actually identifies the
    delivery — see `dedupe.delivery_ref` for the ladder that resolves it — and is never empty.

    `delivery_rung` names which rung answered, so a delivery identified only by its message id can
    be routed to a person instead of being presented as a join that was never made.
    """

    po_number: str
    shipment_number: Optional[str]
    delivery_ref: str = ""
    delivery_rung: str = ""

    @property
    def is_certain(self) -> bool:
        """Whether the mail said something that identifies this delivery, or we fell through."""
        return self.delivery_rung in dedupe.CERTAIN_RUNGS


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

    # --- Fields below carry defaults so the positional constructors already in the adapters
    # keep working. They were added once the real June corpus showed the Authority Logistics
    # format hands over more than the design assumed.

    po_line_number: Optional[int] = None
    """The Spitfire line number, when the source states it outright — the Authority Inbound
    `PO # / Line #` cell reads `908491 : 300`. This turns Stage 4 from a fuzzy description
    search into an exact lookup, so it is the single most valuable field on the record."""

    received_by: Optional[str] = None
    """Warehouse staffer who signed the goods in ("Jordan T.") or the POD's `Signed for by`."""

    package_quantity: Optional[float] = None
    package_uom: Optional[str] = None
    """Cartons/pallets/skids — deliberately kept apart from `quantity_received`. An Inbound
    header reads `Quantity: 41 CTN` while the line row reads `11 EA`; receiving the carton
    count against the PO line is the failure mode this split exists to prevent."""

    notification_number: Optional[str] = None
    """Authority's own reference — the inbound # for a Class A notice, the Authority # for a
    Class B one. Retained for audit and for tying a record back to the notice that produced it."""

    source_ledger_id: Optional[int] = None
    """Which `attachment_ledger` row this record was read from, when it came from an attachment.

    Two records sharing it are **sibling rows of one grid**, not competing claims about one line.
    That distinction is the whole point: an Atlas receiving report lists one spec on three rows —
    three skids of the same item — and `reconcile_cross_source_duplicates` was reading the three
    quantities as a disagreement and flagging all of them `+quantity_conflict`. 58 of the 66
    flagged groups in the live store are single-document like that.

    It also tells a document apart from a *copy* of the same document, which is what lets the
    orchestrator stage from the accurate copy and supersede the other.

    Carried in memory only, between extraction and staging. `extracted_records` has no column for
    it and needs none — persisting it would be a schema change, and nothing downstream of staging
    asks the question.
    """

    superseded_by_ledger_id: Optional[int] = None
    """Set when a more accurate copy of this same document is attached to the same message.

    Atlas sends the receiving report its own system generated *and* a scan of the signed copy;
    both are read, and the scan's read is the poorer one. This names the attachment whose read
    won, so `stage_records` can keep this record as evidence without offering it as work.

    In memory only, like `source_ledger_id` — it is written to the row's `status`, not a column.
    """

    quantity_ordered: Optional[float] = None
    """What the paperwork says was *ordered*, as distinct from what arrived.

    A confirmation grid's plain `Qty` column is this, not `quantity_received` — the module that
    reads those grids has said so in a comment since it was written, and then fell back to using it
    as the received figure whenever the sheet carried no better column. Premier's own request table
    carries exactly one quantity column, so every row of every unanswered request was staged as a
    receipt for the quantity someone was *asking about*.

    Kept because it is genuinely useful — it is the figure a reviewer compares an answer against —
    but it is never a receipt. PO 910634 was staged at 196 from the request while the carrier's POD
    and the Authority notice both said 202 (`6 yards of overage`, as the thread itself explains)."""


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

    qty_in_transit: float = 0.0
    """`RelatedItemDetail.ReceiptInProgressUnits` — *"units tentatively received not yet
    approved"*: a receipt document that exists but has not been approved through its route.

    Outstanding quantity has to subtract this as well as `qty_received`, or a delivery whose
    receipt is still awaiting approval reads as entirely un-received and gets a second receipt
    raised against it. One physical delivery becoming two receivers is the failure that ended
    Premier's previous attempt, and this field is the only thing in the API that reveals the
    in-flight case. It is also column 53 ("Qty In Transit") of the receiver file spec."""

    @property
    def qty_outstanding(self) -> float:
        return self.qty_ordered - self.qty_received - self.qty_in_transit


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
