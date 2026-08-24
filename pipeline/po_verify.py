"""Check one extracted record against the purchase order Spitfire actually holds.

This is what the Records page's **Verify** buttons run. It answers a narrow question — *does the
PO exist, does the spec resolve a line on it, and what quantities does that line carry* — and it
answers it in figures, not verdicts.

Deliberately not a decision. `api/services/reconcile.py::compute_match` already produces routing
verdicts (`flagged`, `route_target`, over-receipt) and this module does **not** call it: a
reviewer pressing Verify is asking what Spitfire says, and a screen that answers "MISMATCH" when
the email quantity equals the ordered quantity but the line is already fully received has
substituted a policy for the fact. Premier has not settled that policy — the overage question
(202 received against 196 ordered) is still open with them — so every difference is stated and
none is adjudicated.

The line *selection* is shared with the matcher: `reconcile.score_candidate` ranks candidates on
the same three signals (PO, spec, fuzzy description) against the same `DESC_MATCH_THRESHOLD`, so
the line this screen shows is the line Stage 4 will later propose. Two implementations of "which
line is this" would eventually disagree, and the screen would be lying about the pipeline.
"""

import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from api.services import reconcile
from api.stores.po_lines_store import POLineRow
from config import settings
from connectors.spitfire import PODocument, SpitfireReadClient
from pipeline import spitfire_mirror
from pipeline.models import POLine

_logger = logging.getLogger(__name__)

# Read timeout for the UI path. The connector defaults to 60s, which is right for a batch pull run
# from a terminal and wrong behind a browser fetch: a hung ERP would leave the popup spinning with
# no way to tell that from a slow one.
UI_TIMEOUT = 20

# How many POs to read at once on "Verify all". Measured 2026-08-12: one PO costs ~8.6s even with
# a cached document key, so 13 records read one after another is roughly two minutes. Five workers
# brings that to ~25s. Each worker gets its own client because `requests.Session` is not
# thread-safe; in cookie mode a second client costs nothing, as there is no login round trip.
DEFAULT_WORKERS = 5

SOURCE_LIVE = "live"
SOURCE_MIRROR = "mirror"

# The two sides spell the same unit differently, and a warning that fires on a difference that is
# not one is worse than no warning: a reader who sees "not in the same unit" on rows where the
# units plainly agree stops reading the line at all.
#
# Counted in the live store 2026-08-12 — records say EA (59), EACH (6), SET (5), YD (3); Spitfire
# lines say EA (361), YD (8), Set (7), SF (4). Case is handled by upper-casing, which settles
# SET/Set; EACH is the only genuine spelling difference in the data. Nothing is added here
# speculatively — an alias that is wrong (say, treating CS as EA) would silence a real mismatch,
# which is the failure this whole screen exists to prevent.
UOM_ALIASES = {"EACH": "EA"}


def uom_key(value: Optional[str]) -> str:
    """The comparable form of a unit. Empty when there is nothing to compare."""
    text = (value or "").strip().upper()
    return UOM_ALIASES.get(text, text)


@dataclass
class RecordFacts:
    """What the email said, in the shape `reconcile.score_candidate` reads.

    `records_ready()` hands back `sqlite3.Row`, which has no attribute access, and the scorer takes
    an `ExtractedRecord`. This carries only the fields the comparison touches rather than rebuilding
    a full record from a row that does not contain every column.

    `package_quantity` is carried but never compared. An Authority Inbound header reads
    "Quantity: 41 CTN" against a line of "11 EA" — eleven items in forty-one cartons — so comparing
    the package count to a PO line quantity is the specific mistake the two-field split exists to
    prevent. It is surfaced as a note instead.
    """
    id: int
    po_number: str
    spec_code: Optional[str] = None
    parent_spec_code: Optional[str] = None
    sub_spec_suffix: Optional[str] = None
    """`B`, `SH` — the component a split delivery is for. Its presence is what says the email's
    quantity counts parts while the purchase order counts assembled units."""
    item_description: Optional[str] = None
    quantity_received: Optional[float] = None
    unit_of_measure: Optional[str] = None
    package_quantity: Optional[float] = None
    package_uom: Optional[str] = None
    vendor_name: Optional[str] = None
    """Carried so the screen can put the mail's vendor beside the PO's. Not used for line
    selection — `reconcile.score_candidate` scores PO, spec and description only, and adding a
    fourth signal here would make this screen disagree with Stage 4 about which line a delivery is."""

    @property
    def stated_spec(self) -> Optional[str]:
        """The sub-spec if the mail gave one, else the parent — the same precedence the scorer uses."""
        return self.spec_code or self.parent_spec_code


def facts_from_row(row) -> RecordFacts:
    keys = row.keys() if hasattr(row, "keys") else ()

    def get(name):
        return row[name] if name in keys else None

    return RecordFacts(
        id=row["id"],
        po_number=str(row["po_number"] or ""),
        spec_code=get("spec_code"),
        parent_spec_code=get("parent_spec_code"),
        sub_spec_suffix=get("sub_spec_suffix"),
        item_description=get("item_description"),
        quantity_received=get("quantity_received"),
        unit_of_measure=get("unit_of_measure"),
        package_quantity=get("package_quantity"),
        package_uom=get("package_uom"),
        vendor_name=get("vendor_name"),
    )


@dataclass
class LineCheck:
    """One PO line lined up against one record, both sides in full."""
    line_number: Optional[int]
    spec_code: str
    description: str
    unit_of_measure: str
    qty_ordered: float
    qty_received: float
    qty_in_transit: float
    qty_outstanding: float
    record_quantity: Optional[float]
    record_uom: Optional[str]
    spec_resolved: bool
    """True when the record's own spec code selected this line. False means the line was reached by
    description alone, which is a weaker claim and is said so on screen."""
    record_description: Optional[str] = None
    record_spec: Optional[str] = None
    """The mail's own description and spec, carried so the renderer has both sides of every row it
    shows without reaching back to the `sqlite3.Row` the check was built from."""
    reviewer_chose: bool = False
    """True when a reviewer picked this line from the alternatives rather than the scorer selecting
    it. Recorded because a comparison against a hand-picked line is a different claim from one the
    pipeline stands behind, and the screen says which it is."""
    matched_on_parent: bool = False
    """True when the record's *parent* spec resolved this line, not its own. The mail said
    `STE-402-LT-B`; the purchase order carries `STE-402-LT`. Still an exact match, but on a
    different code — and the screen has to say so, or the spec row reads as a difference while the
    note beside it says the line is right."""

    @property
    def qty_delta(self) -> Optional[float]:
        """Email quantity minus quantity ordered. None when the email gave no quantity."""
        if self.record_quantity is None:
            return None
        return self.record_quantity - self.qty_ordered

    @property
    def qty_agrees(self) -> Optional[bool]:
        delta = self.qty_delta
        return None if delta is None else abs(delta) < 0.001

    @property
    def uom_agrees(self) -> Optional[bool]:
        """None when either side is silent — that is neither agreement nor disagreement."""
        ours, theirs = uom_key(self.record_uom), uom_key(self.unit_of_measure)
        if not ours or not theirs:
            return None
        return ours == theirs


@dataclass
class LineOption:
    """One receivable line, offered to a reviewer as an alternative to the matched one."""
    line_number: Optional[int]
    spec_code: str
    description: str
    unit_of_measure: str
    qty_ordered: float
    qty_outstanding: float


@dataclass
class RecordVerification:
    """Everything the popup shows for one record."""
    record_id: int
    po_number: str
    po_found: bool = False
    vendor_name: str = ""
    record_vendor_name: str = ""
    """The vendor the *email* named. Sits here rather than on `LineCheck` because a vendor is a
    property of the purchase order, not of one line on it — and it is worth showing even when no
    line resolved."""
    doc_status_label: str = ""
    order_date: Optional[str] = None
    matched: Optional[LineCheck] = None
    po_spec_codes: List[str] = field(default_factory=list)
    line_options: List["LineOption"] = field(default_factory=list)
    """Every receivable line on the PO. Populated always, shown only where the screen decides it
    helps — the module states facts and leaves presentation to the caller."""
    line_count: int = 0
    notes: List[str] = field(default_factory=list)
    source: str = SOURCE_LIVE
    read_at: Optional[str] = None
    error: Optional[str] = None

    @property
    def has_finding(self) -> bool:
        """Whether anything here needs a person's eye — drives the sort on "Verify all".

        A quantity that agrees and a UOM that agrees is the only quiet case. Everything else — a PO
        that could not be read, a spec that did not resolve, an email with no quantity to compare —
        is something the reviewer came to this screen to see.
        """
        if self.error or not self.po_found or self.matched is None:
            return True
        return self.matched.qty_agrees is not True or self.matched.uom_agrees is False


def fmt_qty(value: Optional[float]) -> str:
    """Quantities read as `196`, not `196.0`; `218.8` keeps its decimal."""
    if value is None:
        return "—"
    return f"{value:g}"


# --- the comparison -----------------------------------------------------------


def verify_record(facts: RecordFacts, doc: Optional[PODocument],
                  lines: Sequence[POLine],
                  chosen_line: Optional[int] = None) -> RecordVerification:
    """Pure. No network, no database — the whole comparison, given the PO already read.

    Kept separate from the fetching so the interesting cases (spec absent, quantity absent, spec
    not on the PO, UOM disagreeing) are testable against hand-built `POLine` lists.

    `chosen_line` is a reviewer overriding the scorer from the popup. It short-circuits selection
    rather than nudging it: a reviewer who has read both descriptions is a better judge than a
    fuzzy ratio, and a scorer that could veto the person reading it would make the choice
    pointless. The override is recorded on the check so the screen can say the line was picked
    rather than matched.
    """
    result = RecordVerification(record_id=facts.id, po_number=facts.po_number,
                                record_vendor_name=(facts.vendor_name or ""))
    if doc is not None:
        result.po_found = True
        result.vendor_name = doc.vendor_name
        result.doc_status_label = doc.doc_status_label
        result.order_date = doc.order_date
    elif lines:
        # Mirrored lines with no live header still prove the PO exists and carry every quantity
        # the comparison needs. Refusing to compare here would report "PO not found" about a PO
        # sitting in the mirror.
        result.po_found = True

    result.line_count = len(lines)
    result.po_spec_codes = [line.spec_code for line in lines if line.spec_code]
    result.line_options = [
        LineOption(
            line_number=line.line_number,
            spec_code=line.spec_code or "",
            description=line.description or "",
            unit_of_measure=line.unit_of_measure or "",
            qty_ordered=line.qty_ordered,
            qty_outstanding=line.qty_outstanding,
        )
        for line in lines
    ]

    if not result.po_found:
        result.notes.append(
            f"No purchase order numbered {facts.po_number} was found in the projects this "
            f"connector searches ({', '.join(settings.SPITFIRE_PROJECT_IDS)}). That is not the "
            f"same as it not existing — a PO on another project would read this way too."
        )
        return result

    if not lines:
        result.notes.append(
            "This purchase order has no receivable lines. Tax and freight lines are not counted, "
            "so a PO of nothing but those reads as empty here."
        )
        return result

    if chosen_line is not None:
        picked = next((l for l in lines if l.line_number == chosen_line), None)
        if picked is None:
            result.notes.append(
                f"Line {chosen_line:04d} is not a receivable line on this purchase order, so the "
                f"comparison below is the one this software worked out instead."
            )
        else:
            result.matched = _line_check(facts, picked, spec_resolved=False, reviewer_chose=True)
            result.notes.append(
                f"A reviewer chose line {chosen_line:04d}. The figures below are that line's, not "
                f"a line this software matched — nothing here says the choice is right."
            )
            result.notes.extend(_line_notes(facts, result.matched, None))
            _append_package_note(facts, result)
            return result

    candidates = reconcile.rank_candidates(
        facts, [POLineRow(id=0, line=line) for line in lines], limit=len(lines) or 1
    )
    # A PO-number match alone is not a line. Every line on the PO carries the same PO number, so
    # the top candidate would otherwise be an arbitrary line whenever the mail gave no spec and no
    # usable description — and the screen would show quantities from a line nobody identified.
    candidates = [c for c in candidates if c.spec_signal or c.desc_signal]
    best, tied = _pick_line(facts, candidates)

    if best is None:
        result.notes.extend(_no_line_notes(facts, result))
        _append_package_note(facts, result)
        return result

    line = best.po_line.line
    if tied:
        result.notes.append(_ambiguity_note(facts, best, tied))
    result.matched = _line_check(facts, line, spec_resolved=best.spec_signal)
    result.notes.extend(_line_notes(facts, result.matched, best))
    _append_package_note(facts, result)
    return result


def _line_check(facts: RecordFacts, line: POLine, *, spec_resolved: bool,
                reviewer_chose: bool = False) -> LineCheck:
    """One PO line paired with the record, both sides carried in full.

    Shared by the scorer's own selection and the reviewer's override so the two cannot render
    different shapes of the same comparison.
    """
    # Which of the record's two codes actually matched. Only meaningful when the spec resolved the
    # line at all — on a description match neither code matched, and claiming the parent did would
    # overstate it.
    def same(a, b):
        return bool(a) and bool(b) and a.strip().upper() == b.strip().upper()

    on_parent = bool(
        spec_resolved
        and not same(facts.spec_code, line.spec_code)
        and same(facts.parent_spec_code, line.spec_code)
    )

    return LineCheck(
        line_number=line.line_number,
        spec_code=line.spec_code,
        description=line.description,
        unit_of_measure=line.unit_of_measure,
        qty_ordered=line.qty_ordered,
        qty_received=line.qty_received,
        qty_in_transit=line.qty_in_transit,
        qty_outstanding=line.qty_outstanding,
        record_quantity=facts.quantity_received,
        record_uom=facts.unit_of_measure,
        spec_resolved=spec_resolved,
        record_description=facts.item_description,
        record_spec=facts.stated_spec,
        reviewer_chose=reviewer_chose,
        matched_on_parent=on_parent,
    )


def _pick_line(facts: RecordFacts, candidates: Sequence):
    """Choose between lines that score identically. Returns `(best, others_it_beat)`.

    The scorer ranks on signals then description similarity, and that is not enough on a real PO.
    Live example, 210635: spec `GR-350c-WTF` appears on **two** lines — 0001 is 84 YD of sheer
    fabric, 0003 is a 1 EA tariff surcharge on it. Both match the spec exactly, so both score two
    signals, and the surcharge's shorter description won on similarity. The screen then reported
    "84 YD is 83 more than the 1 EA ordered", which is not a discrepancy in Premier's data at all —
    it is this software having picked the wrong line and blamed the vendor for it.

    The unit breaks the tie. A delivery measured in yards belongs against the line ordered in
    yards; that is a fact about the two documents and holds however the quantities compare.

    Quantity is deliberately **not** used as a tie-break. Picking whichever line agrees with the
    email would make every comparison agree by construction, which is the one outcome that would
    make this screen worthless.

    Anything the tie-break does not settle is reported rather than silently resolved.
    """
    if not candidates:
        return None, []

    # An exact spec match outranks any number of fuzzy ones. `rank_candidates` sorts on signal
    # count then description similarity, which treats the spec as one vote of three — and on real
    # data that loses. Spitfire line descriptions run to a thousand characters (model numbers,
    # finishes, "SHOP DRAWINGS APPROVAL REQUIRED"); a short mail description scores ~35 against the
    # correct line and can score 80+ against a neighbouring one. The correct line then holds
    # PO + spec = 2 signals, the wrong line PO + description = 2 signals, they tie, and the higher
    # description score wins. `score_candidate` already says the spec "is what resolves the exact
    # line"; this makes the selection agree with that.
    #
    # Not currently reachable on live data — 0 of 28 records with a spec hit it on 2026-08-14 — but
    # it is one long description away, and the failure is silent: a confident-looking comparison
    # against the wrong line.
    by_spec = [c for c in candidates if c.spec_signal]
    if by_spec:
        candidates = by_spec

    top = candidates[0].signals_matched
    tied = [c for c in candidates if c.signals_matched == top]
    if len(tied) == 1:
        return tied[0], []

    ours = uom_key(facts.unit_of_measure)
    if ours:
        by_unit = [c for c in tied if uom_key(c.po_line.line.unit_of_measure) == ours]
        if len(by_unit) == 1:
            return by_unit[0], [c for c in tied if c is not by_unit[0]]
        if by_unit:
            tied = by_unit

    return tied[0], tied[1:]


def _ambiguity_note(facts: RecordFacts, best, others: Sequence) -> str:
    """Name the lines that were not chosen. A reader who cannot see the alternatives cannot tell a
    confident match from a coin toss, and on a PO with a surcharge line the difference is the whole
    answer."""
    def describe(candidate) -> str:
        line = candidate.po_line.line
        number = f"line {line.line_number:04d}" if line.line_number is not None else "an unnumbered line"
        return f"{number} ({fmt_qty(line.qty_ordered)} {line.unit_of_measure or '—'})"

    chosen = describe(best)
    rest = ", ".join(describe(c) for c in others)
    spec = facts.stated_spec or "this description"
    reason = ("its unit matches the email's" if uom_key(facts.unit_of_measure)
              == uom_key(best.po_line.line.unit_of_measure)
              else "it scored highest on description")
    return (f"More than one line on this purchase order matches {spec}: {chosen} and {rest}. "
            f"{chosen[0].upper() + chosen[1:]} was used because {reason} — check it is the right "
            f"one.")


def _no_line_notes(facts: RecordFacts, result: RecordVerification) -> List[str]:
    """Why no single line could be identified — three different reasons, said apart."""
    notes = []
    spec = facts.stated_spec
    if not spec:
        notes.append(
            f"The email gave no spec code, so no single line could be identified. This purchase "
            f"order has {result.line_count} receivable line(s)."
        )
    else:
        carried = ", ".join(result.po_spec_codes) or "none"
        notes.append(
            f"Spec {spec} is not on this purchase order. The PO carries: {carried}."
        )
    if facts.quantity_received is None:
        notes.append("The email gave no quantity either, so there is nothing to compare.")
    else:
        notes.append(
            f"The email states {fmt_qty(facts.quantity_received)} {facts.unit_of_measure or ''}".strip()
            + ", but with no line identified there is nothing to compare it against."
        )
    return notes


def _line_notes(facts: RecordFacts, check: LineCheck, candidate) -> List[str]:
    """Plain statements of what the two sides hold. No recommendation, by design.

    `candidate` is None when a reviewer chose the line rather than the scorer finding it. There is
    no similarity score to report in that case, and the caller has already said the line was
    picked — repeating it as a description match would misattribute the choice to this software.
    """
    notes = []
    if not check.spec_resolved and candidate is not None:
        notes.append(
            f"The email gave no matching spec code — this line was reached by description alone "
            f"({candidate.desc_score:.0f}% similar, threshold {settings.DESC_MATCH_THRESHOLD}). "
            f"Confirm it is the right line before relying on the figures below."
        )

    # A component delivery against an assembled line. Said before the quantity notes because it is
    # what makes them readable: "1 EA is 11 less than the 12 EA ordered" is arithmetically true and
    # means nothing on its own, because a base is not a lamp. Authority Inbound splits
    # `STE-402-LT` into `-B` and `-SH` and ships them separately; the purchase order counts
    # assembled units.
    #
    # The suffix is quoted, not translated. `B` is almost certainly "base" and `SH` "shade", but
    # nobody at Premier has confirmed that vocabulary, and a wrong expansion printed as fact is
    # worse than the raw code the reader can look up.
    if facts.sub_spec_suffix and check.spec_resolved:
        parent = facts.parent_spec_code or check.spec_code
        notes.append(
            f"The email is for the “{facts.sub_spec_suffix}” component of {parent}, not the whole "
            f"item. The purchase order counts assembled units, so the quantity below is not a "
            f"like-for-like comparison."
        )

    uom = check.record_uom or check.unit_of_measure or ""
    if check.record_quantity is None:
        notes.append(
            f"The email gave no quantity, so there is nothing to compare against the "
            f"{fmt_qty(check.qty_ordered)} {check.unit_of_measure} ordered."
        )
    elif check.qty_agrees:
        notes.append(
            f"The email quantity is the same as the quantity ordered "
            f"({fmt_qty(check.qty_ordered)} {check.unit_of_measure})."
        )
    else:
        delta = check.qty_delta or 0.0
        direction = "more than" if delta > 0 else "less than"
        notes.append(
            f"The email quantity {fmt_qty(check.record_quantity)} {uom} is {fmt_qty(abs(delta))} "
            f"{direction} the {fmt_qty(check.qty_ordered)} {check.unit_of_measure} ordered."
        )

    if check.uom_agrees is False:
        notes.append(
            f"The email says {check.record_uom}; the purchase order line is in "
            f"{check.unit_of_measure}. The quantities above are therefore not in the same unit."
        )

    if check.qty_received:
        notes.append(
            f"Spitfire already records {fmt_qty(check.qty_received)} {check.unit_of_measure} "
            f"received against this line."
        )
    if check.qty_in_transit:
        notes.append(
            f"{fmt_qty(check.qty_in_transit)} {check.unit_of_measure} is in transit — a receipt "
            f"exists but has not been approved through its route."
        )
    if abs(check.qty_outstanding) < 0.001:
        notes.append("Nothing is outstanding on this line.")
    return notes


def _append_package_note(facts: RecordFacts, result: RecordVerification) -> None:
    if facts.package_quantity is None:
        return
    result.notes.append(
        f"The email also states {fmt_qty(facts.package_quantity)} {facts.package_uom or ''} of "
        f"packaging. Package counts are not item counts and are not compared.".replace("  ", " ")
    )


# --- reading the purchase order ------------------------------------------------


def verify_records(conn: sqlite3.Connection, rows: Sequence,
                   *, workers: int = DEFAULT_WORKERS,
                   client_factory=None,
                   chosen_line: Optional[int] = None) -> List[RecordVerification]:
    """Verify every record in `rows`, reading each distinct PO from Spitfire once.

    Threads do the reading; the sqlite connection stays on the calling thread. Mirror refreshes and
    fallback reads all happen here, after the futures resolve, because a sqlite3 connection created
    on one thread must not be used from another.

    `chosen_line` is a reviewer's override from the popup and only makes sense for one record, so
    it is applied only when `rows` holds one. "Verify all" passes none, and a line number chosen
    against one PO would be meaningless against another anyway.
    """
    facts = [facts_from_row(row) for row in rows]
    pick = chosen_line if len(facts) == 1 else None
    po_numbers = sorted({f.po_number for f in facts if f.po_number})
    if not po_numbers:
        return [verify_record(f, None, [], pick) for f in facts]

    factory = client_factory or (lambda: SpitfireReadClient(timeout=UI_TIMEOUT))
    keys = {po: spitfire_mirror.doc_key_for(conn, po) for po in po_numbers}

    def read(po_number: str):
        # One client per task rather than per worker: constructing it is a dict and a cookie jar,
        # and sharing one `requests.Session` across threads is the bug this avoids.
        client = factory()
        try:
            key = keys.get(po_number) or client.resolve_po(po_number)
            if not key:
                return po_number, None, None
            return po_number, client.read_po(key), None
        except Exception as exc:                   # noqa: BLE001
            _logger.warning("live read of PO %s failed: %s", po_number, exc)
            return po_number, None, str(exc)

    if len(po_numbers) == 1:
        fetched = [read(po_numbers[0])]
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(po_numbers))) as pool:
            fetched = list(pool.map(read, po_numbers))

    docs: Dict[str, Optional[PODocument]] = {}
    errors: Dict[str, Optional[str]] = {}
    for po_number, doc, error in fetched:
        docs[po_number] = doc
        errors[po_number] = error
        if doc is not None:
            try:
                spitfire_mirror.save_po(conn, doc, _now())
            except sqlite3.Error as exc:
                _logger.warning("could not mirror PO %s: %s", po_number, exc)

    results = []
    for f in facts:
        doc = docs.get(f.po_number)
        error = errors.get(f.po_number)
        header = None
        if doc is not None:
            lines, source, read_at = doc.lines, SOURCE_LIVE, _now()
        else:
            # Live read failed. The mirror is the honest fallback — stale figures clearly stamped
            # beat an empty popup, which reads as "nothing matched" rather than "we could not look".
            lines = spitfire_mirror.lines_for(conn, f.po_number)
            source = SOURCE_MIRROR if lines else SOURCE_LIVE
            read_at = spitfire_mirror.refreshed_at(conn, f.po_number)
            header = spitfire_mirror.header_for(conn, f.po_number)
        result = verify_record(f, doc, lines, pick)
        if header:
            # The mirror knows the vendor and the status too. Without this the fallback shows
            # quantities under a blank header, which reads as data we do not have rather than as
            # data from the last time we could reach Spitfire.
            result.vendor_name = header.get("vendor_name") or ""
            result.doc_status_label = header.get("doc_status_label") or ""
            result.order_date = header.get("order_date")
        result.source = source
        result.read_at = read_at
        result.error = error
        results.append(result)
    return results


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def mismatch_flags(conn: sqlite3.Connection, rows: Sequence) -> Dict[int, Dict[str, str]]:
    """Which cells on the Records table disagree with Spitfire. `{record id: {field: reason}}`.

    Reads the **mirror**, never the network. Verifying 29 rows live costs about 145 seconds, which
    is not a page render; the mirror already holds every PO the page lists. `verify_record` is pure,
    so the same function that fills the Verify popup decides the table's flags — one comparison, not
    two implementations drifting apart.

    Fields are `qty`, `spec` and `uom`, all exact. **Description is not flagged**: measured over the
    29 records on this page, the pipeline's `token_sort_ratio` at its threshold of 80 would mark 21
    of them red, nearly all correct — record 132 scores 10 on "Sheer Fabric" against
    "GR-350c-WTF Sheer Fabric Pattern Name: Saint Martin…", which is plainly the same item. Spitfire
    is simply more verbose than the mail, and no threshold on fuzzy prose separates that from a real
    disagreement. Spec catches wrong-item cases exactly, which is what the flag is for.

    **An absent flag means "nothing disagrees", never "not checked."** A record whose PO is not
    mirrored, or whose line did not resolve, gets no flags — and would read as clean. That is the
    one way this could mislead, so it is stated here and the page keeps the Verify button for the
    live answer.
    """
    facts = [facts_from_row(row) for row in rows]
    by_po: Dict[str, List[POLine]] = {}
    for f in facts:
        if f.po_number and f.po_number not in by_po:
            by_po[f.po_number] = spitfire_mirror.lines_for(conn, f.po_number)

    flags: Dict[int, Dict[str, str]] = {}
    for f in facts:
        lines = by_po.get(f.po_number) or []
        if not lines:
            continue
        result = verify_record(f, None, lines)
        check = result.matched
        found: Dict[str, str] = {}

        if check is None:
            # The purchase order is mirrored and has receivable lines, yet nothing matched. That is
            # a finding, not a blank: a spec the PO has never heard of is the strongest signal on
            # this page that something is wrong, and it would otherwise render as a clean row.
            if f.stated_spec:
                found["spec"] = (
                    f"{f.stated_spec} is not on purchase order {f.po_number}, and no line matched "
                    f"by description either. Open Verify to see the {result.line_count} line(s) it "
                    f"does have."
                )
                flags[f.id] = found
            # With no spec stated there is no cell to point at — the row is unflagged and Verify is
            # the answer. Noted rather than silently accepted: this is the one shape of record that
            # can look clean without having been compared.
            continue

        if check.qty_agrees is False:
            if f.sub_spec_suffix:
                # Flagged anyway — the quantities genuinely differ and a reader scanning the table
                # needs to see that. The reason carries the explanation so a known case reads as
                # explained without opening the popup.
                found["qty"] = (
                    f"The email counts the “{f.sub_spec_suffix}” component of "
                    f"{f.parent_spec_code or check.spec_code}; the purchase order counts assembled "
                    f"units ({fmt_qty(check.qty_ordered)} {check.unit_of_measure} ordered)."
                )
            else:
                found["qty"] = (
                    f"The email says {fmt_qty(check.record_quantity)} {check.record_uom or ''}"
                    f"; Spitfire has {fmt_qty(check.qty_ordered)} {check.unit_of_measure} ordered."
                ).replace("  ", " ")

        # `matched_on_parent` is agreement, not a difference: the mail named the sub-spec and the
        # purchase order the parent, and they resolve to the same line.
        if check.spec_resolved and not check.matched_on_parent:
            ours = (f.stated_spec or "").strip().upper()
            theirs = (check.spec_code or "").strip().upper()
            if ours and theirs and ours != theirs:
                found["spec"] = f"The email says {f.stated_spec}; Spitfire has {check.spec_code}."
        elif not check.spec_resolved and f.stated_spec:
            found["spec"] = (
                f"{f.stated_spec} is not on this purchase order — the line was reached by "
                f"description instead."
            )

        if check.uom_agrees is False:
            found["uom"] = (f"The email says {check.record_uom}; Spitfire has "
                            f"{check.unit_of_measure}.")

        if found:
            flags[f.id] = found
    return flags
