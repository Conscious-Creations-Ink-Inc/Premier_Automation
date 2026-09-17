"""The record destinations partition the work, and every queued item must say why.

Every pending record belongs to **exactly one** of three. On `records_ready` means it can be
posted; a record awaiting confirmation was read out of a message Premier wrote itself, so nothing
outside Premier has said the goods arrived; anything else is on `manual_queue`, where the controls
that fix it live.

Both halves are asserted, because both have failed in production. A record on *neither* page is
work nobody was ever told about — twenty-one of them were sitting in Premier's live store, Excel
tracker lines whose sources disagreed on quantity, kept off Records by `_READY_CLAUSE` and skipped
by the queue for having no missing fields. A record on *both* is the opposite failure: for a while
this file asserted that overlap was correct, until `post_decision` was pointed at the eleven
records it applied to and refused every one. A Post button on a page that means "ready" has to
work.
"""
import pytest

from pipeline import (attachment_ledger, delivery_status, email_log, extracted_records_store,
                      mail_overrides, read_views, state_db)
from pipeline.models import ExtractedRecord, NotificationType

NOW = "2026-08-03T12:00:00Z"


def new_conn():
    return state_db.get_connection(":memory:")


def make_record(**overrides) -> ExtractedRecord:
    defaults = dict(
        source_email_id="msg-1", po_number="908491", shipment_number=None,
        spec_code="STE-402-LT", parent_spec_code="STE-402-LT", sub_spec_suffix=None,
        item_description="Side table", vendor_name=None, carrier_name=None, tracking_number=None,
        quantity_received=11.0, unit_of_measure="EA", pod_stated_date=None,
        email_date="2026-06-08T14:00:00Z", delivery_location=None, comments=None,
        extraction_source="html_table", extraction_confidence=0.9, raw_snippet="…",
    )
    defaults.update(overrides)
    return ExtractedRecord(**defaults)


def seed_email(conn, email_id="msg-1", category="surface", folder="Processed", reason="rule matched",
               origin_sender="atlas.notifications@example-logistics.test", sender="a@b.com"):
    """A message in the log. External by default, because most delivery mail is.

    `origin_sender` is a parameter because it decides a destination now: a message Premier wrote
    itself cannot be evidence its own goods arrived. It defaults to a warehouse address rather than
    to blank so every test that does not care about authorship keeps testing what it meant to.
    """
    email_log.record(
        conn, email_id=email_id, subject=f"Subject for {email_id}", sender=sender,
        origin_sender=origin_sender,
        category=category, matched_rule="rule_1a", reason=reason, folder=folder,
        processed_at=NOW,
    )


INTERNAL = "expeditor@example-pm.test"
"""An address inside `settings.INTERNAL_DOMAINS` — Premier writing to Premier."""

FORWARDED_BY_PREMIER = dict(sender="expeditor@example-pm.test",
                            origin_sender="atlas.notifications@example-logistics.test")
"""The trap this whole rule has to survive: a warehouse receiving report **forwarded** by staff.

Every message in the real mailbox arrives this way, so the envelope sender says Premier on eleven
of the ninety-four postable records in the live store. Reading `sender` instead of `origin_sender`
would withhold exactly the documents the system exists to post, and would look like the rule
working.
"""


def make_complete_record(**overrides) -> ExtractedRecord:
    """A record carrying every field `completeness.REQUIRED` asks for."""
    defaults = dict(
        po_line_number=1, vendor_name="P. Kaufmann", pod_stated_date="2025-09-10",
        received_by="U ALI", carrier_name="GlobalTranz", tracking_number="91457971",
    )
    defaults.update(overrides)
    return make_record(**defaults)


def _pages(c):
    ready = {r["id"] for r in read_views.records_ready(c)}
    manual = {i.ref_id for i in read_views.manual_queue(c) if i.kind == "record"}
    all_pending = {row[0] for row in c.execute(
        "SELECT id FROM extracted_records WHERE status = 'pending'")}
    return ready, manual, all_pending


def _destinations(c):
    """The three places a pending record can be, as sets of ids.

    A third one arrived on 2026-09-07: a record read out of a message Premier wrote itself is
    neither postable nor fixable until somebody confirms the goods arrived, and it belongs on
    neither of the first two pages while that is true.
    """
    ready, manual, all_pending = _pages(c)
    awaiting = {r["id"] for r in read_views.records_awaiting_confirmation(c)}
    return ready, manual, awaiting, all_pending


def _seed_one_of_each(c):
    """A record for every way a record can be stuck, plus one that is not."""
    seed_email(c)
    extracted_records_store.write_pending(c, make_complete_record(), NOW)          # postable
    extracted_records_store.write_pending(c, make_record(po_number=""), NOW)       # no PO
    extracted_records_store.write_pending(c, make_record(extraction_confidence=0.0), NOW)
    extracted_records_store.write_pending(
        c, make_record(extraction_source="html_table+quantity_conflict"), NOW)
    # Complete, and still not postable: every field present, but two sources disagreed on the
    # quantity. Not a *gap*, so the queue used to skip it while `_READY_CLAUSE` kept it off
    # Records — the exact shape of the twenty-one invisible records.
    extracted_records_store.write_pending(
        c, make_complete_record(extraction_source="excel:Hoja1+quantity_conflict"), NOW)
    # Complete, unconflicted, and still not postable: Premier wrote the message it came from, so
    # nothing outside Premier has said these goods arrived. The expediting-report population.
    seed_email(c, email_id="msg-internal", origin_sender=INTERNAL)
    extracted_records_store.write_pending(
        c, make_complete_record(source_email_id="msg-internal",
                                extraction_source="excel:Expediting"), NOW)


def test_no_pending_record_is_missing_from_every_destination():
    """A record on no destination is work nobody was ever told about."""
    c = new_conn()
    _seed_one_of_each(c)
    ready, manual, awaiting, all_pending = _destinations(c)
    assert ready | manual | awaiting == all_pending


def test_no_pending_record_appears_on_both_pages():
    """The other half, and the one that was false in production.

    Records means postable. A record listed there that `post_decision` would refuse is a Post
    button that does not post, and it was true of eleven of the forty-one rows on that page.
    """
    c = new_conn()
    _seed_one_of_each(c)
    ready, manual, awaiting, _ = _destinations(c)
    assert ready & manual == set()
    assert ready & awaiting == set()
    assert manual & awaiting == set()


def test_a_record_from_premiers_own_mail_is_not_offered_as_ready():
    """The one this rule exists for.

    An expediting report states a purchase order, a spec, a quantity and a date, so it passes every
    completeness test there is — and asserts nothing about goods arriving, because Premier wrote it
    to ask. It was reaching the Records page as postable work: 1,067 rows across 14 messages in the
    live store, against 151 rows of real delivery evidence.
    """
    c = new_conn()
    seed_email(c, email_id="ours", origin_sender=INTERNAL)
    rid = extracted_records_store.write_pending(
        c, make_complete_record(source_email_id="ours", extraction_source="excel:Expediting"), NOW)

    assert rid not in {r["id"] for r in read_views.records_ready(c)}
    assert rid in {r["id"] for r in read_views.records_awaiting_confirmation(c)}


def test_a_forwarded_warehouse_report_stays_on_records():
    """The trap. Every message in the mailbox is forwarded by Premier, so the **envelope** sender
    says Premier on the warehouse's own receiving reports too.

    Reading `sender` instead of `origin_sender` withholds the documents this system exists to post
    — eleven of the ninety-four postable records in the live store — and it fails silently, because
    a rule that hides real work looks exactly like a rule that is working.
    """
    c = new_conn()
    seed_email(c, email_id="fwd", **FORWARDED_BY_PREMIER)
    rid = extracted_records_store.write_pending(
        c, make_complete_record(source_email_id="fwd", extraction_source="pdf"), NOW)

    assert rid in {r["id"] for r in read_views.records_ready(c)}
    assert rid not in {r["id"] for r in read_views.records_awaiting_confirmation(c)}


def test_confirming_the_message_sends_every_record_on_it_to_records():
    """Confirmation is given per message, because that is what a person is looking at.

    They are reading one spreadsheet and deciding whether its delivery happened — not deciding row
    604 of it. So one verdict releases the whole message.
    """
    c = new_conn()
    seed_email(c, email_id="ours", origin_sender=INTERNAL)
    ids = [extracted_records_store.write_pending(
        c, make_complete_record(source_email_id="ours", spec_code=spec,
                                extraction_source="excel:Expediting"), NOW)
        for spec in ("STE-402-LT", "GR-350a-WTF", "LOB-400-LT")]

    assert not set(ids) & {r["id"] for r in read_views.records_ready(c)}

    mail_overrides.set_verdict(c, email_id="ours", verdict=mail_overrides.CONFIRMED,
                               decided_by="A Reviewer", at=NOW,
                               note="the property confirmed these were received")

    assert set(ids) <= {r["id"] for r in read_views.records_ready(c)}
    assert read_views.records_awaiting_confirmation(c) == []


def test_withdrawing_a_confirmation_puts_the_records_back():
    """A reversal is a fresh decision, and it has to work in both directions or the control is a
    one-way door on a judgement people get wrong."""
    c = new_conn()
    seed_email(c, email_id="ours", origin_sender=INTERNAL)
    rid = extracted_records_store.write_pending(
        c, make_complete_record(source_email_id="ours", extraction_source="excel:Expediting"), NOW)
    mail_overrides.set_verdict(c, email_id="ours", verdict=mail_overrides.CONFIRMED,
                               decided_by="A Reviewer", at=NOW)
    assert rid in {r["id"] for r in read_views.records_ready(c)}

    # `DELIVERY`, not `NOT_DELIVERY`. Both withdraw the confirmation, because the table holds one
    # verdict per message — but they answer different questions, and only this one leaves the
    # records in play. `NOT_DELIVERY` retires them; that is asserted separately below.
    mail_overrides.set_verdict(c, email_id="ours", verdict=mail_overrides.DELIVERY,
                               decided_by="A Second Reviewer", at=NOW, note="the POD is attached")

    assert rid not in {r["id"] for r in read_views.records_ready(c)}
    assert rid in {r["id"] for r in read_views.records_awaiting_confirmation(c)}


def test_an_incomplete_record_from_our_own_mail_is_not_offered_as_a_gap_to_fix():
    """`_READY_CLAUSE` is deliberately not applied to the awaiting set.

    A half-read row of an expediting sheet is no more a delivery than a fully-read one, and putting
    it on the queue as "missing: spec code" asks somebody to finish a receipt for goods nobody says
    arrived. It waits with its siblings instead.
    """
    c = new_conn()
    seed_email(c, email_id="ours", origin_sender=INTERNAL)
    rid = extracted_records_store.write_pending(
        c, make_record(source_email_id="ours", spec_code=None,
                       extraction_source="excel:qPOExpeditor"), NOW)

    assert rid not in {i.ref_id for i in read_views.manual_queue(c) if i.kind == "record"}
    assert rid in {r["id"] for r in read_views.records_awaiting_confirmation(c)}


def test_a_failed_record_stays_on_the_queue_whoever_wrote_the_message():
    """A record that failed is a thing that went wrong in our software. That is real work, and it
    does not become somebody else's decision because Premier happened to write the mail."""
    c = new_conn()
    seed_email(c, email_id="ours", origin_sender=INTERNAL)
    rid = extracted_records_store.write_pending(
        c, make_complete_record(source_email_id="ours", extraction_source="excel:Expediting"), NOW)
    extracted_records_store.mark_failed(c, rid, "Spitfire refused the line", NOW)

    assert rid in {i.ref_id for i in read_views.manual_queue(c) if i.kind == "record"}
    assert rid not in {r["id"] for r in read_views.records_awaiting_confirmation(c)}


def test_the_queue_shows_one_row_per_message_not_one_per_record():
    """Fourteen messages produced 1,067 of these in the live store. A queue that reads
    "record #4727, record #4728, …" six hundred times is a queue nobody can work."""
    c = new_conn()
    seed_email(c, email_id="ours", origin_sender=INTERNAL)
    for spec in ("STE-402-LT", "GR-350a-WTF", "LOB-400-LT", "POOL-152-WT"):
        extracted_records_store.write_pending(
            c, make_complete_record(source_email_id="ours", spec_code=spec,
                                    extraction_source="excel:Expediting"), NOW)

    waiting = [i for i in read_views.manual_queue(c) if i.code == "awaiting_confirmation"]
    assert len(waiting) == 1
    assert waiting[0].rolled_up == 4
    assert waiting[0].ref_id is None          # the decision is about the message, not a record
    assert "nothing in it says the goods arrived" in waiting[0].reason


def test_the_summary_counts_waiting_records_not_waiting_rows():
    """The header answers "where did the other records go", and the queue cannot: it shows one row
    per message, and those rows stand for thousands of records."""
    c = new_conn()
    seed_email(c, email_id="ours", origin_sender=INTERNAL)
    for spec in ("STE-402-LT", "GR-350a-WTF", "LOB-400-LT"):
        extracted_records_store.write_pending(
            c, make_complete_record(source_email_id="ours", spec_code=spec,
                                    extraction_source="excel:Expediting"), NOW)

    s = read_views.summary(c)
    assert s.records_awaiting_confirmation == 3
    assert len([i for i in read_views.manual_queue(c) if i.code == "awaiting_confirmation"]) == 1


def test_a_complete_record_with_a_quantity_conflict_is_queued_not_ready():
    """Completeness is not the only way to be stuck.

    Every field is present, so `completeness.gaps` finds nothing — but two sources stated different
    quantities and neither was chosen, so it cannot be posted and a person has to settle it.
    """
    c = new_conn()
    seed_email(c)
    conflicted = extracted_records_store.write_pending(
        c, make_complete_record(extraction_source="excel:Hoja1+quantity_conflict"), NOW)

    assert conflicted not in {r["id"] for r in read_views.records_ready(c)}
    item = [i for i in read_views.manual_queue(c) if i.ref_id == conflicted][0]
    assert "quantity conflict" in item.reason


def test_a_complete_record_is_ready_and_not_in_the_queue():
    c = new_conn()
    seed_email(c)
    complete = extracted_records_store.write_pending(c, make_complete_record(), NOW)

    assert complete in {r["id"] for r in read_views.records_ready(c)}
    assert complete not in {i.ref_id for i in read_views.manual_queue(c) if i.kind == "record"}


def test_a_record_with_no_proof_of_delivery_is_queued_and_not_offered_as_ready():
    """The case that prompted the contract, and the one that later reversed it.

    A PO and a confidence above zero was all `_READY_CLAUSE` ever asked for, so three live records
    sat under "Ready to process further" with no delivery date, carrier, tracking or signature —
    nothing to compare against a purchase order.

    This used to assert the record was ready *and* queued at once. It is not: `post_decision`
    gate 1 refuses it for the missing delivery date, so offering it on the page that means
    "postable" was a promise the Post button then broke. It belongs on one page, and that page is
    the one carrying the controls to fix it.
    """
    c = new_conn()
    seed_email(c)
    no_pod = extracted_records_store.write_pending(
        c, make_complete_record(pod_stated_date=None, received_by=None), NOW)

    assert no_pod not in {r["id"] for r in read_views.records_ready(c)}
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
    # A complete record, so it is genuinely postable — `make_record` alone is not, and "ready" now
    # means the Post button will accept it rather than merely that a PO is present.
    extracted_records_store.write_pending(c, make_complete_record(), NOW)
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
    accumulate(c, "912614", "property_confirmation")

    [row] = read_views.po_delivery_status(c)
    assert row.po_number == "912614"
    assert row.records == 0
    assert row.status == delivery_status.DELIVERED


def test_the_most_advanced_notification_wins_for_one_po():
    """These notices track one shipment's progress, so the furthest one reached is the truth —
    the opposite of the cross-line rollup, which takes the least advanced."""
    c = new_conn()
    accumulate(c, "908491", "delivered_shipped", email_id="msg-1")
    accumulate(c, "908491", "warehouse_inbound", email_id="msg-2")

    [row] = read_views.po_delivery_status(c)
    assert row.status == delivery_status.DELIVERED
    assert row.notifications == {"delivered_shipped": 1, "warehouse_inbound": 1}


def test_a_warehouse_receipt_is_a_delivery():
    """The Authority notice reads "Received at <address> / Received By <name>". That is a receipt —
    it is the event that triggers a receiver in Spitfire, annotated by Premier as "WH rec'd" — not a
    waypoint notice. Treating it as an intermediate state left every record reading "At partnered
    warehouse" when the goods had in fact been delivered and signed for."""
    c = new_conn()
    accumulate(c, "908491", "warehouse_inbound")
    assert read_views.po_delivery_status(c)[0].status == delivery_status.DELIVERED


def test_a_warehouse_receipt_still_shows_the_warehouse_on_the_route():
    """It proves two stages, not one: the goods demonstrably passed through the partnered warehouse
    *and* that arrival was the delivery. Dropping the warehouse node would hide the route; treating
    it as the end state would misreport the status."""
    c = new_conn()
    accumulate(c, "908491", "warehouse_inbound")
    email_log.record(
        conn=c, email_id="msg-1", subject="Inbound", sender="a@b.com", category="surface",
        matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="warehouse_inbound", po_hints="908491", email_date="2025-10-09T09:00:00Z",
    )
    stages = {s.key: s for s in read_views.po_timeline(c, "908491").stages}
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
    accumulate(c, "908491", "warehouse_inbound")
    row = read_views.po_delivery_status(c)[0]
    assert row.qty_ordered is None
    assert row.qty_outstanding is None
    assert row.qty_received == 0.0     # this one IS a real zero — no records extracted yet


def test_the_receipt_location_and_signer_are_carried_through():
    """"Delivered" cannot tell a reader whether the receipt address was the final destination. The
    address can, so it travels with the status rather than being left in the extraction table."""
    c = new_conn()
    seed_email(c)
    accumulate(c, "908491", "warehouse_inbound")
    extracted_records_store.write_pending(c, make_record(
        delivery_location="Example Storage Moving & Storage - Riverside", received_by="Jordan T."), NOW)

    row = read_views.po_delivery_status(c)[0]
    assert row.received_by == "Jordan T."
    assert "Example Storage" in row.delivery_location
    assert read_views.po_timeline(c, "908491").received_by == "Jordan T."


def test_records_and_quantities_roll_up_onto_the_po():
    c = new_conn()
    seed_email(c)
    accumulate(c, "908491", "warehouse_inbound")
    extracted_records_store.write_pending(c, make_record(quantity_received=11.0, po_line_number=300), NOW)
    extracted_records_store.write_pending(c, make_record(quantity_received=12.0, po_line_number=301), NOW)

    row = read_views.po_delivery_status(c)[0]
    assert row.records == 2
    assert row.qty_received == 23.0
    assert row.lines_seen == 2, "sub-parts are separate Spitfire lines and must not collapse"


def test_a_released_delivery_carries_its_reason():
    c = new_conn()
    accumulate(c, "908491", "warehouse_inbound")
    c.execute(
        "INSERT INTO released_events (po_number, shipment_number, released_at, release_reason) "
        "VALUES ('908491', '90052', ?, 'true final event received')", (NOW,),
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
    """`po_hints` is a comma-space blob. A LIKE '%2084%' would match 908491 — and a purchase-order
    page showing another order's mail is worse than one showing none."""
    c = new_conn()
    route_email(c, "msg-x", "loss_or_claim", "908491, 911400")

    by_po = {r.po_number for r in read_views.po_delivery_status(c)}
    assert by_po == {"908491", "911400"}
    assert read_views.po_timeline(c, "2084") is None
    assert read_views.po_timeline(c, "908491") is not None


def test_route_only_pos_appear_at_all():
    """Cancellations and loss/claim notices are triaged ROUTE and never accumulate. Sourced from
    `accumulation` these POs were invisible; they are the exceptions most needing a person."""
    c = new_conn()
    route_email(c, "msg-loss", "loss_or_claim", "908453, 911400, 911067")

    rows = {r.po_number: r for r in read_views.po_delivery_status(c)}
    assert set(rows) == {"908453", "911400", "911067"}
    assert all(r.status == delivery_status.LOSS_OR_CLAIM for r in rows.values())


def test_a_cancellation_terminates_the_bar():
    """The corpus contains no cancellation, so this path exists only under test until one arrives."""
    c = new_conn()
    seed_email(c, email_id="msg-ship")
    accumulate(c, "908491", "delivered_shipped", email_id="msg-ship")
    email_log.record(
        conn=c, email_id="msg-ship", subject="shipped", sender="a@b.com", category="hold",
        matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="delivered_shipped", po_hints="908491", email_date="2026-06-01T09:00:00Z",
    )
    route_email(c, "msg-cancel", "order_cancellation", "908491", email_date="2026-06-05T09:00:00Z")

    timeline = read_views.po_timeline(c, "908491")
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
    route_email(c, "msg-loss", "loss_or_claim", "911067")
    timeline = read_views.po_timeline(c, "911067")
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
            notification_type="warehouse_inbound", po_hints="908491", email_date=f"{day}T09:00:00Z",
        )
    accumulate(c, "908491", "warehouse_inbound", email_id="msg-1")   # only one accumulated

    timeline = read_views.po_timeline(c, "908491")
    assert len(timeline.events) == 3
    assert [e.when[:10] for e in timeline.events] == ["2025-10-02", "2025-10-09", "2026-06-06"]


def _mirror_po(conn, po_number="908491", order_date=None, source_date="2025-10-08T00:00:00"):
    """A minimal `spitfire_po_index` row — the PO-number join the timeline reads its order date
    from."""
    conn.execute(
        "INSERT OR REPLACE INTO spitfire_po_index "
        "(po_number, doc_master_key, source_date, order_date, refreshed_at) VALUES (?,?,?,?,?)",
        (po_number, "key-1", source_date, order_date, NOW),
    )
    conn.commit()


def test_the_order_date_is_never_inferred_from_mail():
    """The reported bug: PO 908491 read `Ordered 2025-10-02`, a day *after* it was delivered,
    because the node took "the first email we happened to see". No mail states when a PO was
    raised — it is a property of the order, not of any delivery."""
    c = new_conn()
    email_log.record(
        conn=c, email_id="msg-1", subject="Inbound", sender="a@b.com", category="surface",
        matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="warehouse_inbound", po_hints="908491", email_date="2025-10-02T09:00:00Z",
    )
    stages = {s.key: s for s in read_views.po_timeline(c, "908491").stages}
    ordered = stages[delivery_status.OPEN]
    assert ordered.reached is True, "the PO exists — it is named in mail"
    assert ordered.on == "", "but nothing here says when it was raised"
    assert not any(s.on and s.on < "2025-10-02" for s in stages.values() if s.reached)


def test_the_order_date_comes_from_the_purchase_order():
    c = new_conn()
    _mirror_po(c, "908491", order_date="2025-09-30T00:00:00")
    email_log.record(
        conn=c, email_id="msg-1", subject="Inbound", sender="a@b.com", category="surface",
        matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="warehouse_inbound", po_hints="908491", email_date="2025-10-02T09:00:00Z",
    )
    stages = {s.key: s for s in read_views.po_timeline(c, "908491").stages}
    assert stages[delivery_status.OPEN].on == "2025-09-30"
    assert stages[delivery_status.OPEN].conflict == "", "ordered before delivery — nothing to flag"


def test_source_date_is_not_used_as_the_order_date():
    """`SourceDate` was the obvious candidate and was measured and rejected against the live host:
    11 of the 17 mirrored POs with line due dates have a line due *before* it, and sorting the 28
    POs by number inverts it 11 times where `DocDate` inverts 0. A mirror row carrying only
    `source_date` must leave the node undated rather than fall back to it."""
    c = new_conn()
    _mirror_po(c, "908491", order_date=None, source_date="2025-10-08T00:00:00")
    email_log.record(
        conn=c, email_id="msg-1", subject="Inbound", sender="a@b.com", category="surface",
        matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="warehouse_inbound", po_hints="908491", email_date="2025-10-02T09:00:00Z",
    )
    stages = {s.key: s for s in read_views.po_timeline(c, "908491").stages}
    assert stages[delivery_status.OPEN].on == ""
    assert "2025-10-08" not in [s.on for s in stages.values()]


def test_a_delivery_before_the_order_date_is_reported_not_repaired():
    """Either Spitfire's date is wrong or the PO really was raised after the goods moved — Premier
    re-raises POs. Both need a person. Shuffling the dates until the bar looked plausible would
    destroy the only evidence that either happened."""
    c = new_conn()
    _mirror_po(c, "908491", order_date="2025-10-08T00:00:00")
    email_log.record(
        conn=c, email_id="msg-1", subject="Inbound", sender="a@b.com", category="surface",
        matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="warehouse_inbound", po_hints="908491",
        email_date="2026-06-06T09:00:00Z", origin_sent_at="2025-10-01",
    )
    stages = {s.key: s for s in read_views.po_timeline(c, "908491").stages}
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
        conn=c, email_id="msg-1", subject="Fw: Inbound", sender="arivera@example-pm.test",
        category="surface", matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="warehouse_inbound", po_hints="908491",
        email_date="2026-06-06T02:12:21Z",          # the day it was forwarded
        origin_sent_at="2025-10-01",                # the day Authority actually sent it
    )
    timeline = read_views.po_timeline(c, "908491")
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
        conn=c, email_id="msg-1", subject="Inbound", sender="warehousing@example-logistics.test",
        category="surface", matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="warehouse_inbound", po_hints="908491",
        email_date="2025-10-09T23:17:49Z", origin_sent_at=None,
    )
    stages = {s.key: s for s in read_views.po_timeline(c, "908491").stages}
    assert stages[delivery_status.AT_WAREHOUSE].on == "2025-10-09"


def test_a_stated_delivery_date_outranks_the_date_the_notice_was_sent():
    """`Received Date: 09/24/2025` in the body is the arrival itself. The send date of the mail
    carrying it is only when someone wrote about it, and the two differ by a week on PO 906725."""
    c = new_conn()
    email_log.record(
        conn=c, email_id="msg-1", subject="Fw: Inbound", sender="a@b.com", category="surface",
        matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="warehouse_inbound", po_hints="906725",
        email_date="2026-06-06T02:07:31Z", origin_sent_at="2025-10-01",
    )
    extracted_records_store.write_pending(c, make_record(po_number="906725",
                                                         pod_stated_date="2025-09-24"), NOW)
    stages = {s.key: s for s in read_views.po_timeline(c, "906725").stages}
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
        notification_type="delivered_shipped", po_hints="906725",
        email_date="2026-06-06T02:06:44Z", origin_sent_at="2025-09-15",
    )
    email_log.record(
        conn=c, email_id="msg-wh", subject="Fw: Inbound", sender="a@b.com", category="surface",
        matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="warehouse_inbound", po_hints="906725",
        email_date="2026-06-06T02:07:31Z", origin_sent_at="2025-10-01",
    )
    stages = {s.key: s for s in read_views.po_timeline(c, "906725").stages}
    assert stages[delivery_status.IN_TRANSIT].on == "2025-09-15"
    assert stages[delivery_status.AT_WAREHOUSE].on == "2025-10-01"
    assert len({s.on for s in read_views.po_timeline(c, "906725").stages if s.on}) > 1


def test_an_undelivered_purchase_order_is_left_unmarked():
    """A vendor confirmation proves the goods shipped and nothing more. The delivered node must be
    unreached and undated — a delivery that has not happened must never look like one that has."""
    c = new_conn()
    email_log.record(
        conn=c, email_id="msg-1", subject="Fw: Vendor confirmation", sender="a@b.com",
        category="surface", matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="vendor_confirmation", po_hints="910634",
        email_date="2026-06-06T02:34:46Z", origin_sent_at="2025-12-01",
    )
    timeline = read_views.po_timeline(c, "910634")
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
        notification_type="warehouse_inbound", po_hints="908491", email_date="2025-10-02T09:00:00Z",
    )
    stages = {s.key: s for s in read_views.po_timeline(c, "908491").stages}
    assert stages[delivery_status.IN_TRANSIT].reached is True
    assert stages[delivery_status.IN_TRANSIT].on == "", "no carrier notice arrived, so no date"
    assert stages[delivery_status.AT_WAREHOUSE].on == "2025-10-02"


def test_the_bar_stops_at_delivered():
    """Delivered is the last node. `pod_submitted` and `pushed_to_spitfire` can never be reached —
    no receipt is staged in this store and nothing writes to Spitfire — so two dead nodes rendered
    on every purchase order, permanently unlit and undated. They are still named in the vocabulary
    and in `/ui/po`'s caveat; they are simply not positions on a timeline."""
    c = new_conn()
    accumulate(c, "908491", "warehouse_inbound")
    email_log.record(
        conn=c, email_id="msg-1", subject="Inbound", sender="a@b.com", category="surface",
        matched_rule="r", reason="", folder="Processed", processed_at=NOW,
        notification_type="warehouse_inbound", po_hints="908491", email_date="2025-10-02T09:00:00Z",
    )
    keys = [s.key for s in read_views.po_timeline(c, "908491").stages]
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
                 VALUES ('908491', NULL, 'msg-held', 'delivered', 'hold', ?, '{}')""", (NOW,))
    c.commit()

    assert not [i for i in read_views.manual_queue(c) if i.email_id == "msg-held"]


def test_routed_mail_is_not_double_listed_as_silent_delivery_mail():
    c = new_conn()
    seed_email(c, email_id="msg-routed", category="route", folder="Routed", reason="no PO anywhere")

    assert len([i for i in read_views.manual_queue(c) if i.email_id == "msg-routed"]) == 1


# --- Attachments that read cleanly and said nothing ---------------------------

def _only(items):
    """The single queue row for a message, asserting that it *is* single.

    These tests seed one message and one attachment, which used to be two queue rows. They are one
    now — see `read_views._merge_no_record_emails` — and the classification each test is really
    about is carried on that row. Asserting the count here rather than indexing past it keeps the
    merge itself under test in four more places.
    """
    assert len(items) == 1, f"expected one merged row, got {[(i.kind, i.code) for i in items]}"
    return items[0]


def _ledger_row(conn, email_id, filename, disposition, claimed_by="TextAdapter", records=0,
                kind="text", ordinal=0):
    conn.execute("""
        INSERT INTO attachment_ledger (email_id, parent_id, depth, ordinal, container_path,
            filename, sniffed_kind, sha256, size_bytes, is_inline, claimed_by, records_extracted,
            disposition, disposition_detail, review_status, first_seen_at, last_updated_at)
        VALUES (?, NULL, 0, ?, ?, ?, ?, ?, 14206, 0, ?, ?, ?,
                'read cleanly, nothing extractable', 'none', ?, ?)
    """, (email_id, ordinal, filename, filename, kind, f"sha-{ordinal}", claimed_by, records,
          disposition, NOW, NOW))
    conn.commit()


def test_an_attachment_that_read_cleanly_and_said_nothing_is_queued_on_delivery_mail():
    """The hole this closes. The embedded `Delivered Notification` carrying the POD date, the
    signature, the carrier and the real delivered quantity was read as plain text, produced
    nothing, and was filed `empty` — a disposition `NEEDS_ATTENTION` deliberately ignores."""
    c = new_conn()
    seed_email(c, email_id="msg-1", category="hold")
    _ledger_row(c, "msg-1", "Delivered Notification.msg", attachment_ledger.EMPTY)

    # One row, not two: the message and its unreadable attachment are one piece of work. The file
    # is still named, and the reason is still the attachment's — see `_merge_no_record_emails`.
    item = _only(read_views.manual_queue(c))
    assert item.item == "Delivered Notification.msg"
    assert item.reason, "it must say why it is here"
    assert "TextAdapter" in item.detail


def test_a_text_attachment_that_said_nothing_is_not_labelled_needs_ocr():
    """"Needs OCR" names a remedy, and naming the wrong one costs money.

    Every row in this bucket used to be coded `needs_ocr` on the inference that bytes which read
    cleanly and yielded nothing must be a scan. On the live store that was wrong for all fourteen:
    nine text-layer PDFs (invoices, a vendor line card, terms and conditions), two MIME body parts,
    two HTML parts, and one image OcrAdapter had already read. Their text was extracted in full —
    what failed was finding a delivery in it, usually because there was none. `OcrAdapter` would
    decline every one of them, so the suggested fix was one the software refuses.
    """
    c = new_conn()
    seed_email(c, email_id="msg-1", category="hold")
    _ledger_row(c, "msg-1", "Invoice.pdf", attachment_ledger.EMPTY,
                claimed_by="PdfAdapter", kind="pdf")

    item = _only(read_views.manual_queue(c))
    assert item.code == "nothing_recognised"
    assert "will not change that" in item.reason


def test_an_image_ocr_already_read_is_not_labelled_needs_ocr_either():
    """`empty` means an adapter read the file and found nothing. For an image that adapter can only
    have been `OcrAdapter`, so OCR has *already run* — telling a reviewer it "needs OCR" sends them
    to buy a page for a document that has been read. Nothing about this bucket is an OCR problem."""
    c = new_conn()
    seed_email(c, email_id="msg-1", category="hold")
    _ledger_row(c, "msg-1", "signed-bol.jpg", attachment_ledger.EMPTY,
                claimed_by="OcrAdapter", kind="image")

    item = _only(read_views.manual_queue(c))
    assert item.code == "nothing_recognised"
    assert "OcrAdapter read it in full" in item.reason


def test_an_ocr_outage_is_needs_ocr_and_not_a_corrupt_file():
    """Where the label does belong, and the mislabel it replaces.

    `service_unavailable` carries an `error_type`, and the old split sent anything with one to
    "Corrupt" — a chip that says the sender shipped a broken document. 104 intact attachments read
    that way at once when a 15-page OCR budget refused them, so a plain spend ceiling looked like a
    wave of corrupt mail. The file is fine; nobody has read it yet; `tools.reextract` is the fix.
    """
    c = new_conn()
    seed_email(c, email_id="msg-1", category="hold")
    _ledger_row(c, "msg-1", "signed-bol.pdf", attachment_ledger.SERVICE_UNAVAILABLE,
                claimed_by="OcrAdapter", kind="pdf")
    c.execute("UPDATE attachment_ledger SET error_type = 'OcrServiceUnavailable'")
    c.commit()

    item = _only(read_views.manual_queue(c))
    assert item.code == "needs_ocr"


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
    extracted_records_store.write_pending(c, make_record(po_number="910634"), NOW)

    item = [i for i in read_views.manual_queue(c) if i.kind == "record"][0]
    assert item.po_number == "910634"
    assert "PO 910634" not in item.detail, "the PO moved out of detail into its own column"


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


def test_the_record_says_what_was_read_and_where_it_was_read_from():
    """Split in two, because the two halves repeat differently.

    What was delivered differs on every row; where it was read from is the same string for every
    row of one document. Held together they were truncated as one, and on PO 907514's 23 signage
    rows the identical `via excel:Hoja1` tail crowded out the only text that differed.
    """
    c = new_conn()
    seed_email(c)
    extracted_records_store.write_pending(c, make_record(), NOW)

    row = [i for i in read_views.manual_queue(c) if i.kind == "record"][0]
    assert "STE-402-LT" in row.item and "Side table" in row.item
    assert "via" in row.detail
    assert "STE-402-LT" not in row.detail, "the item must not be repeated in both"


def test_records_ready_puts_the_newest_record_first():
    """Ordering by purchase order read tidily but buried the row a person came to look at: a
    record extracted a minute ago landed wherever its PO number happened to sort, halfway down
    thirty-odd rows."""
    c = new_conn()
    seed_email(c)
    ids = [extracted_records_store.write_pending(c, make_complete_record(po_number=po), NOW)
           for po in ("912614", "906481", "909395")]
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


# --- a correctly suppressed duplicate is not a failure to extract --------------------------------

def test_a_suppressed_duplicate_says_what_it_duplicates_not_that_nothing_was_read():
    """Two of Premier's messages sat under "no PO, spec, quantity or POD was recovered from the
    body or any attachment" — every clause false. Both were second copies of a thread whose
    delivery Stage 2 had already released, which is the pipeline working, not failing.
    """
    c = new_conn()
    seed_email(c, email_id="msg-copy-2", category="hold")
    c.execute(
        "INSERT INTO suppressed_notices "
        "(email_id, po_number, delivery_ref, released_at, suppressed_at, reason) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("msg-copy-2", "910634", "shipment:99985", "2026-08-12T11:05:31Z", NOW, "already released"),
    )
    c.commit()

    item = next(i for i in read_views.manual_queue(c) if i.email_id == "msg-copy-2")
    assert item.code == "duplicate"
    assert "already came through on an earlier message" in item.reason
    assert "910634" in item.reason
    assert "nothing was extracted" not in item.reason


def test_mail_that_genuinely_produced_nothing_still_says_so():
    """The residual bucket has to keep working — it is how a real read failure surfaces."""
    c = new_conn()
    seed_email(c, email_id="msg-empty", category="hold")
    c.commit()

    item = next(i for i in read_views.manual_queue(c) if i.email_id == "msg-empty")
    assert item.code == "nothing_extracted"


def test_the_two_buckets_never_both_claim_one_message():
    c = new_conn()
    seed_email(c, email_id="msg-copy-2", category="hold")
    c.execute(
        "INSERT INTO suppressed_notices "
        "(email_id, po_number, delivery_ref, released_at, suppressed_at, reason) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("msg-copy-2", "910634", "", None, NOW, "already released"),
    )
    c.commit()

    codes = [i.code for i in read_views.manual_queue(c) if i.email_id == "msg-copy-2"]
    assert codes == ["duplicate"]


# --- One message, one row -----------------------------------------------------------------


def test_a_message_that_yielded_nothing_is_one_row_however_many_attachments_failed():
    """The count this was built to fix.

    A message nobody could read anything out of was landing on the queue once as the message and
    once per attachment that defeated its adapter — 1092 rows describing 651 messages in Premier's
    live store, one of them with 26. Every one of those rows says the same thing to the person
    working the queue and offers the same single way out, so they are one piece of work.
    """
    c = new_conn()
    seed_email(c, email_id="msg-1", category="route", folder="Routed", reason="asks a question")
    for n in range(4):
        _ledger_row(c, "msg-1", f"scan-{n}.pdf", attachment_ledger.SERVICE_UNAVAILABLE,
                    claimed_by="OcrAdapter", kind="pdf", ordinal=n)

    item = _only(read_views.manual_queue(c))
    assert item.kind == "email"
    assert item.rolled_up == 5, "the message plus its four attachments"
    assert set(item.codes) == {"routed", "needs_ocr"}
    assert "4 attachments awaiting OCR" in item.reason


def test_a_merged_row_is_findable_under_every_reason_it_carries():
    """`codes`, not `code`, is what the chips match. The badge shows one word; the row stands for
    more than one problem, and pressing `Needs OCR` has to surface the message now holding them."""
    c = new_conn()
    seed_email(c, email_id="msg-1", category="route", folder="Routed", reason="asks a question")
    _ledger_row(c, "msg-1", "scan.pdf", attachment_ledger.SERVICE_UNAVAILABLE,
                claimed_by="OcrAdapter", kind="pdf")

    item = _only(read_views.manual_queue(c))
    assert item.code == "routed", "the message's own reason is the headline"
    assert "needs_ocr" in item.codes, "the attachment's reason is still on the row"


def test_the_message_leads_the_sentence_not_one_of_its_files():
    """Ranked by priority alone, a routed message carrying an OCR-failed PDF led with the Azure
    outage — a sentence about a file, on a row about a message."""
    c = new_conn()
    seed_email(c, email_id="msg-1", category="route", folder="Routed",
               reason="asks whether goods were received")
    _ledger_row(c, "msg-1", "scan.pdf", attachment_ledger.SERVICE_UNAVAILABLE,
                claimed_by="OcrAdapter", kind="pdf")

    assert _only(read_views.manual_queue(c)).reason.startswith("asks whether goods were received")


def test_the_cause_leads_when_the_message_only_says_nothing_came_out():
    """`nothing_extracted` is the residual bucket — it means no branch above claimed the message,
    which describes our classifying and not the message. When an attachment nobody could read is
    the reason nothing came out, that is the sentence worth leading with."""
    c = new_conn()
    seed_email(c, email_id="msg-1", category="hold")
    _ledger_row(c, "msg-1", "scan.pdf", attachment_ledger.SERVICE_UNAVAILABLE,
                claimed_by="OcrAdapter", kind="pdf")

    item = _only(read_views.manual_queue(c))
    assert item.code == "needs_ocr"
    assert "nothing_extracted" in item.codes, "the message's own verdict is not thrown away"


def test_a_message_that_did_produce_records_keeps_a_row_per_record():
    """The other half of the rule. A record is already the unit of work — it exists, it has gaps,
    and Fix / Fill / Verify act on that record and no other. Merging those would take every action
    on the page and point it at nothing."""
    c = new_conn()
    seed_email(c, email_id="msg-1", category="hold")
    for spec in ("STE-402-LT", "STE-403-LT", "STE-404-LT"):
        extracted_records_store.write_pending(c, make_record(spec_code=spec), NOW)
    c.commit()

    items = read_views.manual_queue(c)
    assert [i.kind for i in items] == ["record"] * 3
    assert not any(i.rolled_up for i in items), "record rows stand only for themselves"


def test_merging_loses_no_message_and_no_item():
    """Every message still on the queue, and every item still accounted for by the row that
    absorbed it. A merge that quietly dropped work would be worse than the duplication it fixes."""
    c = new_conn()
    seed_email(c, email_id="msg-quiet", category="hold")
    seed_email(c, email_id="msg-routed", category="route", folder="Routed", reason="no PO")
    seed_email(c, email_id="msg-recorded", category="hold")
    _ledger_row(c, "msg-routed", "a.pdf", attachment_ledger.SERVICE_UNAVAILABLE, kind="pdf")
    _ledger_row(c, "msg-routed", "b.pdf", attachment_ledger.SERVICE_UNAVAILABLE, kind="pdf",
                ordinal=1)
    extracted_records_store.write_pending(
        c, make_record(source_email_id="msg-recorded"), NOW)
    c.commit()

    items = read_views.manual_queue(c)
    assert {i.email_id for i in items} == {"msg-quiet", "msg-routed", "msg-recorded"}
    assert sum(i.rolled_up or 1 for i in items) == 5,         "1 quiet + 3 routed-and-its-files + 1 incomplete record"


def test_the_ui_labels_the_reasons_in_the_priority_order_the_merge_uses():
    """`REASON_PRIORITY` decides which of a merged row's reasons is its badge; `_REASON_LABELS`
    decides the words and the chip order. Two orderings of the same twelve codes, in two files,
    that a reader would reasonably assume agree — so they are made to."""
    from api.ui.routes import _REASON_LABELS

    assert tuple(_REASON_LABELS) == read_views.REASON_PRIORITY


# --- A person overruling triage ------------------------------------------------
#
# `email_log.not_a_delivery` is a rule's answer. These assert the other half: a person's answer,
# which wins in both directions and is the only thing that can move a message between the queue and
# the not-a-delivery page.

def _override(conn, email_id, verdict, by="Reviewer", note=""):
    mail_overrides.set_verdict(conn, email_id=email_id, verdict=verdict, decided_by=by,
                               note=note, at=NOW)


def _hidden_email(conn, email_id="msg-hidden"):
    """Mail triage set aside — HIDE, which is what every rule in `NOT_A_DELIVERY_RULES` files."""
    email_log.record(
        conn, email_id=email_id, subject=f"Subject for {email_id}", sender="a@b.com",
        category="hide", matched_rule="rule_5d_no_delivery_claim",
        reason="no hop in this thread states goods arrived", folder="Hidden",
        processed_at=NOW, not_a_delivery=True,
    )


def test_a_message_a_person_sets_aside_leaves_the_queue():
    c = new_conn()
    seed_email(c, email_id="msg-routed", category="route", folder="Routed", reason="no PO")
    assert [i.email_id for i in read_views.manual_queue(c)] == ["msg-routed"]

    _override(c, "msg-routed", mail_overrides.NOT_DELIVERY)

    assert read_views.manual_queue(c) == []


def test_its_attachments_leave_with_it():
    """Filtering only the email branches would leave the message on the queue through its files —
    which reads as a button that did nothing, not as a second problem."""
    c = new_conn()
    seed_email(c, email_id="msg-routed", category="route", folder="Routed", reason="no PO")
    _ledger_row(c, "msg-routed", "a.pdf", attachment_ledger.SERVICE_UNAVAILABLE, kind="pdf")

    _override(c, "msg-routed", mail_overrides.NOT_DELIVERY)

    assert read_views.manual_queue(c) == []


def test_its_records_leave_with_it_too():
    """Setting a message aside is a statement about the whole message.

    This used to be an exemption: a record was held to be work that exists whatever anyone says
    about the mail it came from. That reasoning holds while the only way off this queue is
    finishing the record. It does not hold for a message a person has signed as not delivery mail
    — the document never said goods arrived, so there is nothing to fix — and leaving the rows made
    the verdict look broken.
    """
    c = new_conn()
    seed_email(c, email_id="msg-1", category="route", folder="Routed")
    extracted_records_store.write_pending(c, make_record(po_number=""), NOW)

    _override(c, "msg-1", mail_overrides.NOT_DELIVERY)

    assert read_views.manual_queue(c) == []


def test_a_hidden_message_a_person_calls_a_delivery_comes_back_to_the_queue():
    """The fourth branch. Nothing else on this queue looks at HIDE mail, so without it the action
    takes a message off the not-a-delivery page and puts it nowhere."""
    c = new_conn()
    _hidden_email(c)
    _override(c, "msg-hidden", mail_overrides.DELIVERY, by="Grace Hopper",
              note="the signed POD is attached")

    item = _only(read_views.manual_queue(c))

    assert item.kind == "email"
    assert item.code == "overridden"
    assert "Grace Hopper" in item.reason
    assert "the signed POD is attached" in item.reason
    assert "rule_5d_no_delivery_claim" in item.reason


def test_it_leaves_the_queue_again_once_a_record_is_made_from_it():
    """The whole no-auto-staging contract: nothing extracts from an overridden message, and the
    record a person builds by hand is what ends the row."""
    c = new_conn()
    _hidden_email(c)
    _override(c, "msg-hidden", mail_overrides.DELIVERY)
    extracted_records_store.write_pending(
        c, make_complete_record(source_email_id="msg-hidden"), NOW)

    assert not [i for i in read_views.manual_queue(c) if i.kind == "email"]


def test_a_routed_message_called_a_delivery_is_not_listed_twice():
    """It is already on the queue through the routed branch, and the override adds nothing to it."""
    c = new_conn()
    seed_email(c, email_id="msg-routed", category="route", folder="Routed", reason="no PO")
    _override(c, "msg-routed", mail_overrides.DELIVERY)

    assert len([i for i in read_views.manual_queue(c) if i.email_id == "msg-routed"]) == 1


def test_the_set_aside_view_carries_both_populations():
    """The COALESCE regression. With a bare `v.verdict <> 'delivery'` the NULL on an un-overridden
    row is not true, and every rule-flagged message vanishes while the page looks perfectly well."""
    c = new_conn()
    _hidden_email(c, email_id="msg-by-rule")
    seed_email(c, email_id="msg-by-hand", category="route", folder="Routed")
    _override(c, "msg-by-hand", mail_overrides.NOT_DELIVERY, by="Ada Lovelace",
              note="all-associates broadcast")

    by_id = {r["email_id"]: r for r in read_views.filtered_mail(c)}

    assert set(by_id) == {"msg-by-rule", "msg-by-hand"}
    assert by_id["msg-by-rule"]["decided_by"] is None
    assert by_id["msg-by-rule"]["matched_rule"] == "rule_5d_no_delivery_claim"
    assert by_id["msg-by-hand"]["decided_by"] == "Ada Lovelace"
    assert by_id["msg-by-hand"]["decided_note"] == "all-associates broadcast"
    assert read_views.filtered_count(c) == 2


def test_a_message_called_a_delivery_leaves_the_set_aside_view():
    c = new_conn()
    _hidden_email(c)
    _override(c, "msg-hidden", mail_overrides.DELIVERY)

    assert read_views.filtered_mail(c) == []
    assert read_views.filtered_count(c) == 0


def test_flipping_the_verdict_moves_the_message_back():
    """Reversible in both directions, which is the point of an upsert on one row per message."""
    c = new_conn()
    _hidden_email(c)
    _override(c, "msg-hidden", mail_overrides.DELIVERY)
    assert len(read_views.manual_queue(c)) == 1

    _override(c, "msg-hidden", mail_overrides.NOT_DELIVERY)

    assert read_views.manual_queue(c) == []
    assert len(read_views.filtered_mail(c)) == 1


def test_mail_that_might_be_advertising_is_badged_rather_than_labelled_routed():
    """`rule_5f` exists so "we are not sure" is a state the queue can show. Badging it `routed`
    would put it among four hundred other unresolved messages, which is where it was already."""
    c = new_conn()
    email_log.record(
        c, email_id="msg-maybe", subject="70% off clearance for Sheraton Anchorage",
        sender="deals@members.shop.test", category="route",
        matched_rule="rule_5f_possible_advertising",
        reason="this looks like advertising", folder="Routed", processed_at=NOW)

    item = _only(read_views.manual_queue(c))

    assert item.code == "maybe_advertising"
    assert item.kind == "email"


def test_ordinary_routed_mail_keeps_its_own_label():
    c = new_conn()
    seed_email(c, email_id="msg-routed", category="route", folder="Routed", reason="no PO")

    assert _only(read_views.manual_queue(c)).code == "routed"


# --- Premier's own worklists get their own chip ----------------------------------------------


def _flag_status_report(conn, email_id, filename, reason):
    """Mark a ledger row the way ingest now does, without needing the workbook itself."""
    conn.execute(
        "UPDATE attachment_ledger SET is_status_report = 1, status_report_reason = ? "
        "WHERE email_id = ? AND filename = ?",
        (reason, email_id, filename))
    conn.commit()


def test_a_message_carrying_an_expediting_report_is_chipped_as_a_status_report():
    """The five routes onto this queue become one chip.

    `DBR025PB100002 Expediting Report 09.10.2026.xlsx` reached the queue saying only that nothing
    was extractable, which is true and useless — nothing ever will be. The row now says why.
    """
    c = new_conn()
    seed_email(c, email_id="msg-1", category="hold")
    _ledger_row(c, "msg-1", "DBR025 Expediting Report.xlsx", attachment_ledger.EMPTY,
                claimed_by="ExcelAdapter", kind="xlsx")
    _flag_status_report(c, "msg-1", "DBR025 Expediting Report.xlsx",
                        "the sheet 'Expediting' is a system export")

    items = read_views.manual_queue(c)
    assert [i.code for i in items] == ["status_report"]
    assert "not a delivery document" in items[0].reason
    assert "a system export" in items[0].reason


def test_a_workbook_that_records_a_receipt_keeps_its_own_reason():
    """`Cameo Receivers.xlsx` is the real thing and is never re-badged."""
    c = new_conn()
    seed_email(c, email_id="msg-1", category="hold")
    _ledger_row(c, "msg-1", "Cameo Receivers.xlsx", attachment_ledger.EMPTY,
                claimed_by="ExcelAdapter", kind="xlsx")

    items = read_views.manual_queue(c)
    assert [i.code for i in items] == ["nothing_recognised"]


def test_an_unrelated_broken_attachment_keeps_saying_so():
    """A corrupt PDF riding on the same message is a second, unrelated problem."""
    c = new_conn()
    seed_email(c, email_id="msg-1", category="hold")
    _ledger_row(c, "msg-1", "Expediting Report.xlsx", attachment_ledger.EMPTY,
                claimed_by="ExcelAdapter", kind="xlsx", ordinal=0)
    _ledger_row(c, "msg-1", "scan.pdf", attachment_ledger.CORRUPT,
                claimed_by="PdfAdapter", kind="pdf", ordinal=1)
    _flag_status_report(c, "msg-1", "Expediting Report.xlsx", "the sheet 'Expediting' is a system export")

    items = read_views.manual_queue(c)
    # One merged row for the message, but both facts survive in `codes`.
    assert len(items) == 1
    assert "status_report" in items[0].codes
    assert "corrupt" in items[0].codes


def test_a_record_on_a_status_report_message_keeps_its_own_code():
    """Record rows are exempt from the set-aside filter, so re-badging one would promise a
    disposal this page cannot deliver."""
    c = new_conn()
    seed_email(c, email_id="msg-1", category="hold")
    _ledger_row(c, "msg-1", "Expediting Report.xlsx", attachment_ledger.EXTRACTED,
                claimed_by="ExcelAdapter", kind="xlsx", records=9)
    _flag_status_report(c, "msg-1", "Expediting Report.xlsx", "the sheet 'Expediting' is a system export")
    extracted_records_store.write_pending(c, make_record(po_number="", source_email_id="msg-1"), NOW)

    records = [i for i in read_views.manual_queue(c) if i.kind == "record"]
    assert records and all(i.code != "status_report" for i in records)


def test_setting_a_status_report_aside_clears_its_rows():
    c = new_conn()
    seed_email(c, email_id="msg-1", category="hold")
    _ledger_row(c, "msg-1", "Expediting Report.xlsx", attachment_ledger.EMPTY,
                claimed_by="ExcelAdapter", kind="xlsx")
    _flag_status_report(c, "msg-1", "Expediting Report.xlsx", "the sheet 'Expediting' is a system export")
    assert read_views.manual_queue(c)

    mail_overrides.set_verdict(c, email_id="msg-1", verdict=mail_overrides.NOT_DELIVERY,
                               decided_by="RK", at=NOW)
    assert read_views.manual_queue(c) == []


def test_a_person_vouching_for_the_mail_outranks_the_spreadsheet_rule():
    """Both can be true: Premier forwards an expediting report with a signed POD under it.

    `overridden` is the one code on this queue that records somebody vouching for the mail, and
    overwriting it would leave them looking at a row saying the opposite of what they decided.
    """
    c = new_conn()
    seed_email(c, email_id="msg-1", category="hide", folder="Hidden")
    _ledger_row(c, "msg-1", "Expediting Report.xlsx", attachment_ledger.EXTRACTED,
                claimed_by="ExcelAdapter", kind="xlsx", records=0)
    _flag_status_report(c, "msg-1", "Expediting Report.xlsx",
                        "the sheet 'Expediting' is a system export")
    mail_overrides.set_verdict(c, email_id="msg-1", verdict=mail_overrides.DELIVERY,
                               decided_by="RK", at=NOW)

    codes = {i.code for i in read_views.manual_queue(c)}
    assert "overridden" in codes


# --- a set-aside message retires every record read out of it ----------------------------------


def test_its_records_leave_the_awaiting_confirmation_count():
    """The leak that was 251 records wide on the live store.

    `records_awaiting_confirmation` only ever asked who wrote the message and whether anyone had
    confirmed it. A person could sign an expediting report as not delivery mail and its 632 records
    would keep being counted as work waiting on them.
    """
    c = new_conn()
    seed_email(c, email_id="ours", origin_sender=INTERNAL)
    extracted_records_store.write_pending(
        c, make_complete_record(source_email_id="ours", extraction_source="excel:Expediting"), NOW)
    assert len(read_views.records_awaiting_confirmation(c)) == 1

    _override(c, "ours", mail_overrides.NOT_DELIVERY)

    assert read_views.records_awaiting_confirmation(c) == []


def test_a_complete_record_on_set_aside_external_mail_is_never_postable():
    """The one that was only closed by luck.

    Nothing from a set-aside message reached the Records page before this — but only because every
    such message in the store happened to be internally authored, and `_awaits_confirmation` held
    its records back. An **externally** authored message with complete records put them straight on
    Records with a live Post button, and `routes.post_report_fragment` loads its row through
    `records_ready`. So this asserts the guard that actually stops a receipt being posted for a
    delivery somebody has declared was never a delivery.
    """
    c = new_conn()
    seed_email(c, email_id="msg-1")          # external sender, so nothing else withholds it
    rid = extracted_records_store.write_pending(c, make_complete_record(), NOW)
    assert rid in {r["id"] for r in read_views.records_ready(c)}

    _override(c, "msg-1", mail_overrides.NOT_DELIVERY)

    assert read_views.records_ready(c) == []


def test_the_edit_form_stops_serving_a_retired_record():
    """`records_fixable` is what `/ui/records/{id}/edit` resolves against, so a retired record has
    to fall out of it as well — otherwise the row is off every page and still editable by URL."""
    c = new_conn()
    seed_email(c, email_id="msg-1", category="route", folder="Routed")
    extracted_records_store.write_pending(c, make_record(), NOW)
    assert len(read_views.records_fixable(c)) == 1

    _override(c, "msg-1", mail_overrides.NOT_DELIVERY)

    assert read_views.records_fixable(c) == []


def test_putting_the_message_back_brings_every_record_with_it():
    """Retired, not destroyed — which is the whole reason this is derived from the verdict rather
    than written onto the row. One click has to undo all of it."""
    c = new_conn()
    seed_email(c, email_id="msg-1")
    rid = extracted_records_store.write_pending(c, make_complete_record(), NOW)
    _override(c, "msg-1", mail_overrides.NOT_DELIVERY)
    assert read_views.records_ready(c) == []

    _override(c, "msg-1", mail_overrides.DELIVERY)

    assert rid in {r["id"] for r in read_views.records_ready(c)}


def test_a_retired_record_is_on_no_destination_and_that_is_the_invariant():
    """The partition claim, restated. The three destinations partition the pending records *that
    nobody has set aside*; a retired one is deliberately on none of them."""
    c = new_conn()
    seed_email(c, email_id="msg-1", category="route", folder="Routed")
    rid = extracted_records_store.write_pending(c, make_record(po_number=""), NOW)
    _override(c, "msg-1", mail_overrides.NOT_DELIVERY)

    ready, manual, awaiting, all_pending = _destinations(c)
    assert rid in all_pending, "the row is kept, not deleted"
    assert rid not in ready | manual | awaiting


# --- the rest of one conversation -----------------------------------------------------------------
# Setting a message aside covers that message and no other, so the replies and forwards carrying the
# same thing stay on the queue. Measured on the live store 2026-09-15: 786 other messages sharing a
# subject with a set-aside one still put 1,308 rows on Needs a human, and one thread accounted for
# 95 of them. `thread_siblings` is what the page offers to set aside with it.


def _mail(conn, email_id, subject, po_hints="212749", sender="vendor@example-mill.test"):
    email_log.record(conn, email_id=email_id, subject=subject, sender=sender,
                     origin_sender=sender, category="surface", matched_rule="rule_1a",
                     reason="names a delivery", folder="Processed", processed_at=NOW,
                     po_hints=po_hints)


def test_a_reply_and_a_forward_of_the_same_message_are_siblings():
    conn = new_conn()
    _mail(conn, "msg-1", "Cameo Public Space delivery confirmation")
    _mail(conn, "msg-2", "RE: Cameo Public Space delivery confirmation")
    _mail(conn, "msg-3", "Fwd: FW: [External] Cameo Public Space delivery confirmation")

    found = [s.email_id for s in read_views.thread_siblings(conn, "msg-1")]

    assert sorted(found) == ["msg-2", "msg-3"]


def test_the_message_itself_is_never_one_of_its_own_siblings():
    conn = new_conn()
    _mail(conn, "msg-1", "Cameo Public Space delivery confirmation")

    assert read_views.thread_siblings(conn, "msg-1") == []


def test_the_same_subject_is_not_enough_without_a_purchase_order_in_common():
    """Two properties can both send `Delivery confirmation`. Gathering them together would set
    aside a real delivery on the strength of a shared word."""
    conn = new_conn()
    _mail(conn, "msg-1", "Delivery confirmation required for the shipment", po_hints="212749")
    _mail(conn, "msg-2", "RE: Delivery confirmation required for the shipment", po_hints="999001")

    assert read_views.thread_siblings(conn, "msg-1") == []


def test_a_short_generic_subject_with_no_purchase_order_gathers_nothing():
    conn = new_conn()
    _mail(conn, "msg-1", "Delivery", po_hints="")
    _mail(conn, "msg-2", "RE: Delivery", po_hints="")

    assert read_views.thread_siblings(conn, "msg-1") == []


def test_a_long_subject_with_no_purchase_order_on_either_side_still_matches():
    conn = new_conn()
    _mail(conn, "msg-1", "Ritz Carlton model room area rugs shipment paperwork", po_hints="")
    _mail(conn, "msg-2", "RE: Ritz Carlton model room area rugs shipment paperwork", po_hints="")

    assert [s.email_id for s in read_views.thread_siblings(conn, "msg-1")] == ["msg-2"]


def test_a_message_a_person_has_already_ruled_on_is_offered_but_marked_as_decided():
    """Their decision is not silently reversed, and it is not hidden either — the page shows it
    greyed with the reason, so nobody wonders why the rows from it stayed."""
    conn = new_conn()
    _mail(conn, "msg-1", "Cameo Public Space delivery confirmation")
    _mail(conn, "msg-2", "RE: Cameo Public Space delivery confirmation")
    mail_overrides.set_verdict(conn, email_id="msg-2", verdict=mail_overrides.DELIVERY,
                               decided_by="M Rivera", note="the POD is attached", at=NOW)

    sibling = read_views.thread_siblings(conn, "msg-1")[0]

    assert sibling.email_id == "msg-2"
    assert sibling.excluded, "a decision a person signed must not be quietly overwritten"
    assert "M Rivera" in sibling.excluded


def test_a_message_someone_built_a_record_from_by_hand_is_not_offered_either():
    conn = new_conn()
    _mail(conn, "msg-1", "Cameo Public Space delivery confirmation")
    _mail(conn, "msg-2", "RE: Cameo Public Space delivery confirmation")
    extracted_records_store.write_pending(
        conn, make_record(source_email_id="msg-2"), NOW,
        extra={"origin": "manual", "created_by": "M Rivera"})

    sibling = read_views.thread_siblings(conn, "msg-1")[0]

    assert sibling.excluded


def test_a_sibling_says_how_many_records_would_go_with_it():
    conn = new_conn()
    _mail(conn, "msg-1", "Cameo Public Space delivery confirmation")
    _mail(conn, "msg-2", "RE: Cameo Public Space delivery confirmation")
    for _ in range(3):
        extracted_records_store.write_pending(conn, make_record(source_email_id="msg-2"), NOW)

    sibling = read_views.thread_siblings(conn, "msg-1")[0]

    assert sibling.records == 3
    assert not sibling.excluded


def test_the_queue_count_follows_a_write_made_through_the_app(tmp_path):
    """The queue is cached with no expiry, so the thing that must be exact is its change key.

    This is the failure that key exists to prevent: someone sets a message aside, the page redraws,
    and the header still counts it. Written against a write on a *separate* connection because that
    is how the app does it -- a POST handler opens its own (`routes._live_conn`), and the GET that
    follows the redirect opens another.
    """
    from pipeline import email_log, read_views, state_db

    store = tmp_path / "queue.sqlite3"
    reader = state_db.get_connection(store)
    assert read_views.manual_queue(reader) == []

    writer = state_db.get_connection(store)
    email_log.record(writer, email_id="q1", subject="a delivery", sender="v@example.test",
                     category="surface", email_date="2026-09-16T09:00:00", folder="Inbox",
                     processed_at="2026-09-16T09:00:00")
    writer.commit()
    writer.close()

    # No invalidation call anywhere between these two lines. The key alone has to notice.
    assert len(read_views.manual_queue(reader)) == 1
    reader.close()


# --- cancellations -----------------------------------------------------------------------------
#
# Triage routes an order cancellation out of the delivery path and says it "needs a manual PO
# update in Spitfire". Nothing carried it the last step: measured on the live store 2026-09-16,
# 7 purchase orders had been cancelled by mail and none of the 7 was Canceled in Spitfire.

def _cancellation(conn, email_id, po_hints, *, subject="Order cancelled", sender="v@vendor.test",
                  email_date="2026-09-10T09:00:00Z"):
    email_log.record(
        conn=conn, email_id=email_id, subject=subject, sender=sender, category="route",
        matched_rule="rule_0a_order_cancellation", reason="order cancellation notice",
        folder="Routed", processed_at=NOW, notification_type="order_cancellation",
        po_hints=po_hints, email_date=email_date)


def _mirror_po_status(conn, po_number, doc_status, label):
    """Distinct from `_mirror_po` above, which this file already uses for order dates."""
    conn.execute(
        "INSERT INTO spitfire_po_index (po_number, doc_master_key, doc_status, doc_status_label,"
        " refreshed_at) VALUES (?,?,?,?,?)",
        (po_number, "key-" + po_number, doc_status, label, NOW))
    conn.commit()


def test_a_cancelled_order_is_listed_until_spitfire_says_canceled():
    c = new_conn()
    _cancellation(c, "msg-c1", "908491")
    _mirror_po_status(c, "908491", "M", "Committed")

    work = read_views.cancellation_worklist(c)
    assert [w["po_number"] for w in work] == ["908491"]
    assert work[0]["done"] is False, "Spitfire still holds it — the job is outstanding"
    assert work[0]["spitfire_status"] == "Committed"


def test_an_order_cancelled_in_spitfire_drops_off_by_itself():
    """`done` is read back from the mirror rather than remembered, so somebody closing the order
    out directly in Spitfire clears the row without telling us."""
    c = new_conn()
    _cancellation(c, "msg-c2", "908491")
    _mirror_po_status(c, "908491", "C", "Canceled")

    assert read_views.cancellation_worklist(c)[0]["done"] is True


def test_an_order_the_mirror_has_never_seen_is_not_reported_as_outstanding_or_done():
    """Three states, not two. `None` means nothing here knows what Spitfire holds — the same
    distinction `mirror_line_verdicts` draws, and for the same reason."""
    c = new_conn()
    _cancellation(c, "msg-c3", "908491")

    assert read_views.cancellation_worklist(c)[0]["done"] is None


def test_the_newest_notice_wins_when_an_order_is_cancelled_twice():
    c = new_conn()
    _cancellation(c, "msg-old", "908491", subject="first", email_date="2026-09-01T09:00:00Z")
    _cancellation(c, "msg-new", "908491", subject="second", email_date="2026-09-20T09:00:00Z")

    work = read_views.cancellation_worklist(c)
    assert len(work) == 1, "an order cancelled twice is still one job"
    assert work[0]["subject"] == "second"


def test_one_notice_naming_several_orders_becomes_several_rows():
    """Cintas cancelled two purchase orders in one mail; the work is per order."""
    c = new_conn()
    _cancellation(c, "msg-c4", "908491, 908492")

    assert sorted(w["po_number"] for w in read_views.cancellation_worklist(c)) == \
        ["908491", "908492"]


def test_the_notice_itself_is_carried_so_a_reviewer_can_judge_it():
    """`CANCELLATION_RE` is a keyword match over subject and body, and two of the sixty-six on the
    live store are out-of-office replies on a thread whose subject reads FINAL NOTICE. They name
    two purchase orders Spitfire still holds as Committed. The subject and sender travel with the
    row precisely so that is visible before anyone retires a commitment."""
    c = new_conn()
    _cancellation(c, "msg-c5", "908491",
                  subject="Automatic reply: [External] RE: FINAL NOTICE",
                  sender="someone@vendor.test")

    row = read_views.cancellation_worklist(c)[0]
    assert row["subject"].startswith("Automatic reply")
    assert row["sender"] == "someone@vendor.test"
    assert row["email_id"] == "msg-c5", "the notice must be openable from the row"


def test_notices_naming_no_order_are_counted_not_listed():
    """Sixty of the sixty-six name nothing actionable. Listing them would bury the seven that do;
    counting them keeps the omission visible."""
    c = new_conn()
    _cancellation(c, "msg-c6", "908491")
    _cancellation(c, "msg-c7", "")

    assert len(read_views.cancellation_worklist(c)) == 1
    assert read_views.cancellations_without_a_po(c) == 1
