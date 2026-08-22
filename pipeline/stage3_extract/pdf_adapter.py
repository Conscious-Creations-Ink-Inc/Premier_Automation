"""Native, text-layer PDFs — carrier PODs and warehouse receipts.

Order of attempts, most specific first:

1. **Carrier POD grammar.** A POD is a labelled form, not a table; running `extract_tables` over
   one yields nothing and the text fallback misreads its reference line. The corpus POD says
   `Purchase Order 31457971,210634,49985 : 1`, where only the middle token is the PO — see
   `parsing/pod.py`.
2. **Tables**, for warehouse receipts and packing slips.
3. **Free text**, as a last resort.

Dispatch is by byte sniff plus a text-yield gate: a `.pdf` that is really a photograph has no
text layer and belongs to the OCR adapter, whatever its extension says.
"""

import io
from typing import List, Optional

import pdfplumber

from pipeline.models import ExtractedRecord
from pipeline.parsing import pod as pod_parser
from pipeline.parsing import sniff
from pipeline.stage3_extract.base import (
    ExtractionAdapter,
    ExtractionSource,
    build_record_from_row,
    is_meaningful_text,
    map_headers,
    strip_print_chrome,
)
from pipeline.stage3_extract.freetext_adapter import FreetextAdapter


def _page_texts(content_bytes: bytes) -> List[str]:
    try:
        with pdfplumber.open(io.BytesIO(content_bytes)) as pdf:
            return [page.extract_text() or "" for page in pdf.pages]
    except Exception:
        return []


def _has_text_layer(content_bytes: bytes) -> bool:
    return any(is_meaningful_text(text) for text in _page_texts(content_bytes))


def records_from_pod(document: pod_parser.PodDocument, source: ExtractionSource) -> List[ExtractedRecord]:
    """A POD proves *a delivery*, not *which lines* were on it — it names no spec and no
    quantity. So it emits one record per PO it references, carrying the evidence fields
    (date, carrier, tracking, signature) and leaving the line identity to Stage 4, which
    reconciles it against the notification for the same shipment."""
    # This attachment *is* the proof, whatever its file type — the grammar recognised it, and the
    # grammar is fed by a PDF text layer, by OCR over a photograph, and by text lifted out of a
    # .docx alike. The dispatcher writes this onto the ledger row; see `ExtractionSource.pod_document`.
    source.pod_document = document

    records = []
    tracking = document.tracking_numbers[0] if document.tracking_numbers else None
    for po_number in document.po_numbers or [""]:
        records.append(ExtractedRecord(
            source_email_id=source.source_email_id,
            po_number=po_number,
            shipment_number=None,
            spec_code=None,
            parent_spec_code=None,
            sub_spec_suffix=None,
            item_description=None,
            vendor_name=None,
            carrier_name=document.carrier_name,
            tracking_number=tracking,
            quantity_received=None,
            unit_of_measure=None,
            pod_stated_date=document.delivery_date,
            email_date=source.email_date,
            delivery_location=document.recipient,
            comments="; ".join(filter(None, [
                f"POD signed for by {document.signed_for_by}" if document.signed_for_by else None,
                f"service {document.service_type}" if document.service_type else None,
                f"other refs on POD: {', '.join(document.other_references)}" if document.other_references else None,
            ])) or None,
            extraction_source="pdf:carrier_pod",
            # Deliberately capped below a line-level match. A POD is strong evidence of delivery
            # and no evidence at all of which lines were on the truck.
            extraction_confidence=0.7 if (po_number and document.delivery_date) else 0.4,
            raw_snippet=document.raw_text[:1000],
            received_by=document.signed_for_by,
        ))
    return records


class PdfAdapter(ExtractionAdapter):
    def can_handle(self, source: ExtractionSource) -> bool:
        if source.source_type != "attachment" or not source.content_bytes:
            return False
        if sniff.sniff(source.content_bytes, source.filename or "", source.content_type or "").kind != sniff.KIND_PDF:
            return False
        return _has_text_layer(source.content_bytes)

    def extract(self, source: ExtractionSource) -> List[ExtractedRecord]:
        texts = _page_texts(source.content_bytes)
        whole = "\n".join(texts)

        document = pod_parser.parse_pod(whole)
        if document is not None and (document.po_numbers or document.delivery_date):
            return records_from_pod(document, source)

        records: List[ExtractedRecord] = []
        with pdfplumber.open(io.BytesIO(source.content_bytes)) as pdf:
            for page_index, page in enumerate(pdf.pages):
                found_table = False
                for table in page.extract_tables() or []:
                    if not table or len(table) < 2:
                        continue
                    header_cells = [c or "" for c in table[0]]
                    column_map = map_headers(header_cells)
                    if not column_map:
                        continue
                    found_table = True
                    for row in table[1:]:
                        records.append(build_record_from_row(source, [c or "" for c in row], column_map, "pdf"))

                if not found_table:
                    records.extend(self._records_from_text(source, texts[page_index] if page_index < len(texts) else ""))
        return records

    def _records_from_text(self, source: ExtractionSource, page_text: str) -> List[ExtractedRecord]:
        text = strip_print_chrome(page_text)
        if not text.strip():
            return []
        text_source = ExtractionSource(
            source_email_id=source.source_email_id, email_date=source.email_date,
            source_type="body", body_text=text,
        )
        records = FreetextAdapter().extract(text_source)
        for record in records:
            record.extraction_source = "pdf:text"
        return records
