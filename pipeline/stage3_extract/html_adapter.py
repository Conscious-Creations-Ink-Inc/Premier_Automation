"""HTML bodies — Authority notifications first, then confirmation grids, then generic tables.

The Authority parser runs here rather than as a separate stage because a notification *is* an
HTML body, and putting it first means the richest format in the corpus is read by the parser
that knows its grammar instead of by the generic column-mapper, which finds no PO column in it
at all (there isn't one — the header reads `PO # / Line #`).

Nested `.msg` attachments reach this adapter too: the intake connector re-presents their bodies
as `text/html` attachments, so a Delivered Notification quoted inside a vendor thread is parsed
exactly like a directly-received one.
"""

from pathlib import Path
from typing import List, Optional

from pipeline.models import ExtractedRecord
from pipeline.parsing import confirmation, sniff, tables, text, thread
from pipeline.stage3_extract.base import (
    ExtractionAdapter,
    ExtractionSource,
    build_record_from_row,
    map_headers,
)
from pipeline.vendors import authority


class HtmlAdapter(ExtractionAdapter):
    def can_handle(self, source: ExtractionSource) -> bool:
        if source.source_type == "body":
            return bool(source.body_html or source.body_text)
        if source.source_type == "attachment" and source.content_bytes:
            return sniff.sniff(source.content_bytes, source.filename or "",
                               source.content_type or "").kind == sniff.KIND_HTML
        return False

    def extract(self, source: ExtractionSource) -> List[ExtractedRecord]:
        html = self._html_of(source)
        body = source.body_text or text.html_to_text(html)

        records = self._authority_records(source, html, body)
        if records:
            return records

        records = self._confirmation_records(source, html)
        if records:
            return records

        return self._generic_table_records(source, html)

    # --- passes ---------------------------------------------------------------

    def _html_of(self, source: ExtractionSource) -> Optional[str]:
        if source.source_type == "attachment" and source.content_bytes:
            return source.content_bytes.decode("utf-8", "replace")
        return source.body_html

    def _authority_records(self, source: ExtractionSource, html: Optional[str], body: str) -> List[ExtractedRecord]:
        """Recover the originating sender and subject from the quoted chain before asking the
        Authority parser anything — the notification is usually two hops down inside a forward,
        where the envelope sender is an internal expeditor."""
        parsed = thread.split_thread(body, source.sender_address or "", source.subject or "")
        origin = thread.resolve_origin(source.sender_address or "", source.subject or "", parsed)

        sender = origin.sender_address or source.sender_address or ""
        subject = origin.subject or source.subject or ""
        if source.source_type == "attachment" and source.filename:
            # A nested message is re-presented as `<its subject>.html`, and its own subject is
            # the only thing identifying it — the enclosing email's sender is somebody else.
            subject = Path(source.filename).stem or subject

        # Asked of the whole chain, not just the resolved origin: the subject a forwarder typed
        # is not evidence about what the notice is, and triage now reads it the same way.
        notice = authority.notice_in_thread(sender, subject, html, body, parsed)
        if notice is None:
            return []
        return authority.records_from_notice(
            notice, source.source_email_id, source.email_date, only_po=source.only_po,
        )

    def _confirmation_records(self, source: ExtractionSource, html: Optional[str]) -> List[ExtractedRecord]:
        """Request/tracker grids quoted anywhere in the thread.

        Every hop re-quotes the table, so the same grid appears many times over; records are
        deduped on (PO, spec, quantity) with the first — newest — copy kept, since that is the
        one carrying any answers the recipient filled in.
        """
        records: List[ExtractedRecord] = []
        seen = set()
        for grid in tables.extract_tables(html):
            if len(grid.rows) < 2 or not confirmation.is_confirmation_grid(grid.header):
                continue
            for record in confirmation.records_from_grid(
                grid.header, grid.body_rows, source.source_email_id, source.email_date,
                "html:confirmation_grid",
            ):
                key = (record.po_number, record.spec_code, record.quantity_received, record.item_description)
                if key in seen:
                    continue
                seen.add(key)
                records.append(record)
        return records

    def _generic_table_records(self, source: ExtractionSource, html: Optional[str]) -> List[ExtractedRecord]:
        records: List[ExtractedRecord] = []
        for grid in tables.extract_tables(html):
            if len(grid.rows) < 2:
                continue
            column_map = map_headers(grid.header)
            if not column_map:
                continue
            for row in grid.body_rows:
                if any(cell for cell in row):
                    records.append(build_record_from_row(source, list(row), column_map, "html"))
        return records
