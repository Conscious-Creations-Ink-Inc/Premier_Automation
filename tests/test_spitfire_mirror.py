"""The local mirror of Spitfire PO data — the cache Stage 4 matches against.

What is pinned here is the refresh semantics. A PO is re-read whenever a delivery arrives against
it, and lines genuinely come and go between reads (a cancelled item, a revision that renumbers).
A refresh that merged instead of replacing would leave stale lines for the matcher to find, and a
stale line is worse than a missing one: it looks receivable.
"""

import pytest

from connectors.spitfire import PODocument
from config import settings
from pipeline import spitfire_mirror, spitfire_warehouse, state_db
from pipeline.models import POLine

NOW = "2026-08-08T12:00:00+00:00"


@pytest.fixture
def conn():
    connection = state_db.get_connection(":memory:")
    yield connection
    connection.close()


def _line(**overrides) -> POLine:
    base = dict(
        po_number="912456", line_number=1,
        line_key="45fd1906-0b83-4868-b12a-bdcac04a8bfc", spec_code="FIT-902-TV",
        description="FIT-902-TV - TV Wall Mount - Exercise Area",
        vendor_name="Northgate Industries Inc", unit_of_measure="EA",
        qty_ordered=2.0, qty_received=0.0, cost_code="102011249",
        project_code="PNW025TB100012", project_name="Westin Princeton Public Space",
        line_status="N", expected_date=None, ship_to="***DO NOT SHIP ON YOUR OWN***",
        assigned_agent="Delfina Marsetti", pay_terms=None,
    )
    base.update(overrides)
    return POLine(**base)


def _doc(lines=None, **overrides) -> PODocument:
    base = dict(
        doc_master_key="6aad38da-39f6-41b7-afc2-480f372e1fa4", po_number="912456",
        project_code="PNW025TB100012", project_name="Westin Princeton Public Space",
        doc_status="M", doc_status_label="Committed", source_date=None,
        vendor_name="Northgate Industries Inc", vendor_email="contact@example-mounts.test",
        ship_to="***DO NOT SHIP ON YOUR OWN***", assigned_agent="Delfina Marsetti",
        pay_terms_prose=None, tax_lines_skipped=1,
    )
    base.update(overrides)
    return PODocument(lines=lines if lines is not None else [_line()], **base)


def test_po_round_trips_through_the_mirror(conn):
    assert spitfire_mirror.save_po(conn, _doc(), NOW) == 1
    assert spitfire_mirror.doc_key_for(conn, "912456") == "6aad38da-39f6-41b7-afc2-480f372e1fa4"
    assert spitfire_mirror.refreshed_at(conn, "912456") == NOW

    [line] = spitfire_mirror.lines_for(conn, "912456")
    assert (line.spec_code, line.qty_ordered, line.unit_of_measure) == ("FIT-902-TV", 2.0, "EA")
    assert line.project_name == "Westin Princeton Public Space"


def test_unknown_po_reads_as_absent_not_as_an_error(conn):
    """Stage 4 asks about POs that may not be in Spitfire at all — PO 911067 has no 3PL record,
    and a mis-parsed six-digit number can look like a PO. That is an ordinary outcome."""
    assert spitfire_mirror.doc_key_for(conn, "999999") is None
    assert spitfire_mirror.lines_for(conn, "999999") == []
    assert spitfire_mirror.refreshed_at(conn, "999999") is None


def test_refresh_replaces_lines_rather_than_merging(conn):
    """A line that disappears from the PO must disappear from the mirror. Merging would leave a
    cancelled line sitting there looking receivable."""
    spitfire_mirror.save_po(conn, _doc(lines=[
        _line(), _line(line_key="aaaa-1111", line_number=2, spec_code="STE-402-LT-B"),
    ]), NOW)
    assert len(spitfire_mirror.lines_for(conn, "912456")) == 2

    spitfire_mirror.save_po(conn, _doc(lines=[_line(qty_received=2.0)]), "2026-08-09T09:00:00+00:00")
    lines = spitfire_mirror.lines_for(conn, "912456")
    assert [l.spec_code for l in lines] == ["FIT-902-TV"]
    assert lines[0].qty_received == 2.0
    assert spitfire_mirror.refreshed_at(conn, "912456") == "2026-08-09T09:00:00+00:00"


def test_refreshing_one_po_leaves_another_alone(conn):
    """The delete is scoped by PO number. Getting that wrong would empty the mirror on every
    single-PO refresh, which reads to Stage 4 as "no lines" rather than as an error."""
    spitfire_mirror.save_po(conn, _doc(), NOW)
    spitfire_mirror.save_po(conn, _doc(
        po_number="912547", doc_master_key="bbbb-2222",
        lines=[_line(po_number="912547", line_key="cccc-3333", spec_code="GR-350a-WTF")],
    ), NOW)

    spitfire_mirror.save_po(conn, _doc(), "2026-08-09T09:00:00+00:00")
    assert len(spitfire_mirror.lines_for(conn, "912547")) == 1
    assert spitfire_mirror.mirrored_po_numbers(conn) == ["912456", "912547"]


def test_sub_parts_are_kept_as_separate_lines(conn):
    """STE-402-LT-B (11 bases) and STE-402-LT-SH (12 shades) are lines 300 and 301 of one PO.
    Keying the mirror on anything that collapsed them into their shared parent would fabricate a
    quantity conflict on every shipment that splits — which most of them do."""
    spitfire_mirror.save_po(conn, _doc(lines=[
        _line(line_key="b-key", line_number=300, spec_code="STE-402-LT-B", qty_ordered=11.0),
        _line(line_key="sh-key", line_number=301, spec_code="STE-402-LT-SH", qty_ordered=12.0),
    ]), NOW)
    lines = spitfire_mirror.lines_for(conn, "912456")
    assert [(l.line_number, l.spec_code, l.qty_ordered) for l in lines] == [
        (300, "STE-402-LT-B", 11.0), (301, "STE-402-LT-SH", 12.0),
    ]


def test_fractional_quantities_survive_the_round_trip(conn):
    """202.5 YD is representable here and is not through Premier's stored procedures, whose
    `@Quantity numeric(18,0)` is an integer. The mirror must not quietly acquire that limit."""
    spitfire_mirror.save_po(conn, _doc(lines=[_line(qty_ordered=202.5, unit_of_measure="YD")]), NOW)
    assert spitfire_mirror.lines_for(conn, "912456")[0].qty_ordered == 202.5


def test_in_transit_quantity_survives_the_round_trip(conn):
    """The mirror is what Stage 4 reads. If in-transit is lost here, the duplicate-receipt guard
    is lost with it — the connector reading the field correctly would not matter."""
    spitfire_mirror.save_po(conn, _doc(lines=[
        _line(qty_ordered=10.0, qty_received=4.0, qty_in_transit=6.0),
    ]), NOW)
    [line] = spitfire_mirror.lines_for(conn, "912456")
    assert line.qty_in_transit == 6.0
    assert line.qty_outstanding == 0.0


def test_every_po_line_field_is_persisted():
    """A field POLine carries but the mirror drops is silently lost between the connector and
    Stage 4 — the same guard extracted_records_store._COLUMNS gets in test_models.py."""
    from dataclasses import fields
    assert {f.name for f in fields(POLine)} == set(spitfire_mirror._LINE_COLUMNS)


# --- projecting the warehouse into the mirror ---------------------------------
# The mirror used to be filled one PO at a time by `client.read_po`, so it held 36 purchase orders
# while the warehouse held 420 of the same documents. These pin the offline projection that closed
# that gap: it must reassemble the warehouse's normalised rows into exactly the payload shape
# `build_po_document` expects, or the field traps that parser exists for are silently bypassed.

def _warehouse(rows=None):
    """A warehouse holding one PO with one goods line and one tax line."""
    conn = spitfire_warehouse.get_connection(":memory:")
    doc_key = "6AAD38DA-39F6-41B7-AFC2-480F372E1FA4"
    spitfire_warehouse.save_rows(conn, "sf_document", [{
        "DocMasterKey": doc_key, "DocNo": "912456", "Project": "PNW025TB100012",
        "DocTypeKey": settings.SPITFIRE_PO_DOC_TYPE_KEY,
        "Project_dv": "Westin Princeton Public Space", "Status": "M", "Status_dv": "Committed",
        "DocDate": "2025-09-30T00:00:00", "SourceDate": "2025-10-08T00:00:00",
    }], {"po_number": "912456", "project_code": "PNW025TB100012"})

    goods, tax = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
    spitfire_warehouse.save_rows(conn, "sf_document_item", [
        # The document key is stored lower-case here and upper-case on the header, which is how
        # Spitfire really answers — the projection must still join them.
        {"DocItemKey": goods, "DocItemNumber": "0001", "SourceItemNumber": "FIT-902-TV",
         "Description": "<div>FIT-902-TV - TV Wall Mount&nbsp;</div>", "ItemQuantity": 0.0},
        {"DocItemKey": tax, "DocItemNumber": "0002", "Description": "Tax", "ItemQuantity": 0.0},
    ], {"doc_master_key": doc_key.lower(), "po_number": "912456"})

    spitfire_warehouse.save_rows(conn, "sf_item_task", [
        {"ItemTaskKey": "aaaa1111-0000-0000-0000-000000000001", "UOM": "EA",
         "ProjEntity": "102011249", "AccountCategory": "MAT-FP0", "Quantity": 2.0}],
        {"doc_item_key": goods, "doc_master_key": doc_key.lower(), "po_number": "912456"})
    spitfire_warehouse.save_rows(conn, "sf_item_task", [
        {"ItemTaskKey": "aaaa1111-0000-0000-0000-000000000002", "AccountCategory": "TAX-FP0"}],
        {"doc_item_key": tax, "doc_master_key": doc_key.lower(), "po_number": "912456"})

    spitfire_warehouse.save_rows(conn, "sf_item_related", [
        {"ContractUnits": 2.0, "ReceivedUnits": 1.0, "ReceiptInProgressUnits": 0.5,
         "AccountCategory": "MAT-FP0", "UOM": "EA"}],
        {"doc_item_key": goods, "doc_master_key": doc_key.lower(), "po_number": "912456"})

    spitfire_warehouse.save_rows(conn, "sf_document_address", [
        {"DocAddrKey": "dddd1111-0000-0000-0000-000000000001", "AddrType": "T",
         "Company": "Northgate Industries Inc", "Email": "contact@example-mounts.test"},
        {"DocAddrKey": "dddd1111-0000-0000-0000-000000000002", "AddrType": "S",
         "Company": "***DO NOT SHIP ON YOUR OWN***"}],
        {"doc_master_key": doc_key.lower(), "po_number": "912456"})
    spitfire_warehouse.save_rows(conn, "sf_document_route", [
        {"RouteID": 1, "UserName": "Delfina Marsetti"}],
        {"doc_master_key": doc_key.lower(), "po_number": "912456"})
    conn.commit()
    return conn


def test_projection_rebuilds_a_po_from_warehouse_rows(conn):
    wh = _warehouse()
    try:
        assert spitfire_mirror.refresh_from_warehouse(conn, wh, NOW) == (1, 1)
    finally:
        wh.close()

    header = spitfire_mirror.header_for(conn, "912456")
    assert header["vendor_name"] == "Northgate Industries Inc"
    assert header["ship_to"] == "***DO NOT SHIP ON YOUR OWN***"
    assert header["assigned_agent"] == "Delfina Marsetti"
    assert header["project_name"] == "Westin Princeton Public Space"
    # DocDate, not SourceDate — the header carries both and only one of them orders correctly.
    assert header["order_date"] == "2025-09-30"
    assert header["tax_lines_skipped"] == 1


def test_projection_applies_the_same_field_traps_as_the_live_read(conn):
    wh = _warehouse()
    try:
        spitfire_mirror.refresh_from_warehouse(conn, wh, NOW)
    finally:
        wh.close()

    [line] = spitfire_mirror.lines_for(conn, "912456")
    # ItemQuantity reads 0.0; the ordered quantity is RelatedLineDetails.ContractUnits.
    assert line.qty_ordered == 2.0
    # Specification is null — the spec code lives in SourceItemNumber.
    assert line.spec_code == "FIT-902-TV"
    # Description arrives as HTML and must be stripped before it reaches the matcher.
    assert line.description == "FIT-902-TV - TV Wall Mount"
    assert "<div>" not in line.description
    # An unapproved receipt sits in ReceiptInProgressUnits, not ReceivedUnits.
    assert (line.qty_received, line.qty_in_transit) == (1.0, 0.5)
    assert line.unit_of_measure == "EA"
    assert line.cost_code == "102011249"


def test_projection_skips_tax_lines_rather_than_offering_them_to_the_matcher(conn):
    wh = _warehouse()
    try:
        spitfire_mirror.refresh_from_warehouse(conn, wh, NOW)
    finally:
        wh.close()
    assert [l.spec_code for l in spitfire_mirror.lines_for(conn, "912456")] == ["FIT-902-TV"]


def test_projection_can_be_narrowed_to_named_purchase_orders(conn):
    wh = _warehouse()
    try:
        assert spitfire_mirror.refresh_from_warehouse(conn, wh, NOW, ["999999"]) == (0, 0)
        assert spitfire_mirror.mirrored_po_numbers(conn) == []
        assert spitfire_mirror.refresh_from_warehouse(conn, wh, NOW, ["912456"]) == (1, 1)
        assert spitfire_mirror.mirrored_po_numbers(conn) == ["912456"]
    finally:
        wh.close()


def test_projection_issues_no_spitfire_request(conn, monkeypatch):
    """The whole point of the projection: it must work with the network unavailable.

    `refresh_mirror` used to call `client.read_po` per PO, so a lapsed cookie left the mirror
    half-filled. Anything that reopens a socket here reintroduces that failure.
    """
    import connectors.spitfire as spitfire

    def explode(*args, **kwargs):
        raise AssertionError("the warehouse projection must not talk to Spitfire")

    monkeypatch.setattr(spitfire.SpitfireReadClient, "_request", explode)
    wh = _warehouse()
    try:
        assert spitfire_mirror.refresh_from_warehouse(conn, wh, NOW) == (1, 1)
    finally:
        wh.close()


def test_projection_refuses_to_treat_a_receipt_as_a_purchase_order(conn):
    """`--doc-types all` puts receipts in `sf_document` beside the purchase orders.

    A receipt numbers its documents per-parent, so its `DocNo` is `0002`. Projected unfiltered it
    would land in `spitfire_po_index` as a purchase order called "0002" and offer its lines to the
    matcher against a real delivery. Only PO/Contracts documents are purchase orders.
    """
    wh = _warehouse()
    try:
        spitfire_warehouse.save_rows(wh, "sf_document", [{
            "DocMasterKey": "99999999-9999-9999-9999-999999999999",
            "DocTypeKey": settings.SPITFIRE_RECEIPT_DOC_TYPE_KEY,
            "DocNo": "0002", "Project": "PNW025TB100012", "Status_dv": "In Process",
        }], {"po_number": "0002", "project_code": "PNW025TB100012"})
        wh.commit()

        assert spitfire_mirror.refresh_from_warehouse(conn, wh, NOW) == (1, 1)
        assert spitfire_mirror.mirrored_po_numbers(conn) == ["912456"]
    finally:
        wh.close()


def test_projection_matches_the_document_type_case_insensitively(conn):
    """The same GUID arrives upper-case from `DocMasterAlt` and lower-case from the header, so the
    type filter must not depend on which spelling the sweep happened to store."""
    wh = _warehouse()
    try:
        wh.execute("UPDATE sf_document SET DocTypeKey = ?",
                   (settings.SPITFIRE_PO_DOC_TYPE_KEY.upper(),))
        wh.commit()
        assert spitfire_mirror.refresh_from_warehouse(conn, wh, NOW) == (1, 1)
    finally:
        wh.close()


def test_projection_without_a_type_filter_takes_everything(conn):
    """None is an explicit "no filter", distinct from the caller not saying. The two cases need
    separate answers, which is why the default is a sentinel rather than None."""
    wh = _warehouse()
    try:
        spitfire_warehouse.save_rows(wh, "sf_document", [{
            "DocMasterKey": "99999999-9999-9999-9999-999999999999",
            "DocTypeKey": settings.SPITFIRE_RECEIPT_DOC_TYPE_KEY, "DocNo": "0002",
        }], {"po_number": "0002"})
        wh.commit()
        assert spitfire_mirror.refresh_from_warehouse(conn, wh, NOW, doc_type_key=None) == (2, 1)
        assert spitfire_mirror.mirrored_po_numbers(conn) == ["0002", "912456"]
    finally:
        wh.close()
