"""The local mirror of Spitfire PO data — the cache Stage 4 matches against.

What is pinned here is the refresh semantics. A PO is re-read whenever a delivery arrives against
it, and lines genuinely come and go between reads (a cancelled item, a revision that renumbers).
A refresh that merged instead of replacing would leave stale lines for the matcher to find, and a
stale line is worse than a missing one: it looks receivable.
"""

import pytest

from connectors.spitfire import PODocument
from pipeline import spitfire_mirror, state_db
from pipeline.models import POLine

NOW = "2026-08-08T12:00:00+00:00"


@pytest.fixture
def conn():
    connection = state_db.get_connection(":memory:")
    yield connection
    connection.close()


def _line(**overrides) -> POLine:
    base = dict(
        po_number="212456", line_number=1,
        line_key="45fd1906-0b83-4868-b12a-bdcac04a8bfc", spec_code="FIT-902-TV",
        description="FIT-902-TV - TV Wall Mount - Exercise Area",
        vendor_name="Peerless Industries Inc", unit_of_measure="EA",
        qty_ordered=2.0, qty_received=0.0, cost_code="102011249",
        project_code="PNW025TB100012", project_name="Westin Princeton Public Space",
        line_status="N", expected_date=None, ship_to="***DO NOT SHIP ON YOUR OWN***",
        assigned_agent="Delfina Marsetti", pay_terms=None,
    )
    base.update(overrides)
    return POLine(**base)


def _doc(lines=None, **overrides) -> PODocument:
    base = dict(
        doc_master_key="6aad38da-39f6-41b7-afc2-480f372e1fa4", po_number="212456",
        project_code="PNW025TB100012", project_name="Westin Princeton Public Space",
        doc_status="M", doc_status_label="Committed", source_date=None,
        vendor_name="Peerless Industries Inc", vendor_email="KPetrin@peerless-av.com",
        ship_to="***DO NOT SHIP ON YOUR OWN***", assigned_agent="Delfina Marsetti",
        pay_terms_prose=None, tax_lines_skipped=1,
    )
    base.update(overrides)
    return PODocument(lines=lines if lines is not None else [_line()], **base)


def test_po_round_trips_through_the_mirror(conn):
    assert spitfire_mirror.save_po(conn, _doc(), NOW) == 1
    assert spitfire_mirror.doc_key_for(conn, "212456") == "6aad38da-39f6-41b7-afc2-480f372e1fa4"
    assert spitfire_mirror.refreshed_at(conn, "212456") == NOW

    [line] = spitfire_mirror.lines_for(conn, "212456")
    assert (line.spec_code, line.qty_ordered, line.unit_of_measure) == ("FIT-902-TV", 2.0, "EA")
    assert line.project_name == "Westin Princeton Public Space"


def test_unknown_po_reads_as_absent_not_as_an_error(conn):
    """Stage 4 asks about POs that may not be in Spitfire at all — PO 211067 has no 3PL record,
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
    assert len(spitfire_mirror.lines_for(conn, "212456")) == 2

    spitfire_mirror.save_po(conn, _doc(lines=[_line(qty_received=2.0)]), "2026-08-09T09:00:00+00:00")
    lines = spitfire_mirror.lines_for(conn, "212456")
    assert [l.spec_code for l in lines] == ["FIT-902-TV"]
    assert lines[0].qty_received == 2.0
    assert spitfire_mirror.refreshed_at(conn, "212456") == "2026-08-09T09:00:00+00:00"


def test_refreshing_one_po_leaves_another_alone(conn):
    """The delete is scoped by PO number. Getting that wrong would empty the mirror on every
    single-PO refresh, which reads to Stage 4 as "no lines" rather than as an error."""
    spitfire_mirror.save_po(conn, _doc(), NOW)
    spitfire_mirror.save_po(conn, _doc(
        po_number="212547", doc_master_key="bbbb-2222",
        lines=[_line(po_number="212547", line_key="cccc-3333", spec_code="GR-350a-WTF")],
    ), NOW)

    spitfire_mirror.save_po(conn, _doc(), "2026-08-09T09:00:00+00:00")
    assert len(spitfire_mirror.lines_for(conn, "212547")) == 1
    assert spitfire_mirror.mirrored_po_numbers(conn) == ["212456", "212547"]


def test_sub_parts_are_kept_as_separate_lines(conn):
    """STE-402-LT-B (11 bases) and STE-402-LT-SH (12 shades) are lines 300 and 301 of one PO.
    Keying the mirror on anything that collapsed them into their shared parent would fabricate a
    quantity conflict on every shipment that splits — which most of them do."""
    spitfire_mirror.save_po(conn, _doc(lines=[
        _line(line_key="b-key", line_number=300, spec_code="STE-402-LT-B", qty_ordered=11.0),
        _line(line_key="sh-key", line_number=301, spec_code="STE-402-LT-SH", qty_ordered=12.0),
    ]), NOW)
    lines = spitfire_mirror.lines_for(conn, "212456")
    assert [(l.line_number, l.spec_code, l.qty_ordered) for l in lines] == [
        (300, "STE-402-LT-B", 11.0), (301, "STE-402-LT-SH", 12.0),
    ]


def test_fractional_quantities_survive_the_round_trip(conn):
    """202.5 YD is representable here and is not through Premier's stored procedures, whose
    `@Quantity numeric(18,0)` is an integer. The mirror must not quietly acquire that limit."""
    spitfire_mirror.save_po(conn, _doc(lines=[_line(qty_ordered=202.5, unit_of_measure="YD")]), NOW)
    assert spitfire_mirror.lines_for(conn, "212456")[0].qty_ordered == 202.5


def test_in_transit_quantity_survives_the_round_trip(conn):
    """The mirror is what Stage 4 reads. If in-transit is lost here, the duplicate-receipt guard
    is lost with it — the connector reading the field correctly would not matter."""
    spitfire_mirror.save_po(conn, _doc(lines=[
        _line(qty_ordered=10.0, qty_received=4.0, qty_in_transit=6.0),
    ]), NOW)
    [line] = spitfire_mirror.lines_for(conn, "212456")
    assert line.qty_in_transit == 6.0
    assert line.qty_outstanding == 0.0


def test_every_po_line_field_is_persisted():
    """A field POLine carries but the mirror drops is silently lost between the connector and
    Stage 4 — the same guard extracted_records_store._COLUMNS gets in test_models.py."""
    from dataclasses import fields
    assert {f.name for f in fields(POLine)} == set(spitfire_mirror._LINE_COLUMNS)
