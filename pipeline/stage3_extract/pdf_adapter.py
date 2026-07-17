import io
from typing import List

import pdfplumber

from pipeline.models import ExtractedRecord
from pipeline.stage3_extract.base import (
    ExtractionAdapter,
    ExtractionSource,
    build_record_from_row,
    is_meaningful_text,
    map_headers,
    strip_print_chrome,
)
from pipeline.stage3_extract.freetext_adapter import FreetextAdapter


def _has_text_layer(content_bytes: bytes) -> bool:
    try:
        with pdfplumber.open(io.BytesIO(content_bytes)) as pdf:
            return any(is_meaningful_text(page.extract_text() or "") for page in pdf.pages)
    except Exception:
        return False


class PdfAdapter(ExtractionAdapter):
    """Native, text-based PDFs — warehouse receipts and courier PODs. Scanned/photographed
    PDFs (no text layer) are explicitly out of scope here — that's OcrAdapter's job."""

    def can_handle(self, source: ExtractionSource) -> bool:
        return (
            source.source_type == "attachment"
            and source.content_type == "application/pdf"
            and source.content_bytes is not None
            and _has_text_layer(source.content_bytes)
        )

    def extract(self, source: ExtractionSource) -> List[ExtractedRecord]:
        records = []
        with pdfplumber.open(io.BytesIO(source.content_bytes)) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                found_table = False
                for table in tables:
                    if not table or len(table) < 2:
                        continue
                    header_cells = [c or "" for c in table[0]]
                    column_map = map_headers(header_cells)
                    if not column_map:
                        continue
                    found_table = True
                    for row in table[1:]:
                        cells = [c or "" for c in row]
                        records.append(build_record_from_row(source, cells, column_map, "pdf"))

                if not found_table:
                    text = strip_print_chrome(page.extract_text() or "")
                    if text.strip():
                        text_source = ExtractionSource(
                            source_email_id=source.source_email_id,
                            email_date=source.email_date,
                            source_type="body",
                            body_text=text,
                        )
                        for record in FreetextAdapter().extract(text_source):
                            record.extraction_source = "pdf"
                            records.append(record)
        return records
