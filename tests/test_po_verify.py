"""What the Verify button says, and — just as much — what it refuses to say.

The comparison is pure (`verify_record` takes the PO already read), so every case here runs against
hand-built `POLine` lists and touches neither Spitfire nor a database. The live path is covered by
one test that stubs the client, because the thing worth pinning there is the *fallback*: a lapsed
session cookie must produce mirrored figures under a warning, not an empty popup.
"""
import pytest

from connectors.spitfire import PODocument
from pipeline import po_verify
from pipeline.models import POLine


def line(spec="GR-350a-WTF", *, number=1, ordered=196.0, received=0.0, in_transit=0.0,
         uom="YD", description="Main Drapery Fabric") -> POLine:
    return POLine(
        po_number="910634", line_number=number, line_key=f"key-{number}", spec_code=spec,
        description=description, vendor_name="P. Kaufmann", unit_of_measure=uom,
        qty_ordered=ordered, qty_received=received, qty_in_transit=in_transit,
        cost_code="", project_code="PRJ001PB100003", project_name="", line_status="Open",
        expected_date=None, ship_to=None, assigned_agent=None,
    )


def facts(**overrides) -> po_verify.RecordFacts:
    base = dict(id=131, po_number="910634", spec_code="GR-350a-WTF",
                parent_spec_code="GR-350a-WTF", item_description="Main Drapery Fabric",
                quantity_received=196.0, unit_of_measure="YD")
    base.update(overrides)
    return po_verify.RecordFacts(**base)


def doc(**overrides) -> PODocument:
    base = dict(doc_master_key="k", po_number="910634", project_code="PRJ001PB100003",
                project_name="", doc_status="M", doc_status_label="Committed",
                source_date=None, vendor_name="P. Kaufmann", vendor_email=None, ship_to=None,
                assigned_agent=None, pay_terms_prose=None, order_date="2025-08-17")
    base.update(overrides)
    return PODocument(**base)


def notes(result) -> str:
    return " ".join(result.notes)


# --- the quantity comparison --------------------------------------------------


def test_a_quantity_equal_to_the_ordered_quantity_is_reported_as_equal():
    result = po_verify.verify_record(facts(), doc(), [line()])
    assert result.matched.qty_agrees is True
    assert result.matched.qty_delta == pytest.approx(0.0)
    assert "same as the quantity ordered" in notes(result)


def test_a_larger_quantity_names_both_figures_and_the_difference():
    """Premier's open overage question is 202 received against 196 ordered. The screen states the
    gap; it does not decide whether 6 extra yards are acceptable."""
    result = po_verify.verify_record(facts(quantity_received=202.0), doc(), [line()])
    assert result.matched.qty_agrees is False
    assert "6 more than the 196" in notes(result)
    assert result.has_finding


def test_a_smaller_quantity_reads_as_less_than_not_as_an_error():
    result = po_verify.verify_record(facts(quantity_received=100.0), doc(), [line()])
    assert "96 less than the 196" in notes(result)


def test_a_fully_received_line_is_stated_but_not_judged():
    """This is the case every one of Premier's three quantified records is in today: the email
    quantity equals the ordered quantity *and* Spitfire already shows the line received. Both facts
    are reported; neither is turned into a verdict, because which one wins is Premier's call."""
    result = po_verify.verify_record(facts(), doc(), [line(received=196.0)])
    text = notes(result)
    assert "same as the quantity ordered" in text
    assert "already records 196 YD received" in text
    assert "Nothing is outstanding" in text
    # No word that decides for the reader.
    assert "MISMATCH" not in text.upper()
    assert "over-receipt" not in text.lower()


def test_in_transit_quantity_is_called_out_separately_from_received():
    result = po_verify.verify_record(facts(), doc(), [line(in_transit=50.0)])
    assert "50 YD is in transit" in notes(result)
    assert result.matched.qty_outstanding == pytest.approx(146.0)


# --- units --------------------------------------------------------------------


def test_a_different_unit_says_the_quantities_are_not_comparable():
    result = po_verify.verify_record(facts(unit_of_measure="EA"), doc(), [line(uom="YD")])
    assert result.matched.uom_agrees is False
    assert "not in the same unit" in notes(result)


@pytest.mark.parametrize("ours, theirs", [("EACH", "EA"), ("EA", "EACH"), ("SET", "Set"),
                                          ("ea", "EA")])
def test_the_same_unit_spelled_differently_is_not_a_difference(ours, theirs):
    """Six live records say EACH where Spitfire says EA, and seven PO lines say `Set` against
    records saying `SET`. Warning on those would put a unit complaint on rows where the units
    plainly agree, and a reader who learns to ignore that warning will ignore a real one."""
    result = po_verify.verify_record(
        facts(unit_of_measure=ours, quantity_received=2.0),
        doc(), [line(uom=theirs, ordered=2.0)],
    )
    assert result.matched.uom_agrees is True
    assert "not in the same unit" not in notes(result)
    assert result.has_finding is False


def test_an_unknown_unit_is_never_quietly_treated_as_equal():
    """The alias table is deliberately tiny. Guessing that CS means EA would silence exactly the
    mismatch this screen exists to surface."""
    result = po_verify.verify_record(facts(unit_of_measure="CS"), doc(), [line(uom="EA")])
    assert result.matched.uom_agrees is False


def test_a_unit_missing_on_either_side_is_neither_agreement_nor_disagreement():
    result = po_verify.verify_record(facts(unit_of_measure=None), doc(), [line(uom="EA")])
    assert result.matched.uom_agrees is None
    assert "not in the same unit" not in notes(result)


# --- what is missing ----------------------------------------------------------


def test_a_spec_that_is_not_on_the_po_lists_the_specs_that_are():
    result = po_verify.verify_record(
        facts(spec_code="GR-999-XX", parent_spec_code=None, item_description=None),
        doc(), [line(), line(spec="GR-350b-WTF", number=3)],
    )
    assert result.matched is None
    assert "is not on this purchase order" in notes(result)
    assert "GR-350a-WTF, GR-350b-WTF" in notes(result)


def test_a_record_with_no_spec_or_description_matches_no_line_at_all():
    """A PO-number signal alone must not select a line. Every line on the PO carries the same PO
    number, so accepting it would show quantities from a line nobody identified."""
    result = po_verify.verify_record(
        facts(spec_code=None, parent_spec_code=None, item_description=None,
              quantity_received=None, unit_of_measure=None),
        doc(), [line(), line(spec="GR-350b-WTF", number=3)],
    )
    assert result.matched is None
    assert "gave no spec code" in notes(result)
    assert "2 receivable line(s)" in notes(result)
    assert "nothing to compare" in notes(result)


def test_a_line_matched_but_no_quantity_stated_compares_nothing():
    result = po_verify.verify_record(facts(quantity_received=None, unit_of_measure=None),
                                     doc(), [line()])
    assert result.matched.qty_agrees is None
    assert "gave no quantity" in notes(result)


def test_a_po_that_was_not_found_does_not_claim_it_does_not_exist():
    result = po_verify.verify_record(facts(po_number="912456"), None, [])
    assert result.po_found is False
    assert "not the same as it not existing" in notes(result)


def test_a_po_with_no_receivable_lines_says_so():
    result = po_verify.verify_record(facts(), doc(), [])
    assert result.po_found is True
    assert "no receivable lines" in notes(result)


# --- package quantities -------------------------------------------------------


def test_package_counts_are_reported_but_never_compared():
    """41 CTN against a line of 11 EA is eleven items in forty-one cartons. Comparing the carton
    count to the PO line is the mistake the two-field split exists to prevent."""
    result = po_verify.verify_record(
        facts(quantity_received=11.0, unit_of_measure="EA", package_quantity=41.0,
              package_uom="CTN"),
        doc(), [line(ordered=11.0, uom="EA")],
    )
    assert result.matched.record_quantity == 11.0        # the item quantity, not 41
    assert "41 CTN of packaging" in notes(result)
    assert "not compared" in notes(result)


# --- description-only matches -------------------------------------------------


def test_a_line_reached_by_description_alone_is_flagged_as_the_weaker_claim():
    result = po_verify.verify_record(
        facts(spec_code=None, parent_spec_code=None),
        doc(), [line(spec="")],
    )
    assert result.matched is not None
    assert result.matched.spec_resolved is False
    assert "description alone" in notes(result)


# --- more than one line carrying the same spec --------------------------------


def test_a_surcharge_line_sharing_a_spec_does_not_steal_the_match():
    """Live PO 910635, and the reason the tie-break exists. Spec GR-350c-WTF sits on two lines —
    0001 is 84 YD of sheer fabric, 0003 is a 1 EA tariff surcharge on it. Both match the spec
    exactly, and the surcharge's shorter description scored higher, so the screen reported "84 YD
    is 83 more than the 1 EA ordered" — a discrepancy that existed only because the wrong line had
    been picked. The unit settles it."""
    result = po_verify.verify_record(
        facts(spec_code="GR-350c-WTF", parent_spec_code=None,
              item_description="Sheer Fabric", quantity_received=84.0, unit_of_measure="YD"),
        doc(),
        [line(spec="GR-350c-WTF", number=1, ordered=84.0, received=84.0, uom="YD",
              description="GR-350c-WTF Sheer Fabric Pattern Name: Sonoma"),
         line(spec="GR-350c-WTF", number=3, ordered=1.0, received=1.0, uom="EA",
              description="GR-350c-WTF Sheer Fabric - Tariff Surcharge 5.5%")],
    )
    assert result.matched.line_number == 1
    assert result.matched.qty_agrees is True
    assert "More than one line" in notes(result)
    assert "line 0003 (1 EA)" in notes(result), "the line not chosen must be named"


def test_the_quantity_is_never_used_to_choose_between_lines():
    """Picking whichever line agrees with the email would make every comparison agree by
    construction — the one outcome that would make this screen worthless. Here both candidates are
    in the email's unit and only the second matches the quantity; the tie-break must not reach for
    it."""
    result = po_verify.verify_record(
        facts(quantity_received=9.0, unit_of_measure="EA", item_description="Throw Pillow"),
        doc(),
        [line(spec="GR-350a-WTF", number=1, ordered=12.0, uom="EA", description="Throw Pillow"),
         line(spec="GR-350a-WTF", number=2, ordered=9.0, uom="EA", description="Throw Pillow")],
    )
    assert result.matched.qty_ordered == 12.0, "the tie-break reached for the agreeing quantity"
    assert "More than one line" in notes(result)


def test_an_unambiguous_match_says_nothing_about_alternatives():
    result = po_verify.verify_record(facts(), doc(),
                                     [line(), line(spec="GR-350b-WTF", number=3, ordered=91.0)])
    assert "More than one line" not in notes(result)


# --- the sort on "Verify all" -------------------------------------------------


def test_only_a_clean_agreeing_comparison_counts_as_nothing_to_report():
    clean = po_verify.verify_record(facts(), doc(), [line()])
    assert clean.has_finding is False
    for broken in (po_verify.verify_record(facts(quantity_received=202.0), doc(), [line()]),
                   po_verify.verify_record(facts(unit_of_measure="EA"), doc(), [line()]),
                   po_verify.verify_record(facts(po_number="912456"), None, [])):
        assert broken.has_finding is True


# --- the live path ------------------------------------------------------------


class _Row(dict):
    """A stand-in for `sqlite3.Row` — `records_ready` hands those back, and `facts_from_row` is
    written against the `keys()` / `__getitem__` pair rather than attribute access."""


def test_a_lapsed_cookie_falls_back_to_the_mirror_under_a_warning(monkeypatch, tmp_path):
    """The failure that will actually happen. The cookie is hand-captured and lapses; when it does,
    stale figures clearly stamped beat an empty popup, which a reader takes for "nothing matched"
    rather than "we could not look."""
    import sqlite3

    from pipeline import spitfire_mirror, state_db

    db = tmp_path / "t.sqlite3"
    conn = state_db.get_connection(str(db))
    spitfire_mirror.save_po(conn, doc(lines=[line(received=196.0)]), "2026-08-11T18:32:45")

    class Lapsed:
        def resolve_po(self, po_number):
            raise RuntimeError("the supplied sfPMSAuth cookie has expired or was rejected.")

        def read_po(self, key):
            raise RuntimeError("the supplied sfPMSAuth cookie has expired or was rejected.")

    row = _Row(id=131, po_number="910634", spec_code="GR-350a-WTF",
               parent_spec_code=None, item_description="Main Drapery Fabric",
               quantity_received=196.0, unit_of_measure="YD",
               package_quantity=None, package_uom=None)
    result = po_verify.verify_records(conn, [row], client_factory=Lapsed)[0]

    assert result.source == po_verify.SOURCE_MIRROR
    assert result.read_at == "2026-08-11T18:32:45"
    assert "cookie has expired" in result.error
    # The figures still arrive — from the mirror, and labelled as such.
    assert result.matched.qty_ordered == 196.0
    assert result.matched.qty_received == 196.0
    # Header too. Quantities under a blank vendor read as data we do not have, rather than as data
    # from the last time Spitfire could be reached.
    assert result.vendor_name == "P. Kaufmann"
    assert result.doc_status_label == "Committed"
    assert result.order_date == "2025-08-17"
    conn.close()


def test_a_successful_live_read_refreshes_the_mirror(tmp_path):
    from pipeline import spitfire_mirror, state_db

    conn = state_db.get_connection(str(tmp_path / "t.sqlite3"))

    class Live:
        def resolve_po(self, po_number):
            return "doc-key"

        def read_po(self, key):
            return doc(lines=[line(received=12.0)])

    row = _Row(id=131, po_number="910634", spec_code="GR-350a-WTF", parent_spec_code=None,
               item_description=None, quantity_received=196.0, unit_of_measure="YD",
               package_quantity=None, package_uom=None)
    result = po_verify.verify_records(conn, [row], client_factory=Live)[0]

    assert result.source == po_verify.SOURCE_LIVE
    assert result.error is None
    assert [l.spec_code for l in spitfire_mirror.lines_for(conn, "910634")] == ["GR-350a-WTF"]
    assert spitfire_mirror.refreshed_at(conn, "910634") is not None
    conn.close()
