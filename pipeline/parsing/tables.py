"""HTML table extraction and classification.

Outlook lays messages out with tables, so "the body contains a `<table>`" says nothing about
whether it contains data. The Cintas property-confirmation thread holds **117 tables**, of which
one is the item list and the rest are signature blocks, spacers and quoted-hop wrappers. Stage 1
used `soup.find("table") is not None` as a triage signal (finding C6's sibling problem); this
module replaces that with header-signature matching.

Two table shapes matter:

* **key/value** — the Authority Inbound header block (`Received Date:` | `09/24/2025`, ...).
* **grid** — a header row followed by data rows (`PO # / Line # | Supplier | Part # | Item |
  Package | Comments`).
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from bs4 import BeautifulSoup
from rapidfuzz import fuzz

HEADER_MATCH_THRESHOLD = 82   # rapidfuzz ratio; below this a header is considered absent


@dataclass
class HtmlTable:
    index: int
    rows: List[List[str]]

    @property
    def header(self) -> List[str]:
        return self.rows[0] if self.rows else []

    @property
    def body_rows(self) -> List[List[str]]:
        return self.rows[1:] if len(self.rows) > 1 else []

    @property
    def width(self) -> int:
        return max((len(row) for row in self.rows), default=0)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def extract_tables(html: Optional[str]) -> List[HtmlTable]:
    """Every table in the document, innermost included, in document order.

    Nested tables are kept: Outlook wraps real content in layout tables, so discarding a table
    because it has a table inside it would discard the wrapper *and* leave the inner one
    unreachable if we only walked top-level nodes.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    tables: List[HtmlTable] = []
    for index, element in enumerate(soup.find_all("table")):
        rows: List[List[str]] = []
        for tr in element.find_all("tr"):
            # A cell belonging to a nested table is that table's, not this one's.
            cells = [_clean(cell.get_text(" ", strip=True))
                     for cell in tr.find_all(["td", "th"], recursive=False) or tr.find_all(["td", "th"])]
            if any(cells):
                rows.append(cells)
        if rows:
            tables.append(HtmlTable(index=index, rows=rows))
    return tables


def header_score(header: Sequence[str], expected: Sequence[str]) -> float:
    """Mean best-match score of each expected header against the row, 0-100.

    Fuzzy because the same column shows up as `PO #`, `PO#`, `P.O.#` and `Purchase Order` across
    senders, and because a stray non-breaking space or trailing colon should not disqualify a row.
    """
    if not header or not expected:
        return 0.0
    normalized = [h.lower().strip(" :#") for h in header if h]
    if not normalized:
        return 0.0
    total = 0.0
    for want in expected:
        want_norm = want.lower().strip(" :#")
        total += max(fuzz.ratio(want_norm, candidate) for candidate in normalized)
    return total / len(expected)


def find_grid(tables: Sequence[HtmlTable], expected_headers: Sequence[str]) -> Optional[HtmlTable]:
    """The table whose header row best matches `expected_headers`, or None if none clears the
    threshold. Returns the *best* match, not the first — a layout table can score above the
    threshold by accident when the real one scores higher."""
    best, best_score = None, HEADER_MATCH_THRESHOLD
    for table in tables:
        if len(table.rows) < 2:
            continue
        score = header_score(table.header, expected_headers)
        if score > best_score:
            best, best_score = table, score
    return best


def find_all_grids(tables: Sequence[HtmlTable], expected_headers: Sequence[str]) -> List[HtmlTable]:
    """Every table clearing the threshold, in document order.

    Needed for quoted threads: the same request table is re-quoted at each hop, and a property
    reply may answer a table three hops down while a newer, partially-filled copy sits above it.
    """
    return [
        table for table in tables
        if len(table.rows) >= 2 and header_score(table.header, expected_headers) >= HEADER_MATCH_THRESHOLD
    ]


def as_key_values(table: HtmlTable) -> Dict[str, str]:
    """Two-column key/value table -> dict, keyed on the label with trailing `:` and case removed.

    Rows wider than two cells are read as label + everything-else-joined, which is how the
    Authority header block renders when the address wraps onto extra cells.
    """
    values: Dict[str, str] = {}
    for row in table.rows:
        if len(row) < 2 or not row[0]:
            continue
        key = row[0].strip().rstrip(":").strip().lower()
        value = " ".join(cell for cell in row[1:] if cell).strip()
        if key and key not in values:
            values[key] = value
    return values


def column_index(header: Sequence[str], names: Sequence[str], threshold: float = HEADER_MATCH_THRESHOLD) -> Optional[int]:
    """Index of the column matching any of `names`, fuzzily. None when absent — callers must
    handle that rather than defaulting to column 0, because a missing column and a column of
    empty values mean very different things (see the tracker rows with no Spec#)."""
    best_index, best_score = None, threshold
    for index, cell in enumerate(header):
        candidate = (cell or "").lower().strip(" :#")
        if not candidate:
            continue
        score = max(fuzz.ratio(name.lower().strip(" :#"), candidate) for name in names)
        if score > best_score:
            best_index, best_score = index, score
    return best_index


def cell(row: Sequence[str], index: Optional[int]) -> str:
    """Bounds-safe cell read — data rows are routinely shorter than the header row when the
    sender leaves trailing cells off entirely."""
    if index is None or index < 0 or index >= len(row):
        return ""
    return row[index] or ""
