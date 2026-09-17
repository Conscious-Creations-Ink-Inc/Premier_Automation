"""Read/update access to `extracted_records`.

`pipeline.extracted_records_store` owns the writes the pipeline needs (`write_pending`,
`mark_matched`, `mark_failed`) and exposes only `get_pending` for reading. The dashboard needs
to read one record by id, list with filters, and let a reviewer correct a field before
approving — that lives here so the pipeline module stays untouched.

The column tuple is imported rather than re-declared: it is the same physical table, and two
copies of the list would eventually drift.
"""
import sqlite3
from typing import Dict, List, Optional, Sequence

from pipeline.extracted_records_store import _COLUMNS as RECORD_COLUMNS
from pipeline.models import ExtractedRecord, ExtractedRecordRow

# What a reviewer is allowed to correct. Provenance fields (extraction_source, raw_snippet,
# confidence) are deliberately not editable — they record what the machine actually saw.
#
# Two fields are absent on purpose, and both absences are load-bearing:
#
# `po_number` — changing which purchase order a delivery is against would silently move a receipt
# onto another budget line. That is a different act from correcting a spec code, not a larger one.
#
# `po_line_number` — a text box here would be worse than useless. `post_decision` gate 5 refuses a
# line matched on description alone *unless a person chose it*, and `po_verify` treats a record's
# stored line number as exactly that choice (`chosen_line`, `reviewer_chose=True`). So a typo in
# this box would not be caught by the gate that exists to catch it — it would satisfy it. The
# sanctioned route is the alternatives table in the Verify popup, which shows the reviewer each
# line's description and outstanding quantity before they pick one.
EDITABLE_FIELDS = (
    # What arrived.
    "spec_code", "item_description", "quantity_received", "unit_of_measure",
    "package_quantity", "package_uom",
    # How and when it arrived.
    "pod_stated_date", "carrier_name", "tracking_number", "received_by",
    # Who it came from, and under what reference.
    "vendor_name", "notification_number",
)


def get(conn: sqlite3.Connection, record_id: int) -> Optional[ExtractedRecordRow]:
    rows = _query(conn, "SELECT * FROM extracted_records WHERE id = ?", (record_id,))
    return rows[0] if rows else None


def list_all(
    conn: sqlite3.Connection, status: Optional[str] = None, search: Optional[str] = None
) -> List[ExtractedRecordRow]:
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if search:
        clauses.append("(po_number LIKE ? OR spec_code LIKE ? OR item_description LIKE ?)")
        params.extend([f"%{search}%"] * 3)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return _query(conn, f"SELECT * FROM extracted_records{where} ORDER BY id", tuple(params))


def counts_by_status(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) FROM extracted_records GROUP BY status").fetchall()
    return {row[0]: row[1] for row in rows}


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM extracted_records").fetchone()[0]


def update_fields(conn: sqlite3.Connection, record_id: int, fields: Dict, now: str) -> int:
    """Applies a reviewer's corrections. Unknown or non-editable keys are ignored rather than
    raising, so a stale client can never write somewhere it shouldn't. Returns how many fields
    were actually written."""
    updates = {key: value for key, value in (fields or {}).items() if key in EDITABLE_FIELDS}
    if not updates:
        return 0
    assignments = ", ".join(f"{column} = ?" for column in updates)
    conn.execute(
        f"UPDATE extracted_records SET {assignments}, updated_at = ? WHERE id = ?",
        tuple(updates.values()) + (now, record_id),
    )
    conn.commit()
    return len(updates)


def _query(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> List[ExtractedRecordRow]:
    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return [
            ExtractedRecordRow(
                id=row["id"],
                record=ExtractedRecord(**{column: row[column] for column in RECORD_COLUMNS}),
                status=row["status"], created_at=row["created_at"], updated_at=row["updated_at"],
            )
            for row in rows
        ]
    finally:
        conn.row_factory = prior_factory
