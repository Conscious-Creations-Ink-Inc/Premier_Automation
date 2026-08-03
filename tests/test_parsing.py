"""The shared parsing primitives.

Every case here is a literal string from the real June corpus. Where a case asserts that
something is *not* matched, the false positive it guards against was one the earlier regexes
actually produced.
"""

import pytest

from pipeline.parsing import boilerplate, confirmation, pod, sniff, tables, text, thread, tokens


# --- PO numbers -------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("PO 213987", ["213987"]),
    ("PO# 213987", ["213987"]),
    ("PO #213987", ["213987"]),
    ("P.O.# 210634", ["210634"]),
    ("PO: 213987", ["213987"]),
    ("po 213987", ["213987"]),                       # case-insensitive — the old regex was not
    ("Purchase Order 210634", ["210634"]),
    ("PO 211310 + PO 212578", ["211310", "212578"]),
    ("PO 207505, 207514, 212559", ["207505", "207514", "212559"]),
])
def test_labelled_po_forms(raw, expected):
    assert tokens.find_po_numbers(raw) == expected


def test_bare_six_digit_numbers_are_not_treated_as_pos():
    """An Authority subject holds the inbound number, two POs and a project number, three of
    them six digits. Only position tells them apart, so the free-text scan claims none of them."""
    subject = "239260 - Inbound Notification - 206725, 207665 - 2978 : LXR Cameo Beverly Hills"
    assert tokens.find_po_numbers(subject) == []


def test_po_list_reads_an_isolated_slot():
    assert tokens.parse_po_list("206725, 207665") == ["206725", "207665"]


def test_po_line_ref():
    reference = tokens.parse_po_line_ref("208491 : 300")
    assert (reference.po_number, reference.line_number) == ("208491", 300)
    assert tokens.parse_po_line_ref("208491") is None


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
    """The Inbound states `50052 : 1` and the matching Delivered states `50052`. Keeping the leg
    counter would stop the two joining."""
    assert tokens.parse_shipment_number("50052 : 1") == "50052"
    assert tokens.parse_shipment_number("50009") == "50009"
    assert tokens.parse_shipment_number("") is None


def test_tracking_cell_with_carrier_appended():
    assert tokens.parse_tracking_numbers("31457971\n7497809572 FEDEX") == ["31457971", "7497809572"]


def test_fedex_reference_field_is_split_not_trusted():
    """`Purchase Order 31457971,210634,49985 : 1` is labelled PO but holds a carrier reference,
    the PO, and the Authority shipment. Reading the field whole gives 31457971 as the PO."""
    pos, others = tokens.split_reference_field("31457971,210634,49985 : 1")
    assert pos == ["210634"]
    assert others == ["31457971", "49985"]


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
 warehousing@authoritylogistics.com <warehousing@authoritylogistics.com>

Sent:
 Wednesday, October 1, 2025 1:59 PM

To:
 Johnson, Kamilah <kamilahjohnson@premierpm.com>; victoria.cortez@goarmstrong.com
 <victoria.cortez@goarmstrong.com>; nick.beasley@goarmstrong.com
 <nick.beasley@goarmstrong.com>; Morales, Michael <michaelmorales@premierpm.com>

Subject:
 [External] 239260 - Inbound Notification - 206725, 207665

Received Date:
09/24/2025
"""


def test_forwarded_origin_is_recovered_from_the_quoted_chain():
    """Every corpus file is a `Fw:` from an internal expeditor, so the envelope sender is
    premierpm.com on all fourteen and identifies the originator on none."""
    parsed = thread.split_thread(FORWARDED, "mariagutierrez@premierpm.com", "Fw: [External] 239260 - Inbound Notification")
    origin = thread.resolve_origin("mariagutierrez@premierpm.com",
                                   "Fw: [External] 239260 - Inbound Notification", parsed)
    assert origin.sender_address == "warehousing@authoritylogistics.com"
    assert "239260 - Inbound Notification" in origin.subject


def test_a_multi_line_recipient_list_does_not_break_hop_detection():
    """The `To:` list wraps over several lines before `Subject:` appears — a tighter pattern
    matched zero hops on every Authority forward."""
    parsed = thread.split_thread(FORWARDED, "mariagutierrez@premierpm.com", "Fw: x")
    assert len(parsed.hops) == 2


def test_direct_mail_is_taken_at_face_value():
    parsed = thread.split_thread("Received Date:\n10/09/2025", "warehousing@authoritylogistics.com", "x")
    origin = thread.resolve_origin("warehousing@authoritylogistics.com", "x", parsed)
    assert origin.sender_address == "warehousing@authoritylogistics.com"


@pytest.mark.parametrize("subject, expected", [
    ("Fw: [External] RE: Cameo Public Space", "Cameo Public Space"),
    ("RE: FW: [External] Verification of Fabric Receipt", "Verification of Fabric Receipt"),
    ("[External] 239475 - Inbound Notification", "239475 - Inbound Notification"),
])
def test_forward_prefixes_are_stripped(subject, expected):
    assert thread.strip_forward_prefixes(subject) == expected


# --- Content sniffing -------------------------------------------------------


def test_kinds_are_decided_by_bytes_not_by_name():
    assert sniff.sniff(b"%PDF-1.4 rest", "not-a-pdf.txt").kind == sniff.KIND_PDF
    assert sniff.sniff(b"\x89PNG\r\n\x1a\n", "photo.doc").kind == sniff.KIND_IMAGE
    assert sniff.sniff(b"\xff\xd8\xff\xe1", "IMG_2479.jpeg").kind == sniff.KIND_IMAGE


def test_xlsx_is_recognised_with_no_content_type():
    """`Cameo Receivers.xlsx` arrives with `mimetype=None`; a content-type equality check
    rejected it and the tracker never reached an adapter."""
    import io

    import openpyxl
    workbook = openpyxl.Workbook()
    workbook.active.append(["PO#", "Spec#", "QTY"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    assert sniff.sniff(buffer.getvalue(), "Cameo Receivers.xlsx", "").kind == sniff.KIND_XLSX


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
        "<tr><td>208491 : 300</td><td>Light Annex</td><td>STE-402-LT-B</td>"
        "<td>11 EA - BASE</td><td>11 CTN</td><td></td></tr></table>"
    )
    grid = tables.find_grid(tables.extract_tables(html),
                            ["PO # / Line #", "Supplier", "Part #", "Item", "Package", "Comments"])
    assert grid is not None and len(grid.body_rows) == 1


def test_key_value_table_reads_as_a_dict():
    html = ("<table><tr><td>Received Date:</td><td>09/24/2025</td></tr>"
            "<tr><td>ALS Shipment #:</td><td>50009 : 1</td></tr></table>")
    values = tables.as_key_values(tables.extract_tables(html)[0])
    assert values["received date"] == "09/24/2025"
    assert values["als shipment #"] == "50009 : 1"


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
    rows = [["Daniel Stuart", "207030", "LOB-203-PI", "12", '18"x18" Throw Pillow',
             "FedEx 476858924781", "2025-09-22", "yes"]]
    record = confirmation.records_from_grid(header, rows, "msg-1", "2026-01-01", "excel")[0]
    assert record.po_number == "207030"
    assert record.spec_code == "LOB-203-PI"
    assert record.quantity_received == 12.0
    assert record.pod_stated_date == "2025-09-22"
    assert record.carrier_name == "FedEx"
    assert record.tracking_number == "476858924781"
    assert record.extraction_confidence == 0.85


def test_a_no_answer_is_recorded_with_no_quantity_rather_than_dropped():
    """Dropping the row would erase the one record stating the goods did not arrive."""
    header = ["Vendor", "PO#", "Spec#", "QTY", "Item Description", "Confirmed Received: Yes or No"]
    rows = [["Amtrend", "206481", "PAT-200-SG", "1", "L-Shaped Banquette", "no"]]
    record = confirmation.records_from_grid(header, rows, "msg-1", "2026-01-01", "excel")[0]
    assert record.quantity_received is None
    assert record.extraction_confidence == 0.0
    assert "NOT received" in record.comments


def test_qty_delivered_wins_over_ordered_qty():
    header = ["Description of Item", "SPEC # or Phase Code", "UOM", "Qty", "Qty Delivered",
              "Qty to be Received", "P.O.#", "Vendor"]
    rows = [["Amenity Tray", "BRR-803-AC", "Set", "4", "", "4", "212559", "Pigeon & Poodle"]]
    record = confirmation.records_from_grid(header, rows, "msg-1", "2026-01-01", "excel")[0]
    assert record.quantity_received == 4.0
    assert record.unit_of_measure == "SET"


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
Purchase Order 31457971,210634,49985 : 1
"""


def test_fedex_pod_fields():
    document = pod.parse_pod(FEDEX_POD)
    assert document.delivery_date == "2025-09-10"
    assert document.signed_for_by == "U ALI"
    assert document.carrier_name == "FedEx"
    assert document.tracking_numbers[0] == "7497809572"
    assert document.po_numbers == ["210634"]
    assert "49985" in document.other_references


def test_a_non_pod_document_is_not_parsed_as_one():
    assert pod.parse_pod("Invoice 12345\nAmount due: $400") is None
