"""The shared parsing primitives.

Every case here is a literal string from the real June corpus. Where a case asserts that
something is *not* matched, the false positive it guards against was one the earlier regexes
actually produced.
"""

import pytest

from pipeline.parsing import boilerplate, confirmation, pod, sniff, tables, text, thread, tokens


# --- PO numbers -------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("PO 913987", ["913987"]),
    ("PO# 913987", ["913987"]),
    ("PO #913987", ["913987"]),
    ("P.O.# 910634", ["910634"]),
    ("PO: 913987", ["913987"]),
    ("po 913987", ["913987"]),                       # case-insensitive — the old regex was not
    ("Purchase Order 910634", ["910634"]),
    ("PO 911310 + PO 912578", ["911310", "912578"]),
    ("PO 907505, 907514, 912559", ["907505", "907514", "912559"]),
])
def test_labelled_po_forms(raw, expected):
    assert tokens.find_po_numbers(raw) == expected


def test_bare_six_digit_numbers_are_not_treated_as_pos():
    """An Authority subject holds the inbound number, two POs and a project number, three of
    them six digits. Only position tells them apart, so the free-text scan claims none of them."""
    subject = "939260 - Inbound Notification - 906725, 907665 - 9078 : Example Hotel Downtown"
    assert tokens.find_po_numbers(subject) == []


def test_po_list_reads_an_isolated_slot():
    assert tokens.parse_po_list("906725, 907665") == ["906725", "907665"]


def test_po_line_ref():
    reference = tokens.parse_po_line_ref("908491 : 300")
    assert (reference.po_number, reference.line_number) == ("908491", 300)
    assert tokens.parse_po_line_ref("908491") is None


# --- Spec codes -------------------------------------------------------------


@pytest.mark.parametrize("raw, full, parent, sub", [
    ("STE-402-LT-B", "STE-402-LT-B", "STE-402-LT", "B"),
    ("STE-402-LT-SH", "STE-402-LT-SH", "STE-402-LT", "SH"),
    ("GR-350a-WTF", "GR-350a-WTF", "GR-350a-WTF", None),
    ("POOL-201.1-PL", "POOL-201.1-PL", "POOL-201.1-PL", None),
    ("RES-200-SG.1", "RES-200-SG.1", "RES-200-SG.1", None),
    ("STE-211R-SG", "STE-211R-SG", "STE-211R-SG", None),
    ("EXT-930-KCK", "EXT-930-KCK", "EXT-930", "KCK"),
    ("TI-40", "TI-40", "TI-40", None),
    ("LT-43-BLB", "LT-43-BLB", "LT-43-BLB", None),
])
def test_spec_grammar(raw, full, parent, sub):
    spec = tokens.normalize_spec(raw)
    assert (spec.full, spec.parent, spec.sub_part) == (full, parent, sub)


def test_description_words_are_not_swallowed_into_a_spec():
    """`16 EACH - POOL-201-SG-Pool Chaises` used to yield the phantom spec POOL-201-SG-POOL,
    because the text was uppercased before matching."""
    assert tokens.find_specs("16 EACH - POOL-201-SG-Pool Chaises") == ["POOL-201-SG"]


def test_lowercase_mention_in_a_note_is_not_a_spec():
    """"...for the (3) Pool-925-AC, 60x30x24 planters only" is prose about another line, and
    attributing that spec to this row would be wrong."""
    assert tokens.find_specs("Toe Kick Option for the (3) Pool-925-AC planters only") == []


def test_model_numbers_are_not_specs():
    assert "WWR-AL603024" not in tokens.find_specs("Model Number: WWR-AL603024")


# --- Quantities -------------------------------------------------------------


def test_item_quantity_is_read_from_the_front_of_the_item_cell():
    quantity = tokens.parse_item_quantity('11 EA - STE-402-LT-B - BASE, Floor Lamp 96"Lx30"Wx24"')
    assert (quantity.value, quantity.uom, quantity.is_package) == (11.0, "EA", False)


def test_package_units_are_never_returned_as_an_item_quantity():
    """`Quantity: 41 CTN` in an Inbound header is cartons on the truck; the receivable figure is
    the line row's `11 EA`. Receiving 41 against the PO line is the failure this prevents."""
    assert tokens.parse_item_quantity("41 CTN") is None
    package = tokens.parse_package_quantity("9 PLT - 3084.00 lb")
    assert (package.value, package.uom, package.is_package) == (9.0, "PLT", True)


def test_decimal_and_word_units():
    assert tokens.parse_item_quantity("175.04 SF").value == 175.04
    assert tokens.parse_item_quantity("16 EACH - POOL-201-SG").uom == "EACH"
    assert tokens.parse_item_quantity("202 YD - GR-350a-WTF").value == 202.0


def test_dimensions_in_a_description_are_not_read_as_a_quantity():
    assert tokens.parse_item_quantity('Linear Planter w/Pocket(s) 96"Lx30"Wx24"') is None


# --- Dates ------------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("09/24/2025", "2025-09-24"),
    ("10/31/25 at 3:22", "2025-10-31"),
    ("Sep 10, 2025 10:18", "2025-09-10"),
    ("2025-09-22 00:00:00", "2025-09-22"),
    ("TBC", None),
    ("", None),
])
def test_date_normalization(raw, expected):
    assert tokens.normalize_date(raw) == expected


# --- Shipment / tracking / references ---------------------------------------


def test_shipment_leg_suffix_is_dropped():
    """The Inbound states `90052 : 1` and the matching Delivered states `90052`. Keeping the leg
    counter would stop the two joining."""
    assert tokens.parse_shipment_number("90052 : 1") == "90052"
    assert tokens.parse_shipment_number("90009") == "90009"
    assert tokens.parse_shipment_number("") is None


def test_tracking_cell_with_carrier_appended():
    assert tokens.parse_tracking_numbers("91457971\n7497809572 FEDEX") == ["91457971", "7497809572"]


def test_fedex_reference_field_is_split_not_trusted():
    """`Purchase Order 91457971,910634,99985 : 1` is labelled PO but holds a carrier reference,
    the PO, and the Authority shipment. Reading the field whole gives 91457971 as the PO."""
    pos, others = tokens.split_reference_field("91457971,910634,99985 : 1")
    assert pos == ["910634"]
    assert others == ["91457971", "99985"]


# --- Boilerplate ------------------------------------------------------------


def test_repeated_confidentiality_notices_are_stripped():
    notice = ("NOTICE: This email contains confidential information solely for the use of the "
              "intended recipient(s). If you are not said recipient your use, disclosure or other "
              "distribution of any information included herewith is STRICTLY PROHIBITED, and you "
              "are instructed to notify the sender immediately and delete this email, all copies "
              "and attachments.")
    body = "Yes ma'am, this was received!\n\n" + "\n\n".join([notice] * 6)
    assert boilerplate.significant_word_count(body) < 10


def test_caution_banner_is_stripped():
    body = ("CAUTION: This email originated from outside the organization. Please exercise caution "
            "when clicking links or opening attachments. As a security reminder, please do not "
            "click any links or open attachments from an unknown source. If you receive one, "
            "report it and delete it immediately.\n\nWe confirm receipt.")
    assert boilerplate.strip_boilerplate(body).strip() == "We confirm receipt."


# --- Threads ----------------------------------------------------------------


FORWARDED = """anything else weird that happened on this one!!

From:
 warehousing@example-logistics.test <warehousing@example-logistics.test>

Sent:
 Wednesday, October 1, 2025 1:59 PM

To:
 Johnson, Kamilah <kamilahjohnson@example-pm.test>; victoria.cortez@example-flooring.test
 <victoria.cortez@example-flooring.test>; nick.beasley@example-flooring.test
 <nick.beasley@example-flooring.test>; Morales, Michael <michaelmorales@example-pm.test>

Subject:
 [External] 939260 - Inbound Notification - 906725, 907665

Received Date:
09/24/2025
"""


def test_forwarded_origin_is_recovered_from_the_quoted_chain():
    """Every corpus file is a `Fw:` from an internal expeditor, so the envelope sender is
    example-pm.test on all fourteen and identifies the originator on none."""
    parsed = thread.split_thread(FORWARDED, "arivera@example-pm.test", "Fw: [External] 939260 - Inbound Notification")
    origin = thread.resolve_origin("arivera@example-pm.test",
                                   "Fw: [External] 939260 - Inbound Notification", parsed)
    assert origin.sender_address == "warehousing@example-logistics.test"
    assert "939260 - Inbound Notification" in origin.subject


def test_a_multi_line_recipient_list_does_not_break_hop_detection():
    """The `To:` list wraps over several lines before `Subject:` appears — a tighter pattern
    matched zero hops on every Authority forward."""
    parsed = thread.split_thread(FORWARDED, "arivera@example-pm.test", "Fw: x")
    assert len(parsed.hops) == 2


def test_direct_mail_is_taken_at_face_value():
    parsed = thread.split_thread("Received Date:\n10/09/2025", "warehousing@example-logistics.test", "x")
    origin = thread.resolve_origin("warehousing@example-logistics.test", "x", parsed)
    assert origin.sender_address == "warehousing@example-logistics.test"


@pytest.mark.parametrize("raw, expected", [
    # Every shape present across the corpus's 111 quoted `Sent:` headers.
    ("Monday, December 1, 2025 2:14 PM", "2025-12-01"),
    ("Wednesday, October 01, 2025 5:24 PM", "2025-10-01"),        # zero-padded day
    ("Thursday, October 9, 2025 11:17:49 AM", "2025-10-09"),      # with seconds
    ("Thursday, May 7, 2026 17:03", "2026-05-07"),                # 24-hour, no AM/PM
    ("  Friday, September 26, 2025 11:14 AM  ", "2025-09-26"),    # surrounding whitespace
])
def test_a_quoted_sent_header_parses_to_a_date(raw, expected):
    """The date a delivery timeline should show. The envelope date of a forward is the day Premier
    forwarded it — 2026-06-06 on twelve of the fourteen files, which is how every stage of every
    purchase order came to carry one date.

    The 24-hour case is the one worth keeping: it appears six times and a format list written from
    the obvious samples alone silently drops it.
    """
    assert thread.parse_sent(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "yesterday", "2025-10-09", "Sent: whenever"])
def test_an_unreadable_sent_header_is_none_rather_than_an_error(raw):
    """This runs over mail from ~3,000 outside parties. One unfamiliar locale must not fail the
    ingest of an otherwise readable message — the envelope date is the fallback."""
    assert thread.parse_sent(raw) is None


def test_the_raw_sent_header_survives_even_when_it_cannot_be_parsed():
    """A shape we cannot read must stay visible rather than disappear, or nobody ever learns the
    format list is short."""
    hop = thread.ThreadHop(depth=1, sender_address="a@b.com", sender_domain="b.com",
                           sent_raw="17 Vendémiaire an XIV", to_raw="", subject="", body="")
    assert hop.sent_at is None
    assert hop.sent_raw == "17 Vendémiaire an XIV"


def test_the_forwarded_hop_carries_the_date_it_was_actually_sent():
    """End to end on the real corpus fixture: the quoted Authority hop states its own send date,
    months before Premier forwarded the file."""
    parsed = thread.split_thread(FORWARDED, "arivera@example-pm.test", "Fw: x")
    origin = thread.resolve_origin("arivera@example-pm.test", "Fw: x", parsed)
    assert origin.sent_at == "2025-10-01"


@pytest.mark.parametrize("subject, expected", [
    ("Fw: [External] RE: Example Hotel Public Space", "Example Hotel Public Space"),
    ("RE: FW: [External] Verification of Fabric Receipt", "Verification of Fabric Receipt"),
    ("[External] 939475 - Inbound Notification", "939475 - Inbound Notification"),
])
def test_forward_prefixes_are_stripped(subject, expected):
    assert thread.strip_forward_prefixes(subject) == expected


# --- Content sniffing -------------------------------------------------------


def test_kinds_are_decided_by_bytes_not_by_name():
    assert sniff.sniff(b"%PDF-1.4 rest", "not-a-pdf.txt").kind == sniff.KIND_PDF
    assert sniff.sniff(b"\x89PNG\r\n\x1a\n", "photo.doc").kind == sniff.KIND_IMAGE
    assert sniff.sniff(b"\xff\xd8\xff\xe1", "IMG_2479.jpeg").kind == sniff.KIND_IMAGE


def test_xlsx_is_recognised_with_no_content_type():
    """`Property Receivers.xlsx` arrives with `mimetype=None`; a content-type equality check
    rejected it and the tracker never reached an adapter."""
    import io

    import openpyxl
    workbook = openpyxl.Workbook()
    workbook.active.append(["PO#", "Spec#", "QTY"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    assert sniff.sniff(buffer.getvalue(), "Property Receivers.xlsx", "").kind == sniff.KIND_XLSX


def test_signature_logo_is_decorative_and_a_photo_is_not():
    logo = b"\x89PNG\r\n\x1a\n" + b"\x00" * 2000
    assert sniff.is_decorative_image(logo, "image001.png") is True
    photo = b"\xff\xd8\xff\xe1" + b"\x00" * (3 * 1024 * 1024)
    assert sniff.is_decorative_image(photo, "IMG_2479.jpeg") is False


# --- HTML tables ------------------------------------------------------------


def test_grid_is_found_by_header_signature_among_layout_tables():
    """Outlook lays messages out with tables — one corpus thread has 117 of them and one is the
    data. "The body contains a <table>" is not a signal."""
    html = (
        "<table><tr><td>spacer</td></tr></table>"
        "<table><tr><td>signature block</td></tr></table>"
        "<table><tr><th>PO # / Line #</th><th>Supplier</th><th>Part #</th><th>Item</th>"
        "<th>Package</th><th>Comments</th></tr>"
        "<tr><td>908491 : 300</td><td>Light Annex</td><td>STE-402-LT-B</td>"
        "<td>11 EA - BASE</td><td>11 CTN</td><td></td></tr></table>"
    )
    grid = tables.find_grid(tables.extract_tables(html),
                            ["PO # / Line #", "Supplier", "Part #", "Item", "Package", "Comments"])
    assert grid is not None and len(grid.body_rows) == 1


def test_key_value_table_reads_as_a_dict():
    html = ("<table><tr><td>Received Date:</td><td>09/24/2025</td></tr>"
            "<tr><td>ALS Shipment #:</td><td>90009 : 1</td></tr></table>")
    values = tables.as_key_values(tables.extract_tables(html)[0])
    assert values["received date"] == "09/24/2025"
    assert values["als shipment #"] == "90009 : 1"


def test_html_to_text_keeps_block_boundaries():
    assert text.html_to_text("<p>From:</p><p>a@b.com</p>") == "From:\na@b.com"


# --- Confirmation grids -----------------------------------------------------


def test_plain_po_spec_qty_table_is_not_a_confirmation_grid():
    """Identity plus quantity also describes a vendor packing list, which belongs to the generic
    row builder rather than to a confirm/awaiting-confirmation workflow."""
    assert confirmation.is_confirmation_grid(["PO", "Spec", "Description", "Qty"]) is False
    assert confirmation.is_confirmation_grid(
        ["Vendor", "PO#", "Spec#", "QTY", "Item Description", "Tracking", "Delivery Date",
         "Confirmed Received: Yes or No"]) is True
    assert confirmation.is_confirmation_grid(
        ["Description of Item", "SPEC # or Phase Code", "UOM", "Qty", "P.O.#", "Vendor"]) is True


def test_tracker_row_marked_yes_becomes_a_confident_record():
    header = ["Vendor", "PO#", "Spec#", "QTY", "Item Description", "Tracking", "Delivery Date",
              "Confirmed Received: Yes or No"]
    rows = [["Daniel Stuart", "907030", "LOB-203-PI", "12", '18"x18" Throw Pillow',
             "FedEx 476858924781", "2025-09-22", "yes"]]
    record = confirmation.records_from_grid(header, rows, "msg-1", "2026-01-01", "excel")[0]
    assert record.po_number == "907030"
    assert record.spec_code == "LOB-203-PI"
    assert record.quantity_received == 12.0
    assert record.pod_stated_date == "2025-09-22"
    assert record.carrier_name == "FedEx"
    assert record.tracking_number == "476858924781"
    assert record.extraction_confidence == 0.85


def test_a_no_answer_is_recorded_with_no_quantity_rather_than_dropped():
    """Dropping the row would erase the one record stating the goods did not arrive."""
    header = ["Vendor", "PO#", "Spec#", "QTY", "Item Description", "Confirmed Received: Yes or No"]
    rows = [["Amtrend", "906481", "PAT-200-SG", "1", "L-Shaped Banquette", "no"]]
    record = confirmation.records_from_grid(header, rows, "msg-1", "2026-01-01", "excel")[0]
    assert record.quantity_received is None
    assert record.extraction_confidence == 0.0
    assert "NOT received" in record.comments


def test_qty_delivered_wins_over_ordered_qty():
    header = ["Description of Item", "SPEC # or Phase Code", "UOM", "Qty", "Qty Delivered",
              "Qty to be Received", "P.O.#", "Vendor"]
    rows = [["Amenity Tray", "BRR-803-AC", "Set", "4", "4", "0", "912559", "Pigeon & Poodle"]]
    record = confirmation.records_from_grid(header, rows, "msg-1", "2026-01-01", "excel")[0]
    assert record.quantity_received == 4.0
    assert record.unit_of_measure == "SET"


def test_an_outstanding_balance_is_not_a_received_quantity():
    """`Qty to be Received` is what has *not* arrived, and it may not supply a receipt.

    This row is copied from `Public Space - Pending Receipt Confirmation Orders.xlsx`, where every
    line reads `Qty 4 | Qty Delivered blank | Qty to be Received 4 | RECEIVED? blank` — an
    unanswered request. It used to be the second entry in the gate's quantity columns, so the
    outstanding figure was read as the delivered one and `stated is not None` then proved receipt:
    57 phantom receipts on that one attachment, and on the Spitfire expediting export the same
    path staged rows reading "0 delivered" against live purchase orders.
    """
    header = ["Description of Item", "SPEC # or Phase Code", "UOM", "Qty", "Qty Delivered",
              "Qty to be Received", "P.O.#", "Vendor"]
    rows = [["Amenity Tray", "BRR-803-AC", "Set", "4", "", "4", "912559", "Pigeon & Poodle"]]
    record = confirmation.records_from_grid(header, rows, "msg-1", "2026-01-01", "excel")[0]
    assert record.quantity_received is None
    assert record.quantity_ordered == 4.0
    assert record.extraction_confidence < 0.5


# --- Carrier PODs -----------------------------------------------------------


FEDEX_POD = """December 01, 2025
Dear Customer,
The following is the proof-of-delivery for tracking number: 7497809572
Delivery Information:
Status: Delivered Delivery date: Sep 10, 2025 10:18
Signed for by: U ALI
Service type: FedEx Freight Priority
Tracking number: 7497809572 Ship Date: Sep 5, 2025
Weight: 392.0 LB/177.97 KG
Purchase Order 91457971,910634,99985 : 1
"""


def test_fedex_pod_fields():
    document = pod.parse_pod(FEDEX_POD)
    assert document.delivery_date == "2025-09-10"
    assert document.signed_for_by == "U ALI"
    assert document.carrier_name == "FedEx"
    assert document.tracking_numbers[0] == "7497809572"
    assert document.po_numbers == ["910634"]
    assert "99985" in document.other_references


def test_a_non_pod_document_is_not_parsed_as_one():
    assert pod.parse_pod("Invoice 12345\nAmount due: $400") is None


# --- Graph itemAttachment MIME ------------------------------------------------
# Graph returns an attached Outlook message from `/$value` as RFC-822 MIME with
# `contentType: message/rfc822` — never as an OLE `.msg`. Exchange prepends a `Received:` chain to
# it, and on the real Delivered Notification that chain fills the first 2,255 bytes on its own:
# `Content-Type`, `Date`, `From`, `Message-ID` and `Subject` all sit past it. Sniffing only the
# first 2 KB therefore saw one header name, failed the "two or more" test, and classified the one
# attachment carrying the POD as plain text.

def _exchange_mime(received_chain_bytes: int = 3000) -> bytes:
    hop = (b"Received: from LV8PR14MB7645.namprd14.prod.outlook.com (2603:10b6:408:263::6)\r\n"
           b" by BN8PR14MB3028.namprd14.prod.outlook.com with HTTPS; Wed, 10 Sep 2025\r\n"
           b" 20:00:53 +0000\r\n")
    chain = hop * (received_chain_bytes // len(hop) + 1)
    return (chain
            + b"Content-Type: multipart/alternative; boundary=\"x\"\r\n"
            + b"Date: Wed, 10 Sep 2025 20:00:47 +0000\r\n"
            + b"From: routing@example-logistics.test\r\n"
            + b"Subject: 99985 - Delivered Notification - 910634\r\n"
            + b"\r\nbody\r\n")


def test_exchange_mime_with_a_long_received_chain_is_a_message_not_text():
    raw = _exchange_mime()
    assert raw.find(b"\r\nFrom:") > 2048, "fixture must reproduce the real offsets"
    assert sniff.is_eml(raw) is True
    assert sniff.sniff(raw, "Delivered Notification.msg", "message/rfc822").kind == sniff.KIND_MSG


def test_a_received_only_prefix_still_reads_as_a_message():
    """The header block can be nothing but `Received:` lines for kilobytes. No plain text file
    carries that header, so one distinct name is enough here."""
    assert sniff.is_eml(_exchange_mime(60_000)) is True


def test_message_rfc822_content_type_is_honoured():
    assert sniff.sniff(b"no headers at all, just prose", "note", "message/rfc822").kind == sniff.KIND_MSG


def test_a_msg_filename_is_enough_when_the_bytes_are_mime():
    """`connectors.mailbox` names every itemAttachment `.msg` regardless of its actual encoding,
    so `.msg` has to be accepted alongside `.eml` or the container adapter never sees it."""
    assert sniff.sniff(b"Subject: hi\r\nTo: a@b.c\r\n\r\nbody", "forwarded.msg", "").kind == sniff.KIND_MSG


def test_ordinary_text_is_still_text():
    assert sniff.sniff(b"Just a note about a delivery.\nNothing structured.", "notes.txt", "").kind == sniff.KIND_TEXT


def test_a_body_quoting_a_from_line_is_not_a_message():
    """The header block ends at the first blank line; a quoted `From:` below it must not count."""
    body = b"Please see below.\r\n\r\nFrom: someone@example.com\r\nSubject: quoted\r\n"
    assert sniff.is_eml(body) is False


def test_an_external_tagged_forward_is_still_a_forward():
    """Exchange prepends `[External] ` to anything originating outside the tenant, so a genuine
    forward reaches us as `[External] Fw: ...`. Testing the raw subject read that as "not a
    forward" and took the forwarder's own annotation as the payload; three subjects in Premier's
    live mailbox already have that shape."""
    parsed = thread.split_thread(FORWARDED, "arivera@example-pm.test",
                                 "[External] Fw: 939260 - Inbound Notification")
    origin = thread.resolve_origin("arivera@example-pm.test",
                                   "[External] Fw: 939260 - Inbound Notification", parsed)
    assert origin.sender_address == "warehousing@example-logistics.test"


def test_a_reply_is_not_treated_as_a_forward():
    """`Re:` alone must not reach past the top hop — a genuine reply *is* the payload."""
    parsed = thread.split_thread(FORWARDED, "arivera@example-pm.test",
                                 "RE: 939260 - Inbound Notification")
    origin = thread.resolve_origin("arivera@example-pm.test",
                                   "RE: 939260 - Inbound Notification", parsed)
    assert origin.sender_address == "arivera@example-pm.test"


# --- A non-PO must never become a delivery key ------------------------------

def test_only_six_digit_tokens_are_purchase_orders():
    """`is_po_number` is the shape gate every path into an accumulation key now passes.

    Measured 2026-08-25 on an Atlas Logistics "Warehouse Receiving Report" that no parser knew:
    the generic table path read an address block and produced the purchase orders
    `(812) 424-2222`, `Premier`, `1150 New` and `Circuit of\nc/o EDC\nDept #`. All four became
    delivery-event keys in `released_events` and `accumulation`, while the document's real PO sat
    in it as `Project #: 911798`.
    """
    from pipeline.parsing import tokens

    assert tokens.is_po_number("912614")
    assert tokens.is_po_number("  912614  "), "surrounding whitespace is not a difference"

    for junk in ("(812) 424-2222", "Premier", "1150 New", "Circuit of\nc/o EDC\nDept #",
                 "", None, "21261", "2126145", "21261a"):
        assert not tokens.is_po_number(junk), f"{junk!r} is not a purchase order"


def test_a_table_column_that_is_not_a_po_does_not_become_one():
    """`map_headers` matches headers fuzzily against short synonyms, so on an unrecognised document
    it lands on whatever column looked closest. The cell value has to be checked too."""
    from pipeline.stage3_extract.base import build_record_from_row, ExtractionSource

    src = ExtractionSource(source_email_id="m1", email_date="2026-08-25T00:00:00Z",
                           source_type="attachment")
    record = build_record_from_row(
        src, ["(812) 424-2222", "CTP-025-NA", "1"],
        {"po_number": 0, "spec_code": 1, "quantity_received": 2}, "pdf")
    assert record.po_number != "(812) 424-2222"
    assert record.spec_code == "CTP-025-NA", "the rest of the row is still read"


def test_attachment_evidence_only_contributes_real_po_numbers():
    from pipeline import evidence
    from pipeline.models import ExtractedRecord

    def rec(po):
        return ExtractedRecord(
            source_email_id="m1", po_number=po, shipment_number=None, spec_code=None,
            parent_spec_code=None, sub_spec_suffix=None, item_description=None, vendor_name=None,
            carrier_name=None, tracking_number=None, quantity_received=None, unit_of_measure=None,
            pod_stated_date=None, email_date="2026-08-25T00:00:00Z", delivery_location=None,
            comments=None, extraction_source="pdf", extraction_confidence=0.3, raw_snippet="")

    bundle = evidence.EmailEvidence(email_id="m1",
                                    records=[rec("911798"), rec("Premier"), rec("(812) 424-2222")])
    evidence._summarise(bundle)
    assert bundle.po_numbers == ["911798"]
