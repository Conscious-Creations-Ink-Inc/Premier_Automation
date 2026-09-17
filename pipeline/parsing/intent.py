"""What a message *says* about a delivery — asked, answered, denied, or scheduled.

Triage decided "is this a delivery thread" with a single bag of keywords:

    \\b(deliver(?:y|ed|ies)|receipt|receiv(?:e|ed|ing)|confirm(?:ation)?|BOL|...)\\b

That regex cannot separate four sentences that all contain "receiv" and mean opposite things:

    "Could you please verify whether the fabrics listed below were received"   -> a question
    "We can confirm that we have only received the Sheer Fabric (GR-350c-WTF)" -> a partial yes
    "Driver is onsite now getting loaded This will be delivered tomorrow"      -> not yet
    "I am confirming receipt of check 1000781 for $87,663.85"                  -> not goods

All four are real corpus lines. The first two are the same email — Premier's own request table
forwarded back, with the property's answer typed above it. Reading that email as a confirmation
staged three receipts, one of them for goods the property had explicitly said did not arrive.

Six lists, and the important thing about them is that they are **data**. Premier extends a word
list without touching control flow; the precedence below is the only logic, and it is short enough
to hold in your head.

`classify` judges one piece of prose. `resolve_thread` at the bottom of this module is what
callers normally want: it walks the quoted hops newest-first and decides **per spec code**, because
one email routinely contains a question, its answer, and a later correction — and because depth 0,
the forwarding wrapper, is usually empty. Scoring the whole body at once cannot tell which of those
came last.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Sequence, Tuple

from pipeline.parsing import boilerplate, tokens

# --- A: the goods arrived, stated in the past, first-hand ---------------------------------------

DELIVERY_AFFIRMATIVE: Tuple[str, ...] = (
    "received", "was received", "were received", "have received", "has received", "we received",
    "receipt of", "delivered", "was delivered", "were delivered", "has been delivered",
    "delivered on", "delivered to", "delivery date", "date delivered", "actual delivery date",
    "received date", "receiving date", "receipt date", "signed by", "signed for by", "signature",
    "proof of delivery", "POD", "POD attached", "delivered notification", "delivery notification",
    "inbound notification", "arrived", "has arrived", "arrived at", "dropped off", "offloaded",
    "unloaded", "qty delivered", "quantity delivered", "qty received", "qty rcvd",
    "warehouse receiving report", "receiving report", "RR #", "installed", "was installed",
    "confirmed delivery", "confirmed receipt", "confirming receipt", "BOL", "bill of lading",
    "packing slip", "pack slip", "signed pack slip", "ALS shipment #", "authority #",
)

# --- B: somebody is being asked to answer. Never a receipt --------------------------------------

VERIFICATION_REQUEST: Tuple[str, ...] = (
    "could you please verify/confirm/check", "could you verify/confirm/check",
    "could you kindly confirm", "can you please confirm/verify/mark", "can you confirm/verify",
    "please verify", "please confirm", "please advise",
    "kindly confirm", "kindly double check", "kindly find out",
    "need confirmation", "needs confirmation", "need to confirm", "needs the receipt confirmation",
    "needs receiving confirmation", "requires confirmation", "require receipt confirmation",
    "awaiting confirmation", "waiting to hear back", "pending confirmation",
    "pending property receipt confirmation", "delivery confirmation required",
    "to be confirmed", "to be confirmed as received", "did you have the chance to check",
    "has ... been received", "have ... been received", "were ... received",
    "whether ... were received", "whether ... was received",
    "confirm receipt when possible",
    "kind reminder", "reminder to review", "mark with yes or no",
    # A promise to answer later is not an answer. Both are real corpus replies on live threads:
    # "I will be on-site Friday. I will confirm then." and "I will check for photos in the
    # project file for the other items."
    "I will confirm", "I will check",
)
"""Deliberately excludes the generic courtesies.

`"please review"`, `"please check"`, `"please let me know"`, `"yes or no"` and `"following up"` were
all in the first draft and all had to come out: `"Warehouse Receiving Report # RR 211798-5 is
approved. Please review the attachment for details."` is a receiving document, not a request, and
`"Please let me know if you need any additional information"` is a sign-off that appears under
genuine confirmations. Every entry left here asks about *receipt* specifically. The cost of the two
mistakes is not symmetrical — a request mistaken for a receipt stages goods nobody received, but a
courtesy mistaken for a request only queues a mail somebody still reads.
"""

# --- C: limits an affirmative to what it names --------------------------------------------------

SCOPE_EXCLUSION: Tuple[str, ...] = (
    "only", "just", "solely", "exclusively", "except", "other than", "apart from", "aside from",
    "but not", "nothing else", "none of the others", "the rest", "partial", "partially",
)
"""The list that decides whether one "yes" covers one line or a whole table.

`"We can confirm that we have only received the Sheer Fabric (GR-350c-WTF) attic stock"` is an
affirmative about **one** of the three POs in the request it answers, and a denial of the other
two. Without this list that sentence reads as a plain confirmation and spreads across every row —
which is how PO 210636 came to carry a staged receipt for goods the sender had just said were
missing.
"""

# --- D: explicitly did not arrive ---------------------------------------------------------------

NEGATIVE_RECEIPT: Tuple[str, ...] = (
    "not received", "have not received", "has not been received", "never received",
    "did not arrive", "hasn't arrived", "has not arrived", "still waiting", "still pending",
    "outstanding", "missing", "pallet missing", "shortage", "no sign of",
    "back order", "backordered", "back-ordered",
)

# --- E: it will arrive, which is not the same as it arrived -------------------------------------

FUTURE_SCHEDULED: Tuple[str, ...] = (
    "will be delivered", "will deliver", "will arrive", "will ship", "will be picked up",
    "scheduled for", "schedule delivery", "to be delivered", "due to arrive", "ETA",
    "estimated delivery", "expected", "can this pickup", "how soon can",
    "still available to be picked up", "do not ship before", "will be ready", "ready Monday",
)
"""Real corpus lines a bare list-A match reads as receipts:

    "the freight company contacted me today to schedule delivery for June 3, 2026"
    "Driver is onsite now getting loaded This will be delivered to jobsite tomorrow."
    "RES-200-SG - MODIFICATION - Amtrend will deliver these 3 pieces Monday to install."
"""

# --- F: a receipt of something that is not goods ------------------------------------------------

NON_GOODS: Tuple[str, ...] = (
    "receipt of check", "receipt of payment", "receipt of invoice", "paid in full",
    "approvals received", "approval received", "receiving this email because",
    "set to receive only replies", "subscribed to",
    # "Field Verification" is scope of work, not a request: the corpus carries
    # `POOL-152-WT-Drapery Fabrication & Field Verification at Existing Large Cabana` as an item
    # description. Matching on the newest hop's prose keeps it out of reach most of the time; this
    # entry covers the case where somebody quotes a line item into their reply.
    "field verification", "template/field verification", "inbox verification",
)


# --- topic: is this thread about a delivery at all? ---------------------------------------------

DELIVERY_TOPIC: Tuple[str, ...] = (
    "delivery", "deliveries", "deliver", "delivered", "receipt", "receive", "received",
    "receiving", "receiver", "confirm", "confirmation", "BOL", "bill of lading", "packing slip",
    "pack slip", "POD", "proof of delivery", "pallet", "skid", "crate", "shipment", "freight",
    "carrier", "tracking", "warehouse", "inbound", "consignment", "waybill",
)
"""Subject matter, not assertion — a strictly separate question from what list A asks.

Both are needed and conflating them is a real bug. `"the freight company contacted me today to
schedule delivery for June 3, 2026"` is unmistakably *about* a delivery and asserts nothing about
one having happened. The rules that act on attachments alone (a tracker spreadsheet, a
photographed POD) guard on the topic — they need to know the thread is in scope, not that somebody
already said the goods landed. `classify` below answers the second question; `is_delivery_topic`
answers the first.
"""


class Intent(Enum):
    """What the newest hop says. One verdict per message; the per-PO split happens above this."""

    DELIVERY = "delivery"
    VERIFICATION = "verification"
    NEGATIVE = "negative"
    SCHEDULED = "scheduled"
    NON_GOODS = "non_goods"
    NEITHER = "neither"


@dataclass(frozen=True)
class IntentResult:
    """The verdict, and the words it rests on.

    `matched` exists so every downstream reason string can quote what it matched, the same
    principle Stage 1's tracker-attachment rule already follows — an operator reading the queue
    sees the evidence, not a heuristic's name.
    """

    intent: Intent
    matched: Tuple[str, ...] = ()
    scope_specs: Tuple[str, ...] = ()
    """Spec codes an affirmative was limited to, when a list-C word narrowed it."""
    scope_uncertain: bool = False
    """A scope word was present but named nothing recoverable.

    "I only wanted to confirm everything was received" limits nothing — there is no item to limit
    it to. Never silently widen back to the whole table: the caller routes this to a person
    instead, because a scope word we cannot resolve is exactly the sentence we misread before.
    """

    @property
    def is_receipt_evidence(self) -> bool:
        """True only for a plain affirmative. Everything else needs a person or another document."""
        return self.intent is Intent.DELIVERY and not self.scope_uncertain


_GAP = r"[^\n]{0,60}?"
"""What `...` inside a phrase expands to.

`"were ... received"` has to reach across "the fabrics listed below" (26 characters) without
reaching across a paragraph. Bounded and non-greedy, and it never crosses a newline — two adjacent
sentences are not one claim.
"""


def _phrase_to_pattern(phrase: str) -> str:
    """One plain-language phrase to regex source.

    Three conveniences, so a word list stays a word list:

    * whitespace is flexible — a phrase typed with single spaces still matches text wrapped
      across a line, which HTML-to-text conversion produces constantly;
    * `word1/word2` is an alternation, so `"please verify/confirm"` is one entry rather than two;
    * `...` is a bounded gap (see `_GAP`).

    Word boundaries are added only where the phrase actually ends in a word character. `"RR #"`
    and `"authority #"` end in punctuation, and `\\b` after `#` would never match.
    """
    parts: List[str] = []
    for token in re.split(r"(\s+)", phrase.strip()):
        if not token:
            continue
        if token.isspace():
            parts.append(r"\s+")
        elif token in ("...", "…"):
            parts.append(_GAP)
        elif "/" in token:
            alts = [re.escape(alt) for alt in token.split("/") if alt]
            parts.append("(?:" + "|".join(alts) + ")")
        else:
            parts.append(re.escape(token))
    body = "".join(parts)
    prefix = r"\b" if phrase[:1].isalnum() else ""
    suffix = r"\b" if phrase[-1:].isalnum() else ""
    return prefix + body + suffix


def _compile(phrases: Sequence[str]) -> re.Pattern:
    """One alternation per list. Longest first, so `"were received"` is reported rather than the
    `"received"` sitting inside it — the quoted phrase in a reason string should be the specific
    one that fired."""
    ordered = sorted(phrases, key=len, reverse=True)
    return re.compile("|".join(f"(?:{_phrase_to_pattern(p)})" for p in ordered), re.IGNORECASE)


# --- A, qualified: which affirmatives can stand on their own ------------------------------------

GENERIC_AFFIRMATIVE: Tuple[str, ...] = (
    "received", "was received", "were received", "have received", "has received", "we received",
    "receipt of", "arrived", "has arrived", "signature", "installed", "was installed",
)
"""List-A phrases that say *something* was received without saying what.

These are the entries that made list A over-fire. Read against the live store, the sentences they
actually matched were "our accounting department just confirmed the **check was received** and
processed", "I have **received approval** for the following claims", and "**Received** thanks" —
about an email. None is a receipt of goods, and all three were read as one.

Everything else in list A names the goods or the document proving them — `packing slip`,
`proof of delivery`, `qty delivered`, `warehouse receiving report`, `bill of lading` — and needs
no object to be unambiguous. Those stand alone; these need something arriving nearby.
"""

GOODS_CONTEXT: Tuple[str, ...] = (
    "item", "good", "order", "shipment", "freight", "furniture", "piece", "carton", "pallet",
    "box", "crate", "roll", "yard", "unit", "product", "merchandise", "fabric", "chair", "table",
    "lamp", "mirror", "rug", "casegood", "mattress", "headboard", "artwork", "signage",
    "delivery", "delivered", "container", "truck", "load", "warehouse", "jobsite", "job site",
    "on site", "onsite", "POD", "packing slip", "spec",
    # Premier's own nouns for goods. "a copy of the WRR noting we received..." and "after we
    # received the remaining COMs" are both receipts, and neither names anything on the plain
    # furniture list above.
    "WRR", "RR", "receiving report", "COM", "material", "attic stock", "fixture", "millwork",
    # A universal referent stands in for the goods: "everything was received fine" is a receipt,
    # and the only thing it could be a receipt of is the order under discussion. Deliberately not
    # "all", which reaches "all approvals received" and would reopen the hole this list closes.
    "everything",
)
"""What has to be nearby for a bare "received" to be a receipt of goods.

Written singular; `_GOODS_RE` matches an optional plural, so "the fabrics listed below were
received" is a receipt without the list carrying both forms of every noun.
"""


def _compile_goods(words: Sequence[str]) -> re.Pattern:
    """Like `_compile`, plus an optional plural `s` — and no `...` or `/` handling, which these
    do not use. Longest first so `packing slip` is preferred over `slip`."""
    parts = []
    for word in sorted(words, key=len, reverse=True):
        body = r"\s+".join(re.escape(token) for token in word.split())
        parts.append(rf"\b{body}s?\b")
    return re.compile("|".join(parts), re.IGNORECASE)


_GENERIC_A_RE = _compile(GENERIC_AFFIRMATIVE)
_GOODS_RE = _compile_goods(GOODS_CONTEXT)

_GOODS_WINDOW_BEFORE = 70
_GOODS_WINDOW_AFTER = 50
"""How far either side of a bare affirmative to look for the goods it claims.

Wide enough for "the fabrics listed below were received" and "we received the 4 chairs against
PO 214312"; narrow enough that a signature block two paragraphs down cannot supply the context.
"""


def _has_goods_context(text: str, start: int, end: int) -> bool:
    """Is there anything resembling goods around the affirmative at `text[start:end]`?

    The window is clipped at newlines on both sides, so a claim cannot borrow its object from an
    adjacent line — the same reasoning as `_GAP` in the phrase compiler.
    """
    left = text[max(0, start - _GOODS_WINDOW_BEFORE):start].rpartition("\n")[2]
    right = text[end:end + _GOODS_WINDOW_AFTER].partition("\n")[0]
    return bool(_GOODS_RE.search(left + " " + right))


def _goods_affirmatives(text: str, limit: int = 4) -> Tuple[str, ...]:
    """List-A hits that actually claim goods arrived.

    A self-sufficient phrase counts wherever it appears. A generic one counts only where goods are
    named around it. Returns the phrases in order, capped like `_hits`, so reason strings read the
    same as they always did.
    """
    kept: List[str] = []
    for match in _A_RE.finditer(text or ""):
        phrase = re.sub(r"\s+", " ", match.group(0)).strip()
        if _GENERIC_A_RE.fullmatch(phrase) and not _has_goods_context(text, match.start(), match.end()):
            continue
        if phrase.lower() not in {k.lower() for k in kept}:
            kept.append(phrase)
        if len(kept) >= limit:
            break
    return tuple(kept)


_A_RE = _compile(DELIVERY_AFFIRMATIVE)
_B_RE = _compile(VERIFICATION_REQUEST)
_C_RE = _compile(SCOPE_EXCLUSION)
_D_RE = _compile(NEGATIVE_RECEIPT)
_E_RE = _compile(FUTURE_SCHEDULED)
_F_RE = _compile(NON_GOODS)
_TOPIC_RE = _compile(DELIVERY_TOPIC)

_SENTENCE_END = re.compile(r"[.!?\n]")


def is_delivery_topic(text: str) -> bool:
    """Is this thread about a delivery at all? Replaces Stage 1's `_DELIVERY_THREAD_RE`.

    Deliberately permissive: it guards rules that act on attachments, where the cost of the two
    mistakes is not symmetrical. An unrelated thread carrying a spreadsheet is noise; a real
    delivery whose PO lives only inside that spreadsheet is a receiver nobody creates.
    """
    return bool(_TOPIC_RE.search(text or ""))


def _hits(pattern: re.Pattern, text: str, limit: int = 4) -> Tuple[str, ...]:
    """The distinct phrases that fired, in order, capped so a reason string stays readable."""
    seen: List[str] = []
    for match in pattern.finditer(text):
        value = re.sub(r"\s+", " ", match.group(0)).strip()
        if value.lower() not in {s.lower() for s in seen}:
            seen.append(value)
        if len(seen) >= limit:
            break
    return tuple(seen)


def scope_spans(text: str) -> List[str]:
    """The text a scope-exclusion word governs — from the word to the end of its sentence.

    Returned rather than resolved here because half of what a scope limit names is an item
    *description*, and the only authoritative list of those is the grid being answered. The caller
    matches its own `Description of Item` column against these spans; `scope_limited_specs` below
    handles the half that is a spec code.
    """
    spans: List[str] = []
    for match in _C_RE.finditer(text or ""):
        tail = text[match.start():match.start() + 300]
        end = _SENTENCE_END.search(tail, 1)
        spans.append(tail[: end.start()] if end else tail)
    return spans


def scope_limited_specs(text: str) -> Tuple[str, ...]:
    """Spec codes named inside a scope limit.

    `"we have only received the Sheer Fabric (GR-350c-WTF) attic stock"` -> `("GR-350c-WTF",)`.
    """
    found: List[str] = []
    for span in scope_spans(text):
        for spec in tokens.find_specs(span):
            if spec not in found:
                found.append(spec)
    return tuple(found)


def classify(text: str) -> IntentResult:
    """What the newest hop says about a delivery.

    Precedence, first match wins. The order is a statement about which mistake costs more:

    0. (the caller's) a POD naming this PO — a question in the covering mail cannot un-deliver
       goods the carrier proved arrived. Handled above this function, which sees only prose.
    1. **F** — a receipt of a cheque is not a receipt of goods. Cheapest and most certain, so first.
    2. **B** — a question is never a receipt, however much delivery vocabulary it quotes. This is
       the rule the corpus proves was missing.
    3. **D** — an explicit "not received" outranks any affirmative in the same message.
    4. **A** without **E** — the goods arrived.
    5. **A** with **E** — they are coming. Not a receipt.
    6. neither — existing handling is unchanged.
    """
    body = text or ""
    if not body.strip():
        return IntentResult(Intent.NEITHER)

    non_goods = _hits(_F_RE, body)
    if non_goods:
        return IntentResult(Intent.NON_GOODS, non_goods)

    verification = _hits(_B_RE, body)
    if verification:
        return IntentResult(Intent.VERIFICATION, verification)

    negative = _hits(_D_RE, body)
    if negative:
        return IntentResult(Intent.NEGATIVE, negative)

    affirmative = _goods_affirmatives(body)

    # Checked before the affirmative gate, not after it. "the freight company contacted me today
    # to schedule delivery for June 3, 2026" states a future delivery in words that never appear
    # in list A — falling through to NEITHER would be *safe* but would lose the fact that this is
    # live delivery mail, which the attachment rules downstream still need.
    scheduled = _hits(_E_RE, body)
    if scheduled and (affirmative or is_delivery_topic(body)):
        return IntentResult(Intent.SCHEDULED, affirmative + scheduled)

    if not affirmative:
        return IntentResult(Intent.NEITHER)

    scope = _hits(_C_RE, body)
    if scope:
        specs = scope_limited_specs(body)
        # A scope word with nothing recoverable to scope to. Not widened back out to the whole
        # request — see `IntentResult.scope_uncertain`.
        return IntentResult(Intent.DELIVERY, affirmative + scope,
                            scope_specs=specs, scope_uncertain=not specs)

    return IntentResult(Intent.DELIVERY, affirmative)


# --- resolving a whole thread -------------------------------------------------------------------
#
# `classify` judges one piece of prose. That is not enough on its own, because **the newest hop is
# usually empty**. Every message in this corpus is a forward, and depth 0 is the forwarding
# wrapper: on the ATTIC STOCK thread it strips to zero words, while the six hops beneath it hold an
# entire conversation.
#
#     depth 5  Maria    "Could you please verify whether the fabrics listed below were received?"
#     depth 4  Maria    "Friendly reminder of this."
#     depth 3  Elber    "We can confirm that we have only received the Sheer Fabric (GR-350c-WTF)"
#     depth 2  Maria    "...GR-350a-WTF POD and delivery notification for 202 yards (6 yards of
#                        overage). There's also 884603885067 POD attached which belongs to
#                        GR-350d-WTF 78 yards from Daniel Stuart."
#
# Reading only depth 0 sees none of it. Reading the whole body at once sees the question and the
# answer as one undifferentiated blob and cannot tell which came last. The resolution is per spec,
# newest hop first: the most recent thing anybody said about a given line is what stands.
#
# Note what that yields on depth 2, and that it is correct. Maria claims a POD for GR-350d-WTF and
# she is wrong — the attached file is byte-identical to the GR-350a-WTF one. This module reports
# what the thread *says*; `attachment_ledger` is what disproves it. Keeping those two jobs apart is
# deliberate: a text reader that also tried to adjudicate evidence would have to be trusted twice.

_MIN_HOP_WORDS = 3
"""Below this a hop is a wrapper, a greeting or a signature remnant, not an assertion."""


@dataclass(frozen=True)
class HopIntent:
    """One hop's verdict, with the identifiers it named."""

    depth: int
    sender: str
    result: IntentResult
    specs: Tuple[str, ...] = ()
    po_numbers: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ThreadVerdict:
    overall: Intent
    """The newest hop that asserted anything. `NEITHER` when no hop did."""
    by_spec: dict
    """spec code -> `Intent`, decided by the newest hop that spoke about it."""
    hops: Tuple[HopIntent, ...] = ()
    """Newest first, substantive hops only — the audit trail behind `by_spec`."""

    def verdict_for(self, spec: str) -> Intent:
        """What the thread says about one line. Unmentioned lines are `NEITHER`, never a receipt."""
        return self.by_spec.get(spec, Intent.NEITHER)


def hop_intents(parsed) -> List[HopIntent]:
    """Every substantive hop's verdict, newest first."""
    out: List[HopIntent] = []
    for hop in sorted(getattr(parsed, "hops", []) or [], key=lambda h: h.depth):
        body = boilerplate.strip_boilerplate(hop.body or "")
        if len(body.split()) < _MIN_HOP_WORDS:
            continue
        out.append(HopIntent(
            depth=hop.depth,
            sender=getattr(hop, "sender_address", "") or "",
            result=classify(body),
            specs=tuple(tokens.find_specs(body)),
            po_numbers=tuple(tokens.find_po_numbers(body)),
        ))
    return out


def resolve_thread(parsed, known_specs: Sequence[str] = ()) -> ThreadVerdict:
    """Per-spec verdicts for a whole thread, newest assertion winning.

    `known_specs` is the line-up being answered — the spec column of the request grid. It is what
    lets a scope limit say what it *denied*: "we have only received the Sheer Fabric" names one
    line and refuses the other two, but only the grid knows which two those are.
    """
    hops = hop_intents(parsed)
    by_spec: dict = {}
    overall = Intent.NEITHER
    excluded: dict = {}

    for hop in hops:
        res = hop.result
        if overall is Intent.NEITHER and res.intent is not Intent.NEITHER:
            overall = res.intent

        if res.intent is Intent.DELIVERY:
            # A scope limit narrows the claim to what it named; without one the claim covers every
            # line the hop mentions.
            for spec in (res.scope_specs or hop.specs):
                by_spec.setdefault(spec, Intent.DELIVERY)
            if res.scope_specs:
                for spec in known_specs:
                    if spec not in res.scope_specs:
                        excluded.setdefault(spec, hop.depth)
        elif res.intent is Intent.NEGATIVE:
            for spec in hop.specs:
                by_spec.setdefault(spec, Intent.NEGATIVE)

    # Applied last and only where nothing newer spoke: a later hop supplying a POD for a line the
    # property had not received is a real update, not a contradiction to be suppressed.
    for spec in excluded:
        by_spec.setdefault(spec, Intent.NEGATIVE)

    return ThreadVerdict(overall=overall, by_spec=by_spec, hops=tuple(hops))
