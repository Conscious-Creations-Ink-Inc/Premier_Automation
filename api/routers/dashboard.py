"""Progress across the automation — the landing screen.

The funnel is the honest shape of the pipeline: mail arrives, some of it is genuinely a
receiving event, those get extracted, extracted records get reconciled, and only some of those
settle without a person. The gap between "reconciled" and "settled" is the queue.
"""
from datetime import datetime, timezone
from typing import Dict

from fastapi import APIRouter, Depends

from api import deps, schemas
from api.stores import extracted_store, reconciliation_store

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

_CONFIDENCE_KEYS = ("high", "medium", "low", "none")
_STATUS_KEYS = ("pending", "matched", "failed", "routed")
_REVIEW_KEYS = (
    reconciliation_store.REVIEW_AUTO_APPROVED, reconciliation_store.REVIEW_PENDING,
    reconciliation_store.REVIEW_APPROVED, reconciliation_store.REVIEW_CANCELLED,
)


def _with_zeros(counts: Dict[str, int], keys) -> Dict[str, int]:
    """Charts need every category present, including the empty ones, or the axis jumps around
    as the demo is clicked through."""
    filled = {key: 0 for key in keys}
    filled.update(counts)
    return filled


@router.get("/summary", response_model=schemas.DashboardSummary)
def summary(conn=Depends(deps.get_conn)) -> schemas.DashboardSummary:
    def scalar(sql: str, params=()) -> int:
        return conn.execute(sql, params).fetchone()[0]

    emails = scalar("SELECT COUNT(*) FROM demo_emails")
    receiving_events = scalar(
        "SELECT COUNT(*) FROM demo_emails WHERE triage_category IN ('surface','hold')"
    )
    extracted = extracted_store.count(conn)
    matches = scalar("SELECT COUNT(*) FROM match_results")
    settled = scalar(
        "SELECT COUNT(*) FROM match_results WHERE review_status IN (?, ?)",
        (reconciliation_store.REVIEW_AUTO_APPROVED, reconciliation_store.REVIEW_APPROVED),
    )
    receipts = reconciliation_store.count_receipts(conn)

    by_review = _with_zeros(reconciliation_store.counts_by(conn, "review_status"), _REVIEW_KEYS)
    by_status = _with_zeros(extracted_store.counts_by_status(conn), _STATUS_KEYS)
    by_confidence = _with_zeros(reconciliation_store.counts_by(conn, "confidence"), _CONFIDENCE_KEYS)
    queue = by_review[reconciliation_store.REVIEW_PENDING]

    return schemas.DashboardSummary(
        generated_at=datetime.now(timezone.utc).isoformat(),
        totals={
            "emails": emails,
            "receiving_events": receiving_events,
            "extracted_records": extracted,
            "reconciled": matches,
            "flagged": scalar("SELECT COUNT(*) FROM match_results WHERE flagged = 1"),
            "auto_approved": by_review[reconciliation_store.REVIEW_AUTO_APPROVED],
            "approved": by_review[reconciliation_store.REVIEW_APPROVED],
            "cancelled": by_review[reconciliation_store.REVIEW_CANCELLED],
            "staged_receipts": receipts,
            "po_lines": scalar("SELECT COUNT(*) FROM po_lines"),
        },
        funnel=[
            schemas.FunnelStage(key="ingested", label="Mail ingested", count=emails),
            schemas.FunnelStage(key="receiving", label="Receiving events", count=receiving_events),
            schemas.FunnelStage(key="extracted", label="Records extracted", count=extracted),
            schemas.FunnelStage(key="reconciled", label="Reconciled", count=matches),
            schemas.FunnelStage(key="settled", label="Settled", count=settled),
            schemas.FunnelStage(key="receipts", label="Receipts staged", count=receipts),
        ],
        by_status=by_status,
        by_confidence=by_confidence,
        by_review_status=by_review,
        by_route_target=reconciliation_store.counts_by(conn, "route_target"),
        exception_queue_size=queue,
    )
