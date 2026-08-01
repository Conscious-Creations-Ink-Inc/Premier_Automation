"""Approve and cancel — the human half of reconciliation, and mock stages 6-7.

Approving is not just a status flip. Per the agreed review rule, the reviewer must resolve the
item: pick the PO line the automation could not, and fill any required field the vendor left
out. Only then is a receipt staged. That way approving *fixes* the data rather than rubber-
stamping a gap.

Both decisions reuse the pipeline's own transitions — `mark_matched` and `mark_failed` — so a
record reviewed here ends up in exactly the state the pipeline would have put it in. Cancelling
uses `mark_failed`, which appends the reviewer's reason to the record's `comments`, so the
"why" travels with the record instead of living only in the audit table.
"""
from dataclasses import dataclass
from typing import Dict, Optional

from api.services import reconcile
from api.stores import extracted_store, po_lines_store, reconciliation_store
from config import settings
from pipeline import extracted_records_store
from pipeline.models import RouteTarget

_SETTLED_AS_APPROVED = (
    reconciliation_store.REVIEW_APPROVED,
    reconciliation_store.REVIEW_AUTO_APPROVED,
)


class DecisionError(ValueError):
    """A decision that cannot be applied. Routers turn this into a 4xx carrying the message, so
    the reviewer is told exactly what to fix."""


@dataclass
class DecisionOutcome:
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


def approve(
    conn,
    match_id: int,
    operator: str,
    now: str,
    po_line_id: Optional[int] = None,
    filled_fields: Optional[Dict] = None,
    note: str = "",
) -> DecisionOutcome:
    """Resolve and approve. Idempotent: approving something already approved returns the
    existing outcome rather than staging a second receipt or double-counting the quantity."""
    match = _load(conn, match_id)

    if match.review_status in _SETTLED_AS_APPROVED:
        existing = reconciliation_store.receipt_for_record(conn, match.extracted_record_id)
        return DecisionOutcome(
            match_id=match_id, extracted_record_id=match.extracted_record_id, decision="approve",
            review_status=match.review_status, extracted_status="matched", decided_by=operator,
            decided_at=now, po_line_id=match.po_line_id,
            receipt_id=(existing or {}).get("id"), already_applied=True,
        )
    if match.review_status == reconciliation_store.REVIEW_CANCELLED:
        raise DecisionError("This item was already cancelled and cannot be approved.")

    chosen_line_id = match.po_line_id if po_line_id is None else po_line_id
    if chosen_line_id is None:
        raise DecisionError(
            "No PO line is selected. Pick the correct line before approving — the automation "
            "could not resolve one."
        )
    po_line = po_lines_store.get(conn, chosen_line_id)
    if po_line is None:
        raise DecisionError(f"No such PO line: {chosen_line_id}.")

    if filled_fields:
        extracted_store.update_fields(conn, match.extracted_record_id, filled_fields, now)

    row = extracted_store.get(conn, match.extracted_record_id)
    if row is None:
        raise DecisionError(f"Extracted record {match.extracted_record_id} no longer exists.")
    record = row.record

    still_missing = reconcile.missing_required_fields(record)
    if still_missing:
        labels = ", ".join(reconcile.FIELD_LABELS[field] for field in still_missing)
        raise DecisionError(f"Still missing {labels}. Fill these in before approving.")

    receipt_id = _stage_receipt(conn, row, po_line, note, now)
    po_lines_store.add_received_qty(conn, chosen_line_id, record.quantity_received)
    extracted_records_store.mark_matched(conn, row.id, now)
    reconciliation_store.apply_decision(
        conn, match_id, reconciliation_store.REVIEW_APPROVED, now,
        po_line_id=chosen_line_id, route_target=RouteTarget.AUTO_APPROVED.value, notes=note,
    )
    reconciliation_store.write_decision(conn, reconciliation_store.ReviewDecision(
        match_result_id=match_id, extracted_record_id=row.id, decision="approve",
        decided_by=operator, decided_at=now, reason=note, resulting_status="matched",
        po_line_id=chosen_line_id, receipt_id=receipt_id,
    ))

    return DecisionOutcome(
        match_id=match_id, extracted_record_id=row.id, decision="approve",
        review_status=reconciliation_store.REVIEW_APPROVED, extracted_status="matched",
        decided_by=operator, decided_at=now, reason=note, po_line_id=chosen_line_id,
        receipt_id=receipt_id,
    )


def cancel(conn, match_id: int, operator: str, reason: str, now: str) -> DecisionOutcome:
    """Reject the item. A reason is mandatory — a cancelled receipt with no explanation is
    exactly the sort of gap this system exists to remove."""
    if not (reason or "").strip():
        raise DecisionError("A reason is required to cancel.")

    match = _load(conn, match_id)

    if match.review_status == reconciliation_store.REVIEW_CANCELLED:
        return DecisionOutcome(
            match_id=match_id, extracted_record_id=match.extracted_record_id, decision="cancel",
            review_status=match.review_status, extracted_status="failed", decided_by=operator,
            decided_at=now, reason=reason, already_applied=True,
        )
    if match.review_status in _SETTLED_AS_APPROVED:
        raise DecisionError("This item was already approved and a receipt was staged.")

    extracted_records_store.mark_failed(conn, match.extracted_record_id, reason, now)
    reconciliation_store.apply_decision(
        conn, match_id, reconciliation_store.REVIEW_CANCELLED, now,
        route_target=RouteTarget.NOT_APPLICABLE.value,
    )
    reconciliation_store.write_decision(conn, reconciliation_store.ReviewDecision(
        match_result_id=match_id, extracted_record_id=match.extracted_record_id,
        decision="cancel", decided_by=operator, decided_at=now, reason=reason,
        resulting_status="failed", po_line_id=match.po_line_id,
    ))

    return DecisionOutcome(
        match_id=match_id, extracted_record_id=match.extracted_record_id, decision="cancel",
        review_status=reconciliation_store.REVIEW_CANCELLED, extracted_status="failed",
        decided_by=operator, decided_at=now, reason=reason, po_line_id=match.po_line_id,
    )


def auto_approve(conn, match_id: int, now: str) -> Optional[int]:
    """The machine's own approval. A record with three signals and nothing missing would never
    reach a person, so the seed settles those the same way the automation would: stage the
    receipt, move the quantity, mark the record matched — but write no `review_decisions` row,
    because no human decided anything. That is what keeps `auto_approved` distinct from
    `approved` on the dashboard.
    """
    match = _load(conn, match_id)
    if match.po_line_id is None:
        return None
    row = extracted_store.get(conn, match.extracted_record_id)
    po_line = po_lines_store.get(conn, match.po_line_id)
    if row is None or po_line is None or row.record.quantity_received is None:
        return None

    receipt_id = _stage_receipt(conn, row, po_line, "", now)
    po_lines_store.add_received_qty(conn, match.po_line_id, row.record.quantity_received)
    extracted_records_store.mark_matched(conn, row.id, now)
    return receipt_id


def _stage_receipt(conn, row, po_line, note: str, now: str) -> int:
    """Builds the mock `StagedPOReceipt` — what stage 6 would push into Spitfire."""
    record = row.record
    return reconciliation_store.write_receipt(conn, {
        "extracted_record_id": row.id,
        "po_line_id": po_line.id,
        "shipment_number": record.shipment_number,
        "purchase_order": po_line.line.po_number,
        # The line key is Spitfire's GUID for the PO line — what makes the receipt exact.
        "item_number": po_line.line.line_key,
        "item_description": po_line.line.description,
        "vendor": po_line.line.vendor_name,
        "carrier_name": record.carrier_name,
        "pro_number": record.tracking_number,
        "quantity": record.quantity_received,
        "quantity_types": record.unit_of_measure or po_line.line.unit_of_measure,
        "act_delivery_date": record.pod_stated_date or record.email_date,
        "delivery_location": record.delivery_location or po_line.line.ship_to,
        "comments": note or record.comments,
        "created_by": settings.STAGED_BY_USER,
        "created_at": now,
    })


def _load(conn, match_id: int) -> reconciliation_store.MatchRow:
    match = reconciliation_store.get(conn, match_id)
    if match is None:
        raise DecisionError(f"No such reconciliation item: {match_id}.")
    return match
