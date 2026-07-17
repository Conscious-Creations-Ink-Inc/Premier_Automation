import io
import re
from typing import List

import pdfplumber

from pipeline.models import ExtractedRecord
from pipeline.stage3_extract.base import (
    ExtractionAdapter,
    ExtractionSource,
    build_record_from_row,
    map_headers,
)
from pipeline.stage3_extract.freetext_adapter import FreetextAdapter

# A browser "print to PDF" of a scanned/photographed POD leaves behind its own print header/
# footer (a timestamp line, and a file:// path + page number line) as real vector text, even
# though the actual page content is a raster image. Found via real dummy test documents: every
# one of them had exactly this shape, and extract_text() returning that boilerplate made
# _has_text_layer wrongly report True, so PdfAdapter grabbed the browser chrome as if it were
# the document's real content instead of correctly routing to OcrAdapter.
_PRINT_CHROME_LINE_RE = re.compile(
    r"^\d{1,2}/\d{1,2}/\d{2,4},?\s+\d{1,2}:\d{2}\s*(AM|PM)$|^file:///\S+(\s+\d+/\d+)?$",
    re.IGNORECASE,
)


def _is_meaningful_text(text: str) -> bool:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    real_lines = [ln for ln in lines if not _PRINT_CHROME_LINE_RE.match(ln)]
    return bool(" ".join(real_lines).strip())


def _has_text_layer(content_bytes: bytes) -> bool:
    try:
        with pdfplumber.open(io.BytesIO(content_bytes)) as pdf:
            return any(_is_meaningful_text(page.extract_text() or "") for page in pdf.pages)
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
                    text = page.extract_text() or ""
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
