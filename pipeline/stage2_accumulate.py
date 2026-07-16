import base64
import json
import sqlite3
from datetime import datetime, timedelta
from typing import List, Optional

from config import settings
from pipeline.models import (
    AccumulationKey,
    Attachment,
    DeliveryEvent,
    NotificationType,
    RawEmail,
    TriageCategory,
    TriagedEmail,
)

FINAL_EVENT_TYPES = {NotificationType.WAREHOUSE_INBOUND, NotificationType.INBOUND_NOTIFICATION}


def _log(message: str) -> None:
    print(f"[stage2_accumulate] {message}")


def _serialize_triaged_email(te: TriagedEmail) -> str:
    return json.dumps({
        "email": {
            "email_id": te.email.email_id,
            "received_at": te.email.received_at,
            "sender_address": te.email.sender_address,
            "sender_domain": te.email.sender_domain,
            "subject": te.email.subject,
            "body_html": te.email.body_html,
            "body_text": te.email.body_text,
            "attachments": [
                {
                    "filename": a.filename,
                    "content_type": a.content_type,
                    "content_b64": base64.b64encode(a.content_bytes).decode("ascii"),
                }
                for a in te.email.attachments
            ],
        },
        "notification_type": te.notification_type.value,
        "category": te.category.value,
        "matched_rule": te.matched_rule,
        "extracted_po_hints": te.extracted_po_hints,
        "extracted_shipment_hint": te.extracted_shipment_hint,
        "reason": te.reason,
    })


def _deserialize_triaged_email(payload: str) -> TriagedEmail:
    data = json.loads(payload)
    ed = data["email"]
    attachments = [
        Attachment(
            filename=a["filename"],
            content_type=a["content_type"],
            content_bytes=base64.b64decode(a["content_b64"]),
        )
        for a in ed["attachments"]
    ]
    email = RawEmail(
        email_id=ed["email_id"], received_at=ed["received_at"],
        sender_address=ed["sender_address"], sender_domain=ed["sender_domain"],
        subject=ed["subject"], body_html=ed["body_html"], body_text=ed["body_text"],
        attachments=attachments,
    )
    return TriagedEmail(
        email=email,
        notification_type=NotificationType(data["notification_type"]),
        category=TriageCategory(data["category"]),
        matched_rule=data["matched_rule"],
        extracted_po_hints=data["extracted_po_hints"],
        extracted_shipment_hint=data["extracted_shipment_hint"],
        reason=data["reason"],
    )


def _is_released(conn: sqlite3.Connection, po_number: str, shipment_number: Optional[str]) -> bool:
    if shipment_number is None:
        row = conn.execute(
            "SELECT 1 FROM released_events WHERE po_number = ? AND shipment_number IS NULL", (po_number,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM released_events WHERE po_number = ? AND shipment_number = ?",
            (po_number, shipment_number),
        ).fetchone()
    return row is not None


def _bundle_for_key(conn: sqlite3.Connection, po_number: str, shipment_number: Optional[str]) -> List[TriagedEmail]:
    if shipment_number is None:
        rows = conn.execute(
            "SELECT payload_json FROM accumulation WHERE po_number = ? AND shipment_number IS NULL", (po_number,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT payload_json FROM accumulation WHERE po_number = ? AND shipment_number = ?",
            (po_number, shipment_number),
        ).fetchall()
    return [_deserialize_triaged_email(row[0]) for row in rows]


def _mark_released(conn: sqlite3.Connection, po_number: str, shipment_number: Optional[str], now: str, reason: str) -> None:
    conn.execute(
        "INSERT INTO released_events (po_number, shipment_number, released_at, release_reason) VALUES (?, ?, ?, ?)",
        (po_number, shipment_number, now, reason),
    )
    conn.commit()


def process_triaged_email(conn: sqlite3.Connection, te: TriagedEmail, now: str) -> List[DeliveryEvent]:
    """Runs one already-filtered (SURFACE/HOLD) TriagedEmail through the release algorithm.

    Returns zero or more released DeliveryEvents — usually zero or one; more than one only when
    this single email referenced multiple POs (see BuildPlan/STAGE_2_ACCUMULATE.md, Multi-PO Handling).
    One email's processing never raises — a per-PO failure is logged and skipped, per the
    defensive principles in ORCHESTRATOR_DESIGN.md.
    """
    released = []
    for po_number in (te.extracted_po_hints or []):
        try:
            shipment_number = te.extracted_shipment_hint

            if _is_released(conn, po_number, shipment_number):
                _log(f"duplicate notice for an already-released delivery: {te.email.email_id} / PO {po_number}")
                continue

            conn.execute(
                "INSERT OR IGNORE INTO accumulation "
                "(po_number, shipment_number, email_id, notification_type, category, received_at, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (po_number, shipment_number, te.email.email_id, te.notification_type.value,
                 te.category.value, now, _serialize_triaged_email(te)),
            )
            conn.commit()

            if te.category == TriageCategory.SURFACE and te.notification_type in FINAL_EVENT_TYPES:
                bundle = _bundle_for_key(conn, po_number, shipment_number)
                _mark_released(conn, po_number, shipment_number, now, "true final event received")
                released.append(DeliveryEvent(
                    key=AccumulationKey(po_number=po_number, shipment_number=shipment_number),
                    trigger_notification_type=te.notification_type,
                    emails=bundle, released_at=now, release_reason="true final event received",
                ))
        except Exception as e:
            _log(f"error accumulating {te.email.email_id} for PO {po_number}: {e}")
            continue

    return released


def sweep_stale_holds(conn: sqlite3.Connection, now: datetime) -> List[DeliveryEvent]:
    """Releases HOLD-only deliveries (e.g. direct-to-property confirmations with no warehouse
    leg) whose oldest notice is older than HOLD_GRACE_PERIOD_HOURS. Run once per Ingest
    Orchestrator pass, after processing all new mail.
    """
    released = []
    cutoff = (now - timedelta(hours=settings.HOLD_GRACE_PERIOD_HOURS)).isoformat()

    groups = conn.execute("""
        SELECT a.po_number, a.shipment_number, MIN(a.received_at) as oldest
        FROM accumulation a
        WHERE NOT EXISTS (
            SELECT 1 FROM released_events r
            WHERE r.po_number = a.po_number
              AND (r.shipment_number = a.shipment_number
                   OR (r.shipment_number IS NULL AND a.shipment_number IS NULL))
        )
        GROUP BY a.po_number, a.shipment_number
        HAVING oldest < ?
    """, (cutoff,)).fetchall()

    for po_number, shipment_number, _oldest in groups:
        bundle = _bundle_for_key(conn, po_number, shipment_number)
        hold_emails = [te for te in bundle if te.category == TriageCategory.HOLD]
        if not hold_emails:
            continue  # nothing HOLD-worthy accumulated here yet — leave it waiting

        now_iso = now.isoformat()
        reason = "grace period elapsed, using confirmation as trigger"
        _mark_released(conn, po_number, shipment_number, now_iso, reason)
        released.append(DeliveryEvent(
            key=AccumulationKey(po_number=po_number, shipment_number=shipment_number),
            trigger_notification_type=hold_emails[0].notification_type,
            emails=bundle, released_at=now_iso, release_reason=reason,
        ))
    return released
