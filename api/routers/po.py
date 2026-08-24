"""Delivery status per purchase order — the lifecycle view the 8 August meeting asked for.

Two endpoints: every PO with its rolled-up status, and one PO with its lines and email audit trail.

What this deliberately does **not** show is money. The meeting asked for dollars by status, and a
`POLine` carries no rate, amount or currency — Spitfire returns `Rate` and `ContractAmount` but the
connector does not map them yet. That is M4. Quantities are what exist, so quantities are what this
reports; adding the money columns later is additive and changes nothing here.

Statuses are inferred (`api/services/po_status.py`) until M1 persists real ones. The response says so
in `derived`, and names the two statuses it cannot produce at all, rather than letting a reader
assume an inferred lifecycle is a recorded one.
"""
from collections import defaultdict
from typing import Dict, List, Sequence

from fastapi import APIRouter, Depends, HTTPException

from api import deps, schemas, util
from api.services import po_status
from api.stores import emails_store, po_lines_store, reconciliation_store
from api.stores.emails_store import DemoEmail
from api.stores.po_lines_store import POLineRow

router = APIRouter(prefix="/api/po", tags=["po"])


@router.get("", response_model=schemas.PoStatusReport)
def list_purchase_orders(conn=Depends(deps.get_conn)) -> schemas.PoStatusReport:
    lines = po_lines_store.list_all(conn)
    staged = reconciliation_store.staged_po_line_ids(conn)

    # Emails and staged receipts are fetched once for every PO rather than per row. The page exists
    # to show all of them at once, so a per-PO query here is a query per row on the only screen
    # that renders every row.
    emails_by_po: Dict[str, List[DemoEmail]] = defaultdict(list)
    for email in emails_store.list_all(conn):
        if email.po_number:
            emails_by_po[email.po_number].append(email)

    lines_by_po: Dict[str, List[POLineRow]] = defaultdict(list)
    for line in lines:
        lines_by_po[line.line.po_number].append(line)

    rows = [
        _build_row(po_number, po_lines, emails_by_po.get(po_number, []), staged)
        for po_number, po_lines in lines_by_po.items()
    ]
    rows.sort(key=lambda row: row.po_number)

    return schemas.PoStatusReport(
        generated_at=util.now_iso(),
        derived=True,
        unreachable_statuses=list(po_status.UNREACHABLE_STATUSES),
        status_labels=dict(po_status.STATUS_LABELS),
        rows=rows,
    )


@router.get("/{po_number}", response_model=schemas.PoDetail)
def purchase_order_detail(po_number: str, conn=Depends(deps.get_conn)) -> schemas.PoDetail:
    lines = po_lines_store.list_for_po(conn, po_number)
    if not lines:
        # A PO with no lines is genuinely absent, not empty: the seed's PO 999111 appears on an
        # extracted record but has no catalogue entry, which is exactly the "nothing to match"
        # case, and a 200 with zero lines would read as "this PO has nothing on it".
        raise HTTPException(status_code=404, detail=f"No such purchase order: {po_number}.")

    emails = emails_store.list_for_po(conn, po_number)
    staged = reconciliation_store.staged_po_line_ids(conn)
    statuses = po_status.statuses_for_po(lines, emails, staged)
    base = _build_row(po_number, lines, emails, staged)

    return schemas.PoDetail(
        **base.model_dump(),
        lines=[
            schemas.PoLineStatus(
                line=schemas.POLineSchema.from_row(line_status.line),
                status=line_status.status,
                label=line_status.label,
                reason=line_status.reason,
            )
            for line_status in statuses
        ],
        emails=[
            schemas.DeliveryRow(
                email_id=email.email_id, received_at=email.received_at, subject=email.subject,
                sender_address=email.sender_address, po_number=email.po_number,
                notification_type=email.notification_type,
                triage_category=email.triage_category, status_keyword=email.status_keyword,
                proposed_folder=email.proposed_folder, has_attachment=email.has_attachment,
            )
            for email in emails
        ],
    )


def _build_row(
    po_number: str,
    lines: Sequence[POLineRow],
    emails: Sequence[DemoEmail],
    staged_line_ids: set,
) -> schemas.PoStatusRow:
    statuses = po_status.statuses_for_po(lines, emails, staged_line_ids)
    rolled_up = po_status.rollup(statuses) or po_status.OPEN
    first = lines[0].line

    return schemas.PoStatusRow(
        po_number=po_number,
        # A PO is one vendor's in this data. Taken from the first line rather than asserted, so a
        # future mixed-vendor PO renders something instead of raising.
        vendor_name=first.vendor_name,
        project_code=first.project_code,
        project_name=first.project_name,
        status=rolled_up,
        label=po_status.STATUS_LABELS[rolled_up],
        line_count=len(lines),
        counts_by_status=po_status.count_by_status(statuses),
        qty_ordered=sum(line.line.qty_ordered for line in lines),
        qty_received=sum(line.line.qty_received for line in lines),
        qty_in_transit=sum(line.line.qty_in_transit for line in lines),
        qty_outstanding=sum(line.qty_outstanding for line in lines),
        last_email_at=max((email.received_at for email in emails), default=None),
        email_count=len(emails),
    )
