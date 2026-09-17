"""A table row read into the right fields — gap G14, cases A, C and E.

Measured on the live store 2026-09-15: 130 pending records came from table rows read into the
wrong fields. A quantity or a unit word landed in the spec (`"1"`, `"Each"`), a vendor's own SKU was
taken for Premier's spec while the real spec sat later in the same row, and header, total and
address rows (`Grand Total`, `Item Description: Area Name:`, `Shipping Address: …`) were staged as
delivered goods.

Every value below is invented. No real purchase order, vendor or property appears here.
"""

from pipeline.stage3_extract.base import ExtractionSource, build_record_from_row, is_item_row
from pipeline.stage3_extract.html_adapter import HtmlAdapter
from pipeline.stage3_extract.ocr_adapter import OcrResult, records_from_ocr_result


def _source(**overrides):
    defaults = dict(source_email_id="msg-1", email_date="2026-06-08T14:00:00Z",
                    source_type="attachment")
    defaults.update(overrides)
    return ExtractionSource(**defaults)


# --- case A and E: the spec column holds something that is not a spec ------------------------------

def test_a_quantity_in_the_spec_column_gives_way_to_the_real_spec_in_the_row():
    """Case A. The columns were misaligned by one blank cell, so the spec column read the quantity."""
    row = ["900101", "Credenza @ Entry", "Example Furniture Co", "", "LOB-105-CG",
           "2340.00", "1", "2340.00", "", "EA"]
    column_map = {"po_number": 0, "item_description": 1, "spec_code": 6, "quantity_received": 6}
    record = build_record_from_row(_source(), row, column_map, "pdf")
    assert record.spec_code == "LOB-105-CG"
    assert record.po_number == "900101"


def test_a_unit_word_is_never_a_spec():
    """Case E. `Each` and `Window` were staged as spec codes."""
    row = ["10", "Each", "", "Sheer Roller Shade 70W x 71L", "WIN-310-SH"]
    column_map = {"quantity_received": 0, "spec_code": 1, "item_description": 3}
    record = build_record_from_row(_source(), row, column_map, "html")
    assert record.spec_code == "WIN-310-SH"


def test_a_vendor_sku_gives_way_to_premiers_spec_later_in_the_row():
    """Case E. The vendor's own item code sat in the column mapped as spec."""
    row = ["1", "VND_SKU_01", "Table Lamp 16in Opal", "1", "LOB-400-LT"]
    column_map = {"quantity_received": 0, "spec_code": 1, "item_description": 2}
    record = build_record_from_row(_source(), row, column_map, "docx")
    assert record.spec_code == "LOB-400-LT"


def test_a_row_with_no_spec_anywhere_leaves_the_spec_empty_rather_than_inventing_one():
    row = ["1", "Vienna", "Botanical", "55501234", "21.3"]
    column_map = {"quantity_received": 0, "item_description": 1, "spec_code": 3}
    record = build_record_from_row(_source(), row, column_map, "pdf")
    assert not record.spec_code, "a catalogue number is not a spec, and nothing else in the row is one"


def test_a_document_number_elsewhere_in_the_row_is_not_taken_for_the_spec():
    """Found by the control set: with the spec cell cleared, a receiving-report number `RR-28` two
    cells over has the spec's shape and was taken instead. A real spec carrying the same letters —
    `RR-701-MR`, a restroom item — must still be read."""
    row = ["10 pallets", "904-TV", "RR-28", "100"]
    column_map = {"item_description": 0, "spec_code": 1, "quantity_received": 3}
    # `904-TV` is kept — the mirror holds `GR-904-TV` and `STE-904-TV`, so an unfamiliar code is far
    # more likely a spec written short than it is noise. What must not happen is the report number
    # two cells over being pulled in over it.
    assert build_record_from_row(_source(), row, column_map, "ocr").spec_code == "904-TV"

    row = ["Mirror", "n/a", "RR-701-MR", "6"]
    assert build_record_from_row(_source(), row, column_map, "ocr").spec_code == "RR-701-MR"


def test_a_real_line_with_no_spec_stays_as_an_incomplete_record_instead_of_vanishing():
    """Found by the control set. `Alarm Clocks | 9` carried the spec `1` and `New Mount | 2` the spec
    `TBD`. Clearing those correctly left rows naming neither PO nor spec — which the table reader
    then dropped outright, so a delivered line disappeared instead of waiting for a person."""
    table = [
        ["Spec", "Description", "Qty"],
        ["1", "Alarm Clocks", "9"],
        ["TBD", "New Mount @ Existing TV", "2"],
    ]
    records = records_from_ocr_result(_source(), OcrResult(tables=[table], raw_text="", confidence=0.9),
                                      extraction_source="pdf", confidence_cap=None)
    assert [(r.spec_code, r.item_description, r.quantity_received) for r in records] == [
        (None, "Alarm Clocks", 9.0), (None, "New Mount @ Existing TV", 2.0)]


def test_an_item_name_misread_into_the_spec_column_becomes_the_description():
    """Found store-wide: 256 lines carried the item's name in the spec column and nothing in the
    description column. Clearing the spec threw away the only thing saying what the item was."""
    row = ["24424 *EXAMPLE DESK 130lbs", "", "1"]
    column_map = {"spec_code": 0, "item_description": 1, "quantity_received": 2}
    record = build_record_from_row(_source(), row, column_map, "pdf")
    assert not record.spec_code
    assert record.item_description == "24424 *EXAMPLE DESK 130lbs"


def test_a_name_in_the_spec_column_on_a_row_with_no_quantity_does_not_create_a_record():
    """Found store-wide: moving the name across on rows with no quantity made 142 records that had
    never been staged — `via email`, a mill's name, a plan reference. Such a row named a spec and
    nothing else, so the reader dropped it before; correcting fields must not start staging it."""
    table = [
        ["PO", "Spec", "Description", "Qty"],
        ["900101", "via email", "", ""],
        ["", "Example Textiles", "", ""],
    ]
    records = records_from_ocr_result(_source(), OcrResult(tables=[table], raw_text="", confidence=0.9),
                                      extraction_source="pdf", confidence_cap=None)
    assert records == []


def test_a_unit_word_or_placeholder_in_the_spec_column_is_not_a_description():
    column_map = {"spec_code": 0, "item_description": 1, "quantity_received": 2}
    for junk in ("Each", "TBD", "N/A"):
        record = build_record_from_row(_source(), [junk, "", "2"], column_map, "pdf")
        assert not record.item_description, junk


def test_a_description_already_in_its_column_is_not_replaced_by_the_rejected_spec():
    row = ["VND_SKU_01", "Table Lamp", "1"]
    column_map = {"spec_code": 0, "item_description": 1, "quantity_received": 2}
    assert build_record_from_row(_source(), row, column_map, "pdf").item_description == "Table Lamp"


def test_a_line_that_existed_before_with_a_description_but_no_quantity_is_kept():
    """Found store-wide: 394 lines like `Corridor Broadloom` had a junk spec, a description and no
    quantity. They existed before this change and it must not delete them — only correct them."""
    table = [
        ["Spec", "Description", "Qty"],
        ["300101", "Corridor Broadloom", ""],
    ]
    records = records_from_ocr_result(_source(), OcrResult(tables=[table], raw_text="", confidence=0.9),
                                      extraction_source="pdf", confidence_cap=None)
    assert [(r.spec_code, r.item_description) for r in records] == [(None, "Corridor Broadloom")]


def test_a_line_with_an_empty_spec_column_is_still_dropped_as_before():
    """The keep rule is narrow on purpose. A row whose spec column was simply empty never survived
    the reader's guard, and must not start to: widening it was measured at 628 new records across 77
    documents that had never been staged."""
    table = [
        ["Spec", "Description", "Qty"],
        ["", "Service Parts Hose Kit", "15"],
    ]
    records = records_from_ocr_result(_source(), OcrResult(tables=[table], raw_text="", confidence=0.9),
                                      extraction_source="pdf", confidence_cap=None)
    assert records == []


def test_a_real_spec_in_the_spec_column_is_kept_as_it_is():
    """The existing behaviour, which must not move: a spec-shaped cell is the spec."""
    row = ["900101", "CTP-025-NA", "3"]
    column_map = {"po_number": 0, "spec_code": 1, "quantity_received": 2}
    assert build_record_from_row(_source(), row, column_map, "pdf").spec_code == "CTP-025-NA"


def test_a_lowercase_spec_is_still_a_spec():
    row = ["900101", "lob-105-cg", "2"]
    column_map = {"po_number": 0, "spec_code": 1, "quantity_received": 2}
    assert build_record_from_row(_source(), row, column_map, "html").spec_code == "lob-105-cg"


def test_a_carton_marking_around_a_spec_is_still_read_as_that_spec():
    """`strip_package_marking` owns this shape. A cell *containing* a spec is never replaced."""
    row = ["900101", "LOB-400-LT-1/1 of 2", "1"]
    column_map = {"po_number": 0, "spec_code": 1, "quantity_received": 2}
    record = build_record_from_row(_source(), row, column_map, "pdf")
    assert record.parent_spec_code == "LOB-400-LT"


# --- case C: rows that are not item lines -------------------------------------------------------

def test_a_total_row_is_not_an_item_line():
    assert not is_item_row(["Grand Total", "22", "2", "*", "$600", "$12,040", "$12,639"])
    assert not is_item_row(["Subtotal", "", "4", "$1,200"])
    assert not is_item_row(["", "Total:", "12"])


def test_a_label_row_with_no_spec_is_not_an_item_line():
    assert not is_item_row(["Item Description: Area Name:", "Curved Sofas at Green RM (3 Units) 04- LOBBY"])


def test_an_address_or_order_information_block_is_not_an_item_line():
    assert not is_item_row(["Shipping Address: 100 Example Way, Springfield"])
    assert not is_item_row(["Ship To:", "Example Hotel", "100 Example Way"])
    assert not is_item_row(["Order Information"])


def test_an_ordinary_item_row_is_an_item_line():
    assert is_item_row(["900101", "Credenza @ Entry", "LOB-105-CG", "1", "EA"])
    # A label-shaped first cell is fine when the row names a spec — the label is just a prefix.
    assert is_item_row(["PO#: 900101", "LOB-105-CG", "2"])


def test_an_empty_row_is_not_an_item_line():
    assert not is_item_row(["", "  ", None])


# --- the readers apply it -------------------------------------------------------------------------

def test_the_html_reader_stages_no_record_from_a_total_or_address_row():
    """The generic table path — a plain `Qty`, not a confirmation grid, so nothing upstream filters
    the rows. Before `is_item_row` both extra rows here were staged as goods."""
    html = """
    <table>
      <tr><th>PO</th><th>Spec</th><th>Description</th><th>Qty</th></tr>
      <tr><td>900101</td><td>LOB-105-CG</td><td>Credenza</td><td>1</td></tr>
      <tr><td>Grand Total</td><td></td><td></td><td>1</td></tr>
      <tr><td>Shipping Address: 100 Example Way</td><td></td><td></td><td>2</td></tr>
    </table>"""
    records = HtmlAdapter().extract(_source(source_type="body", body_html=html))
    assert [r.spec_code for r in records] == ["LOB-105-CG"]


def test_the_shared_table_reader_stages_no_record_from_a_label_or_address_row():
    """`records_from_ocr_result` is the path PDF text tables, OCR tables and replayed parses take.

    Its existing guard drops a row naming neither PO nor spec, so these rows carry a PO — which is
    exactly how the live ones survived: `Shipping Address: … PO 900101 …` reads as a purchase order
    to the regex fallback."""
    table = [
        ["PO", "Spec", "Description", "Qty"],
        ["900101", "LOB-105-CG", "Credenza", "1"],
        ["Shipping Address: PO 900101, 100 Example Way", "", "", "2"],
        ["Item Description: Area Name: PO 900101", "", "Curved Sofas at Green RM", "3"],
    ]
    records = records_from_ocr_result(_source(), OcrResult(tables=[table], raw_text="", confidence=0.9),
                                      extraction_source="pdf", confidence_cap=None)
    assert [r.spec_code for r in records] == ["LOB-105-CG"]


# --- a spec that does not look like one is still a spec --------------------------------------------
# Measured on the mirror 2026-09-15: 122 of 1,386 real Spitfire spec codes (8.8%) do not have the
# `ABC-123` shape — `IT-EQ`, `P-01`, `Photo 1`, `Dryer 01`, `HydroKit-01`, `STE-201Ra-SGF`. Clearing
# every cell that fails the shape test threw those away, and took `ST650` off a real receipt with it.
# So the rule is narrowed: clear only what is certainly not a spec, and keep everything else.


def test_a_spec_that_does_not_have_the_usual_shape_is_left_alone():
    column_map = {"po_number": 0, "spec_code": 1, "quantity_received": 2}
    for real in ("ST650", "IT-EQ", "IT-COMPUTER", "P-01", "Photo 1", "Dryer 01", "HydroKit-01"):
        record = build_record_from_row(_source(), ["900101", real, "3"], column_map, "pdf")
        assert record.spec_code == real, real


def test_a_quantity_a_unit_word_or_a_report_number_is_still_cleared():
    """The three things that are certainly not a spec, whatever else a row holds."""
    column_map = {"po_number": 0, "spec_code": 1, "quantity_received": 2}
    for junk in ("1", "2.0", "300101", "Each", "EA", "Window", "TBD", "N/A", "RR-28", "WRR-17"):
        record = build_record_from_row(_source(), ["900101", junk, "3"], column_map, "pdf")
        assert not record.spec_code, junk


def test_an_unfamiliar_code_never_displaces_a_real_spec_sitting_in_the_same_row():
    """Keeping unfamiliar codes must not undo the vendor-SKU fix: when the row names a spec of the
    usual shape elsewhere, that one is Premier's and the mapped cell was the vendor's."""
    row = ["1", "VND_SKU_01", "Table Lamp 16in Opal", "1", "LOB-400-LT"]
    column_map = {"quantity_received": 0, "spec_code": 1, "item_description": 2}
    assert build_record_from_row(_source(), row, column_map, "docx").spec_code == "LOB-400-LT"


def test_a_sentence_in_the_spec_column_is_still_read_as_the_item_name():
    """Two or more real words is prose, not a code — `Photo 1` and `Dryer 01` have one apiece."""
    column_map = {"spec_code": 0, "item_description": 1, "quantity_received": 2}
    record = build_record_from_row(_source(), ["24424 *EXAMPLE DESK 130lbs", "", "1"],
                                   column_map, "pdf")
    assert not record.spec_code
    assert record.item_description == "24424 *EXAMPLE DESK 130lbs"
