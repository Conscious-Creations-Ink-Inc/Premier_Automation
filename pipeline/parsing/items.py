"""Is this the same item? — one definition, used wherever two item names are compared.

Two callers, asking the same question from opposite ends:

* `ingest_orchestrator._descriptions_conflict` — are two rows of one document the same line?
* `record_completion.resolve_line_by_description` — which purchase-order line is this row?

They must agree, or a pair of rows kept apart at ingest could be resolved onto one line
afterwards, and the double receipt that grouping exists to prevent arrives by the back door.

**Why not `token_set_ratio`.** It scores a subset as a perfect match, which is right when asking
whether a terser wording names the same thing ("Custom Accessory Pocket" vs "Accessory Pocket",
100) and badly wrong when asking whether two entries in one list are the same item:

    Exit                     vs Exit Route                 -> set 100, sort  57
    Accesible Lift           vs Handicap Lift Accesible    -> set 100, sort  76
    Restroom Door Placard    vs Restroom Wall Placard      -> set  86, sort  76
    Maximum Occupancy        vs Max Occupancy              -> set  87, sort  87

Every one of the first three is a separate Spitfire line on PO 207514; only the last is one item
named twice. `token_sort_ratio` separates all four correctly at a threshold of 85.

**Why digits are decisive.** Fuzzy scoring cannot see that a number carries the whole meaning.
"Medicine Ball 4 Kg", "6 Kg", "9 Kg" and "11 Kg" differ by one character and score 94 against each
other, yet they are lines 14, 15, 16 and 13, each ordered 1 and each with its own CODE. So a
difference in the digits present settles the question before any score is consulted.
"""

from __future__ import annotations

import re
from typing import Any, Optional, Sequence, Tuple

from rapidfuzz import fuzz

SAME_ITEM_THRESHOLD = 85
"""rapidfuzz `token_sort_ratio`, 0-100. See the module docstring for why this metric."""

_DIGITS_RE = re.compile(r"\d+")

_CATALOGUE_CODE_RE = re.compile(
    r"\s*\b(?:item|code|spec|sku|model|part)\s*(?:no\.?|number|#)?\s*[:#]\s*\S+\s*$",
    re.IGNORECASE,
)


def normalise(text: Optional[str]) -> str:
    """Collapse whitespace and case. Nothing else — this is not a content judgement."""
    return " ".join((text or "").split()).lower()


def digits(text: Optional[str]) -> list:
    """Every run of digits, in order. `Medicine Ball 11 Kg` -> `['11']`."""
    return _DIGITS_RE.findall(normalise(text))


def strip_catalogue_code(text: Optional[str]) -> str:
    """Drop a trailing `Item: ST-3` / `CODE: A0001002` from a purchase-order line description.

    Spitfire stores the catalogue code inside the description; the delivery paperwork does not
    repeat it. So `P.S. Small Directional Item: ST-3` and the email's `P.S. Small Directional` are
    one item — but the code carries digits the email never mentions, and the digit rule above,
    rightly strict about numbers, then rejects every candidate outright. Measured before this
    existed: **0 of 23** PO 207514 rows matched any line, every one scoring 0.

    Only a trailing `LABEL: value` is removed, so `Item Description Only` keeps its wording.
    """
    stripped = _CATALOGUE_CODE_RE.sub("", (text or "").strip())
    return stripped or (text or "")


def similarity(left: Optional[str], right: Optional[str]) -> float:
    """How alike two item names read, 0-100. Blank on either side scores 0."""
    a, b = normalise(left), normalise(right)
    if not a or not b:
        return 0.0
    return float(fuzz.token_sort_ratio(a, b))


def same_item(left: Optional[str], right: Optional[str]) -> bool:
    """Do these two names describe the same item?

    False when either is blank — absence is not a match. Callers that need "a missing name must not
    separate anything" must test for blankness themselves; that is a different question and the
    grouping code asks it explicitly.
    """
    a, b = normalise(left), normalise(right)
    if not a or not b:
        return False
    if digits(a) != digits(b):
        return False
    return similarity(a, b) >= SAME_ITEM_THRESHOLD


def best_match(description: Optional[str], candidates: Sequence[Any], *,
               key=lambda c: c,
               threshold: float = SAME_ITEM_THRESHOLD) -> Tuple[Optional[Any], float, bool]:
    """The candidate whose name best matches `description`: `(candidate, score, is_unique)`.

    `is_unique` is False when two or more candidates tie at the top score. A tie is not a match —
    it means the description cannot tell those lines apart, and the caller must leave the decision
    to a person rather than take the first.

    Each candidate's name has its catalogue code stripped, then digits are applied as a filter
    before any scoring, so `Medicine Ball 4 Kg` never scores against `Medicine Ball 6 Kg` at all.
    """
    wanted = normalise(strip_catalogue_code(description))
    if not wanted:
        return None, 0.0, False

    scored = []
    for candidate in candidates:
        name = normalise(strip_catalogue_code(key(candidate)))
        if not name or digits(name) != digits(wanted):
            continue
        scored.append((similarity(wanted, name), candidate))

    if not scored:
        return None, 0.0, False

    best_score = max(score for score, _ in scored)
    if best_score < threshold:
        return None, best_score, False

    tied = [candidate for score, candidate in scored if score == best_score]
    return tied[0], best_score, len(tied) == 1
