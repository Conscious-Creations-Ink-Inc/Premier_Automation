import sqlite3
from typing import List

from pipeline.models import ExtractedRecord, ExtractedRecordRow

_COLUMNS = (
    "source_email_id", "po_number", "shipment_number", "spec_code", "parent_spec_code",
    "sub_spec_suffix", "item_description", "vendor_name", "carrier_name", "tracking_number",
    "quantity_received", "unit_of_measure", "pod_stated_date", "email_date", "delivery_location",
    "comments", "extraction_source", "extraction_confidence", "raw_snippet",
)


def write_pending(conn: sqlite3.Connection, record: ExtractedRecord, now: str) -> int:
    """Inserts one ExtractedRecord as a 'pending' row — the Stage 3/4 hand-off. Returns the new row id."""
    values = tuple(getattr(record, col) for col in _COLUMNS)
    placeholders = ", ".join(["?"] * len(_COLUMNS))
    cursor = conn.execute(
        f"INSERT INTO extracted_records ({', '.join(_COLUMNS)}, status, created_at) "
        f"VALUES ({placeholders}, 'pending', ?)",
        values + (now,),
    )
    conn.commit()
    return cursor.lastrowid


def get_pending(conn: sqlite3.Connection) -> List[ExtractedRecordRow]:
    """Everything Stage 4 hasn't handled yet, oldest first."""
    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM extracted_records WHERE status = 'pending' ORDER BY id").fetchall()
        return [_row_to_extracted_record_row(row) for row in rows]
    finally:
        conn.row_factory = prior_factory


def mark_matched(conn: sqlite3.Connection, record_id: int, now: str) -> None:
    conn.execute(
        "UPDATE extracted_records SET status = 'matched', updated_at = ? WHERE id = ?",
        (now, record_id),
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, record_id: int, reason: str, now: str) -> None:
    """Appends the failure reason rather than overwriting comments — Stage 3 may already have
    put something useful there (e.g. a Confirmed Y/N note), and losing it would hurt the
    exception queue's usefulness for the human reviewing it."""
    conn.execute(
        """UPDATE extracted_records SET
               status = 'failed',
               comments = CASE WHEN comments IS NULL OR comments = '' THEN ?
                               ELSE comments || ' | ' || ? END,
               updated_at = ?
           WHERE id = ?""",
        (reason, reason, now, record_id),
    )
    conn.commit()


def _row_to_extracted_record_row(row: sqlite3.Row) -> ExtractedRecordRow:
    record = ExtractedRecord(**{col: row[col] for col in _COLUMNS})
    return ExtractedRecordRow(
        id=row["id"], record=record, status=row["status"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )
