"""The attachment ledger — one durable row per attachment, always, with a terminal verdict.

The problem this solves: before it existed, an attachment nobody could read produced a single
`print()` to a console that, in an unattended run, nobody is watching. It never reached the
exception queue either, because that queue is keyed on `match_results`, and a file that was
never read produces no extracted record and therefore no match row. Five of the fifteen sniffed
kinds had no reader at all, and four further drops inside the intake connector were bare
`continue` statements. The system could lose a proof of delivery and show no sign of it.

Two design points carry the weight:

* **Rows are written before triage branches.** `process_new_mail` exits early for ROUTE and HIDE
  mail, so anything recorded during extraction structurally cannot see those messages — and
  ROUTE is exactly where the interesting failures land, since that is where a photographed POD
  with no readable text ends up.
* **`orphans()` must always return nothing.** A row still sitting at `OBSERVED` after a run means
  an attachment escaped the dispatcher. That is the bug class this module exists to make
  impossible to hide, so it is queryable rather than merely hoped for.
"""

import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config import settings
from pipeline import attachment_store
from pipeline.models import Attachment, RawEmail
from pipeline.parsing import receipt, sniff

# --- Dispositions -----------------------------------------------------------

OBSERVED = "observed"
"""Transient — recorded at ingest, not yet dispatched. Never a resting state."""

NOT_DISPATCHED = "not_dispatched"     # the email was ROUTE/HIDE, so Stage 3 never ran on it
AWAITING_RELEASE = "awaiting_release"
"""On a HOLD email whose delivery event has not fired yet. A resting state, not a loss — the
attachment is read when the partner notice arrives or the grace sweep releases it."""
CONTAINER_EXPANDED = "container_expanded"   # a zip/.msg/.eml; its members have their own rows
EXTRACTED = "extracted"               # an adapter claimed it and produced records
EMPTY = "empty"                       # an adapter claimed it, ran clean, found nothing
UNREADABLE = "unreadable"             # an adapter claimed it and raised
ENCRYPTED = "encrypted"               # password- or DRM-protected
CORRUPT = "corrupt"                   # structurally broken, and not encrypted
NO_ADAPTER = "no_adapter"             # nothing claimed it — a real coverage gap
UNSUPPORTED_FORMAT = "unsupported_format"   # recognised, deliberately no reader (.doc, .pptx)
DROPPED_DECORATIVE = "dropped_decorative"
DROPPED_DUPLICATE = "dropped_duplicate"
DUPLICATE_CONTENT_MISMATCH = "duplicate_content_mismatch"
"""Byte-identical to another attachment, but its filename promises a different document.

Premier really does send the same POD twice under two names, and dropping the second copy is
correct. But the corpus carries a case where that is a different fact entirely:
`FedEx 884603885067  GR-350d-WTF 78 yards from Daniel Stuart.pdf` is byte-identical to
`910634 - P. Kaufmann FedEx POD.pdf` — a different tracking number and a different spec on the
label, the same 910634 POD inside. Somebody attached the wrong file, so a proof everybody
believes was supplied does not exist. Still dropped; no longer dropped silently."""
DROPPED_OVERSIZE = "dropped_oversize"
DROPPED_DEPTH = "dropped_depth"
DROPPED_COUNT = "dropped_count"
DROPPED_EMPTY = "dropped_empty"       # zero bytes
DROPPED_REFERENCE = "dropped_reference"
"""A cloud link rather than a file — Graph's `referenceAttachment`, a OneDrive/SharePoint URL
carrying no bytes. Distinct from `NO_ADAPTER` on purpose: nothing is missing from our readers,
there is simply nothing to read. Fetching it would need `Files.Read.All` and a separate consent
conversation, so the URL is recorded and a person opens it."""

SERVICE_UNAVAILABLE = "service_unavailable"
"""An external dependency failed. The file is fine, the bytes are here, and this is re-runnable
the moment the dependency is.

Distinct from `UNREADABLE` and `CORRUPT`, and the distinction is the whole point. Azure Document
Intelligence returned 401 for an unknown period; every photographed POD that arrived in that
window read nothing, and the pipeline recorded each one as `extracted` with `records_extracted=1`
because `OcrAdapter` returned a one-element placeholder list that `dispatch`'s `if records:` found
truthy. The ledger — the table whose docstring promises nothing is silently dropped — asserted
success for every one of them, so after the key was fixed there was no way to name what to re-run.

`unreadable` would have been a lie of a different kind ("our reader failed on this file") and
`corrupt` worse still ("the file is structurally broken"), both blaming a sender's photograph for
someone else's auth failure. This says what actually happened, carries the reason in
`disposition_detail`, and is what `tools/reextract.py` selects on."""

ALL_DISPOSITIONS = frozenset({
    OBSERVED, NOT_DISPATCHED, AWAITING_RELEASE, CONTAINER_EXPANDED, EXTRACTED, EMPTY, UNREADABLE, ENCRYPTED,
    CORRUPT, NO_ADAPTER, UNSUPPORTED_FORMAT, SERVICE_UNAVAILABLE, DROPPED_DECORATIVE,
    DROPPED_DUPLICATE, DROPPED_OVERSIZE, DROPPED_DEPTH, DROPPED_COUNT, DROPPED_EMPTY,
    DROPPED_REFERENCE, DUPLICATE_CONTENT_MISMATCH,
})
TERMINAL = ALL_DISPOSITIONS - {OBSERVED}

NEEDS_ATTENTION = frozenset({
    UNREADABLE, ENCRYPTED, CORRUPT, NO_ADAPTER, UNSUPPORTED_FORMAT, SERVICE_UNAVAILABLE,
    DROPPED_OVERSIZE, DROPPED_DEPTH, DROPPED_COUNT, DROPPED_REFERENCE,
    DUPLICATE_CONTENT_MISMATCH,
})
"""Dispositions a person should see. Deliberately excludes the decorative/duplicate drops,
which are routine and correct, and `empty`, which means we read it and it genuinely held
nothing. `dropped_reference` is here because the POD may well be behind that link.

`empty` stays out of *this* set because it also drives `review_status`, and a genuinely empty
signature image is not review work. It is not ignored either — see `list_silent_on_delivery_mail`,
which exists because "we read it and it held nothing" turned out to be a lie."""

REVIEW_NONE = "none"
REVIEW_PENDING = "pending_review"
REVIEW_ACKNOWLEDGED = "acknowledged"
REVIEW_REPROCESS = "reprocess_requested"


def status_report_verdict(grid_verdicts) -> str:
    """The reason this file is a status report rather than a delivery document, or `""`.

    Reads what `grid_reader` kept on the source. A workbook is judged sheet by sheet and the
    verdicts disagree routinely — `ANS-026 ... Expediting Report.xlsx` holds a Spitfire export
    beside a confirmation grid — so this answers for the file as a whole, and the order is what
    decides it.

    **A positive `delivery_document` on any sheet wins.** Premier really does send one workbook
    holding both a receiver and a tracker, and a file somebody can receive goods against is never
    to be set aside on the strength of a second sheet. `no_receipt_column` is not consulted at all:
    it is the absence of evidence, the same thing `stage1_triage` declines to act on for
    `Intent.NEITHER`, and acting on it would sweep up packing slips and receiving reports whose
    header simply did not parse.
    """
    verdicts = list(grid_verdicts or [])
    if any(kind == receipt.DELIVERY_DOCUMENT for _label, kind, _reason in verdicts):
        return ""
    for label, kind, reason in verdicts:
        if kind != receipt.STATUS_REPORT:
            continue
        # `classify_grid` phrases every reason as a noun phrase — "a system export — …",
        # "a status tracker — …" — so naming the sheet in front of it makes one sentence rather
        # than two clauses joined by a second dash. The adapter prefix is dropped for reading:
        # `excel:` is already said by the `via excel:Expediting` line the record rows carry.
        sheet = (label or "").split(":", 1)[-1].strip()
        return f"the sheet '{sheet}' is {reason}" if sheet else reason
    return ""

_COLUMNS = (
    "email_id", "parent_id", "depth", "ordinal", "container_path", "filename",
    "declared_content_type", "sniffed_kind", "sniff_reason", "sha256", "size_bytes",
    "content_id", "is_inline", "triage_category", "claimed_by", "records_extracted",
    "disposition", "disposition_detail", "error_type", "review_status",
    "first_seen_at", "last_updated_at",
)


@dataclass
class LedgerRow:
    id: int
    email_id: str
    filename: str
    sniffed_kind: str
    sha256: str
    size_bytes: int
    disposition: str
    disposition_detail: str
    container_path: str = ""
    depth: int = 0
    claimed_by: Optional[str] = None
    records_extracted: int = 0
    error_type: Optional[str] = None
    triage_category: Optional[str] = None
    review_status: str = REVIEW_NONE
    parent_id: Optional[int] = None
    first_seen_at: str = ""
    """When this attachment was first recorded. The column has existed since the table did and two
    queries already order by it, but it was never carried onto the row — so the manual queue built
    its attachment items with a blank date, and the When column was empty on every one of them."""

    is_status_report: int = 0
    status_report_reason: str = ""
    """Whether this file is one of Premier's own worklists, and in `classify_grid`'s own words.

    Set at ingest from the verdict `grid_reader` takes anyway. Stored rather than recomputed at
    read time because the manual queue builds four thousand items per render and reopening every
    workbook to do it is not a page load."""


def _row(record: sqlite3.Row) -> LedgerRow:
    return LedgerRow(
        id=record["id"], email_id=record["email_id"], filename=record["filename"],
        sniffed_kind=record["sniffed_kind"], sha256=record["sha256"],
        size_bytes=record["size_bytes"], disposition=record["disposition"],
        disposition_detail=record["disposition_detail"], container_path=record["container_path"],
        depth=record["depth"], claimed_by=record["claimed_by"],
        records_extracted=record["records_extracted"], error_type=record["error_type"],
        triage_category=record["triage_category"], review_status=record["review_status"],
        parent_id=record["parent_id"], first_seen_at=record["first_seen_at"] or "",
        is_status_report=record["is_status_report"] or 0,
        status_report_reason=record["status_report_reason"] or "",
    )


# --- Writing ----------------------------------------------------------------


def observe(
    conn: sqlite3.Connection,
    email: RawEmail,
    triage_category: Optional[str],
    now: str,
) -> None:
    """Record every attachment on this email and stamp each one's `ledger_id` in place.

    Attachments the connector already decided against still get a row — carrying their
    `drop_hint` as a terminal disposition — because "we dropped this and here is why" is the
    whole point. Idempotent on the unique key, so a re-run adds nothing.
    """
    for ordinal, attachment in enumerate(email.attachments):
        disposition, detail = _disposition_for_hint(attachment.drop_hint)
        attachment.ledger_id = _insert(
            conn,
            email_id=email.email_id,
            parent_id=None,
            depth=0,
            ordinal=ordinal,
            container_path=attachment.container_path or attachment.filename,
            attachment=attachment,
            triage_category=triage_category,
            disposition=disposition,
            detail=detail,
            now=now,
        )


def add_child(
    conn: sqlite3.Connection,
    parent_id: Optional[int],
    email_id: str,
    attachment: Attachment,
    depth: int,
    ordinal: int,
    triage_category: Optional[str],
    now: str,
) -> int:
    """Record one member of an expanded container, parented to it."""
    disposition, detail = _disposition_for_hint(attachment.drop_hint)
    attachment.ledger_id = _insert(
        conn, email_id=email_id, parent_id=parent_id, depth=depth, ordinal=ordinal,
        container_path=attachment.container_path or attachment.filename,
        attachment=attachment, triage_category=triage_category,
        disposition=disposition, detail=detail, now=now,
    )
    return attachment.ledger_id


def keeps_bytes(drop_hint: Optional[str]) -> bool:
    """Whether this attachment's bytes may go to the store, judged from its drop hint alone.

    Asked by the connectors *before* they release the bytes, and by `_insert` after. Those were two
    different rules, and that is why `PREMIER_STORE_DECORATIVE=0` did nothing: it gated `_insert`
    only, while both connectors had already called `attachment_store.put()` on the way past and
    `_insert`'s `exists()` re-link then attached the row to the bytes anyway. Measured 2026-09-16:
    23,863 of 23,867 decorative rows had blobs with the setting off, the newest stamped that day.

    **The connectors were not wrong to keep them.** Their comments make a real argument -- the
    classifier decides "decorative" from a known hash, or from being cid-referenced and under 64 KB,
    or from being under 16 KB (`parsing/sniff.py:334-348`), and `mail_view` says in as many words
    that "a pasted photograph is often the proof of delivery itself". A misclassification that
    destroys bytes destroys evidence. That hedge is now spelled `PREMIER_STORE_DECORATIVE=1`, which
    restores exactly the old behaviour on every path at once, and is what
    `test_the_setting_brings_the_old_behaviour_back` holds open.

    Only `decorative` is gated. A `duplicate` hint must still call `put`: it is a no-op on bytes
    already stored under that hash, and narrowing it would be a second behaviour change riding
    along on this one.
    """
    if settings.STORE_DECORATIVE_ATTACHMENTS:
        return True
    return _disposition_for_hint(drop_hint)[0] != DROPPED_DECORATIVE


def _disposition_for_hint(drop_hint: Optional[str]) -> tuple:
    """A connector's `drop_hint` becomes a terminal disposition; no hint means still in flight."""
    if not drop_hint:
        return OBSERVED, ""
    head = drop_hint.split(":", 1)[0]
    mapping = {
        "decorative": DROPPED_DECORATIVE,
        "duplicate": DROPPED_DUPLICATE,
        "duplicate_mismatch": DUPLICATE_CONTENT_MISMATCH,
        "oversize": DROPPED_OVERSIZE,
        "empty": DROPPED_EMPTY,
        "depth": DROPPED_DEPTH,
        "count": DROPPED_COUNT,
        "reference": DROPPED_REFERENCE,
    }
    return mapping.get(head, NO_ADAPTER), drop_hint


def _insert(
    conn: sqlite3.Connection,
    *,
    email_id: str,
    parent_id: Optional[int],
    depth: int,
    ordinal: int,
    container_path: str,
    attachment: Attachment,
    triage_category: Optional[str],
    disposition: str,
    detail: str,
    now: str,
) -> Optional[int]:
    result = sniff.sniff(attachment.content_bytes, attachment.filename, attachment.content_type or "")
    # A dropped attachment has had its bytes cleared, so re-sniffing returns `unknown`. The kind
    # recorded at ingest, while the bytes were still present, is the truthful one.
    kind = attachment.sniffed_kind or result.kind
    reason = result.reason if kind == result.kind else "recorded at ingest, before the bytes were released"

    # Keep the bytes. Ingest used to keep none at all, so the only copy of every POD was Premier's
    # Outlook mailbox. Two cases meet here: an attachment still holding its bytes is stored now,
    # and one already released by the connector was stored there, before the release — the
    # `exists` branch is what re-attaches that row to its blob.
    #
    # Except a logo. `settings.STORE_DECORATIVE_ATTACHMENTS` is off by default, and a sender's
    # logo or signature graphic is never evidence — so its bytes are not kept, while the row
    # recording that we saw it still is. The row is the audit trail; the pixels are not.
    #
    # Gated here, before `put`, because that is the only place it saves anything: deciding after
    # the write would store the blob and then think better of it. An already-stored blob is still
    # re-attached below, so turning the setting off never orphans a row from bytes already on disk.
    digest = attachment.sha256 or result.sha256
    store_bytes = settings.STORE_DECORATIVE_ATTACHMENTS or disposition != DROPPED_DECORATIVE
    blob = attachment_store.put(attachment.content_bytes) if store_bytes else None
    if blob is None and digest and attachment_store.exists(digest):
        blob = digest
    values = (
        email_id, parent_id, depth, ordinal, container_path, attachment.filename,
        attachment.content_type, kind, reason,
        digest,
        attachment.size_bytes or len(attachment.content_bytes or b""),
        attachment.content_id, 1 if attachment.is_inline else 0,
        triage_category, None, 0, disposition, detail, None, REVIEW_NONE, now, None,
    )
    placeholders = ", ".join(["?"] * len(_COLUMNS))
    cursor = conn.execute(
        f"INSERT OR IGNORE INTO attachment_ledger ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
        values,
    )
    conn.commit()
    row_id = cursor.lastrowid
    if not row_id:
        existing = conn.execute(
            "SELECT id FROM attachment_ledger WHERE email_id = ? AND depth = ? AND ordinal = ? AND sha256 = ?",
            (email_id, depth, ordinal, digest),
        ).fetchone()
        row_id = existing[0] if existing else None

    # Separate from the INSERT so a re-run of an email ingested before the store existed
    # back-fills its blob columns rather than leaving them null for ever.
    if row_id and blob:
        conn.execute(
            "UPDATE attachment_ledger SET blob_sha256 = ?, blob_stored_at = ? "
            "WHERE id = ? AND blob_sha256 IS NULL",
            (blob, now, row_id),
        )
        conn.commit()
    return row_id


def record_outcome(
    conn: sqlite3.Connection,
    ledger_id: Optional[int],
    disposition: str,
    now: str,
    *,
    claimed_by: Optional[str] = None,
    records_extracted: int = 0,
    detail: str = "",
    error_type: Optional[str] = None,
    pod_document: Any = None,
    grid_verdicts: Any = None,
) -> None:
    """Close a row with what actually happened. A no-op for `ledger_id=None` so callers that
    don't have one (unit tests, direct adapter calls) need no special-casing.

    `pod_document` is the parsed proof of delivery when the adapter that read this attachment
    recognised one — whatever the file type. Recorded here because this is the one write that
    already happens for every dispatched attachment, and because deciding it once at ingest is
    what keeps `spitfire_post` from paying for OCR again to answer "is this the proof?".

    `grid_verdicts` is the same arrangement for the other question a reader answers on the way
    past: is this document a record of goods arriving, or one of Premier's own worklists. Written
    on every outcome, `extracted` included — the largest expediting reports produce records from
    one sheet while another sheet is plainly a Spitfire export.
    """
    if ledger_id is None:
        return
    if disposition not in ALL_DISPOSITIONS:
        raise ValueError(f"unknown disposition {disposition!r}")
    review = REVIEW_PENDING if disposition in NEEDS_ATTENTION else REVIEW_NONE
    conn.execute(
        """UPDATE attachment_ledger
              SET disposition = ?, disposition_detail = ?, claimed_by = ?,
                  records_extracted = ?, error_type = ?, review_status = ?, last_updated_at = ?
            WHERE id = ?""",
        (disposition, detail, claimed_by, records_extracted, error_type, review, now, ledger_id),
    )
    status_reason = status_report_verdict(grid_verdicts)
    if status_reason:
        conn.execute(
            """UPDATE attachment_ledger
                  SET is_status_report = 1, status_report_reason = ?
                WHERE id = ?""",
            (status_reason, ledger_id),
        )
    if pod_document is not None:
        conn.execute(
            """UPDATE attachment_ledger
                  SET is_pod = 1, pod_po_numbers = ?, pod_delivery_date = ?, pod_signed_by = ?
                WHERE id = ?""",
            (",".join(getattr(pod_document, "po_numbers", None) or []),
             getattr(pod_document, "delivery_date", None),
             getattr(pod_document, "signed_for_by", None),
             ledger_id),
        )
    conn.commit()


def close_undispatched(conn: sqlite3.Connection, email: RawEmail, now: str) -> None:
    """Mark this email's still-open rows as never dispatched — used on the ROUTE/HIDE paths,
    where Stage 3 legitimately never runs but the attachments must still be accounted for."""
    for attachment in email.attachments:
        if attachment.ledger_id is None:
            continue
        conn.execute(
            """UPDATE attachment_ledger
                  SET disposition = ?, disposition_detail = ?, last_updated_at = ?
                WHERE id = ? AND disposition = ?""",
            (NOT_DISPATCHED, "email was routed or hidden before extraction", now,
             attachment.ledger_id, OBSERVED),
        )
    conn.commit()


def close_open_rows(conn: sqlite3.Connection, now: str) -> int:
    """Settle anything still `observed` at the end of a pass.

    These are attachments on HOLD mail whose delivery event has not fired — they are waiting,
    not lost. Closing them explicitly is what lets `orphans()` mean "something escaped the
    dispatcher" rather than "something is pending", which is the only way that check is useful.
    """
    cursor = conn.execute(
        """UPDATE attachment_ledger
              SET disposition = ?, disposition_detail = ?, last_updated_at = ?
            WHERE disposition = ?""",
        (AWAITING_RELEASE, "held pending the delivery event that will release it", now, OBSERVED),
    )
    conn.commit()
    return cursor.rowcount


def set_review_status(conn: sqlite3.Connection, ledger_id: int, status: str, now: str) -> None:
    conn.execute(
        "UPDATE attachment_ledger SET review_status = ?, last_updated_at = ? WHERE id = ?",
        (status, now, ledger_id),
    )
    conn.commit()


# --- Reading ----------------------------------------------------------------


def _query(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> List[LedgerRow]:
    prior = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return [_row(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.row_factory = prior


def for_email(conn: sqlite3.Connection, email_id: str) -> List[LedgerRow]:
    return _query(conn, "SELECT * FROM attachment_ledger WHERE email_id = ? ORDER BY depth, ordinal, id", (email_id,))


def list_needing_attention(conn: sqlite3.Connection) -> List[LedgerRow]:
    marks = ", ".join(["?"] * len(NEEDS_ATTENTION))
    return _query(
        conn,
        f"SELECT * FROM attachment_ledger WHERE disposition IN ({marks}) ORDER BY first_seen_at DESC",
        tuple(sorted(NEEDS_ATTENTION)),
    )


def list_silent_on_delivery_mail(conn: sqlite3.Connection) -> List[LedgerRow]:
    """Attachments on delivery mail that were read successfully and yielded nothing.

    `NEEDS_ATTENTION` leaves `empty` out on the reasoning that we read it and it genuinely held
    nothing. Premier's live mail disproved that. The embedded `Delivered Notification` — the one
    attachment carrying the POD date, the signature, the carrier, the tracking numbers and the
    actual delivered quantity — was mis-classified as plain text, handed to `TextAdapter`, read
    "cleanly", and recorded `empty`. It produced no records and nobody was ever told.

    Restricted to `surface`/`hold` mail so an empty signature image on an all-associates broadcast
    stays quiet: on delivery mail, an attachment that says nothing is a question, not a fact.
    """
    return _query(conn, f"""
        SELECT l.* FROM attachment_ledger l
          JOIN email_log e ON e.email_id = l.email_id
         WHERE l.disposition = ?
           AND l.records_extracted = 0
           AND e.category IN ('surface', 'hold')
         ORDER BY l.first_seen_at DESC
    """, (EMPTY,))


def status_reports(conn: sqlite3.Connection) -> Tuple[Dict[str, str], Dict[int, str]]:
    """Every file judged one of Premier's own worklists, as `(by email_id, by ledger id)`.

    Two indexes off one query because the queue needs both and asking per row would be thousands
    of statements: the message-level rows want "does this mail carry one", and the attachment row
    for the workbook itself wants its own sentence. Where a message carries several, the first by
    id wins — they are near-always the same weekly report under two names.
    """
    by_email: Dict[str, str] = {}
    by_ledger: Dict[int, str] = {}
    for row in conn.execute(
            "SELECT id, email_id, status_report_reason FROM attachment_ledger "
            "WHERE is_status_report = 1 ORDER BY id"):
        ledger_id, email_id, reason = row[0], row[1], (row[2] or "")
        by_ledger[ledger_id] = reason
        by_email.setdefault(email_id, reason)
    return by_email, by_ledger


def orphans(conn: sqlite3.Connection) -> List[LedgerRow]:
    """Rows still at `OBSERVED`. Always empty in a correct run — a non-empty result means an
    attachment slipped past the dispatcher without a verdict."""
    return _query(conn, "SELECT * FROM attachment_ledger WHERE disposition = ?", (OBSERVED,))


def counts_by_disposition(conn: sqlite3.Connection) -> Dict[str, int]:
    return {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT disposition, COUNT(*) FROM attachment_ledger GROUP BY disposition"
        ).fetchall()
    }


def counts_by_kind(conn: sqlite3.Connection) -> Dict[str, int]:
    return {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT sniffed_kind, COUNT(*) FROM attachment_ledger GROUP BY sniffed_kind"
        ).fetchall()
    }


# --- naming a duplicate that should not have been one --------------------------------------------

def filename_identifiers(filename: str) -> frozenset:
    """The purchase orders, spec codes and tracking numbers a filename claims.

    Senders name these files after what is inside them — `910634 - P. Kaufmann FedEx POD.pdf`,
    `FedEx 884603885067  GR-350d-WTF 78 yards from Daniel Stuart.pdf`. That naming is the only
    signal available *before* anything is parsed, and comparing two of them is what tells a
    routine second copy apart from the wrong file attached.
    """
    from pipeline.parsing import tokens

    stem = str(filename or "").rsplit(".", 1)[0]
    # `parse_po_list`, not `find_po_numbers`: the latter wants a "PO" label near the digits, and
    # nobody labels a filename — `910634 - P. Kaufmann FedEx POD.pdf` just leads with the number.
    # A filename is an already-isolated slot, which is exactly what `parse_po_list` is for, and its
    # strict six-digit shape keeps a twelve-digit tracking number out of the PO set.
    return frozenset(
        tokens.parse_po_list(stem)
        + tokens.find_specs(stem)
        + tokens.parse_tracking_numbers(stem)
    )


def duplicate_drop_hint(filename: str, first_filename: str, sha256: str) -> str:
    """The drop hint for a byte-identical attachment — routine, or a mismatch worth a person.

    Both copies are dropped either way. The difference is whether anybody is told: a sender who
    attached the wrong file believes a proof was supplied, and nothing downstream can discover
    that from the bytes, because the bytes are a perfectly valid POD for a different order.
    """
    mine = filename_identifiers(filename)
    theirs = filename_identifiers(first_filename)
    # Only when both sides actually name something and the two sets are disjoint. One unnamed file
    # says nothing, and an overlap means they are describing the same delivery from two angles.
    if mine and theirs and not (mine & theirs):
        return (f"duplicate_mismatch:{sha256[:12]} — filename names "
                f"{', '.join(sorted(mine))} but the bytes are those of "
                f"{first_filename!r} ({', '.join(sorted(theirs))})")
    return f"duplicate:{sha256[:12]}"
