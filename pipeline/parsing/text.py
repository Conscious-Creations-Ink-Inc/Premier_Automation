"""HTML -> text rendering.

Every message in the corpus has an empty `body` and a populated `htmlBody`, so anything reading
"the text of the email" has to render the HTML first. Doing that consistently in one place also
fixes a subtler problem: Outlook emits each label and its value in separate block elements
(`From:` / newline / the address), and the thread splitter's header patterns only work if that
line structure is preserved rather than collapsed to a single space.
"""

import re
from typing import Optional

from bs4 import BeautifulSoup

_BLOCK_TAGS = ["p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"]


def html_to_text(html: Optional[str]) -> str:
    """Render to text with block boundaries preserved as newlines.

    `get_text("\\n")` alone is not enough: it inserts a separator between *every* string node,
    which shreds a sentence split across inline `<span>`s into one word per line. Instead the
    block-level tags get an explicit newline appended and inline runs are joined normally.
    """
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.append("\n")
    for cell in soup.find_all(["td", "th"]):
        cell.append("\t")   # keeps table rows readable on one line, columns separable
    text = soup.get_text()
    text = text.replace("\xa0", " ").replace("​", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def body_text_of(email) -> str:
    """The best available plain text for a `RawEmail` — its own `body_text` when the sender
    supplied one, otherwise the rendered HTML."""
    if getattr(email, "body_text", None) and email.body_text.strip():
        return email.body_text
    return html_to_text(getattr(email, "body_html", None))
