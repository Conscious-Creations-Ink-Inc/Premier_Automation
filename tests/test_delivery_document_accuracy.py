"""What a delivery document is allowed to become a receipt line.

Every case here is a shape taken from a real warehouse receiving report and its attachments, with
the purchase orders, spec codes, property and vendor names replaced — CLAUDE.md §3 forbids
production data in tests. The *shapes* are what matter and they are exact:

- an item grid that lists one spec on several rows, because the item arrived on several skids;
- marginal handwriting that Document Intelligence returns as rows of that same grid;
- a page with no grid at all, where a regex over running text is the only thing left;
- the same document attached twice, once as the warehouse's own PDF and once as a scan of the
  signed copy;
- a packing slip that heads a column `Customer Order Number` over a number that is six digits long
  and is not a purchase order.

Each of those staged something wrong in the live store. The counts in the docstrings below are
measured, not estimated.
"""

import pytest

from pipeline.stage3_extract.base import (
    ExtractionSource,
    build_record_from_row,
    map_headers,
    quantity_from_text,
)
from pipeline.stage3_extract.ocr_adapter import OcrResult, records_from_ocr_result

PO = "990001"
KNOWN_POS = frozenset({PO})

# The shipment-level band every receiving report carries above its items, and the item grid itself.
# Three rows of AAA-100-EQ are three skids of one item; the fourth row is a different spec. The last
# two rows are the reader's rendering of handwriting in the margin: a spec-shaped cell with no
# quantity and no description anywhere on the row.
_DELIVERY_BAND = [
    ["Carrier", "Vendor / Shipper's Name", "Date Received", "Received by"],
    ["ACME FREIGHT", "Example Supply /", "8/14/2026", "J. TESTER"],
]
_TRACKING_BAND = [
    ["BOL, PRO, Or Tracking #", "Total Weight of Shipment", "Total Received"],
    ["7770001", "1000.00", "4 SKID"],
]
_ITEM_GRID = [
    ["PO #", "Description\n(such as Desk, Chair, Lamp)",
     "Carton Markings\n(Item#/Mfg#/Serial#)", "Qty", "Weight"],
    [PO, "Wall Mount: XY999", "AAA-100-EQ////105 PER SKID", "210.00", "11.143"],
    [PO, "Wall Mount: XY999", "AAA-100-EQ", "75.00", "11.187"],
    [PO, "Wall Mount: XY999", "AAA-100-EQ", "65.00", "0.000"],
    [PO, "Wall Mount: XY999", "BBB-200-EQ", "23.00", "43.130"],
    ["", "", "? BBB-200-EQ?", "", ""],
    ["", "", "PO#990001.", "", ""],
    ["", "", "AAA-100-EQ\n:selected:", "", ""],
]

# The carrier's freight bill, bound in behind the report. Its `Qty.` column counts pallets and
# hundredweight, and `is_usable_column_map` is what keeps it out — there is no PO or spec column.
_FREIGHT_BILL = [
    ["Qty.", "Pkg", "HM", "Description", "Weight (lbs)"],
    ["2", "PLT", "", "RACK MOUNTS AND ACCESSORIES", "2340"],
    ["4171", "CWT", "", "Liftgate Charge", ""],
    ["4", "", "", "TOTAL PREPAID", "4171"],
]


def _read(tables, *, ledger_id=1, raw_text=""):
    source = ExtractionSource(
        source_email_id="<synthetic@test>", email_date="2026-08-28T19:09:22Z",
        source_type="attachment", filename="receiving-report.pdf",
        ledger_id=ledger_id, known_po_numbers=KNOWN_POS,
    )
    records = records_from_ocr_result(
        source, OcrResult(tables=tables, raw_text=raw_text, confidence=0.8))
    return source, records


def test_every_row_of_the_item_grid_becomes_its_own_record():
    """Three skids of one spec are three rows, and all four quantities survive verbatim.

    The report states 210, 75, 65 and 23; nothing here may round, merge or reinterpret them.
    """
    _, records = _read([_DELIVERY_BAND, _TRACKING_BAND, _ITEM_GRID])

    assert [r.quantity_received for r in records] == [210.0, 75.0, 65.0, 23.0]
    assert [r.spec_code for r in records] == [
        "AAA-100-EQ////105 PER SKID", "AAA-100-EQ", "AAA-100-EQ", "BBB-200-EQ"]
    assert {r.po_number for r in records} == {PO}
    assert {r.item_description for r in records} == {"Wall Mount: XY999"}


def test_the_delivery_band_above_the_grid_reaches_every_line():
    """`pod_stated_date` is one of the five fields `completeness` demands and the one no other
    system holds. It is stated once, above the items, so a line-by-line reader never sees it."""
    _, records = _read([_DELIVERY_BAND, _TRACKING_BAND, _ITEM_GRID])

    assert {r.pod_stated_date for r in records} == {"2026-08-14"}
    assert {r.received_by for r in records} == {"J. TESTER"}
    assert {r.carrier_name for r in records} == {"ACME FREIGHT"}


def test_a_row_with_no_quantity_and_no_description_is_not_a_line_item():
    """Handwriting in the margin comes back as rows of the grid.

    `? BBB-200-EQ?` and `PO#990001.` are spec-shaped and nothing else — no quantity beside them, no
    description. Staged, each became a delivered line against a real purchase order that a person
    then had to dismiss by hand. 89 such records are in the live store and none has ever been
    completable.
    """
    _, records = _read([_DELIVERY_BAND, _TRACKING_BAND, _ITEM_GRID])

    assert len(records) == 4, "only the four real item rows"
    for record in records:
        assert record.quantity_received is not None
        assert record.item_description


def test_a_checkbox_token_never_reaches_a_spec_code():
    """Document Intelligence renders a tick as `:selected:`, inline in the cell beside it. Fused
    onto a spec it matches no purchase order line and never will."""
    _, records = _read([_DELIVERY_BAND, _TRACKING_BAND, _ITEM_GRID])

    assert not any(":selected:" in (r.spec_code or "") for r in records)
    assert not any("\n" in (r.spec_code or "") for r in records)


def test_the_carriers_freight_bill_is_not_an_item_grid():
    """`Qty | Pkg | HM | Description | Weight` names no PO and no spec, so it is not a list of
    goods received — it is a list of charges. Read as one it staged a liftgate charge of 4171 and
    a prepaid total of 4 as deliveries."""
    _, records = _read([_DELIVERY_BAND, _TRACKING_BAND, _FREIGHT_BILL])

    assert records == []


# --- running text, where there is no grid to read ----------------------------

@pytest.mark.parametrize("text, reason", [
    ("WRR- 17 / PH 3/4 + 4/ 4.\nSPEC/ITEM #\nAAA-100-EQ/ BBB-200-EQ.", "a tag reference"),
    ("Page: 1 of 4\nBBB-200-EQ", "a page number"),
    ("Issue Date: 10/22 Area Name:\nAAA-100-EQ", "a date"),
    ("PLAN: Corner Sectional 1:8 (1-1/2'=1' 0\")\nAAA-100-EQ", "a drawing scale"),
])
def test_a_spec_mentioned_beside_a_number_is_not_a_delivery(text, reason):
    """A page with no grid falls to a regex over its prose, and prose is full of numbers that are
    not quantities. Every string here produced a staged record in the live store, each one a spec
    the page merely mentions at a quantity taken from %s.

    529 records came from this pass and **not one has ever been complete.**
    """
    _, records = _read([], raw_text=text)

    assert records == []


def test_a_sentence_that_does_state_a_delivery_still_reads():
    """The gate is on evidence, not on formatting. A quantity that says what it is — labelled, or
    carrying a unit, or following a word meaning goods arrived — is still read."""
    assert quantity_from_text("received 11 of 12 chairs") == 11.0
    assert quantity_from_text("Qty: 23") == 23.0
    assert quantity_from_text("373 EA delivered") == 373.0
    # ...and a carton count is not an item count. That split is why the two fields exist.
    assert quantity_from_text("9 PLT - 3084.00 lb") is None


# --- the same document, attached twice ---------------------------------------

def test_a_scan_of_the_same_report_is_superseded_by_the_warehouses_own_pdf():
    """Atlas attaches the report its system generated *and* a scan of the signed copy. Both are
    read; the scan's read is the poorer one, and both used to stage records, so a clean parse
    arrived on the page beside a set of mangled twins.

    They are matched on what the document says about itself — the carrier reference and the
    delivery date — which survives the difference between `7770001` and `# 7770001`, `8/14/2026`
    and `8/14/26`.
    """
    from pipeline import evidence

    native_source, native = _read([_DELIVERY_BAND, _TRACKING_BAND, _ITEM_GRID], ledger_id=10)
    scanned_source, scanned = _read(
        [
            [["Carrier", "Vendor / Shipper's Name", "Date Received", "Received by"],
             ["Acme Freight.", "Example Supply /", "8/14/26", "J. Tester"]],
            [["BOL, PRO, Or Tracking #", "Total Weight of Shipment", "Total Received"],
             ["# 7770001", "1000 #", "4 pallets."]],
            [["Description", "Carton Markings", "Qty", "Weight"],
             ["Wall Mount.", "XY999", "373", "1000"]],
        ],
        ledger_id=11,
    )
    for record in native:
        record.extraction_source = "pdf"     # a text layer, not OCR

    assert evidence._document_key(native_source) == evidence._document_key(scanned_source)
    assert evidence._fidelity_of(native) > evidence._fidelity_of(scanned)

    superseded = evidence.supersede_duplicate_documents([
        evidence._DocumentRead(10, "report.pdf", evidence._document_key(native_source),
                               evidence._fidelity_of(native), native),
        evidence._DocumentRead(11, "scan.pdf", evidence._document_key(scanned_source),
                               evidence._fidelity_of(scanned), scanned),
    ])

    assert superseded == len(scanned)
    assert all(r.superseded_by_ledger_id == 10 for r in scanned)
    assert all(r.superseded_by_ledger_id is None for r in native), "the good read stays work"
    assert all("report.pdf" in (r.comments or "") for r in scanned), "it says which copy won"


def test_a_scan_is_paired_by_the_reports_own_number_when_it_states_nothing_else():
    """The case the shipment key could not reach.

    On a real pair, the scan's OCR read neither the tracking number nor the delivery date, so the
    two copies looked like different documents — and a scan restating one delivery as a single
    173-unit row sat on the queue beside the printed report's five lines that sum to exactly 173.

    A receiving report is issued with a number, and both copies carry it whatever else the reader
    lost.
    """
    from pipeline import evidence

    native_source, native = _read([_DELIVERY_BAND, _TRACKING_BAND, _ITEM_GRID], ledger_id=40,
                                  raw_text="WAREHOUSE RECEIVING REPORT\nRR 211999-21\n")
    # No delivery band and no tracking band: this reader lost both, as OCR on a scan really did.
    scanned_source, scanned = _read(
        [[["Description", "Carton Markings", "Qty", "Weight"],
          ["Wall Mount.", "XY999", "373", "1000"]]],
        ledger_id=41, raw_text="Warehouse Receiving Report # WRR-211999-21\n",
    )
    for record in native:
        record.extraction_source = "pdf"

    assert evidence._document_key(scanned_source) == ("report", "211999-21")
    assert evidence._document_key(native_source) == evidence._document_key(scanned_source)

    superseded = evidence.supersede_duplicate_documents([
        evidence._DocumentRead(40, "report.pdf", evidence._document_key(native_source),
                               evidence._fidelity_of(native), native),
        evidence._DocumentRead(41, "scan.pdf", evidence._document_key(scanned_source),
                               evidence._fidelity_of(scanned), scanned),
    ])

    assert superseded == len(scanned)
    assert all(r.superseded_by_ledger_id == 40 for r in scanned)


@pytest.mark.parametrize("text, expected", [
    # How the documents in the corpus really word it, in every case checked: `RR <project>-<seq>`.
    ("Warehouse Receiving Report # RR 211999-21", "211999-21"),
    ("Atlas Whse Receiving Report RR 211964-2", "211964-2"),
    ("warehouse receiving report rr 211999-21", "211999-21"),
    # The number as it appears in a *filename* carries no `RR`, and filenames are not scanned:
    # a file may be named anything, and the document's own text is the thing that identifies it.
    ("Atlas Whse Receiving Report #211964-2 for WR-35172", None),
    # Two different reports named on one page: no key. A page mentioning both is not evidence that
    # either one *is* this document.
    ("covers RR 211999-21 and RR 211999-22", None),
    # A project number with no report sequence identifies the job, not the delivery.
    ("Project #: 211999 Site # 1", None),
    ("no report number here at all", None),
])
def test_the_report_number_refuses_rather_than_guesses(text, expected):
    from pipeline import evidence

    assert evidence._report_number(text) == expected


def test_a_document_with_no_report_number_still_pairs_on_its_shipment():
    """The fallback has to keep working — a carrier POD carries no receiving-report number."""
    from pipeline import evidence

    source, _ = _read([_DELIVERY_BAND, _TRACKING_BAND, _ITEM_GRID])
    assert evidence._document_key(source) == ("shipment", "7770001", "2026-08-14")


def test_two_equally_good_reads_of_one_document_are_both_kept():
    """Nothing here is a reason to prefer one of two identical reads, and choosing anyway would be
    a guess dressed as a rule."""
    from pipeline import evidence

    left_source, left = _read([_DELIVERY_BAND, _TRACKING_BAND, _ITEM_GRID], ledger_id=20)
    right_source, right = _read([_DELIVERY_BAND, _TRACKING_BAND, _ITEM_GRID], ledger_id=21)

    superseded = evidence.supersede_duplicate_documents([
        evidence._DocumentRead(20, "a.pdf", evidence._document_key(left_source),
                               evidence._fidelity_of(left), left),
        evidence._DocumentRead(21, "b.pdf", evidence._document_key(right_source),
                               evidence._fidelity_of(right), right),
    ])

    assert superseded == 0
    assert all(r.superseded_by_ledger_id is None for r in left + right)


def test_documents_that_cannot_be_identified_are_never_merged():
    """Failing to notice a duplicate costs a person one extra row. Merging two genuinely different
    deliveries costs Premier a receipt. The key requires both halves for that reason."""
    from pipeline import evidence

    source, _ = _read([_ITEM_GRID])     # no delivery band, so no date and no carrier reference
    assert evidence._document_key(source) is None


# --- rows of one grid are a list, not a disagreement -------------------------

def test_three_skids_of_one_spec_are_not_a_quantity_conflict():
    """`reconcile_cross_source_duplicates` settles disagreement *between sources*. Rows of one
    table are not claims about each other, and reading 210/75/65 as a disagreement flagged all
    three and put them in front of a person who had nothing to decide.

    58 of the 66 flagged groups in the live store are one document like this.
    """
    from pipeline.ingest_orchestrator import reconcile_cross_source_duplicates

    _, records = _read([_DELIVERY_BAND, _TRACKING_BAND, _ITEM_GRID])
    reconciled = reconcile_cross_source_duplicates(records)

    assert len(reconciled) == 4, "no row is dropped and none is merged"
    assert not any("quantity_conflict" in (r.extraction_source or "") for r in reconciled)
    assert sorted(r.quantity_received for r in reconciled) == [23.0, 65.0, 75.0, 210.0]


def test_the_same_spec_from_two_different_documents_still_conflicts():
    """The rule narrows to siblings of one grid. Two documents disagreeing about one line is the
    case the flag exists for, and it must keep working."""
    from pipeline.ingest_orchestrator import reconcile_cross_source_duplicates

    _, first = _read([_DELIVERY_BAND, _TRACKING_BAND, _ITEM_GRID[:2]], ledger_id=30)
    _, second = _read(
        [_DELIVERY_BAND, _TRACKING_BAND,
         [_ITEM_GRID[0], [PO, "Wall Mount: XY999", "AAA-100-EQ////105 PER SKID", "9.00", "11.143"]]],
        ledger_id=31,
    )
    reconciled = reconcile_cross_source_duplicates(first + second)

    assert any("quantity_conflict" in (r.extraction_source or "") for r in reconciled)


# --- the purchase order a document names --------------------------------------

def test_a_warehouse_writes_the_purchase_order_under_its_own_name_for_it():
    """`Customer PO #` is a purchase order column and mapped to nothing at all. Across the stored
    parses 33 header cells carry real purchase orders behind that spelling and its siblings."""
    for header in ("Customer PO #", "Customer PO", "Customer P.O.", "Customer PO Number"):
        assert map_headers([header, "Item #", "Qty"]).get("po_number") == 0, header


def test_a_six_digit_order_number_that_is_not_a_purchase_order_is_refused():
    """A packing slip heads `Customer Order Number` over the vendor's own reference. It is six
    digits, so it passes the shape test, and taking it would attribute goods to a purchase order
    that does not exist — while the real one, stated elsewhere on the message, is discarded.

    This is the guard that makes the synonyms above safe to add.
    """
    column_map = map_headers(["Customer Order Number", "Item / Lotserial NBR", "Delivered Qty"])
    assert column_map.get("po_number") == 0, "the column is still recognised..."

    source = ExtractionSource(
        source_email_id="<synthetic@test>", email_date="2026-08-28T19:09:22Z",
        source_type="attachment", ledger_id=1, known_po_numbers=KNOWN_POS,
    )
    refused = build_record_from_row(source, ["880002", "AAA-100-EQ", "373"], column_map, "ocr")
    assert refused.po_number == "", "...but a number the system has never heard of is not a PO"

    accepted = build_record_from_row(source, [PO, "AAA-100-EQ", "373"], column_map, "ocr")
    assert accepted.po_number == PO


def test_without_a_known_set_only_the_shape_is_checked():
    """`known_po_numbers` is `None` wherever the caller cannot say, and there the behaviour is
    exactly what every caller had before it existed."""
    source = ExtractionSource(
        source_email_id="<synthetic@test>", email_date="2026-08-28T19:09:22Z",
        source_type="attachment", ledger_id=1,
    )
    column_map = map_headers(["Customer Order Number", "Item #", "Qty"])
    record = build_record_from_row(source, ["880002", "AAA-100-EQ", "373"], column_map, "ocr")

    assert record.po_number == "880002"


# --- a spec code with the warehouse's own notes fused onto it -----------------

@pytest.mark.parametrize("written, parent, marking", [
    # How the goods were stacked, appended with no separator the carton pattern recognises. This
    # is the shape that left a real line unmatchable: the purchase order carries the bare spec.
    ("AAA-100-EQ////105 PER SKID", "AAA-100-EQ", "105 PER SKID"),
    ("BBB-200-EQ////10 PER SKID", "BBB-200-EQ", "10 PER SKID"),
    # The carton numbering, which was already handled and must stay handled.
    ("CCC-400-LT-1/1 of 2", "CCC-400-LT", "1/1 of 2"),
])
def test_a_spec_with_packing_notes_still_names_its_line(written, parent, marking):
    """`parent_spec_code` is what `po_verify._line_check` falls back to, and it reports the match
    as `matched_on_parent` — so the cleaned value is a *suggestion the reviewer can see*, never a
    rewrite of what the warehouse wrote."""
    from pipeline.stage3_extract.base import strip_package_marking

    assert strip_package_marking(written) == (parent, marking)


@pytest.mark.parametrize("spec", [
    "AAA-100-EQ", "BBB-200-EQ",          # already bare
    "CCC-402-LT-B",                       # a sub-part suffix, which split_sub_spec owns
    "POOL-201.1-PL", "GR-350a-WTF",       # the awkward real shapes
    "Wall Mount: XY999",                  # prose — a description cell, not a spec cell
    "XY999",                              # a model number
])
def test_a_spec_with_nothing_fused_on_is_left_exactly_as_written(spec):
    """The rule is anchored at the start and only fires when something trails a real spec token.
    Everything else must come back untouched, or a working match becomes a broken one."""
    from pipeline.stage3_extract.base import strip_package_marking

    assert strip_package_marking(spec) == (spec, None)


def test_the_document_keeps_the_last_word_on_what_it_said():
    """The cleaned form travels as the parent; `spec_code` on the record is still verbatim."""
    _, records = _read([_DELIVERY_BAND, _TRACKING_BAND, _ITEM_GRID])
    first = records[0]

    assert first.spec_code == "AAA-100-EQ////105 PER SKID"
    assert first.parent_spec_code == "AAA-100-EQ"


def test_a_record_remembers_which_attachment_it_was_read_from():
    """The field two of the rules above are built on. Without it, rows of one grid and claims from
    two different documents are indistinguishable."""
    _, records = _read([_DELIVERY_BAND, _TRACKING_BAND, _ITEM_GRID], ledger_id=77)

    assert {r.source_ledger_id for r in records} == {77}


# --- a receiving report covering two purchase orders -------------------------

SECOND_PO = "990002"

# The shape of the Warehouse Receiving Report for RR 211373-29: ten item lines, seven against one
# purchase order and three against another, printed in the grid's own `PO #` column. Only one of
# the two orders had ever been pulled into the mirror.
_TWO_PO_GRID = [
    ["PO #", "Description\n(such as Desk, Chair, Lamp)",
     "Carton Markings\n(Item#/Mfg#/Serial#)", "Qty", "Weight"],
    [SECOND_PO, "King Headboard", "AAA-100-EQ", "15.00", "238.000"],
    [SECOND_PO, "Headboard - MIDDLE PANEL", "AAA-101-EQ-1/2", "10.00", "81.000"],
    [SECOND_PO, "Headboard - BACK PANEL", "AAA-101-EQ-2/2", "10.00", "238.000"],
    [PO, "ADA Vanity - 30\"W", "BBB-200-EQ", "11.00", "68.000"],
    [PO, "ADA Vanity - 55.5\"W", "BBB-201-EQ", "1.00", "123.000"],
]


def _two_po_source():
    return ExtractionSource(
        source_email_id="<synthetic@test>", email_date="2026-09-10T16:20:51Z",
        source_type="attachment", ledger_id=1, known_po_numbers=KNOWN_POS,
    )


def test_a_grid_naming_two_purchase_orders_keeps_each_row_on_its_own():
    """Every row is recorded against the purchase order printed beside it.

    The live failure: seven lines belonging to one order were written against another, because the
    mirror had never been asked for the second order and the guard above blanked its cell. Spitfire
    settled it — the receiving order's own lines were exactly the three the document attributed to
    it, and none of the other seven.
    """
    from pipeline.stage3_extract.base import po_column_is_corroborated

    column_map = map_headers(_TWO_PO_GRID[0])
    body = _TWO_PO_GRID[1:]
    verified = po_column_is_corroborated(body, column_map, KNOWN_POS)
    assert verified, "one recognised order in the column vouches for the rest of it"

    source = _two_po_source()
    staged = [build_record_from_row(source, row, column_map, "pdf", po_column_verified=verified)
              for row in body]

    assert [r.po_number for r in staged] == [SECOND_PO, SECOND_PO, SECOND_PO, PO, PO]
    # ...and the spec on each line is the one its own row printed, not its neighbour's.
    assert [(r.po_number, r.spec_code) for r in staged if r.po_number == PO] == [
        (PO, "BBB-200-EQ"), (PO, "BBB-201-EQ")]


def test_an_unpulled_purchase_order_is_not_noise():
    """A purchase order we have never pulled is not a purchase order that does not exist.

    Without the column's corroboration the cell is still dropped — that is what keeps a packing
    slip's `Customer Order Number` out — so the two behaviours are asserted side by side.
    """
    column_map = map_headers(_TWO_PO_GRID[0])
    row = [SECOND_PO, "King Headboard", "AAA-100-EQ", "15.00", "238.000"]

    kept = build_record_from_row(_two_po_source(), row, column_map, "pdf", po_column_verified=True)
    assert kept.po_number == SECOND_PO

    dropped = build_record_from_row(_two_po_source(), row, column_map, "pdf")
    assert dropped.po_number == "", "an uncorroborated column is still checked against the mirror"
