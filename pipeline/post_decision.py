"""Whether a record may be posted to Spitfire, and if not, exactly why.

This is the judgement `pipeline/po_verify.py` deliberately refuses to make. That module's own
docstring is explicit — it *"answers it in figures, not verdicts"*, because a screen that says
MISMATCH when 202 were received against 196 ordered is wrong: an over-delivery is still open
with Premier, and a reviewer pressing Verify is asking what Spitfire holds, not what to do about
it. That design is right for a person reading a comparison, and it is left untouched.

But something has to decide, because posting is automatic. So the figures come from `po_verify`
and the ruling is made here, in one file, where it can be read end to end and argued with. The
split matters: change a rule here and the Verify screen is unaffected; change `po_verify` and
both move together, which is what nobody wants.

**Every gate is a refusal by default.** Each returns a sentence naming the thing that is wrong
and, where it can, the two numbers that disagree — that sentence is what a human sees in the
dialog and what lands in the ledger, so it is written for them, not for a log.

One gate is not ours to soften. `pipeline/completeness.py` closes with a standing instruction:

    Whoever builds the write path must refuse a record whose `is_complete` is False, or a
    receiver with no POD date reaches Premier's ERP.

`REQUIRED` there is `receipt_log._HEADERS` read backwards — a record is complete exactly when the
receiver line can be built from it — so honouring it is also what keeps the posted receipt and
the report we attach to it describing the same delivery.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence

from config import settings
from pipeline import completeness, dedupe, post_ledger, po_verify

POST = "post"
FLAG = "flag"

OVER_RECEIPT_TOLERANCE = 0.0
"""How far **past what is still owed** a delivery may go before it needs a person.

This used to be `QUANTITY_TOLERANCE`, compared against `qty_ordered` with an equality test, and
that was wrong in a way that rejected most real traffic: it demanded every delivery satisfy the
whole order in one go, so receiving 2 against 19 ordered was refused permanently with no way to
accept it. Partial deliveries are ordinary — PO 212559's own lines are 4/4/2/2 across separate
shipments.

The rule is now "anything from 1 up to what is outstanding", and this constant governs only the
other end: booking *more* than was ordered. Zero is a defensible default for that, where zero was
never a defensible default for "is this the full order". An over-receipt is a real event — 202
delivered against 196 ordered is in the corpus — but it is a conversation with the vendor, not
something to book unattended.
"""


@dataclass
class Decision:
    """The ruling on one record, with everything the dialog and the ledger need."""
    record_id: int
    verdict: str
    reason: str = ""
    """Empty on POST. On FLAG, one sentence naming what is wrong — shown verbatim to a person."""

    po_number: str = ""
    project_code: str = ""
    line_number: Optional[int] = None
    quantity: Optional[float] = None
    unit_of_measure: str = ""
    spec_code: str = ""
    description: str = ""
    cost_code: str = ""
    """`ProjEntity` from the matched PO line's `DocItemTask[0]` — the budget line the receipt posts
    against. Copied from the order rather than derived, because a wrong one books the receipt to
    the wrong cost code and nothing downstream would notice."""

    verification: Optional[po_verify.RecordVerification] = None
    existing: Optional[post_ledger.PostAttempt] = None

    pod_waived_by: str = ""
    """Who accepted that this delivery may post with no proof document, when it has none.

    Empty on every record that has a POD, and on every record that was refused for not having one.
    Non-empty only on a decision that reached POST *because* a person waived the requirement — so
    it is the one place the confirm dialog and the ledger can both read "this receipt will carry no
    proof, and X said that was acceptable"."""
    """Set when the ledger already holds this delivery, so the caller can say *when* it was posted
    and which receipt it became rather than only refusing."""

    @property
    def may_post(self) -> bool:
        return self.verdict == POST


def _flag(record_id: int, reason: str, **extra: Any) -> Decision:
    return Decision(record_id=record_id, verdict=FLAG, reason=reason, **extra)


def decide(conn: sqlite3.Connection, row: Any,
           verification: Optional[po_verify.RecordVerification] = None,
           pod_md5: str = "", client_factory=None, pod_reason: str = "",
           evidence_key: str = "") -> Decision:
    """Rule on one record. `row` is an `extracted_records` row; `pod_md5` hashes its POD bytes.

    `evidence_key` is what the duplicate guard keys on, and is **not** the same as `pod_md5`:
    the POD's hash when there is one, and a hash of the email that asserted the delivery when there
    is not. Gate 2 asks about `pod_md5` because it is asking whether a proof document exists; gate 3
    asks about `evidence_key` because it is asking whether *this delivery* has already posted, and a
    body-only delivery has to be answerable too. Defaulting it to `pod_md5` keeps every existing
    caller and every row already in `spitfire_post` behaving exactly as before.

    The gates run cheapest-and-most-certain first, so a record missing a POD date never costs a
    call to Spitfire, and the reason a person is shown is the *first* thing wrong rather than
    whichever check happened to run last.

    The comparison is read **live** rather than taken from whatever the reviewer last saw on the
    Verify screen: quantities move, and a decision to write to an ERP should rest on the order as
    it is now, not as it was when somebody opened the page. `client_factory` is `po_verify`'s own
    injection seam, passed through so tests can drive the gates without a network.
    """
    record_id = int(_get(row, "id") or 0)
    po_number = str(_get(row, "po_number") or "").strip()

    # 1. Completeness. The standing instruction from completeness.py, and the cheapest check.
    gaps = completeness.gaps(row)
    if not gaps.is_complete:
        return _flag(record_id, f"the record is incomplete — {gaps.describe()}",
                     po_number=po_number)

    # 2. A POD we can actually send. The ledger keys on this hash, and Spitfire's whole purpose
    #    here is to hold the proof of delivery — a receipt without one is worse than no receipt,
    #    because it asserts goods arrived and offers nothing to check that against. Live measurement
    #    on 2026-08-14: only 17 of 35 stored attachments carry their bytes, so this is not
    #    hypothetical.
    #
    #    A record with no POD is refused **unless a named person has waived it**. Some deliveries
    #    are stated entirely in the email body — most Authority Inbound notifications are, and they
    #    carry a clean line table and nothing attached — and refusing those for ever meant a real
    #    delivery could never be received at all. But **automation must never make that call**: it
    #    cannot see the message a reviewer can, and a receipt asserting goods arrived with nothing
    #    behind it is exactly what this gate exists to prevent.
    #
    #    So the waiver is a stored fact about one record, set by one person through one route, and
    #    null on every row that existed before the column. Nothing infers it, and nothing sets it
    #    in bulk. See `waiver_of` below and `api/ui/routes.py::waive_pod`.
    if not pod_md5:
        waived_by = str(_get(row, "pod_waived_by") or "").strip()
        if not waived_by:
            # `pod_reason` distinguishes the two cases the caller can tell apart and this module
            # cannot: an email that carried nothing usable, versus an attachment whose bytes were
            # never stored. They need different fixes — one is a data problem at Premier's end, the
            # other is ours — and a single message covering both sent people to the wrong one.
            return _flag(record_id,
                         pod_reason or "there is no proof of delivery to upload",
                         po_number=po_number)

    # 3. Already posted, or in flight. Asked before Spitfire is touched, because Spitfire cannot
    #    answer it: ReceiptInProgressUnits reads 0.0 against an unapproved receipt.
    line_hint = _int_or_none(_get(row, "po_line_number"))
    #    Asked of the *delivery*, not of this record. The idempotency key includes the record id,
    #    which changes for reasons that have nothing to do with the goods — reprocessing a mail
    #    re-extracts it under a new id, and so does a corrected extraction — so keying the question
    #    on it let the same delivery post again under a new number. PO 212559 collected eight
    #    receipts that way. (PO, line, POD hash) is what "the same delivery" actually means.
    existing = post_ledger.find_delivery(conn, po_number, line_hint, evidence_key or pod_md5)
    if existing:
        return _flag(record_id, _describe_existing(existing), po_number=po_number,
                     existing=existing)

    # 4. The comparison itself. Read live where possible; po_verify falls back to the mirror and
    #    says which it used.
    if verification is None:
        # The record's own line number is passed as the reviewer's choice, because that is what it
        # is: `record_completion` writes it when somebody picks a line from the Verify popup, and
        # `po_verify` has no other way to hear about it — `RecordFacts` does not carry a line
        # number and the scorer reads PO, spec and description only. Without this the choice is
        # silently re-derived from the description on every post, and the recovery path a reviewer
        # was offered does nothing.
        results = po_verify.verify_records(conn, [row], client_factory=client_factory,
                                           chosen_line=line_hint)
        verification = results[0] if results else None
    if verification is None:
        return _flag(record_id, "the purchase order could not be read", po_number=po_number)
    if verification.error:
        return _flag(record_id, f"Spitfire could not be read: {verification.error}",
                     po_number=po_number, verification=verification)
    if not verification.po_found:
        return _flag(record_id,
                     f"purchase order {po_number} was not found in the projects this connector "
                     f"can search ({', '.join(settings.SPITFIRE_PROJECT_IDS)})",
                     po_number=po_number, verification=verification)

    check = verification.matched
    if check is None:
        return _flag(record_id,
                     f"no line on purchase order {po_number} matched this delivery",
                     po_number=po_number, verification=verification)

    # 5. The line must have been resolved by its spec code — or chosen by a person.
    #    A fuzzy description match is a weaker claim than anything writing to an ERP should rest
    #    on. A reviewer picking a line from the alternatives table is a *stronger* one, and
    #    refusing it as "matched on description alone" was both a block on the only recovery path
    #    there is and a false statement about what happened.
    if not (check.spec_resolved or check.reviewer_chose):
        return _flag(record_id,
                     f"the line was matched on description alone, not on a spec code — "
                     f"{check.description[:60] or 'this line'} needs a person to confirm it",
                     po_number=po_number, verification=verification)

    # 6. A line with nothing left. Checked before the quantity comparison so the sentence names
    #    the actual situation — "this line is already fully received" is what a reviewer needs to
    #    read, not "you would over-receive by 19", which is technically true and unhelpful.
    #    Spitfire's own numbers lag until approval, so this is a weak signal that only ever fires
    #    when the ERP is certain; our ledger is what stops the repeat it cannot see.
    if check.qty_outstanding <= 0:
        return _flag(record_id,
                     f"purchase order {po_number} shows nothing outstanding on this line "
                     f"({po_verify.fmt_qty(check.qty_received)} of "
                     f"{po_verify.fmt_qty(check.qty_ordered)} already received)",
                     po_number=po_number, verification=verification)

    # 7. Quantities, measured against what is still owed rather than against the whole order.
    #    A delivery of part of a line is an ordinary receipt, not a discrepancy: it books what
    #    arrived and leaves the rest outstanding for the next shipment.
    if check.record_quantity is None:
        return _flag(record_id, "the email states no quantity to receive",
                     po_number=po_number, verification=verification)
    if check.record_quantity <= 0:
        return _flag(record_id,
                     f"the email states a quantity of "
                     f"{po_verify.fmt_qty(check.record_quantity)} — there is nothing to receive",
                     po_number=po_number, verification=verification)
    if check.record_quantity > check.qty_outstanding + OVER_RECEIPT_TOLERANCE:
        return _flag(record_id,
                     f"this would over-receive: the email says "
                     f"{po_verify.fmt_qty(check.record_quantity)} {check.record_uom or ''}".rstrip()
                     + f" and only {po_verify.fmt_qty(check.qty_outstanding)} "
                       f"{check.unit_of_measure} of the "
                       f"{po_verify.fmt_qty(check.qty_ordered)} ordered is still outstanding",
                     po_number=po_number, verification=verification)

    # 8. Units. `uom_agrees` is None when either side is silent, which is not disagreement —
    #    but a stated unit that contradicts the order is, and 19 EA against 19 CS is not the
    #    same delivery.
    if check.uom_agrees is False:
        return _flag(record_id,
                     f"the email says {check.record_uom} and the purchase order says "
                     f"{check.unit_of_measure}",
                     po_number=po_number, verification=verification)

    project_code = _project_of(conn, po_number)
    if not project_code:
        # Without it there is no `forProject`, and the receipt cannot be created at all.
        return _flag(record_id,
                     f"the project for purchase order {po_number} is not known locally — "
                     "verify the PO once so the mirror records it",
                     po_number=po_number, verification=verification)

    return Decision(
        record_id=record_id, verdict=POST, po_number=po_number, project_code=project_code,
        line_number=check.line_number, quantity=check.record_quantity,
        unit_of_measure=check.unit_of_measure, spec_code=check.spec_code,
        description=check.description, cost_code=_cost_code_of(conn, po_number, check),
        verification=verification,
        pod_waived_by="" if pod_md5 else str(_get(row, "pod_waived_by") or "").strip())


def _describe_existing(attempt: post_ledger.PostAttempt) -> str:
    """Why a claimed delivery is refused, in the terms a person needs to act on."""
    when = (attempt.settled_at or attempt.claimed_at or "")[:10]
    if attempt.state == post_ledger.POSTED:
        which = f" as receipt {attempt.receipt_doc_no}" if attempt.receipt_doc_no else ""
        return f"this delivery was already posted{which} on {when}"
    if attempt.state == post_ledger.POD_POSTED:
        # Not a failure and not "in flight" — a real receipt exists with the proof of delivery on
        # it, and the report is the reviewer's next step rather than something that went wrong.
        # Without this branch it fell through to the "still in flight" wording below and told
        # people a post was running when it had finished half an hour earlier.
        which = f" receipt {attempt.receipt_doc_no}" if attempt.receipt_doc_no else " the receipt"
        return (f"the proof of delivery is already on{which} — post the receiver report next, "
                f"not the POD again")
    if attempt.state == post_ledger.PARTIAL:
        return (f"a previous post on {when} half-completed and needs a person — receipt "
                f"{attempt.receipt_doc_no or attempt.receipt_key[:8]} exists but was not finished")
    return f"a post claimed on {when} is still in flight"


def _project_of(conn: sqlite3.Connection, po_number: str) -> str:
    """The project the PO lives in, from the mirror — needed as `forProject` at creation.

    Not on `RecordVerification`, which carries no project at all: the Verify popup never had a
    reason to show one. It comes from `spitfire_po_index`, which records it when the PO is first
    resolved, and that resolution is the only place the mapping exists — Spitfire will not tell us
    which project a PO belongs to (`POST /api/projects` returns an empty list for this account).
    """
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT project_code FROM spitfire_po_index WHERE po_number = ?",
                       (po_number,)).fetchone()
    return str(row["project_code"]) if row and row["project_code"] else ""


def _cost_code_of(conn: sqlite3.Connection, po_number: str,
                  check: po_verify.LineCheck) -> str:
    """`ProjEntity` for the matched line, from the mirrored PO lines.

    Read from the mirror rather than carried on `LineCheck`, which does not hold it — the Verify
    screen has no use for a cost code, and adding one there for this module's sake would widen a
    dataclass that exists to answer a different question.
    """
    if check.line_number is None:
        return ""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT cost_code FROM spitfire_po_lines WHERE po_number = ? AND line_number = ?",
        (po_number, check.line_number)).fetchone()
    return str(row["cost_code"]) if row and row["cost_code"] else ""


def _get(row: Any, name: str) -> Any:
    try:
        return row[name]
    except (IndexError, KeyError, TypeError):
        return getattr(row, name, None)


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
