"""The delivery report — every delivery-related mail, in three buckets by delivery status.

Bucketing is by what the mail actually said happened (delivered / still moving / cancelled),
which is the question someone chasing an order is asking. The pipeline's own triage category
travels along on each row for anyone who wants the automation's view instead.
"""
from typing import Dict, List

from fastapi import APIRouter, Depends

from api import deps, schemas
from api.stores import emails_store

router = APIRouter(prefix="/api/delivery-report", tags=["delivery"])

# status_keyword -> bucket. Anything unrecognised falls to the exception bucket rather than
# being dropped, so the report always accounts for every mail.
_BUCKETS = (
    ("delivered_received", "Delivered / Received", {"delivered", "received"}),
    ("in_transit", "In-Transit / Out-for-Delivery", {"out_for_delivery", "shipped"}),
    ("cancelled", "Cancelled / Exception", {"cancelled"}),
)
_FALLBACK_BUCKET = "cancelled"


@router.get("", response_model=schemas.DeliveryReport)
def delivery_report(conn=Depends(deps.get_conn)) -> schemas.DeliveryReport:
    emails = emails_store.list_all(conn)

    grouped: Dict[str, List[schemas.DeliveryRow]] = {key: [] for key, _, _ in _BUCKETS}
    for email in emails:
        bucket_key = next(
            (key for key, _, keywords in _BUCKETS if email.status_keyword in keywords),
            _FALLBACK_BUCKET,
        )
        grouped[bucket_key].append(schemas.DeliveryRow(
            email_id=email.email_id, received_at=email.received_at, subject=email.subject,
            sender_address=email.sender_address, po_number=email.po_number,
            notification_type=email.notification_type, triage_category=email.triage_category,
            status_keyword=email.status_keyword, proposed_folder=email.proposed_folder,
            has_attachment=email.has_attachment,
        ))

    return schemas.DeliveryReport(
        total=len(emails),
        buckets=[
            schemas.DeliveryBucket(
                key=key, label=label, count=len(grouped[key]), rows=grouped[key]
            )
            for key, label, _ in _BUCKETS
        ],
    )
