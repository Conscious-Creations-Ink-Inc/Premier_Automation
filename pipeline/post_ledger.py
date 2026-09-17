"""One durable row per attempt to post a delivery to Spitfire. The duplicate guard lives here.

**Spitfire cannot tell us whether a delivery has already been received, so this table is the only
thing that can.** Three measurements, all from the training instance:

* The catalog does not deduplicate. On 2026-08-14 the same 37,352-byte PDF was uploaded twice and
  produced two different fileKeys and two separate catalog entries.
* `POST /api/document/{id}/attachments` has no idempotency either — the identical body twice
  gives two rows, 200 both times.
* `RelatedLineDetails.ReceiptInProgressUnits` — the field whose own schema calls it *"units
  tentatively received not yet approved"*, and the obvious thing to build a guard on — reads
  **0.0** on a PO carrying an unapproved receipt. Measured on PO 907030, which has a receipt with
  a line of qty 1.0 against it and still reports `received 0.0 / inProgress 0.0`.

That last one is the trap: quantities appear to roll up only on approval, and receipts we create
are deliberately never routed, so the window in which Spitfire looks untouched is not seconds but
however long a human takes. A guard that reads Spitfire's numbers would post a second receipt
every time.

**The claim is written before the first call, not after the last.** A crash mid-chain then leaves
a row at `CLAIMED`, which is visible and recoverable by reading the receipt back; the opposite
order would lose the record of a receipt that exists, and the retry would create another.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

# --- States ------------------------------------------------------------------------------------

CLAIMED = "claimed"
"""Reserved, nothing sent yet. Never a resting state — a row still here after a run means the
chain died partway and Spitfire may hold a partial receipt."""

POSTED = "posted"          # every call landed and the read-back confirmed it
PARTIAL = "partial"        # the receipt exists but a later step failed; needs a human
FAILED = "failed"          # nothing landed, or the failure came before anything was created

POD_POSTED = "pod_posted"
"""The receipt exists, the proof of delivery is on it and hash-verified, the report is not built.

A resting state, unlike CLAIMED, and a deliberate one: posting is two steps a person takes
separately, so the gap between them is normal rather than evidence of a crash. It is *not* PARTIAL
— that word means a step failed and a human has to work out what — whereas this means the half
that was asked for succeeded and the other half has not been asked for yet.

It still blocks, because what it blocks is a *second* POD post: the receipt already exists, and
running that step again would create another one beside it. `post_report` therefore looks for this
state by name rather than asking `is_blocking`, which would refuse the very step it is waiting for.
"""

FLAGGED = "flagged"
"""The gate refused it, and nothing was sent.

Recorded rather than merely returned. Without a row the reason lived only in the dialog the
reviewer closed, and the next person to look at the record saw an untouched Post button and had to
click it — costing a live purchase-order read — to learn what the last person already knew. A
refused write to Premier's ERP had less accounting than a dropped attachment, which
`attachment_ledger` exists precisely to prevent.
"""

TERMINAL = (POSTED, PARTIAL, FAILED, FLAGGED, POD_POSTED)

# A claim that blocks another attempt. FAILED does not: a post that never created anything is
# free to be retried once whatever broke is fixed. PARTIAL does block, because retrying it would
# add a second receipt alongside the half-built one.
#
# FLAGGED deliberately does not block either, and that is the whole point of the state. Records
# are refused for things that get fixed — a missing receiver, a quantity nobody had confirmed, a
# line matched to the wrong item — and the moment the record is corrected it must post with
# nothing to clear by hand. A flag that had to be dismissed would become a second queue nobody
# tends.
#
# POD_POSTED blocks for the same reason PARTIAL does — the receipt exists, so re-running the POD
# step would build a second one. The report step is offered by looking for that state by name.
BLOCKING = (CLAIMED, POSTED, PARTIAL, POD_POSTED)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def idempotency_key(record_id: int, po_number: str, line_number: Optional[int],
                    pod_md5: str) -> str:
    """What makes two attempts "the same delivery".

    All four parts matter. The record alone is not enough — a corrected re-extraction produces a
    new record for a delivery already posted. The PO and line pin *which* commitment is being
    received against, since one email can deliver several lines of the same order. And the POD's
    content hash distinguishes a genuine second shipment on the same line from a re-run of the
    first: same paperwork means same delivery, different paperwork means a new one.

    Hashed rather than concatenated so the column has a fixed width and an unusual spec code or a
    PO with a slash in it cannot collide with a delimiter.
    """
    parts = f"{record_id}|{po_number.strip().upper()}|{line_number if line_number is not None else ''}|{pod_md5.upper()}"
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


@dataclass
class PostAttempt:
    """A row, as the UI and the orchestrator want to read it."""
    id: int
    idempotency_key: str
    record_id: int
    po_number: str
    line_number: Optional[int]
    state: str
    detail: str
    receipt_key: str
    receipt_doc_no: str
    pod_file_key: str
    report_file_key: str
    claimed_at: str
    settled_at: Optional[str]
    attempts: int = 1
    last_attempt_at: Optional[str] = None
    group_key: str = ""
    """Which one attempt to build a receipt this row was part of. Empty on every row written before
    grouping existed, and on every single-record post — both are one row that is its own group."""

    @property
    def is_blocking(self) -> bool:
        return self.state in BLOCKING


def _row_to_attempt(row: sqlite3.Row) -> PostAttempt:
    return PostAttempt(
        id=row["id"], idempotency_key=row["idempotency_key"], record_id=row["record_id"],
        po_number=row["po_number"], line_number=row["line_number"], state=row["state"],
        detail=row["detail"] or "", receipt_key=row["receipt_key"] or "",
        receipt_doc_no=row["receipt_doc_no"] or "", pod_file_key=row["pod_file_key"] or "",
        report_file_key=row["report_file_key"] or "", claimed_at=row["claimed_at"],
        settled_at=row["settled_at"],
        attempts=row["attempts"] if "attempts" in row.keys() else 1,
        last_attempt_at=row["last_attempt_at"] if "last_attempt_at" in row.keys() else None,
        # Same guard as the two above, and for the same reason: a row selected from a store that
        # predates the column has no such key, and reading it raises rather than returning None.
        group_key=(row["group_key"] or "") if "group_key" in row.keys() else "")


def find(conn: sqlite3.Connection, key: str) -> Optional[PostAttempt]:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM spitfire_post WHERE idempotency_key = ?", (key,)).fetchone()
    return _row_to_attempt(row) if row else None


def find_delivery(conn: sqlite3.Connection, po_number: str, line_number: Optional[int],
                  pod_md5: str) -> Optional[PostAttempt]:
    """A blocking attempt for this *delivery*, whichever record it was posted from.

    `idempotency_key` includes the record id, and that is right for claiming — two operators on
    one record must not both win. It is wrong for asking "has this delivery already been posted",
    because the record id is the one part of the key that changes for reasons having nothing to do
    with the delivery: reprocessing a mail erases records and re-extracts them under new ids, and
    a corrected re-extraction produces a new id for goods already received.

    That gap is not hypothetical. PO 912559 accumulated **eight** receipts on the training
    instance, each from a re-extraction of the same delivery, because every one hashed to a new
    key and nothing else could see they were the same. Spitfire cannot answer this either —
    `ReceiptInProgressUnits` reads 0.0 against an unapproved receipt, which is exactly what we
    create — so the ledger is the only thing that can.

    The triple (PO, line, POD hash) is what the key's own docstring already claims to mean: same
    paperwork on the same line is the same delivery; different paperwork is a new one.
    """
    conn.row_factory = sqlite3.Row
    placeholders = ", ".join("?" * len(BLOCKING))
    if line_number is None:
        clause, params = "line_number IS NULL", []
    else:
        clause, params = "line_number = ?", [line_number]
    row = conn.execute(
        f"""SELECT * FROM spitfire_post
             WHERE po_number = ? AND pod_md5 = ? AND {clause}
               AND state IN ({placeholders})
             ORDER BY id DESC LIMIT 1""",
        [po_number.strip(), pod_md5.upper(), *params, *BLOCKING]).fetchone()
    return _row_to_attempt(row) if row else None


def existing_for_record(conn: sqlite3.Connection, record_id: int) -> List[PostAttempt]:
    """Every attempt against a record, newest first. The Records page uses this to show a row's
    posting history rather than only its latest state."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM spitfire_post WHERE record_id = ? ORDER BY id DESC", (record_id,)
    ).fetchall()
    return [_row_to_attempt(r) for r in rows]


def new_group_key() -> str:
    """A fresh identity for one attempt to build one receipt.

    Minted per attempt rather than derived from the delivery, and that is deliberate. A line held
    back today and fixed tomorrow becomes a **second** receipt on the same delivery — a posted
    receipt is never reopened, because Premier may already have approved it and `DELETE` is refused
    — so one delivery legitimately owns several groups over its life. Deriving this from
    `delivery_id` would file both receipts under one key and make the second invisible.
    """
    return uuid.uuid4().hex


def existing_for_group(conn: sqlite3.Connection, group_key: str) -> List[PostAttempt]:
    """Every row claimed under one attempt, oldest first. Empty for an empty key.

    The empty string is not a group. Rows written before grouping existed all carry it, and treating
    that as a group would return the whole table as one receipt.
    """
    if not group_key:
        return []
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM spitfire_post WHERE group_key = ? ORDER BY id", (group_key,)).fetchall()
    return [_row_to_attempt(r) for r in rows]


def for_receipt(conn: sqlite3.Connection, receipt_key: str) -> List[PostAttempt]:
    """Every row describing one receipt, oldest first.

    `group_key` is the identity of an *attempt*; this is the identity of the *document*. They agree
    on everything that reached Spitfire, and differ for a group that died before `create_receipt`,
    which has a group and no receipt. The UI groups by this one — what a person is looking at is a
    receipt, not an attempt.
    """
    if not receipt_key:
        return []
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM spitfire_post WHERE receipt_key = ? ORDER BY id", (receipt_key,)).fetchall()
    return [_row_to_attempt(r) for r in rows]


def settle_group(conn: sqlite3.Connection, keys: Sequence[str], state: str, detail: str = "",
                 audit: Optional[Sequence[Dict[str, Any]]] = None) -> List[str]:
    """Close every attempt in a group, returning the keys that could not be closed.

    **Each row is settled in its own transaction, and a failure on one does not stop the rest.**
    The alternative — one statement over all twenty — sounds tidier and is worse: if it raises
    halfway, the rows it did not reach stay `CLAIMED` *and* the caller has no idea which those are.
    A row left at `CLAIMED` is the safe direction to fail in, because `CLAIMED` is in `BLOCKING`, so
    nothing will post a second receipt over it, and `stranded()` lists it for
    `spitfire_post.evidenced_state` to settle by reading Spitfire back.

    Returning the failures rather than raising lets the caller say "17 settled, 3 need recovery"
    instead of losing the 17 that worked.
    """
    unsettled: List[str] = []
    for key in keys:
        try:
            settle(conn, key, state, detail, audit)
        except Exception:                                          # noqa: BLE001 — see docstring
            unsettled.append(key)
    return unsettled


# What a record's state *is*, when its rows disagree. Lower sorts first and wins.
#
# Not the newest row. `MAX(id)` was the original rule and it inverted the answer: record 221's POD
# posted as receipt 0008, a later click added a `flagged` row saying "still in flight", and the
# newer flag shadowed the receipt — so the Records page offered "Post POD · blocked" on a delivery
# whose POD was already in Premier's ERP, and the Post report button could never appear.
#
# Two rows for one record is not a bug to be prevented, either: the early gates refuse before a PO
# line is known, so a refusal keys on `line=None` while the eventual success keys on `line=3`.
# They are genuinely different keys for the same delivery, and ranking is what reconciles them.
_STATE_RANK = {
    PARTIAL: 0,      # a half-built receipt on Premier's instance outranks everything — it needs a person
    POSTED: 1,
    CLAIMED: 3,      # in flight right now
    FLAGGED: 4,      # only what a record is when nothing better happened to it
    FAILED: 5,
}
_DEFAULT_RANK = 2
"""Anything not named above — `POD_POSTED` and any state added later — sorts between POSTED and
CLAIMED. Deliberately generous: an unrecognised state means work reached Spitfire, and treating it
as more significant than a flag is the safe direction to be wrong in."""


def _rank(state: str) -> int:
    return _STATE_RANK.get(state, _DEFAULT_RANK)


def latest_by_record(conn: sqlite3.Connection) -> List[PostAttempt]:
    """One attempt per record — the one that says what actually happened to it.

    A query rather than a call per row: the page already refuses to verify every record live
    because that costs about 145 seconds, and it should not reintroduce the same shape in sqlite.
    Ranking is done in Python rather than in SQL because the order is a judgement about meaning,
    and a CASE expression buried in a query is where that judgement goes to hide.
    """
    conn.row_factory = sqlite3.Row
    best: Dict[int, sqlite3.Row] = {}
    for row in conn.execute("SELECT * FROM spitfire_post ORDER BY id"):
        current = best.get(row["record_id"])
        # `<=` so a later row wins ties: two flags on one record should show the newer reason.
        if current is None or _rank(row["state"]) <= _rank(current["state"]):
            best[row["record_id"]] = row
    return [_row_to_attempt(r) for r in best.values()]


def posted_po_numbers(conn: sqlite3.Connection) -> set:
    """Purchase orders whose final receipt we submitted to Spitfire — state POSTED only.

    POSTED means the POD and the receiver report both landed and were read back. POD_POSTED (report
    still due), PARTIAL, CLAIMED, FLAGGED and FAILED are not a final receipt, so they are left out.
    One query for the Delivery status page and its CSV.
    """
    rows = conn.execute(
        "SELECT DISTINCT po_number FROM spitfire_post WHERE state = ?", (POSTED,)).fetchall()
    return {str(r[0]).strip() for r in rows}


def latest_for_record(conn: sqlite3.Connection, record_id: int) -> Optional[PostAttempt]:
    """`latest_by_record` for one record — the attempt that says what happened to it.

    Same ranking, one row's worth of work. A page drawing a control per row already holds the whole
    map from `latest_by_record` and should keep using it; this is for the handlers that are about
    to act on a single record and must not read the entire ledger to do it.
    """
    attempts = existing_for_record(conn, record_id)
    return min(attempts, key=lambda a: _rank(a.state)) if attempts else None


def claim(conn: sqlite3.Connection, *, record_id: int, po_number: str,
          line_number: Optional[int], pod_md5: str, project_code: str = "",
          quantity: Optional[float] = None, actor: str = "",
          group_key: str = "") -> Optional[PostAttempt]:
    """Reserve this delivery, or return None if it is already claimed.

    The UNIQUE constraint on `idempotency_key` is the lock, not a check-then-insert: two requests
    racing on the same record both see "no existing row" if the check is separate, and both post.
    Here the second `INSERT` raises `IntegrityError` and is turned into a refusal.

    Returning `None` means *somebody else has this* — the caller reads the existing row to say
    whether it was already posted, is in flight, or needs a human.
    """
    key = idempotency_key(record_id, po_number, line_number, pod_md5)
    now = _now()
    try:
        conn.execute(
            """INSERT INTO spitfire_post
               (idempotency_key, record_id, po_number, line_number, pod_md5, state, detail,
                project_code, quantity, actor, claimed_at, attempts, last_attempt_at, group_key)
               VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, 1, ?, ?)""",
            (key, record_id, po_number, line_number, pod_md5.upper(), CLAIMED,
             project_code, quantity, actor, now, now, group_key))
        conn.commit()
        return find(conn, key)
    except sqlite3.IntegrityError:
        pass

    # A row already exists. If it is a refusal or a failure — neither of which created anything —
    # the delivery is free and this claim takes it over, which is what makes fixing a record and
    # pressing Post again just work. Without this the UNIQUE index turned every flag into a
    # permanent block, the exact opposite of what excluding FLAGGED from BLOCKING is for.
    #
    # Conditional on the state inside the UPDATE rather than checked first: a separate read would
    # reintroduce the race the index exists to settle, and `rowcount` says whether this caller won.
    placeholders = ",".join("?" * len(BLOCKING))
    updated = conn.execute(
        f"""UPDATE spitfire_post
               SET state = ?, detail = '', project_code = ?, quantity = ?, actor = ?,
                   claimed_at = ?, settled_at = NULL, attempts = COALESCE(attempts, 1) + 1,
                   last_attempt_at = ?, group_key = ?
             WHERE idempotency_key = ? AND state NOT IN ({placeholders})""",
        # `group_key` is re-stamped rather than left alone: a row being retaken is joining *this*
        # attempt, and keeping the previous receipt's group would file it under a receipt it is not
        # going to be on.
        (CLAIMED, project_code, quantity, actor, now, now, group_key, key, *BLOCKING)).rowcount
    conn.commit()
    return find(conn, key) if updated else None


def record_refusal(conn: sqlite3.Connection, *, record_id: int, po_number: str,
                   line_number: Optional[int], pod_md5: str, reason: str,
                   actor: str = "") -> None:
    """Record that the gate refused this delivery, and why. Nothing was sent.

    An upsert, not an append. A reviewer who presses Post, reads the reason, fixes nothing and
    presses again has not produced two events worth keeping — they have produced one fact
    ("this is still blocked") and a count. Five rows saying the same sentence would bury the
    twenty other records that are blocked for twenty other reasons, which is the exact thing the
    grouped queue exists to surface.

    Left non-blocking on purpose: see `BLOCKING`. Correcting the record is all it should take.
    """
    key = idempotency_key(record_id, po_number, line_number, pod_md5)
    now = _now()

    # Guarded on the *record*, not on this key. The two cannot be reconciled by key: the early
    # gates refuse before a PO line is known, so a refusal keys on `line=None` while the success
    # that follows keys on `line=3`. Checking only this key let a click on an already-posted
    # record insert a second row reading "still in flight" — which then outranked nothing, but did
    # shadow the receipt on the Records page and hid the Post report button behind it.
    placeholders = ",".join("?" * len(BLOCKING))
    if conn.execute(
            f"""SELECT 1 FROM spitfire_post
                 WHERE record_id = ? AND state IN ({placeholders}) LIMIT 1""",
            (record_id, *BLOCKING)).fetchone():
        return

    existing = find(conn, key)
    if existing is None:
        conn.execute(
            """INSERT INTO spitfire_post
               (idempotency_key, record_id, po_number, line_number, pod_md5, state, detail,
                actor, claimed_at, attempts, last_attempt_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (key, record_id, po_number, line_number, pod_md5.upper(), FLAGGED, reason,
             actor, now, now))
        conn.commit()
        return
    if existing.state in BLOCKING:
        # A posted or in-flight attempt is not overwritten by a later refusal. The refusal in that
        # case *is* "you already posted this", and turning the record of a real receipt into a
        # flag would lose the receipt number.
        return
    conn.execute(
        """UPDATE spitfire_post
              SET state = ?, detail = ?, attempts = COALESCE(attempts, 1) + 1,
                  last_attempt_at = ?, settled_at = ?
            WHERE idempotency_key = ?""",
        (FLAGGED, reason, now, now, key))
    conn.commit()


def awaiting_report(conn: sqlite3.Connection) -> List[PostAttempt]:
    """Receipts carrying a POD whose report has not been posted, newest first.

    The state splitting the post introduced and the atomic version could not produce: a real
    receipt in Premier's ERP with proof of delivery on it and no receiver report. It is a normal
    resting state rather than a fault — somebody posted the POD and has not yet posted the report —
    but it is one nobody would see unless it were listed, and an unfinished write to an ERP that
    nobody is told about is exactly what `blocked()` below exists to prevent for refusals.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM spitfire_post WHERE state = ? "
        "ORDER BY COALESCE(settled_at, claimed_at) DESC", (POD_POSTED,)).fetchall()
    return [_row_to_attempt(row) for row in rows]


def blocked(conn: sqlite3.Connection) -> List[PostAttempt]:
    """Every record currently refused, newest attempt first.

    **A record that has since posted is excluded, even though its flag row survives.** The two are
    keyed differently on purpose and cannot be reconciled by key: the idempotency key carries the
    PO line and the POD hash, and the gates that refuse earliest — an incomplete record, a missing
    POD — fire before either is known, so a refusal is filed under `line=None` and the eventual
    post under `line=3`. Matching on record instead of key is what stops a fixed-and-posted
    delivery sitting in the queue for ever.

    The flag row is kept rather than deleted. "This was blocked for three days on a missing
    receiver, then posted" is the only evidence of how long the data problem cost, and that is
    worth more than a tidy table.

    Feeds the grouped queue on `/ui/manual`. Grouping happens there rather than here because the
    reason is a sentence written for a person, and deciding which sentences are "the same reason"
    is a presentation question — `received_by` missing on 27 records produces 27 identical
    strings, but a quantity disagreement produces 27 different ones naming different numbers.
    """
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(BLOCKING))
    rows = conn.execute(
        f"""SELECT * FROM spitfire_post
             WHERE state = ?
               AND record_id NOT IN (SELECT record_id FROM spitfire_post
                                      WHERE state IN ({placeholders}))
             ORDER BY last_attempt_at DESC, id DESC""",
        (FLAGGED, *BLOCKING)).fetchall()
    return [_row_to_attempt(r) for r in rows]


def record_receipt(conn: sqlite3.Connection, key: str, *, receipt_key: str,
                   receipt_doc_no: str = "") -> None:
    """Persist the receipt GUID the moment it exists, before the file work starts.

    Deliberately its own call. If the chain dies during the upload, this is what tells a human
    *which* document to go and look at — without it a `CLAIMED` row says a receipt might exist
    somewhere on training with no way to find it.
    """
    conn.execute(
        "UPDATE spitfire_post SET receipt_key = ?, receipt_doc_no = ? WHERE idempotency_key = ?",
        (receipt_key, receipt_doc_no, key))
    conn.commit()


def record_file(conn: sqlite3.Connection, key: str, *, pod_file_key: str = "",
                report_file_key: str = "") -> None:
    """Same reasoning as `record_receipt`: an uploaded file that is never attached is litter in
    Premier's catalog, and this is the only record that it happened."""
    sets, values = [], []
    if pod_file_key:
        sets.append("pod_file_key = ?")
        values.append(pod_file_key)
    if report_file_key:
        sets.append("report_file_key = ?")
        values.append(report_file_key)
    if not sets:
        return
    values.append(key)
    conn.execute(f"UPDATE spitfire_post SET {', '.join(sets)} WHERE idempotency_key = ?", values)
    conn.commit()


def settle(conn: sqlite3.Connection, key: str, state: str, detail: str = "",
           audit: Optional[Sequence[Dict[str, Any]]] = None) -> None:
    """Close the attempt.

    `audit` is the write client's request log — every method, path and status the attempt issued.
    Spitfire records all of them as `api@consciouscreations.ai` no matter who triggered the post,
    so its own history cannot say which operator did what; this column is the only place that
    answer exists.
    """
    if state not in TERMINAL:
        raise ValueError(f"{state!r} is not a terminal state; expected one of {TERMINAL}")
    conn.execute(
        """UPDATE spitfire_post SET state = ?, detail = ?, audit_json = ?, settled_at = ?
           WHERE idempotency_key = ?""",
        (state, detail, json.dumps(list(audit or []), default=str), _now(), key))
    conn.commit()


def stranded(conn: sqlite3.Connection) -> List[PostAttempt]:
    """Attempts still at `CLAIMED`, i.e. a chain that died mid-flight.

    Must be empty between runs. A row here means Spitfire may hold a receipt that our records do
    not know is finished — the same class of silent loss `attachment_ledger.orphans()` exists to
    expose, and it is queryable for the same reason: so it cannot hide.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM spitfire_post WHERE state = ? ORDER BY claimed_at", (CLAIMED,)).fetchall()
    return [_row_to_attempt(r) for r in rows]
