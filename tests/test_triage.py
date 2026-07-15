import sqlite3
from pathlib import Path

from pipeline import state_db
from pipeline.models import NotificationType, RawEmail, TriageCategory
from pipeline.stage1_triage import triage
from connectors.mailbox import LocalFolderMailbox

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "sample_data" / "emails"


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


def test_rule1_warehouse_table_surfaces_as_inbound():
    email = make_email(
        sender_address="notify@authoritylogistics.com",
        sender_domain="authoritylogistics.com",
        subject="Inbound - PO 208491",
        body_html="<table><tr><th>PO</th><th>Spec</th><th>Qty</th></tr><tr><td>208491</td><td>LI-1</td><td>1</td></tr></table>",
    )
    result = triage(email)
    assert result.notification_type == NotificationType.WAREHOUSE_INBOUND
    assert result.category == TriageCategory.SURFACE
    assert result.matched_rule == "rule_1_warehouse_table"


def test_rule2_freight_status_is_hidden():
    email = make_email(
        sender_address="tracking@fedex.com",
        sender_domain="fedex.com",
        subject="Your package has been delivered",
        body_text="Your package was delivered to the front desk.",
    )
    result = triage(email)
    assert result.notification_type == NotificationType.DELIVERED_SHIPPED
    assert result.category == TriageCategory.HIDE
    assert result.matched_rule == "rule_2_freight_status"
    assert "intermediate" in result.reason


def test_rule3_warehouse_without_table_still_surfaces():
    email = make_email(
        sender_address="notify@hospitalitylogistics.com",
        sender_domain="hospitalitylogistics.com",
        subject="Inbound 239336 for PO 208491",
        body_text="Inbound 239336 received for PO 208491, all items accounted for.",
    )
    result = triage(email)
    assert result.notification_type == NotificationType.INBOUND_NOTIFICATION
    assert result.category == TriageCategory.SURFACE
    assert result.matched_rule == "rule_3_warehouse_no_table"


def test_rule4_property_reply_is_held():
    email = make_email(
        sender_address="gm@hgivirginiabeach-example.com",
        sender_domain="hgivirginiabeach-example.com",
        subject="RE: SCC: PO 212448 confirmation",
        body_text="Hi, yes we received all items for PO 212448 in good condition. Thanks!",
    )
    result = triage(email)
    assert result.notification_type == NotificationType.PROPERTY_CONFIRMATION
    assert result.category == TriageCategory.HOLD
    assert result.matched_rule == "rule_4_property_reply"


def test_rule5_vendor_confirmation_checked_before_generic_property_rule():
    email = make_email(
        sender_address="gary@pbhhospitality.com",
        sender_domain="pbhhospitality.com",
        subject="RE: PO 213987 delivery confirmed",
        body_text="Confirming the delivery for PO 213987 has been completed.",
    )
    result = triage(email)
    assert result.notification_type == NotificationType.VENDOR_CONFIRMATION
    assert result.category == TriageCategory.HOLD
    assert result.matched_rule == "rule_5_vendor_confirmation"


def test_rule6_no_po_reference_is_routed():
    email = make_email(
        sender_address="marketing@wayfair.com",
        sender_domain="wayfair.com",
        subject="It's delivery day!",
        body_text="Great news — your order is arriving today.",
    )
    result = triage(email)
    assert result.notification_type == NotificationType.UNKNOWN
    assert result.category == TriageCategory.ROUTE
    assert result.matched_rule == "rule_6_unknown"
    assert result.extracted_po_hints == []


def test_two_po_numbers_in_one_table_both_captured():
    email = make_email(
        sender_address="notify@atlaslogistics.com",
        sender_domain="atlaslogistics.com",
        subject="Inbound for PO 206725 and PO 207665",
        body_html=(
            "<table><tr><th>PO</th><th>Spec</th><th>Qty</th></tr>"
            "<tr><td>206725</td><td>EXT-901-AC</td><td>4</td></tr>"
            "<tr><td>207665</td><td>EXT-902-AC</td><td>2</td></tr></table>"
        ),
    )
    result = triage(email)
    assert result.category == TriageCategory.SURFACE
    assert result.extracted_po_hints == ["206725", "207665"]


def test_local_folder_mailbox_reads_fixture_emails():
    mailbox = LocalFolderMailbox(FIXTURES_DIR)
    emails = mailbox.fetch_new()
    ids = {e.email_id for e in emails}
    assert "msg-warehouse-213987" in ids
    assert "msg-wayfair-delivery-day" in ids
    warehouse_email = next(e for e in emails if e.email_id == "msg-warehouse-213987")
    result = triage(warehouse_email)
    assert result.notification_type == NotificationType.WAREHOUSE_INBOUND
    assert result.extracted_po_hints == ["213987"]


def test_idempotency_second_call_with_same_id_is_skipped():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE seen_message_ids (email_id TEXT PRIMARY KEY, seen_at TEXT NOT NULL)"
    )
    assert state_db.is_new_message(conn, "msg-dup-1", "2026-06-08T14:00:00Z") is True
    assert state_db.is_new_message(conn, "msg-dup-1", "2026-06-08T14:05:00Z") is False
