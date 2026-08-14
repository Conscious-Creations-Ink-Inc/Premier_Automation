"""Local mirror of Spitfire PO headers and lines.

Stage 4 needs a PO's lines for every delivery notification it matches. Fetching them live each
time costs four round trips per PO, and a PO number costs a site-wide search before that — so a
busy morning's mail would hammer Premier's ERP re-reading the same handful of purchase orders.
This is the cache that stops it, and it is also what lets the matcher run at all when Spitfire is
unreachable, against data that is stale but truthfully stamped.

Writes here are strictly local. Nothing in this module talks to Spitfire; `connectors.spitfire`
does the reading, and it cannot write.
"""

import sqlite3
from typing import List, Optional

from connectors.spitfire import PODocument
from pipeline.models import POLine

_LINE_COLUMNS = (
    "line_key", "po_number", "line_number", "spec_code", "description", "vendor_name",
    "unit_of_measure", "qty_ordered", "qty_received", "qty_in_transit", "cost_code",
    "project_code", "project_name", "line_status", "expected_date", "ship_to", "assigned_agent",
    "pay_terms",
)
# Every field on POLine appears here. A field the model carries but the mirror drops is silently
# lost between the connector and Stage 4 — the same failure `extracted_records_store._COLUMNS`
# guards against, and `test_models.py` pins that one the same way.

_INDEX_COLUMNS = (
    "po_number", "doc_master_key", "project_code", "project_name", "doc_status",
    "doc_status_label", "source_date", "order_date", "vendor_name", "vendor_email", "ship_to",
    "assigned_agent", "pay_terms_prose", "tax_lines_skipped",
)
# `source_date` and `order_date` are both stored and are not interchangeable — see
# `PODocument.source_date`. The timeline reads `order_date`; `source_date` is kept for comparison.


def save_po(conn: sqlite3.Connection, doc: PODocument, now: str) -> int:
    """Upsert one PO and replace its lines wholesale. Returns the number of lines stored.

    Lines are deleted and re-inserted rather than merged, because a line can genuinely disappear
    from a PO — a cancelled item, or a revision that renumbers — and a merge would leave the
    stale row behind for the matcher to find. The delete is scoped to the PO, inside the same
    transaction as the insert, so a failed refresh leaves the previous mirror intact rather than
    an empty PO that reads as "no lines" to Stage 4.
    """
    index_values = tuple(getattr(doc, name) for name in _INDEX_COLUMNS)
    conn.execute(
        f"INSERT INTO spitfire_po_index ({', '.join(_INDEX_COLUMNS)}, refreshed_at) "
        f"VALUES ({', '.join('?' * len(_INDEX_COLUMNS))}, ?) "
        f"ON CONFLICT(po_number) DO UPDATE SET "
        + ", ".join(f"{c} = excluded.{c}" for c in _INDEX_COLUMNS if c != "po_number")
        + ", refreshed_at = excluded.refreshed_at",
        index_values + (now,),
    )
    conn.execute("DELETE FROM spitfire_po_lines WHERE po_number = ?", (doc.po_number,))
    for line in doc.lines:
        conn.execute(
            f"INSERT INTO spitfire_po_lines ({', '.join(_LINE_COLUMNS)}, refreshed_at) "
            f"VALUES ({', '.join('?' * len(_LINE_COLUMNS))}, ?)",
            tuple(getattr(line, col) for col in _LINE_COLUMNS) + (now,),
        )
    conn.commit()
    return len(doc.lines)


def doc_key_for(conn: sqlite3.Connection, po_number: str) -> Optional[str]:
    """The cached `DocMasterKey`, so a known PO costs no search at all."""
    row = conn.execute(
        "SELECT doc_master_key FROM spitfire_po_index WHERE po_number = ?", (po_number,)
    ).fetchone()
    return row[0] if row else None


def lines_for(conn: sqlite3.Connection, po_number: str) -> List[POLine]:
    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM spitfire_po_lines WHERE po_number = ? ORDER BY line_number, spec_code",
            (po_number,),
        ).fetchall()
        return [POLine(**{col: row[col] for col in _LINE_COLUMNS}) for row in rows]
    finally:
        conn.row_factory = prior_factory


def header_for(conn: sqlite3.Connection, po_number: str) -> Optional[dict]:
    """The mirrored PO header — vendor, status, order date.

    `lines_for` alone is not the whole purchase order. A screen falling back to the mirror because
    Spitfire could not be reached would otherwise show quantities under a blank vendor and no
    status, which reads as missing data rather than as data from a moment ago.
    """
    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM spitfire_po_index WHERE po_number = ?", (po_number,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.row_factory = prior_factory


def refreshed_at(conn: sqlite3.Connection, po_number: str) -> Optional[str]:
    """When this PO was last read from Spitfire.

    Stage 4 should put this on anything it proposes. A match made against a three-week-old mirror
    is not wrong, but a reviewer deciding whether to trust it needs to know — quantities move.
    """
    row = conn.execute(
        "SELECT refreshed_at FROM spitfire_po_index WHERE po_number = ?", (po_number,)
    ).fetchone()
    return row[0] if row else None


def mirrored_po_numbers(conn: sqlite3.Connection) -> List[str]:
    return [r[0] for r in conn.execute(
        "SELECT po_number FROM spitfire_po_index ORDER BY po_number"
    ).fetchall()]
