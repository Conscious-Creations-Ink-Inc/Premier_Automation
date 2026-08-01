"""Mock stage 4/5 scoring — the rules that decide whether a person is needed."""
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
