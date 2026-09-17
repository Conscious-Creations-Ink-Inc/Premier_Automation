"""One row per physical delivery, and the item lines that belong to it.

A purchase order does not arrive; a *delivery against* it arrives, carrying some of its lines. That
distinction is the whole point of this table. Before it, one Authority notice covering six lines of
PO 206725 became six unrelated rows in `extracted_records`, each of which went on to create its own
Spitfire receipt — eight of them on PO 212559 in one afternoon.

**`extracted_records` stays the child**, one row per item line, and keeps every column it had.
Thirty read sites across twenty-seven files select from it, and the shape of the graph is the same
whichever table is named "the record": one delivery, N items. Adding the parent beside it breaks
none of them; moving the item columns out of it would have broken all of them at once.

The key is `(po_number, delivery_ref)` — the same key Stage 2 accumulates on, resolved by
`dedupe.delivery_ref`. So a delivery has exactly one row here no matter how many messages described
it, and two genuine deliveries on one purchase order have two.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable, List, Optional, Sequence

# Facts about the delivery rather than about any one item on it. Read off the first message that
# states each: an Inbound notice names the carrier and the tracking number, a POD names the date it
# arrived and who signed, and neither states what the other does.
_FIELDS: Sequence[str] = (
    "shipment_number", "notification_number", "source_email_id", "pod_stated_date",
    "received_by", "carrier_name", "tracking_number", "delivery_location", "vendor_name",
    "email_date", "extraction_source",
)


def _get(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    try:
        return row[name]
    except (IndexError, KeyError, TypeError):
        return getattr(row, name, None)


def upsert(conn: sqlite3.Connection, *, po_number: str, delivery_ref: str, delivery_rung: str,
           now: str, facts: Any = None) -> int:
    """The id of this delivery, creating it if this is the first message to describe it.

    Never overwrites a fact already recorded. The first message to state a carrier is as good an
    authority on it as the second, and a later notice that happens to omit it must not blank what
    an earlier one supplied — an empty `carrier_name` on a receipt is indistinguishable from a
    delivery that genuinely had none.
    """
    row = conn.execute(
        "SELECT * FROM deliveries WHERE po_number = ? AND delivery_ref = ?",
        (po_number, delivery_ref)).fetchone()

    stated = {name: _get(facts, name) for name in _FIELDS} if facts is not None else {}
    stated = {name: value for name, value in stated.items()
              if value not in (None, "")}

    if row is None:
        columns = ["po_number", "delivery_ref", "delivery_rung", "created_at", *stated]
        values = [po_number, delivery_ref, delivery_rung, now, *stated.values()]
        placeholders = ", ".join("?" * len(columns))
        cursor = conn.execute(
            f"INSERT INTO deliveries ({', '.join(columns)}) VALUES ({placeholders})", values)
        conn.commit()
        return int(cursor.lastrowid)

    delivery_id = int(row["id"])
    missing = {name: value for name, value in stated.items()
               if _get(row, name) in (None, "")}
    if missing:
        assignments = ", ".join(f"{name} = ?" for name in missing)
        conn.execute(f"UPDATE deliveries SET {assignments}, updated_at = ? WHERE id = ?",
                     (*missing.values(), now, delivery_id))
        conn.commit()
    return delivery_id


def attach(conn: sqlite3.Connection, delivery_id: int, record_ids: Iterable[int]) -> int:
    """Point item lines at the delivery they arrived on. Returns how many were attached."""
    ids = [int(r) for r in record_ids]
    if not ids:
        return 0
    placeholders = ", ".join("?" * len(ids))
    cursor = conn.execute(
        f"UPDATE extracted_records SET delivery_id = ? WHERE id IN ({placeholders})",
        (delivery_id, *ids))
    conn.commit()
    return cursor.rowcount


def lines_for(conn: sqlite3.Connection, delivery_id: int) -> List[sqlite3.Row]:
    """This delivery's item lines, in the order they were staged."""
    prior = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM extracted_records WHERE delivery_id = ? ORDER BY id",
            (delivery_id,)).fetchall()
    finally:
        conn.row_factory = prior


def get(conn: sqlite3.Connection, delivery_id: int) -> Optional[sqlite3.Row]:
    prior = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
    finally:
        conn.row_factory = prior


def find(conn: sqlite3.Connection, po_number: str, delivery_ref: str) -> Optional[sqlite3.Row]:
    prior = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM deliveries WHERE po_number = ? AND delivery_ref = ?",
            (po_number, delivery_ref)).fetchone()
    finally:
        conn.row_factory = prior
