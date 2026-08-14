"""The receiver report: grouping, and the columns that must stay empty.

This sheet is read as authoritative — it is laid out to sit beside Spitfire's own Receipt Log. So
the two things worth pinning are that rows group into the right lines, and that the three columns
we cannot know are never quietly filled with a plausible number.
"""

from io import BytesIO

import openpyxl

from pipeline import extracted_records_store, receipt_log, state_db
from pipeline.models import ExtractedRecord

NOW = "2026-08-03T12:00:00Z"


def new_conn():
    return state_db.get_connection(":memory:")


def record(**overrides) -> ExtractedRecord:
    values = dict(
        source_email_id="msg-1", po_number="208491", shipment_number=None,
        spec_code="STE-402-LT-B", parent_spec_code="STE-402-LT", sub_spec_suffix="B",
        item_description="BASE, Floor Lamp 2", vendor_name="Light Annex",
        carrier_name="Custom Companies", tracking_number="69942177", quantity_received=1.0,
        unit_of_measure="EA", pod_stated_date="2025-10-09",
        email_date="2025-10-09T23:17:49+05:30", delivery_location=None, comments=None,
        extraction_source="authority_inbound", extraction_confidence=1.0, raw_snippet="…",
        po_line_number=300, received_by="Miguel C.", notification_number="239475",
    )
    values.update(overrides)
    return ExtractedRecord(**values)


def test_build_groups_rows_into_po_then_line_then_receipts():
    conn = new_conn()
    extracted_records_store.write_pending(conn, record(quantity_received=1.0), NOW)
    extracted_records_store.write_pending(conn, record(quantity_received=2.0), NOW)

    report = receipt_log.build(conn)
    [po] = report.purchase_orders
    assert po.po_number == "208491"
    assert po.vendor == "Light Annex"
    [line] = po.lines
    assert line.label == "0300"
    assert line.received == 3.0, "two receipts against one line sum onto it"
    assert len(line.receipts) == 2


def test_two_line_numbers_are_two_lines():
    """Sub-parts are separate Spitfire lines — STE-402-LT-B and -SH are 300 and 301. Merging them
    on their shared parent would report one line with a quantity neither of them has."""
    conn = new_conn()
    extracted_records_store.write_pending(conn, record(po_line_number=300, spec_code="STE-402-LT-B"), NOW)
    extracted_records_store.write_pending(conn, record(po_line_number=301, spec_code="STE-402-LT-SH"), NOW)

    [po] = receipt_log.build(conn).purchase_orders
    assert [line.label for line in po.lines] == ["0300", "0301"]


def test_the_receipt_reference_falls_back_through_three_sources():
    conn = new_conn()
    extracted_records_store.write_pending(conn, record(notification_number="239475"), NOW)
    extracted_records_store.write_pending(
        conn, record(po_line_number=301, notification_number=None, shipment_number="50052"), NOW)
    extracted_records_store.write_pending(
        conn, record(po_line_number=302, notification_number=None, shipment_number=None), NOW)

    refs = [line.receipts[0].reference for line in receipt_log.build(conn).purchase_orders[0].lines]
    assert refs == ["239475", "50052", "Email confirmation"]


def test_order_qty_and_final_are_never_populated_from_email():
    """Order Qty lives on the purchase order inside Spitfire and no delivery email carries it. A
    sheet in this layout reads as authoritative, so an invented order quantity is worse than a gap.

    `Final` is separate: it is a flag someone sets in Spitfire, not a state we could infer.
    """
    conn = new_conn()
    extracted_records_store.write_pending(conn, record(), NOW)
    [line] = receipt_log.build(conn).purchase_orders[0].lines
    assert line.order_qty is None
    assert line.final is False
    assert line.net is None, "Net cannot be known while Order Qty is not"


def test_net_is_derived_from_order_qty_rather_than_stored():
    """`Net = Order Qty - Received` held for all 486 line rows in Premier's own export, so it is
    not an independent fact and must not need a source of its own. Computing it here is what makes
    the column fill itself the day the Spitfire read lands."""
    assert receipt_log.Line(1, "SP", "d", received=2, order_qty=5).net == 3
    assert receipt_log.Line(1, "SP", "d", received=5, order_qty=5).net == 0
    # Over-delivery is real — the corpus has 202 received against 196 ordered — and must show as a
    # negative rather than being clamped, or the sheet hides the discrepancy it exists to surface.
    assert receipt_log.Line(1, "SP", "d", received=7, order_qty=5).net == -2
    # A line with no receipts yet is outstanding in full, not zero.
    assert receipt_log.Line(1, "SP", "d", received=None, order_qty=5).net == 5


def test_final_is_not_inferred_from_quantities():
    """The regression guard. In Premier's export 118 fully-received lines carry no `*` and 5
    partially-received lines do, so any received-versus-ordered rule would be wrong 123 times in
    486. If someone later "fixes" Final by computing it, this fails."""
    fully_received = receipt_log.Line(1, "SP", "d", received=5, order_qty=5)
    assert fully_received.final is False


def test_to_html_returns_a_plain_string_both_uis_can_wrap():
    """Not either UI's `Raw`: the console and /ui each have their own marker class, and this module
    must depend on neither. Escaping still happens here."""
    conn = new_conn()
    extracted_records_store.write_pending(conn, record(item_description='Lamp <b>"A&B"</b>'), NOW)
    out = receipt_log.to_html(receipt_log.build(conn))

    assert type(out) is str
    # `to_html` emits no <b> of its own, so any that survived came from the description.
    assert "<b>" not in out
    assert "&lt;b&gt;" in out and "&amp;" in out and "&quot;" in out


def test_every_preview_row_is_exactly_as_wide_as_the_header():
    """A row one cell short does not fail loudly — every column right of the gap shifts left by
    one and the table still renders, which is exactly how a receipt date came to sit under a
    heading that said Order Qty. Counting `colspan` here is what makes that a test failure rather
    than something noticed in a screenshot."""
    import re

    conn = new_conn()
    extracted_records_store.write_pending(conn, record(), NOW)
    out = receipt_log.to_html(receipt_log.build(conn))

    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", out)
    assert rows, "the preview rendered no rows at all"
    for row in rows:
        width = sum(int(m or 1) for m in re.findall(r'<td(?:[^>]*?colspan="(\d+)")?[^>]*>', row))
        assert width == receipt_log._HTML_COLUMNS, f"row is {width} cells wide: {row}"


def test_the_preview_never_puts_a_date_in_the_order_qty_column():
    """Spitfire's own sheet reuses Order Qty (col F) for a receipt's date. The preview splits Date
    out instead, so Order Qty holds order quantities and nothing else. `to_xlsx` still follows
    Spitfire — see `test_receipt_log_conformance.py`."""
    import re

    conn = new_conn()
    extracted_records_store.write_pending(conn, record(), NOW)
    out = receipt_log.to_html(receipt_log.build(conn))

    # `[^>]*` because a row also carries `data-group`, which is what lets the UI page this sheet by
    # purchase order rather than splitting one across a page boundary. Matching the tag exactly made
    # this test fail on a markup addition it has no opinion about.
    header = re.search(r'<tr class="r-head"[^>]*>(.*?)</tr>', out).group(1)
    columns = re.findall(r"<td[^>]*>(.*?)</td>", header)
    assert columns.index("Date") < columns.index("Order Qty")

    receipt = re.search(r'<tr class="r-rcpt"[^>]*>(.*?)</tr>', out).group(1)
    cells = re.findall(r"<td[^>]*>(.*?)</td>", receipt)
    assert re.match(r"\d{4}-\d{2}-\d{2}", cells[columns.index("Date")])
    assert cells[columns.index("Order Qty")] == ""


def test_the_sheet_title_matches_premiers_own_file():
    """Premier's `_Spitfire-LIVE_General_Receipt Log.xlsx` reads 'Premier Design to Completion
    Report' in H1. The word 'Report' was missing here."""
    assert receipt_log.SHEET_TITLE == "Premier Design to Completion Report"


def test_to_xlsx_produces_a_workbook_with_the_headers_on_row_five():
    conn = new_conn()
    extracted_records_store.write_pending(conn, record(), NOW)
    workbook = openpyxl.load_workbook(BytesIO(receipt_log.to_xlsx(receipt_log.build(conn))))

    sheet = workbook["Receipt Log"]
    assert sheet["H1"].value == receipt_log.SHEET_TITLE
    assert sheet["H2"].value == receipt_log.SHEET_SUBTITLE
    assert [sheet.cell(5, col).value for col in (1, 2, 3, 4)] == ["DocNo", "Vendor", "Line", "Description"]
    # The three unknowable columns carry a header and never a value.
    assert sheet.cell(5, 6).value == "Order Qty"
    assert all(sheet.cell(row, 6).value in (None, "On") or hasattr(sheet.cell(row, 6).value, "year")
               for row in range(7, sheet.max_row + 1))


def test_an_empty_store_still_renders_both_ways():
    """A fresh checkout must produce a sheet that says so rather than raising."""
    conn = new_conn()
    report = receipt_log.build(conn)
    assert report.is_empty
    assert report.empty_message in receipt_log.to_html(report)
    assert receipt_log.to_xlsx(report)[:2] == b"PK"


def test_build_does_not_require_the_caller_to_set_a_row_factory():
    """`api.deps.get_pipeline_conn` does not set one. Without `build` setting it itself the failure
    is a TypeError deep in the grouping loop that names nothing useful."""
    conn = new_conn()
    conn.row_factory = None
    extracted_records_store.write_pending(conn, record(), NOW)
    assert receipt_log.build(conn).purchase_orders[0].po_number == "208491"
    assert conn.row_factory is None, "the caller's factory is restored"
