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
from typing import List

_BLOCK_PATTERNS: List[re.Pattern] = [
    # Confidentiality footer, in the two wordings seen. Runs to the end of the sentence.
    re.compile(
        r"NOTICE:\s*This email contains confidential information.*?(?:copies and attachments\.|delete it immediately\.)",
        re.IGNORECASE | re.DOTALL,
    ),
    # External-sender security banner prepended by Premier's gateway.
    re.compile(
        r"CAUTION:\s*This email originated from outside the organization.*?(?:future attacks\.|delete it immediately\.)",
        re.IGNORECASE | re.DOTALL,
    ),
    # Authority Logistics' own footer.
    re.compile(
        r"400 Chesterfield Center,\s*Suite 400.*?(?:844\.502\.9998|www\.authoritylogistics\.com)",
        re.IGNORECASE | re.DOTALL,
    ),
    # Premier signature: closing through the social-link row. Anchored on the address line so a
    # closing that is *followed by real content* (a reply typed under "Thanks,") is left alone.
    re.compile(
        r"(?:Best regards|Kind regards|Regards|Thanks|Thank you)[,!]?\s*\n.{0,400}?"
        r"14185 Dallas Parkway.*?(?:Twitter|premierpm\.com)",
        re.IGNORECASE | re.DOTALL,
    ),
]

# Leftover chrome once the blocks above are gone: bare social words on their own line, the
# separator pipes between them, and the "Website:" label.
_LINE_NOISE_RE = re.compile(
    r"^\s*(?:\|+|Instagram|Facebook|LinkedIn|Twitter|Website:?|Email:?|http://premierpm\.com|"
    r"www\.(?:premierpm|authoritylogistics|5starinterior)\.com)\s*\|?\s*$",
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
