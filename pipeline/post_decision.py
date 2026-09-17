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
from typing import Any, Dict, List, Optional, Sequence

from pipeline import authorship, completeness, dedupe, post_ledger, po_verify

POST = "post"
FLAG = "flag"

OVER_RECEIPT_TOLERANCE = 0.0
"""How far **past what is still owed** a delivery may go before it needs a person.

This used to be `QUANTITY_TOLERANCE`, compared against `qty_ordered` with an equality test, and
that was wrong in a way that rejected most real traffic: it demanded every delivery satisfy the
whole order in one go, so receiving 2 against 19 ordered was refused permanently with no way to
accept it. Partial deliveries are ordinary — PO 912559's own lines are 4/4/2/2 across separate
shipments.

The rule is now "anything from 1 up to what is outstanding", and this constant governs only the
other end: booking *more* than was ordered. Zero is a defensible default for that, where zero was
never a defensible default for "is this the full order". An over-receipt is a real event — 202
delivered against 196 ordered is in the corpus — but it is a conversation with the vendor, not
something to book unattended.
"""


POD_DATE_FALLBACKS = ("email_received_date", "email_sent_date")
"""`pod_source` values meaning *we* supplied the delivery date, because the document did not.

`ingest_orchestrator._fallback_pod_date` stamps these under Premier's 2026-09-03 rule and says why
they must not be trusted on their own: *"This is an estimate standing in for a fact, so the caller
records which one it used, and posting still wants a linked proof of delivery, a `pod_waived_by`
waiver, or body evidence - a fallback date cannot smuggle anything into Spitfire on its own."*

`document_evidence` is that sentence enforced. Measured on the live store, 58 of the 185 records on
the Records page carry a fallback date; without this exclusion every one of them would post on a
date that is only the day the email landed.
"""

DELIVERY_DOCUMENT_SOURCES = ("pdf", "ocr", "docx",
                             "authority_delivered", "authority_inbound")
"""`extraction_source` families that are a delivery document rather than a worklist about one.

Matched on the part before the first colon, so `pdf:text` counts and `excel:Hoja1` does not.

**The exclusions are the point.** Every `excel:*` source in this store is an expediting or
confirmation worklist - `excel:Expediting`, `excel:qPOExpeditor`, `excel:Combined Forecast`,
`excel:Hoja1` - and a worklist asks whether goods arrived rather than saying they did. Measured
2026-09-10: admitting them would have passed 32 records read out of one message whose subject is
literally *"Delivery Confirmation Required"*, forwarded into the mailbox and so externally authored.
That is the failure `body_evidence`'s own docstring recorded as `excel:Hoja1 0/57`, and the
authorship gate cannot catch it - the message really did come from outside.

`freetext`, `html*` and `manual` are left out for the same reason in weaker form: none of them is a
document asserting a delivery. A manually created record already chose a proof or waived one at
creation, so it has no need of this route.
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

    line_key: str = ""
    """`DocItemKey` of the matched purchase order line — the GUID Spitfire uses to build the
    receipt, and what `spitfire_write.find_prepopulated_line` matches on to find the row to write
    the quantity into. Read from the mirror rather than carried on `LineCheck`, for the same reason
    as `cost_code`: the Verify screen has no use for it."""

    verification: Optional[po_verify.RecordVerification] = None
    existing: Optional[post_ledger.PostAttempt] = None

    body_evidence: str = ""
    """Which signal stood in for a proof document, when there is none.

    `"signer+date"`, `"carrier+tracking+date"` or `"document+date"` — see `body_evidence()`. Empty whenever a POD
    exists, and empty on a record that reached POST through a person's waiver instead, so the two
    routes past gate 2 are always distinguishable in the ledger and in the confirm dialog."""

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


def body_evidence(row: Any, *, origin_sender: Optional[str] = None) -> str:
    """The delivery evidence carried in the email body itself, or "" when there is none.

    Premier's decision, 2026-08-22: a delivery whose particulars are all stated in the mail may
    post without a proof document. This is what "all stated in the mail" is allowed to mean, and
    it is deliberately narrower than "the record is complete".

    Completeness alone would be the wrong test. 57 of the 92 records in the corpus come from
    `Public Space - Pending Receipt Confirmation Orders.xlsx` and `Property Receivers.xlsx` — Premier's
    own worklists of goods *awaiting* delivery. They are complete, they carry quantities and dates,
    and nothing has arrived. Posting them would assert receipt of goods still in transit.

    So the test is for evidence that someone took delivery: a person who signed for it, or a
    carrier and a tracking number that can be checked against the carrier. Plus the date, because
    a receipt has to state when.

    Measured over the corpus, this admits exactly the sources that are genuine delivery
    notifications and nothing else:

        authority_delivered      10/10   carrier+tracking+date
        authority_inbound         5/5    signer+date
        pdf:carrier_pod           4/4    signer+date
        html:confirmation_grid    1/5
        excel:Hoja1               0/57    <- the pending-confirmation worklists
        freetext                  0/10

    Read only from fields the parsers extracted, never from prose. A signal this returns is a fact
    the extraction already committed to, which is what makes it auditable after the fact.

    **A third route was added 2026-09-11** — see `document_evidence`, tried last. The two tests
    above are a 2026-08-22 proxy for "somebody outside Premier said so", and the confirmation gate
    added on 2026-09-07 now asks that directly. The measurement above still holds under it: the 57
    `excel:Hoja1` worklist rows are refused there too, by source rather than by silence.
    """
    if not str(_get(row, "pod_stated_date") or "").strip():
        return ""
    if str(_get(row, "received_by") or "").strip():
        return "signer+date"
    if (str(_get(row, "carrier_name") or "").strip()
            and str(_get(row, "tracking_number") or "").strip()):
        return "carrier+tracking+date"
    return document_evidence(row, origin_sender=origin_sender)


def document_evidence(row: Any, *, origin_sender: Optional[str] = None) -> str:
    """`"document+date"` where a delivery *document* from outside Premier states the date, else `""`.

    **Why this was added, 2026-09-11.** The signer/carrier tests above were written on 2026-08-22 as
    a proxy for one question: did somebody outside Premier say these goods arrived. They were the
    only instrument available, and they are a poor one - measured on the live store, 89 of 4,717
    records name a signer and 97 name a carrier, so the proxy refuses about 96% of the corpus
    including every delivery note that simply does not print a name.

    Since 2026-09-07 that question is asked directly and far better, one layer up:
    `read_views._awaits_confirmation` withholds every record read out of Premier's own mail until a
    person confirms it, keyed on `email_log.origin_sender` - the oldest hop of the thread, not the
    envelope, because every message here is forwarded. All 185 records on the Records page have
    already cleared it. Requiring the old proxy *as well* was asking the same question twice and
    taking the worse answer.

    So this route replaces the proxy with the three things that actually have to be true:

    1. **Somebody outside Premier wrote it.** Not merely "not resolvably internal" - the origin has
       to be known and external. `authorship.authored_internally` treats an unresolvable sender as
       external on purpose, so that a message we know nothing about is not silently withheld from a
       *queue*; that default is right there and wrong here, where the answer permits a write to an
       ERP. Unknown fails closed.
    2. **The date is stated, not supplied by us.** See `POD_DATE_FALLBACKS`.
    3. **It came off a delivery document, not a worklist.** See `DELIVERY_DOCUMENT_SOURCES`.

    Measured before and after on the Records page: 54 records passed `body_evidence`, 128 pass it
    now, and the 32 "Delivery Confirmation Required" spreadsheet rows are refused by rule 3 while
    the 58 fallback-dated rows are refused by rule 2. Those still need a person - which is what the
    waiver is for, and why it must stay reachable.

    Read only from stored fields, like the rest of this module: a signal returned here is a fact
    extraction already committed to, which is what makes it auditable afterwards.
    """
    if not str(_get(row, "pod_stated_date") or "").strip():
        return ""

    sender = origin_sender if origin_sender is not None else _get(row, "origin_sender")
    sender = str(sender or "").strip()
    if not sender or authorship.authored_internally(sender):
        return ""

    if str(_get(row, "pod_source") or "").strip() in POD_DATE_FALLBACKS:
        return ""

    source = str(_get(row, "extraction_source") or "").strip().lower()
    if "quantity_conflict" in source:
        return ""
    if source.split(":", 1)[0] not in DELIVERY_DOCUMENT_SOURCES:
        return ""
    return "document+date"


def can_post_offline(row: Any, *, has_pod_bytes: bool,
                     origin_sender: Optional[str] = None) -> str:
    """Why this record may be posted without asking Spitfire anything, or `""` if it may not.

    Gate 2 of `decide`, asked as a question rather than answered as a refusal — because a *list*
    needs the same answer and cannot pay for the network. Returns the reason (`"pod"`, `"waived"`,
    `"signer+date"`, `"carrier+tracking+date"`, `"document+date"`) so a screen can say which of the
    four routes applies, rather than only that something is allowed.

    `origin_sender` is threaded through for `document_evidence`, which needs to know who wrote the
    message. Left None it is read off the row — `read_views._records_pending` projects it, so the
    Records page needs to pass nothing. A projection without the column (`deliveries_store.lines_for`
    is `SELECT *` over `extracted_records`, which has no such column) answers `""` for that route
    rather than guessing, so `decide` resolves it explicitly and the two cannot drift.

    **This exists because the Records page had its own, different test.** It asked whether the
    email carried anything that could be a POD (`_emails_with_a_possible_pod`) and, when it did
    not, drew a "waive POD" prompt. That test does not know about `body_evidence` — Premier's
    2026-08-22 route for a delivery stated entirely in the mail — so 11 records that this gate
    would have accepted were sitting behind a prompt asking someone to waive a proof that was
    never required. A second definition of a rule is a second answer to it.

    `has_pod_bytes` is a parameter rather than a ledger read: the page resolves it for every row in
    one query, and looking it up per row here would reintroduce the N+1 that query exists to avoid.

    Deliberately *offline* and deliberately only gate 2. Gates 3 and 4 — already posted, and the
    live purchase-order comparison — need I/O, and a list view that paid for them would cost about
    145 seconds. A row this returns a reason for is one the Post button will offer; whether it
    lands is still the confirm dialog's live question to answer.
    """
    if has_pod_bytes:
        return "pod"
    if str(_get(row, "pod_waived_by") or "").strip():
        return "waived"
    return body_evidence(row, origin_sender=origin_sender)


def line_refusal(check: Any, po_number: str) -> str:
    """Gates 5-8 against one matched purchase-order line: the reason to refuse, or empty.

    Split out of `decide` so a *list* view can ask the same question. `decide` reads the order
    live; the Records page reads `spitfire_mirror`, which costs 0.04s for 385 rows where a live
    read of the same rows costs about 145 seconds. Both then rule on the result here, so the
    count on the page and the verdict on the button cannot drift apart — the same drift
    `can_post_offline` was written to end, one gate further down.

    `check` is `po_verify.verify_record(...).matched`. It says nothing about whether a proof of
    delivery exists (gate 2) or whether this delivery already posted (gate 3); those are asked
    elsewhere and are deliberately not re-asked here.
    """
    # 5. The line must have been resolved by its spec code — or chosen by a person.
    #    A fuzzy description match is a weaker claim than anything writing to an ERP should rest
    #    on. A reviewer picking a line from the alternatives table is a *stronger* one, and
    #    refusing it as "matched on description alone" was both a block on the only recovery path
    #    there is and a false statement about what happened.
    if not (check.spec_resolved or check.reviewer_chose):
        return (f"the line was matched on description alone, not on a spec code — "
                f"{check.description[:60] or 'this line'} needs a person to confirm it")

    # 6. A line with nothing left. Checked before the quantity comparison so the sentence names
    #    the actual situation — "this line is already fully received" is what a reviewer needs to
    #    read, not "you would over-receive by 19", which is technically true and unhelpful.
    #    Spitfire's own numbers lag until approval, so this is a weak signal that only ever fires
    #    when the ERP is certain; our ledger is what stops the repeat it cannot see.
    if check.qty_outstanding <= 0:
        return (f"purchase order {po_number} shows nothing outstanding on this line "
                f"({po_verify.fmt_qty(check.qty_received)} of "
                f"{po_verify.fmt_qty(check.qty_ordered)} already received)")

    # 7. Quantities, measured against what is still owed rather than against the whole order.
    #    A delivery of part of a line is an ordinary receipt, not a discrepancy: it books what
    #    arrived and leaves the rest outstanding for the next shipment.
    if check.record_quantity is None:
        return "the email states no quantity to receive"
    if check.record_quantity <= 0:
        return (f"the email states a quantity of "
                f"{po_verify.fmt_qty(check.record_quantity)} — there is nothing to receive")
    if check.record_quantity > check.qty_outstanding + OVER_RECEIPT_TOLERANCE:
        return (f"this would over-receive: the email says "
                f"{po_verify.fmt_qty(check.record_quantity)} {check.record_uom or ''}".rstrip()
                + f" and only {po_verify.fmt_qty(check.qty_outstanding)} "
                  f"{check.unit_of_measure} of the "
                  f"{po_verify.fmt_qty(check.qty_ordered)} ordered is still outstanding")

    # 8. Units. `uom_agrees` is None when either side is silent, which is not disagreement —
    #    but a stated unit that contradicts the order is, and 19 EA against 19 CS is not the
    #    same delivery.
    if check.uom_agrees is False:
        return (f"the email says {check.record_uom} and the purchase order says "
                f"{check.unit_of_measure}")
    return ""


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
    # Resolved here, once, and passed explicitly into every gate-2 question below. Not left to be
    # read off `row`: the Records page passes rows from `read_views._records_pending`, which
    # projects `origin_sender`, while the grouped post passes rows from
    # `deliveries_store.lines_for` (`SELECT *` over `extracted_records`), which cannot. Reading it
    # off the row would make the same delivery answer differently depending on which screen asked —
    # the button offering a line the gate then refuses, which is the drift `can_post_offline` was
    # written to end.
    origin_sender = _origin_sender(conn, row)

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
        # Premier's decision, 2026-08-22: a delivery whose particulars are all stated in the mail
        # may post without a proof document. `body_evidence` is deliberately narrower than "the
        # record is complete" — it requires a signer or a carrier reference, which is what
        # separates a delivery notification from a row on a pending-confirmation spreadsheet. The
        # objection above still stands for everything it does not admit.
        #
        # Asked through `can_post_offline` so the Records page asks the identical question. It
        # used to ask its own, narrower one and hid the Post button on 11 records this gate would
        # have accepted.
        if not can_post_offline(row, has_pod_bytes=False, origin_sender=origin_sender):
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
    #    on it let the same delivery post again under a new number. PO 912559 collected eight
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
        # Spitfire answered and holds no such purchase order — `po_verify.read` turns a lookup
        # that could not complete into `verification.error` above, so reaching here means this is
        # their data and not our connection. Said plainly for that reason: the old wording named
        # our three configured project ids, which described our configuration to somebody who can
        # do nothing about either.
        #
        # No receipt can be created without a `forProject`, so this is refused like any other
        # blocked record — but it is not work anybody can finish, and `read_views` keeps it off
        # the queue of records a person is asked to correct.
        return _flag(record_id,
                     f"Spitfire has no purchase order {po_number} — the delivery is recorded and "
                     f"appears on the receiver report, but there is no order to receive it against",
                     po_number=po_number, verification=verification)

    check = verification.matched
    if check is None:
        return _flag(record_id,
                     f"no line on purchase order {po_number} matched this delivery",
                     po_number=po_number, verification=verification)

    # 5-8. The line itself: resolved, still owed, the quantity, the unit. Asked through
    #      `line_refusal` so a list view can ask the identical question against the mirror
    #      without holding a second copy of these rules.
    refusal = line_refusal(check, po_number)
    if refusal:
        return _flag(record_id, refusal, po_number=po_number, verification=verification)

    project_code = _project_of(conn, po_number)
    if not project_code:
        # Without it there is no `forProject`, and the receipt cannot be created at all.
        return _flag(record_id,
                     f"the project for purchase order {po_number} is not known locally — "
                     "verify the PO once so the mirror records it",
                     po_number=po_number, verification=verification)

    facts = _line_facts(conn, po_number, check)
    return Decision(
        record_id=record_id, verdict=POST, po_number=po_number, project_code=project_code,
        line_number=check.line_number, quantity=check.record_quantity,
        unit_of_measure=check.unit_of_measure, spec_code=check.spec_code,
        description=check.description, cost_code=facts["cost_code"], line_key=facts["line_key"],
        verification=verification,
        pod_waived_by="" if pod_md5 else str(_get(row, "pod_waived_by") or "").strip(),
        body_evidence="" if (pod_md5 or str(_get(row, "pod_waived_by") or "").strip())
                      else body_evidence(row, origin_sender=origin_sender))


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


def _origin_sender(conn: sqlite3.Connection, row: Any) -> str:
    """Who *wrote* this record's message, for `document_evidence`.

    Taken from the row where the projection carries it and read from `email_log` otherwise, so
    every caller gets the same answer for the same record whatever query it arrived through.

    `origin_sender`, never `sender`: every message in this mailbox is forwarded, so the envelope
    says `premierpm.com` on a warehouse receiving report too. `pipeline/authorship.py` carries the
    full reasoning, and `read_views._awaits_confirmation` keys on the same column.
    """
    carried = _get(row, "origin_sender")
    if carried is not None:
        return str(carried or "").strip()

    email_id = str(_get(row, "source_email_id") or "").strip()
    if not email_id:
        return ""
    found = conn.execute("SELECT COALESCE(origin_sender, '') FROM email_log WHERE email_id = ?",
                         (email_id,)).fetchone()
    return str(found[0] or "").strip() if found else ""


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


def _line_facts(conn: sqlite3.Connection, po_number: str,
                check: po_verify.LineCheck) -> Dict[str, str]:
    """`ProjEntity` and `DocItemKey` for the matched line, from the mirrored PO lines.

    Read from the mirror rather than carried on `LineCheck`, which holds neither — the Verify
    screen has no use for a cost code or a line key, and adding them there for this module's sake
    would widen a dataclass that exists to answer a different question.

    One query for both, because they are always wanted together and always come from the same row.
    """
    if check.line_number is None:
        return {"cost_code": "", "line_key": ""}
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT cost_code, line_key FROM spitfire_po_lines WHERE po_number = ? AND line_number = ?",
        (po_number, check.line_number)).fetchone()
    if row is None:
        return {"cost_code": "", "line_key": ""}
    return {"cost_code": str(row["cost_code"] or ""), "line_key": str(row["line_key"] or "")}


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
