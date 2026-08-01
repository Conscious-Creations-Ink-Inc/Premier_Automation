"""Reconciliation — the centre of the dashboard.

`/exceptions` is the queue a reviewer lives in. `/{id}` gives them everything needed to decide
in one payload: the extracted record, why it was flagged, the ranked candidate PO lines with
their per-signal breakdown, and the decision history. `/approve` and `/cancel` are the two ways
out.
"""
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api import deps, schemas, util
from api.services import decisions as decisions_service
from api.services import reconcile
from api.stores import emails_store, extracted_store, po_lines_store, reconciliation_store
from api.stores import vendor_store

router = APIRouter(prefix="/api/reconciliation", tags=["reconciliation"])

# How many PO lines to offer when nothing scored at all (an unknown PO number). The reviewer
# still has to be able to resolve it by hand.
_FALLBACK_CANDIDATE_LIMIT = 10


def _caches(conn):
    return (
        {row.id: row for row in extracted_store.list_all(conn)},
        {row.id: row for row in po_lines_store.list_all(conn)},
        {email.email_id: email for email in emails_store.list_all(conn)},
    )


def _to_row(match, records, lines, emails) -> schemas.ReconciliationRowSchema:
    record_row = records.get(match.extracted_record_id)
    po_line = lines.get(match.po_line_id) if match.po_line_id is not None else None
    email = emails.get(record_row.record.source_email_id) if record_row else None
    return schemas.ReconciliationRowSchema(
        match=schemas.MatchSchema.model_validate(match),
        record=schemas.ExtractedRecordRowSchema.model_validate(record_row),
        po_line=schemas.POLineSchema.from_row(po_line) if po_line else None,
        email=schemas.EmailContextSchema.model_validate(email) if email else None,
    )


@router.get("/exceptions", response_model=List[schemas.ReconciliationRowSchema])
def exception_queue(conn=Depends(deps.get_conn)) -> List[schemas.ReconciliationRowSchema]:
    """Flagged and still awaiting a person — what the review screen lists."""
    records, lines, emails = _caches(conn)
    return [
        _to_row(match, records, lines, emails)
        for match in reconciliation_store.list_exception_queue(conn)
    ]


@router.get("", response_model=List[schemas.ReconciliationRowSchema])
def list_reconciliation(
    flagged: Optional[bool] = None,
    review_status: Optional[str] = None,
    confidence: Optional[str] = None,
    conn=Depends(deps.get_conn),
) -> List[schemas.ReconciliationRowSchema]:
    records, lines, emails = _caches(conn)
    matches = reconciliation_store.list_matches(
        conn, flagged=flagged, review_status=review_status, confidence=confidence
    )
    return [_to_row(match, records, lines, emails) for match in matches]


@router.get("/{match_id}", response_model=schemas.ReconciliationDetailSchema)
def reconciliation_detail(
    match_id: int, conn=Depends(deps.get_conn)
) -> schemas.ReconciliationDetailSchema:
    match = reconciliation_store.get(conn, match_id)
    if match is None:
        raise HTTPException(status_code=404, detail=f"No such reconciliation item: {match_id}.")

    records, lines, emails = _caches(conn)
    base = _to_row(match, records, lines, emails)
    record = records[match.extracted_record_id].record
    po_lines = list(lines.values())

    candidates = reconcile.rank_candidates(record, po_lines, limit=5)
    if not candidates:
        # Nothing scored — usually an unknown PO number. Offer the open lines so the item can
        # still be resolved by hand rather than being a dead end.
        open_lines = [row for row in po_lines if row.line.line_status == "Open"]
        candidates = [
            reconcile.score_candidate(record, row)
            for row in open_lines[:_FALLBACK_CANDIDATE_LIMIT]
        ]

    return schemas.ReconciliationDetailSchema(
        **base.model_dump(),
        candidates=[schemas.CandidateSchema.from_candidate(c) for c in candidates],
        decisions=[
            schemas.DecisionSchema.model_validate(d)
            for d in reconciliation_store.decisions_for_match(conn, match_id)
        ],
        receipt=reconciliation_store.receipt_for_record(conn, match.extracted_record_id),
        editable_fields=list(extracted_store.EDITABLE_FIELDS),
    )


@router.post("/{match_id}/approve", response_model=schemas.DecisionResponse)
def approve(
    match_id: int,
    payload: schemas.ApproveRequest,
    conn=Depends(deps.get_conn),
    operator: str = Depends(deps.get_operator),
) -> schemas.DecisionResponse:
    try:
        outcome = decisions_service.approve(
            conn, match_id, payload.operator or operator, util.now_iso(),
            po_line_id=payload.po_line_id, filled_fields=payload.filled_fields, note=payload.note,
        )
    except decisions_service.DecisionError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return schemas.DecisionResponse.model_validate(outcome)


@router.post("/{match_id}/cancel", response_model=schemas.DecisionResponse)
def cancel(
    match_id: int,
    payload: schemas.CancelRequest,
    conn=Depends(deps.get_conn),
    operator: str = Depends(deps.get_operator),
) -> schemas.DecisionResponse:
    try:
        outcome = decisions_service.cancel(
            conn, match_id, payload.operator or operator, payload.reason, util.now_iso()
        )
    except decisions_service.DecisionError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return schemas.DecisionResponse.model_validate(outcome)


@router.post("/{match_id}/request-info", response_model=schemas.VendorSendResponse)
def request_info(
    match_id: int,
    conn=Depends(deps.get_conn),
    operator: str = Depends(deps.get_operator),
) -> schemas.VendorSendResponse:
    """Chase the vendor for exactly what this item is missing, using the standard template.

    This is the link between the queue and the template: rather than guessing at a vendor's
    format, we ask them for the fields reconciliation actually needs."""
    match = reconciliation_store.get(conn, match_id)
    if match is None:
        raise HTTPException(status_code=404, detail=f"No such reconciliation item: {match_id}.")

    row = extracted_store.get(conn, match.extracted_record_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Extracted record no longer exists.")
    record = row.record

    missing = [reconcile.FIELD_LABELS[field] for field in match.missing_fields] or ["confirmation"]
    template = vendor_store.get_template(conn)
    rendered = vendor_store.render(template, {
        "vendor": record.vendor_name,
        "po_number": record.po_number,
        "spec_code": record.spec_code,
        "description": record.item_description,
        "quantity": record.quantity_received,
        "missing_fields": ", ".join(missing),
    })

    sent_at = util.now_iso()
    recipient = f"receiving@{(record.vendor_name or 'vendor').lower().replace(' ', '')}.example"
    vendor_store.log_send(conn, {
        "extracted_record_id": row.id, "po_number": record.po_number, "sent_to": recipient,
        "subject": rendered["subject"], "rendered_body": rendered["body"], "sent_at": sent_at,
        "sent_by": operator, "status": "mock_sent",
    })
    return schemas.VendorSendResponse(
        sent_to=recipient, subject=rendered["subject"], rendered_body=rendered["body"],
        sent_at=sent_at, status="mock_sent", po_number=record.po_number,
    )
