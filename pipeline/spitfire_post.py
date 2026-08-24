"""Post one verified delivery to Spitfire: a receipt, its POD, its report, and its links.

The order of the nine calls is not arbitrary. Each step is written to the ledger the moment it
succeeds, because this API offers no transaction and no way to ask afterwards what happened —
`ReceiptInProgressUnits` reads 0.0 against an unapproved receipt, so a half-finished post is
invisible from Spitfire's side and the ledger is the only place it can be seen.

    1  create the receipt      the PO link, via forBatch -> SubContract
    2  title it                immediately, so it is never anonymous in Premier's UI
    3  add the receipt line    quantity, UOM and cost code copied from the matched PO line
    4  upload the POD          -> fileKey, hash-verified against our own MD5
    5  attach the POD
    6  build + upload + attach the receiver report
    7  link the purchase order
    8  link its pay requests
    9  read everything back    the only proof any of it landed

**Nothing here routes.** Creating the receipt already stages six routees, three of them real
Premier employees at sequence 10 — Spitfire applies the configured chain on creation, before
anyone asks it to. `route/apply` would email them, and `connectors/spitfire_write.py` refuses it
by name. The receipt is left In Process for a human to approve, which is also why the purchase
order will go on reporting nothing received until they do.

**Failure is never silently retried.** A timeout that actually succeeded server-side would post a
second receipt: nothing in this API is idempotent, the catalog does not deduplicate identical
bytes, and re-attaching creates another row. So the first failure stops the chain, the ledger
records how far it got, and a person decides. `PARTIAL` means "a receipt exists on training and
is incomplete" — a state worth being able to name.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from config import settings
from connectors import spitfire_write
from connectors.spitfire_write import SpitfireSessionExpired, SpitfireWriteClient
from pipeline import (attachment_bytes, dedupe, delivery_status, post_decision, post_ledger,
                      receipt_log, report_pdf)

_logger = logging.getLogger(__name__)

PAY_REQUEST_DOC_TYPE = "5b0a71d8-ed55-455b-bb99-2ab7d3b7a1cf"
"""From Premier's own `czx_TPICreate_ReceiptDoc.sql`. Not from the API: every
`/api/configuration/*` endpoint that could list document types returns
`500 … not yet implemented (case 36629)`, read and write alike."""

TEST_MARKER = "CC-TEST"
"""While this runs against the training instance every artefact says so, in the title, where a
person opening it in Spitfire sees it before anything else. Removed when Premier moves us to
production — deliberately a constant here rather than a setting, so that removal is a code change
somebody reviews."""

_HTML_TAG = re.compile(r"<[^>]+>")

# A POD is chosen by what the file *says*, not by its extension — see `_pod_for`. Any kind can be
# one, provided something read it and recorded the verdict on the ledger (`is_pod`,
# `pod_po_numbers`). The only shortcut is that a PDF with no stored verdict is re-read here,
# because a text layer is free; an unparsed image needs OCR, which is a paid call belonging at
# ingest rather than on the path a reviewer is waiting on.
_PDF_KIND = "pdf"


@dataclass
class PostResult:
    """What happened, in the terms the dialog and the ledger both need."""
    ok: bool
    state: str
    message: str
    record_id: int = 0
    po_number: str = ""
    receipt_key: str = ""
    receipt_doc_no: str = ""
    pod_file_key: str = ""
    report_file_key: str = ""
    steps: List[str] = field(default_factory=list)
    """Each completed step, in order, so a partial post can say exactly where it stopped."""


def post_record(conn: sqlite3.Connection, row: Any, *,
                client: Optional[SpitfireWriteClient] = None,
                read_client_factory=None, actor: str = "") -> PostResult:
    """Both halves, POD first, in one call.

    Kept as the whole flow for callers that want it — the tests, and any future unattended path —
    while the UI drives `post_pod` and `post_report` separately so a person decides each. The order
    is not a preference: `post_report` refuses until a `POD_POSTED` attempt exists, so a report
    physically cannot reach Spitfire ahead of the proof it reports on, or without it.
    """
    first = post_pod(conn, row, client=client, read_client_factory=read_client_factory, actor=actor)
    if not first.ok:
        return first
    return post_report(conn, row, client=client, actor=actor)


def post_pod(conn: sqlite3.Connection, row: Any, *,
             client: Optional[SpitfireWriteClient] = None,
             read_client_factory=None, actor: str = "") -> PostResult:
    """Create the receipt and put the proof of delivery on it. Steps 1-5.

    Settles `POD_POSTED` rather than `POSTED`: the receipt is real and the POD is on it, and the
    report is a second decision somebody makes afterwards. That state blocks another POD post — the
    receipt already exists and a retry would build a second one beside it — and is what
    `post_report` looks for by name.

    Both clients are injectable so tests can drive the chain without a network: `client` writes,
    `read_client_factory` is handed to `po_verify` for the comparison.
    """
    record_id = int(_get(row, "id") or 0)
    po_number = str(_get(row, "po_number") or "").strip()

    # --- the POD, before anything else -----------------------------------------------------
    # Its hash is part of the idempotency key, so it has to be in hand before the claim, and a
    # record whose POD bytes were never stored must not create a receipt asserting delivery.
    pod = _pod_for(conn, row)
    pod_md5 = hashlib.md5(pod.content).hexdigest().upper() if pod else ""

    # What the ledger keys this delivery on. Identical to `pod_md5` whenever a POD exists, so every
    # row already in `spitfire_post` still matches; derived from the email that asserted the
    # delivery when one does not, so the duplicate guard is not blind on body-only traffic.
    evidence_key = pod_md5 or dedupe.evidence_key(body_text=_body_text_of(conn, row),
                                                  email_id=_get(row, "source_email_id"))

    decision = post_decision.decide(
        conn, row, pod_md5=pod_md5, client_factory=read_client_factory,
        evidence_key=evidence_key,
        # Only computed on the failure path; it costs a query and is never needed otherwise.
        pod_reason="" if pod else _pod_absence_reason(conn, row))
    if not decision.may_post:
        # Recorded, not merely returned. The dialog the reviewer closes used to be the only place
        # this reason existed, so the next person clicked Post to rediscover it — at the cost of a
        # live purchase-order read each time.
        post_ledger.record_refusal(
            conn, record_id=record_id, po_number=po_number, line_number=decision.line_number,
            pod_md5=evidence_key, reason=decision.reason, actor=actor)
        return PostResult(ok=False, state="flagged", message=decision.reason,
                          record_id=record_id, po_number=po_number)

    claim = post_ledger.claim(
        conn, record_id=record_id, po_number=po_number, line_number=decision.line_number,
        pod_md5=evidence_key, project_code=decision.project_code, quantity=decision.quantity,
        actor=actor)
    if claim is None:
        # Lost the race between deciding and claiming — two operators on the same record, or a
        # double submit that outran the disabled button. The UNIQUE index is what settles it.
        existing = post_ledger.find(
            conn, post_ledger.idempotency_key(record_id, po_number, decision.line_number,
                                              evidence_key))
        return PostResult(ok=False, state="flagged", record_id=record_id, po_number=po_number,
                          message=(post_decision._describe_existing(existing) if existing
                                   else "this delivery is already being posted"))

    key = claim.idempotency_key
    client = client or SpitfireWriteClient()
    steps: List[str] = []
    receipt_key = ""

    try:
        client.whoami()   # fails loudly on a lapsed ticket, before a document exists

        # --- 1-2. the receipt ---------------------------------------------------------------
        receipt_key = client.create_receipt(decision.project_code, po_number)
        steps.append(f"receipt created ({receipt_key[:8]}…)")
        post_ledger.record_receipt(conn, key, receipt_key=receipt_key)

        title = (f"{TEST_MARKER} - receiver automation - PO {po_number}"
                 f" - {date.today().isoformat()} - DO NOT PROCESS")
        client.set_title(receipt_key, title)
        steps.append("titled")

        header = client.read_header(receipt_key)
        doc_no = str(header.get("DocNo") or "")
        post_ledger.record_receipt(conn, key, receipt_key=receipt_key, receipt_doc_no=doc_no)
        if str(header.get("SubContract") or "").strip() != po_number:
            # forBatch is the only thing that ties a receipt to its purchase order. Without it the
            # document is an orphan, and adding lines and files to it would compound the problem.
            raise RuntimeError(
                f"the receipt was created but its SubContract reads "
                f"{header.get('SubContract')!r} instead of {po_number} — it is not linked to the "
                f"purchase order, so nothing further was attached")
        steps.append(f"linked to PO via SubContract (DocNo {doc_no or '?'})")

        # --- 3. the line ----------------------------------------------------------------------
        client.add_line(
            receipt_key,
            description=f"{TEST_MARKER} {_strip(decision.description) or decision.spec_code}",
            quantity=float(decision.quantity or 0),
            source_item_number=decision.spec_code or None,
            uom=decision.unit_of_measure or None,
            proj_entity=decision.cost_code or None)
        steps.append(f"line added ({post_decision.po_verify.fmt_qty(decision.quantity)} "
                     f"{decision.unit_of_measure})".rstrip())

        # --- 4-5. the POD, when there is one --------------------------------------------------
        # Skipped entirely for a delivery stated in the email body with nothing attached. Reaching
        # here at all means `post_decision` found a waiver from a named person, so the omission is
        # a decision somebody made rather than an oversight — it is named in the ledger detail and
        # in the result below, and the receiver report attached at step 6 still lands on this
        # receipt, so the document is not left bare.
        pod_key = ""
        if pod:
            pod_name = _safe_name(pod.filename, f"POD_{po_number}")
            pod_key = client.upload_file(pod.content, pod_name,
                                         keywords=f"{TEST_MARKER} POD {po_number}")
            post_ledger.record_file(conn, key, pod_file_key=pod_key)
            if not client.verify_upload(pod_key, pod_md5):
                raise RuntimeError(
                    f"the POD uploaded but the server's hash does not match ours — the file in the "
                    f"catalog ({pod_key}) is not the file we sent")
            steps.append(f"POD uploaded and hash-verified ({pod_name})")

            client.attach_file(receipt_key, pod_key, note=f"{TEST_MARKER} proof of delivery")
            steps.append("POD attached")

            # Read back before resting. A 200 on the attach is not evidence the row exists, and this
            # is now a state somebody may leave alone for a while — it must be true while they do.
            if pod_key.lower() not in _attachment_keys(client, receipt_key):
                raise RuntimeError(
                    "the POD was attached but reading the receipt back did not show it")
            steps.append("read back and confirmed")
        else:
            steps.append(f"no proof of delivery — waived by {decision.pod_waived_by}")

        settled = (f"receipt {doc_no or receipt_key[:8]} — report not posted" if pod else
                   f"receipt {doc_no or receipt_key[:8]} — no POD, waived by "
                   f"{decision.pod_waived_by}; report not posted")
        post_ledger.settle(conn, key, post_ledger.POD_POSTED, settled, client.audit_rows())
        return PostResult(
            ok=True, state=post_ledger.POD_POSTED, record_id=record_id, po_number=po_number,
            message=((f"the proof of delivery is on receipt {doc_no or receipt_key[:8]}. "
                      f"The receiver report has not been posted yet.") if pod else
                     (f"receipt {doc_no or receipt_key[:8]} was created with no proof of delivery "
                      f"attached, as accepted by {decision.pod_waived_by}. The receiver report has "
                      f"not been posted yet.")),
            receipt_key=receipt_key, receipt_doc_no=doc_no, pod_file_key=pod_key, steps=steps)

    except SpitfireSessionExpired as exc:
        # Not a failure of the record. Settled as FAILED only when nothing was created, so the
        # same delivery can be posted again once a fresh cookie is in place.
        state = post_ledger.PARTIAL if receipt_key else post_ledger.FAILED
        post_ledger.settle(conn, key, state, str(exc), client.audit_rows())
        return PostResult(ok=False, state="session_expired", message=str(exc),
                          record_id=record_id, po_number=po_number, receipt_key=receipt_key,
                          steps=steps)
    except Exception as exc:                      # noqa: BLE001 — every failure must be recorded
        state = post_ledger.PARTIAL if receipt_key else post_ledger.FAILED
        post_ledger.settle(conn, key, state, str(exc), client.audit_rows())
        _logger.exception("posting the POD for record %s to Spitfire failed", record_id)
        return PostResult(ok=False, state=state, message=str(exc), record_id=record_id,
                          po_number=po_number, receipt_key=receipt_key, steps=steps)


def post_report(conn: sqlite3.Connection, row: Any, *,
                client: Optional[SpitfireWriteClient] = None, actor: str = "") -> PostResult:
    """Build the receiver report and hang it on the receipt the POD already made. Steps 6-9.

    Refuses unless a `POD_POSTED` attempt exists for this record. That is the guarantee which
    survives splitting the chain: the report describes a delivery whose proof is already filed, and
    it can neither precede that proof nor stand in for it.

    `post_decision` is deliberately not consulted again. It ruled once, the receipt exists as a
    result, and asking a second time would let a quantity that moved in the meantime strand a
    receipt with a POD and no report — the very state this step exists to clear.
    """
    record_id = int(_get(row, "id") or 0)
    po_number = str(_get(row, "po_number") or "").strip()

    attempt = _awaiting_report_attempt(conn, record_id)
    if attempt is None:
        return PostResult(
            ok=False, state="flagged", record_id=record_id, po_number=po_number,
            message=("the proof of delivery has not been posted for this record, so there is no "
                     "receipt to put a report on — post the POD first"))

    key = attempt.idempotency_key
    receipt_key = attempt.receipt_key
    doc_no = attempt.receipt_doc_no
    client = client or SpitfireWriteClient()
    steps: List[str] = []

    try:
        client.whoami()

        # --- 6. the report ---------------------------------------------------------------------
        report_bytes = _build_report(
            conn, record_id, po_number=po_number,
            description=str(_get(row, "item_description") or ""),
            quantity=_get(row, "quantity_received"),
            unit_of_measure=str(_get(row, "unit_of_measure") or ""))
        report_name = _safe_name(f"Receiver_Report_PO{po_number}.pdf", "Receiver_Report.pdf")
        report_key = client.upload_file(
            report_bytes, report_name, keywords=f"{TEST_MARKER} receiver report {po_number}")
        post_ledger.record_file(conn, key, report_file_key=report_key)
        client.attach_file(receipt_key, report_key, note=f"{TEST_MARKER} receiver report")
        steps.append("report built, uploaded and attached")

        # --- 7-8. the document links -----------------------------------------------------------
        linked = _link_related(conn, client, receipt_key, po_number,
                               post_decision._project_of(conn, po_number))
        steps.extend(linked)

        # --- 9. read back ------------------------------------------------------------------------
        if not _confirm(client, receipt_key, attempt.pod_file_key, report_key):
            raise RuntimeError("the receipt was built but reading it back did not show the POD "
                               "and the report on it")
        steps.append("read back and confirmed")

        _mark_pushed(conn, record_id)
        post_ledger.settle(conn, key, post_ledger.POSTED,
                           f"receipt {doc_no or receipt_key[:8]}", client.audit_rows())
        return PostResult(
            ok=True, state=post_ledger.POSTED, record_id=record_id, po_number=po_number,
            message=f"posted to Spitfire as receipt {doc_no or receipt_key[:8]}",
            receipt_key=receipt_key, receipt_doc_no=doc_no,
            pod_file_key=attempt.pod_file_key, report_file_key=report_key, steps=steps)

    except SpitfireSessionExpired as exc:
        # The receipt and its POD are real and stay that way; only the report is outstanding, so
        # the attempt rests where it was rather than becoming PARTIAL. Re-posting is safe.
        post_ledger.settle(conn, key, post_ledger.POD_POSTED, str(exc), client.audit_rows())
        return PostResult(ok=False, state="session_expired", message=str(exc),
                          record_id=record_id, po_number=po_number, receipt_key=receipt_key,
                          steps=steps)
    except Exception as exc:                      # noqa: BLE001 — every failure must be recorded
        post_ledger.settle(conn, key, post_ledger.PARTIAL, str(exc), client.audit_rows())
        _logger.exception("posting the report for record %s to Spitfire failed", record_id)
        return PostResult(ok=False, state=post_ledger.PARTIAL, message=str(exc),
                          record_id=record_id, po_number=po_number, receipt_key=receipt_key,
                          steps=steps)


def verify_pod(conn: sqlite3.Connection, row: Any, *,
               client: Optional[SpitfireWriteClient] = None) -> PostResult:
    """Prove the POD in Spitfire is still the file we sent. Reads only.

    Two questions, and the second is the one a 200 never answered: *is it on the receipt*, and *are
    the bytes ours*. The first is `read_attachments`; the second re-compares the catalog's own
    `DataHash` against the MD5 of the file still in our store, which is the same check the upload
    made and the only one that can notice the file being replaced afterwards.
    """
    record_id = int(_get(row, "id") or 0)
    po_number = str(_get(row, "po_number") or "").strip()

    attempt = _posted_attempt(conn, record_id)
    if attempt is None:
        return PostResult(ok=False, state="flagged", record_id=record_id, po_number=po_number,
                          message="nothing has been posted to Spitfire for this record yet")

    pod = _pod_for(conn, row)
    if pod is None:
        return PostResult(
            ok=False, state="flagged", record_id=record_id, po_number=po_number,
            message="the proof of delivery is no longer in our store, so there is nothing to compare against")
    pod_md5 = hashlib.md5(pod.content).hexdigest().upper()

    client = client or SpitfireWriteClient()
    steps: List[str] = []
    receipt_label = attempt.receipt_doc_no or attempt.receipt_key[:8]
    try:
        client.whoami()
        present = attempt.pod_file_key.lower() in _attachment_keys(client, attempt.receipt_key)
        steps.append(f"POD {'found on' if present else 'MISSING from'} receipt {receipt_label}")
        if not present:
            return PostResult(ok=False, state="flagged", record_id=record_id, po_number=po_number,
                              message=f"the POD is not on receipt {receipt_label} in Spitfire",
                              receipt_key=attempt.receipt_key,
                              receipt_doc_no=attempt.receipt_doc_no, steps=steps)

        intact = client.verify_upload(attempt.pod_file_key, pod_md5)
        steps.append(f"catalog hash {'matches' if intact else 'DOES NOT MATCH'} ours ({pod_md5})")
        return PostResult(
            ok=intact, state="verified" if intact else "flagged",
            record_id=record_id, po_number=po_number,
            message=(f"the proof of delivery is on receipt {receipt_label}, and the bytes in "
                     f"Spitfire are the bytes we sent." if intact else
                     f"the POD is on receipt {receipt_label} but its hash no longer matches the "
                     f"file we sent."),
            receipt_key=attempt.receipt_key, receipt_doc_no=attempt.receipt_doc_no,
            pod_file_key=attempt.pod_file_key, report_file_key=attempt.report_file_key,
            steps=steps)
    except Exception as exc:                      # noqa: BLE001
        return PostResult(ok=False, state="failed", message=str(exc), record_id=record_id,
                          po_number=po_number, steps=steps)


def _awaiting_report_attempt(conn: sqlite3.Connection, record_id: int):
    """This record's receipt that is still waiting for its report, or None."""
    for attempt in post_ledger.existing_for_record(conn, record_id):
        if attempt.state == post_ledger.POD_POSTED and attempt.receipt_key:
            return attempt
    return None


def _posted_attempt(conn: sqlite3.Connection, record_id: int):
    """This record's receipt, report posted or not — whatever a POD can be verified against."""
    for attempt in post_ledger.existing_for_record(conn, record_id):
        if (attempt.state in (post_ledger.POD_POSTED, post_ledger.POSTED)
                and attempt.receipt_key and attempt.pod_file_key):
            return attempt
    return None


def _attachment_keys(client: SpitfireWriteClient, receipt_key: str) -> set:
    """The catalog keys actually on a receipt, lowercased for comparison."""
    return {str(a.get("DocKey") or "").lower() for a in client.read_attachments(receipt_key)}


# --- the pieces ---------------------------------------------------------------------------------

def _pod_for(conn: sqlite3.Connection, row: Any) -> Optional[attachment_bytes.ResolvedAttachment]:
    """The proof-of-delivery file for this record, chosen by what the file *says*.

    Inline attachments are excluded outright — signature logos and letterhead, not evidence.
    Beyond that the choice is made by reading each candidate, in this order:

        1. a PDF that parses as a proof of delivery **and names this record's purchase order**
        2. a PDF that parses as a proof of delivery naming no purchase order at all
        3. nothing

    It used to be "the first non-inline attachment of an acceptable kind, in ledger order", with
    `xlsx`, `msg`, `html` and `text` among the acceptable kinds. On an email carrying a tracker
    spreadsheet at ordinal 0 and the POD at ordinal 6 — the shape of the 210634 thread, which
    carries eight attachments — that uploaded **the spreadsheet to Premier's ERP as the proof of
    delivery**, and hashed it into the idempotency key so the duplicate guard keyed on the wrong
    document too. Position in a mail is not evidence of anything.

    A PDF naming a *different* purchase order is rejected rather than used as a fallback: on this
    corpus two byte-identical PDFs arrive under filenames citing different specs and tracking
    numbers, so the filename cannot be trusted and neither can proximity.

    Returning None is a real outcome, not an error. `post_decision` refuses a record with no POD
    unless a named person has waived it — a receipt asserting delivery with a spreadsheet attached
    as proof is worse than no receipt.

    **A reviewer's explicit choice outranks all of it.** `pod_ledger_id` names one attachment and
    that is the one used, whatever the file says about itself. Three cases need it and none can be
    reached by reading the files:

    * a photographed delivery note — the rules below re-read PDFs only, because OCR is a paid call
      that belongs at ingest, so a JPEG BOL the ingest-time pass never flagged is invisible here
    * a POD naming a *different* purchase order, which is rejected outright above and is exactly
      the case a person is there to overrule
    * a proof that is not a carrier POD at all — a signed packing slip, a photo of the pallet

    With `pod_ledger_id` null the order below is unchanged, so the 210634 tracker-spreadsheet bug
    cannot return through this door.
    """
    email_id = str(_get(row, "source_email_id") or "")
    if not email_id:
        return None
    po_number = str(_get(row, "po_number") or "").strip()

    chosen = _get(row, "pod_ledger_id")
    if chosen not in (None, ""):
        return _chosen_pod(conn, email_id, int(chosen))

    conn.row_factory = sqlite3.Row
    candidates = conn.execute(
        """SELECT ordinal, filename, sniffed_kind, disposition,
                  COALESCE(is_pod, 0) AS is_pod, COALESCE(pod_po_numbers, '') AS pod_po_numbers
             FROM attachment_ledger
            WHERE email_id = ? AND COALESCE(is_inline, 0) = 0
            ORDER BY CASE disposition WHEN 'extracted' THEN 0 ELSE 1 END, depth, ordinal""",
        (email_id,)).fetchall()

    unattributed = None
    for candidate in candidates:
        named = [p for p in str(candidate["pod_po_numbers"] or "").split(",") if p]
        if candidate["is_pod"]:
            verdict_pos = po_number and po_number in named
            verdict_unnamed = not named
        else:
            # No stored verdict. Either the row predates the column, or the adapter that read it
            # never reached the POD grammar. Re-read it here only when that is cheap — a PDF's
            # text layer is; OCR on a photograph is a paid call and belongs at ingest, not on the
            # path a reviewer waits on.
            if candidate["sniffed_kind"] != "pdf":
                continue
            resolved = attachment_bytes.resolve(conn, email_id, candidate["ordinal"],
                                                candidate["filename"] or "")
            if not (resolved and resolved.content):
                continue
            document = _read_pod(resolved.content)
            if document is None:
                continue
            named = document.po_numbers or []
            verdict_pos = po_number and po_number in named
            verdict_unnamed = not named

        if not (verdict_pos or verdict_unnamed):
            continue
        resolved = attachment_bytes.resolve(conn, email_id, candidate["ordinal"],
                                            candidate["filename"] or "")
        if not (resolved and resolved.content):
            continue
        if verdict_pos:
            return resolved
        if unattributed is None:
            # A POD naming no purchase order is still proof of *a* delivery, and the email it
            # arrived on is the only thing tying it to this record. Held back so an explicit
            # match on a later attachment always wins.
            unattributed = resolved
    return unattributed


def _body_text_of(conn: sqlite3.Connection, row: Any) -> str:
    """The delivery email's own text, for the evidence key when there is no POD to hash.

    Text rather than HTML: Outlook rewrites the markup on every hop, so the HTML of one message is
    not stable across a re-forward while its words are. Falls back to the subject if the body was
    never cached — thin, but the email id in the key carries the identity either way, and returning
    nothing at all would make two different body-only deliveries on one PO line hash alike.
    """
    email_id = str(_get(row, "source_email_id") or "")
    if not email_id:
        return ""
    conn.row_factory = sqlite3.Row
    cached = conn.execute(
        "SELECT subject, body_text, body_html FROM mail_body WHERE email_id = ?",
        (email_id,)).fetchone()
    if cached is None:
        return ""
    return str(cached["body_text"] or cached["body_html"] or cached["subject"] or "")


def _chosen_pod(conn: sqlite3.Connection,
                email_id: str, ledger_id: int) -> Optional[attachment_bytes.ResolvedAttachment]:
    """The attachment a reviewer picked, resolved to bytes.

    Scoped to the record's own email: a ledger id names a row in a table covering every message, and
    without the `email_id` guard a wrong or stale id could attach some other delivery's proof to
    this receipt. If the row does not belong to this message, or its bytes were never stored, this
    returns None and the record is refused for having no POD — which is the honest answer. It does
    **not** silently fall back to the automatic rules, because a person who chose a file and got a
    different one uploaded would have no way to know.
    """
    conn.row_factory = sqlite3.Row
    chosen = conn.execute(
        "SELECT ordinal, filename FROM attachment_ledger WHERE id = ? AND email_id = ?",
        (ledger_id, email_id)).fetchone()
    if chosen is None:
        return None
    resolved = attachment_bytes.resolve(conn, email_id, chosen["ordinal"],
                                        chosen["filename"] or "")
    return resolved if (resolved and resolved.content) else None


def _read_pod(content: bytes):
    """Parse these bytes as a proof of delivery, or None. Never raises — a file we cannot read is
    simply not the proof, and a malformed attachment must not stop a post."""
    from pipeline.parsing import pod as pod_parser
    from pipeline.stage3_extract.pdf_adapter import _page_texts
    try:
        return pod_parser.parse_pod("\n".join(_page_texts(content)))
    except Exception:                                              # noqa: BLE001
        return None


def _pod_absence_reason(conn: sqlite3.Connection, row: Any) -> str:
    """Why no POD was found, in terms that point at the right fix.

    Four genuinely different situations, which a single "no stored bytes" message conflated —
    false in three of the four, and it sent people looking in the attachment store for a file that
    was either never sent, never read, or read and found to be about a different purchase order:

    * the email carried no attachments, or only inline ones — a data problem at Premier's end;
    * it carried attachments but nothing read them as a proof of delivery — a coverage gap in the
      readers, and the message says which files went unread so the gap can be closed;
    * something *was* read as a POD, and it names a different purchase order — the interesting
      case, because it usually means the record and the paperwork disagree;
    * the chosen file's bytes were never stored — ours, and the one worth investigating.

    It mirrors `_pod_for`'s rule, so the two must move together: a reason describing a rule that
    is no longer applied is worse than no reason at all.
    """
    email_id = str(_get(row, "source_email_id") or "")
    if not email_id:
        return "this record is not linked to an email, so there is no attachment to upload"
    po_number = str(_get(row, "po_number") or "").strip()

    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT filename, sniffed_kind, is_inline, COALESCE(is_pod, 0) AS is_pod,
                  COALESCE(pod_po_numbers, '') AS pod_po_numbers
             FROM attachment_ledger WHERE email_id = ?""", (email_id,)).fetchall()
    if not rows:
        return "the delivery email carried no attachments, so there is no proof of delivery"

    attached = [r for r in rows if not r["is_inline"]]
    if not attached:
        return ("the email's only attachments are inline images — signature logos and letterhead, "
                "not a proof of delivery")

    pods = [r for r in attached if r["is_pod"]]
    if not pods:
        names = ", ".join(f"{str(r['filename'])[:34]} ({r['sniffed_kind']})" for r in attached[:3])
        return (f"nothing on this email was read as a proof of delivery — {names}. A PDF is "
                f"re-read here if it was missed; anything else has to be read at ingest.")

    elsewhere = sorted({p for r in pods
                        for p in str(r["pod_po_numbers"] or "").split(",") if p})
    if elsewhere and po_number and po_number not in elsewhere:
        return (f"the proof of delivery on this email names purchase order "
                f"{', '.join(elsewhere)}, not {po_number}")

    names = ", ".join(str(r["filename"])[:40] for r in pods[:2])
    return (f"the proof of delivery ({names}) has no stored bytes — only its metadata was kept, "
            f"so there is nothing to upload")


def _build_report(conn: sqlite3.Connection, record_id: int, *, po_number: str,
                  description: str, quantity, unit_of_measure: str) -> bytes:
    """The per-delivery receiver report, as PDF bytes.

    Scoped to this one record. The whole-store report would put every other purchase order's
    quantities on a document attached to one receipt, which Premier reads as evidence for that
    delivery.

    Takes the four display values rather than a `Decision`. `post_report` runs after the gate has
    already passed and the receipt exists; re-deriving a Decision there would mean re-running gates
    that could now refuse — stranding a receipt with a POD and no report over a quantity that moved
    after the POD went up.
    """
    report = receipt_log.build(conn, record_ids=[record_id])
    body = receipt_log.to_html(report)
    html = report_pdf.document(
        body,
        title=f"Receiver Report — PO {po_number}",
        subtitle=(f"{description[:70]} · "
                  f"{post_decision.po_verify.fmt_qty(quantity)} "
                  f"{unit_of_measure}").strip(" ·"),
        note=(f"{TEST_MARKER} — generated by the Premier receiver automation from the delivery "
              f"email. Attached to the receipt alongside the proof of delivery."))
    return report_pdf.render(html)


def _link_related(conn: sqlite3.Connection, client: SpitfireWriteClient, receipt_key: str,
                  po_number: str, project_code: str) -> List[str]:
    """Link the receipt to its purchase order and to any pay requests on the same PO.

    `SubContract` is the join: commitment, pay request, change order and receipt all carry the PO
    number there, and it is the only field that relates them. The pay-request link matters more
    than it looks — the SoW is explicit that a receiver without it leaves reporting treating the
    item as never received, so depreciation never starts, which is the gap behind Premier's manual
    quarterly catch-up.

    Failures here are collected and reported, not raised. By this point the receipt carries the
    POD and the report, which is the evidence Premier needs; a missing link is worth telling
    somebody about but not worth discarding a good receipt over.
    """
    done: List[str] = []
    conn.row_factory = sqlite3.Row
    po_row = conn.execute("SELECT doc_master_key FROM spitfire_po_index WHERE po_number = ?",
                          (po_number,)).fetchone()
    if po_row and po_row["doc_master_key"]:
        try:
            client.link_document(receipt_key, str(po_row["doc_master_key"]),
                                 note=f"{TEST_MARKER} purchase order")
            done.append(f"linked to PO {po_number}")
        except Exception as exc:                  # noqa: BLE001 — see docstring
            done.append(f"could not link the PO: {exc}")
    else:
        done.append("no local DocMasterKey for the PO, so it was not linked")

    try:
        pay_requests = client.find_documents(project_code, PAY_REQUEST_DOC_TYPE, limit=60)
    except Exception as exc:                      # noqa: BLE001
        done.append(f"could not search for pay requests: {exc}")
        return done

    matches = [d for d in pay_requests
               if str(d.get("SubContract") or "").strip() == po_number]
    if not matches:
        done.append("no pay requests on this PO to link")
        return done
    for pay_request in matches:
        try:
            client.link_document(receipt_key, str(pay_request["DocMasterKey"]),
                                 note=f"{TEST_MARKER} pay request",
                                 cat_type=PAY_REQUEST_DOC_TYPE)
            done.append(f"linked pay request {pay_request.get('DocNo') or ''}".rstrip())
        except Exception as exc:                  # noqa: BLE001
            done.append(f"could not link pay request {pay_request.get('DocNo')}: {exc}")
    return done


def _confirm(client: SpitfireWriteClient, receipt_key: str, pod_key: str,
             report_key: str) -> bool:
    """Read the receipt back and check both files are on it.

    The write responses do not establish this. Empty-body writes on this API return 204 whether
    they changed anything or not, and a 200 on the attach is not evidence the row exists.
    """
    attachments = client.read_attachments(receipt_key)
    keys = {str(a.get("DocKey") or "").lower() for a in attachments}
    return pod_key.lower() in keys and report_key.lower() in keys


def _mark_pushed(conn: sqlite3.Connection, record_id: int) -> None:
    """Move the record to `pushed_to_spitfire`, the last rung of the agreed lifecycle.

    The vocabulary was fixed at the 8 August meeting and has been in `delivery_status.LIFECYCLE`
    unused ever since, because nothing could reach it. This is what reaches it.
    """
    conn.execute("UPDATE extracted_records SET status = ? WHERE id = ?",
                 (delivery_status.PUSHED_TO_SPITFIRE, record_id))
    conn.commit()


def _safe_name(name: str, fallback: str) -> str:
    """A filename Spitfire and Windows will both accept.

    Spitfire takes the stored filename from the multipart `file` part and ignores `fileMeta.Name`
    — measured 2026-08-14 — so this is what Premier sees in the attachments list.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip()).strip("._") or fallback
    # A file already carrying the marker keeps one, not two. The first live run produced
    # `CC-TEST.CC-TEST.POD.FedEx.pdf` in Premier's attachments list, which reads as a mistake and
    # is one.
    if cleaned.upper().startswith(f"{TEST_MARKER}."):
        return cleaned
    return f"{TEST_MARKER}.{cleaned}"


def _strip(value: Any) -> str:
    return re.sub(r"\s+", " ", _HTML_TAG.sub(" ", str(value or ""))).strip()


def _get(row: Any, name: str) -> Any:
    try:
        return row[name]
    except (IndexError, KeyError, TypeError):
        return getattr(row, name, None)
