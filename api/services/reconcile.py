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
import re
from dataclasses import dataclass, field
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

# Lines that exist on a PO but can never receive goods. `connectors.spitfire.is_tax_line` screens
# these by `AccountCategory` on the live read, but the mirror and the demo store keep only the
# description, so the same lines must be screened again by what they say. PO 207249 line 0002 is
# "Delivery & Installation" carrying `AccountCategory: SUB-FDP` — it passes the account screen and
# would otherwise be offered as a candidate for a treadmill.
NON_RECEIVABLE_DESCRIPTION = re.compile(
    r"^\s*(tax|freight|shipping|delivery\s*&\s*install\w*|install\w*)\b", re.I)

# The vendor's own code, which is what actually identifies an item when the spec does not. Premier's
# POs carry it inline in the description under several labels: "CODE: DGY100LBNRNR20",
# "Item: ST-5", "Model Number: AC-347371". Four or more characters so a stray "Item: 1" cannot
# match half the PO.
VENDOR_CODE = re.compile(r"(?:CODE|Item|Model\s*Number|Part)\s*[:#]\s*([A-Z0-9][A-Z0-9\-.]{3,})", re.I)

NUMBER = re.compile(r"\d+(?:\.\d+)?")


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

    # The sub-spec first, then the parent. Both exact — widening *which* codes are compared, not
    # how, so the rule above still holds.
    #
    # Vendors ship a lamp as parts: Authority Inbound splits `STE-402-LT` into `STE-402-LT-B`
    # (base) and `STE-402-LT-SH` (shade), and the purchase order carries only the assembled
    # `STE-402-LT`. The sub-spec therefore never matches a line, and `or` short-circuits — the
    # parent the parser went to the trouble of extracting was never tried. Record 208 landed on
    # the right line only because its description happened to score 94%.
    record_spec = _normalize(record.spec_code)
    parent_spec = _normalize(record.parent_spec_code)
    line_spec = _normalize(line.spec_code)
    # `bool(line_spec)` so a line with no spec cannot match a record with no spec: two blanks are
    # not an identification.
    spec_signal = bool(line_spec) and (
        (bool(record_spec) and record_spec == line_spec)
        or (bool(parent_spec) and parent_spec == line_spec)
    )

    # `token_set_ratio`, not `token_sort_ratio`. A PO line description is far longer than what an
    # email says about the same goods — "EXCITE LIVE VARIO LIVE 16 P 5000 METEOR BLACK CODE:
    # DFFU3Q3AAN00EA2U" against "VARIO LIVE 16 P 5000" — and sort-ratio scores that asymmetry down
    # as a difference rather than recognising the subset. Measured over the 92-record corpus:
    # set-ratio resolves 84%, sort-ratio 75%.
    #
    # The cost of set-ratio is that it discounts tokens the record does not mention, so items that
    # differ only by a size score within a point or two of each other: PO 207249's four medicine
    # balls scored 100 on the right line and 94.1 on the wrong one, a gap of 5.9 that no threshold
    # can safely split. `_by_numbers` below separates them, and must run before scoring, not after.
    desc_score = 0.0
    if record.item_description and line.description:
        desc_score = float(fuzz.token_set_ratio(record.item_description, line.description))
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


@dataclass
class Resolution:
    """Which PO line the record is receiving against, and how that was decided.

    `chosen` is None when the record is genuinely ambiguous. That is a real answer, not a failure:
    23 signs on PO 207514 all carry spec `LOB-900-SI`, and an email saying only "Exit" does not
    identify one of them. `tied` carries what it could not choose between so a reviewer sees the
    same shortlist the matcher did.
    """
    chosen: Optional[POLineRow]
    step: str
    tied: List[POLineRow] = field(default_factory=list)


def _receivable(record: ExtractedRecord, po_lines: Sequence[POLineRow]) -> List[POLineRow]:
    """Drop labour and freight lines — unless the record names one by its spec.

    Offering "Delivery & Installation" as a candidate for a treadmill invites a wrong match on a
    PO whose specs repeat, which is why these are screened out at all. But Premier does receive
    against them: PO 206993 line 0003 *is* `Installation`, its spec is `FIT-902b-EQ`, and the
    corpus holds a record for exactly that spec reading "Installation of Water Dispenser".

    So the screen protects against a wrong match, never against a right one. An exact spec match
    is the record naming this line on purpose, and outranks the screen.
    """
    named = {_normalize(record.spec_code), _normalize(record.parent_spec_code)} - {""}
    return [row for row in po_lines
            if not NON_RECEIVABLE_DESCRIPTION.match(row.line.description or "")
            or _normalize(row.line.spec_code) in named]


def _by_spec(record: ExtractedRecord, rows: Sequence[POLineRow]) -> List[POLineRow]:
    """Sub-spec then parent, exact — the same rule `score_candidate` applies, used as a filter."""
    record_spec = _normalize(record.spec_code)
    parent_spec = _normalize(record.parent_spec_code)
    if not (record_spec or parent_spec):
        return list(rows)
    narrowed = [r for r in rows if _normalize(r.line.spec_code)
                and _normalize(r.line.spec_code) in (record_spec, parent_spec)]
    return narrowed or list(rows)


def _by_vendor_code(record: ExtractedRecord, rows: Sequence[POLineRow]) -> List[POLineRow]:
    """The vendor's code out of the record's description, matched into the PO line's.

    This is what identifies an item when the spec cannot. Every one of PO 207249's 21 fitness
    items carries spec `FIT-900-FIT`; they are told apart by `CODE: DGY100LBNRNR20` and its
    siblings, which the PO line descriptions also carry.
    """
    codes = {m.group(1).upper() for m in VENDOR_CODE.finditer(record.item_description or "")}
    if not codes:
        return list(rows)
    narrowed = [r for r in rows
                if any(code in (r.line.description or "").upper() for code in codes)]
    return narrowed or list(rows)


def _by_numbers(record: ExtractedRecord, rows: Sequence[POLineRow]) -> List[POLineRow]:
    """Every number the record states must appear in the PO line.

    Sizes and weights are the whole identity of some items and are invisible to a token-set
    score: "Medicine Ball 4 Kg" and "Medicine Ball 11 Kg" both score 100 against each other. This
    is a subset test rather than equality because the PO line carries numbers the email does not —
    part codes, dimensions, wattage.
    """
    wanted = set(NUMBER.findall(record.item_description or ""))
    if not wanted:
        return list(rows)
    narrowed = [r for r in rows if wanted <= set(NUMBER.findall(r.line.description or ""))]
    return narrowed or list(rows)


def _by_uom(record: ExtractedRecord, rows: Sequence[POLineRow]) -> List[POLineRow]:
    """Units must agree where both sides state one. Silence on either side is not disagreement.

    PO 210635 carries two `GR-350c-WTF` lines — 84 YD and 1 EA — with descriptions that score
    identically. The unit is the only thing that separates them.
    """
    # Imported here, not at module scope: `po_verify` imports this module for `score_candidate`,
    # so a top-level import is a cycle. One alias map, one owner — duplicating it here would drift.
    from pipeline.po_verify import uom_key

    unit = uom_key(record.unit_of_measure)
    if not unit:
        return list(rows)
    narrowed = [r for r in rows
                if not uom_key(r.line.unit_of_measure) or uom_key(r.line.unit_of_measure) == unit]
    return narrowed or list(rows)


def _by_quantity(record: ExtractedRecord, rows: Sequence[POLineRow]) -> List[POLineRow]:
    """A line that ordered less than was delivered is not the line that was delivered against.

    Weak on its own and deliberately last of the hard filters: it uses `qty_ordered` rather than
    outstanding, because a second delivery against a partly-received line is legitimate and
    outstanding would reject it.
    """
    quantity = record.quantity_received
    if quantity is None or quantity <= 0:
        return list(rows)
    narrowed = [r for r in rows if (r.line.qty_ordered or 0) >= quantity]
    return narrowed or list(rows)


def resolve_line(record: ExtractedRecord, po_lines: Sequence[POLineRow]) -> Resolution:
    """Which line this record is receiving against — a ladder of hard filters, fuzzy only at the end.

    Ordering is the point. Each rung is an exact fact about the goods; the description score is a
    guess, and a guess must not overrule a fact. The old behaviour scored every line and took the
    top one with no tie check, which meant a record whose spec matched 21 lines was resolved by
    whichever description happened to score highest — silently, with no signal that it had been a
    coin toss. Measured over the 92-record corpus, the ladder resolves 77 where scoring alone
    resolved 33.

    Returning `Resolution(chosen=None)` is the honest outcome for a genuine tie and is what routes
    the record to a person.
    """
    # The purchase order first, and as a hard gate rather than a signal. A line belonging to some
    # other PO is not a weaker candidate, it is not a candidate: Spitfire receipts are a child of
    # exactly one PO. Callers usually pass only that PO's lines, but not always — the demo store
    # holds every line in one table.
    if record.po_number:
        po_lines = [row for row in po_lines
                    if _normalize(row.line.po_number) == _normalize(record.po_number)]
        if not po_lines:
            return Resolution(None, "no lines on that purchase order")

    rows = _receivable(record, po_lines)
    if not rows:
        return Resolution(None, "no receivable lines")

    for step, narrow in (("spec", _by_spec), ("vendor code", _by_vendor_code),
                         ("size", _by_numbers), ("unit", _by_uom), ("quantity", _by_quantity)):
        rows = narrow(record, rows)
        if len(rows) == 1:
            return Resolution(rows[0], step)

    if not record.item_description:
        return Resolution(None, "no description to compare", tied=list(rows))

    scored = sorted(rows, key=lambda r: score_candidate(record, r).desc_score, reverse=True)
    best = score_candidate(record, scored[0]).desc_score
    second = score_candidate(record, scored[1]).desc_score if len(scored) > 1 else 0.0
    if best >= settings.DESC_MATCH_THRESHOLD and (best - second) >= settings.DESC_MATCH_GAP:
        return Resolution(scored[0], "description")

    # Everything still standing is what the reviewer needs to see, not just the top one.
    tied = [r for r in scored if score_candidate(record, r).desc_score >= second] or scored[:2]
    return Resolution(None, "ambiguous", tied=tied)


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
    resolution = resolve_line(record, po_lines)
    best = score_candidate(record, resolution.chosen) if resolution.chosen else None

    missing = missing_required_fields(record)
    reasons: List[str] = []

    if best is None:
        confidence = "none"
        if resolution.tied:
            # Name them. "no PO line matched" sent a reviewer back to the whole purchase order;
            # what they actually need is the shortlist the matcher could not choose between.
            lines = ", ".join(str(row.line.line_number) for row in resolution.tied[:5])
            reasons.append(f"ambiguous — {len(resolution.tied)} candidate lines ({lines})")
        else:
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
