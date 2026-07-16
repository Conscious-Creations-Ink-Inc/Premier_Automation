import io
from typing import List, Optional

import openpyxl

from pipeline.models import ExtractedRecord
from pipeline.stage3_extract.base import ExtractionAdapter, ExtractionSource, build_record_from_row, map_headers

EXCEL_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _find_confirmed_column(header_cells: List[str]) -> Optional[int]:
    for idx, header in enumerate(header_cells):
        if "confirm" in header.lower():
            return idx
    return None


class ExcelAdapter(ExtractionAdapter):
    """Property-confirmation trackers arriving as populated .xlsx spreadsheets
    (discovery doc §7.1.3) — uses openpyxl, the lightweight choice over pandas."""

    def can_handle(self, source: ExtractionSource) -> bool:
        return (
            source.source_type == "attachment"
            and source.content_type == EXCEL_CONTENT_TYPE
            and source.content_bytes is not None
        )

    def extract(self, source: ExtractionSource) -> List[ExtractedRecord]:
        workbook = openpyxl.load_workbook(io.BytesIO(source.content_bytes), data_only=True)
        sheet = workbook.worksheets[0]
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            return []

        header_cells = [str(c) if c is not None else "" for c in rows[0]]
        column_map = map_headers(header_cells)
        if not column_map:
            return []

        confirmed_idx = _find_confirmed_column(header_cells)

        records = []
        for row in rows[1:]:
            cells = [str(c) if c is not None else "" for c in row]
            if not any(cells):
                continue
            record = build_record_from_row(source, cells, column_map, "excel")
            if confirmed_idx is not None and confirmed_idx < len(cells) and cells[confirmed_idx]:
                record.comments = f"Confirmed: {cells[confirmed_idx]}"
            records.append(record)
        return records
