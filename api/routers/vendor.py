"""The vendor chase template — one format we actually accept.

Rather than parsing whatever each vendor invents, we ask the under-reporting ones for exactly
the fields reconciliation needs. The schedule control is stored but nothing is dispatched: this
phase has no send permission, and the demo must not mail anyone.
"""
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException

from api import deps, schemas, util
from api.stores import extracted_store, vendor_store

router = APIRouter(prefix="/api/vendor", tags=["vendor"])


def _to_schema(template: vendor_store.VendorTemplate) -> schemas.VendorTemplateSchema:
    return schemas.VendorTemplateSchema(
        subject=template.subject, body=template.body, schedule_hours=template.schedule_hours,
        enabled=template.enabled, updated_at=template.updated_at, updated_by=template.updated_by,
        placeholders=list(vendor_store.TEMPLATE_PLACEHOLDERS),
    )


@router.get("/template", response_model=schemas.VendorTemplateSchema)
def get_template(conn=Depends(deps.get_conn)) -> schemas.VendorTemplateSchema:
    return _to_schema(vendor_store.get_template(conn))


@router.put("/template", response_model=schemas.VendorTemplateSchema)
def update_template(
    payload: schemas.VendorTemplateUpdate,
    conn=Depends(deps.get_conn),
    operator: str = Depends(deps.get_operator),
) -> schemas.VendorTemplateSchema:
    return _to_schema(vendor_store.save_template(
        conn, util.now_iso(), operator,
        subject=payload.subject, body=payload.body,
        schedule_hours=payload.schedule_hours, enabled=payload.enabled,
    ))


@router.post("/template/schedule", response_model=schemas.ScheduleResponse)
def set_schedule(
    payload: schemas.ScheduleRequest,
    conn=Depends(deps.get_conn),
    operator: str = Depends(deps.get_operator),
) -> schemas.ScheduleResponse:
    """Stores the cadence. No scheduler runs yet — wiring this to the real orchestrator is a
    later step, and pretending otherwise would be misleading."""
    template = vendor_store.save_template(
        conn, util.now_iso(), operator,
        schedule_hours=payload.every_n_hours, enabled=payload.enabled,
    )
    return schemas.ScheduleResponse(
        enabled=template.enabled,
        every_n_hours=template.schedule_hours,
        next_run_at=util.hours_from_now_iso(template.schedule_hours) if template.enabled else None,
    )


@router.post("/template/send", response_model=schemas.VendorSendResponse)
def send(
    payload: schemas.VendorSendRequest,
    conn=Depends(deps.get_conn),
    operator: str = Depends(deps.get_operator),
) -> schemas.VendorSendResponse:
    """Renders the template for one record and logs a simulated send."""
    values: Dict[str, object] = {}
    po_number = None
    record_id = payload.extracted_record_id

    if record_id is not None:
        row = extracted_store.get(conn, record_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"No such extracted record: {record_id}.")
        record = row.record
        po_number = record.po_number
        values = {
            "vendor": record.vendor_name, "po_number": record.po_number,
            "spec_code": record.spec_code, "description": record.item_description,
            "quantity": record.quantity_received, "missing_fields": "the details below",
        }

    template = vendor_store.get_template(conn)
    rendered = vendor_store.render(template, values)
    recipient = payload.to or (
        f"receiving@{(values.get('vendor') or 'vendor')}".lower().replace(" ", "") + ".example"
    )
    sent_at = util.now_iso()

    vendor_store.log_send(conn, {
        "extracted_record_id": record_id, "po_number": po_number, "sent_to": recipient,
        "subject": rendered["subject"], "rendered_body": rendered["body"], "sent_at": sent_at,
        "sent_by": operator, "status": "mock_sent",
    })
    return schemas.VendorSendResponse(
        sent_to=recipient, subject=rendered["subject"], rendered_body=rendered["body"],
        sent_at=sent_at, status="mock_sent", po_number=po_number,
    )


@router.get("/sends", response_model=List[Dict])
def list_sends(conn=Depends(deps.get_conn)) -> List[Dict]:
    return vendor_store.list_sends(conn)
