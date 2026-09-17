"""Boilerplate stripping.

Byte count in this corpus is dominated by text that carries no signal: the confidentiality
NOTICE is repeated once per forwarding hop (six times in the Cintas thread), every external
message carries a CAUTION banner, and each hop ends with a signature block plus a row of social
links. The Cintas body is ~113 KB of HTML for roughly twenty lines of actual content.

This matters for correctness, not just tidiness. Stage 1's property-reply rule classifies on a
word count (`PROPERTY_REPLY_MAX_WORDS`); with boilerplate counted, a two-word reply — *"Yes
ma'am, this was received!"* — measures in the hundreds of words and fails the rule.
"""

import re
from typing import List, Sequence

from config import settings


def _anchor(fragment: str) -> str:
    """A configured contact fragment as a whitespace-tolerant pattern.

    Signature blocks survive a round trip through HTML rendering, so the run of whitespace between
    words is not reliable — `Suite 400` arrives as `Suite&nbsp;400`, across a line break, or with
    the space gone entirely. Every gap therefore becomes `\\s*`, and everything else is escaped.

    Split first, escape second. `re.escape` escapes spaces too (they are significant under
    `re.VERBOSE`), so substituting into an already-escaped string leaves the backslash behind and
    yields `\\\\s*` — a pattern that matches a literal backslash and never the address it was
    built from.
    """
    return r"\s*".join(re.escape(part) for part in fragment.split())


def _domain_alternation(domains: Sequence[str]) -> str:
    """`(?:a\\.test|b\\.test)` from configured domains, longest first so a suffix cannot win."""
    ordered = sorted({d.strip().lower() for d in domains if d.strip()}, key=len, reverse=True)
    return r"(?:" + "|".join(re.escape(d) for d in ordered) + r")" if ordered else r"(?!)"


_BLOCK_PATTERNS: List[re.Pattern] = [
    # Confidentiality footer, in the two wordings seen. Runs to the end of the sentence.
    re.compile(
        r"NOTICE:\s*This email contains confidential information.*?(?:copies and attachments\.|delete it immediately\.)",
        re.IGNORECASE | re.DOTALL,
    ),
    # External-sender security banner prepended by Premier's gateway.
    #
    # Greedy and bounded, not lazy. The banner contains *both* terminators, in this order:
    # "...report it and delete it immediately. PDF files in particular may contain hidden scripts
    # or payloads that can be used to stage malware for future attacks." A lazy `.*?` stops at the
    # first one and leaves the PDF sentence behind as the entire visible content of the newest hop
    # on every forwarded message in the corpus. `{0,900}` greedy takes the last terminator inside
    # the banner's own length while still refusing to run away into real content.
    re.compile(
        r"CAUTION:\s*This email originated from outside the organization[\s\S]{0,900}"
        r"(?:future attacks\.|delete it immediately\.)",
        re.IGNORECASE | re.DOTALL,
    ),
    # The same banner without its "CAUTION:" opener — Outlook drops the prefix when the message is
    # forwarded, so the quoted hops below depth 0 carry the body alone.
    re.compile(
        r"(?:Please exercise caution when clicking|As a security reminder, please do not click)"
        r"[\s\S]{0,900}(?:future attacks\.|delete it immediately\.)",
        re.IGNORECASE | re.DOTALL,
    ),
    # Outlook's first-contact interstitial, which sits above the banner on external mail.
    re.compile(
        r"(?:You don't often get email from|Some people who received this message don't often get"
        r" email from)[\s\S]{0,120}?Learn why this is important",
        re.IGNORECASE,
    ),
    # The warehouse partner's own footer.
    re.compile(
        _anchor(settings.BOILERPLATE_PARTNER_ADDRESS)
        + r".*?(?:" + re.escape(settings.BOILERPLATE_PARTNER_PHONE) + r"|www\."
        + _domain_alternation(settings.WAREHOUSE_SENDER_DOMAINS) + r")",
        re.IGNORECASE | re.DOTALL,
    ),
    # Internal signature: closing through the social-link row. Anchored on the address line so a
    # closing that is *followed by real content* (a reply typed under "Thanks,") is left alone.
    re.compile(
        r"(?:Best regards|Kind regards|Regards|Thanks|Thank you)[,!]?\s*\n.{0,400}?"
        + _anchor(settings.BOILERPLATE_INTERNAL_ADDRESS)
        + r".*?(?:Twitter|" + _domain_alternation(settings.INTERNAL_DOMAINS) + r")",
        re.IGNORECASE | re.DOTALL,
    ),
]

# Leftover chrome once the blocks above are gone: bare social words on their own line, the
# separator pipes between them, and the "Website:" label.
_NOISE_DOMAINS = _domain_alternation(
    settings.INTERNAL_DOMAINS + settings.WAREHOUSE_SENDER_DOMAINS
    + settings.VENDOR_CONFIRMATION_DOMAINS
)
_LINE_NOISE_RE = re.compile(
    r"^\s*(?:\|+|Instagram|Facebook|LinkedIn|Twitter|Website:?|Email:?|"
    r"(?:https?://|www\.)?" + _NOISE_DOMAINS + r")\s*\|?\s*$",
    re.IGNORECASE,
)


def strip_boilerplate(text: str) -> str:
    """Remove signature/legal/banner blocks. Quoted hops are left in place — they are where the
    Class C/D identifying data lives (`thread.py` is what splits those apart)."""
    if not text:
        return ""
    cleaned = text
    for pattern in _BLOCK_PATTERNS:
        cleaned = pattern.sub("\n", cleaned)
    kept = [line for line in cleaned.splitlines() if not _LINE_NOISE_RE.match(line)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def significant_word_count(text: str) -> int:
    """Word count after stripping — the number the property-reply heuristic should be reading."""
    return len(strip_boilerplate(text).split())
