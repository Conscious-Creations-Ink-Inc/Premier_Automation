"""The gate that separates a delivery document from a status report.

Every header row here is copied from a real workbook in Premier's mailbox, because the whole
point of the module is that the two shapes look almost identical on paper: both carry
`Description of Item`, `SPEC # or Phase Code`, `Qty` and `Qty Delivered`. What separates them is
the order-lifecycle columns, which a receipt document never has.
"""

from pipeline.parsing import receipt

# `Public Space - Pending Receipt Confirmation Orders.xlsx`, sheet `Hoja1` — the real thing.
CONFIRMATION_GRID = [
    "Description of Item", "SPEC # or Phase Code", "UOM", "Qty", "Qty Delivered",
    "Qty to be Received", "P.O.#", "Vendor", "Actual Delivery Date",
    "(Ship to) GC,WH, Property, Vendor", "Comments", "RECEIVED? YES or NO",
]

# `2026.09.03 ANS-025-TB-10 ... Expediting Report.xlsx`, sheet ` EXPEDITING REPORT`.
EXPEDITING_REPORT = [
    "Description of Item", "SPEC # or Phase Code", "UOM", "Qty", "Qty\nDelivered",
    "Qty to be Received", "P.O.#", "Line", "P.O. Created", "Vendor", "Ack Rec'd",
    "Dep Check Tracked", "Dep Check Sent", "Estimated Delivery (Date)", "Actual Delivery Date",
    "(Ship to) GC,WH, Property, Vendor", "Flame Cert.", "Finish Sample Requested",
    "Shop Drawing due from Vendor", "Shop Drawing Final Sign off Due from Design",
    "Fabric Sample/Strike-Off", "Seaming Diagram", "Hardware Sample", "Production Start Date",
    "Production End Date", "Target Ship Date", "Actual Ship Date", "Target Port Arrival Date",
    "Actual Port Arrival Date", "Port Pick-Up Date", "Next Call Hidden", "Last Call to Vendor",
    "Next Call to Vendor", "WH RR#", "Comments", "Completed\n(Yes/No)",
]

# The same workbook, sheet `qPOExpeditor` — a financial tab carrying Spitfire's own keys.
QPO_EXPEDITOR = [
    "Title", "Project", "Prop_Name", "proj_name", "AccountCategory", "Spec", "Area", "Qty",
    "orig_exp", "SCLineAmount", "Paid_Amnt", "perc_paid", "SourceContact", "DocNo", "DocDate",
    "Due", "DocItemKey", "POLinkedItemKey", "POLinkedDocKey",
]

# `2026.03.30 ES WPB Inventory vs Spec Index`, sheet `Warehouse` — receipts, no lifecycle.
WAREHOUSE_INVENTORY = [
    "ITEM #", "VENDOR", "DESCRIPTION", "PS2 RR #", "ES AUSTIN RR #", "QTY REC'D", "UOM",
    "QTY AVAILABLE", "NOTES",
]

# `Deer Valley Resort LLC, Park Peak (PP) Vendor Bid Analysis` — priced lines, never a receipt.
VENDOR_BID_ANALYSIS = [
    "Design Firm", "Area", "Item Number", "Item Description", "UOM", "Quantity",
    "Selected Vendor Per Item", "Extended Price for Selected Vendor", "Unit Price",
    "Extended Price",
]


def test_the_real_confirmation_grid_is_a_delivery_document():
    assert receipt.classify_grid(CONFIRMATION_GRID)[0] == receipt.DELIVERY_DOCUMENT


def test_a_warehouse_inventory_with_qty_received_is_a_delivery_document():
    assert receipt.classify_grid(WAREHOUSE_INVENTORY)[0] == receipt.DELIVERY_DOCUMENT


def test_an_expediting_report_is_a_status_report_despite_qty_delivered():
    """The regression this module exists for.

    The sheet carries `Qty Delivered` and `Actual Delivery Date`, so every receipt-column test
    passes on it. It is still a tracker, and reading it as receipts staged 2,850 records.
    """
    kind, reason = receipt.classify_grid(EXPEDITING_REPORT)
    assert kind == receipt.STATUS_REPORT
    assert "lifecycle" in reason


def test_a_system_export_is_rejected_on_its_key_columns():
    kind, reason = receipt.classify_grid(QPO_EXPEDITOR)
    assert kind == receipt.STATUS_REPORT
    assert "system export" in reason


def test_a_priced_bid_analysis_is_rejected_as_a_commercial_document():
    """It has the same Item/Description/Qty/UOM shape as Premier's request table and, like it,
    no receipt column — so only the unit prices separate them."""
    kind, reason = receipt.classify_grid(VENDOR_BID_ANALYSIS)
    assert kind == receipt.STATUS_REPORT
    assert "priced document" in reason


def test_a_request_table_is_not_rejected_for_having_no_receipt_column():
    """Premier's 5-Star request table is the question, not the answer. Its rows must still be
    read as open questions — `tests/test_receipt_gate.py` covers what may not become a receipt."""
    header = ["Description of Item", "SPEC # or Phase Code", "UOM", "Qty", "P.O.#", "Vendor"]
    assert receipt.classify_grid(header)[0] == receipt.NO_RECEIPT_COLUMN


def test_a_short_confirmation_column_still_counts_as_a_receipt_column():
    """`Confirmed Y/N` is too far from `Confirmed Received` for the fuzzy header match to
    bridge, so it is listed explicitly. A tracker headed this way is a receipt document."""
    assert receipt.has_receipt_column(["PO", "Spec", "Qty", "Confirmed Y/N"])


def test_one_planning_column_does_not_make_a_receipt_sheet_a_report():
    """`Estimated Delivery` beside `Actual Delivery` is a reasonable thing to put on a receipt
    sheet. Three distinct lifecycle columns is the point where it stops being one."""
    header = ["PO", "Spec", "Qty", "Qty Delivered", "Estimated Delivery Date"]
    assert receipt.classify_grid(header)[0] == receipt.DELIVERY_DOCUMENT


# --- the verdict is kept, not only used -----------------------------------------------------


def _source():
    from pipeline.stage3_extract.base import ExtractionSource
    return ExtractionSource(source_email_id="m1", email_date="2026-09-10T00:00:00Z",
                            source_type="attachment", filename="report.xlsx", ledger_id=7)


def test_a_refused_sheet_leaves_its_verdict_on_the_source():
    """The gate used to compute this and throw it away, so the queue could not say why."""
    from pipeline.stage3_extract import grid_reader

    source = _source()
    rows = [EXPEDITING_REPORT, ["a chair", "GR-101-CH", "EA", "4", "4"] + [""] * 31]
    grid_reader.records_from_rows(source, rows, "excel: EXPEDITING REPORT")

    assert source.grid_verdicts
    label, kind, reason = source.grid_verdicts[0]
    assert label == "excel: EXPEDITING REPORT"
    assert kind == receipt.STATUS_REPORT
    assert "lifecycle" in reason


def test_the_verdict_is_kept_even_when_the_sheet_does_produce_records():
    """The case a record-less implementation misses.

    `ANS-026 ... Expediting Report.xlsx` yields 1,122 records from its confirmation grid. Keeping
    the verdict only on the path that returns `[]` would miss exactly the largest reports.
    """
    from pipeline.stage3_extract import grid_reader

    source = _source()
    rows = [CONFIRMATION_GRID, ["a chair", "GR-101-CH", "EA", "4", "4", "0", "214287",
                                "Vendor", "2026-09-01", "WH", "", "YES"]]
    records = grid_reader.records_from_rows(source, rows, "excel:Hoja1")

    assert records, "the confirmation grid must still produce records"
    assert source.grid_verdicts[0][1] == receipt.DELIVERY_DOCUMENT


def test_a_workbook_recording_a_receipt_is_never_flagged():
    """A tracker sitting beside a receiver does not make the file a tracker."""
    from pipeline import attachment_ledger

    mixed = [("excel:Expediting", receipt.STATUS_REPORT, "a system export"),
             ("excel:Hoja1", receipt.DELIVERY_DOCUMENT, "records a receipt")]
    assert attachment_ledger.status_report_verdict(mixed) == ""


def test_a_workbook_of_trackers_carries_the_reason_in_the_rules_own_words():
    from pipeline import attachment_ledger

    verdicts = [("excel: EXPEDITING REPORT", receipt.STATUS_REPORT,
                 "a status tracker — 22 lifecycle columns")]
    reason = attachment_ledger.status_report_verdict(verdicts)
    assert reason == "the sheet 'EXPEDITING REPORT' is a status tracker — 22 lifecycle columns"


def test_absence_of_a_receipt_column_is_not_enough_to_flag():
    """`no_receipt_column` is absence of evidence — the same thing triage declines to act on for
    `Intent.NEITHER`. Acting on it would sweep up packing slips whose header did not parse."""
    from pipeline import attachment_ledger

    verdicts = [("excel:Sheet1", receipt.NO_RECEIPT_COLUMN, "no column records a receipt")]
    assert attachment_ledger.status_report_verdict(verdicts) == ""
