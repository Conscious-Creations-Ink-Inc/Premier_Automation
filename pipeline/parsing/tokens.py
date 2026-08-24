"""The token grammar: PO numbers, spec codes, quantities/UOM and dates.

Written against the real June corpus. The single most important rule here is that a **bare
6-digit number is not a PO number**. In one Authority Logistics subject —

    [External] 239260 - Inbound Notification - 206725, 207665 - 2978 : LXR Cameo ... - MRC

— 239260 is the inbound number and 206725/207665 are the POs, all six digits. Tracking numbers
(8801592, 69942177, 31457971), Authority/shipment numbers (50009, 49985) and project numbers
(2978, 2985) coexist in the same text. So a PO is only ever recognised from a *position*: a
label, a subject slot, or a known PO column. `find_po_numbers` will not guess.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

# --- PO numbers -------------------------------------------------------------

PO_LENGTH_RE = re.compile(r"^\d{6}$")

# Labelled forms, all observed or plausible: "PO 213987", "PO# 213987", "P.O.# 210634",
# "PO: 213987", "PO #213987", "Purchase Order 210634". Case-insensitive — the old
# settings.PO_TOKEN_REGEX was not, and missed every lowercase "po #" (finding C4).
PO_LABELLED_RE = re.compile(
    r"\b(?:P\.?\s?O\.?|purchase\s+order)s?\s*(?:numbers?|nos?\.?|#)?\s*[:#]?\s*"
    r"(\d{6}(?:\s*(?:,|/|&|\+|and)\s*(?:P\.?\s?O\.?\s*#?\s*)?\d{6})*)",
    re.IGNORECASE,
)
# The trailing group deliberately swallows a whole list. One label routinely covers several POs
# — `PO 207505, 207514, 212559, 207249, 208705, 212560, 212614` and `PO 211310 + PO 212578` are
# both real subjects — and matching only the first left six of seven POs unaccumulated.

# "206725 : 1" / "208491 : 300" — the Authority Inbound `PO # / Line #` cell. This is the
# richest token in the whole corpus: it hands us the PO *and* the Spitfire line number.
PO_LINE_REF_RE = re.compile(r"^\s*(\d{6})\s*:\s*(\d{1,5})\s*$")

# A comma/slash-separated PO list as it appears in a subject slot: "206725, 207665".
PO_LIST_RE = re.compile(r"\b\d{6}\b")


@dataclass(frozen=True)
class PoLineRef:
    po_number: str
    line_number: Optional[int]


def parse_po_line_ref(cell: str) -> Optional[PoLineRef]:
    """`"208491 : 300"` -> PoLineRef("208491", 300). Returns None for anything else, including a
    bare PO with no line — callers that accept a bare PO must say so explicitly."""
    if not cell:
        return None
    match = PO_LINE_REF_RE.match(cell.replace("\xa0", " "))
    if match:
        return PoLineRef(match.group(1), int(match.group(2)))
    return None


def find_po_numbers(text: str) -> List[str]:
    """POs from *labelled* occurrences only, in order of first appearance, deduped.

    Deliberately conservative: it returns nothing for the Authority Logistics emails, whose POs
    live in a subject slot and a table column with no "PO" prefix anywhere near them. Those are
    the job of `parse_po_list` (given an already-isolated slot) and the vendor parsers.
    """
    if not text:
        return []
    seen: List[str] = []
    for group in PO_LABELLED_RE.findall(text):
        for po in PO_LIST_RE.findall(group):
            if po not in seen:
                seen.append(po)
    return seen


def parse_po_list(slot_text: str) -> List[str]:
    """Every 6-digit token in an already-isolated PO slot ("206725, 207665").

    Only call this with text you have already established *is* a PO field — a subject slot
    matched by a vendor grammar, or a cell under a `PO #` header. Calling it on free text
    reintroduces exactly the false positives the module docstring warns about.
    """
    if not slot_text:
        return []
    seen: List[str] = []
    for po in PO_LIST_RE.findall(slot_text):
        if po not in seen:
            seen.append(po)
    return seen


# --- Spec codes -------------------------------------------------------------

# {AREA}-{NUM}{letter?}{.sub?}-{TYPE}{-PART?}. Verified against every spec in the corpus:
# STE-402-LT-B, GR-350a-WTF, GR-350c-WTF, GR-350d-WTF, POOL-201.1-PL, POOL-201-SG, POOL-925-AC,
# EXT-925-AC .. EXT-932-AC, EXT-930-KCK, EXT-930-PCK, RES-100b-EQ, LOB-203-PI, FIT-902-TV,
# TI-40, UNI-05.
SPEC_RE = re.compile(r"\b([A-Z]{2,5}-\d{2,4}[A-Za-z]?(?:\.\d{1,2})?(?:-[A-Z]{1,4})*(?:\.\d{1,2})?)\b")
# The two optional `.n` positions and the case-insensitive variant letter are all load-bearing,
# from specs in the real trackers: `POOL-201.1-PL` (sub-number before the type), `RES-200-SG.1`
# (sub-number after it) and `STE-211R-SG` (uppercase variant letter).
# Case-sensitive on purpose, and matched against the original text rather than an uppercased
# copy. Two real over-captures in the corpus come from ignoring case: `16 EACH -
# POOL-201-SG-Pool Chaises` yields the phantom spec `POOL-201-SG-POOL` (the description word
# "Pool" swallowed as a segment), and a note reading "for the (3) Pool-925-AC planters only"
# yields a spec attribution to a line that has none. Requiring real uppercase rejects both.

# Sub-part suffixes: a lamp arrives as a base and a shade, separately, days apart
# ("WH Inbound - 11 bases, 12 tops of STE-402" then "1 base only"). Both are the same Spitfire
# line; the suffix identifies which physical part it was, which the unequal-parts rule needs.
SUB_PART_SUFFIXES = {"B", "SH", "TOP", "BASE", "KCK", "PCK"}

# Type segments that are part of the spec identity, never a sub-part. Without this list
# `STE-402-LT` would be read as spec STE-402 + part "LT".
_TYPE_SEGMENTS = {
    "LT", "SG", "PI", "AC", "EQ", "WTF", "PL", "TV", "BLB", "CS", "TB", "MR", "AR", "RG", "DR",
    "CH", "SO", "OT", "BS", "HW", "FX", "WC", "LN", "UP",
}


@dataclass(frozen=True)
class SpecCode:
    full: str
    parent: str
    sub_part: Optional[str]


def normalize_spec(raw: str) -> Optional[SpecCode]:
    """Split a spec into the line-identifying parent and the optional physical sub-part.

    `STE-402-LT-B` -> parent `STE-402-LT`, sub-part `B`.
    `EXT-930-KCK`  -> parent `EXT-930`,    sub-part `KCK`.
    `POOL-201.1-PL`-> parent `POOL-201.1-PL`, no sub-part (PL is a type, not a part).
    """
    if not raw:
        return None
    candidate = raw.strip().rstrip(".,;:")
    match = SPEC_RE.search(candidate)
    if not match:
        return None
    full = match.group(1)
    segments = full.split("-")
    if len(segments) >= 3:
        last = segments[-1]
        if last in SUB_PART_SUFFIXES and last not in _TYPE_SEGMENTS:
            return SpecCode(full=full, parent="-".join(segments[:-1]), sub_part=last)
    return SpecCode(full=full, parent=full, sub_part=None)


def find_specs(text: str) -> List[str]:
    """All spec codes in order of first appearance, deduped. Safe on free text — the grammar is
    specific enough that model numbers (`WWR-AL603024`) and standards (`NFPA 701`) don't match."""
    if not text:
        return []
    seen: List[str] = []
    for spec in SPEC_RE.findall(text):
        if spec not in seen:
            seen.append(spec)
    return seen


# --- Quantities and units ---------------------------------------------------

# The trap that breaks receivers: an Authority Inbound header says `Quantity: 41 CTN` (cartons
# on the truck) while the line row says `11 EA` (units received against the PO line). Only the
# second is receivable. Keeping the two vocabularies apart is what enforces that.
ITEM_UOMS = {"EA", "EACH", "YD", "YDS", "SF", "SQFT", "LF", "PC", "PCS", "PIECE", "PIECES", "SET", "PR", "PAIR", "ROLL", "GAL"}
PACKAGE_UOMS = {"PLT", "PALLET", "PALLETS", "CTN", "CTNS", "CARTON", "CARTONS", "SKID", "SKIDS", "BOX", "BOXES", "CRATE", "CRATES", "CS", "CASE", "BUNDLE", "PKG"}

_UOM_ALIASES = {"YDS": "YD", "SQFT": "SF", "PIECE": "PC", "PIECES": "PCS", "CTNS": "CTN", "SKIDS": "SKID", "PALLETS": "PLT", "PAIR": "PR"}

# "2 EACH - ...", "11 EA - ...", "202 YD - ...", "175.04 SF", "9 PLT - 3084.00 lb".
QTY_UOM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([A-Za-z]{2,7})\b")


@dataclass(frozen=True)
class Quantity:
    value: float
    uom: str
    is_package: bool


def _canonical_uom(raw: str) -> str:
    upper = raw.upper()
    return _UOM_ALIASES.get(upper, upper)


def parse_leading_quantity(cell: str) -> Optional[Quantity]:
    """The quantity at the *front* of an Item cell — `"2 EACH - EXT-925-AC ..."` -> 2.0 EACH.

    Anchored to the start on purpose. Item descriptions are full of other numbers
    (`96"Lx30"Wx24"`, `Model Number: WWR-AL603024`, `Width: 56"`); only the leading token is the
    quantity, and every Authority line row in the corpus follows that shape.
    """
    if not cell:
        return None
    text = cell.replace("\xa0", " ").strip()
    match = re.match(r"\s*(\d+(?:\.\d+)?)\s*([A-Za-z]{2,7})\b", text)
    if not match:
        return None
    uom = _canonical_uom(match.group(2))
    if uom not in ITEM_UOMS and uom not in PACKAGE_UOMS:
        return None
    return Quantity(float(match.group(1)), uom, is_package=uom in PACKAGE_UOMS)


def parse_item_quantity(cell: str) -> Optional[Quantity]:
    """`parse_leading_quantity` restricted to receivable item units — returns None for `9 PLT`."""
    quantity = parse_leading_quantity(cell)
    return quantity if quantity and not quantity.is_package else None


def parse_package_quantity(cell: str) -> Optional[Quantity]:
    """`parse_leading_quantity` restricted to package units — `"9 PLT - 3084.00 lb"` -> 9 PLT."""
    quantity = parse_leading_quantity(cell)
    return quantity if quantity and quantity.is_package else None


# --- Dates ------------------------------------------------------------------

# Four formats coexist in the corpus and Excel adds a fifth (real datetimes).
_DATE_FORMATS = [
    "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y",
    "%d %b %Y", "%m-%d-%Y", "%Y/%m/%d",
]
_DATE_RE = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{2,4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|[A-Z][a-z]{2,8}\s+\d{1,2},\s*\d{4})"
)


def normalize_date(raw) -> Optional[str]:
    """Any observed date shape -> `YYYY-MM-DD`. Returns None rather than guessing.

    Trailing time is tolerated and discarded (`"10/31/25 at 3:22"`, `"Sep 10, 2025 10:18"`) —
    Spitfire receipts carry a date, not a timestamp, and the mailbox timezone is not readable
    with the permissions we hold (MailboxSettings.Read was denied), so an hour we cannot place
    in a timezone is worse than no hour at all.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if hasattr(raw, "isoformat") and not isinstance(raw, str):
        return raw.isoformat()[:10]

    text = str(raw).strip().replace("\xa0", " ")
    if not text:
        return None

    match = _DATE_RE.search(text)
    if not match:
        return None
    candidate = match.group(1)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(candidate, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# --- Shipment / tracking / carrier reference numbers -------------------------

# "ALS Shipment #: 50052 : 1" (Inbound) and "Authority #: 50009" (Delivered) are the *same*
# number under two names — the join that stops one physical delivery becoming two receivers.
SHIPMENT_SUFFIX_RE = re.compile(r"^\s*(\d{4,8})\s*(?::\s*(\d{1,3}))?\s*$")


def parse_shipment_number(raw: str) -> Optional[str]:
    """`"50052 : 1"` -> `"50052"`, `"50009"` -> `"50009"`, `""` -> None.

    The `: 1` suffix is Authority's own leg counter within a shipment; dropping it is what makes
    the Delivered<->Inbound join work, since the Delivered side never carries it.
    """
    if not raw:
        return None
    match = SHIPMENT_SUFFIX_RE.match(str(raw).replace("\xa0", " "))
    return match.group(1) if match else None


def parse_tracking_numbers(raw: str) -> List[str]:
    """Tracking cells hold one or several numbers, sometimes with the carrier appended
    (`"31457971\\n7497809572 FEDEX"`). Returns them in order, deduped."""
    if not raw:
        return []
    seen: List[str] = []
    for token in re.findall(r"\b([A-Z0-9]{7,34})\b", str(raw).upper()):
        if token.isalpha():
            continue   # a bare word like "FEDEX" is the carrier, not the number
        if token not in seen:
            seen.append(token)
    return seen


def split_reference_field(raw: str) -> Tuple[List[str], List[str]]:
    """FedEx POD `Purchase Order` fields are a comma-joined grab bag:

        Purchase Order 31457971,210634,49985 : 1

    which is {carrier reference, PO, Authority shipment}. Returns (po_candidates, others) —
    6-digit tokens are treated as PO candidates, everything else is handed back for the caller
    to reconcile against the shipment/tracking it already knows.
    """
    if not raw:
        return [], []
    pos, others = [], []
    for chunk in re.split(r"[,;]", str(raw)):
        token = chunk.strip().split(":")[0].strip()
        if not token:
            continue
        (pos if PO_LENGTH_RE.match(token) else others).append(token)
    return pos, others
