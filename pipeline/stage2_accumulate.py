import base64
import json
import sqlite3
from datetime import datetime, timedelta
from typing import List, Optional

from config import settings
from pipeline import attachment_store, dedupe
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


def _embedded_bytes(attachment: Attachment) -> str:
    """The base64 copy to persist inline — empty whenever the content-addressed store has it.

    This payload used to carry every attachment's bytes base64'd into the JSON, and that one field
    became **1,870 MB of a 1,930 MB database**: 98% of the payload, at a median of 921 KB a row and
    43 MB for the worst. Accumulation rows are per (PO, shipment), so an email touching three POs
    embedded its attachments three times over — measured at 2.2x duplication, with one 9.3 MB file
    written twelve times.

    All of it was already on disk. `attachment_store` is content-addressed by sha256 and holds
    every attachment carrying bytes; the embedded copy bought nothing but size.

    The check is `exists()` rather than a blanket omission on purpose: **a byte is only dropped
    once its replacement is confirmed present.** An attachment the store does not have — one
    refused as oversize, say — keeps its inline copy and behaves exactly as before. That is what
    makes this safe to deploy against rows written by either version.
    """
    if not attachment.content_bytes:
        return ""
    if attachment.sha256 and attachment_store.exists(attachment.sha256):
        return ""
    return base64.b64encode(attachment.content_bytes).decode("ascii")


def _attachment_bytes(serialized: dict) -> bytes:
    """Bytes back out, from wherever this row happens to keep them.

    Reads the inline copy first so rows written before `_embedded_bytes` — and rows whose blob the
    store never took — deserialize unchanged. Otherwise the store answers.

    A miss yields `b""` rather than raising. That is the same state an attachment the connector
    dropped has always produced, and the pipeline already handles it; a store gap must degrade a
    single attachment, never fail the delivery it belongs to.
    """
    inline = serialized.get("content_b64")
    if inline:
        return base64.b64decode(inline)
    sha = serialized.get("sha256") or ""
    return (attachment_store.get(sha) or b"") if sha else b""


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
                    # Normally empty: the bytes live in the content-addressed store and are
                    # fetched back by sha256. Populated only when the store does not have them,
                    # so nothing is ever dropped without a confirmed replacement. See
                    # `_embedded_bytes` for what this field cost before.
                    "content_b64": _embedded_bytes(a),
                    "content_id": a.content_id,
                    "is_inline": a.is_inline,
                    "sha256": a.sha256,
                    "size_bytes": a.size_bytes,
                    "drop_hint": a.drop_hint,
                    "ledger_id": a.ledger_id,
                    "container_path": a.container_path,
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
        "origin_sender_address": te.origin_sender_address,
        "origin_sent_at": te.origin_sent_at,
        "notification_number": te.notification_number,
    })


def _deserialize_triaged_email(payload: str) -> TriagedEmail:
    data = json.loads(payload)
    ed = data["email"]
    attachments = [
        Attachment(
            filename=a["filename"],
            content_type=a["content_type"],
            content_bytes=_attachment_bytes(a),
            content_id=a.get("content_id"),
            is_inline=a.get("is_inline", False),
            sha256=a.get("sha256", ""),
            size_bytes=a.get("size_bytes", 0),
            drop_hint=a.get("drop_hint"),
            ledger_id=a.get("ledger_id"),
            container_path=a.get("container_path", ""),
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
        origin_sender_address=data.get("origin_sender_address"),
        # `.get`, like its neighbours: a payload written before this field existed must still
        # deserialize rather than KeyError a stored accumulation into an unreadable state.
        origin_sent_at=data.get("origin_sent_at"),
        notification_number=data.get("notification_number"),
    )


def _key_for(conn: sqlite3.Connection, te: TriagedEmail, po_number: str) -> AccumulationKey:
    """Which delivery on `po_number` this message is about.

    The whole key used to be `(po_number, shipment_number)`, and `shipment_number` is NULL on the
    majority of real traffic — 22 of the 28 released events in Premier's live store. With a NULL in
    it the key collapsed to the purchase order, so a second genuine delivery on one PO was either
    merged into the first or, if it arrived after release, discarded outright.

    `dedupe.delivery_ref` resolves the rest of the key from what the message actually states, and
    is never empty. `shipment_number` is still carried for display and is still rung 1, so the A↔B
    join that pairs an Inbound with its Delivered notice is unchanged.
    """
    ref, rung = dedupe.ref_for_email(
        conn, te.email.email_id,
        shipment_number=te.extracted_shipment_hint,
        notification_number=te.notification_number)
    return AccumulationKey(po_number=po_number, shipment_number=te.extracted_shipment_hint,
                           delivery_ref=ref, delivery_rung=rung)


def _is_released(conn: sqlite3.Connection, key: AccumulationKey):
    """The release row for this delivery, or None. Returns the row rather than a bool so the
    suppression it causes can record *when* the delivery it duplicates was released."""
    prior = conn.row_factory
    conn.row_factory = None
    try:
        return conn.execute(
            "SELECT released_at, release_reason FROM released_events "
            "WHERE po_number = ? AND delivery_ref = ?",
            (key.po_number, key.delivery_ref),
        ).fetchone()
    finally:
        conn.row_factory = prior


def _record_suppression(conn: sqlite3.Connection, email_id: str, key: AccumulationKey,
                        released_at, now: str) -> None:
    """Write down a notice we deliberately did not accumulate.

    Without this the skip leaves no trace at all, and the email — no accumulation row, no record —
    falls into `read_views.manual_queue`'s residual bucket and is shown to a person as "nothing was
    extracted from it", which is both false and unactionable. What actually happened is that the
    delivery had already been staged from an earlier message, and that is a sentence somebody can
    do something with.
    """
    conn.execute(
        "INSERT OR IGNORE INTO suppressed_notices "
        "(email_id, po_number, delivery_ref, released_at, suppressed_at, reason) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (email_id, key.po_number, key.delivery_ref or "", released_at, now,
         "the same delivery was already released and staged from an earlier message"),
    )
    conn.commit()


def _floating_refs(conn: sqlite3.Connection, key: AccumulationKey) -> List[str]:
    """Un-released evidence on this purchase order that names no delivery of its own.

    A property reply saying the goods arrived is about *a* delivery and cannot say which. It
    accumulates under its own ref so it is never lost, and is absorbed here by the first identified
    delivery on the same purchase order to release — which is the Inbound notice it was always
    evidence for.

    Only ever absorbed *into* an identifying delivery. Floating evidence does not absorb other
    floating evidence: two undated property replies on one PO are exactly the case nobody can
    resolve from the mail, and merging them silently would be the nullable-key bug returning under
    a new name.
    """
    if key.delivery_rung not in dedupe.IDENTIFYING_RUNGS:
        return []
    placeholders = ", ".join("?" * len(dedupe.FLOATING_RUNGS))
    rows = conn.execute(
        f"""SELECT DISTINCT a.delivery_ref FROM accumulation a
             WHERE a.po_number = ? AND a.delivery_rung IN ({placeholders})
               AND NOT EXISTS (SELECT 1 FROM released_events r
                                WHERE r.po_number = a.po_number
                                  AND r.delivery_ref = a.delivery_ref)""",
        (key.po_number, *dedupe.FLOATING_RUNGS),
    ).fetchall()
    return [str(row[0]) for row in rows if row[0]]


def _bundle_for_key(conn: sqlite3.Connection, key: AccumulationKey) -> List[TriagedEmail]:
    """Every message accumulated for this delivery, plus the floating evidence it absorbs."""
    refs = [key.delivery_ref] + _floating_refs(conn, key)
    placeholders = ", ".join("?" * len(refs))
    # `COALESCE` over the two homes a payload can have. New rows keep it in `accumulation_payload`,
    # one copy per message; rows written before that table existed still carry it inline. Reading
    # both means neither the migration nor its absence can change what this returns.
    rows = conn.execute(
        f"SELECT COALESCE(NULLIF(a.payload_json, ''), p.payload_json) "
        f"  FROM accumulation a "
        f"  LEFT JOIN accumulation_payload p ON p.email_id = a.email_id "
        f" WHERE a.po_number = ? AND a.delivery_ref IN ({placeholders}) "
        f" ORDER BY a.received_at, a.email_id",
        (key.po_number, *refs),
    ).fetchall()
    # A row whose payload is in neither place is a row we cannot reconstruct the message from.
    # Skipping it is right — it is missing evidence, not an empty message — and silently handing a
    # bundle a `None` would raise inside the deserializer with nothing naming the cause.
    return [_deserialize_triaged_email(row[0]) for row in rows if row[0]]


def _mark_released(conn: sqlite3.Connection, key: AccumulationKey, now: str, reason: str,
                   absorbed: Optional[List[str]] = None) -> None:
    # Claim the floating evidence first, so a second identified delivery on this purchase order
    # cannot absorb it as well and bundle the same message into two receivers.
    for ref in (absorbed or []):
        conn.execute(
            "UPDATE accumulation SET delivery_ref = ?, delivery_rung = ? "
            "WHERE po_number = ? AND delivery_ref = ?",
            (key.delivery_ref, key.delivery_rung, key.po_number, ref))
    # OR IGNORE, because the unique index on (po_number, delivery_ref) is now the authority on
    # whether this delivery has already fired. A plain INSERT would raise where the old nullable
    # key silently appended a second row.
    conn.execute(
        "INSERT OR IGNORE INTO released_events "
        "(po_number, shipment_number, released_at, release_reason, delivery_ref, delivery_rung) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (key.po_number, key.shipment_number, now, reason, key.delivery_ref, key.delivery_rung),
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
            key = _key_for(conn, te, po_number)

            # Not named `released` — that is the accumulator this function returns, and shadowing
            # it here makes the function return None for every email.
            prior_release = _is_released(conn, key)
            if prior_release is not None:
                _log(f"duplicate notice for an already-released delivery: "
                     f"{te.email.email_id} / PO {po_number} / {key.delivery_ref}")
                _record_suppression(conn, te.email.email_id, key, prior_release[0], now)
                continue

            # The payload once per message, in its own table; the accumulation row then carries
            # only which delivery it belongs to. A message naming 74 purchase orders used to store
            # its whole base64 body 74 times — 173 MB from one email, and 2.9x across the store.
            #
            # `payload_json` stays on the row and is written empty rather than dropped: rows
            # written before this table existed still hold their payload there, and `_bundle_for_key`
            # reads whichever of the two has it. That is what lets the backlog be migrated on
            # somebody's schedule instead of this change needing a migration to be correct.
            conn.execute(
                "INSERT OR IGNORE INTO accumulation_payload (email_id, payload_json) VALUES (?, ?)",
                (te.email.email_id, _serialize_triaged_email(te)),
            )
            conn.execute(
                "INSERT OR IGNORE INTO accumulation "
                "(po_number, shipment_number, email_id, notification_type, category, received_at, "
                " payload_json, delivery_ref, delivery_rung) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (po_number, key.shipment_number, te.email.email_id, te.notification_type.value,
                 te.category.value, now, "",
                 key.delivery_ref, key.delivery_rung),
            )
            conn.commit()

            if te.category == TriageCategory.SURFACE and te.notification_type in FINAL_EVENT_TYPES:
                absorbed = _floating_refs(conn, key)
                bundle = _bundle_for_key(conn, key)
                _mark_released(conn, key, now, "true final event received", absorbed=absorbed)
                released.append(DeliveryEvent(
                    key=key,
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

    # Grouped and joined on `delivery_ref` rather than on a nullable `shipment_number`. The old
    # join had to spell out `IS NULL AND IS NULL` because SQL will not equate two NULLs — which is
    # the same defect as the primary key, in query form: every shipment-less delivery on one PO
    # was one group, so a second one could never sweep out on its own.
    groups = conn.execute("""
        SELECT a.po_number, a.shipment_number, a.delivery_ref, a.delivery_rung,
               MIN(a.received_at) as oldest
        FROM accumulation a
        WHERE NOT EXISTS (
            SELECT 1 FROM released_events r
            WHERE r.po_number = a.po_number AND r.delivery_ref = a.delivery_ref
        )
        GROUP BY a.po_number, a.delivery_ref
        HAVING oldest < ?
    """, (cutoff,)).fetchall()

    for po_number, shipment_number, delivery_ref, delivery_rung, _oldest in groups:
        key = AccumulationKey(po_number=po_number, shipment_number=shipment_number,
                              delivery_ref=delivery_ref or "", delivery_rung=delivery_rung or "")
        bundle = _bundle_for_key(conn, key)
        hold_emails = [te for te in bundle if te.category == TriageCategory.HOLD]
        if not hold_emails:
            continue  # nothing HOLD-worthy accumulated here yet — leave it waiting

        now_iso = now.isoformat()
        reason = "grace period elapsed, using confirmation as trigger"
        _mark_released(conn, key, now_iso, reason)
        released.append(DeliveryEvent(
            key=key,
            trigger_notification_type=hold_emails[0].notification_type,
            emails=bundle, released_at=now_iso, release_reason=reason,
        ))
    return released
