from datetime import datetime, timedelta, timezone

import pytest

from pipeline import state_db, stage2_accumulate
from pipeline.models import RawEmail, TriageCategory
from pipeline.stage1_triage import triage


def make_email(**overrides):
    defaults = dict(
        email_id="msg-1",
        received_at="2026-06-08T14:00:00Z",
        sender_address="someone@example.com",
        sender_domain="example.com",
        subject="",
        body_html=None,
        body_text=None,
        attachments=[],
    )
    defaults.update(overrides)
    return RawEmail(**defaults)


@pytest.fixture
def conn():
    c = state_db.get_connection(":memory:")
    yield c
    c.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def test_hidden_emails_never_reach_stage2_by_construction():
    email = make_email(
        sender_address="tracking@fedex.com", sender_domain="fedex.com",
        subject="Your package has been delivered", body_text="delivered to the dock",
    )
    triaged = triage(email)
    assert triaged.category == TriageCategory.HIDE
    # Documents the contract: the real orchestrator only ever calls Stage 2 with
    # SURFACE/HOLD categories — HIDE'd emails are discarded at Stage 1 already.


def test_inbound_notification_alone_releases_immediately(conn):
    email = make_email(
        email_id="msg-inbound-1",
        sender_address="notify@authoritylogistics.com", sender_domain="authoritylogistics.com",
        subject="Inbound 239336 - shipment 50052 / PO 208491",
        body_text="Inbound 239336 received for PO 208491, all items accounted for.",
    )
    triaged = triage(email)
    assert triaged.category == TriageCategory.SURFACE

    events = stage2_accumulate.process_triaged_email(conn, triaged, now_iso())
    assert len(events) == 1
    assert events[0].key.po_number == "208491"
    assert events[0].key.shipment_number == "50052"
    assert len(events[0].emails) == 1


def test_duplicate_after_release_is_discarded_not_rereleased(conn):
    email = make_email(
        email_id="msg-inbound-2",
        sender_address="notify@authoritylogistics.com", sender_domain="authoritylogistics.com",
        subject="Inbound - shipment 60001 / PO 208491",
        body_text="Inbound received for PO 208491.",
    )
    first = stage2_accumulate.process_triaged_email(conn, triage(email), now_iso())
    assert len(first) == 1

    resent = make_email(
        email_id="msg-inbound-2-resent",
        sender_address="notify@authoritylogistics.com", sender_domain="authoritylogistics.com",
        subject="FWD: Inbound - shipment 60001 / PO 208491",
        body_text="Inbound received for PO 208491.",
    )
    second = stage2_accumulate.process_triaged_email(conn, triage(resent), now_iso())
    assert second == []


def test_property_confirmation_only_releases_after_grace_period(conn):
    email = make_email(
        email_id="msg-property-1",
        sender_address="gm@hgivirginiabeach-example.com", sender_domain="hgivirginiabeach-example.com",
        subject="RE: PO 212448 confirmation",
        body_text="Yes, we received all items for PO 212448 in good condition.",
    )
    triaged = triage(email)
    assert triaged.category == TriageCategory.HOLD

    old_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert stage2_accumulate.process_triaged_email(conn, triaged, old_time.isoformat()) == []

    too_soon = old_time + timedelta(hours=1)
    assert stage2_accumulate.sweep_stale_holds(conn, too_soon) == []

    past_grace_period = old_time + timedelta(hours=49)
    swept = stage2_accumulate.sweep_stale_holds(conn, past_grace_period)
    assert len(swept) == 1
    assert swept[0].release_reason == "grace period elapsed, using confirmation as trigger"


def test_two_po_email_releases_two_independent_delivery_events(conn):
    email = make_email(
        email_id="msg-two-po",
        sender_address="notify@atlaslogistics.com", sender_domain="atlaslogistics.com",
        subject="Inbound for PO 206725 and PO 207665 - shipment 70009",
        body_html=(
            "<table><tr><th>PO</th><th>Spec</th><th>Qty</th></tr>"
            "<tr><td>206725</td><td>EXT-901-AC</td><td>4</td></tr>"
            "<tr><td>207665</td><td>EXT-902-AC</td><td>2</td></tr></table>"
        ),
    )
    events = stage2_accumulate.process_triaged_email(conn, triage(email), now_iso())
    assert len(events) == 2
    assert {e.key.po_number for e in events} == {"206725", "207665"}
    assert all(e.key.shipment_number == "70009" for e in events)


def test_property_confirmation_then_true_inbound_releases_on_inbound_with_both_bundled(conn):
    property_email = make_email(
        email_id="msg-property-2",
        sender_address="gm@somehotel-example.com", sender_domain="somehotel-example.com",
        subject="RE: PO 213500 confirmation",
        body_text="We received the items for PO 213500 today.",
    )
    assert stage2_accumulate.process_triaged_email(conn, triage(property_email), now_iso()) == []

    inbound_email = make_email(
        email_id="msg-inbound-3",
        sender_address="notify@hospitalitylogistics.com", sender_domain="hospitalitylogistics.com",
        subject="Inbound for PO 213500",
        body_text="Inbound received for PO 213500.",
    )
    events = stage2_accumulate.process_triaged_email(conn, triage(inbound_email), now_iso())
    assert len(events) == 1
    assert len(events[0].emails) == 2
