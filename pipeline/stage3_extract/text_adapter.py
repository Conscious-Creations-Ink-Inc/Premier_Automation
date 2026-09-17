"""Plain-text attachments: `.csv`, `.tsv`, `.txt`, `.rtf`.

`KIND_TEXT` previously had no reader at all, which mattered most for **CSV** — the obvious
substitute for the `.xlsx` trackers Premier already relies on, and one export away from being
the format they actually send. A CSV of `Property Receivers.xlsx` holds identical data, so it
routes through the same `grid_reader` and yields identical records.

One rule carries disproportionate weight here, in `_freetext_records`: **a record with no PO,
no spec and no quantity is discarded rather than staged.** This adapter claims a broad kind, and
the orchestrator stops at the first adapter that returns anything, so without that guard a
two-line note attachment would stage an empty record — which `_belongs_to_event` then adopts and
stamps with the delivery's PO, inventing a receipt from nothing. The `FreetextAdapter` docstring
records that this exact failure has happened before.
"""

import csv
import io
import re
from typing import List, Optional

from pipeline.models import ExtractedRecord
from pipeline.parsing import sniff
from pipeline.stage3_extract import grid_reader
from pipeline.stage3_extract.base import ExtractionAdapter, ExtractionSource, apply_confidence_floor
from pipeline.stage3_extract.freetext_adapter import FreetextAdapter

# Tried in order. utf-8-sig first strips the BOM Excel writes on every CSV it exports.
_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")

_DELIMITED_SUFFIXES = (".csv", ".tsv", ".tab", ".txt")
_RTF_SUFFIXES = (".rtf",)


def decode(content_bytes: bytes) -> str:
    """Decode tolerantly. `latin-1` cannot fail, so this always returns something rather than
    letting an encoding surprise lose the attachment."""
    for encoding in _ENCODINGS:
        try:
            return content_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content_bytes.decode("latin-1", "replace")


def looks_delimited(text: str, filename: str = "") -> Optional[str]:
    """The delimiter if this is a delimited table, else None.

    Extension is only a hint — a tracker exported as `.txt` is still a table. The real test is
    that several consecutive lines split into the same number of fields, on the same character.
    """
    lines = [line for line in text.splitlines()[:20] if line.strip()]
    if len(lines) < 2:
        return None

    for delimiter in (",", "\t", ";", "|"):
        counts = [line.count(delimiter) for line in lines[:10]]
        if counts[0] >= 2 and len(set(counts)) == 1:
            return delimiter

    if filename.lower().endswith((".csv", ".tsv")):
        try:
            return csv.Sniffer().sniff(text[:4096]).delimiter
        except csv.Error:
            return None
    return None


def strip_rtf(text: str) -> str:
    """RTF to plain text. Uses RTFDE when available, else strips control words directly —
    enough to recover a PO number and a spec code, which is all we need from one."""
    try:
        from RTFDE.deencapsulate import DeEncapsulator
        parser = DeEncapsulator(text)
        parser.deencapsulate()
        return parser.text or parser.html or ""
    except Exception:
        pass
    without_groups = re.sub(r"\{\\\*?[^{}]*\}", " ", text)
    without_controls = re.sub(r"\\[a-zA-Z]+-?\d*\s?", " ", without_groups)
    return re.sub(r"[{}]", " ", without_controls)


class TextAdapter(ExtractionAdapter):
    def can_handle(self, source: ExtractionSource) -> bool:
        if source.source_type != "attachment" or not source.content_bytes:
            return False
        return sniff.sniff(source.content_bytes, source.filename or "",
                           source.content_type or "").kind == sniff.KIND_TEXT

    def extract(self, source: ExtractionSource) -> List[ExtractedRecord]:
        filename = source.filename or ""
        text = decode(source.content_bytes)

        if filename.lower().endswith(_RTF_SUFFIXES) or text.lstrip().startswith(r"{\rtf"):
            return self._freetext_records(source, strip_rtf(text), "rtf")

        delimiter = looks_delimited(text, filename)
        if delimiter:
            records = self._delimited_records(source, text, delimiter)
            if records:
                return records

        return self._freetext_records(source, text, "text")

    # --- passes ---------------------------------------------------------------

    def _delimited_records(self, source: ExtractionSource, text: str, delimiter: str) -> List[ExtractedRecord]:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows = [grid_reader.as_strings(row) for row in reader]
        return grid_reader.records_from_rows(source, rows, f"csv{delimiter!r}" if delimiter != "," else "csv")

    def _freetext_records(self, source: ExtractionSource, text: str, label: str) -> List[ExtractedRecord]:
        """Run the free-text grammar over an attachment's text.

        `FreetextAdapter` deliberately refuses to claim attachments, so it is invoked through a
        synthetic body source — the same pattern `PdfAdapter` and `DocxAdapter` already use for
        their own text fallbacks.
        """
        if not text.strip():
            return []
        body_source = ExtractionSource(
            source_email_id=source.source_email_id, email_date=source.email_date,
            source_type="body", body_text=text,
            ledger_id=source.ledger_id, known_po_numbers=source.known_po_numbers,
        )
        records = []
        for record in FreetextAdapter().extract(body_source):
            record.extraction_source = label
            apply_confidence_floor(record)
            # Zero confidence means no PO, no spec and no quantity were found — nothing worth
            # staging, and actively harmful if staged. See the module docstring.
            if record.extraction_confidence > 0.0:
                records.append(record)
        return records
