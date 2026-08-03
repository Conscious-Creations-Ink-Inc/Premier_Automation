"""Stage 1 triage, against corpus-shaped mail (see tests/corpus_fixtures.py)."""

import sqlite3
from pathlib import Path

from connectors.mailbox import LocalFolderMailbox
from pipeline import state_db
from pipeline.models import Attachment, NotificationType, RawEmail, TriageCategory
from pipeline.stage1_triage import triage
from tests import corpus_fixtures as fx

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


# --- Authority Logistics: the (local part, subject) rules --------------------


def test_inbound_notification_surfaces_as_the_receiver_trigger():
    result = triage(fx.inbound_email())
    assert result.notification_type == NotificationType.WAREHOUSE_INBOUND
    assert result.category == TriageCategory.SURFACE
    assert result.matched_rule == "rule_1a_authority_inbound"
    assert result.extracted_po_hints == ["208491"]


def test_delivered_notification_is_held_not_surfaced():
    """The carrier reaching the warehouse door is not a receiving event. Surfacing it as well as
    the Inbound that follows is the double-receipt that ended Premier's previous attempt."""
    result = triage(fx.delivered_email())
    assert result.notification_type == NotificationType.DELIVERED_SHIPPED
    assert result.category == TriageCategory.HOLD
    assert result.matched_rule == "rule_1b_authority_delivered"


def test_status_report_from_the_trigger_sender_is_hidden():
    """The weekly summary arrives from `warehousing@` — the same address as the receiver
    trigger — so only the subject can tell them apart."""
    result = triage(fx.status_report_email())
    assert result.notification_type == NotificationType.WAREHOUSE_STATUS_REPORT
    assert result.category == TriageCategory.HIDE
    assert result.matched_rule == "rule_1c_authority_status_report"


def test_same_notice_direct_and_forwarded_triage_identically():
    """Notice 239336 exists in the corpus twice: once delivered straight from Authority, once
    forwarded by an expeditor. Rules keyed on the envelope sender see `premierpm.com` on the
    second one and get it wrong."""
    direct = triage(fx.inbound_email(email_id="direct", forwarded=False))
    forwarded = triage(fx.inbound_email(email_id="forwarded", forwarded=True))

    assert forwarded.email.sender_domain == "premierpm.com"
    assert forwarded.origin_sender_address == fx.WAREHOUSING
    for field in ("category", "notification_type", "matched_rule", "extracted_po_hints",
                  "extracted_shipment_hint", "notification_number"):
        assert getattr(direct, field) == getattr(forwarded, field), field


def test_delivered_and_inbound_share_one_shipment_key():
    """`Authority #` on a Delivered notice is `ALS Shipment #` on the matching Inbound. That
    equality is the join Stage 2 needs to fire once for one physical delivery."""
    delivered = triage(fx.delivered_email(notice="50009", po_numbers=("206725",)))
    inbound = triage(fx.inbound_email(notice="239260", po_numbers=("206725",), shipment="50009 : 1"))
    assert delivered.extracted_shipment_hint == inbound.extracted_shipment_hint == "50009"


def test_inbound_with_blank_shipment_number_reports_none_not_the_inbound_number():
    """Corpus notice 239475 leaves `ALS Shipment #` empty. Substituting the inbound number would
    look like a key but join to nothing, silently splitting one delivery in two."""
    result = triage(fx.inbound_email(notice="239475", shipment=""))
    assert result.extracted_shipment_hint is None
    assert result.notification_number == "239475"


def test_multi_po_inbound_captures_every_po_from_the_subject_slot():
    email = fx.inbound_email(
        notice="239260", po_numbers=("206725", "207665"),
        lines=[
            {"po": "206725", "line": "1", "part": "EXT-925-AC",
             "item": '2 EACH - EXT-925-AC-Linear Planter w/Pocket(s) 96"Lx30"Wx24"'},
            {"po": "207665", "line": "1", "part": "POOL-925-AC", "item": "3 EA - POOL-925-AC Linear Planter"},
        ],
    )
    result = triage(email)
    assert result.category == TriageCategory.SURFACE
    assert result.extracted_po_hints == ["206725", "207665"]


def test_inbound_number_is_never_mistaken_for_a_po():
    """Inbound numbers are six digits too — 239336 sits in the same subject as PO 208491. A bare
    `\\d{6}` scan returns both."""
    result = triage(fx.inbound_email(notice="239336", po_numbers=("208491",)))
    assert "239336" not in result.extracted_po_hints


# --- Other senders ----------------------------------------------------------


def test_freight_status_is_hidden():
    email = make_email(
        sender_address="tracking@fedex.com", sender_domain="fedex.com",
        subject="Your package has been delivered",
        body_text="Your package was delivered to the front desk.",
    )
    result = triage(email)
    assert result.notification_type == NotificationType.DELIVERED_SHIPPED
    assert result.category == TriageCategory.HIDE
    assert result.matched_rule == "rule_2_freight_status"


def test_vendor_confirmation_reads_the_po_out_of_the_quoted_request_grid():
    """The reply itself names no PO and no quantity — both live in the grid Premier sent three
    hops earlier. Reading only the newest hop finds nothing."""
    result = triage(fx.confirmation_request_email())
    assert result.notification_type == NotificationType.VENDOR_CONFIRMATION
    assert result.category == TriageCategory.HOLD
    assert result.matched_rule == "rule_3_vendor_confirmation"
    assert result.extracted_po_hints == ["210634", "210635"]


def test_property_reply_is_held():
    email = make_email(
        sender_address="joshuasoto@remingtonhotels.com", sender_domain="remingtonhotels.com",
        subject="RE: Cameo Public Space Knoxtile PO 212448",
        body_text="Hi, yes we received all items for PO 212448 in good condition. Thanks!",
    )
    result = triage(email)
    assert result.notification_type == NotificationType.PROPERTY_CONFIRMATION
    assert result.category == TriageCategory.HOLD
    assert result.matched_rule == "rule_4_property_reply"


def test_boilerplate_does_not_push_a_short_reply_past_the_word_limit():
    """A four-word confirmation carrying six repeated confidentiality notices and a signature
    block measures in the hundreds of words unless boilerplate is stripped first."""
    notice = ("NOTICE: This email contains confidential information solely for the use of the "
              "intended recipient(s). If you are not said recipient your use, disclosure or other "
              "distribution of any information included herewith is STRICTLY PROHIBITED, and you "
              "are instructed to notify the sender immediately and delete this email, all copies "
              "and attachments.")
    email = make_email(
        sender_address="joshuasoto@remingtonhotels.com", sender_domain="remingtonhotels.com",
        subject="RE: PO 212448",
        body_text="Yes ma'am, this was received!\n\n" + ("\n\n".join([notice] * 6)),
    )
    result = triage(email)
    assert result.category == TriageCategory.HOLD
    assert result.matched_rule == "rule_4_property_reply"


def test_loss_or_claim_thread_is_routed_to_a_person():
    email = make_email(
        sender_address="msmith@authoritylogistics.com", sender_domain="authoritylogistics.com",
        subject="RE: Cameo Public Space",
        body_text=("RES-100b-EQ for PO #208453 was lost by the warehouse. "
                   "The new PO for the replacement is PO #211400."),
    )
    result = triage(email)
    assert result.notification_type == NotificationType.LOSS_OR_CLAIM
    assert result.category == TriageCategory.ROUTE
    assert result.matched_rule == "rule_0b_loss_or_claim"


def test_cancellation_is_routed_regardless_of_sender():
    email = make_email(
        sender_address=fx.WAREHOUSING, sender_domain="authoritylogistics.com",
        subject="Please cancel PO 213987 - no longer required",
        body_text="This order has been cancelled and is no longer required.",
    )
    result = triage(email)
    assert result.notification_type == NotificationType.ORDER_CANCELLATION
    assert result.category == TriageCategory.ROUTE
    assert "manual PO update" in result.reason


def test_cancellation_wins_over_a_valid_inbound_notification():
    email = fx.inbound_email()
    email.body_html = "<p>This shipment is cancelled, void, please disregard.</p>" + email.body_html
    email.body_text = "This shipment is cancelled, void, please disregard."
    result = triage(email)
    assert result.notification_type == NotificationType.ORDER_CANCELLATION
    assert result.category == TriageCategory.ROUTE


def test_tracker_attachment_is_held_so_extraction_can_read_it():
    """Two corpus threads carry no PO anywhere in their text because the POs are in an attached
    spreadsheet. Falling through to the unknown rule sends a machine-readable file to a human."""
    import io

    import openpyxl
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Vendor", "PO#", "Spec#", "QTY", "Item Description", "Tracking",
                  "Delivery Date", "Confirmed Received: Yes or No"])
    sheet.append(["Daniel Stuart", "207030", "LOB-203-PI", 12, '18"x18" Throw Pillow',
                  "FedEx 476858924781", "2025-09-22", "yes"])
    buffer = io.BytesIO()
    workbook.save(buffer)

    email = make_email(
        sender_address="ivopavlov@premierpm.com", sender_domain="premierpm.com",
        subject="Fw: Cameo Public Space pending property receipt confirmation",
        body_text="Please see attached. I have marked with yes the ones I am certain were delivered.",
        attachments=[Attachment(filename="Cameo Receivers.xlsx", content_type="",
                                content_bytes=buffer.getvalue())],
    )
    result = triage(email)
    assert result.category == TriageCategory.HOLD
    assert result.matched_rule == "rule_5a_tracker_attachment"


def test_photo_only_evidence_is_routed_with_a_reason_naming_ocr():
    """A 3 MB pallet photo is the POD. It must reach a person with an explanation, not be
    dismissed as "no PO found"."""
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * (700 * 1024)
    email = make_email(
        sender_address="johngallo@premierpm.com", sender_domain="premierpm.com",
        subject="Fw: Cameo Harbour Delivery",
        body_text="Attached are the BOL and Packing slips. Pallet missing for delivery to property.",
        attachments=[Attachment(filename="IMG_2479.jpeg", content_type="", content_bytes=jpeg)],
    )
    result = triage(email)
    assert result.category == TriageCategory.ROUTE
    assert result.matched_rule == "rule_5b_image_only_evidence"
    assert "OCR" in result.reason


def test_no_po_reference_anywhere_is_routed():
    email = make_email(
        sender_address="marketing@wayfair.com", sender_domain="wayfair.com",
        subject="It's delivery day!",
        body_text="Great news - your order is arriving today.",
    )
    result = triage(email)
    assert result.notification_type == NotificationType.UNKNOWN
    assert result.category == TriageCategory.ROUTE
    assert result.extracted_po_hints == []


# --- Fixtures on disk -------------------------------------------------------


def test_local_folder_mailbox_reads_fixture_emails():
    mailbox = LocalFolderMailbox(FIXTURES_DIR)
    emails = mailbox.fetch_new()
    ids = {e.email_id for e in emails}
    assert "warehouse_inbound_239336" in ids
    assert "wayfair_no_po" in ids

    inbound = next(e for e in emails if e.email_id == "warehouse_inbound_239336")
    result = triage(inbound)
    assert result.notification_type == NotificationType.WAREHOUSE_INBOUND
    assert result.extracted_po_hints == ["208491"]


def test_idempotency_second_call_with_same_id_is_skipped():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE seen_message_ids (email_id TEXT PRIMARY KEY, seen_at TEXT NOT NULL)")
    assert state_db.is_new_message(conn, "msg-dup-1", "2026-06-08T14:00:00Z") is True
    assert state_db.is_new_message(conn, "msg-dup-1", "2026-06-08T14:05:00Z") is False
