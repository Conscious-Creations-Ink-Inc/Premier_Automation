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
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from config import settings
from connectors.spitfire import PODocument, build_po_document
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


# --- projecting the warehouse into the mirror ---------------------------------
# `pipeline/spitfire_warehouse.py` holds every field of every endpoint for every PO the sweep has
# read. This mirror holds the 14 header and 18 line fields the matcher and the UI actually use.
# Until now the only way to fill it was `connectors.spitfire.read_po` — four live requests per PO —
# so it tracked whatever a reviewer happened to open rather than what we already had on disk.

# Our own bookkeeping columns, dropped before a warehouse row is handed to `build_po_document`.
# They are snake_case and Spitfire's are PascalCase, so nothing collides today; stripping them
# means nothing will if a future local column is ever named after an API field.
# Distinguishes "caller did not say" from "caller explicitly asked for no filter". A plain None
# default cannot express both, and the difference decides whether receipts reach the matcher.
_UNSET = object()

_WAREHOUSE_LOCAL_COLUMNS = frozenset((
    "sweep_id", "fetched_at", "raw_sha256", "row_sha256",
    "doc_master_key", "doc_item_key", "po_number", "project_code",
))


def _api_row(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys() if k not in _WAREHOUSE_LOCAL_COLUMNS}


def _children_by_parent(wh_conn: sqlite3.Connection, table: str, parent_column: str,
                        keys: Sequence[str]) -> Dict[str, List[dict]]:
    """{parent key (lowercased): [API-shaped rows]} for one batch of parents.

    Keys are lowercased on both sides because the same document key genuinely arrives in different
    cases from different endpoints — `DocMasterAlt` answers upper, the header lower. The warehouse
    columns collate NOCASE so the SQL matches either way, but a plain dict does not, and a case
    mismatch here would silently produce purchase orders with no lines.
    """
    out: Dict[str, List[dict]] = {}
    if not keys:
        return out
    prior = wh_conn.row_factory
    wh_conn.row_factory = sqlite3.Row
    try:
        marks = ",".join("?" * len(keys))
        for row in wh_conn.execute(
                f'SELECT * FROM {table} WHERE "{parent_column}" IN ({marks})', tuple(keys)):
            out.setdefault(str(row[parent_column] or "").lower(), []).append(_api_row(row))
    finally:
        wh_conn.row_factory = prior
    return out


def refresh_from_warehouse(state_conn: sqlite3.Connection, wh_conn: sqlite3.Connection, now: str,
                           po_numbers: Optional[Iterable[str]] = None,
                           doc_type_key: Optional[str] = _UNSET,
                           batch_size: int = 400) -> Tuple[int, int]:
    """Rebuild `spitfire_po_index` / `spitfire_po_lines` from the warehouse. Returns (POs, lines).

    **No network.** The warehouse already holds the four payloads `read_po` fetches — header,
    items (with their `DocItemTask` and `RelatedLineDetails` children), addresses and route — so
    they are reassembled into the same shape and handed to `connectors.spitfire.build_po_document`,
    the one parser that knows the field traps. Rows are written through `save_po`, so the
    delete-then-insert rule for lines is the same on this path as on the live one.

    `po_numbers` restricts the projection; None projects every purchase order in the warehouse.

    **`doc_type_key` is a guard, not a convenience.** The warehouse holds whatever the sweep was
    told to fetch, and `--doc-types all` puts receipts, pay requests and AP vouchers in
    `sf_document` alongside the purchase orders. Those number their documents per-parent — a
    receipt's `DocNo` is `0002` — so projecting them unfiltered would write a purchase order
    numbered "0002" into `spitfire_po_index` and let the matcher offer its lines against a real
    delivery. Only PO/Contracts documents are a purchase order. Pass None to disable the filter,
    which is only right for a warehouse known to hold nothing else.

    Work is batched by document so memory stays flat whether the warehouse holds 400 purchase
    orders or forty thousand.
    """
    if doc_type_key is _UNSET:
        doc_type_key = settings.SPITFIRE_PO_DOC_TYPE_KEY
    wanted = None
    if po_numbers is not None:
        wanted = {str(n).strip() for n in po_numbers if str(n or "").strip()}
        if not wanted:
            return 0, 0

    prior = wh_conn.row_factory
    wh_conn.row_factory = sqlite3.Row
    try:
        if doc_type_key:
            rows = wh_conn.execute(
                "SELECT * FROM sf_document WHERE DocTypeKey = ? ORDER BY DocNo", (doc_type_key,))
        else:
            rows = wh_conn.execute("SELECT * FROM sf_document ORDER BY DocNo")
        headers = [_api_row(r) for r in rows]
    finally:
        wh_conn.row_factory = prior

    def number_of(header: dict) -> str:
        return str(header.get("DocNo") or header.get("SubContract") or "").strip()

    headers = [h for h in headers if number_of(h)
               and (wanted is None or number_of(h) in wanted)]

    pos = lines = 0
    for start in range(0, len(headers), batch_size):
        batch = headers[start:start + batch_size]
        keys = [str(h.get("DocMasterKey") or "") for h in batch]

        items_by_doc = _children_by_parent(wh_conn, "sf_document_item", "doc_master_key", keys)
        addr_by_doc = _children_by_parent(wh_conn, "sf_document_address", "doc_master_key", keys)
        route_by_doc = _children_by_parent(wh_conn, "sf_document_route", "doc_master_key", keys)

        # The line children are keyed by DocItemKey, not by document, so they are fetched for the
        # batch's lines rather than its documents.
        item_keys = [str(i.get("DocItemKey") or "")
                     for rows in items_by_doc.values() for i in rows]
        tasks_by_item = _children_by_parent(wh_conn, "sf_item_task", "doc_item_key", item_keys)
        related_by_item = _children_by_parent(wh_conn, "sf_item_related", "doc_item_key", item_keys)

        for header in batch:
            key = str(header.get("DocMasterKey") or "")
            items = items_by_doc.get(key.lower(), [])
            for item in items:
                item_key = str(item.get("DocItemKey") or "").lower()
                item["DocItemTask"] = tasks_by_item.get(item_key, [])
                # `RelatedLineDetails` is one row per line; `_related_details` accepts the bare
                # object or a one-element list, and the warehouse stores at most one.
                item["RelatedLineDetails"] = related_by_item.get(item_key, [])
            items.sort(key=lambda i: str(i.get("DocItemNumber") or ""))

            doc = build_po_document(key, header, items,
                                    addr_by_doc.get(key.lower(), []),
                                    route_by_doc.get(key.lower(), []))
            if not doc.po_number:
                continue
            lines += save_po(state_conn, doc, now)
            pos += 1
    return pos, lines
