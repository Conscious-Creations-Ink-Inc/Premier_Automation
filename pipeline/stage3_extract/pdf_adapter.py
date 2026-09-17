"""Native, text-layer PDFs — carrier PODs and warehouse receipts.

Order of attempts, most specific first:

1. **Carrier POD grammar.** A POD is a labelled form, not a table; running `extract_tables` over
   one yields nothing and the text fallback misreads its reference line. The corpus POD says
   `Purchase Order 91457971,910634,99985 : 1`, where only the middle token is the PO — see
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
    apply_document_fields,
    build_record_from_row,
    is_item_row,
    kept_despite_a_misread_spec,
    find_header_row,
    harvest_document_fields,
    is_meaningful_text,
    map_headers,
    po_column_is_corroborated,
    states_a_line_item,
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
            source_ledger_id=source.ledger_id,
        ))
    return records


def _all_tables(content_bytes: bytes) -> list:
    """Every table on every page, kept whole.

    `extract` builds records only from tables whose headers `map_headers` recognises. The others
    are just as real — see `ExtractionSource.parsed_text` for what discarding them cost — so they
    are captured here before any of that filtering happens.
    """
    tables: list = []
    try:
        with pdfplumber.open(io.BytesIO(content_bytes)) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables() or []:
                    if table:
                        tables.append([[str(c or "") for c in row] for row in table])
    except Exception:                                              # noqa: BLE001
        # Capturing evidence must never cost us the extraction it was captured alongside.
        pass
    return tables


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
        source.parsed_text = whole
        source.parsed_tables = _all_tables(source.content_bytes)

        document = pod_parser.parse_pod(whole)
        if document is not None and (document.po_numbers or document.delivery_date):
            return records_from_pod(document, source)

        # Read the form's labelled bands once, before the item grid, so every line built below can
        # carry the delivery date and signer the document states for the shipment as a whole.
        document_fields = harvest_document_fields(source.parsed_tables or [])

        records: List[ExtractedRecord] = []
        with pdfplumber.open(io.BytesIO(source.content_bytes)) as pdf:
            for page_index, page in enumerate(pdf.pages):
                found_table = False
                for table in page.extract_tables() or []:
                    if not table or len(table) < 2:
                        continue
                    located = find_header_row(table)
                    if located is None:
                        continue
                    header_index, column_map = located
                    found_table = True
                    # Asked once of the whole grid, before any row is read: does this PO column
                    # hold purchase orders we recognise? If it does, the orders in it we do *not*
                    # recognise are orders nobody has pulled yet — not noise to be discarded.
                    po_column_verified = po_column_is_corroborated(
                        [[c or "" for c in r] for r in table[header_index + 1:]],
                        column_map, source.known_po_numbers)
                    for row in table[header_index + 1:]:
                        cells = [c or "" for c in row]
                        if not any(cells[i].strip() for i in column_map.values() if i < len(cells)):
                            continue
                        if not is_item_row(cells):
                            continue
                        record = build_record_from_row(source, cells, column_map, "pdf",
                                                       po_column_verified=po_column_verified)
                        # A form's item grid runs on past its last item. Atlas closes with
                        # "SEND: Please scan this document..." and "BOL is NOT attached." — prose
                        # that lands in the PO column, is correctly rejected as not a PO number,
                        # and leaves a record identifying nothing. One that names neither a PO nor
                        # a spec cannot ever become a receipt, so it is dropped here rather than
                        # queued for a person who has nothing to act on.
                        if not (record.po_number or record.spec_code
                                or kept_despite_a_misread_spec(record, cells, column_map)):
                            continue
                        # ...and it must say something about what arrived. A row naming a spec and
                        # nothing else is a reference, not a receipt — see `states_a_line_item`.
                        if not states_a_line_item(record):
                            continue
                        records.append(apply_document_fields(record, document_fields))

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
            ledger_id=source.ledger_id, known_po_numbers=source.known_po_numbers,
        )
        records = FreetextAdapter().extract(text_source)
        for record in records:
            record.extraction_source = "pdf:text"
        # A page with no grid — a cover sheet, an "ATTACHMENTS" page — yields a record naming
        # neither a PO nor a spec. `completeness` requires both, so it can never be finished; all
        # it does is occupy a line on somebody's manual queue.
        #
        # `FreetextAdapter` now applies the stronger test (`states_a_delivery`) before it returns
        # anything, which is what stops the four "ATTACHMENTS" pages of an Atlas receiving report
        # each yielding a record whose spec is the report's own number, `WRR-17`. This filter stays
        # as the belt to that braces: it is cheap, and it is the invariant the rest of the pipeline
        # relies on.
        return [r for r in records if r.po_number or r.spec_code]
