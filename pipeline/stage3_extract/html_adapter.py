from typing import List

from bs4 import BeautifulSoup

from pipeline.models import ExtractedRecord
from pipeline.stage3_extract.base import (
    ExtractionAdapter,
    ExtractionSource,
    build_record_from_row,
    map_headers,
)


class HtmlAdapter(ExtractionAdapter):
    """Warehouse-inbound emails that carry PO/spec/qty directly in a structured HTML table."""

    def can_handle(self, source: ExtractionSource) -> bool:
        return source.source_type == "body" and bool(source.body_html)

    def extract(self, source: ExtractionSource) -> List[ExtractedRecord]:
        soup = BeautifulSoup(source.body_html, "lxml")
        records = []
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if not rows:
                continue
            header_cells = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
            column_map = map_headers(header_cells)
            if not column_map:
                continue  # not a recognizable PO/spec/qty table — leave it for FreetextAdapter
            for row in rows[1:]:
                cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
                if not cells:
                    continue
                records.append(build_record_from_row(source, cells, column_map, "html"))
        return records
