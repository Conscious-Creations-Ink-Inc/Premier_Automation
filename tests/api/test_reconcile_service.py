"""Mock stage 4/5 scoring — the rules that decide whether a person is needed."""
from rapidfuzz import fuzz

from api.services import reconcile
from api.stores import reconciliation_store
from config import settings
from pipeline.models import RouteTarget

NOW = "2026-07-20T00:00:00+00:00"


def test_three_signals_and_complete_settles_without_a_human(record_factory, line_factory):
    match = reconcile.compute_match(1, record_factory(), [line_factory()], NOW)

    assert (match.po_signal, match.spec_signal, match.desc_signal) == (True, True, True)
    assert match.signals_matched == 3
    assert match.confidence == "high"
    assert match.flagged is False
    assert match.route_target == RouteTarget.AUTO_APPROVED.value
    assert match.review_status == reconciliation_store.REVIEW_AUTO_APPROVED


def test_missing_spec_is_flagged_even_when_the_po_matches(record_factory, line_factory):
    match = reconcile.compute_match(1, record_factory(spec_code=None), [line_factory()], NOW)

    assert match.spec_signal is False
    assert "spec_code" in match.missing_fields
    assert match.flagged is True
    assert match.route_target == RouteTarget.EXCEPTION_QUEUE.value
    assert match.review_status == reconciliation_store.REVIEW_PENDING


def test_missing_quantity_is_flagged_despite_a_certain_line_match(record_factory, line_factory):
    """The 'not enough info' case: we know exactly which line, but not how many arrived."""
    match = reconcile.compute_match(1, record_factory(quantity_received=None), [line_factory()], NOW)

    assert match.confidence == "high"
    assert match.signals_matched == 3
    assert match.missing_fields == ["quantity_received"]
    assert match.flagged is True


def test_missing_pod_date_is_flagged(record_factory, line_factory):
    match = reconcile.compute_match(1, record_factory(pod_stated_date=None), [line_factory()], NOW)

    assert match.missing_fields == ["pod_stated_date"]
    assert match.flagged is True


def test_unknown_po_has_no_candidate_at_all(record_factory, line_factory):
    match = reconcile.compute_match(
        1, record_factory(po_number="999111", spec_code=None, item_description="something else"),
        [line_factory()], NOW,
    )

    assert match.po_line_id is None
    assert match.signals_matched == 0
    assert match.confidence == "none"
    assert match.flagged is True
    assert "no PO line matched" in match.flag_reason


def test_over_receipt_is_flagged_however_certain_the_match(record_factory, line_factory):
    line = line_factory(qty_ordered=12.0, qty_received=10.0)   # only 2 outstanding
    match = reconcile.compute_match(1, record_factory(quantity_received=5.0), [line], NOW)

    assert match.confidence == "high"
    assert match.missing_fields == []
    assert match.flagged is True
    assert "exceeds 2 outstanding" in match.flag_reason


def test_weak_description_does_not_earn_a_signal(record_factory, line_factory):
    match = reconcile.compute_match(
        1, record_factory(spec_code=None, item_description="totally unrelated text"),
        [line_factory()], NOW,
    )

    assert match.desc_signal is False
    assert match.desc_score < settings.DESC_MATCH_THRESHOLD
    assert match.confidence == "low"   # PO number alone


def test_candidates_are_ranked_best_first(record_factory, line_factory):
    exact = line_factory(line_id=1)
    same_po_only = line_factory(line_id=2, line_number=2, spec_code="STE-402-UP",
                                description="Steelcase 402 Upper Shelf Unit")

    ranked = reconcile.rank_candidates(record_factory(), [same_po_only, exact])

    assert ranked[0].po_line.id == exact.id
    assert ranked[0].signals_matched > ranked[1].signals_matched


# --- the ladder: exact facts before fuzzy scores, and a refusal instead of a guess -------------
#
# Every case below is drawn from Premier's own corpus, where the spec code stopped identifying a
# line: 82 of 179 purchase order lines share their spec with another line on the same PO.


def test_two_lines_sharing_a_spec_are_not_silently_guessed(record_factory, line_factory):
    """The bug this ladder exists for.

    PO 207514 carries 23 signs, every one of them spec `LOB-900-SI`. An email saying "Exit" does
    not identify one. The old scorer sorted by description and returned the top row with no tie
    check, so the record was resolved by whichever line happened to score highest — and nothing
    recorded that it had been a coin toss.
    """
    match = reconcile.compute_match(
        1, record_factory(item_description="Exit"),
        [line_factory(1, line_number=4, description="Exit sign, acrylic"),
         line_factory(2, line_number=9, description="Exit sign, acrylic")],
        NOW,
    )

    assert match.po_line_id is None
    assert match.flagged is True
    assert match.route_target == RouteTarget.EXCEPTION_QUEUE.value
    assert "ambiguous" in match.flag_reason
    # The reviewer gets the shortlist, not the whole purchase order.
    assert "4" in match.flag_reason and "9" in match.flag_reason


def test_vendor_code_resolves_lines_that_share_a_spec(record_factory, line_factory):
    """PO 207249: 21 fitness items, all `FIT-900-FIT`, told apart only by the vendor's own code."""
    chosen = line_factory(2, line_number=6, description="TECHNOGYM BENCH CODE: DGY100LBNRNR20")
    match = reconcile.compute_match(
        1, record_factory(item_description="TECHNOGYM BENCH CODE: DGY100LBNRNR20"),
        [line_factory(1, line_number=5, description="ADJUSTABLE BENCH CODE: PA04-ANZ0GG"), chosen],
        NOW,
    )

    assert match.po_line_id == 2
    assert match.flagged is False


def test_size_separates_items_a_token_set_score_calls_identical(record_factory, line_factory):
    """Four medicine balls on PO 207249 differ only by weight, and a description score cannot
    separate them: measured on the corpus the right line scored 100 and the wrong one 94.1 — a
    gap of 5.9, well inside `DESC_MATCH_GAP`. Scoring alone would refer all four to a person."""
    assert fuzz.token_set_ratio("Medicine Ball 4 Kg", "Medicine Ball 11 Kg") > 90
    assert 100 - fuzz.token_set_ratio("Medicine Ball 4 Kg",
                                      "Medicine Ball 11 Kg") < settings.DESC_MATCH_GAP

    match = reconcile.compute_match(
        1, record_factory(item_description="Medicine Ball 11 Kg"),
        [line_factory(1, line_number=20, description="Medicine Ball 4 Kg"),
         line_factory(2, line_number=21, description="Medicine Ball 11 Kg")],
        NOW,
    )

    assert match.po_line_id == 2
    assert match.flagged is False


def test_unit_separates_two_lines_with_the_same_spec_and_description(record_factory, line_factory):
    """PO 210635 orders `GR-350c-WTF` twice — 84 YD and 1 EA. The unit is all there is."""
    match = reconcile.compute_match(
        1, record_factory(item_description="Sheer Fabric", unit_of_measure="YD",
                          quantity_received=78.0),
        [line_factory(1, line_number=1, description="Sheer Fabric",
                      unit_of_measure="EA", qty_ordered=1.0),
         line_factory(2, line_number=2, description="Sheer Fabric",
                      unit_of_measure="YD", qty_ordered=84.0)],
        NOW,
    )

    assert match.po_line_id == 2


def test_a_labour_line_is_not_offered_as_a_candidate_for_goods(record_factory, line_factory):
    """PO 207249 line 0002 is "Delivery & Installation" carrying `AccountCategory: SUB-FDP`, so it
    survives the account-category screen in `connectors.spitfire.is_tax_line`. A treadmill cannot
    be received against labour, and offering it invites a wrong match on a PO whose specs repeat.
    """
    match = reconcile.compute_match(
        1, record_factory(spec_code="FIT-900-FIT", item_description="TECHNOGYM BENCH"),
        [line_factory(1, line_number=2, spec_code="FIT-900-INS",
                      description="Delivery & Installation"),
         line_factory(2, line_number=6, spec_code="FIT-900-FIT",
                      description="TECHNOGYM BENCH CODE: DGY100LBNRNR20")],
        NOW,
    )

    assert match.po_line_id == 2


def test_but_a_record_may_name_a_labour_line_by_its_spec(record_factory, line_factory):
    """The screen must not block a delivery Premier really does receive.

    PO 206993 line 0003 *is* `Installation`, its spec is `FIT-902b-EQ`, and the corpus carries a
    record for exactly that spec reading "Installation of Water Dispenser". An exact spec match is
    the record naming the line on purpose and outranks the screen.
    """
    match = reconcile.compute_match(
        1, record_factory(spec_code="FIT-902b-EQ",
                          item_description="Installation of Water Dispenser"),
        [line_factory(1, line_number=3, spec_code="FIT-902b-EQ", description="Installation"),
         line_factory(2, line_number=6, spec_code="FIT-900-FIT", description="TECHNOGYM BENCH")],
        NOW,
    )

    assert match.po_line_id == 1


def test_a_line_on_another_purchase_order_is_not_a_weak_candidate_but_no_candidate(
        record_factory, line_factory):
    """A Spitfire receipt is a child of exactly one PO, so the PO number is a gate, not a signal."""
    resolution = reconcile.resolve_line(
        record_factory(po_number="212456"), [line_factory(1, po_number="999111")])

    assert resolution.chosen is None
    assert resolution.step == "no lines on that purchase order"
