"""Thread splitting and origin-sender recovery.

Two corpus facts drive this module:

**The newest hop is rarely the informative one.** Every file Premier sent is a `Fw:` from Maria
Gutierrez carrying a one-line annotation, with the payload — an Authority Logistics notification,
or a property reply to a table sent three hops earlier — quoted underneath. Triaging on
`RawEmail.sender_address` alone therefore sees `premierpm.com` on all fourteen messages and the
real originator (`warehousing@authoritylogistics.com`, `routing@authoritylogistics.com`,
`Elber@5starinterior.com`) on none of them.

**Confirmations carry no PO.** *"We can confirm that we have only received the Sheer Fabric
(GR-350c-WTF)"* names neither a PO nor a quantity; both live in the request table quoted below
it. Extraction must be allowed to read down the thread, which means knowing where the hops are.

Production mail will usually arrive direct from the sender, but Premier forwards internally as a
matter of course, so both shapes have to work.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

# Outlook renders a quoted hop as a From/Sent/To[/Cc]/Subject header block. Coming out of HTML
# each label frequently lands on its own line with the value on the next, so the pattern has to
# tolerate newlines between the label and its value.
_HOP_HEADER_RE = re.compile(
    r"^[ \t]*From:[ \t]*(?:\n[ \t]*)*(?P<from>[^\n]+)\n"
    r"(?:[^\n]*\n){0,10}?[ \t]*Sent:[ \t]*(?:\n[ \t]*)*(?P<sent>[^\n]+)\n"
    r"(?:[^\n]*\n){0,10}?[ \t]*To:[ \t]*(?:\n[ \t]*)*(?P<to>[^\n]+)\n"
    r"(?:[^\n]*\n){0,15}?[ \t]*Subject:[ \t]*(?:\n[ \t]*)*(?P<subject>[^\n]+)",
    re.MULTILINE,
)
# The bounded `(?:[^\n]*\n){0,N}?` gaps absorb what sits between the labels: a `Cc:` line, and —
# the reason a tighter pattern failed on the Authority forwards — a `To:` recipient list that
# wraps across three or four lines before `Subject:` appears.

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Addresses that only ever forward — seeing one as the sender means the payload came from
# somewhere else and the quoted chain must be consulted.
INTERNAL_DOMAINS = {"premierpm.com"}


# Every `Sent:` shape in the corpus, measured across its 111 quoted headers rather than guessed.
# Three of the four were obvious; the fourth was not — six headers use 24-hour time with no AM/PM
# marker, and a list without it silently drops them.
_SENT_FORMATS = (
    "%A, %B %d, %Y %I:%M %p",       # Monday, December 1, 2025 2:14 PM
    "%A, %B %d, %Y %I:%M:%S %p",    # Thursday, October 9, 2025 11:17:49 AM
    "%A, %B %d, %Y %H:%M",          # Thursday, May 7, 2026 17:03
    "%A, %B %d, %Y %H:%M:%S",
)


def parse_sent(raw: str) -> Optional[str]:
    """An Outlook `Sent:` header as an ISO date, or None.

    Returns the date only — `YYYY-MM-DD`. These are local wall-clock strings with no zone, so the
    time of day is not comparable across senders and pretending otherwise would invent precision.
    The date is what a delivery timeline is asked about.

    None rather than an exception on anything unrecognised: this runs over mail from ~3,000 outside
    parties, and one unfamiliar locale must not fail the ingest of an otherwise readable message.
    `ThreadHop.sent_raw` keeps the original either way, so a format we cannot read stays visible
    rather than disappearing.
    """
    text = (raw or "").strip()
    if not text:
        return None
    for fmt in _SENT_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


@dataclass
class ThreadHop:
    """One message in the quoted chain. `depth` 0 is the newest (the delivered message itself)."""
    depth: int
    sender_address: str
    sender_domain: str
    sent_raw: str
    to_raw: str
    subject: str
    body: str

    @property
    def sender_local_part(self) -> str:
        return self.sender_address.split("@")[0].lower() if "@" in self.sender_address else ""

    @property
    def sent_at(self) -> Optional[str]:
        """When this hop was actually sent, `YYYY-MM-DD`, or None.

        This is the date a delivery timeline should show. The envelope date of a forwarded message
        is the day Premier forwarded it — on the corpus that is 2026-06-06 for twelve of fourteen
        messages, which is how every stage of every purchase order came to show one date.
        """
        return parse_sent(self.sent_raw)


@dataclass
class ParsedThread:
    hops: List[ThreadHop] = field(default_factory=list)

    @property
    def newest(self) -> Optional[ThreadHop]:
        return self.hops[0] if self.hops else None

    def full_text(self) -> str:
        return "\n".join(hop.body for hop in self.hops)


def _first_address(raw: str) -> str:
    match = _EMAIL_RE.search(raw or "")
    return match.group(0).lower() if match else ""


def _domain_of(address: str) -> str:
    return address.split("@")[-1].lower() if "@" in address else ""


def split_thread(text: str, top_sender: str = "", top_subject: str = "") -> ParsedThread:
    """Split rendered body text into hops at each quoted From/Sent/To/Subject block.

    The text above the first quoted block becomes hop 0, attributed to `top_sender` — that is
    where Maria's ground-truth annotation ("straightforward, WH rec'd", "Human at property
    confirmed delivery") lives, and it is worth keeping addressable.
    """
    thread = ParsedThread()
    if not text:
        return thread

    matches = list(_HOP_HEADER_RE.finditer(text))

    head = text[: matches[0].start()] if matches else text
    address = _first_address(top_sender)
    thread.hops.append(ThreadHop(
        depth=0, sender_address=address, sender_domain=_domain_of(address),
        sent_raw="", to_raw="", subject=top_subject or "", body=head.strip(),
    ))

    for index, match in enumerate(matches):
        body_start = match.end()
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        hop_address = _first_address(match.group("from"))
        thread.hops.append(ThreadHop(
            depth=index + 1,
            sender_address=hop_address,
            sender_domain=_domain_of(hop_address),
            sent_raw=(match.group("sent") or "").strip(),
            to_raw=(match.group("to") or "").strip(),
            subject=(match.group("subject") or "").strip(),
            body=text[body_start:body_end].strip(),
        ))
    return thread


def resolve_origin(
    sender_address: str,
    subject: str,
    thread: ParsedThread,
) -> ThreadHop:
    """The hop that actually produced the payload we care about.

    A forward from an internal address with an empty or annotation-only top hop is transparent:
    the payload belongs to the first *external* quoted hop below it. Anything else is taken at
    face value — a genuine reply from a property contact is the payload, even though quoted
    hops sit below it.
    """
    address = _first_address(sender_address)
    top = thread.newest or ThreadHop(0, address, _domain_of(address), "", "", subject, "")
    top.sender_address = top.sender_address or address
    top.sender_domain = top.sender_domain or _domain_of(address)
    top.subject = top.subject or subject

    is_forward = subject.strip().lower().startswith(("fw:", "fwd:"))
    if not is_forward or _domain_of(address) not in INTERNAL_DOMAINS:
        return top

    for hop in thread.hops[1:]:
        if hop.sender_domain and hop.sender_domain not in INTERNAL_DOMAINS:
            return hop
    # An internal-only forward chain (e.g. the Kamilah -> Ivo tracker thread) has no external
    # origin; the newest internal hop below the annotation is the best answer available.
    return thread.hops[1] if len(thread.hops) > 1 else top


def strip_forward_prefixes(subject: str) -> str:
    """`"Fw: [External] RE: Cameo ..."` -> `"Cameo ..."`. Vendor subject grammars are written
    against what the originator actually sent, so the accumulated prefixes have to come off."""
    if not subject:
        return ""
    cleaned = subject
    while True:
        stripped = re.sub(r"^\s*(?:(?:re|fw|fwd|aw|tr)\s*:|\[external\]\s*)\s*", "", cleaned, flags=re.IGNORECASE)
        if stripped == cleaned:
            return cleaned.strip()
        cleaned = stripped
