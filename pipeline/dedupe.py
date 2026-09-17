"""One delivery, however many times it is described.

Premier's previous attempt at this ended because one physical delivery became two receivers. The
join that prevents it — Delivered `Authority #` == Inbound `ALS Shipment #` — is real and is
implemented in `stage2_accumulate`, but it only works when the mail states a shipment number, and
**nine of the fourteen corpus emails state none**. Everything in this module exists for the traffic
that join cannot cover.

Four things can be "the same" and they are not the same thing:

1. **The same message, arriving twice.** `seen_message_ids` keys on `internetMessageId`, which a
   vendor re-send or a second expeditor's forward does not share. `fingerprint` hashes what the
   message *says* instead of the envelope it came in.
2. **The same delivery, described by two messages.** An Inbound notification and a Delivered
   notice. `stage2_accumulate` owns this one.
3. **The same delivery, staged as two records.** `delivery_key`.
4. **The same delivery, posted twice.** `post_ledger` owns this one, keyed on `evidence_key`.

**Nothing here deletes anything.** A suppressed duplicate is recorded, linked to what it duplicates,
and shown — the same principle as the internal-chatter rule, which lists what it filtered rather
than hiding it. A suppression nobody can inspect is a suppression nobody can trust, and the failure
it hides is a real delivery that silently never arrived.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Any, Iterable, List, Optional, Sequence

BODY_PREFIX = "body:"
"""Marks an evidence key derived from an email body rather than from a proof-of-delivery file.

A literal prefix rather than a flag column because the value shares a slot with `pod_md5`, which is
32 hex characters — `body:` cannot collide with one, so a stored key always says which kind it is
and no row is ambiguous about whether a receipt carried proof.
"""

_WHITESPACE = re.compile(r"\s+")
_FORWARD_MARKERS = re.compile(r"^\s*(re|fw|fwd|automatic reply)\s*:\s*", re.IGNORECASE)

_SEP = "\x1f"
"""ASCII unit separator, used to join fields before hashing.

Not `|` or `:`, because a PO number, a spec code and a description can all legitimately contain
punctuation, and `("a|b", "c")` hashing the same as `("a", "b|c")` is a collision waiting for the
one delivery it matters on.
"""


def _norm(text: Any) -> str:
    """Collapse whitespace and case. Nothing else — this is not a content-equality judgement."""
    return _WHITESPACE.sub(" ", str(text or "")).strip().lower()


def normalize_subject(subject: Any) -> str:
    """Strip the forward and reply markers, repeatedly, then normalise.

    `Fwd: FW: RE: 939260 - Inbound Notification` and `939260 - Inbound Notification` are the same
    notification seen from different distances. Everything in this corpus arrives forwarded from
    `example-pm.test`, often twice, so a fingerprint that kept the markers would call two copies of
    one message different — which is precisely the case this exists to catch.
    """
    text = str(subject or "")
    while True:
        stripped = _FORWARD_MARKERS.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    return _norm(text)


def _digest(*parts: Any) -> str:
    """Hash a tuple of fields, unambiguously separated."""
    joined = _SEP.join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# --- 1. the same message, arriving twice --------------------------------------------------------

def fingerprint(*, subject: Any, origin_sender: Any, origin_sent_at: Any,
                attachment_hashes: Iterable[str] = ()) -> str:
    """What this message *says*, hashed — the identity `internetMessageId` fails to provide.

    The four ingredients are the ones that survive a re-send and a re-forward while still
    distinguishing two genuinely different notifications:

    * the subject, with forward markers stripped — it carries the inbound number and the POs
    * `origin_sender`, not `sender`: everything arrives from `example-pm.test`, so the envelope sender
      is the same for nearly all of it and would contribute nothing
    * `origin_sent_at`, not `email_date` — twelve of the fourteen corpus files were forwarded by one
      person on one day, so the envelope date is that day for nearly all of them and cannot separate
      a September notification from a June one
    * the attachment content hashes, sorted — filenames lie (two byte-identical PODs arrive under
      different names), so identity is content and order is not meaningful

    Deliberately **not** the body text. A forward wraps the original in a new hop, quotes it with
    different indentation, and Outlook rewrites the HTML on the way through; a body hash would
    therefore call two copies of one message different every time, which is the opposite of useful.
    """
    return _digest(normalize_subject(subject), _norm(origin_sender), _norm(origin_sent_at),
                   *sorted(str(h or "").lower() for h in attachment_hashes))


def hashes_for_email(conn: sqlite3.Connection, email_id: str) -> List[str]:
    """The content hashes of everything attached to one message.

    Read from the ledger rather than from the `RawEmail`, because by the time a fingerprint is
    taken the ledger has already expanded containers — a `.zip` of three PODs contributes three
    hashes, and two messages carrying the same zip under different names still agree.

    Inline attachments are excluded, matching every other consumer: a signature logo repeated
    across an entire mailbox would push unrelated messages towards the same fingerprint.
    """
    prior = conn.row_factory
    conn.row_factory = None
    try:
        rows = conn.execute(
            "SELECT sha256 FROM attachment_ledger "
            "WHERE email_id = ? AND COALESCE(is_inline, 0) = 0 AND COALESCE(sha256,'') <> ''",
            (email_id,)).fetchall()
    finally:
        conn.row_factory = prior
    return sorted(str(row[0]) for row in rows)


def stamp(conn: sqlite3.Connection, email_id: str, value: str,
          duplicate_of: Optional[str] = None) -> None:
    """Record a message's fingerprint, and what it duplicates if anything.

    **Must run after the `email_log` row is written, not before.** `email_log.record` writes *or
    overwrites* the whole row, so a fingerprint stamped ahead of it is silently erased — which left
    every message carrying `fingerprint = NULL` and made the duplicate lookup match nothing.

    A no-op on an empty value, because every settle path calls this and the error path has no
    fingerprint to record.

    Never raises. This is bookkeeping about an email, and losing the email to save the bookkeeping
    is the wrong trade — the same reasoning `_log_email` gives for swallowing.
    """
    if not value:
        return
    try:
        conn.execute("UPDATE email_log SET fingerprint = ?, duplicate_of = ? WHERE email_id = ?",
                     (value, duplicate_of, email_id))
        conn.commit()
    except sqlite3.Error:
        pass


def is_handled_manually(conn: sqlite3.Connection, email_id: str) -> bool:
    """Whether a person already built a record from this message.

    Scoped to the one message, never to its thread or its purchase order: the next mail on the same
    thread may be a genuinely separate delivery, and suppressing that would hide a real receipt.
    """
    row = conn.execute("SELECT handled_manually FROM email_log WHERE email_id = ?",
                       (email_id,)).fetchone()
    return bool(row and row[0])


def find_by_fingerprint(conn: sqlite3.Connection, value: str,
                        exclude_email_id: Optional[str] = None) -> Optional[sqlite3.Row]:
    """The earliest message already logged under this fingerprint, if any.

    Earliest rather than latest: what a duplicate points at should be the original, and a chain of
    each copy pointing at the one before it makes "how many copies of this arrived" unanswerable
    without walking it.
    """
    if not value:
        return None
    prior = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM email_log WHERE fingerprint = ? "
            "AND COALESCE(duplicate_of, '') = '' ORDER BY id", (value,)).fetchall()
    finally:
        conn.row_factory = prior
    for row in rows:
        if exclude_email_id is None or str(row["email_id"]) != str(exclude_email_id):
            return row
    return None


# --- 2. the same delivery, described by two messages --------------------------------------------

SHIPMENT_RUNG = "shipment"
NOTICE_RUNG = "notice"
POD_RUNG = "pod"
DATE_RUNG = "date"
MESSAGE_RUNG = "message"

IDENTIFYING_RUNGS = (SHIPMENT_RUNG, NOTICE_RUNG, POD_RUNG)
"""Rungs that name *which* delivery. Two of these that differ are two different deliveries.

A shipment number, a notice number and a proof-of-delivery hash are all identifiers issued by
somebody about one physical delivery. A date is not: two deliveries can share one, and one delivery
is dated by some of the mail about it and not by the rest.
"""

FLOATING_RUNGS = (DATE_RUNG, MESSAGE_RUNG)
"""Rungs that describe a delivery without identifying it — corroboration, not a name.

A property reply saying *"We received the items for PO 913500 today"* is evidence about a delivery
and cannot say which. Treating it as its own delivery orphans it from the Inbound notice it belongs
to; treating it as merging with everything on the PO is the nullable-key bug all over again. So it
*floats*: it accumulates under its own ref, and is absorbed by an identified delivery on the same
purchase order when one releases. If none ever does, it releases on its own through the grace
sweep, still separated from any other floating evidence by its date.
"""

CERTAIN_RUNGS = IDENTIFYING_RUNGS + (DATE_RUNG,)
"""Rungs resolved from something the mail states, for reporting rather than for joining.

`MESSAGE_RUNG` is deliberately absent: falling through to the message id does not identify a
delivery, it merely refuses to merge two messages that might be one. That is the honest answer but
it is a guess, and Stage 2 flags it rather than presenting it as a join.
"""


def delivery_ref(*, shipment_number: Any = None, notification_number: Any = None,
                 pod_sha256: Any = None, pod_stated_date: Any = None,
                 email_id: Any = None) -> "tuple[str, str]":
    """Which physical delivery a message is about, and how confidently we know.

    Returns `(ref, rung)`. The ref is scoped to a purchase order by its caller, so it only has to
    separate two deliveries *on the same PO*.

    Five rungs, most trustworthy first. Every one of them is something a real message in Premier's
    mailbox actually carries, and the order is not preference — it is what each one can prove:

    1. **The shipment number.** The A↔B join. The Inbound and Delivered halves of one delivery carry
       *different* notice numbers (939260 and 90009) and the *same* shipment number, so this has to
       stay first or the pair splits and one truck becomes two receivers — the failure that ended
       Premier's previous attempt. Only five of thirteen live delivery mails state one.
    2. **The notification number.** The notice's own reference. `TriagedEmail.notification_number`
       has carried it since triage was written and its docstring already says this is "what lets
       Stage 2 see them as one event" — it simply was never wired up. It is what recognises notice
       939475, which arrived twice under two different message ids and carries no attachment at all.
    3. **The proof of delivery's content hash.** Different paperwork, different delivery. Available
       here because `evidence.gather` parses every attachment *before* triage, so the ledger's
       `is_pod` verdict is already written by the time Stage 2 runs. Handles the corpus's two
       byte-identical PODs saved under different filenames — same hash, one delivery.
    4. **The stated delivery date.** No proof to compare, but a date is still something the message
       asserts. Two deliveries on one PO on one day with no shipment, notice or POD do merge here;
       that is the accepted cost of not splitting every re-forward into a new receiver.
    5. **The message id**, which identifies nothing — see `CERTAIN_RUNGS`.

    The rung name is part of the value, so `90009` as a shipment number and `90009` as a notice
    number cannot be mistaken for each other.
    """
    for rung, value in ((SHIPMENT_RUNG, shipment_number),
                        (NOTICE_RUNG, notification_number),
                        (POD_RUNG, pod_sha256),
                        (DATE_RUNG, pod_stated_date),
                        (MESSAGE_RUNG, email_id)):
        text = _norm(value)
        if text:
            return f"{rung}:{text}", rung
    # Nothing at all to go on — not even a message id. Its own ref rather than an empty string,
    # because an empty key would collapse every such message on a PO into one delivery, which is
    # the exact failure this ladder exists to prevent.
    return f"{MESSAGE_RUNG}:", MESSAGE_RUNG


def pod_sha_for_email(conn: sqlite3.Connection, email_id: str) -> str:
    """The hash of this message's proof of delivery, or "" if nothing on it reads as one.

    Lowest ordinal wins when a message carries several, so the answer does not depend on row order.
    Inline attachments are excluded for the same reason they are excluded from `hashes_for_email`:
    a signature logo is not proof of anything.
    """
    prior = conn.row_factory
    conn.row_factory = None
    try:
        row = conn.execute(
            "SELECT sha256 FROM attachment_ledger "
            "WHERE email_id = ? AND COALESCE(is_pod, 0) = 1 AND COALESCE(is_inline, 0) = 0 "
            "AND COALESCE(sha256,'') <> '' ORDER BY depth, ordinal LIMIT 1",
            (email_id,)).fetchone()
    finally:
        conn.row_factory = prior
    return str(row[0]) if row else ""


def pod_date_for_email(conn: sqlite3.Connection, email_id: str) -> str:
    """The delivery date this message's proof asserts, for rung 4.

    Read off the ledger verdict `stage3_extract` already recorded rather than re-parsing anything —
    the same source `record_completion` uses, so the date that identifies a delivery and the date
    that ends up on its receipt cannot disagree.
    """
    prior = conn.row_factory
    conn.row_factory = None
    try:
        row = conn.execute(
            "SELECT pod_delivery_date FROM attachment_ledger "
            "WHERE email_id = ? AND COALESCE(pod_delivery_date,'') <> '' "
            "ORDER BY depth, ordinal LIMIT 1",
            (email_id,)).fetchone()
    finally:
        conn.row_factory = prior
    return str(row[0]) if row else ""


def ref_for_email(conn: sqlite3.Connection, email_id: str, *,
                  shipment_number: Any = None,
                  notification_number: Any = None) -> "tuple[str, str]":
    """`delivery_ref` for one message, reading rungs 3 and 4 off the ledger."""
    return delivery_ref(
        shipment_number=shipment_number,
        notification_number=notification_number,
        pod_sha256=pod_sha_for_email(conn, email_id),
        pod_stated_date=pod_date_for_email(conn, email_id),
        email_id=email_id)


# --- 3. the same delivery, staged as two records ------------------------------------------------

def delivery_key(*, po_number: Any, line_number: Any = None, spec_code: Any = None,
                 quantity: Any = None, pod_stated_date: Any = None,
                 pod_sha256: Any = None) -> str:
    """The physical delivery a record describes, hashed.

    Line number *or* spec code, in that order: the line is what Spitfire books against and is
    exact, but most mail does not state one, and a spec code identifies the same goods well enough
    to catch a re-extraction. Falling back is what makes this usable on real traffic rather than on
    the seventeen records that happen to carry a line.

    The quantity is in the key on purpose. **Two deliveries against one PO line are ordinary** —
    PO 908491 line 300 took 11 pieces on 1 October and 1 more on the 9th — so a key without it
    would merge two genuine partial deliveries into one and lose the second.
    """
    return _digest(_norm(po_number),
                   "" if line_number in (None, "") else int(line_number),
                   "" if line_number not in (None, "") else _norm(spec_code),
                   "" if quantity in (None, "") else f"{float(quantity):.4f}",
                   _norm(pod_stated_date),
                   _norm(pod_sha256))


def _get(row: Any, name: str) -> Any:
    """Read a field from a sqlite3.Row, a mapping, or an ExtractedRecord alike."""
    if isinstance(row, dict):
        return row.get(name)
    try:
        return row[name]
    except (IndexError, KeyError, TypeError):
        return getattr(row, name, None)


def key_for_row(row: Any, *, pod_sha256: str = "") -> str:
    """`delivery_key` for an `extracted_records` row or an `ExtractedRecord`."""
    return delivery_key(
        po_number=_get(row, "po_number"),
        line_number=_get(row, "po_line_number"),
        spec_code=_get(row, "spec_code"),
        quantity=_get(row, "quantity_received"),
        pod_stated_date=_get(row, "pod_stated_date"),
        pod_sha256=pod_sha256)


BLOCKING_STATUSES = ("pending", "matched", "pushed_to_spitfire")
"""The statuses a record has to be in for it to stand in the way of staging the same delivery again.

`failed` is deliberately absent. A record that failed is not a record of anything — it is a row
saying an attempt did not work — and letting it block meant a delivery that failed once could never
be staged again, which is the opposite of what a failure should cost. The same reasoning is already
in `post_ledger.BLOCKING`, which excludes `FAILED` and `FLAGGED` for exactly this reason: a guard
against duplicates must not become a guard against retries.
"""


def find_by_delivery_key(conn: sqlite3.Connection, key: str,
                         exclude_id: Optional[int] = None) -> Sequence[sqlite3.Row]:
    """Records already staged for this same delivery, oldest first.

    Only records still standing — see `BLOCKING_STATUSES`. Without that filter this scanned the
    whole table, so a `failed` row went on blocking a fresh staging of the delivery it failed to
    record.
    """
    if not key:
        return []
    prior = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ", ".join("?" * len(BLOCKING_STATUSES))
        rows = conn.execute(
            f"SELECT * FROM extracted_records WHERE delivery_key = ? "
            f"AND status IN ({placeholders}) ORDER BY id",
            (key, *BLOCKING_STATUSES)).fetchall()
    finally:
        conn.row_factory = prior
    return [r for r in rows if exclude_id is None or int(r["id"]) != int(exclude_id)]


# --- 4. the same delivery, posted twice ---------------------------------------------------------

def evidence_key(*, pod_md5: str = "", body_text: Any = None, email_id: Any = None) -> str:
    """What the ledger keys a posted delivery on: the POD's hash, or the email that stood in for it.

    `post_ledger.find_delivery` asks "(PO, line, POD hash) — has this posted?", and that hash is
    what makes two receipts against one line distinguishable. **A body-only delivery has no hash**,
    so every one of them for the same PO and line would key alike, or to nothing — and the guard
    that stops one delivery becoming two receipts would be blind for exactly the traffic that has
    no attachment to check.

    So when there is no POD the key is derived from the message that asserted the delivery instead.
    The body text and the message id together, because two Inbound notifications on the same PO can
    legitimately carry near-identical bodies, and the id separates them where the text does not.

    **Not** a hash of a rendered PDF of the email: headless Chrome stamps its own metadata, so the
    same body produces different bytes on every render, and the key would change under a row that
    had already posted. The text does not move.

    When a POD *is* present this returns `pod_md5` unchanged, so every row already in
    `spitfire_post` keeps matching and no existing claim is orphaned.
    """
    if pod_md5:
        return pod_md5
    return BODY_PREFIX + _digest(_norm(body_text), _norm(email_id))


def is_body_evidence(key: Any) -> bool:
    """Whether a stored ledger key stands for an email body rather than a proof-of-delivery file."""
    return str(key or "").startswith(BODY_PREFIX)
