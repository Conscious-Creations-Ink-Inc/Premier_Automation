"""Evidence gathered from an email's attachments, before anything decides whether it matters.

The ordering this enables is the point. Triage used to run first and read only subject and body
text; attachments were opened much later, at extraction, and only for mail that had already
survived triage. Mail that was routed or hidden had its attachments opened *never*.

The corpus shows why that is the wrong way round. `Del to Property - used excel attachment to
track 2.msg` carries **no PO number in any body, at any hop** — every PO lives inside
`Cameo Receivers.xlsx`. Under the old order, triage guessed ("there is a spreadsheet on a
delivery thread, hold it") and happened to be right. Read the sheet first and the same decision
rests on 17 extracted lines instead of a guess, and the ones where guessing would have been
*wrong* stop being invisible.

Parsing is not repeated later: the bundle is cached per email and handed to extraction when the
delivery event releases, so this moves work earlier rather than adding it.

**Cost control.** Reading every attachment on every marketing email would mean an OCR bill for
nothing. Three tiers:

* Tier 0 — sniff, hash and ledger. Always, for everything. No external calls, no parsing.
* Tier 1 — local parsing (xlsx, csv, xls, pdf text layer, docx, html, msg, zip). Always, unless
  the mail is certain noise.
* Tier 2 — OCR, which costs money per call. Only when tier 1 found no PO at all *and* the mail
  is not certain noise.

"Certain noise" is deliberately a very short list — the two shapes already proven in the corpus
to be non-deliveries — because the cost of being wrong is a missed receipt.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from pipeline.models import ExtractedRecord, RawEmail
from pipeline.parsing import sniff, thread, tokens

_logger = logging.getLogger(__name__)

# Shapes that are certainly not a delivery, so their attachments need no parsing beyond tier 0.
# Both are verified against the corpus: Authority's weekly summary comes from the same address
# as the receiver trigger, and carrier status mail is an intermediate movement notice.
_STATUS_REPORT_RE = re.compile(r"purchase\s+order\s+status\s+report", re.IGNORECASE)
_CARRIER_STATUS_RE = re.compile(
    r"\b(?:out\s+for\s+delivery|shipment\s+(?:created|picked\s+up)|tracking\s+update|"
    r"your\s+package\s+(?:has\s+)?(?:shipped|is\s+on\s+its\s+way))\b",
    re.IGNORECASE,
)


@dataclass
class EmailEvidence:
    """What the attachments on one email actually contained."""

    email_id: str
    records: List[ExtractedRecord] = field(default_factory=list)
    po_numbers: List[str] = field(default_factory=list)
    spec_codes: List[str] = field(default_factory=list)
    parsed_attachments: int = 0
    ocr_attempted: int = 0
    skipped_reason: Optional[str] = None

    @property
    def has_records(self) -> bool:
        return bool(self.records)

    def records_for_po(self, po_number: str) -> List[ExtractedRecord]:
        return [r for r in self.records if not r.po_number or r.po_number == po_number]


def is_certain_noise(email: RawEmail, body_text: str) -> Optional[str]:
    """A reason string when this mail certainly is not a delivery, else None.

    Kept narrow on purpose. Anything that merely *looks* uninteresting is still parsed — a
    missed receipt costs Premier far more than a wasted parse.
    """
    subject = thread.strip_forward_prefixes(email.subject or "")
    if _STATUS_REPORT_RE.search(subject):
        return "Authority Purchase Order Status Report — a periodic summary, never a delivery"
    if _CARRIER_STATUS_RE.search(f"{subject}\n{body_text[:2000]}"):
        return "carrier movement notice — not a receiving event"
    return None


def gather(
    conn,
    email: RawEmail,
    body_text: str,
    adapters: Sequence,
    *,
    now: str,
    allow_ocr: bool = True,
) -> EmailEvidence:
    """Parse every attachment on `email` and summarise what they held.

    Runs before triage, so the ledger already holds a row per attachment (`observe`) and this
    fills in each one's outcome. Never raises: one unreadable attachment must not stop the mail
    being triaged, and the failure is recorded against its ledger row either way.
    """
    from pipeline.stage3_extract import containers, dispatch
    from pipeline.stage3_extract.base import ExtractionSource

    evidence = EmailEvidence(email_id=email.email_id)

    noise_reason = is_certain_noise(email, body_text)
    if noise_reason:
        evidence.skipped_reason = noise_reason
        return evidence

    budget = containers.Budget.fresh()
    for attachment in email.attachments:
        if attachment.drop_hint:
            continue   # already closed in the ledger at ingest

        if attachment.sniffed_kind == sniff.KIND_IMAGE and not allow_ocr:
            continue

        source = ExtractionSource(
            source_email_id=email.email_id,
            email_date=email.received_at,
            source_type="attachment",
            filename=attachment.filename,
            content_type=attachment.content_type,
            content_bytes=attachment.content_bytes,
            sender_address=email.sender_address,
            subject=email.subject,
            ledger_id=attachment.ledger_id,
            container_path=attachment.container_path,
        )
        try:
            produced = dispatch.dispatch_source(
                conn, source, adapters, budget=budget, now=now,
            )
        except Exception as e:
            _logger.warning("evidence pass failed on %s (%s): %s",
                            email.email_id, attachment.filename, e, exc_info=True)
            continue

        evidence.parsed_attachments += 1
        if attachment.sniffed_kind == sniff.KIND_IMAGE:
            evidence.ocr_attempted += 1
        evidence.records.extend(produced)

    _summarise(evidence)
    return evidence


def _summarise(evidence: EmailEvidence) -> None:
    for record in evidence.records:
        if record.po_number and record.po_number not in evidence.po_numbers:
            evidence.po_numbers.append(record.po_number)
        spec = record.spec_code or record.parent_spec_code
        if spec and spec not in evidence.spec_codes:
            evidence.spec_codes.append(spec)


def po_numbers_from_text(text: str) -> List[str]:
    """Convenience for callers combining attachment evidence with body text."""
    return tokens.find_po_numbers(text or "")


class EvidenceCache:
    """Per-run store, so an email's attachments are parsed once and reused at release.

    An email is accumulated at ingest and extracted later, when its delivery event fires — often
    in the same pass. Without this the attachments would be parsed twice, and OCR would be
    charged twice.
    """

    def __init__(self):
        self._by_email: Dict[str, EmailEvidence] = {}

    def put(self, evidence: EmailEvidence) -> None:
        self._by_email[evidence.email_id] = evidence

    def get(self, email_id: str) -> Optional[EmailEvidence]:
        return self._by_email.get(email_id)

    def records_for(self, email_id: str, po_number: str) -> List[ExtractedRecord]:
        evidence = self._by_email.get(email_id)
        return evidence.records_for_po(po_number) if evidence else []

    def __len__(self) -> int:
        return len(self._by_email)
