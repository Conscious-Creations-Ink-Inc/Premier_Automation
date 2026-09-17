import sqlite3
from typing import List, Mapping, Optional

from pipeline.models import ExtractedRecord, ExtractedRecordRow

_COLUMNS = (
    "source_email_id", "po_number", "shipment_number", "spec_code", "parent_spec_code",
    "sub_spec_suffix", "item_description", "vendor_name", "carrier_name", "tracking_number",
    "quantity_received", "unit_of_measure", "pod_stated_date", "email_date", "delivery_location",
    "comments", "extraction_source", "extraction_confidence", "raw_snippet",
    "po_line_number", "received_by", "package_quantity", "package_uom", "notification_number",
    "quantity_ordered",
    # Which attachment the record was read from. Stored so the document can be attached to the
    # receipt as its proof when no carrier POD resolves — see `spitfire_post._pod_for`.
    "source_ledger_id",
)
# Every field on ExtractedRecord must appear here — a field the record carries but the store
# does not is silently lost between Stage 3 and Stage 4. `test_models.py` asserts the two stay
# in step so adding a model field without a column fails loudly instead of quietly.

_NOT_PERSISTED = frozenset({
    # A model field is either stored, or listed here with the reason it is not — the parity check
    # in `test_models.py` holds either way.
    #
    # Set when a more accurate copy of the same document superseded this record. It is written to
    # the row's `status`, not to a column of its own.
    "superseded_by_ledger_id",
})
"""Model fields deliberately kept in memory. Adding to this set is a decision, not a shortcut."""


# Row metadata a caller may set at insert time. These are *not* extraction facts and so are
# deliberately absent from `ExtractedRecord` — they say where the row came from and what a person
# decided about it, not what the email said. `test_models.py` pins the model against `_COLUMNS`
# above, and adding them there would make it demand fields no adapter can produce.
_EXTRA_COLUMNS = frozenset({
    "origin", "created_by", "manual_note",
    "pod_ledger_id", "pod_source", "pod_waived_by", "pod_waived_at",
    "delivery_key",
    # Which delivery this item line arrived on — `deliveries.id`. Row metadata rather than a field
    # of `ExtractedRecord`, for the same reason `origin` is: an adapter reads an item off a
    # document and cannot know which delivery the orchestrator will attribute it to.
    "delivery_id",
})


SUPERSEDED = "superseded"
"""Staged, kept, and not offered as work.

A message routinely carries the same document twice — Atlas sends the receiving report its system
generated *and* a scan of the signed copy, and both are read. The scan's read is the poorer one:
handwriting, checkbox tokens, the freight bill and packing slip bound in behind it. Both used to
stage records, so a clean four-line parse arrived on the page beside a set of mangled twins and a
person had to tell them apart by eye.

The lower-fidelity records are not deleted — they are evidence that a second document said
something, and the file behind them may still be the proof of delivery. They are staged with this
status, which keeps them out of the manual queue and out of the ready set, and their `comments`
name the copy that superseded them.
"""


def write_pending(conn: sqlite3.Connection, record: ExtractedRecord, now: str,
                  extra: Optional[Mapping[str, object]] = None,
                  status: str = "pending") -> int:
    """Inserts one ExtractedRecord as a 'pending' row — the Stage 3/4 hand-off. Returns the new row id.

    `status` is almost always the default. `SUPERSEDED` is the exception: a record read from a
    less accurate copy of a document another attachment on the same message states better.

    `extra` carries row metadata alongside the record's own fields, in exactly the way `status` and
    `created_at` already are: written here, not modelled on `ExtractedRecord`. An unknown key is a
    programming error and raises rather than being dropped, because a provenance field that
    silently fails to persist is invisible until somebody asks who created a record and nothing
    answers.
    """
    columns = list(_COLUMNS)
    values = [getattr(record, col) for col in _COLUMNS]
    for key, value in (extra or {}).items():
        if key not in _EXTRA_COLUMNS:
            raise ValueError(f"{key!r} is not a settable row-metadata column "
                             f"(expected one of {sorted(_EXTRA_COLUMNS)})")
        columns.append(key)
        values.append(value)

    placeholders = ", ".join(["?"] * len(columns))
    cursor = conn.execute(
        f"INSERT INTO extracted_records ({', '.join(columns)}, status, created_at) "
        f"VALUES ({placeholders}, ?, ?)",
        tuple(values) + (status, now),
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
