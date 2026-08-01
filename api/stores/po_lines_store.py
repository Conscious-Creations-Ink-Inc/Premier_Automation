"""Query layer for the demo PO-line catalogue — the candidate set reconciliation matches against.

Mirrors `pipeline.extracted_records_store`: the connection comes first, writes commit
themselves, and rows convert back into the pipeline's own `POLine` dataclass so the matching
code never sees a raw sqlite row.
"""
import sqlite3
from dataclasses import dataclass
from typing import List, Optional, Sequence

from pipeline.models import POLine

_COLUMNS = (
    "po_number", "line_number", "line_key", "spec_code", "description", "vendor_name",
    "unit_of_measure", "qty_ordered", "qty_received", "cost_code", "project_code",
    "project_name", "line_status", "expected_date", "ship_to", "assigned_agent", "pay_terms",
)


@dataclass
class POLineRow:
    """A `POLine` plus its row id — the id is what the UI sends back when a reviewer picks a
    candidate line to approve against."""
    id: int
    line: POLine

    @property
    def qty_outstanding(self) -> float:
        return self.line.qty_ordered - self.line.qty_received


def insert(conn: sqlite3.Connection, line: POLine) -> int:
    placeholders = ", ".join(["?"] * len(_COLUMNS))
    cursor = conn.execute(
        f"INSERT INTO po_lines ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
        tuple(getattr(line, column) for column in _COLUMNS),
    )
    conn.commit()
    return cursor.lastrowid


def insert_many(conn: sqlite3.Connection, lines: Sequence[POLine]) -> List[int]:
    return [insert(conn, line) for line in lines]


def list_all(conn: sqlite3.Connection) -> List[POLineRow]:
    return _query(conn, "SELECT * FROM po_lines ORDER BY po_number, line_number")


def get(conn: sqlite3.Connection, po_line_id: int) -> Optional[POLineRow]:
    rows = _query(conn, "SELECT * FROM po_lines WHERE id = ?", (po_line_id,))
    return rows[0] if rows else None


def list_for_po(conn: sqlite3.Connection, po_number: str) -> List[POLineRow]:
    return _query(
        conn, "SELECT * FROM po_lines WHERE po_number = ? ORDER BY line_number", (po_number,)
    )


def add_received_qty(conn: sqlite3.Connection, po_line_id: int, quantity: float) -> None:
    """Approving a receipt moves quantity onto the line, which keeps the demo dataset
    self-consistent — a later receipt against the same line can then legitimately over-receive
    and get flagged for it."""
    conn.execute(
        "UPDATE po_lines SET qty_received = qty_received + ? WHERE id = ?", (quantity, po_line_id)
    )
    conn.commit()


def _query(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> List[POLineRow]:
    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return [
            POLineRow(id=row["id"], line=POLine(**{column: row[column] for column in _COLUMNS}))
            for row in rows
        ]
    finally:
        conn.row_factory = prior_factory
