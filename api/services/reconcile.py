"""Mock stages 4-5 — reconcile an extracted record against the PO lines, then decide whether a
human needs to look at it.

This deliberately speaks the pipeline's own vocabulary (`MatchResult.confidence`, `RouteTarget`)
and reuses the pipeline's own threshold (`settings.DESC_MATCH_THRESHOLD`) and fuzzy matcher
(rapidfuzz), so when the real `stage4_match.py` is written the dashboard keeps working against
the same values. It lives here, not in the stage stubs, so building it did not disturb the
pipeline.

Scoring follows the workflow doc: three signals — PO number, spec (which resolves the exact
line), and a fuzzy description match. Three signals is a certainty, fewer is not.
"""
from dataclasses import dataclass
from typing import List, Optional, Sequence

from rapidfuzz import fuzz

from api.stores import reconciliation_store
from api.stores.po_lines_store import POLineRow
from config import settings
from pipeline.models import ExtractedRecord, RouteTarget

# A receipt cannot be posted without these three. Anything missing goes to a person even if the
# line match itself is certain — that is the "not enough info" case.
REQUIRED_FIELDS = ("spec_code", "quantity_received", "pod_stated_date")

FIELD_LABELS = {
    "spec_code": "spec ID",
    "quantity_received": "quantity",
    "pod_stated_date": "POD date",
    "po_number": "PO number",
}

CONFIDENCE_BY_SIGNALS = {3: "high", 2: "medium", 1: "low", 0: "none"}


@dataclass
class Candidate:
    """One PO line scored against the record, with the per-signal breakdown the review drawer
    shows so a reviewer can see *why* it ranked where it did."""
    po_line: POLineRow
    po_signal: bool
    spec_signal: bool
    desc_signal: bool
    desc_score: float

    @property
    def signals_matched(self) -> int:
        return int(self.po_signal) + int(self.spec_signal) + int(self.desc_signal)

    @property
    def confidence(self) -> str:
        return CONFIDENCE_BY_SIGNALS[self.signals_matched]


def _normalize(value: Optional[str]) -> str:
    return (value or "").strip().upper()


def score_candidate(record: ExtractedRecord, po_line: POLineRow) -> Candidate:
    """PO and spec are exact comparisons — the spec is what resolves the exact line, so a fuzzy
    spec match would be worse than none. Only the free-text description is fuzzy."""
    line = po_line.line
    po_signal = bool(record.po_number) and _normalize(record.po_number) == _normalize(line.po_number)

    record_spec = _normalize(record.spec_code) or _normalize(record.parent_spec_code)
    spec_signal = bool(record_spec) and record_spec == _normalize(line.spec_code)

    desc_score = 0.0
    if record.item_description and line.description:
        desc_score = float(fuzz.token_sort_ratio(record.item_description, line.description))
    desc_signal = desc_score >= settings.DESC_MATCH_THRESHOLD

    return Candidate(
        po_line=po_line, po_signal=po_signal, spec_signal=spec_signal,
        desc_signal=desc_signal, desc_score=round(desc_score, 1),
    )


def rank_candidates(
    record: ExtractedRecord, po_lines: Sequence[POLineRow], limit: int = 5
) -> List[Candidate]:
    """Best first. Only candidates with at least one signal are worth showing a reviewer —
    everything else is noise from an unrelated PO."""
    scored = [score_candidate(record, po_line) for po_line in po_lines]
    scored = [candidate for candidate in scored if candidate.signals_matched > 0]
    scored.sort(key=lambda c: (c.signals_matched, c.desc_score), reverse=True)
    return scored[:limit]


def missing_required_fields(record: ExtractedRecord) -> List[str]:
    missing = [field for field in REQUIRED_FIELDS if getattr(record, field) in (None, "")]
    if not record.po_number:
        missing.insert(0, "po_number")
    return missing


def compute_match(
    extracted_record_id: int,
    record: ExtractedRecord,
    po_lines: Sequence[POLineRow],
    now: str,
) -> reconciliation_store.MatchRow:
    """The full verdict for one record: best line, confidence, why it is (or is not) flagged,
    and where it routes."""
    candidates = rank_candidates(record, po_lines, limit=1)
    best = candidates[0] if candidates else None

    missing = missing_required_fields(record)
    reasons: List[str] = []

    if best is None:
        confidence = "none"
        reasons.append("no PO line matched")
    else:
        confidence = best.confidence
        if not best.spec_signal:
            reasons.append("spec did not resolve a line")
        if not best.desc_signal and record.item_description:
            reasons.append(
                f"description match {best.desc_score:.0f}% "
                f"(below {settings.DESC_MATCH_THRESHOLD})"
            )

    if missing:
        reasons.append("missing " + ", ".join(FIELD_LABELS[field] for field in missing))

    # Mock stage 5 verify: receiving more than the line still has outstanding is never posted
    # automatically, however confident the match.
    over_receipt = False
    if best is not None and record.quantity_received is not None:
        outstanding = best.po_line.qty_outstanding
        if record.quantity_received > outstanding:
            over_receipt = True
            reasons.append(
                f"quantity {record.quantity_received:g} exceeds {outstanding:g} outstanding"
            )

    flagged = confidence in ("low", "none") or bool(missing) or over_receipt

    return reconciliation_store.MatchRow(
        extracted_record_id=extracted_record_id,
        po_line_id=best.po_line.id if best else None,
        po_signal=best.po_signal if best else False,
        spec_signal=best.spec_signal if best else False,
        desc_signal=best.desc_signal if best else False,
        desc_score=best.desc_score if best else 0.0,
        signals_matched=best.signals_matched if best else 0,
        confidence=confidence,
        missing_fields=missing,
        flagged=flagged,
        flag_reason=" · ".join(reasons),
        route_target=(RouteTarget.EXCEPTION_QUEUE.value if flagged else RouteTarget.AUTO_APPROVED.value),
        review_status=(
            reconciliation_store.REVIEW_PENDING if flagged
            else reconciliation_store.REVIEW_AUTO_APPROVED
        ),
        notes="",
        created_at=now,
    )
