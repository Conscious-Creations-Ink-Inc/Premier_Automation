"""Rows-with-a-header-somewhere -> ExtractedRecords.

Extracted from `ExcelAdapter`, which had this inline, so the `.csv` and `.xls` readers share one
implementation instead of growing two near-copies that drift. Everything tabular in the corpus
converges here: the same tracker exists as `.xlsx` today and could arrive as a CSV export or a
legacy `.xls` tomorrow, and all three should yield identical records.

The cascade is the confirmation grid first (`parsing/confirmation.py`, which understands the
Vendor/PO#/Spec#/QTY/Confirmed vocabulary and the "property answered no" case), then the generic
column map as a fallback for a shape we don't recognise.
"""

from typing import List, Optional, Sequence

from pipeline.models import ExtractedRecord
from pipeline.parsing import confirmation
from pipeline.stage3_extract.base import ExtractionSource, build_record_from_row, map_headers

MAX_HEADER_SCAN_ROWS = 10
"""A header is rarely on row 1 — trackers routinely open with a title, a blank spacer, or a
merged banner cell."""

MIN_HEADER_CELLS = 3


def find_header_row(rows: Sequence[Sequence[str]], max_scan: int = MAX_HEADER_SCAN_ROWS) -> Optional[int]:
    """Index of the first row that looks like a header, or None."""
    for index, row in enumerate(rows[:max_scan]):
        if sum(1 for cell in row if cell) < MIN_HEADER_CELLS:
            continue
        if confirmation.is_confirmation_grid(row) or map_headers(list(row)):
            return index
    return None


def records_from_rows(
    source: ExtractionSource,
    rows: Sequence[Sequence[str]],
    extraction_source: str,
) -> List[ExtractedRecord]:
    """Locate the header, then read every row beneath it."""
    if not rows:
        return []

    header_index = find_header_row(rows)
    if header_index is None:
        return []

    header = list(rows[header_index])
    body = [list(row) for row in rows[header_index + 1:]]

    records = confirmation.records_from_grid(
        header, body, source.source_email_id, source.email_date, extraction_source,
    )
    if records:
        return records

    column_map = map_headers(header)
    if not column_map:
        return []
    return [
        build_record_from_row(source, row, column_map, extraction_source)
        for row in body if any(cell for cell in row)
    ]


def as_strings(row) -> List[str]:
    """Normalise a row of mixed cell types to trimmed strings. `None` becomes `""` rather than
    the string `"None"`, which would otherwise be read as a real value."""
    return [("" if value is None else str(value)).strip() for value in row]
