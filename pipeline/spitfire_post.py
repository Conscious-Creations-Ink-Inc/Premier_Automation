"""Post one verified delivery to Spitfire: a receipt, its POD, its report, and its links.

The order of the nine calls is not arbitrary. Each step is written to the ledger the moment it
succeeds, because this API offers no transaction and no way to ask afterwards what happened —
`ReceiptInProgressUnits` reads 0.0 against an unapproved receipt, so a half-finished post is
invisible from Spitfire's side and the ledger is the only place it can be seen.

    1  create the receipt      the PO link, via forBatch -> SubContract
    2  title it                immediately, so it is never anonymous in Premier's UI
    3  set the quantities      onto the rows Spitfire already built from the order, in one PATCH
    4  upload the POD          -> fileKey, hash-verified against our own MD5
    5  attach the POD
    6  build + upload + attach the receiver report
    7  link the purchase order
    8  link its pay requests
    9  read everything back    the only proof any of it landed

Step 3 said "add the receipt line" until 2026-08-31, describing `spitfire_write.add_line` — which
this module stopped calling on 2026-08-22 and must never call again. Creating a receipt with
`forBatch` builds one row per purchase-order line already carrying its `SCDocItemKey`, spec, cost
code and unit; appending a row beside those produces an orphan whose every linking field is
discarded on insert, which is why nothing this system posted before that date was ever counted.

**Two grains, and the second is the one the UI drives.** `post_pod` / `post_report` take one record
and build one receipt for it — kept for records belonging to no delivery, and for the tests.
`post_delivery_pod` / `post_delivery_report` take a delivery and build **one receipt carrying one
row per item line**, which is what a delivery actually is and what Premier's own automation
produces. See the section header above `post_delivery_pod` for where the two deliberately differ.

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
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config import settings
from connectors import spitfire_write
from connectors.spitfire_write import SpitfireSessionExpired, SpitfireWriteClient
from pipeline import (attachment_bytes, dedupe, deliveries_store, delivery_status, po_verify,
                      post_decision, post_ledger, read_views, receipt_log, report_pdf)

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


def _no_pod_because(decision) -> str:
    """Why this receipt carries no proof document, in the terms the ledger and the reviewer need.

    Two routes past the POD gate and they are not interchangeable: a person accepted the omission,
    or the mail itself carried the evidence. Anyone reading the ledger a month later needs to know
    which, so neither is described in the other's words.
    """
    if decision.pod_waived_by:
        return f"waived by {decision.pod_waived_by}"
    if decision.body_evidence:
        return f"the delivery is evidenced in the email body ({decision.body_evidence})"
    return "no proof of delivery"


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

        # --- 3. the quantity, onto the line Spitfire already built ----------------------------
        # Not `add_line`. Creating the receipt with `forBatch` builds one item per purchase order
        # line, already carrying `SCDocItemKey`, the spec, the cost code and the unit — the only
        # empty field is the quantity. Appending a line instead produced an orphan whose every
        # linking field was stripped on insert, which is why nothing this system posted before
        # 2026-08-22 was ever counted. See `spitfire_write.set_line_quantity`.
        line = client.find_prepopulated_line(receipt_key, decision.line_key)
        if line is None:
            raise RuntimeError(
                f"the receipt was created but carries no line against purchase order line "
                f"{decision.line_number} ({decision.line_key or 'no key'}) — Spitfire builds a "
                f"receipt from the order, so a missing line means the order moved underneath us; "
                f"nothing was written")
        task = (line.get("DocItemTask") or [{}])[0]
        task_key = str(task.get("ItemTaskKey") or "")
        if not task_key:
            raise RuntimeError(
                f"receipt line {line.get('DocItemNumber')!r} has no ItemTaskKey, so there is no "
                f"row to write the quantity into")

        quantity = float(decision.quantity or 0)
        client.set_line_quantity(receipt_key, {task_key: quantity})
        steps.append(f"quantity set on line {line.get('DocItemNumber')} "
                     f"({post_decision.po_verify.fmt_qty(decision.quantity)} "
                     f"{decision.unit_of_measure})".rstrip())

        # Read back only now, after the session has been released. A read taken any earlier
        # returns the pre-change value — that staleness made a correct write look like a failure
        # twice while this was being worked out.
        written = client.verify_quantities(receipt_key, {task_key: quantity})
        if abs(written.get(task_key, 0.0) - quantity) >= 0.001:
            raise RuntimeError(
                f"the quantity was sent but reading the receipt back shows "
                f"{written.get(task_key, 0.0):g} on line {line.get('DocItemNumber')} rather than "
                f"{quantity:g} — the receipt does not say what we asked it to say")
        steps.append("quantity read back and confirmed")

        # --- 4-5. the POD, when there is one --------------------------------------------------
        # Skipped entirely for a delivery stated in the email body with nothing attached. Reaching
        # here at all means `post_decision` opened that path by one of exactly two routes — a
        # waiver from a named person, or evidence in the body itself (a signer, or a carrier and
        # tracking number, with the date) — so the omission is always a decision rather than an
        # oversight. Which route it was is named in the ledger detail and in the result below, and
        # the receiver report attached at step 6 still lands on this receipt, so it is not bare.
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
            why = _no_pod_because(decision)
            steps.append(f"no proof of delivery — {why}")

        # Sign our own route steps now the proof is on the receipt, so it reaches whoever checks
        # it rather than resting at our stop. Non-fatal — see `_sign_off_route`.
        steps.extend(_sign_off_route(client, receipt_key))

        receipt_name = doc_no or receipt_key[:8]
        settled = (f"receipt {receipt_name} — report not posted" if pod else
                   f"receipt {receipt_name} — no POD, {_no_pod_because(decision)}; "
                   f"report not posted")
        post_ledger.settle(conn, key, post_ledger.POD_POSTED, settled, client.audit_rows())
        return PostResult(
            ok=True, state=post_ledger.POD_POSTED, record_id=record_id, po_number=po_number,
            message=((f"the proof of delivery is on receipt {receipt_name}. "
                      f"The receiver report has not been posted yet.") if pod else
                     (f"receipt {receipt_name} was created with no proof of delivery attached — "
                      f"{_no_pod_because(decision)}. The receiver report has not been posted "
                      f"yet.")),
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


def evidenced_state(conn: sqlite3.Connection, attempt, row: Any, *,
                    client: Optional[SpitfireWriteClient] = None) -> PostResult:
    """What a stranded `CLAIMED` attempt actually achieved, read back from Spitfire. Reads only.

    A claim is written *before* the first call, so that a receipt can never be created without a
    row naming it. The cost is that a process killed mid-chain leaves the row at `CLAIMED` for
    ever: it is in `BLOCKING`, so `claim`, `record_refusal`, `post_report`, `verify_pod` and the
    Post button all refuse to touch it, and nothing else may move it. `post_ledger.stranded` lists
    those rows; this is the half that says what to settle each one *as*.

    Live example, record 234 on PO 912560: killed on 2026-08-26 after the receipt, the POD upload
    and the attach had all succeeded, leaving `settled_at` NULL and `detail` empty — a shape no
    exception path can produce, since both handlers in `post_pod` write a reason. Spitfire held
    receipt 0001 with the POD on it and the right hash the whole time; the Records page said
    "Posting…" and Verify POD said nothing had been posted at all.

    **Every verdict is read from Spitfire, never inferred from the ledger.** The ledger is what is
    in doubt — its columns say what we *tried*, and the receipt says what landed:

    - no `receipt_key` on the claim -> `FAILED`. Nothing was created, so nothing is orphaned and
      the record is free to post again: `FAILED` is deliberately not in `BLOCKING`.
    - the report is on the receipt -> `POSTED`. The chain had in fact finished.
    - the POD is on it and the catalog's hash matches our own bytes -> `POD_POSTED`, the resting
      state the two-step split exists for. The report step is then offered normally.
    - a receipt exists but the POD is missing or its hash differs -> `PARTIAL`. Something did not
      land and a person has to work out what; retrying would build a second receipt beside it.

    Raises nothing on a Spitfire failure — it returns `ok=False` with `state=CLAIMED`, meaning
    *still unknown*. A claim that could not be checked must keep blocking: settling it to `FAILED`
    on a guess would unblock a duplicate post against a receipt that already exists.
    """
    record_id = int(_get(row, "id") or 0)
    po_number = str(_get(row, "po_number") or "").strip()
    steps: List[str] = []

    def result(state: str, message: str, **kw) -> PostResult:
        return PostResult(ok=state in post_ledger.TERMINAL, state=state, message=message,
                          record_id=record_id, po_number=po_number,
                          receipt_key=attempt.receipt_key,
                          receipt_doc_no=attempt.receipt_doc_no, steps=steps, **kw)

    if not attempt.receipt_key:
        return result(post_ledger.FAILED,
                      "the claim was recorded but no receipt was ever created, so nothing is "
                      "outstanding in Spitfire and the record can post again")

    label = attempt.receipt_doc_no or attempt.receipt_key[:8]
    client = client or SpitfireWriteClient()
    try:
        client.whoami()
        on_receipt = _attachment_keys(client, attempt.receipt_key)
        steps.append(f"receipt {label} carries {len(on_receipt)} attachment(s)")

        if attempt.report_file_key and attempt.report_file_key.lower() in on_receipt:
            return result(post_ledger.POSTED, f"receipt {label} already carries both the proof of "
                                              f"delivery and the receiver report",
                          pod_file_key=attempt.pod_file_key,
                          report_file_key=attempt.report_file_key)

        if not attempt.pod_file_key or attempt.pod_file_key.lower() not in on_receipt:
            return result(post_ledger.PARTIAL,
                          f"receipt {label} exists but the proof of delivery is not on it")
        steps.append(f"POD found on receipt {label}")

        # The same second question `verify_pod` asks, and for the same reason: being present is not
        # being *ours*. Without it a file replaced after upload would settle as a clean POD_POSTED.
        pod = _pod_for(conn, row)
        if pod is None:
            return result(post_ledger.PARTIAL,
                          f"the proof of delivery for receipt {label} is no longer in our store, "
                          f"so the bytes on the receipt cannot be checked against it")
        pod_md5 = hashlib.md5(pod.content).hexdigest().upper()
        if not client.verify_upload(attempt.pod_file_key, pod_md5):
            return result(post_ledger.PARTIAL,
                          f"the POD on receipt {label} is not the file we sent — its catalog hash "
                          f"does not match ours ({pod_md5})")
        steps.append(f"catalog hash matches ours ({pod_md5})")

        return result(post_ledger.POD_POSTED,
                      f"receipt {label} exists with the proof of delivery on it and hash-verified; "
                      f"the receiver report was never posted",
                      pod_file_key=attempt.pod_file_key)

    except Exception as exc:                      # noqa: BLE001
        # Deliberately not a verdict. Left CLAIMED, which keeps blocking — see the docstring.
        return PostResult(ok=False, state=post_ledger.CLAIMED, message=str(exc),
                          record_id=record_id, po_number=po_number,
                          receipt_key=attempt.receipt_key,
                          receipt_doc_no=attempt.receipt_doc_no, steps=steps)




# --- posting a whole delivery -------------------------------------------------------------------
# One receipt per (purchase order, delivery), carrying one row per item line.
#
# Everything below sits *beside* the per-record pair above rather than replacing it. `post_pod` is
# still the path for a record belonging to no delivery, and the two differ on purpose in one place:
# a purchase-order line with no row on the receipt aborts the single-record post, because that post
# has nothing else to do, and is *dropped* by the grouped post, because nineteen good lines should
# not be discarded to protect one that was already lost. Premier's own
# `czx_TPICreate_ReceiptDoc.sql` does the same — it deletes unmatchable items before creating the
# document, then records "Unable to find match" against them.


@dataclass
class LinePlan:
    """One item line's place in a delivery post — on the receipt, or not, and why."""
    record_id: int
    ok: bool
    reason: str = ""
    """Empty when `ok`. Otherwise one sentence, written for the person reading the dialog."""

    line_number: Optional[int] = None
    quantity: Optional[float] = None
    unit_of_measure: str = ""
    spec_code: str = ""
    description: str = ""

    blocked_on_pod: bool = False
    """Whether the thing stopping this line is gate 2, and so something a person can settle.

    Set from the facts `plan_delivery` already holds, never by reading `reason`. That sentence is
    written for a human and gets reworded; routing a control off it would mean the next rewording
    silently removed the only way out of the dialog, which is the failure this whole change exists
    to fix.
    """


@dataclass
class _Context:
    """What the post carries per line and the dialog never shows."""
    row: Any
    decision: Any = None
    evidence_key: str = ""
    pod: Optional[attachment_bytes.ResolvedAttachment] = None
    pod_md5: str = ""
    idempotency_key: str = ""
    task_key: str = ""


@dataclass
class DeliveryPostResult:
    """What one delivery post did, in the terms the dialog and the ledger both need."""
    ok: bool
    state: str
    message: str
    delivery_id: int = 0
    po_number: str = ""
    receipt_key: str = ""
    receipt_doc_no: str = ""
    group_key: str = ""
    lines: List[LinePlan] = field(default_factory=list)
    steps: List[str] = field(default_factory=list)

    @property
    def posted_lines(self) -> List[LinePlan]:
        return [line for line in self.lines if line.ok]

    @property
    def skipped_lines(self) -> List[LinePlan]:
        return [line for line in self.lines if not line.ok]


def delivery_rows(conn: sqlite3.Connection, delivery_id: int) -> List[Any]:
    """This delivery's item lines, narrowed to the ones the Records page is actually offering.

    `deliveries_store.lines_for` returns every line that arrived, including those
    `read_views._READY_CLAUSE` holds back — quantity conflicts, zero confidence, no purchase order.
    Posting a row a person could not see is the one thing a grouped Post must never do: the button
    reads "16 of 20 lines" because the block on screen shows 20, and both have to be counted from
    the same list. PO 907249 is the case in point — 22 lines arrived, 2 are quantity conflicts, and
    the page shows 20.
    """
    ready = {int(row["id"]) for row in read_views.records_ready(conn)}
    return [row for row in deliveries_store.lines_for(conn, delivery_id)
            if int(row["id"]) in ready]


def plan_delivery(conn: sqlite3.Connection, rows: Sequence[Any], *,
                  read_client_factory=None) -> Tuple[List[LinePlan], Dict[int, "_Context"]]:
    """Rule on every line of a delivery, reading the purchase order **once**.

    `post_decision.decide` reads the order live on every call, so calling it per record would read
    one purchase order twenty times on a request somebody is watching. It is read here instead and
    handed to each `decide` as `verification=`.

    The rows cannot simply be handed to `verify_records` together and left at that: `chosen_line` is
    honoured only for a single row, so a batch call would silently discard every line a reviewer
    picked — the only recovery path there is for an ambiguous spec. `chosen_lines` carries them
    positionally instead.

    **No gate is reimplemented here.** Every refusal below is `decide`'s own sentence, so the grouped
    path and the single-record path cannot drift apart in what they permit.
    """
    verifications = po_verify.verify_records(
        conn, list(rows), client_factory=read_client_factory,
        # `post_decision`'s own coercion, not a second one: the line a reviewer chose has to reach
        # the verification in exactly the form gate 4 would have passed it.
        chosen_lines=[post_decision._int_or_none(_get(row, "po_line_number")) for row in rows])

    plans: List[LinePlan] = []
    contexts: Dict[int, _Context] = {}
    for row, verification in zip(rows, verifications):
        record_id = int(_get(row, "id") or 0)
        description = str(_get(row, "item_description") or "")
        spec_code = str(_get(row, "spec_code") or "")

        pod = _pod_for(conn, row)
        pod_md5 = hashlib.md5(pod.content).hexdigest().upper() if pod else ""
        # Per record, exactly as the single-record path computes it. One delivery-wide evidence key
        # would change the idempotency key of every record whose proof differs from its neighbour's,
        # orphaning rows already in Premier's live ledger.
        evidence_key = pod_md5 or dedupe.evidence_key(
            body_text=_body_text_of(conn, row), email_id=_get(row, "source_email_id"))

        decision = post_decision.decide(
            conn, row, verification=verification, pod_md5=pod_md5,
            client_factory=read_client_factory, evidence_key=evidence_key,
            pod_reason="" if pod else _pod_absence_reason(conn, row))

        contexts[record_id] = _Context(row=row, decision=decision, evidence_key=evidence_key,
                                       pod=pod, pod_md5=pod_md5)
        plans.append(LinePlan(
            record_id=record_id, ok=decision.may_post,
            blocked_on_pod=(not decision.may_post and not pod_md5
                            and not post_decision.can_post_offline(
                                row, has_pod_bytes=False,
                                origin_sender=post_decision._origin_sender(conn, row))),
            reason="" if decision.may_post else decision.reason,
            line_number=decision.line_number, quantity=decision.quantity,
            unit_of_measure=decision.unit_of_measure,
            spec_code=decision.spec_code or spec_code,
            description=decision.description or description))
    _refuse_line_collisions(plans)
    return plans, contexts


@dataclass
class PodBlock:
    """One line of a delivery that gate 2 refuses, and the sentence saying why."""
    row: Any
    record_id: int
    reason: str
    """`_pod_absence_reason`'s own words — the same sentence the confirm dialog shows, so the page
    a person is sent to repeats what sent them there rather than paraphrasing it."""


def pod_blocked(conn: sqlite3.Connection, rows: Sequence[Any]) -> List[PodBlock]:
    """The lines of a delivery blocked on gate 2, resolved from real bytes and nothing else.

    **Gate 2 only, and deliberately not `plan_delivery`.** That function answers every gate, and it
    opens by reading the whole purchase order out of Spitfire — seconds per delivery, the cost the
    confirm dialog exists to pay once. A page that only asks "is there a proof of delivery" must not
    pay for a live purchase-order read, and a line held back for an over-receipt is not something a
    waiver can free: listing it would ask somebody to sign for a refusal their signature cannot
    lift.

    **It also must not ask the optimistic question the page asks.** `emails_with_a_possible_pod` is
    a superset — any non-inline PDF counts — and that guess is exactly what made this page necessary:
    measured 2026-09-10, of 107 rows the Records page believed had a proof, only 16 resolved one,
    and the other 91 offered a Post button that refused and no way forward. Here the bytes are
    resolved for real.

    Affordable because it is scoped to one delivery and memoised. `_pod_for` reads only the row's
    email, purchase order and reviewer choice, so every line of a one-message delivery shares an
    answer: one `attachment_ledger` query and at most one PDF text extraction for thirty-two lines
    rather than thirty-two of each.
    """
    seen: Dict[tuple, bool] = {}
    blocked: List[PodBlock] = []
    for row in rows:
        key = (str(_get(row, "source_email_id") or ""), str(_get(row, "po_number") or ""),
               _get(row, "pod_ledger_id"))
        if key not in seen:
            seen[key] = bool(_pod_for(conn, row))
        if post_decision.can_post_offline(
                row, has_pod_bytes=seen[key],
                origin_sender=post_decision._origin_sender(conn, row)):
            continue
        blocked.append(PodBlock(row=row, record_id=int(_get(row, "id") or 0),
                                reason=_pod_absence_reason(conn, row)))
    return blocked


def _refuse_line_collisions(plans: List[LinePlan]) -> None:
    """Refuse every line of a delivery that resolves to the same purchase-order line as another.

    **Not summed, and the difference matters.** `set_line_quantity` is an assignment, so writing two
    records into one row silently keeps whichever went last. Adding them instead would book a number
    **no gate ever approved**: the gates ran per record, so 4 EA and 4 EA each pass against 4
    outstanding while their sum over-receives — defeating the one gate whose whole purpose is to
    stop that (`post_decision.OVER_RECEIPT_TOLERANCE` is 0.0).

    Premier's own proc does not sum either: its `UPDATE … FROM … JOIN` applies one arbitrary row's
    quantity when two source rows join one item, and reports nothing.

    The three things a collision can mean are all badly served by addition — the same goods
    extracted twice (double-booked), two different items matched onto one line (both booked to the
    wrong cost code), or a component split, where Authority ships `-B` and `-SH` separately while
    the order counts assembled units. A person settles it.
    """
    seen: Dict[int, List[LinePlan]] = {}
    for plan in plans:
        if plan.ok and plan.line_number is not None:
            seen.setdefault(plan.line_number, []).append(plan)
    for line_number, group in seen.items():
        if len(group) < 2:
            continue
        described = " and ".join(
            (f"{post_decision.po_verify.fmt_qty(p.quantity)} {p.unit_of_measure}".strip()
             + f" (record {p.record_id})") for p in group)
        for plan in group:
            plan.ok = False
            plan.reason = (
                f"{len(group)} lines of this delivery all resolve to purchase order line "
                f"{line_number:04d} — {described}. A receipt line holds one quantity, so this "
                f"needs a person to say which line each item belongs to.")


def _refuse(conn: sqlite3.Connection, plans: Sequence[LinePlan], contexts: Dict[int, "_Context"],
            po_number: str, actor: str) -> None:
    """Write the gate's refusal to the ledger for every line that is not going on the receipt.

    Recorded rather than only returned, for the reason `record_refusal` already gives: without a row
    the reason lives only in the dialog somebody closed, and the next person clicks Post to
    rediscover it at the cost of a live purchase-order read.
    """
    for plan in plans:
        if plan.ok:
            continue
        context = contexts.get(plan.record_id)
        if context is None:
            continue
        post_ledger.record_refusal(
            conn, record_id=plan.record_id, po_number=po_number, line_number=plan.line_number,
            pod_md5=context.evidence_key, reason=plan.reason, actor=actor)


def post_delivery_pod(conn: sqlite3.Connection, delivery_id: int, *,
                      client: Optional[SpitfireWriteClient] = None,
                      read_client_factory=None, actor: str = "") -> DeliveryPostResult:
    """Create **one** receipt for one delivery and put every ready item line on it. Steps 1-5.

    This is what `post_pod` should always have been. A purchase order does not arrive; a delivery
    against it arrives, carrying some of its lines — so the receipt is per `(PO, delivery)`, which is
    exactly what `extracted_records.delivery_id` identifies, and one row goes on it per item line.
    Premier's own automation groups the same way, one header per `(PurchaseOrder, ShipmentNumber)`.

    **Grouping is a receipt-level change only.** Every item line is still matched to its own
    purchase-order line, by spec, through `post_decision`; nothing is merged and nothing is summed.
    Twenty lines become twenty rows on one document, not one row of twenty.

    The order of what follows is forced by two facts about this API: nothing in it is idempotent,
    and a created document cannot be deleted. So every question that could refuse the post is asked
    **before** the receipt exists, and once it exists the only remaining question is what the ledger
    should say about a document that will be there for ever.
    """
    delivery = deliveries_store.get(conn, delivery_id)
    if delivery is None:
        return DeliveryPostResult(
            ok=False, state="flagged", delivery_id=delivery_id,
            message="there is no such delivery")

    po_number = str(delivery["po_number"] or "").strip()
    rows = delivery_rows(conn, delivery_id)
    if not rows:
        return DeliveryPostResult(
            ok=False, state="flagged", delivery_id=delivery_id, po_number=po_number,
            message=("nothing on this delivery is ready to post — every line is held back as a "
                     "quantity conflict, incomplete, or already posted"))

    plans, contexts = plan_delivery(conn, rows, read_client_factory=read_client_factory)

    # The duplicate guard, re-asked with the line the verification just *resolved*. Gate 3 inside
    # `decide` runs before the line is known, so a record whose `po_line_number` is null asks about
    # `line IS NULL` and cannot see that this very line was posted last week from a record that has
    # since been re-extracted under a new id. That is the gap PO 912559 fell through eight times.
    for plan in plans:
        if not plan.ok:
            continue
        existing = post_ledger.find_delivery(
            conn, po_number, plan.line_number, contexts[plan.record_id].evidence_key)
        if existing:
            plan.ok = False
            plan.reason = post_decision._describe_existing(existing)

    _refuse(conn, plans, contexts, po_number, actor)
    postable = [plan for plan in plans if plan.ok]
    if not postable:
        # **Before Spitfire is touched.** Falling through to `create_receipt` here would leave an
        # empty document on Premier's instance that nothing can delete — the likeliest way to make
        # this feature worse than what it replaces, and it needs only a second click.
        return DeliveryPostResult(
            ok=False, state="flagged", delivery_id=delivery_id, po_number=po_number, lines=plans,
            message=(f"none of the {len(plans)} lines on this delivery can be posted — "
                     f"{plans[0].reason}" if len(plans) == 1 else
                     f"none of the {len(plans)} lines on this delivery can be posted; "
                     f"open each for its reason"))

    project_code = contexts[postable[0].record_id].decision.project_code
    group_key = post_ledger.new_group_key()

    claimed: List[LinePlan] = []
    for plan in postable:
        context = contexts[plan.record_id]
        claim = post_ledger.claim(
            conn, record_id=plan.record_id, po_number=po_number, line_number=plan.line_number,
            pod_md5=context.evidence_key, project_code=context.decision.project_code,
            quantity=plan.quantity, actor=actor, group_key=group_key)
        if claim is None:
            # Lost the race between deciding and claiming — two operators on one delivery, or a
            # double submit that outran the disabled button. The UNIQUE index settles it.
            plan.ok = False
            existing = post_ledger.find(conn, post_ledger.idempotency_key(
                plan.record_id, po_number, plan.line_number, context.evidence_key))
            plan.reason = (post_decision._describe_existing(existing) if existing
                           else "this line is already being posted")
            continue
        context.idempotency_key = claim.idempotency_key
        claimed.append(plan)

    if not claimed:
        return DeliveryPostResult(
            ok=False, state="flagged", delivery_id=delivery_id, po_number=po_number, lines=plans,
            group_key=group_key,
            message="every line on this delivery is already being posted by someone else")

    keys = [contexts[plan.record_id].idempotency_key for plan in claimed]
    steps: List[str] = []
    receipt_key = ""
    doc_no = ""
    client = client or SpitfireWriteClient()

    def settle_all(state: str, detail: str) -> None:
        """Close every claimed row at once. A row that will not settle is left `CLAIMED`, which
        still blocks and is recoverable by reading the receipt back — the safe way to fail."""
        post_ledger.settle_group(conn, keys, state, detail, client.audit_rows())

    try:
        client.whoami()   # fails loudly on a lapsed ticket, before a document exists

        # --- 1-2. the receipt, once ----------------------------------------------------------
        receipt_key = client.create_receipt(project_code, po_number)
        for key in keys:
            post_ledger.record_receipt(conn, key, receipt_key=receipt_key)
        steps.append(f"receipt created ({receipt_key[:8]}…)")

        title = (f"{TEST_MARKER} - receiver automation - PO {po_number}"
                 f" - {len(claimed)} lines - {date.today().isoformat()} - DO NOT PROCESS")
        client.set_title(receipt_key, title)
        steps.append("titled")

        header = client.read_header(receipt_key)
        doc_no = str(header.get("DocNo") or "")
        for key in keys:
            post_ledger.record_receipt(conn, key, receipt_key=receipt_key, receipt_doc_no=doc_no)
        if str(header.get("SubContract") or "").strip() != po_number:
            raise RuntimeError(
                f"the receipt was created but its SubContract reads "
                f"{header.get('SubContract')!r} instead of {po_number} — it is not linked to the "
                f"purchase order, so nothing further was attached")
        steps.append(f"linked to PO via SubContract (DocNo {doc_no or '?'})")

        # --- 3. the quantities, one PATCH ------------------------------------------------------
        # `read_items` once. `find_prepopulated_line` re-reads the whole document on every call, so
        # asking it twenty questions about one payload would be twenty document reads.
        items = client.read_items(receipt_key)
        quantities: Dict[str, float] = {}
        going: List[LinePlan] = []
        for plan in claimed:
            context = contexts[plan.record_id]
            item = spitfire_write.line_in(items, context.decision.line_key)
            task_key = spitfire_write.task_key_of(item)
            if item is None or not task_key:
                # Dropped, not fatal — see this section's header comment. The row is already
                # CLAIMED, and `record_refusal` refuses to touch a blocking row, so it is settled
                # directly. FLAGGED does not block, so the line posts on a later receipt once the
                # order is re-read.
                plan.ok = False
                plan.reason = (
                    f"purchase order {po_number} carries no receipt line against line "
                    f"{plan.line_number} ({context.decision.line_key or 'no key'}) — Spitfire "
                    f"builds a receipt from the order, so the order moved underneath us. The rest "
                    f"of the delivery was posted; this line was not.")
                post_ledger.settle(conn, context.idempotency_key, post_ledger.FLAGGED, plan.reason,
                                   client.audit_rows())
                continue
            context.task_key = task_key
            quantities[task_key] = float(plan.quantity or 0)
            going.append(plan)

        if not going:
            # Every line missed. The document exists and is empty, which no one can delete — so it
            # is named here rather than quietly abandoned.
            raise RuntimeError(
                f"receipt {doc_no or receipt_key[:8]} was created but not one of the "
                f"{len(claimed)} lines has a row on it — the purchase order moved underneath us")

        # **`settle_all` closes over this name, so narrowing it here narrows what a later failure
        # settles.** That is deliberate: the dropped lines were settled FLAGGED individually just
        # above, and FLAGGED does not block, so a failure from here on must leave them alone rather
        # than sweep them into PARTIAL and wedge lines that are free to post on the next receipt.
        keys = [contexts[plan.record_id].idempotency_key for plan in going]
        client.set_line_quantity(receipt_key, quantities)
        steps.append(f"quantities set on {len(quantities)} lines in one patch")

        # Read back only now, after the session has been released — a read taken any earlier
        # returns the pre-change value. Every key is compared, not the first: this endpoint's known
        # failure is silent and 200-shaped, and "one line disagrees" sends people to the wrong line.
        written = client.verify_quantities(receipt_key, quantities)
        wrong = sorted(task for task, wanted in quantities.items()
                       if abs(written.get(task, 0.0) - wanted) >= 0.001)
        if wrong:
            missed = [p for p in going if contexts[p.record_id].task_key in wrong]
            raise RuntimeError(
                f"{len(quantities) - len(wrong)} of {len(quantities)} quantities landed on receipt "
                f"{doc_no or receipt_key[:8]}, but "
                + "; ".join(
                    f"line {p.line_number} reads "
                    f"{written.get(contexts[p.record_id].task_key, 0.0):g} rather than "
                    f"{float(p.quantity or 0):g}" for p in missed)
                + " — the receipt does not say what we asked it to say")
        steps.append("quantities read back and confirmed")

        # --- 4-5. the proofs of delivery -------------------------------------------------------
        # Grouped by content hash, not assumed to be one. A delivery can be described by two
        # messages — the Inbound notice and the Delivered notice are the designed pair — and a
        # reviewer can point two records at different attachments through `pod_ledger_id`. The
        # catalog does not deduplicate, so uploading the same bytes twice makes two entries.
        uploaded: Dict[str, str] = {}
        for plan in going:
            context = contexts[plan.record_id]
            if not context.pod or context.pod_md5 in uploaded:
                continue
            name = _safe_name(context.pod.filename, f"POD_{po_number}")
            file_key = client.upload_file(context.pod.content, name,
                                          keywords=f"{TEST_MARKER} POD {po_number}")
            if not client.verify_upload(file_key, context.pod_md5):
                raise RuntimeError(
                    f"the POD uploaded but the server's hash does not match ours — the file in the "
                    f"catalog ({file_key}) is not the file we sent")
            client.attach_file(receipt_key, file_key, note=f"{TEST_MARKER} proof of delivery")
            uploaded[context.pod_md5] = file_key
            steps.append(f"POD uploaded, hash-verified and attached ({name})")

        if uploaded:
            on_receipt = _attachment_keys(client, receipt_key)
            absent = [key for key in uploaded.values() if key.lower() not in on_receipt]
            if absent:
                raise RuntimeError(
                    "the proof of delivery was attached but reading the receipt back did not "
                    "show it")
            steps.append("read back and confirmed")
            for plan in going:
                file_key = uploaded.get(contexts[plan.record_id].pod_md5, "")
                if file_key:
                    post_ledger.record_file(conn, contexts[plan.record_id].idempotency_key,
                                            pod_file_key=file_key)
        else:
            steps.append(f"no proof of delivery — {_no_pod_because(contexts[going[0].record_id].decision)}")

        # The same signature the single-record path makes, once for the whole receipt.
        steps.extend(_sign_off_route(client, receipt_key))

        receipt_name = doc_no or receipt_key[:8]
        settle_all(post_ledger.POD_POSTED, f"receipt {receipt_name} — report not posted")
        dropped = len(plans) - len(going)
        return DeliveryPostResult(
            ok=True, state=post_ledger.POD_POSTED, delivery_id=delivery_id, po_number=po_number,
            receipt_key=receipt_key, receipt_doc_no=doc_no, group_key=group_key, lines=plans,
            steps=steps,
            message=(f"{len(going)} item line{'s' if len(going) != 1 else ''} posted to receipt "
                     f"{receipt_name} on PO {po_number}"
                     + (f", {dropped} not posted" if dropped else "")
                     + ". The receiver report has not been posted yet."))

    except SpitfireSessionExpired as exc:
        state = post_ledger.PARTIAL if receipt_key else post_ledger.FAILED
        settle_all(state, str(exc))
        return DeliveryPostResult(ok=False, state="session_expired", delivery_id=delivery_id,
                                  po_number=po_number, receipt_key=receipt_key,
                                  receipt_doc_no=doc_no, group_key=group_key, lines=plans,
                                  steps=steps, message=str(exc))
    except Exception as exc:                      # noqa: BLE001 — every failure must be recorded
        # **Every claimed row takes the same state.** FAILED is deliberately not blocking, so one
        # row settling FAILED while the receipt exists would let the next Post sail past the guard
        # and build a second receipt beside the half-built one.
        state = post_ledger.PARTIAL if receipt_key else post_ledger.FAILED
        settle_all(state, str(exc))
        _logger.exception("posting delivery %s to Spitfire failed", delivery_id)
        return DeliveryPostResult(ok=False, state=state, delivery_id=delivery_id,
                                  po_number=po_number, receipt_key=receipt_key,
                                  receipt_doc_no=doc_no, group_key=group_key, lines=plans,
                                  steps=steps, message=str(exc))




def awaiting_report_group(conn: sqlite3.Connection, delivery_id: int) -> List[Any]:
    """The rows of this delivery's receipt that are waiting for their receiver report.

    Found through the ledger rather than by re-planning the delivery: what the report goes onto is
    the receipt that exists, and the only record of which rows are on it is the group written when
    it was created. Re-deriving the set would re-run gates that could now refuse — stranding a
    receipt carrying a POD and no report over a quantity that moved after the POD went up.
    """
    receipts: Dict[str, List[Any]] = {}
    for row in deliveries_store.lines_for(conn, delivery_id):
        for attempt in post_ledger.existing_for_record(conn, int(row["id"])):
            if attempt.state == post_ledger.POD_POSTED and attempt.receipt_key:
                receipts.setdefault(attempt.receipt_key, []).append(attempt)
    if not receipts:
        return []
    # Newest receipt first when a delivery has more than one — a line fixed after the first receipt
    # posted becomes a second receipt, and it is that one whose report is outstanding.
    newest = max(receipts.values(), key=lambda group: max(a.id for a in group))
    return sorted(newest, key=lambda a: a.id)


def post_delivery_report(conn: sqlite3.Connection, delivery_id: int, *,
                         client: Optional[SpitfireWriteClient] = None,
                         actor: str = "") -> DeliveryPostResult:
    """Build **one** receiver report for the delivery and hang it on the receipt. Steps 6-9.

    One report describing every line on the receipt, not one per line: `receipt_log.build` already
    groups PO → line → receipts and already takes a list of record ids, so the document Premier
    opens says what the receipt says.

    Refuses unless the group is resting at `POD_POSTED`. That is the guarantee which survives
    splitting the post in two — the report describes a delivery whose proof is already filed, and it
    can neither precede that proof nor stand in for it.
    """
    delivery = deliveries_store.get(conn, delivery_id)
    if delivery is None:
        return DeliveryPostResult(ok=False, state="flagged", delivery_id=delivery_id,
                                  message="there is no such delivery")
    po_number = str(delivery["po_number"] or "").strip()

    attempts = awaiting_report_group(conn, delivery_id)
    if not attempts:
        return DeliveryPostResult(
            ok=False, state="flagged", delivery_id=delivery_id, po_number=po_number,
            message=("the proof of delivery has not been posted for this delivery, so there is no "
                     "receipt to put a report on — post the POD first"))

    receipt_key = attempts[0].receipt_key
    doc_no = attempts[0].receipt_doc_no
    keys = [attempt.idempotency_key for attempt in attempts]
    record_ids = [attempt.record_id for attempt in attempts]
    client = client or SpitfireWriteClient()
    steps: List[str] = []

    try:
        client.whoami()

        # --- 6. the report, once for the whole receipt -----------------------------------------
        report_bytes = _build_delivery_report(conn, record_ids, po_number=po_number)
        report_name = _safe_name(f"Receiver_Report_PO{po_number}.pdf", "Receiver_Report.pdf")
        report_key = client.upload_file(
            report_bytes, report_name, keywords=f"{TEST_MARKER} receiver report {po_number}")
        for key in keys:
            post_ledger.record_file(conn, key, report_file_key=report_key)
        client.attach_file(receipt_key, report_key, note=f"{TEST_MARKER} receiver report")
        steps.append(f"report built for {len(record_ids)} lines, uploaded and attached")

        # --- 7-8. the document links, once per receipt -----------------------------------------
        # Attaching twice creates two rows: this API has no idempotency anywhere.
        steps.extend(_link_related(conn, client, receipt_key, po_number,
                                   post_decision._project_of(conn, po_number)))

        # --- 9. read back -----------------------------------------------------------------------
        on_receipt = _attachment_keys(client, receipt_key)
        pod_keys = {a.pod_file_key.lower() for a in attempts if a.pod_file_key}
        if report_key.lower() not in on_receipt or not pod_keys.issubset(on_receipt):
            raise RuntimeError("the receipt was built but reading it back did not show the proof "
                               "of delivery and the report on it")
        steps.append("read back and confirmed")

        for record_id in record_ids:
            _mark_pushed(conn, record_id)
        post_ledger.settle_group(conn, keys, post_ledger.POSTED,
                                 f"receipt {doc_no or receipt_key[:8]}", client.audit_rows())
        return DeliveryPostResult(
            ok=True, state=post_ledger.POSTED, delivery_id=delivery_id, po_number=po_number,
            receipt_key=receipt_key, receipt_doc_no=doc_no, group_key=attempts[0].group_key,
            steps=steps,
            message=(f"posted to Spitfire as receipt {doc_no or receipt_key[:8]} — "
                     f"{len(record_ids)} item lines on one receipt"))

    except SpitfireSessionExpired as exc:
        # The receipt and its POD are real and stay that way; only the report is outstanding, so the
        # group rests where it was rather than becoming PARTIAL. Re-posting is safe.
        post_ledger.settle_group(conn, keys, post_ledger.POD_POSTED, str(exc), client.audit_rows())
        return DeliveryPostResult(ok=False, state="session_expired", delivery_id=delivery_id,
                                  po_number=po_number, receipt_key=receipt_key,
                                  receipt_doc_no=doc_no, steps=steps, message=str(exc))
    except Exception as exc:                      # noqa: BLE001 — every failure must be recorded
        post_ledger.settle_group(conn, keys, post_ledger.PARTIAL, str(exc), client.audit_rows())
        _logger.exception("posting the report for delivery %s to Spitfire failed", delivery_id)
        return DeliveryPostResult(ok=False, state=post_ledger.PARTIAL, delivery_id=delivery_id,
                                  po_number=po_number, receipt_key=receipt_key,
                                  receipt_doc_no=doc_no, steps=steps, message=str(exc))


def _build_delivery_report(conn: sqlite3.Connection, record_ids: Sequence[int], *,
                           po_number: str) -> bytes:
    """The receiver report for a whole delivery, as PDF bytes.

    Scoped to this receipt's own records. The whole-store report would put every other purchase
    order's quantities on a document attached to one receipt, which Premier reads as evidence for
    that delivery.

    The subtitle names the delivery rather than one item, because there is no longer one item to
    name — the single-record version put one description and one quantity at the top of a document
    that now describes twenty.
    """
    report = receipt_log.build(conn, record_ids=list(record_ids))
    html = report_pdf.document(
        receipt_log.to_html(report),
        title=f"Receiver Report — PO {po_number}",
        subtitle=(f"{len(record_ids)} item line{'s' if len(record_ids) != 1 else ''} "
                  f"received on one delivery"),
        note=(f"{TEST_MARKER} — generated by the Premier receiver automation from the delivery "
              f"email. Attached to the receipt alongside the proof of delivery."))
    return report_pdf.render(html)


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


def _sign_off_route(client: SpitfireWriteClient, receipt_key: str) -> List[str]:
    """Respond to our own route stops on a receipt, reporting rather than raising.

    **Called from the POD stage**, as soon as the proof is on the receipt and read back. Premier's
    decision, 2026-09-17: a receipt whose route rests at our own stop is one nobody is asked to
    look at, and that is worse than one reviewed before its receiver report lands. The trade is
    real — three receipts sat at `POD_POSTED` for up to three weeks, and with this they would have
    been visible to reviewers throughout — and it was made knowingly.

    Every receipt this system creates is staged with an approval route whose first stops are ours.
    Until they are responded to the route never advances and Premier's reviewers at sequence 10
    never see the receipt — so before this, every POD this system posted sat unread behind our own
    unsigned step. On a fresh receipt those stops are sequences 1 and 5; they are chosen by
    `UserKey` and `Reached` rather than by number, because our account also sits at 15 and which
    stop is live moves over time.

    This signs; it does not dispatch. `route/apply` and `route/perform` email three real Premier
    employees and are refused by name in `connectors/spitfire_write._DENIED_SUBSTRINGS`.

    Never raises: the caller is past the point where the receipt exists, and `post_delivery_pod`
    turns any exception from here on into PARTIAL, which would mean a person investigating a
    receipt whose only fault is an unsigned route step they can click themselves.
    """
    try:
        return client.sign_off_route_steps(receipt_key)
    except Exception as exc:                                       # noqa: BLE001
        return [f"could not sign off the route: {exc}"]


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
        3. the attachment this record was read from (`source_ledger_id`), whatever its kind
        4. nothing

    It used to be "the first non-inline attachment of an acceptable kind, in ledger order", with
    `xlsx`, `msg`, `html` and `text` among the acceptable kinds. On an email carrying a tracker
    spreadsheet at ordinal 0 and the POD at ordinal 6 — the shape of the 910634 thread, which
    carries eight attachments — that uploaded **the spreadsheet to Premier's ERP as the proof of
    delivery**, and hashed it into the idempotency key so the duplicate guard keyed on the wrong
    document too. Position in a mail is not evidence of anything.

    A PDF naming a *different* purchase order is rejected rather than used as a fallback: on this
    corpus two byte-identical PDFs arrive under filenames citing different specs and tracking
    numbers, so the filename cannot be trusted and neither can proximity.

    Returning None is a real outcome, not an error. `post_decision` refuses a record with no POD
    unless a named person has waived it. Rule 3 is not a return of that bug: it attaches the one
    file the record's own numbers were read from, by id, never a neighbour chosen by position.

    **A reviewer's explicit choice outranks all of it.** `pod_ledger_id` names one attachment and
    that is the one used, whatever the file says about itself. Three cases need it and none can be
    reached by reading the files:

    * a photographed delivery note — the rules below re-read PDFs only, because OCR is a paid call
      that belongs at ingest, so a JPEG BOL the ingest-time pass never flagged is invisible here
    * a POD naming a *different* purchase order, which is rejected outright above and is exactly
      the case a person is there to overrule
    * a proof that is not a carrier POD at all — a signed packing slip, a photo of the pallet

    With `pod_ledger_id` null the order below is unchanged, so the 910634 tracker-spreadsheet bug
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
    if unattributed is not None:
        return unattributed

    # 3. The document this record was read from. Premier's rule (2026-09-15): a line whose numbers
    #    came off a document goes to Spitfire with that document attached, carrier POD or not. Only
    #    after rules 1-2, so a real POD on the same email always wins, and only the record's own
    #    source file — never "the first attachment", which is the 910634 bug.
    source = _get(row, "source_ledger_id")
    if source not in (None, ""):
        return _chosen_pod(conn, email_id, int(source))
    return None


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
