"""Nothing may fall between the pages, and every queued item must say why.

The two record pages no longer partition the work — readiness and completeness are different
questions and a record can be both ready and incomplete — but a record, an email or an attachment
belonging to *no* page would be work nobody was ever told about. That is the failure mode the
manual queue exists to prevent, so it is asserted rather than assumed.
"""
import pytest

from pipeline import (attachment_ledger, delivery_status, email_log, extracted_records_store,
                      read_views, state_db)
from pipeline.models import ExtractedRecord, NotificationType

NOW = "2026-08-03T12:00:00Z"


def new_conn():
    return state_db.get_connection(":memory:")


def make_record(**overrides) -> ExtractedRecord:
    defaults = dict(
        source_email_id="msg-1", po_number="208491", shipment_number=None,
        spec_code="STE-402-LT", parent_spec_code="STE-402-LT", sub_spec_suffix=None,
        item_description="Side table", vendor_name=None, carrier_name=None, tracking_number=None,
        quantity_received=11.0, unit_of_measure="EA", pod_stated_date=None,
        email_date="2026-06-08T14:00:00Z", delivery_location=None, comments=None,
        extraction_source="html_table", extraction_confidence=0.9, raw_snippet="…",
    )
    defaults.update(overrides)
    return ExtractedRecord(**defaults)


def seed_email(conn, email_id="msg-1", category="surface", folder="Processed", reason="rule matched"):
    email_log.record(
        conn, email_id=email_id, subject=f"Subject for {email_id}", sender="a@b.com",
        category=category, matched_rule="rule_1a", reason=reason, folder=folder,
        processed_at=NOW,
    )


def make_complete_record(**overrides) -> ExtractedRecord:
    """A record carrying every field `completeness.REQUIRED` asks for."""
    defaults = dict(
        po_line_number=1, vendor_name="P. Kaufmann", pod_stated_date="2025-09-10",
        received_by="U ALI", carrier_name="GlobalTranz", tracking_number="31457971",
    )
    defaults.update(overrides)
    return make_record(**defaults)


def test_no_pending_record_is_missing_from_both_pages():
    """The half of the old partition that still holds. `records_ready` and the record arm of
    `manual_queue` now *overlap* — readiness and completeness are different questions — but a
    record on neither page would be work nobody was ever told about."""
    c = new_conn()
    seed_email(c)
    extracted_records_store.write_pending(c, make_complete_record(), NOW)
    extracted_records_store.write_pending(c, make_record(po_number=""), NOW)
    extracted_records_store.write_pending(c, make_record(extraction_confidence=0.0), NOW)
    extracted_records_store.write_pending(
        c, make_record(extraction_source="html_table+quantity_conflict"), NOW)

    ready = {r["id"] for r in read_views.records_ready(c)}
    manual = {i.ref_id for i in read_views.manual_queue(c) if i.kind == "record"}
    all_pending = {row[0] for row in c.execute(
        "SELECT id FROM extracted_records WHERE status = 'pending'")}

    assert ready | manual == all_pending


def test_a_complete_record_is_ready_and_not_in_the_queue():
    c = new_conn()
    seed_email(c)
    complete = extracted_records_store.write_pending(c, make_complete_record(), NOW)

    assert complete in {r["id"] for r in read_views.records_ready(c)}
    assert complete not in {i.ref_id for i in read_views.manual_queue(c) if i.kind == "record"}


def test_a_record_with_no_proof_of_delivery_is_queued_even_though_it_is_ready():
    """The case that prompted the contract. A PO and a confidence above zero was all `_READY_CLAUSE`
    ever asked for, so three live records sat under "Ready to process further" with no delivery
    date, carrier, tracking or signature — nothing to compare against a purchase order. It stays
    ready (nothing is blocked) and it is also listed for a person."""
    c = new_conn()
    seed_email(c)
    no_pod = extracted_records_store.write_pending(
        c, make_complete_record(pod_stated_date=None, received_by=None), NOW)

    assert no_pod in {r["id"] for r in read_views.records_ready(c)}
    item = [i for i in read_views.manual_queue(c) if i.ref_id == no_pod][0]
    assert "POD date" in item.reason
    # `received_by` is blank here too and is deliberately *not* named: it is `DERIVED` since
    # 2026-08-21, read from the POD's signature block or supplied by the reviewer who accepts the
    # record. Listing it sent people looking for a cell to type into that they should not fill.
    assert "received-by" not in item.reason


def test_a_record_blocked_two_ways_states_both_reasons():
    c = new_conn()
    seed_email(c)
    extracted_records_store.write_pending(
        c, make_record(po_number="", extraction_confidence=0.0), NOW)

    item = [i for i in read_views.manual_queue(c) if i.kind == "record"][0]
    assert "PO number" in item.reason
    assert "confidence 0.0" in item.reason


def test_the_queue_names_the_missing_fields_not_a_score():
    """"confidence 0.0" was true of ten of the thirteen live records and told nobody which cells
    to fill."""
    c = new_conn()
    seed_email(c)
    extracted_records_store.write_pending(
        c, make_record(spec_code=None, item_description=None, quantity_received=None), NOW)

    reason = [i for i in read_views.manual_queue(c) if i.kind == "record"][0].reason
    assert reason.startswith("missing: ")
    for field_name in ("spec code", "description", "quantity", "POD date"):
        assert field_name in reason
    # And never a field somebody would be wrong to type — see `completeness.DERIVED`.
    for derived in ("vendor", "PO line #", "received-by"):
        assert derived not in reason


def test_every_manual_item_states_a_reason():
    c = new_conn()
    seed_email(c, email_id="msg-routed", category="route", folder="Routed",
               reason="no PO reference anywhere in the mail")
    seed_email(c, email_id="msg-broken", category="error", folder="Errors", reason="")
    extracted_records_store.write_pending(c, make_record(po_number=""), NOW)

    items = read_views.manual_queue(c)
    assert items
    for item in items:
        assert item.reason.strip(), f"{item.kind} {item.ref} was queued without a reason"


def test_an_error_row_with_no_reason_still_gets_one():
    """email_log.reason can legitimately be blank; the queue must not render an empty cell."""
    c = new_conn()
    seed_email(c, email_id="msg-broken", category="error", folder="Errors", reason="")
    item = [i for i in read_views.manual_queue(c) if i.kind == "email"][0]
    assert "processing error" in item.reason


def test_summary_counts_line_up_with_the_pages():
    c = new_conn()
    seed_email(c, email_id="msg-1")
    seed_email(c, email_id="msg-routed", category="route", folder="Routed", reason="routed")
    extracted_records_store.write_pending(c, make_record(), NOW)
    extracted_records_store.write_pending(c, make_record(po_number=""), NOW)

    s = read_views.summary(c)
    assert s.emails == 2
    assert s.by_category == {"surface": 1, "route": 1}
    assert s.records_total == 2
    assert s.records_ready == len(read_views.records_ready(c)) == 1
    assert s.needs_human == len(read_views.manual_queue(c))


def test_mails_view_counts_records_produced_per_email():
    c = new_conn()
    seed_email(c, email_id="msg-1")
    seed_email(c, email_id="msg-2")
    extracted_records_store.write_pending(c, make_record(source_email_id="msg-1"), NOW)
    extracted_records_store.write_pending(c, make_record(source_email_id="msg-1"), NOW)

    by_id = {m.email_id: m for m in read_views.mails(c)}
    assert by_id["msg-1"].records == 2
    assert by_id["msg-2"].records == 0


def test_views_are_empty_and_do_not_raise_before_any_run():
    c = new_conn()
    assert read_views.mails(c) == []
    assert read_views.records_ready(c) == []
    assert read_views.manual_queue(c) == []
    assert read_views.po_delivery_status(c) == []
    assert read_views.summary(c).emails == 0


# --- delivery status per PO ---------------------------------------------------


def accumulate(conn, po_number, notification_type, email_id="msg-1", received_at=NOW,
               category="surface"):
    conn.execute(
        "INSERT INTO accumulation (po_number, shipment_number, email_id, notification_type, "
        "category, received_at, payload_json) VALUES (?, NULL, ?, ?, ?, ?, '{}')",
        (po_number, email_id, notification_type, category, received_at),
    )
    conn.commit()


def test_a_po_is_listed_from_its_notifications_even_with_no_extracted_records():
    """24 of the corpus's 28 POs are property confirmations with nothing machine-readable in them.
    Driving this view from extracted_records would hide exactly the rows that need a person."""
    c = new_conn()
    accumulate(c, "212614", "property_confirmation")

    [row] = read_views.po_delivery_status(c)
    assert row.po_number == "212614"
    assert row.records == 0
    assert row.status == delivery_status.DELIVERED


def test_the_most_advanced_notification_wins_for_one_po():
    """These notices track one shipment's progress, so the furthest one reached is the truth —
    the opposite of the cross-line rollup, which takes the least advanced."""
    c = new_conn()
    accumulate(c, "208491", "delivered_shipped", email_id="msg-1")
    accumulate(c, "208491", "warehouse_inbound", email_id="msg-2")

    [row] = read_views.po_delivery_status(c)
    assert row.status == delivery_status.DELIVERED
    assert row.notifications == {"delivered_shipped": 1, "warehouse_inbound": 1}


def test_a_warehouse_receipt_is_a_delivery():
    """The Authority notice reads "Received at <address> / Received By <name>". That is a receipt —
    it is the event that triggers a receiver in Spitfire, annotated by Premier as "WH rec'd" — not a
    waypoint notice. Treating it as an intermediate state left every record reading "At partnered
    warehouse" when the goods had in fact been delivered and signed for."""
    c = new_conn()
    accumulate(c, "208491", "warehouse_inbound")
    assert read_views.po_delivery_status(c)[0].status == delivery_status.DELIVERED


def test_a_warehouse_receipt_still_shows_the_warehouse_on_the_route():
    """It proves two stages, not one: the goods demonstrably passed through the partnered warehouse
    *and* that arrival was the delivery. Dropping the warehouse node would hide the route; treating
    it as the end state would misreport the status."""
    c = new_conn()
    accumulate(c, "208491", "warehouse_inbound")
    email_log.record(
        conn=c, email_id="msg-1", subject="Inbound", sender="a@b.com", category="surface",
        matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="warehouse_inbound", po_hints="208491", email_date="2025-10-09T09:00:00Z",
    )
    stages = {s.key: s for s in read_views.po_timeline(c, "208491").stages}
    assert stages[delivery_status.AT_WAREHOUSE].reached is True
    assert stages[delivery_status.DELIVERED].reached is True
    assert stages[delivery_status.AT_WAREHOUSE].on == stages[delivery_status.DELIVERED].on == "2025-10-09"
    assert delivery_status.AT_WAREHOUSE not in read_views.UNREACHABLE_STATUSES


def test_an_unknown_notification_type_leaves_the_po_open():
    """A new notification type must not be silently mapped onto a lifecycle state by accident."""
    c = new_conn()
    accumulate(c, "999999", "some_future_type")
    assert read_views.po_delivery_status(c)[0].status == delivery_status.OPEN


def test_ordered_quantity_is_none_not_zero_while_the_spitfire_mirror_is_empty():
    """Zero would read as "nothing was ordered". None reads as "we have not been told", which is
    the true state until the Spitfire pull runs."""
    c = new_conn()
    accumulate(c, "208491", "warehouse_inbound")
    row = read_views.po_delivery_status(c)[0]
    assert row.qty_ordered is None
    assert row.qty_outstanding is None
    assert row.qty_received == 0.0     # this one IS a real zero — no records extracted yet


def test_the_receipt_location_and_signer_are_carried_through():
    """"Delivered" cannot tell a reader whether the receipt address was the final destination. The
    address can, so it travels with the status rather than being left in the extraction table."""
    c = new_conn()
    seed_email(c)
    accumulate(c, "208491", "warehouse_inbound")
    extracted_records_store.write_pending(c, make_record(
        delivery_location="Crown Worldwide Moving & Storage - Mira Loma", received_by="Miguel C."), NOW)

    row = read_views.po_delivery_status(c)[0]
    assert row.received_by == "Miguel C."
    assert "Crown Worldwide" in row.delivery_location
    assert read_views.po_timeline(c, "208491").received_by == "Miguel C."


def test_records_and_quantities_roll_up_onto_the_po():
    c = new_conn()
    seed_email(c)
    accumulate(c, "208491", "warehouse_inbound")
    extracted_records_store.write_pending(c, make_record(quantity_received=11.0, po_line_number=300), NOW)
    extracted_records_store.write_pending(c, make_record(quantity_received=12.0, po_line_number=301), NOW)

    row = read_views.po_delivery_status(c)[0]
    assert row.records == 2
    assert row.qty_received == 23.0
    assert row.lines_seen == 2, "sub-parts are separate Spitfire lines and must not collapse"


def test_a_released_delivery_carries_its_reason():
    c = new_conn()
    accumulate(c, "208491", "warehouse_inbound")
    c.execute(
        "INSERT INTO released_events (po_number, shipment_number, released_at, release_reason) "
        "VALUES ('208491', '50052', ?, 'true final event received')", (NOW,),
    )
    c.commit()

    row = read_views.po_delivery_status(c)[0]
    assert row.released_at == NOW
    assert row.release_reason == "true final event received"


def route_email(conn, email_id, notification_type, po_hints, email_date="2026-06-06T09:00:00Z"):
    """A ROUTE-triaged email — cancellations and loss/claim notices. These never reach Stage 2, so
    they exist only in `email_log`, which is why the PO views are sourced from there."""
    email_log.record(
        conn, email_id=email_id, subject=f"{notification_type} notice", sender="a@b.com",
        category="route", matched_rule="rule_0a", reason="needs a person", folder="Routed",
        processed_at=NOW, notification_type=notification_type, po_hints=po_hints,
        email_date=email_date,
    )


def test_po_hints_are_split_exactly_not_matched_as_a_substring():
    """`po_hints` is a comma-space blob. A LIKE '%2084%' would match 208491 — and a purchase-order
    page showing another order's mail is worse than one showing none."""
    c = new_conn()
    route_email(c, "msg-x", "loss_or_claim", "208491, 211400")

    by_po = {r.po_number for r in read_views.po_delivery_status(c)}
    assert by_po == {"208491", "211400"}
    assert read_views.po_timeline(c, "2084") is None
    assert read_views.po_timeline(c, "208491") is not None


def test_route_only_pos_appear_at_all():
    """Cancellations and loss/claim notices are triaged ROUTE and never accumulate. Sourced from
    `accumulation` these POs were invisible; they are the exceptions most needing a person."""
    c = new_conn()
    route_email(c, "msg-loss", "loss_or_claim", "208453, 211400, 211067")

    rows = {r.po_number: r for r in read_views.po_delivery_status(c)}
    assert set(rows) == {"208453", "211400", "211067"}
    assert all(r.status == delivery_status.LOSS_OR_CLAIM for r in rows.values())


def test_a_cancellation_terminates_the_bar():
    """The corpus contains no cancellation, so this path exists only under test until one arrives."""
    c = new_conn()
    seed_email(c, email_id="msg-ship")
    accumulate(c, "208491", "delivered_shipped", email_id="msg-ship")
    email_log.record(
        conn=c, email_id="msg-ship", subject="shipped", sender="a@b.com", category="hold",
        matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="delivered_shipped", po_hints="208491", email_date="2026-06-01T09:00:00Z",
    )
    route_email(c, "msg-cancel", "order_cancellation", "208491", email_date="2026-06-05T09:00:00Z")

    timeline = read_views.po_timeline(c, "208491")
    assert timeline.status == delivery_status.CANCELLED
    assert timeline.stages[-1].terminal is True
    assert timeline.stages[-1].label == "Cancelled"
    # It still shows how far it got before it stopped, but nothing beyond that point.
    assert [s.key for s in timeline.stages] == [
        delivery_status.OPEN, delivery_status.IN_TRANSIT, delivery_status.CANCELLED,
    ]
    assert delivery_status.DELIVERED not in [s.key for s in timeline.stages]


def test_a_loss_claim_is_not_reported_as_cancelled():
    """Different events with different consequences — one needs a PO update in Spitfire, the other
    a claim against the carrier. Collapsing them sends someone to do the wrong thing."""
    c = new_conn()
    route_email(c, "msg-loss", "loss_or_claim", "211067")
    timeline = read_views.po_timeline(c, "211067")
    assert timeline.status == delivery_status.LOSS_OR_CLAIM
    assert timeline.label == "Loss or claim"
    assert timeline.status != delivery_status.CANCELLED


def test_the_timeline_keeps_events_the_accumulator_drops():
    """A notice arriving after its delivery was released is discarded as a duplicate by Stage 2. It
    is still evidence, and a timeline that loses events is worse than no timeline."""
    c = new_conn()
    for n, day in enumerate(("2025-10-02", "2025-10-09", "2026-06-06"), start=1):
        email_log.record(
            conn=c, email_id=f"msg-{n}", subject=f"Inbound {n}", sender="a@b.com",
            category="surface", matched_rule="r", reason="", folder="Processed", processed_at=NOW,
            notification_type="warehouse_inbound", po_hints="208491", email_date=f"{day}T09:00:00Z",
        )
    accumulate(c, "208491", "warehouse_inbound", email_id="msg-1")   # only one accumulated

    timeline = read_views.po_timeline(c, "208491")
    assert len(timeline.events) == 3
    assert [e.when[:10] for e in timeline.events] == ["2025-10-02", "2025-10-09", "2026-06-06"]


def _mirror_po(conn, po_number="208491", order_date=None, source_date="2025-10-08T00:00:00"):
    """A minimal `spitfire_po_index` row — the PO-number join the timeline reads its order date
    from."""
    conn.execute(
        "INSERT OR REPLACE INTO spitfire_po_index "
        "(po_number, doc_master_key, source_date, order_date, refreshed_at) VALUES (?,?,?,?,?)",
        (po_number, "key-1", source_date, order_date, NOW),
    )
    conn.commit()


def test_the_order_date_is_never_inferred_from_mail():
    """The reported bug: PO 208491 read `Ordered 2025-10-02`, a day *after* it was delivered,
    because the node took "the first email we happened to see". No mail states when a PO was
    raised — it is a property of the order, not of any delivery."""
    c = new_conn()
    email_log.record(
        conn=c, email_id="msg-1", subject="Inbound", sender="a@b.com", category="surface",
        matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="warehouse_inbound", po_hints="208491", email_date="2025-10-02T09:00:00Z",
    )
    stages = {s.key: s for s in read_views.po_timeline(c, "208491").stages}
    ordered = stages[delivery_status.OPEN]
    assert ordered.reached is True, "the PO exists — it is named in mail"
    assert ordered.on == "", "but nothing here says when it was raised"
    assert not any(s.on and s.on < "2025-10-02" for s in stages.values() if s.reached)


def test_the_order_date_comes_from_the_purchase_order():
    c = new_conn()
    _mirror_po(c, "208491", order_date="2025-09-30T00:00:00")
    email_log.record(
        conn=c, email_id="msg-1", subject="Inbound", sender="a@b.com", category="surface",
        matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="warehouse_inbound", po_hints="208491", email_date="2025-10-02T09:00:00Z",
    )
    stages = {s.key: s for s in read_views.po_timeline(c, "208491").stages}
    assert stages[delivery_status.OPEN].on == "2025-09-30"
    assert stages[delivery_status.OPEN].conflict == "", "ordered before delivery — nothing to flag"


def test_source_date_is_not_used_as_the_order_date():
    """`SourceDate` was the obvious candidate and was measured and rejected against the live host:
    11 of the 17 mirrored POs with line due dates have a line due *before* it, and sorting the 28
    POs by number inverts it 11 times where `DocDate` inverts 0. A mirror row carrying only
    `source_date` must leave the node undated rather than fall back to it."""
    c = new_conn()
    _mirror_po(c, "208491", order_date=None, source_date="2025-10-08T00:00:00")
    email_log.record(
        conn=c, email_id="msg-1", subject="Inbound", sender="a@b.com", category="surface",
        matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="warehouse_inbound", po_hints="208491", email_date="2025-10-02T09:00:00Z",
    )
    stages = {s.key: s for s in read_views.po_timeline(c, "208491").stages}
    assert stages[delivery_status.OPEN].on == ""
    assert "2025-10-08" not in [s.on for s in stages.values()]


def test_a_delivery_before_the_order_date_is_reported_not_repaired():
    """Either Spitfire's date is wrong or the PO really was raised after the goods moved — Premier
    re-raises POs. Both need a person. Shuffling the dates until the bar looked plausible would
    destroy the only evidence that either happened."""
    c = new_conn()
    _mirror_po(c, "208491", order_date="2025-10-08T00:00:00")
    email_log.record(
        conn=c, email_id="msg-1", subject="Inbound", sender="a@b.com", category="surface",
        matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="warehouse_inbound", po_hints="208491",
        email_date="2026-06-06T09:00:00Z", origin_sent_at="2025-10-01",
    )
    stages = {s.key: s for s in read_views.po_timeline(c, "208491").stages}
    delivered = stages[delivery_status.DELIVERED]
    assert delivered.on == "2025-10-01", "the evidence date is kept, not moved"
    assert "2025-10-08" in delivered.conflict, "and the order date it clashes with is named"
    assert stages[delivery_status.OPEN].conflict == "", "the order date itself is not the problem"


@pytest.mark.parametrize("notification_type", [t.value for t in NotificationType])
def test_every_notification_type_has_a_label_the_mail_would_recognise(notification_type):
    """The evidence table shows what the mail calls each notice. A type without a label would
    render as our internal slug, which is neither our words nor theirs."""
    assert notification_type in read_views.NOTIFICATION_LABELS


def test_a_delivered_notification_is_not_a_delivery():
    """Authority's "Delivered Notification" means the carrier reached the warehouse; the goods are
    not received until the Inbound notice books them in. The label names the notice, never the
    status — showing it as a status would read "Delivered" against goods still travelling."""
    assert read_views.notification_label("delivered_shipped") == "Delivered Notification"
    assert read_views._NOTIFICATION_STATUS["delivered_shipped"] == (delivery_status.IN_TRANSIT,)


def test_a_forwarded_notice_is_dated_when_it_was_sent_not_when_it_was_forwarded():
    """The bug this was written for. Twelve of the fourteen corpus files are forwards Premier sent
    on 2026-06-06, and dating stages from `email_date` put that one day on every node of every
    purchase order — a bar that looked static because it was."""
    c = new_conn()
    email_log.record(
        conn=c, email_id="msg-1", subject="Fw: Inbound", sender="mariagutierrez@premierpm.com",
        category="surface", matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="warehouse_inbound", po_hints="208491",
        email_date="2026-06-06T02:12:21Z",          # the day it was forwarded
        origin_sent_at="2025-10-01",                # the day Authority actually sent it
    )
    timeline = read_views.po_timeline(c, "208491")
    stages = {s.key: s for s in timeline.stages}
    assert stages[delivery_status.AT_WAREHOUSE].on == "2025-10-01"
    assert "2026-06-06" not in [s.on for s in timeline.stages]
    # `when` stays the envelope date — ordering and dedupe rely on it being always present.
    assert timeline.events[0].when.startswith("2026-06-06")
    assert timeline.events[0].event_on == "2025-10-01"


def test_direct_mail_still_uses_its_envelope_date():
    """Two corpus files arrived unforwarded, so there is no quoted header to recover and the
    envelope date is already the truth. Recovering nothing must not blank the node."""
    c = new_conn()
    email_log.record(
        conn=c, email_id="msg-1", subject="Inbound", sender="warehousing@authoritylogistics.com",
        category="surface", matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="warehouse_inbound", po_hints="208491",
        email_date="2025-10-09T23:17:49Z", origin_sent_at=None,
    )
    stages = {s.key: s for s in read_views.po_timeline(c, "208491").stages}
    assert stages[delivery_status.AT_WAREHOUSE].on == "2025-10-09"


def test_a_stated_delivery_date_outranks_the_date_the_notice_was_sent():
    """`Received Date: 09/24/2025` in the body is the arrival itself. The send date of the mail
    carrying it is only when someone wrote about it, and the two differ by a week on PO 206725."""
    c = new_conn()
    email_log.record(
        conn=c, email_id="msg-1", subject="Fw: Inbound", sender="a@b.com", category="surface",
        matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="warehouse_inbound", po_hints="206725",
        email_date="2026-06-06T02:07:31Z", origin_sent_at="2025-10-01",
    )
    extracted_records_store.write_pending(c, make_record(po_number="206725",
                                                         pod_stated_date="2025-09-24"), NOW)
    stages = {s.key: s for s in read_views.po_timeline(c, "206725").stages}
    assert stages[delivery_status.DELIVERED].on == "2025-09-24"
    assert stages[delivery_status.DELIVERED].stated is True
    # In transit is not a stage the body states a date for, so it keeps the notice's send date.
    assert stages[delivery_status.AT_WAREHOUSE].stated is True
    assert stages[delivery_status.OPEN].stated is False


def test_stages_do_not_all_share_one_date():
    """The visible symptom being fixed. A PO with a shipping notice and a later warehouse receipt
    must show two different dates, not one repeated across the bar."""
    c = new_conn()
    email_log.record(
        conn=c, email_id="msg-ship", subject="Fw: Delivered Notification", sender="a@b.com",
        category="surface", matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="delivered_shipped", po_hints="206725",
        email_date="2026-06-06T02:06:44Z", origin_sent_at="2025-09-15",
    )
    email_log.record(
        conn=c, email_id="msg-wh", subject="Fw: Inbound", sender="a@b.com", category="surface",
        matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="warehouse_inbound", po_hints="206725",
        email_date="2026-06-06T02:07:31Z", origin_sent_at="2025-10-01",
    )
    stages = {s.key: s for s in read_views.po_timeline(c, "206725").stages}
    assert stages[delivery_status.IN_TRANSIT].on == "2025-09-15"
    assert stages[delivery_status.AT_WAREHOUSE].on == "2025-10-01"
    assert len({s.on for s in read_views.po_timeline(c, "206725").stages if s.on}) > 1


def test_an_undelivered_purchase_order_is_left_unmarked():
    """A vendor confirmation proves the goods shipped and nothing more. The delivered node must be
    unreached and undated — a delivery that has not happened must never look like one that has."""
    c = new_conn()
    email_log.record(
        conn=c, email_id="msg-1", subject="Fw: Vendor confirmation", sender="a@b.com",
        category="surface", matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="vendor_confirmation", po_hints="210634",
        email_date="2026-06-06T02:34:46Z", origin_sent_at="2025-12-01",
    )
    timeline = read_views.po_timeline(c, "210634")
    stages = {s.key: s for s in timeline.stages}
    assert stages[delivery_status.IN_TRANSIT].reached is True
    assert stages[delivery_status.DELIVERED].reached is False
    assert stages[delivery_status.DELIVERED].on == "", "an unreached node must carry no date"
    assert timeline.status != delivery_status.DELIVERED


def test_a_stage_carries_a_date_only_where_mail_proves_that_stage():
    """Reaching the warehouse implies the goods were in transit, so that node is filled — but with
    no carrier notice it carries no date. Inventing one is the same failure as inventing a qty."""
    c = new_conn()
    email_log.record(
        conn=c, email_id="msg-1", subject="Inbound", sender="a@b.com", category="surface",
        matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="warehouse_inbound", po_hints="208491", email_date="2025-10-02T09:00:00Z",
    )
    stages = {s.key: s for s in read_views.po_timeline(c, "208491").stages}
    assert stages[delivery_status.IN_TRANSIT].reached is True
    assert stages[delivery_status.IN_TRANSIT].on == "", "no carrier notice arrived, so no date"
    assert stages[delivery_status.AT_WAREHOUSE].on == "2025-10-02"


def test_the_bar_stops_at_delivered():
    """Delivered is the last node. `pod_submitted` and `pushed_to_spitfire` can never be reached —
    no receipt is staged in this store and nothing writes to Spitfire — so two dead nodes rendered
    on every purchase order, permanently unlit and undated. They are still named in the vocabulary
    and in `/ui/po`'s caveat; they are simply not positions on a timeline."""
    c = new_conn()
    accumulate(c, "208491", "warehouse_inbound")
    email_log.record(
        conn=c, email_id="msg-1", subject="Inbound", sender="a@b.com", category="surface",
        matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="warehouse_inbound", po_hints="208491", email_date="2025-10-02T09:00:00Z",
    )
    keys = [s.key for s in read_views.po_timeline(c, "208491").stages]
    assert keys[-1] == delivery_status.DELIVERED
    assert delivery_status.POD_SUBMITTED not in keys
    assert delivery_status.PUSHED_TO_SPITFIRE not in keys


def test_the_full_lifecycle_vocabulary_is_left_alone():
    """`VISIBLE_LIFECYCLE` is a display decision. `delivery_status.LIFECYCLE` is indexed by
    `rank()`, scanned by `rollup()` and imported by `api/services/po_status.py` — shortening it to
    shorten the bar would change the rollup arithmetic and the demo API as a side effect."""
    assert delivery_status.POD_SUBMITTED in delivery_status.LIFECYCLE
    assert delivery_status.PUSHED_TO_SPITFIRE in delivery_status.LIFECYCLE
    assert read_views.VISIBLE_LIFECYCLE == (
        delivery_status.OPEN, delivery_status.IN_TRANSIT,
        delivery_status.AT_WAREHOUSE, delivery_status.DELIVERED,
    )


def test_timeline_is_none_for_a_po_nobody_mentioned():
    assert read_views.po_timeline(new_conn(), "000000") is None


def test_pod_submitted_and_spitfire_are_declared_unreachable():
    """Not "none currently have these" — the pipeline store stages no receipts and nothing writes
    to Spitfire, so no input can produce them."""
    assert read_views.UNREACHABLE_STATUSES == (
        delivery_status.POD_SUBMITTED, delivery_status.PUSHED_TO_SPITFIRE,
    )


# --- Delivery mail that produced nothing --------------------------------------

def test_delivery_mail_that_extracted_nothing_is_queued():
    """Triage called it a delivery, then no record and no accumulation row came of it. Such an
    email appeared on no page at all — one is sitting in Premier's live store right now."""
    c = new_conn()
    seed_email(c, email_id="msg-silent", category="hold", folder="Processed",
               reason="delivery thread whose data is in an attachment")

    item = [i for i in read_views.manual_queue(c) if i.email_id == "msg-silent"][0]
    assert item.kind == "email"
    assert "nothing was extracted from it" in item.reason


def test_delivery_mail_still_awaiting_release_is_not_queued():
    """A held email with an accumulation row has records legitimately pending; it is not a gap."""
    c = new_conn()
    seed_email(c, email_id="msg-held", category="hold", folder="Processed")
    c.execute("""INSERT INTO accumulation (po_number, shipment_number, email_id, notification_type,
                                           category, received_at, payload_json)
                 VALUES ('208491', NULL, 'msg-held', 'delivered', 'hold', ?, '{}')""", (NOW,))
    c.commit()

    assert not [i for i in read_views.manual_queue(c) if i.email_id == "msg-held"]


def test_routed_mail_is_not_double_listed_as_silent_delivery_mail():
    c = new_conn()
    seed_email(c, email_id="msg-routed", category="route", folder="Routed", reason="no PO anywhere")

    assert len([i for i in read_views.manual_queue(c) if i.email_id == "msg-routed"]) == 1


# --- Attachments that read cleanly and said nothing ---------------------------

def _ledger_row(conn, email_id, filename, disposition, claimed_by="TextAdapter", records=0):
    conn.execute("""
        INSERT INTO attachment_ledger (email_id, parent_id, depth, ordinal, container_path,
            filename, sniffed_kind, sha256, size_bytes, is_inline, claimed_by, records_extracted,
            disposition, disposition_detail, review_status, first_seen_at, last_updated_at)
        VALUES (?, NULL, 0, 0, ?, ?, 'text', 'abc', 14206, 0, ?, ?, ?,
                'read cleanly, nothing extractable', 'none', ?, ?)
    """, (email_id, filename, filename, claimed_by, records, disposition, NOW, NOW))
    conn.commit()


def test_an_attachment_that_read_cleanly_and_said_nothing_is_queued_on_delivery_mail():
    """The hole this closes. The embedded `Delivered Notification` carrying the POD date, the
    signature, the carrier and the real delivered quantity was read as plain text, produced
    nothing, and was filed `empty` — a disposition `NEEDS_ATTENTION` deliberately ignores."""
    c = new_conn()
    seed_email(c, email_id="msg-1", category="hold")
    _ledger_row(c, "msg-1", "Delivered Notification.msg", attachment_ledger.EMPTY)

    item = [i for i in read_views.manual_queue(c) if i.kind == "attachment"][0]
    assert item.ref == "Delivered Notification.msg"
    assert "produced no records" in item.reason
    assert "TextAdapter" in item.detail


def test_an_empty_attachment_on_a_broadcast_stays_quiet():
    """An empty signature image on an all-associates email is not review work."""
    c = new_conn()
    seed_email(c, email_id="msg-noise", category="route", folder="Routed", reason="no PO")
    _ledger_row(c, "msg-noise", "logo.png", attachment_ledger.EMPTY, claimed_by="OcrAdapter")

    assert not [i for i in read_views.manual_queue(c) if i.kind == "attachment"]


def test_an_attachment_that_did_produce_records_stays_quiet():
    c = new_conn()
    seed_email(c, email_id="msg-1", category="hold")
    _ledger_row(c, "msg-1", "POD.pdf", attachment_ledger.EMPTY, claimed_by="PdfAdapter", records=1)

    assert not [i for i in read_views.manual_queue(c) if i.kind == "attachment"]


# --- The PO a queue item is about ---------------------------------------------

def test_a_record_carries_its_po_as_a_field():
    """It used to exist only inside the `detail` prose. The page renders it as the control that
    opens the mail the record was read from, and parsing a PO back out of a sentence to do that
    would be absurd."""
    c = new_conn()
    seed_email(c)
    extracted_records_store.write_pending(c, make_record(po_number="210634"), NOW)

    item = [i for i in read_views.manual_queue(c) if i.kind == "record"][0]
    assert item.po_number == "210634"
    assert "PO 210634" not in item.detail, "the PO moved out of detail into its own column"


def test_a_record_with_no_po_carries_no_po():
    """Legitimate: a missing PO is itself one of the gaps that puts a record on this queue."""
    c = new_conn()
    seed_email(c)
    extracted_records_store.write_pending(c, make_record(po_number=""), NOW)

    assert [i for i in read_views.manual_queue(c) if i.kind == "record"][0].po_number == ""


def test_an_email_row_claims_no_single_po():
    """`email_log.po_hints` routinely names ten; picking one would be a guess, and the row already
    opens its own message."""
    c = new_conn()
    seed_email(c, email_id="msg-routed", category="route", folder="Routed", reason="no PO anywhere")

    assert [i for i in read_views.manual_queue(c) if i.kind == "email"][0].po_number == ""


def test_the_record_detail_still_says_what_was_read():
    c = new_conn()
    seed_email(c)
    extracted_records_store.write_pending(c, make_record(), NOW)

    detail = [i for i in read_views.manual_queue(c) if i.kind == "record"][0].detail
    assert "STE-402-LT" in detail and "Side table" in detail


def test_records_ready_puts_the_newest_record_first():
    """Ordering by purchase order read tidily but buried the row a person came to look at: a
    record extracted a minute ago landed wherever its PO number happened to sort, halfway down
    thirty-odd rows."""
    c = new_conn()
    seed_email(c)
    ids = [extracted_records_store.write_pending(c, make_record(po_number=po), NOW)
           for po in ("212614", "206481", "209395")]
    assert [r["id"] for r in read_views.records_ready(c)] == sorted(ids, reverse=True)


def test_attachments_put_the_newest_first_by_when_we_saw_them():
    """Keyed on `first_seen_at`, not the email's own date. `email_date` is the envelope date, and
    for forwarded mail that is the forwarding date — twelve of the fourteen corpus messages were
    forwarded on one afternoon, which collapsed months of deliveries onto a single instant."""
    c = new_conn()
    # The older attachment deliberately carries the *later* email_date — the shape a forward
    # produces, and the one that used to sort it above an attachment we received weeks after it.
    for email_id, email_date, seen in (
            ("msg-a-old-arrival", "2026-08-20T14:00:00Z", "2026-08-01T09:00:00Z"),
            ("msg-z-new-arrival", "2026-06-06T14:00:00Z", "2026-08-17T09:00:00Z")):
        seed_email(c, email_id=email_id)
        c.execute("UPDATE email_log SET email_date = ? WHERE email_id = ?", (email_date, email_id))
        c.execute("""
            INSERT INTO attachment_ledger (email_id, parent_id, depth, ordinal, container_path,
                filename, sniffed_kind, sha256, size_bytes, is_inline, claimed_by,
                records_extracted, disposition, review_status, first_seen_at, last_updated_at)
            VALUES (?, NULL, 0, 0, ?, ?, 'pdf', ?, 10, 0, 'PdfAdapter', 1, ?, 'none', ?, ?)
        """, (email_id, f"{email_id}.pdf", f"{email_id}.pdf", email_id,
              attachment_ledger.EXTRACTED, seen, seen))
    c.commit()
    assert [r["email_id"] for r in read_views.attachments(c)][0] == "msg-z-new-arrival"
