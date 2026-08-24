"""The Authority Logistics parser — corpus classes A and B.

This format is Phase 1's entire structured volume, and it is the one the generic adapters
cannot read: there is no "PO" label anywhere near the PO numbers, and the line-table header is
`PO # / Line #` rather than anything the generic column map recognises.
"""

import pytest

from pipeline.parsing import text
from pipeline.vendors import authority as az
from tests import corpus_fixtures as fx


def parse(email):
    return az.parse_authority_notice(
        email.sender_address if "authoritylogistics" in email.sender_address else fx.WAREHOUSING,
        email.subject, email.body_html, text.html_to_text(email.body_html),
    )


# --- Classification ---------------------------------------------------------


@pytest.mark.parametrize("sender, subject, expected", [
    (fx.WAREHOUSING, "[External] 239336 - Inbound Notification - 208491 - 2985 : LXR Cameo",
     az.NoticeKind.INBOUND),
    (fx.ROUTING, "[External] 50009 - Delivered Notification -  - 206725, 207665",
     az.NoticeKind.DELIVERED),
    (fx.WAREHOUSING, "[External] Purchase Order Status Report - Summary : 2978: LXR Cameo",
     az.NoticeKind.STATUS_REPORT),
    ("gm@remingtonhotels.com", "RE: PO 212448", az.NoticeKind.NOT_AUTHORITY),
])
def test_classification_by_sender_and_subject(sender, subject, expected):
    assert az.classify(sender, subject) == expected


def test_status_report_shares_the_trigger_sender_and_is_told_apart_by_subject_alone():
    """The weekly summary comes from `warehousing@` — the same address as the receiver trigger.
    A sender-only rule routes a status report into the receiver path."""
    assert az.classify(fx.WAREHOUSING, "[External] Purchase Order Status Report - Summary : 2978") \
        != az.classify(fx.WAREHOUSING, "[External] 239336 - Inbound Notification - 208491")


def test_forwarded_subject_prefixes_do_not_defeat_the_grammar():
    assert az.classify(fx.WAREHOUSING, "Fw: [External] 239336 - Inbound Notification - 208491") \
        == az.NoticeKind.INBOUND


# --- Inbound (class A) ------------------------------------------------------


def test_inbound_header_and_line_fields():
    notice = parse(fx.inbound_email(notice="239336", po_numbers=("208491",), shipment="50052 : 1"))
    assert notice.kind == az.NoticeKind.INBOUND
    assert notice.notice_number == "239336"
    assert notice.subject_po_numbers == ["208491"]
    assert notice.project_code == "2985"
    assert notice.received_date == "2025-10-01"
    assert notice.received_by == "Miguel C."
    assert notice.carrier == "Nolan Transportation"
    assert notice.tracking_numbers == ["8840455"]
    assert notice.als_shipment_number == "50052"
    assert notice.received_at.startswith("Crown Worldwide")

    line = notice.lines[0]
    assert (line.po_number, line.line_number) == ("208491", 300)
    assert (line.quantity, line.unit_of_measure) == (11.0, "EA")
    assert (line.package_quantity, line.package_uom) == (11.0, "CTN")
    assert line.spec_code == "STE-402-LT-B"
    assert (line.parent_spec_code, line.sub_spec_suffix) == ("STE-402-LT", "B")


def test_header_package_quantity_never_becomes_the_item_quantity():
    """The header says `41 CTN` (cartons on the truck) and the line says `11 EA` (units against
    the PO line). Receiving 41 would be a 30-unit over-receipt."""
    notice = parse(fx.inbound_email(package_qty="41 CTN"))
    assert notice.header_quantity == 41.0 and notice.header_uom == "CTN"
    assert notice.lines[0].quantity == 11.0


def test_blank_als_shipment_number_yields_no_key_rather_than_a_substitute():
    notice = parse(fx.inbound_email(notice="239475", shipment=""))
    assert notice.als_shipment_number is None
    assert notice.shipment_number is None
    assert notice.notice_number == "239475"


def test_unequal_parts_of_one_lamp_are_separate_lines():
    """A floor lamp arrives as 11 bases and 12 shades — Spitfire lines 300 and 301. They share a
    parent spec but are distinct receivable lines with legitimately different quantities."""
    notice = parse(fx.inbound_email(lines=[
        {"po": "208491", "line": "300", "part": "STE-402-LT-B", "item": "11 EA - STE-402-LT-B BASE, Floor Lamp"},
        {"po": "208491", "line": "301", "part": "STE-402-LT-SH", "item": "12 EA - STE-402-LT-SH SHADE, Floor Lamp"},
    ]))
    base, shade = notice.lines
    assert (base.line_number, base.quantity, base.sub_spec_suffix) == (300, 11.0, "B")
    assert (shade.line_number, shade.quantity, shade.sub_spec_suffix) == (301, 12.0, "SH")
    assert base.parent_spec_code == shade.parent_spec_code == "STE-402-LT"


def test_prose_in_the_part_column_yields_no_spec_rather_than_a_wrong_one():
    """Authority puts "Accessory Pocket" and "Toe Kick Planter" in `Part #`. Neither is a spec,
    and the item note mentions an unrelated spec that must not be attributed to this line."""
    notice = parse(fx.inbound_email(lines=[
        {"po": "207665", "line": "6", "part": "Toe Kick Planter",
         "item": "1 EA - Toe Kick Planter Toe Kick Option for the (3) Pool-925-AC planters only"},
    ]))
    assert notice.lines[0].spec_code is None
    assert notice.lines[0].quantity == 1.0


def test_package_figure_carries_forward_across_a_multi_line_shipment():
    """Authority states the package figure once, on the first row, and leaves the rest blank."""
    notice = parse(fx.inbound_email(lines=[
        {"po": "206725", "line": "1", "part": "EXT-925-AC", "item": "2 EACH - EXT-925-AC Planter",
         "package": "9 PLT - 3084.00 lb"},
        {"po": "206725", "line": "2", "part": "EXT-926-AC", "item": "2 EACH - EXT-926-AC Planter"},
    ]))
    assert all(line.package_quantity == 9.0 and line.package_uom == "PLT" for line in notice.lines)
    assert [line.quantity for line in notice.lines] == [2.0, 2.0]


def test_multi_po_notice_filters_records_to_one_po():
    """The orchestrator runs one event per PO. Without the filter, a two-PO notice stages every
    line twice — once under each PO (finding C1)."""
    notice = parse(fx.inbound_email(notice="239260", po_numbers=("206725", "207665"), lines=[
        {"po": "206725", "line": "1", "part": "EXT-925-AC", "item": "2 EACH - EXT-925-AC Planter"},
        {"po": "207665", "line": "1", "part": "POOL-925-AC", "item": "3 EA - POOL-925-AC Planter"},
    ]))
    assert notice.po_numbers == ["206725", "207665"]
    only_first = az.records_from_notice(notice, "msg-1", "2026-01-01", only_po="206725")
    assert [r.po_number for r in only_first] == ["206725"]
    assert len(az.records_from_notice(notice, "msg-1", "2026-01-01")) == 2


# --- Delivered (class B) ----------------------------------------------------


def test_delivered_header_and_lines():
    notice = parse(fx.delivered_email(notice="50009", po_numbers=("206725",)))
    assert notice.kind == az.NoticeKind.DELIVERED
    assert notice.authority_number == "50009"
    assert notice.received_date == "2025-09-15"
    assert notice.carrier == "Nolan Transportation"
    assert notice.tracking_numbers == ["8801592"]
    assert notice.received_at.startswith("Crown Worldwide")

    line = notice.lines[0]
    assert line.po_number == "206725"
    assert line.line_number is None          # the Delivered format carries no line numbers
    assert (line.quantity, line.unit_of_measure) == (2.0, "EACH")
    assert line.spec_code == "EXT-925-AC"    # recovered from the Item text, not a Part # column
    assert (line.package_quantity, line.package_uom) == (9.0, "SKID")


def test_delivered_and_inbound_report_the_same_shipment_key():
    """`Authority #` on a Delivered notice is `ALS Shipment #` on the matching Inbound. This is
    the join that stops one physical delivery becoming two receivers."""
    delivered = parse(fx.delivered_email(notice="50009"))
    inbound = parse(fx.inbound_email(notice="239260", shipment="50009 : 1"))
    assert delivered.shipment_number == inbound.shipment_number == "50009"


def test_recipient_list_of_a_forwarding_hop_is_not_read_as_the_delivery_location():
    """A forwarded notice carries Outlook's own `To:` header above the notice's own `To:` field.
    Read against the whole message, `delivery_location` came out as a list of premierpm.com
    addresses."""
    email = fx.delivered_email(forwarded=True)
    notice = az.parse_authority_notice(fx.ROUTING, email.subject, email.body_html,
                                       text.html_to_text(email.body_html))
    assert "premierpm.com" not in (notice.received_at or "")
    assert notice.received_at.startswith("Crown Worldwide")


def test_delivered_records_are_less_confident_than_inbound_ones():
    """An Inbound line states the Spitfire line number outright, so it needs no fuzzy matching.
    A Delivered line cannot claim the same."""
    inbound = az.records_from_notice(parse(fx.inbound_email()), "m", "2026-01-01")[0]
    delivered = az.records_from_notice(parse(fx.delivered_email()), "m", "2026-01-01")[0]
    assert inbound.extraction_confidence == 1.0
    assert delivered.extraction_confidence < inbound.extraction_confidence


# --- Status report ----------------------------------------------------------


def test_status_report_parses_to_a_deliberately_empty_notice():
    """Recognised and empty, so it is routed as noise rather than falling through to a generic
    adapter that would scrape PO-looking numbers out of a summary table."""
    notice = az.parse_authority_notice(
        fx.WAREHOUSING, "[External] Purchase Order Status Report - Summary : 2978: LXR Cameo",
        "<table><tr><th>PO</th><th>Status</th></tr><tr><td>208491</td><td>Open</td></tr></table>",
        "",
    )
    assert notice.kind == az.NoticeKind.STATUS_REPORT
    assert notice.lines == []
    assert az.records_from_notice(notice, "m", "2026-01-01") == []


# --- Header labels sharing a line -------------------------------------------
# Authority renders `Delivered: 09/10/2025  Signed by:  U ALI` as ONE line. The old per-label
# regex was line-anchored, so `Signed by` was never found and `received_by` came back empty on
# every Delivered notice in the mailbox — 13 of 13 records had no proof of who took the goods.

_TWO_LABELS_ON_ONE_LINE = (
    "Authority #: 49985\n\n"
    "Carrier: GlobalTranz Enterprises, LLC\n\n"
    "Tracking:\n\r\n        31457971\n7497809572 FEDEX\r\n\n"
    "From: Hampton Textile Printing Inc. : 2230 Eddie Williams Drive, Johnson City, TN\n\n"
    "Delivered: 09/10/2025  Signed by:  U ALI\n\n"
    "To: 5-Star Interior Services, Inc. : 6840 Walthall Way, Paramount, CA\r\n\n"
)


def test_a_label_sharing_a_line_with_another_is_still_read():
    assert az._label_value(_TWO_LABELS_ON_ONE_LINE, "signed by") == "U ALI"


def test_a_value_stops_at_the_next_label_on_the_same_line():
    """`Delivered` used to swallow `Signed by:  U ALI` and the whole `To:` line after it — the
    end-of-value lookahead lost to backtracking."""
    assert az._label_value(_TWO_LABELS_ON_ONE_LINE, "delivered") == "09/10/2025"


def test_a_value_beginning_on_the_line_below_its_label_survives():
    """Outlook renders `Tracking:` with its value on the next line; exactly one leading newline
    belongs to the value, and a second is a paragraph break."""
    assert az._label_value(_TWO_LABELS_ON_ONE_LINE, "tracking") == "31457971 7497809572 FEDEX"


def test_ship_from_and_ship_to_are_not_confused_by_the_shared_line():
    values = az._label_values(_TWO_LABELS_ON_ONE_LINE)
    assert values["from"].startswith("Hampton Textile")
    assert values["to"].startswith("5-Star Interior Services")
