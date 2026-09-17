"""Everything a reader saw in a document, kept whole.

`extracted_records` is the pipeline's answer to "what was delivered" — nine or so useful columns,
shaped by what a Spitfire receipt needs. A real document is not that shape. An Atlas Logistics
Warehouse Receiving Report OCRs into nine clean tables carrying `Customer PO # 213994`,
`Received by T.Reed`, `BOL/PRO ED6 96925779`, `Total Received 2 pallets` and a 22-row line-item
grid with part numbers, weights and dimensions. `build_record_from_row` mapped one of those tables
onto six fields, dropped the other eight, and the email went to a person as "nothing extractable".

The read had worked. The data was thrown away on the way out. That is the gap this table closes:
the reader's full output is stored against the attachment, so a question nobody had asked yet is
answerable later — without re-reading the file, and for a scanned page without paying for OCR a
second time.

**Deliberately not a replacement for `extracted_records`.** That table is a claim about a delivery,
built and checked and eventually posted. This one is evidence: what the document said, verbatim,
with no interpretation. Keeping them separate is what lets the extraction rules change without
rewriting history, and what makes a new parser something you can develop against stored input.

Read it with `for_email` / `get`, or as JSON straight out of `payload`.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_COLUMNS = ("ledger_id", "email_id", "filename", "container_path", "adapter",
            "raw_text", "tables_json", "table_count", "row_count", "char_count",
            "content_sha256", "parsed_at")


@dataclass
class ParsedDocument:
    ledger_id: Optional[int]
    email_id: str
    filename: str
    adapter: str
    raw_text: str = ""
    tables: List[List[List[str]]] = field(default_factory=list)
    container_path: str = ""
    content_sha256: str = ""
    parsed_at: str = ""

    @property
    def table_count(self) -> int:
        return len(self.tables)

    @property
    def row_count(self) -> int:
        return sum(len(t) for t in self.tables)

    def payload(self) -> Dict[str, Any]:
        """The document as JSON — what a caller wanting "give me everything" gets."""
        return {
            "email_id": self.email_id,
            "filename": self.filename,
            "adapter": self.adapter,
            "parsed_at": self.parsed_at,
            "text": self.raw_text,
            "tables": self.tables,
        }

    def cells(self) -> Dict[str, str]:
        """Every two-column `label -> value` pair across every table, flattened.

        Forms like the Atlas report put a label row above a value row, or a label cell beside a
        value cell, and which one varies by table on the *same page*. Both shapes reduce to pairs,
        and a pair is what any later rule wants to ask about — `Customer PO #`, `Received by`,
        `Total Received`. Offered as a convenience over `tables`, never instead of it.
        """
        pairs: Dict[str, str] = {}
        for table in self.tables:
            if not table:
                continue
            # A two-column table is genuinely ambiguous — it can be a list of label/value rows
            # (`Dept. #: | 0`) or a header over one value row (`Customer PO # | Received by` over
            # `213994 | T.Reed`), and the Atlas report contains both on the same page. The trailing
            # colon is what separates them: a label block writes `Dept. #:`, a header does not.
            # Without this the header case reads "Customer PO # = Received by".
            if len(table[0]) == 2:
                labelled = any(str(row[0]).strip().endswith(":") for row in table if row)
                if labelled or len(table) > 2:
                    for row in table:
                        if len(row) == 2 and str(row[0]).strip():
                            pairs.setdefault(str(row[0]).strip().rstrip(":"), str(row[1]).strip())
                    continue
            # Header row over value rows — the shape Azure returns for the report's key blocks.
            header = [str(c).strip().rstrip(":") for c in table[0]]
            for row in table[1:]:
                for name, value in zip(header, row):
                    if name and str(value).strip():
                        pairs.setdefault(name, str(value).strip())
        return pairs


def save(conn: sqlite3.Connection, document: ParsedDocument) -> None:
    """Upsert one document's parse. Keyed on `ledger_id`, so re-reading an attachment replaces
    what it said rather than accumulating copies — a re-extract after an outage is a correction,
    not a second opinion."""
    conn.execute(
        f"INSERT OR REPLACE INTO parsed_documents ({', '.join(_COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(_COLUMNS))})",
        (document.ledger_id, document.email_id, document.filename or "",
         document.container_path or "", document.adapter or "",
         document.raw_text or "", json.dumps(document.tables or [], ensure_ascii=False),
         document.table_count, document.row_count, len(document.raw_text or ""),
         document.content_sha256 or "", document.parsed_at or ""),
    )
    conn.commit()


def _to_document(row: sqlite3.Row) -> ParsedDocument:
    return ParsedDocument(
        ledger_id=row["ledger_id"], email_id=row["email_id"], filename=row["filename"],
        adapter=row["adapter"], raw_text=row["raw_text"] or "",
        tables=json.loads(row["tables_json"] or "[]"),
        container_path=row["container_path"] or "", content_sha256=row["content_sha256"] or "",
        parsed_at=row["parsed_at"] or "",
    )


def _query(conn: sqlite3.Connection, sql: str, params=()) -> List[ParsedDocument]:
    prior = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return [_to_document(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.row_factory = prior


def get(conn: sqlite3.Connection, ledger_id: int) -> Optional[ParsedDocument]:
    found = _query(conn, "SELECT * FROM parsed_documents WHERE ledger_id = ?", (ledger_id,))
    return found[0] if found else None


def for_email(conn: sqlite3.Connection, email_id: str) -> List[ParsedDocument]:
    return _query(conn, "SELECT * FROM parsed_documents WHERE email_id = ? ORDER BY ledger_id",
                  (email_id,))


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM parsed_documents").fetchone()[0]
