"""Spreadsheet trackers (corpus class C2).

When a property or vendor stops replying, Premier switches to a spreadsheet: one row per item,
with a confirmation column the recipient fills in. `Cameo Receivers.xlsx` (148 rows) and
`Public Space - Pending Receipt Confirmation Orders.xlsx` (92 rows) are the real ones, and
between them they carry more receivable lines than the rest of the corpus combined.

Dispatch is by byte sniff, not content type: the corpus tracker arrives with `mimetype=None`,
so the previous content-type equality check never fired on it (finding C13).
"""

import io
from typing import List

import openpyxl

from pipeline.models import ExtractedRecord
from pipeline.parsing import confirmation, sniff
from pipeline.stage3_extract.base import ExtractionAdapter, ExtractionSource, build_record_from_row, map_headers

EXCEL_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
"""Kept for callers building fixtures. Dispatch no longer compares against it — the real
trackers arrive with no content type at all."""

# A header row is rarely row 1 — trackers often open with a title or a blank spacer.
MAX_HEADER_SCAN_ROWS = 10


def _as_strings(row) -> List[str]:
    return [("" if value is None else str(value)).strip() for value in row]


class ExcelAdapter(ExtractionAdapter):
    def can_handle(self, source: ExtractionSource) -> bool:
        if source.source_type != "attachment" or not source.content_bytes:
            return False
        return sniff.sniff(source.content_bytes, source.filename or "", source.content_type or "").kind == sniff.KIND_XLSX

    def extract(self, source: ExtractionSource) -> List[ExtractedRecord]:
        workbook = openpyxl.load_workbook(io.BytesIO(source.content_bytes), data_only=True, read_only=True)
        records: List[ExtractedRecord] = []
        try:
            for sheet in workbook.worksheets:
                records.extend(self._extract_sheet(source, sheet))
        finally:
            workbook.close()
        return records

    def _extract_sheet(self, source: ExtractionSource, sheet) -> List[ExtractedRecord]:
        rows = [_as_strings(row) for row in sheet.iter_rows(values_only=True)]
        if not rows:
            return []

        header_index = self._find_header_row(rows)
        if header_index is None:
            return []

        header = rows[header_index]
        body = rows[header_index + 1:]
        extraction_source = f"excel:{sheet.title}"

        records = confirmation.records_from_grid(
            header, body, source.source_email_id, source.email_date, extraction_source,
        )
        if records:
            return records

        # Not a confirmation grid, but the generic column map recognised something — a vendor's
        # own packing list, say. Falls back to the shared row builder rather than returning
        # nothing.
        column_map = map_headers(header)
        if not column_map:
            return []
        return [build_record_from_row(source, row, column_map, extraction_source)
                for row in body if any(row)]

    def _find_header_row(self, rows: List[List[str]]) -> "int | None":
        for index, row in enumerate(rows[:MAX_HEADER_SCAN_ROWS]):
            if sum(1 for cell in row if cell) < 3:
                continue
            if confirmation.is_confirmation_grid(row) or map_headers(row):
                return index
        return None
