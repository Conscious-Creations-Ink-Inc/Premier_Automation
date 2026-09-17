"""Evidence gathered from an email's attachments, before anything decides whether it matters.

The ordering this enables is the point. Triage used to run first and read only subject and body
text; attachments were opened much later, at extraction, and only for mail that had already
survived triage. Mail that was routed or hidden had its attachments opened *never*.

The corpus shows why that is the wrong way round. `Del to Property - used excel attachment to
track 2.msg` carries **no PO number in any body, at any hop** — every PO lives inside
`Property Receivers.xlsx`. Under the old order, triage guessed ("there is a spreadsheet on a
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

    pod_po_numbers: List[str] = field(default_factory=list)
    """Purchase orders a proof of delivery on this message actually names.

    Read back off the ledger verdict `stage3_extract` already wrote (`is_pod`, `pod_po_numbers`)
    rather than re-parsing anything, so the POD that identifies a delivery and the POD that ends up
    on its receipt cannot disagree — the same source `dedupe.pod_sha_for_email` and
    `record_completion` read.

    This is what precedence 0 rests on: a carrier proved these goods arrived, and no wording in the
    covering email can un-prove it. Everything *not* in this list is decided by what the thread
    says, which is a far weaker thing and is treated as such.
    """

    pod_ledger_ids: Dict[str, int] = field(default_factory=dict)
    """po number -> the ledger row of the proof, so a staged record can point at its evidence.

    Records used to carry a POD's date and signature with no link to the file they came from,
    which made "backed by proof" and "nobody has confirmed this" indistinguishable on the page.
    """

    @property
    def has_records(self) -> bool:
        return bool(self.records)

    @property
    def has_pod(self) -> bool:
        return bool(self.pod_po_numbers)

    def credentials_for(self, po_number: str) -> Dict[str, Optional[str]]:
        """Which of the receipt credentials this message supplies for one purchase order.

        Tested against the *evidence set*, never against a single file, because **no carrier POD in
        this mailbox carries them alone**. All ten in the corpus name a PO and a delivery date and
        a signature, and not one names a spec, a quantity or an item description — a FedEx POD
        proves *that* something arrived and who signed for it, never *what was inside*. The item
        credentials come from the Authority Delivered Notification travelling with it, and the two
        join on tracking number and weight.

        A complete set may be staged as a receipt. An incomplete one is still a delivery — but the
        record belongs in the `incomplete` queue naming what is absent, rather than being quietly
        completed from a request table, which is how a receipt for 196 yards came to be staged
        against a POD and a notice that both said 202.
        """
        found: Dict[str, Optional[str]] = {
            "po_number": po_number if po_number in self.pod_po_numbers else None,
            "spec_code": None, "item_description": None,
            "quantity": None, "delivery_date": None, "received_by": None,
        }
        for record in self.records:
            if record.po_number and record.po_number != po_number:
                continue
            found["po_number"] = found["po_number"] or record.po_number or None
            found["spec_code"] = found["spec_code"] or record.spec_code or record.parent_spec_code
            found["item_description"] = found["item_description"] or record.item_description
            if found["quantity"] is None and record.quantity_received is not None:
                found["quantity"] = record.quantity_received
            found["delivery_date"] = found["delivery_date"] or record.pod_stated_date
            found["received_by"] = found["received_by"] or record.received_by
        return found

    def missing_credentials(self, po_number: str) -> List[str]:
        """What a receipt for this PO still lacks. Empty means it may be staged."""
        found = self.credentials_for(po_number)
        missing = []
        if not found["po_number"]:
            missing.append("purchase order")
        if not (found["spec_code"] or found["item_description"]):
            missing.append("spec or item description")
        if found["quantity"] is None:
            missing.append("quantity")
        if not found["delivery_date"]:
            missing.append("delivery date")
        return missing

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


# --- one document, read twice -------------------------------------------------
#
# A delivery message routinely carries the same document more than once. Atlas attaches the
# receiving report its own system generated *and* a scan of the signed copy: on PO 212749 those are
# `ReportsTGD_WarehouseReceivingReport_2026-08-28_14-03-43.pdf` and `Sheraton.211373.WRR-17.pdf`.
# Across the stored parses the pattern holds — six messages carrying thirteen redundant copies,
# every one of them a generated report paired with its scan.
#
# Both were read and both staged records. The generated report yields four clean lines; the scan
# yields the same delivery through OCR, with the handwriting, the freight bill and the packing slip
# that are bound in behind it. A person then had to tell the good four from their mangled twins.
#
# So the copies are matched to each other and only the best read is offered as work.


@dataclass
class _DocumentRead:
    """What one attachment turned out to be, so it can be compared with its siblings."""
    ledger_id: Optional[int]
    filename: str
    key: Optional[tuple]
    fidelity: int
    records: List[ExtractedRecord]


_FIDELITY = (
    # Most trustworthy first. A text layer is what the document says; OCR is what a reader made of
    # a picture of it; a free-text pass is what a regex made of that.
    (("pdf:text", "ocr:", "freetext", "text"), 0),
    (("ocr",), 1),
)


def _fidelity_of(records: Sequence[ExtractedRecord]) -> int:
    """How good this read was, from what produced it. Higher wins."""
    if not records:
        return -1
    source = (records[0].extraction_source or "").split("+")[0]
    for prefixes, rank in _FIDELITY:
        if any(source == p or source.startswith(p) for p in prefixes):
            return rank
    return 2   # a native table read: pdf, html, docx, excel


_REPORT_NUMBER_RE = re.compile(r"(?:WRR|RR)\s*#?\s*-?\s*(\d{4,7})\s*-\s*(\d{1,3})", re.IGNORECASE)
"""A warehouse receiving report's own number — `RR 211373-17`, `WRR-21`, `#211964-2`.

Project, then the report's sequence within it. Both halves are required: the project alone is
shared by every delivery on the job, and keying on it would merge them all.
"""


def _report_number(text: str) -> Optional[str]:
    """The report number this document states, or None if it states none — **or several**.

    A page naming two different reports is not evidence that either one is this document, and
    merging on it would be a guess. Silence and ambiguity get the same answer: no key.
    """
    found = {f"{project}-{sequence}" for project, sequence in _REPORT_NUMBER_RE.findall(text or "")}
    return found.pop() if len(found) == 1 else None


def _document_key(source) -> Optional[tuple]:
    """What identifies this document independently of how it was read, or None.

    **First, the report's own number.** A receiving report is issued with one, both copies carry it,
    and it survives a bad scan far better than anything in the labelled bands: on the `RR 211373-21`
    pair the scan's OCR read neither a tracking number nor a delivery date, so the fallback below
    could not fire, and a scan restating one delivery as a single 173-unit row sat beside the
    printed report's five lines that sum to exactly 173.

    **Then the carrier's reference and the delivery date**, for documents that are not a numbered
    report — a carrier POD, a packing slip. Two copies agree on those even when one is read poorly
    (`3041310` and `# 3041310`, `8/14/2026` and `8/14/26`), because the digits and the normalised
    date survive the difference. Both halves are required: a key built on a date alone would merge
    two genuinely different deliveries that happened to arrive the same day.

    Measured over every stored parse, the report number takes duplicate detection from 7 messages
    and 14 redundant copies to 13 and 24, and pairs nothing the fallback did not already pair.
    Failing to spot a duplicate costs one extra row on a queue; merging two real deliveries costs
    Premier a receipt. That asymmetry is why both rules refuse rather than guess.
    """
    from pipeline.stage3_extract.base import harvest_document_fields

    report = _report_number(source.parsed_text or "")
    if report:
        return ("report", report)

    fields = harvest_document_fields(source.parsed_tables or [])
    tracking = re.sub(r"\D", "", fields.get("tracking_number", "") or "")
    date = tokens.normalize_date(fields.get("pod_stated_date", "") or "") or ""
    if not tracking or not date:
        return None
    return ("shipment", tracking, date)


def supersede_duplicate_documents(reads: Sequence[_DocumentRead]) -> int:
    """Where several attachments are the same document, keep the best read's records as work and
    mark the rest superseded. Returns how many records were superseded.

    Nothing is deleted and nothing is merged. The superseded records keep every field they were
    read with, and the file they came from keeps its ledger row, its stored parse and its standing
    as a possible proof of delivery — it is only that a person is not asked to work two copies of
    one delivery.

    Ties keep every read. If two attachments are the same document and were read equally well,
    there is no reason here to prefer one, and choosing anyway would be a guess.
    """
    by_key: Dict[tuple, List[_DocumentRead]] = {}
    for read in reads:
        if read.key is not None and read.records:
            by_key.setdefault(read.key, []).append(read)

    superseded = 0
    for key, group in by_key.items():
        if len(group) < 2:
            continue
        best = max(group, key=lambda r: r.fidelity)
        if sum(1 for r in group if r.fidelity == best.fidelity) > 1:
            _logger.info("two equally good reads of the same document (%s); keeping both", key)
            continue
        for read in group:
            if read is best:
                continue
            for record in read.records:
                record.superseded_by_ledger_id = best.ledger_id
                record.comments = "; ".join(part for part in (
                    record.comments,
                    f"superseded by {best.filename}, a more accurate copy of the same document",
                ) if part)
                superseded += 1
            _logger.info("%s supersedes %s (%d record(s)) — same document, better read",
                         best.filename, read.filename, len(read.records))
    return superseded


def _known_po_numbers(conn) -> Optional[frozenset]:
    """Every purchase order the mirror holds, for `ExtractionSource.known_po_numbers`.

    Read once per message rather than per attachment; the index is a few hundred rows. `None` when
    it cannot be read at all, which leaves the adapters checking only the shape of a PO cell —
    exactly what they did before this existed.
    """
    if conn is None:
        return None
    try:
        from pipeline import spitfire_mirror
        return frozenset(spitfire_mirror.mirrored_po_numbers(conn))
    except Exception:                                              # noqa: BLE001
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

    known_pos = _known_po_numbers(conn)
    reads: List["_DocumentRead"] = []

    budget = containers.Budget.fresh()
    # Real attachments first, inline chrome last. Both are still read — people paste POD photos
    # into the body here, and 41 inline images have yielded records — but the OCR quota is finite
    # and shared, and whichever runs first gets it. Ordered the other way round (Outlook's own
    # order, signature block included), letterhead exhausted the tier's calls-per-minute and the
    # 429 it caused landed on the genuine documents queued behind it: a price-list PDF, and the
    # photograph that is the only evidence for a quantity conflict on PO 914711.
    for attachment in sorted(email.attachments, key=lambda a: bool(getattr(a, "is_inline", False))):
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
            known_po_numbers=known_pos,
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
        reads.append(_DocumentRead(
            ledger_id=attachment.ledger_id,
            filename=attachment.filename or "",
            key=_document_key(source),
            fidelity=_fidelity_of(produced),
            records=produced,
        ))

    supersede_duplicate_documents(reads)

    _read_pod_verdicts(conn, evidence)
    _summarise(evidence)
    return evidence


def _read_pod_verdicts(conn, evidence: EmailEvidence) -> None:
    """Pull this message's proof-of-delivery verdicts back off the ledger.

    Read after the dispatch loop, not during it: the adapters write `is_pod`/`pod_po_numbers` as
    they close each row, so by this point every attachment on the message — including ones expanded
    out of a container four levels down — has had its say.

    Inline attachments are excluded, as everywhere else. A signature logo is not proof of anything.
    """
    if conn is None:
        return
    prior = getattr(conn, "row_factory", None)
    try:
        conn.row_factory = None
        rows = conn.execute(
            "SELECT id, COALESCE(pod_po_numbers, '') FROM attachment_ledger "
            "WHERE email_id = ? AND COALESCE(is_pod, 0) = 1 AND COALESCE(is_inline, 0) = 0 "
            "ORDER BY depth, ordinal",
            (evidence.email_id,)).fetchall()
    except Exception as e:
        _logger.warning("could not read POD verdicts for %s: %s", evidence.email_id, e)
        return
    finally:
        try:
            conn.row_factory = prior
        except Exception:
            pass

    for ledger_id, po_csv in rows:
        for po_number in str(po_csv or "").split(","):
            po_number = po_number.strip()
            # Shape-checked for the same reason `_summarise` checks it: this list decides whether
            # goods may be staged as received, so it must not be poisonable by a stray number an
            # adapter happened to read out of an address block.
            if not tokens.is_po_number(po_number):
                continue
            if po_number not in evidence.pod_po_numbers:
                evidence.pod_po_numbers.append(po_number)
            evidence.pod_ledger_ids.setdefault(po_number, ledger_id)


def _summarise(evidence: EmailEvidence) -> None:
    """Roll the attachment records up into the PO and spec lists triage reads.

    `po_numbers` is shape-checked here because this is where a value stops being *something an
    adapter read out of a cell* and becomes *a key triage will accumulate a delivery against*. A
    generic table adapter hands back whatever was in the column it mapped to `po_number`, and on an
    unrecognised document that is an address block — see `tokens.is_po_number`. The records keep
    what they hold; only the key list is filtered.
    """
    for record in evidence.records:
        if (tokens.is_po_number(record.po_number)
                and record.po_number not in evidence.po_numbers):
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
