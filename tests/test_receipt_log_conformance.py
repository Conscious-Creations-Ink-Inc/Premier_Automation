"""Our Receipt Log against Premier's own Spitfire export, cell for cell.

Premier reads this file beside the export it pulls from Spitfire, so "close enough" is not a
standard — a column at a different width or a stray note in a row Spitfire leaves blank is how a
reader starts wondering which of the two documents to believe.

Everything here is read out of the reference workbook at test time rather than hardcoded. That is
deliberate: when Premier sends a newer export, dropping it in re-points every assertion instead of
starting an argument about which numbers in this file are still true.

The reference is committed as a *sample*, not as data — it holds Premier's real PO history, which
is why the corpus fixtures below are used for our own side rather than anything from it.
"""

from io import BytesIO
from pathlib import Path

import openpyxl
import pytest

from pipeline import receipt_log

_DEV_REPORTS = Path(__file__).resolve().parent.parent.parent / "dev_reports"

# Searched in order, first hit wins. There is one reference, not three: all of these are the same
# 220,721-byte export (md5 355860b9b14af1966bed19a1946fad97). The list exists because it has moved
# twice — `SampleReceiverFiles/` was deleted on 2026-08-12 along with the `.msg` corpus, which took
# the whole conformance suite silently to "skipped" with it.
_CANDIDATES = (
    _DEV_REPORTS / "_Spitfire-LIVE_General_Receipt Log.xlsx",
    _DEV_REPORTS / "SampleReceiverFiles" / "SampleReceiverFiles"
    / "_Spitfire-LIVE_General_Receipt Log.xlsx",
    _DEV_REPORTS.parent / "Documents" / "Premier" / "5,8 june"
    / "_Spitfire-LIVE_General_Receipt Log (1).xlsx",
)

REFERENCE = next((path for path in _CANDIDATES if path.exists()), _CANDIDATES[0])

# The reference lives outside the repo, so these skip rather than fail on a checkout without it.
# `test_the_reference_is_where_this_file_expects_it` is what stops that skip from becoming silence.
pytestmark = pytest.mark.skipif(not REFERENCE.exists(), reason=f"reference not at {REFERENCE}")


def test_the_reference_is_where_this_file_expects_it():
    """A guard on the guard: every other test here skips without the reference, so without this one
    a moved file would turn the whole conformance suite green by doing nothing."""
    assert REFERENCE.exists(), (
        f"{REFERENCE} is missing — if Premier's export moved, re-point REFERENCE rather than "
        f"deleting these tests"
    )


@pytest.fixture(scope="module")
def theirs():
    return openpyxl.load_workbook(REFERENCE)["Receipt Log"]


@pytest.fixture(scope="module")
def ours():
    """Our workbook, built from one purchase order — enough to exercise every row type."""
    report = receipt_log.Report(
        purchase_orders=[receipt_log.PurchaseOrder(
            po_number="908491", vendor="Light Annex", title="PO 908491 Lighting",
            lines=[receipt_log.Line(
                line_number=300, spec="STE-402-LT-B", description="BASE, Floor Lamp 2",
                received=1.0, uom="EA",
                receipts=[receipt_log.Receipt(reference="939475", date="2025-10-09",
                                              quantity=1.0, receiver="Jordan T.")],
            )],
        )],
        generated_at="2026-08-11 12:00",
    )
    return openpyxl.load_workbook(BytesIO(receipt_log.to_xlsx(report)))["Receipt Log"]


def test_the_sheet_is_named_as_theirs_is(theirs, ours):
    assert ours.title == theirs.title == receipt_log.SHEET_NAME


def test_the_header_row_matches_label_for_label(theirs, ours):
    """Row 5, nine labels, exact columns. Trailing spaces included — "Received " and "Final " carry
    them in Spitfire's export, and a diff tool will show them even if a reader will not."""
    for column in range(1, 13):
        assert ours.cell(5, column).value == theirs.cell(5, column).value, f"column {column}"


def test_the_same_columns_are_given_an_explicit_width(theirs, ours):
    """Membership, not width. `column_dimensions` is a defaultdict: reading a column the file never
    set invents one at 13.0, so comparing widths alone would pass while we sized two columns the
    reference deliberately leaves alone."""
    assert set(ours.column_dimensions) == set(theirs.column_dimensions)
    assert "D" not in theirs.column_dimensions and "G" not in theirs.column_dimensions


def test_column_widths_match_at_full_precision(theirs, ours):
    """Widths are what make the two files line up when read side by side. Compared exactly rather
    than rounded: 22.3 and 22.28515625 are different columns."""
    for column in sorted(theirs.column_dimensions):
        assert ours.column_dimensions[column].width == theirs.column_dimensions[column].width, \
            f"column {column}"


def test_the_structural_merges_match(theirs, ours):
    """The merges are the sheet's skeleton: the Description pair, the Received pair, the group row
    band, and the PO-title band. Compared as a subset because the reference has 2,352 rows of them
    and ours has one purchase order's worth."""
    theirs_merges = {str(m) for m in theirs.merged_cells.ranges}
    ours_merges = {str(m) for m in ours.merged_cells.ranges}
    for merge in ("D5:E5", "G5:H5", "A7:F7", "C8:E8", "D10:E10"):
        assert merge in theirs_merges, f"{merge} is not in the reference — re-derive this list"
        assert merge in ours_merges, merge


def test_the_top_row_heights_match(theirs, ours):
    for row in (1, 2, 5, 7, 8):
        assert ours.row_dimensions[row].height == theirs.row_dimensions[row].height, f"row {row}"


def test_rows_three_and_four_are_empty_as_they_are_in_the_reference(theirs, ours):
    """They used to carry a generated-on stamp and a note about the blank columns. Both belong on
    screen; inside the file they are two lines Spitfire's own export does not have, in a document
    Premier forwards to people who will read it as Spitfire's."""
    for row in (3, 4):
        for column in range(1, 13):
            assert theirs.cell(row, column).value is None, f"reference r{row}c{column} changed"
            assert ours.cell(row, column).value is None, f"ours r{row}c{column}"


def test_the_columns_we_cannot_know_are_blank_not_zero(ours):
    """Order Qty (F) and Final (J) on the line row. A zero here is a claim — that nothing was
    ordered, or that the line is closed — and both would be false."""
    assert ours.cell(9, 6).value is None, "Order Qty must be blank, not 0"
    assert ours.cell(9, 10).value is None, "Final must be blank"
    assert ours.cell(9, 9).value is None, "Net is unknowable while Order Qty is"


def test_a_receipt_row_carries_the_reference_date_quantity_and_receiver(ours):
    assert ours.cell(11, 4).value == "939475"
    assert ours.cell(11, 7).value == 1.0
    assert ours.cell(11, 11).value == "Jordan T."


def test_receipt_dates_use_the_reference_number_format(theirs, ours):
    """A date rendered as 45939 is not a date. The format string is lifted from the reference so
    the two files sort and display identically."""
    assert receipt_log._DATE_FORMAT == theirs.cell(11, 6).number_format
    assert ours.cell(11, 6).number_format == receipt_log._DATE_FORMAT


def test_net_appears_once_order_qty_does():
    """The whole point of deriving Net rather than storing it: no edit to this module is needed on
    the day the Spitfire read lands."""
    report = receipt_log.Report(purchase_orders=[receipt_log.PurchaseOrder(
        po_number="908491", vendor="Light Annex", title="PO 908491 Lighting",
        lines=[receipt_log.Line(line_number=300, spec="SP", description="d",
                                received=2.0, order_qty=5.0)],
    )])
    sheet = openpyxl.load_workbook(BytesIO(receipt_log.to_xlsx(report)))["Receipt Log"]
    assert sheet.cell(9, 6).value == 5.0
    assert sheet.cell(9, 7).value == 2.0
    assert sheet.cell(9, 9).value == 3.0
    assert sheet.cell(9, 10).value is None, "Final is still Spitfire's to decide"
