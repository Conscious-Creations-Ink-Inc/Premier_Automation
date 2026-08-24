"""Formats we recognise and deliberately do not read.

"Every attachment is analysed" does not mean every attachment is parsed — it means every
attachment gets a type and a verdict, and no file is ever dropped without one. Four formats end
here, each for a stated reason:

* **`.doc`** (Word 97 binary) — no maintained pure-Python reader exists. Scraping printable runs
  out of the OLE `WordDocument` stream produces text littered with structure bytes, which
  `regex_extract_fields` would happily mine for spec codes and quantities. A fabricated receipt
  is far worse than an honest referral.
* **`.pptx`** — would need `python-pptx`; no deck appears in the corpus.
* **`.7z` / `.rar`** — would need a third-party library and, for RAR, a non-free binary.
* Anything the sniffer could not identify at all.

Claiming these rather than leaving them unclaimed is the point. An unclaimed source is
indistinguishable from a coverage gap we have not noticed; a claimed one carries a sentence
telling the operator what to do about it.
"""

from typing import List

from pipeline.models import ExtractedRecord
from pipeline.parsing import sniff
from pipeline.stage3_extract.base import ExtractionAdapter, ExtractionSource


class UnsupportedFormatError(NotImplementedError):
    """Raised with the operator-facing explanation. The dispatcher turns it into an
    `unsupported_format` ledger row rather than an error."""

    def __init__(self, kind: str, guidance: str):
        super().__init__(guidance)
        self.kind = kind
        self.guidance = guidance


GUIDANCE = {
    sniff.KIND_DOC: (
        "Word 97-2003 binary (.doc). No reliable reader is available — ask the sender to "
        "resave as .docx, or open it manually."
    ),
    sniff.KIND_PPTX: (
        "PowerPoint deck. Not read in this phase — open it manually if it carries delivery "
        "evidence."
    ),
    sniff.KIND_ARCHIVE_UNSUPPORTED: (
        "7-Zip or RAR archive. Only .zip is expanded — ask the sender to resend as a .zip, or "
        "extract it manually."
    ),
    sniff.KIND_ARCHIVE: (
        "tar/gzip archive. Not expanded in this phase — extract it manually if it carries "
        "delivery evidence."
    ),
    sniff.KIND_LEGACY_OFFICE: (
        "Legacy Office document we could not narrow to Word or Excel. Open it manually."
    ),
    sniff.KIND_ZIP_OFFICE: (
        "An OOXML-style archive that is neither a workbook nor a document. Open it manually."
    ),
    sniff.KIND_UNKNOWN: (
        "Unrecognised file format. The first bytes are recorded in the ledger — open it "
        "manually to identify it."
    ),
}

UNSUPPORTED_KINDS = frozenset(GUIDANCE)


class UnsupportedFormatAdapter(ExtractionAdapter):
    """Last in the cascade: claims what nothing else did, so it is recorded rather than lost."""

    def can_handle(self, source: ExtractionSource) -> bool:
        if source.source_type != "attachment" or not source.content_bytes:
            return False
        kind = sniff.sniff(source.content_bytes, source.filename or "",
                           source.content_type or "").kind
        return kind in UNSUPPORTED_KINDS

    def extract(self, source: ExtractionSource) -> List[ExtractedRecord]:
        result = sniff.sniff(source.content_bytes, source.filename or "", source.content_type or "")
        guidance = GUIDANCE.get(result.kind, GUIDANCE[sniff.KIND_UNKNOWN])
        if result.kind == sniff.KIND_UNKNOWN:
            head = (source.content_bytes or b"")[:16].hex()
            guidance = f"{guidance} First bytes: {head}"
        raise UnsupportedFormatError(result.kind, guidance)
