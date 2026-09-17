"""Is this message advertising, ordinary work, or too close to call?

`stage1_triage` needed one bar to clear: an unsubscribe block in the body. Precise, and nearly
blind — bodies are not kept at ingest (`mail_body` held 116 rows against 1,961 in `email_log`), so
that test could only ever act on mail somebody had already opened. It also misses every advertisement
whose sender omits an opt-out link, which is all the cold outreach arriving from ordinary domains:
*"Your next hire"*, *"Meet your new solution…artsake!"*, *"Turn document insights into business
action"*.

Two weighted lists, and the important thing about them is that they are **data** — the same shape
`intent.py` uses, for the same reason. The precedence at the bottom is the whole of the logic.

## What the lists are, and where the weights come from

Measured over the live store, read-only: 453 messages reaching the queue as the catch-all, 833 known
delivery mails (`surface`/`hold`, or having produced a record), 600 genuine delivery bodies recovered
from `accumulation.payload_json`, and a seed of 40 unmistakable adverts read off their subjects.

    feature                                 adverts   delivery mail
    names no purchase order                  100%        10.4%
    carries no attachment                    100%         3.0%
    sender on a bulk subdomain                70%         0.0%
    a marketing glyph in the subject          55%         0.8%
    a % or $ figure in the subject          52.5%         1.3%
    a role sender (deals@, news@, editor@)    50%         1.1%
    subject ends with ! or ?                12.5%         0.5%
    IS A REPLY (Re: / Fw:)                     0%        84.6%
    names a Premier property                   0%        53.9%
    carries a spec / RR / project code         0%         6.0%
    an unsubscribe block (body)             36.4%         1.3%
    more <img> than <p> (body)              33.3%         0.5%

**An advertisement is never a reply.** Not one of the seed is a `Re:`/`Fw:`, and 84.6% of genuine
delivery mail is. Premier's receiving happens in conversations; advertising arrives cold. It is the
heaviest business signal here and it earns that on its own.

## Three signals that read like marketing tells and measurably are not

Recorded so nobody adds them back on intuition:

* **ALL-CAPS words** — 22.5% of adverts, 30.4% of delivery mail. Premier's own subjects shout:
  `SHERATON ANCHORAGE GUESTROOMS - RR 211373-16`. It discriminates in the wrong direction.
* **Ten or more outbound links** — 60.6% of candidates, 69.3% of delivery bodies. Delivery mail has
  *more* links, not fewer.
* **A tracking pixel** — 57.6% against 38.8%. Too common on both sides to carry weight.

## The bands

`ADVERTISING` requires a clear promotional score **and a business score of exactly zero**. One
business signal — a PO, a reply, a property name — is a veto, whatever the promotional score. That
asymmetry is the safety property: this module can only ever be confident about mail that has nothing
of Premier's business in it, and everything else resolves to a person.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

ADVERTISING = "advertising"
"""Clear promotional mail with no business signal at all. Safe to file away."""

MAYBE = "maybe"
"""Promotional in shape but contested, or not clear enough. **Goes to a person, badged.**"""

NEITHER = "neither"
"""Nothing promotional about it. Routed exactly as it would have been."""

ADVERTISING_AT = 4
"""The promotional score `ADVERTISING` needs, on top of a zero business score.

Four is two independent tells, never one: the lightest pair that reaches it is a bulk subdomain plus
a deal word. A single strong term cannot hide a message on its own.
"""

MAYBE_AT = 2
"""The promotional score that earns a person's attention when nothing says the message is work.

Only reachable with a business score of zero — see `classify`. Above `ADVERTISING_AT` a message is
contested no matter what else it carries, which is the other way into `MAYBE`.
"""


@dataclass(frozen=True)
class Signal:
    weight: int
    why: str
    """The sentence a person reads on the queue row — "a discount figure in the subject", not a
    token. A reason nobody can read is a suppression nobody can check."""
    test: Callable[["Message"], bool]


@dataclass(frozen=True)
class Message:
    """Everything the lexicon is allowed to look at, gathered once by the caller."""

    subject: str = ""
    body_html: str = ""
    sender: str = ""
    has_po: bool = False
    has_attachment: bool = False


@dataclass(frozen=True)
class Verdict:
    band: str
    promo: int
    business: int
    matched: Tuple[str, ...] = ()
    """Why, in order of weight — every term that fired, promotional and business alike, so the row
    can explain both what made it look like advertising and what saved it."""


OPT_OUT_RE = re.compile(
    r"unsubscrib\w*"
    r"|opt[\s\-_]?outs?"
    r"|(?:manage|update|change|edit)\s+(?:your\s+)?"
    r"(?:e-?mail\s+|communication\s+|notification\s+|subscription\s+)?preferences"
    r"|e-?mail\s+preferences",
    re.IGNORECASE,
)
"""The opt-out mechanism US commercial mail is obliged to provide, named so triage and the reporting
tools share one definition rather than three that drift.

**"View this email in your browser" is deliberately absent.** It is not an opt-out mechanism, it is a
template artefact carried by every campaign layout including transactional ones - and measured here
at 9.1% of candidates against 0.0% of delivery bodies, it is clean but yields almost nothing, while
the senders most likely to carry it *without* an opt-out block are the software notifications this
lexicon is not meant to reach.
"""


def opt_out_phrase(body_html: str) -> Optional[str]:
    """The opt-out phrase a message carries, or None - the phrase itself, for a reason string.

    Reads the raw HTML on purpose. `text.html_to_text` runs `soup.get_text()` and drops every
    `href`, so the rendered body of `<a href="https://x.test/optout/abc">Click here</a>` is
    "Click here" and the mechanism - which is the link, not the words on it - is invisible.
    """
    match = OPT_OUT_RE.search(body_html or "")
    return re.sub(r"\s+", " ", match.group(0)).strip() if match else None


def _re(pattern: str) -> Callable[[Message], bool]:
    """A test over the subject. Most signals are one, so this keeps the lists readable."""
    compiled = re.compile(pattern, re.IGNORECASE)
    return lambda m: bool(compiled.search(m.subject or ""))


PROMOTIONAL: Tuple[Signal, ...] = (
    Signal(4, "an unsubscribe or opt-out block",
           lambda m: bool(OPT_OUT_RE.search(m.body_html or ""))),
    Signal(3, "a discount figure in the subject",
           _re(r"\d{1,3}\s*%\s*(?:off|discount)|up\s+to\s+\d{1,3}\s*%|\$\d+\s*off")),
    Signal(3, "clearance or seasonal-sale vocabulary",
           _re(r"\b(?:clearance|blowout|flash\s+sale|doorbuster|labor\s+day|black\s+friday"
               r"|cyber\s+monday|end[-\s]of[-\s]summer)\b")),
    Signal(2, "a call to shop or buy",
           _re(r"\bshop\s+now\b|\bbuy\s+now\b|\border\s+now\b|\bstock\s+up\b"
               r"|\bshop\b.{0,24}\bdeals?\b")),
    Signal(2, "deal or savings vocabulary",
           _re(r"\b(?:deals?|sale|savings?|discount|promo(?:tion)?|coupon|bogo)\b")),
    Signal(2, "urgency or scarcity language",
           _re(r"\b(?:don'?t\s+miss|last\s+call|ends\s+(?:soon|today)|today\s+only"
               r"|limited\s+time|just\s+for\s+you|too\s+good\s+to\s+last|final\s+hours"
               r"|while\s+supplies\s+last)\b")),
    # Emoji and dingbats. Premier's own mail is plain ASCII: 0.8% of 833 delivery subjects carry one
    # against 55% of adverts, which is the widest single gap measured on the envelope.
    Signal(2, "a marketing glyph in the subject",
           lambda m: any(ord(ch) > 0x2000 for ch in (m.subject or ""))),
    # Never seen once on 833 delivery mails. A campaign platform's own subdomain.
    Signal(2, "the sender is on a bulk-mail subdomain",
           lambda m: bool(re.search(r"@(?:e|em|email|mail|members|campaign|news|marketing|attn"
                                    r"|click|reply|travel\d*)\.", m.sender or "", re.IGNORECASE))),
    Signal(2, "an image-dominant layout",
           lambda m: len(re.findall(r"<img", m.body_html or "", re.IGNORECASE))
                     > max(1, len(re.findall(r"<p[\s>]", m.body_html or "", re.IGNORECASE)))),
    Signal(1, "a role sender rather than a person",
           lambda m: bool(re.match(r"(?:no-?reply|donotreply|deals?|news|editor|info|hello"
                                   r"|marketing|offers?)@", m.sender or "", re.IGNORECASE))),
    Signal(1, "webinar or newsletter framing",
           _re(r"\b(?:webinar|newsletter|free\s+trial|complimentary|register\s+now)\b")),
    Signal(1, "an exclamation or question closing the subject",
           lambda m: (m.subject or "").strip().endswith(("!", "?"))),
)

BUSINESS: Tuple[Signal, ...] = (
    Signal(5, "it names a purchase order",
           lambda m: m.has_po or bool(re.search(r"\bPO\s*#?\s*\d{6}\b|PO/Contract",
                                                m.subject or "", re.IGNORECASE))),
    # The strongest measured signal in either list: 0% of adverts, 84.6% of delivery mail.
    Signal(4, "it is a reply in a conversation",
           lambda m: bool(re.match(r"\s*(?:re|fw|fwd)\s*:", m.subject or "", re.IGNORECASE))),
    Signal(3, "it names a Premier property",
           _re(r"\b(?:sheraton|marriott|sofitel|hyatt|autograph|homewood|westin|aramark|hilton"
               r"|anchorage|duluth|cates\s+creek|sugar\s+land|circuit\s+of\s+the\s+americas)\b")),
    Signal(3, "it carries a spec, RR or project code",
           lambda m: bool(re.search(r"\b[A-Z]{2,4}-\d{3}[a-z]?-[A-Z]{2,3}\b|\bRR\s*\d{6}\b"
                                    r"|\b(?:SP|STT|CIW|ATW|CCB|ANS|MCOH)\d{2,6}\b",
                                    m.subject or ""))),
    Signal(3, "it carries an attachment", lambda m: m.has_attachment),
    Signal(2, "receiving and freight vocabulary",
           _re(r"\b(?:submittal|expediting|receiver|packing\s+slip|BOL|pallet|warehouse"
               r"|freight|consignment)\b")),
    Signal(1, "quote, invoice or contract vocabulary",
           _re(r"\b(?:quote|quotation|rfq|rfp|invoice|proposal|contract|purchase)\b")),
)


def classify(message: Message) -> Verdict:
    """Score one message against both lists and place it in a band.

    A business score above zero vetoes `ADVERTISING` outright, however promotional the rest of the
    message looks. That is deliberate and it is the whole safety argument: a subject reading
    "UP TO 70% OFF" on a thread that also names a purchase order is a vendor's promotion attached to
    real work, and it belongs in front of a person rather than in a folder nobody opens.
    """
    fired_promo = [s for s in PROMOTIONAL if s.test(message)]
    fired_business = [s for s in BUSINESS if s.test(message)]
    promo = sum(s.weight for s in fired_promo)
    business = sum(s.weight for s in fired_business)

    if business == 0 and promo >= ADVERTISING_AT:
        band = ADVERTISING
    elif promo >= ADVERTISING_AT or (promo >= MAYBE_AT and business == 0):
        # Two ways to be genuinely unsure, and neither is "a business email that happens to contain
        # the word sale". Either the message cleared the advertising bar and a business signal
        # vetoed it — a vendor's promotion on a live thread — or it leans promotional with nothing
        # at all to say it is work. Measured: gating only on `promo >= MAYBE_AT` badged replies like
        # "RE: Marriott Sugarland - Hand Tufted", which is real work carrying one stray word, and a
        # badge that fires on real work is a badge people learn to ignore.
        band = MAYBE
    else:
        band = NEITHER

    matched = tuple(s.why for s in
                    sorted(fired_promo + fired_business, key=lambda s: -s.weight))
    return Verdict(band=band, promo=promo, business=business, matched=matched)
