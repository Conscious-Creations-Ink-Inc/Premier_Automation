"""What the OCR path makes of a real warehouse receiving report.

Azure read this document well — 13 clean tables. `records_from_ocr_result` then threw most of it
away and kept the wrong parts: it took the header row of every table that mapped *anything*, so a
freight bill's `Qty | Pkg | HM | Description | Weight` band staged "2 PLT RACK MOUNTS AND
ACCESSORIES" as goods received, next to a liftgate charge and a prepaid total. Meanwhile
`Date Received | 8/14/26` sat two tables up the same page and was never read, so all ten records
staged from this document failed `completeness` on the one field no other system holds.

The tables in `fixtures_wrr_tables.json` are the real ones, lifted verbatim out of
`parsed_documents` — tables 2, 3, 4, 7 and 11 of the document that produced that pilot.
"""

import json
from pathlib import Path

import pytest

from pipeline import completeness
from pipeline.ingest_orchestrator import reconcile_cross_source_duplicates
from pipeline.stage3_extract.base import ExtractionSource, find_header_row
from pipeline.stage3_extract.ocr_adapter import (
    OcrResult,
    records_from_ocr_result,
    strip_selection_marks,
)

WRR_TABLES = json.loads((Path(__file__).parent / "fixtures_wrr_tables.json").read_text("utf-8"))


def wrr_records(tables=None):
    source = ExtractionSource(
        source_email_id="msg-wrr-1", email_date="2026-08-26T10:00:00Z",
        source_type="attachment", filename="Sheraton.211373.WRR-17.pdf", content_bytes=b"x",
    )
    return records_from_ocr_result(source, OcrResult(tables=tables or WRR_TABLES, raw_text=""))


def test_the_delivery_date_printed_on_the_form_reaches_every_record():
    """`Date Received | 8/14/26` — the field whose absence made all ten pilot records incomplete."""
    records = wrr_records()

    assert records, "the item grid produced nothing at all"
    assert all(r.pod_stated_date == "2026-08-14" for r in records), \
        [r.pod_stated_date for r in records]


def test_the_shipment_band_is_read_once_and_stamped_on_the_lines():
    """Carrier, receiver and tracking number are stated above the grid, never per line."""
    record = wrr_records()[0]

    assert record.received_by == "Randy"
    assert record.carrier_name == "Lunden."
    assert "3041310" in (record.tracking_number or "")


def test_the_freight_bill_is_not_read_as_goods_received():
    """`2 PLT RACK MOUNTS AND ACCESSORIES` is how the shipment travelled, not what arrived."""
    descriptions = " | ".join(str(r.item_description or "") for r in wrr_records())

    assert "RACK MOUNTS" not in descriptions.upper()
    assert "LIFTGATE" not in descriptions.upper()
    assert "TOTAL PREPAID" not in descriptions.upper()


def test_the_delivered_item_survives_with_its_quantity():
    """The one real receipt on the page: 373 SmartMount ST650."""
    st650 = [r for r in wrr_records() if (r.spec_code or "").startswith("ST650")]

    assert st650, "the delivered item was dropped"
    assert any(r.quantity_received == 373.0 for r in st650)


def test_the_document_yields_a_complete_record():
    """The measure of the whole change: before it, zero of ten records could be posted."""
    records = wrr_records()
    for record in records:
        record.po_number = record.po_number or "212749"

    complete = [r for r in reconcile_cross_source_duplicates(records)
                if completeness.is_complete(r)]

    assert len(complete) == 1, [completeness.gaps(r).missing_required for r in records]
    assert complete[0].spec_code == "ST650"
    assert complete[0].quantity_received == 373.0


def test_ordered_quantity_is_never_read_as_received():
    """The vendor grid puts `Ordered Qty` and `Delivered Qty` side by side.

    Reading the ordered figure as received is the defect behind records 131-133, where 196 ordered
    was staged as 196 received. The delivered column is index 4; the ordered column is index 2 and
    must map to nothing.
    """
    headers = ["No.", "Item / Lotserial NBR", "Ordered Qty", "UOM", "Delivered Qty",
               "Remaining to Deliver"]
    located = find_header_row([headers, ["1", "ST650", "500.0000", "EA", "373.0000", "127.0000"]])

    assert located is not None
    _, column_map = located
    assert column_map["quantity_received"] == 4, "read the ordered column as received"


def test_a_partly_shipped_line_reports_what_was_delivered():
    """500 ordered, 373 delivered: the receipt is 373."""
    table = [["No.", "Item / Lotserial NBR", "Ordered Qty", "UOM", "Delivered Qty",
              "Remaining to Deliver"],
             ["1", "ST650", "500.0000", "EA", "373.0000", "127.0000"]]

    record = wrr_records([table])[0]

    assert record.quantity_received == 373.0


@pytest.mark.parametrize("raw, expected", [
    ("ST650\n:selected:", "ST650"),
    ("RR - 17.\n:unselected:", "RR - 17."),
    (":selected: 12", "12"),
    ("no marks here", "no marks here"),
    ("", ""),
])
def test_checkbox_markers_are_taken_out_of_cell_text(raw, expected):
    """Document Intelligence writes a tick inline; fused to a spec it matches no purchase order."""
    assert strip_selection_marks(raw) == expected


def test_a_tick_does_not_split_one_item_into_two_records():
    """`ST650` and `ST650 :selected:` are the same item said twice, and must fold to one."""
    records = wrr_records()
    for record in records:
        record.po_number = record.po_number or "212749"

    st650 = [r for r in reconcile_cross_source_duplicates(records)
             if (r.spec_code or "").startswith("ST650")]

    assert len(st650) == 1, [r.spec_code for r in st650]


def test_a_table_that_names_no_item_is_skipped_entirely():
    """An hours grid maps a description and nothing else — words in a table are not a delivery."""
    table = [["No. Of Workers", "Start Time", "End Time", "Work Description", "Notes"],
             ["2", "08:00", "12:00", "Open and inspect", ""]]

    assert wrr_records([table]) == []
