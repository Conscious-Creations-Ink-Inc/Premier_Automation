"""Stage 1 triage, against corpus-shaped mail (see tests/corpus_fixtures.py)."""

import sqlite3
from pathlib import Path

from connectors.mailbox import LocalFolderMailbox
from pipeline import state_db
from pipeline.models import Attachment, NotificationType, RawEmail, TriageCategory
from pipeline.parsing import text
from pipeline import stage1_triage
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
    assert result.extracted_po_hints == ["908491"]


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
    """Notice 939336 exists in the corpus twice: once delivered straight from Authority, once
    forwarded by an expeditor. Rules keyed on the envelope sender see `example-pm.test` on the
    second one and get it wrong."""
    direct = triage(fx.inbound_email(email_id="direct", forwarded=False))
    forwarded = triage(fx.inbound_email(email_id="forwarded", forwarded=True))

    assert forwarded.email.sender_domain == "example-pm.test"
    assert forwarded.origin_sender_address == fx.WAREHOUSING
    for field in ("category", "notification_type", "matched_rule", "extracted_po_hints",
                  "extracted_shipment_hint", "notification_number"):
        assert getattr(direct, field) == getattr(forwarded, field), field


def test_delivered_and_inbound_share_one_shipment_key():
    """`Authority #` on a Delivered notice is `ALS Shipment #` on the matching Inbound. That
    equality is the join Stage 2 needs to fire once for one physical delivery."""
    delivered = triage(fx.delivered_email(notice="90009", po_numbers=("906725",)))
    inbound = triage(fx.inbound_email(notice="939260", po_numbers=("906725",), shipment="90009 : 1"))
    assert delivered.extracted_shipment_hint == inbound.extracted_shipment_hint == "90009"


def test_inbound_with_blank_shipment_number_reports_none_not_the_inbound_number():
    """Corpus notice 939475 leaves `ALS Shipment #` empty. Substituting the inbound number would
    look like a key but join to nothing, silently splitting one delivery in two."""
    result = triage(fx.inbound_email(notice="939475", shipment=""))
    assert result.extracted_shipment_hint is None
    assert result.notification_number == "939475"


def test_multi_po_inbound_captures_every_po_from_the_subject_slot():
    email = fx.inbound_email(
        notice="939260", po_numbers=("906725", "907665"),
        lines=[
            {"po": "906725", "line": "1", "part": "EXT-925-AC",
             "item": '2 EACH - EXT-925-AC-Linear Planter w/Pocket(s) 96"Lx30"Wx24"'},
            {"po": "907665", "line": "1", "part": "POOL-925-AC", "item": "3 EA - POOL-925-AC Linear Planter"},
        ],
    )
    result = triage(email)
    assert result.category == TriageCategory.SURFACE
    assert result.extracted_po_hints == ["906725", "907665"]


def test_inbound_number_is_never_mistaken_for_a_po():
    """Inbound numbers are six digits too — 939336 sits in the same subject as PO 908491. A bare
    `\\d{6}` scan returns both."""
    result = triage(fx.inbound_email(notice="939336", po_numbers=("908491",)))
    assert "939336" not in result.extracted_po_hints


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
    assert result.extracted_po_hints == ["910634", "910635"]


def test_property_reply_is_held():
    email = make_email(
        sender_address="joshuasoto@example-hotels.test", sender_domain="example-hotels.test",
        subject="RE: Example Hotel Public Space Tilesupply PO 912448",
        body_text="Hi, yes we received all items for PO 912448 in good condition. Thanks!",
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
        sender_address="joshuasoto@example-hotels.test", sender_domain="example-hotels.test",
        subject="RE: PO 912448",
        body_text="Yes ma'am, this was received!\n\n" + ("\n\n".join([notice] * 6)),
    )
    result = triage(email)
    assert result.category == TriageCategory.HOLD
    assert result.matched_rule == "rule_4_property_reply"


def test_loss_or_claim_thread_is_routed_to_a_person():
    email = make_email(
        sender_address="msmith@example-logistics.test", sender_domain="example-logistics.test",
        subject="RE: Example Hotel Public Space",
        body_text=("RES-100b-EQ for PO #908453 was lost by the warehouse. "
                   "The new PO for the replacement is PO #911400."),
    )
    result = triage(email)
    assert result.notification_type == NotificationType.LOSS_OR_CLAIM
    assert result.category == TriageCategory.ROUTE
    assert result.matched_rule == "rule_0b_loss_or_claim"


def test_cancellation_is_routed_regardless_of_sender():
    email = make_email(
        sender_address=fx.WAREHOUSING, sender_domain="example-logistics.test",
        subject="Please cancel PO 913987 - no longer required",
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
    sheet.append(["Daniel Stuart", "907030", "LOB-203-PI", 12, '18"x18" Throw Pillow',
                  "FedEx 476858924781", "2025-09-22", "yes"])
    buffer = io.BytesIO()
    workbook.save(buffer)

    # A real `Fw:` carries the quoted header the property sent, which is what makes the origin
    # external and this mail evidence. Without it the authorship override would hide the message —
    # correctly, since then nothing but Premier's own envelope says anything about it. The test
    # below pins that other half.
    email = make_email(
        sender_address="ivopavlov@example-pm.test", sender_domain="example-pm.test",
        subject="Fw: Example Hotel Public Space pending property receipt confirmation",
        body_text=("Please see attached. I have marked with yes the ones I am certain were "
                   "delivered.\n"
                   "\nFrom: receiving@example-hotels.test"
                   "\nSent: Monday, September 22, 2025 9:14 AM"
                   "\nTo: Pavlov, Ivo <ivopavlov@example-pm.test>"
                   "\nSubject: pending property receipt confirmation\n"
                   "\nOur marked-up copy is attached.\n"),
        attachments=[Attachment(filename="Property Receivers.xlsx", content_type="",
                                content_bytes=buffer.getvalue())],
    )
    result = triage(email)
    assert result.category == TriageCategory.HOLD
    assert result.matched_rule == "rule_5a_tracker_attachment"


def test_the_same_tracker_written_inside_premier_is_hidden():
    """The authorship override, on the one rule most likely to be affected by it.

    `rule_5a_tracker_attachment` holds mail so extraction can read the spreadsheet. That is right
    when a property sent it and wrong when Premier wrote it: a sheet Premier filled in about its own
    orders states what Premier believes, not what arrived. 47 messages in the store matched this
    rule internally.
    """
    import io

    import openpyxl
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Vendor", "PO#", "Spec#", "QTY", "Item Description", "Tracking",
                  "Delivery Date", "Confirmed Received: Yes or No"])
    sheet.append(["Daniel Stuart", "907030", "LOB-203-PI", 12, "Throw Pillow",
                  "FedEx 476858924781", "2025-09-22", "yes"])
    buffer = io.BytesIO()
    workbook.save(buffer)

    email = make_email(
        sender_address="ivopavlov@example-pm.test", sender_domain="example-pm.test",
        subject="Public Space pending property receipt confirmation",
        body_text="Attached is where I think we are.",
        attachments=[Attachment(filename="Property Receivers.xlsx", content_type="",
                                content_bytes=buffer.getvalue())],
    )
    result = triage(email)
    assert result.category == TriageCategory.HIDE
    assert result.matched_rule == stage1_triage.INTERNALLY_AUTHORED
    assert result.not_a_delivery
    # The rule that recognised it is kept, so the row still says what it was before authorship
    # overruled it.
    assert "rule_5a_tracker_attachment" in result.reason


def test_photo_only_evidence_is_routed_with_a_reason_naming_ocr():
    """A 3 MB pallet photo is the POD. It must reach a person with an explanation, not be
    dismissed as "no PO found"."""
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * (700 * 1024)
    email = make_email(
        sender_address="johngallo@example-pm.test", sender_domain="example-pm.test",
        subject="Fw: Example Hotel Harbour Delivery",
        body_text="Attached are the BOL and Packing slips. Pallet missing for delivery to property.",
        attachments=[Attachment(filename="IMG_2479.jpeg", content_type="", content_bytes=jpeg)],
    )
    result = triage(email)
    assert result.category == TriageCategory.ROUTE
    assert result.matched_rule == "rule_5b_image_only_evidence"
    assert "OCR" in result.reason


def test_no_po_reference_anywhere_is_routed():
    email = make_email(
        sender_address="marketing@example-retail.test", sender_domain="example-retail.test",
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
    assert "retailer_no_po" in ids

    inbound = next(e for e in emails if e.email_id == "warehouse_inbound_239336")
    result = triage(inbound)
    assert result.notification_type == NotificationType.WAREHOUSE_INBOUND
    assert result.extracted_po_hints == ["908491"]


def test_idempotency_second_call_with_same_id_is_skipped():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE seen_message_ids (email_id TEXT PRIMARY KEY, seen_at TEXT NOT NULL)")
    assert state_db.is_new_message(conn, "msg-dup-1", "2026-06-08T14:00:00Z") is True
    assert state_db.is_new_message(conn, "msg-dup-1", "2026-06-08T14:05:00Z") is False


# --- Rule 5c: internal chatter -----------------------------------------------
# Twelve of the fourteen emails in Premier's live manual queue were all-associates broadcasts and
# calendar invites, every one of them reading "no PO reference found anywhere in the thread".
# True, and useless — they buried the two entries that were real work.

def broadcast(**overrides):
    defaults = dict(
        email_id="msg-broadcast",
        sender_address="LaurieChapman@example-pm.test",
        sender_domain="example-pm.test",
        subject="Premier Monthly Celebration",
        body_html="<p>Join us Friday in the Dallas office. Cake at 3pm.</p>",
    )
    defaults.update(overrides)
    return make_email(**defaults)


def test_an_internal_broadcast_is_hidden_not_queued():
    result = triage(broadcast())
    assert result.category == TriageCategory.HIDE
    assert result.matched_rule == "rule_5c_internal_noise"


def test_a_calendar_invite_is_hidden():
    assert triage(broadcast(subject="Canceled: Voting Holiday Reward")).category == TriageCategory.HIDE


def test_signature_logos_already_dropped_at_ingest_do_not_block_the_rule():
    """A broadcast drags along five copies of the Premier signature logo. The connector drops
    them before triage — `drop_hint` set, bytes cleared — so they must not read as attachments
    worth keeping the mail in the queue for."""
    email = broadcast()
    email.attachments = [Attachment(filename=f"image00{i}.png", content_type="image/png",
                                    content_bytes=b"", content_id=f"image00{i}.png",
                                    is_inline=True, drop_hint="decorative:known_hash")
                         for i in range(1, 6)]
    assert triage(email).matched_rule == "rule_5c_internal_noise"


def test_a_broadcast_that_happens_to_use_delivery_words_is_left_alone():
    """The rule will not hide anything whose text could be about a delivery, even when every
    other signal says chatter. `You've joined the Premier PM All Associates group` is a live
    example: it matched delivery vocabulary at triage and so stays a person's problem."""
    result = triage(broadcast(subject="You've joined the Premier PM All Associates group",
                              body_html="<p>Please confirm your membership.</p>"))
    assert result.matched_rule != "rule_5c_internal_noise"


# The other direction matters more: a delivery hidden is a receiver nobody creates. Which is why
# what survives the authorship override is chosen by hand — see `AUTHORSHIP_YIELDS_TO` — rather
# than by whether the mail happens to mention a purchase order.

def test_internal_mail_naming_a_po_is_hidden_by_authorship():
    """This reverses a deliberate choice, so it is worth saying which one and why.

    A PO in the subject used to be enough to keep internal mail on the queue, on the reasoning that
    a hidden delivery is a receiver nobody creates. Measured since: naming a PO is what Premier's
    own expediting reports, inventory sheets and "has this landed yet" chasers all do, and 846 of
    the 4,155 rows on the manual queue were internally-authored mail standing for 3,427 records.
    None of it said goods arrived; none of it has ever produced a posted receipt.

    So a PO no longer earns an exemption. Real work still does — the cases below.
    """
    result = triage(broadcast(subject="Quick question on PO 912448"))
    assert result.category == TriageCategory.HIDE
    assert result.matched_rule == stage1_triage.INTERNALLY_AUTHORED
    assert result.not_a_delivery


def test_internal_mail_with_delivery_vocabulary_is_hidden_by_authorship():
    """Same reversal. Delivery vocabulary in internal mail is Premier asking, not Premier telling.

    Note what this fixture is *not*: a genuine `Fw:` carries the quoted header of whoever wrote it,
    which makes the origin external and the mail evidence again. `broadcast()` has no quoted header,
    so Premier's envelope is the only authorship there is — and the 117 messages in the store that
    are internal by envelope while authored outside are exactly the ones this must not touch.
    """
    result = triage(broadcast(subject="Fw: Example Hotel Harbour Delivery",
                              body_html="<p>Photos of the pallet attached, please confirm receipt.</p>"))
    assert result.category == TriageCategory.HIDE
    assert result.matched_rule == stage1_triage.INTERNALLY_AUTHORED


def test_internal_mail_carrying_a_photograph_is_never_hidden():
    email = broadcast(subject="Fw: Example Hotel Harbour Delivery",
                      body_html="<p>Delivered this morning, see photos.</p>")
    email.attachments = [Attachment(filename="IMG_2479.jpeg", content_type="image/jpeg",
                                    content_bytes=b"\xff\xd8\xff" + b"\x00" * (700 * 1024))]
    assert triage(email).category != TriageCategory.HIDE


def test_external_mail_is_never_hidden_by_this_rule():
    """A vendor with nothing recognisable in it is still a person's problem, not chatter."""
    result = triage(broadcast(sender_address="someone@example-interiors.test",
                              sender_domain="example-interiors.test",
                              subject="Hello"))
    assert result.matched_rule != "rule_5c_internal_noise"


def test_internal_mail_carrying_a_readable_attachment_is_never_hidden():
    email = broadcast(subject="Updated list")
    email.attachments = [Attachment(filename="tracker.pdf", content_type="application/pdf",
                                    content_bytes=b"%PDF-1.4" + b"\x00" * 500)]
    assert triage(email).matched_rule != "rule_5c_internal_noise"


# --- Rule 5e: advertising and bulk mail --------------------------------------
# 5c's external twin. Measured on Premier's live store: of the 452 messages reaching the queue as
# `rule_7_unknown`, 53 came from bulk-mail subdomains — members.wayfair.com alone sent 27 — and 5c
# can never touch them because it is gated on `_is_internal`.
#
# The signal is the opt-out block commercial mail is obliged to carry. The guards are 5c's, with one
# deliberate difference that the tests below pin: the last guard asks whether any hop *claims* goods
# arrived, not whether the thread mentions delivery. Marketing mail mentions delivery constantly.

UNSUB = ('<p><a href="https://links.example-retail.test/u/unsubscribe?id=9">'
         'Unsubscribe</a></p>')


def bulk_mail(**overrides):
    defaults = dict(
        email_id="msg-bulk",
        sender_address="deals@members.example-retail.test",
        sender_domain="members.example-retail.test",
        subject="Up to 60% off dining, this weekend only",
        body_html="<p>Our biggest sale of the season.</p>" + UNSUB,
    )
    defaults.update(overrides)
    return make_email(**defaults)


def test_advertising_with_an_opt_out_block_is_hidden():
    result = triage(bulk_mail())
    assert result.category == TriageCategory.HIDE
    assert result.matched_rule == "rule_5e_bulk_mail_noise"
    assert result.not_a_delivery is True


def test_the_opt_out_link_is_found_in_the_href_when_the_anchor_text_hides_it():
    """Why the rule reads `body_html` and not the rendered body, asserted rather than commented.

    `html_to_text` runs `soup.get_text()` and drops every href, so on this message the rendered text
    is the words "Click here" and nothing else — the opt-out mechanism is the link, and every other
    rule in the module is blind to it.
    """
    html = ('<p>Half price this week.</p>'
            '<p><a href="https://links.example-retail.test/optout/abc">Click here</a></p>')
    assert "opt" not in text.html_to_text(html).lower(), "the rendered body must not carry it"

    assert triage(bulk_mail(body_html=html)).matched_rule == "rule_5e_bulk_mail_noise"


def test_a_manage_preferences_footer_is_enough():
    result = triage(bulk_mail(
        body_html="<p>New season lookbook.</p><p>Manage your email preferences</p>"))
    assert result.matched_rule == "rule_5e_bulk_mail_noise"


def test_the_reason_quotes_the_phrase_that_fired():
    """A person reading the row should see the evidence, not the name of the heuristic."""
    assert "unsubscribe" in triage(bulk_mail()).reason.lower()


def test_the_rule_is_a_property_of_the_message_not_a_sender_list():
    """The same body from a bulk subdomain and from an ordinary address both hide. A brand list
    stops working the moment next month's marketing arrives from somewhere else."""
    from_anywhere = triage(bulk_mail(sender_address="someone@example.com",
                                     sender_domain="example.com"))
    assert from_anywhere.matched_rule == "rule_5e_bulk_mail_noise"


def test_a_sale_email_that_merely_mentions_delivery_is_still_hidden():
    """The counter-counter-test. An earlier draft guarded on `is_delivery_topic`, which matches 26
    words including *delivery* and *tracking* — so "free delivery" in a sale email spared it, and the
    rule caught 5 of 33 live messages instead of 11. Mentioning a delivery is not claiming one."""
    result = triage(bulk_mail(subject="Free delivery on everything this weekend",
                              body_html="<p>Free delivery and free returns. Track your order "
                                        "any time.</p>" + UNSUB))
    assert result.matched_rule == "rule_5e_bulk_mail_noise"


# The other direction matters more: a delivery hidden is a receiver nobody creates.

def test_ordinary_external_mail_is_still_routed():
    """The most important test in this section. 202 of the 453 messages on the live queue are
    ordinary external people — real business mail with nothing promotional about it. Every one must
    reach a person, and none of them may be badged as even *possibly* advertising."""
    result = triage(bulk_mail(sender_address="dominique@lighting.test",
                              sender_domain="lighting.test",
                              subject="Shop drawings for the guestroom sconce",
                              body_html="<p>Can you send the revised drawing?</p>"))
    assert result.category == TriageCategory.ROUTE
    assert result.matched_rule == "rule_7_unknown"


def test_a_delivery_claim_from_a_sender_that_also_markets_is_never_hidden():
    """The guard the rule turns on. A vendor mailing from a marketing platform still carries the
    footer on its genuine delivery mail, and that mail says the goods arrived."""
    result = triage(bulk_mail(
        subject="Your order was delivered",
        body_html="<p>Your order was delivered on Tuesday and signed for at the dock.</p>" + UNSUB))
    assert result.matched_rule != "rule_5e_bulk_mail_noise"
    assert result.category != TriageCategory.HIDE


def test_a_promotion_naming_a_purchase_order_is_never_hidden():
    result = triage(bulk_mail(
        body_html="<p>A note regarding PO 912448 and our autumn range.</p>" + UNSUB))
    assert result.matched_rule != "rule_5e_bulk_mail_noise"
    assert result.category != TriageCategory.HIDE


def test_a_tracker_attachment_outranks_the_opt_out_footer():
    """The one message in Premier's store that produced a record *and* carries an opt-out block:
    a freight forwarder whose spreadsheet holds the only PO. Rule 5a runs first and must keep it."""
    email = bulk_mail(subject="Sheraton Anchorage - pallet count",
                      body_html="<p>Receiving detail attached.</p>" + UNSUB)
    email.attachments = [Attachment(filename="Property Receivers.xlsx",
                                    content_type="application/vnd.ms-excel",
                                    content_bytes=b"PK\x03\x04" + b"\x00" * 500)]
    assert triage(email).matched_rule != "rule_5e_bulk_mail_noise"


def test_a_photographed_proof_with_a_marketing_footer_is_never_hidden():
    email = bulk_mail(subject="Delivered to the property",
                      body_html="<p>Photo of the signed slip attached.</p>" + UNSUB)
    email.attachments = [Attachment(filename="pod.jpg", content_type="image/jpeg",
                                    content_bytes=b"\xff\xd8\xff\xe0" + b"\x00" * 60000)]
    assert triage(email).matched_rule != "rule_5e_bulk_mail_noise"


def test_internal_mail_with_an_opt_out_footer_stays_the_internal_rule():
    """Pins the ladder order, and why 5e carries no redundant `not _is_internal` guard: anything
    internal that satisfies 5e's conditions satisfies 5c's, and 5c runs first."""
    result = triage(broadcast(
        body_html="<p>Join us Friday in the Dallas office.</p>"
                  "<p>Manage your email preferences</p>"))
    assert result.matched_rule == "rule_5c_internal_noise"


def test_a_denial_keeps_the_more_specific_rule():
    """Pins the 5d/5e order. A footer reading "you subscribed" already routes to 5d, and the more
    specific rule keeps its mail."""
    result = triage(bulk_mail(
        subject="Re: the lobby chairs",
        body_html="<p>We have not received this yet.</p>" + UNSUB))
    assert result.matched_rule == "rule_5d_no_delivery_claim"


def test_cancel_your_subscription_is_still_read_as_a_cancellation():
    """Records in code that this change deliberately does NOT fix the false cancellations a
    marketing footer causes. That is a separate defect with a separate fix — requiring a PO before
    rule_0a can fire — and narrowing rule_0a risks hiding a real cancellation, which is work."""
    result = triage(bulk_mail(body_html="<p>Sale ends soon.</p>"
                                        "<p>You may cancel your subscription at any time.</p>"))
    assert result.matched_rule == "rule_0a_order_cancellation"


def test_a_client_preference_is_not_a_preferences_footer():
    """"per the client's preferences" is the guestroom fabric, not an unsubscribe link. The regex
    negatives are `tests/test_promotional.py`'s to own; this asserts the message-level outcome."""
    result = triage(bulk_mail(
        sender_address="anna@fabrics.test", sender_domain="fabrics.test",
        subject="Swatch selection for the guestroom",
        body_html="<p>Swatches chosen per the client's preferences for the guestroom fabric.</p>"))
    assert result.matched_rule != "rule_5e_bulk_mail_noise"
    assert result.category != TriageCategory.HIDE


def test_a_promotional_subject_naming_a_property_is_badged_not_hidden():
    """The MAYBE band. Promotional in shape, but it names a Premier property — so it goes to a
    person carrying the reason, rather than being filed away on the strength of the sale words."""
    result = triage(bulk_mail(subject="70% off clearance for Sheraton Anchorage",
                              body_html="<p>Sale ends soon.</p>"))
    assert result.matched_rule == "rule_5f_possible_advertising"
    assert result.category == TriageCategory.ROUTE
    assert result.not_a_delivery is False, "MAYBE is a request to look, not a verdict"


def test_a_reply_is_never_advertising():
    """The strongest signal measured on the live store: 0% of adverts are replies, 84.6% of genuine
    delivery mail is. Premier's receiving happens in conversations; advertising arrives cold."""
    result = triage(bulk_mail(subject="RE: Up to 60% off dining, this weekend only"))
    assert result.matched_rule != "rule_5e_bulk_mail_noise"
    assert result.category != TriageCategory.HIDE


def test_these_rules_can_only_take_mail_the_catch_all_would_have_taken():
    """The blast-radius claim, in code. Everything above 5e/5f matches first, and rule 6 requires a
    PO that both refuse to fire with — so the entire population either can take is a subset of
    `rule_7_unknown`. Nothing else in the store can move.

    Asserted by disabling the lexicon rather than by reasoning about the ladder: with `classify`
    returning NEITHER, every one of these messages must fall to the catch-all it came from.
    """
    from pipeline.parsing import promotional
    from pipeline import stage1_triage

    cases = (bulk_mail(),
             bulk_mail(subject="Free delivery this weekend"),
             bulk_mail(sender_address="x@example.com", sender_domain="example.com"),
             bulk_mail(subject="70% off clearance for Sheraton Anchorage"))

    taken = {triage(email).matched_rule for email in cases}
    assert taken <= {"rule_5e_bulk_mail_noise", "rule_5f_possible_advertising"}

    real = stage1_triage.promotional.classify
    stage1_triage.promotional.classify = lambda m: promotional.Verdict(
        band=promotional.NEITHER, promo=0, business=0)
    try:
        for email in cases:
            assert triage(email).matched_rule == "rule_7_unknown"
    finally:
        stage1_triage.promotional.classify = real


# --- The notice is found wherever the chain quoted it ------------------------
#
# The outermost subject is the one part of a forwarded notice a human retypes. Three live sends to
# Premier's receiving mailbox arrived as "Testing Premier Automation", then "Test 3", each carrying
# a perfectly formed Inbound Notification one hop down that nothing ever looked at. Premier's own
# mail has the same shape for duller reasons: always forwarded, sometimes twice, and Exchange
# rewrites the subject on the way in.


def _notice_under_an_unrelated_subject(subject: str) -> RawEmail:
    """A real Inbound Notification, forwarded by someone outside Premier who retyped the subject."""
    email = fx.inbound_email(forwarded=True)
    email.subject = subject
    email.sender_address = "someone@outlook.com"
    email.sender_domain = "outlook.com"
    return email


def test_a_retyped_subject_does_not_hide_the_notice_quoted_below_it():
    result = triage(_notice_under_an_unrelated_subject("[External] Test 3"))
    assert result.matched_rule == "rule_1a_authority_inbound"
    assert result.category == TriageCategory.SURFACE
    assert result.extracted_po_hints == ["908491"]
    # The shipment number is what stops one delivery becoming two receivers, and it only exists
    # when the notice itself was parsed — its absence is how the failure showed up in production.
    assert result.extracted_shipment_hint == "90052"
    assert result.notification_number == "939336"


def test_the_outer_subject_still_wins_when_it_is_itself_a_notice():
    """The resolved origin is tried first, so nothing that worked before resolves differently."""
    result = triage(fx.inbound_email(forwarded=True))
    assert result.matched_rule == "rule_1a_authority_inbound"
    assert result.notification_number == "939336"


def test_a_thread_with_no_authority_hop_is_unchanged():
    """The fallback must not invent a notice. A plain vendor thread triages exactly as before."""
    email = make_email(
        subject="[External] Test 3",
        sender_address="someone@outlook.com", sender_domain="outlook.com",
        body_html="<p>Quick question about the pool pavers, no PO to hand.</p>")
    assert triage(email).matched_rule != "rule_1a_authority_inbound"


# --- Not a delivery mail ----------------------------------------------------

def report_mail(**overrides):
    """The 2am scheduled report, in the shape that cost 126 records."""
    body = ("<table><tr><th>PO</th><th>Vendor</th></tr>"
            + "".join(f"<tr><td>{200000 + i}</td><td>Acme</td></tr>" for i in range(130))
            + "</table>")
    defaults = dict(
        sender_address="Reports@example-pm.test", sender_domain="example-pm.test",
        subject="4-Pending Pay Requests (includes In-Process) was executed at 8/27/2026 2:00:10 AM",
        body_html=body,
    )
    defaults.update(overrides)
    return make_email(**defaults)


def test_a_scheduled_report_is_not_a_delivery_and_stages_nothing():
    """One arrival of this report staged **126 records** — 36% of the whole store — and 126 phantom
    deliveries, because it lists 130 POs and `rule_4_property_reply` asks for nothing more than a PO
    and a short newest hop. It arrives every morning at 2am.
    """
    result = triage(report_mail())
    assert result.matched_rule == "rule_0c_report_sender"
    assert result.category == TriageCategory.HIDE, "HIDE is what stops accumulation"
    assert result.not_a_delivery is True


def test_the_report_rule_beats_the_cancellation_rule():
    """Report subjects carry cancellation vocabulary as a matter of course. `CANCELLATION_RE` runs
    before almost everything, so a `Cancelled POs` report would be routed to a person every morning
    unless the sender rule sits ahead of it."""
    result = triage(report_mail(
        subject="7-Cancelled POs was executed at 8/27/2026 2:00:10 AM"))
    assert result.matched_rule == "rule_0c_report_sender"


def test_a_real_cancellation_from_a_person_is_still_routed():
    """The counter-test. `rule_0c` is safe at the top of the ladder *only* because it is keyed on a
    mailbox that sends nothing but reports — it must not swallow a human's cancellation."""
    result = triage(make_email(
        sender_address="maryvaughn@example-pm.test", sender_domain="example-pm.test",
        subject="PO 908491 has been cancelled",
        body_text="Please cancel PO 908491, the order is no longer required."))
    assert result.matched_rule == "rule_0a_order_cancellation"
    assert result.category == TriageCategory.ROUTE
    assert result.not_a_delivery is False, "a cancellation is work, not something to file away"


def test_a_confirmation_of_something_other_than_goods_is_not_a_delivery():
    """"I am confirming receipt of check 1000781" is a real corpus line. It is full of delivery
    vocabulary and confirms a payment."""
    result = triage(make_email(
        sender_address="someone@example.com", sender_domain="example.com",
        subject="RE: outstanding balance",
        body_text="I am confirming receipt of check 1000781 for $87,663.85. Thank you."))
    assert result.matched_rule == "rule_5d_no_delivery_claim"
    assert result.category == TriageCategory.HIDE
    assert result.not_a_delivery is True


def test_a_denial_on_a_thread_that_names_a_po_is_held_not_hidden():
    """The counter-test, and the reason `rule_5d` sits below rule 4 rather than above it.

    "We have not received this yet" on a thread carrying a request grid is not a whole-email
    verdict: the same thread routinely confirms some lines and denies others — *"we can confirm we
    have only received the Sheer Fabric"* is one corpus sentence doing both. Hiding the message
    would discard the confirmations along with the denial.

    `parsing/confirmation.py` already resolves this **per spec code** — `denies()` refuses exactly
    the lines the thread refused — which is strictly more precise than anything triage could decide
    about the message as a whole. So triage holds it and lets that run.
    """
    result = triage(make_email(
        sender_address="someone@example.com", sender_domain="example.com",
        subject="RE: PO 908491",
        body_text="We have not received this yet, nothing has arrived at the property."))
    assert result.matched_rule == "rule_4_property_reply"
    assert result.not_a_delivery is False


def test_absence_of_a_delivery_claim_is_never_enough_to_hide():
    """`Intent.NEITHER` means no hop said anything either way — absence of evidence, not evidence of
    absence. Treating it as a not-a-delivery finding hid a photographed POD, a tracker attachment
    carrying the only PO, and the whole `rule_7_unknown` bucket, which exists to say *we could not
    tell* and hand the mail to a person."""
    result = triage(make_email(
        sender_address="someone@example.com", sender_domain="example.com",
        subject="PO 908491", body_text="See below."))
    assert result.matched_rule != "rule_5d_no_delivery_claim"
    assert result.category != TriageCategory.HIDE
    assert result.not_a_delivery is False


def test_the_rules_that_mean_not_a_delivery_are_the_ones_that_set_the_flag():
    """The flag is derived from `NOT_A_DELIVERY_RULES`, never passed at a call site, so a rule
    cannot claim it in one branch and forget it in another."""
    from pipeline import stage1_triage

    assert "rule_0a_order_cancellation" not in stage1_triage.NOT_A_DELIVERY_RULES
    assert "rule_0b_loss_or_claim" not in stage1_triage.NOT_A_DELIVERY_RULES
    assert "rule_7_unknown" not in stage1_triage.NOT_A_DELIVERY_RULES
    for rule in ("rule_0c_report_sender", "rule_5d_no_delivery_claim",
                 "rule_5c_internal_noise", "rule_2_freight_status",
                 "rule_1c_authority_status_report", "rule_5e_bulk_mail_noise"):
        assert rule in stage1_triage.NOT_A_DELIVERY_RULES


# --- what survives the authorship override ----------------------------------------------------
#
# Each of these is internally authored and each stays visible, because the rule that recognised it
# identifies work rather than chatter. They are the reason `AUTHORSHIP_YIELDS_TO` is a hand-picked
# list: "internal mail is never a delivery" is true, and "internal mail never needs a person" is
# not.


def test_an_internal_cancellation_still_reaches_a_person():
    """A cancelled order needs a PO updated in Spitfire whoever wrote the mail."""
    result = triage(broadcast(subject="Cancelled: PO 912448 - please void",
                              body_html="<p>We have cancelled this order with the vendor.</p>"))
    assert result.category == TriageCategory.ROUTE
    assert result.matched_rule == "rule_0a_order_cancellation"
    assert not result.not_a_delivery


def test_an_internal_loss_or_claim_still_reaches_a_person():
    result = triage(broadcast(subject="Damaged in transit - filing a claim on PO 912448",
                              body_html="<p>Two cartons arrived crushed, raising a claim.</p>"))
    assert result.category == TriageCategory.ROUTE
    assert result.matched_rule == "rule_0b_loss_or_claim"
    assert not result.not_a_delivery


def test_an_internal_message_whose_attachment_is_a_pod_is_not_hidden():
    """`evidence.has_pod` means a reader recognised a carrier proof of delivery in the file.

    No internally-authored message in the store carries one today, which is most of why this
    override is safe — but "none so far" is not a rule, and the day one arrives it must not be
    filed under "no action needed".
    """
    class _Evidence:
        has_pod = True
        po_numbers = ()
        ocr_attempted = 0

    result = triage(broadcast(subject="POD for PO 912448"), evidence=_Evidence())
    assert result.category != TriageCategory.HIDE
    assert result.matched_rule != stage1_triage.INTERNALLY_AUTHORED


# --- a question with nothing to receive against (Premier, 2026-09-15) -------------------------
#
# 496 of these were the second-largest thing on the manual queue. Premier's rule: a mail asking
# whether goods arrived is not delivery mail *unless* the thread or a reply carries a purchase
# order or a delivery.

ASKING = "<p>Could you please confirm whether these items were received at the property?</p>"


def test_a_question_naming_no_purchase_order_is_not_delivery_mail():
    result = triage(make_email(subject="Re: Duluth model room", body_html=ASKING))
    assert result.category == TriageCategory.HIDE
    assert result.matched_rule == "rule_2a_verification_no_reference"
    assert result.not_a_delivery, "it must reach the Not deliveries page, not vanish"


def test_the_same_question_stays_on_the_queue_when_the_thread_names_a_purchase_order():
    """The guard. A question about a real order is work, and answering it is somebody's job."""
    result = triage(make_email(subject="Re: PO 908491 Duluth model room", body_html=ASKING))
    assert result.category == TriageCategory.ROUTE
    assert result.matched_rule == "rule_2a_verification_request"
    assert not result.not_a_delivery


def test_a_question_carrying_an_attachment_is_never_set_aside_unread():
    """The file may hold the only PO reference. Deciding on it unread is deciding on nothing."""
    result = triage(make_email(
        subject="Re: Duluth model room", body_html=ASKING,
        attachments=[Attachment(filename="Receiving list.xlsx", content_bytes=b"PK\x03\x04data",
                                content_type="application/vnd.ms-excel")]))
    assert result.category == TriageCategory.ROUTE
    assert result.matched_rule == "rule_2a_verification_request"
