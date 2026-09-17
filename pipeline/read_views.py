"""Read-only views over pipeline_state.sqlite3, shaped for the operations UI.

Query-only and dependency-free on purpose — no FastAPI, no pydantic — so the same three views
back the web pages, the CLI runner's summary, and the tests that assert the pipeline left a
trace. They live here rather than in `api/stores/` because every module there reads the *demo*
database; pipeline state is owned next to `state_db`.

`records_ready`, the record arm of `manual_queue` and `records_awaiting_confirmation` **partition
the pending records**: every one on exactly one destination, never two, never none.

With one set held back from all three: records whose message a person has set aside as not a
delivery. They are **retired, not stranded** — the row and its evidence stay, and the verdict is one
click from being reversed, which puts every one of them back on whichever destination it belonged
to. Membership is exactly `mail_overrides.verdict = 'not_delivery'`, expressed once as
`_NOT_SET_ASIDE` and applied by `_records_pending` and by the queue's record arm. So the invariant
is: the three destinations partition the pending records *that no one has set aside*. Anything
weaker was a claim about work nobody was told about; anything stronger would mean a person could
declare a message not delivery mail and still be offered a Post button for the receipts read out
of it.

The third destination arrived on 2026-09-07. Premier writes its own delivery mail — expediting
reports, inventory sheets, "has this landed yet" chasers — and those messages carry purchase
orders, spec codes and quantities, so extraction reads them as well as it reads a warehouse
receiving report. What they do not carry is any statement that goods arrived. They were reaching
the Records page as postable work: 1,067 rows across 14 messages, against 151 rows of real
delivery evidence.

They were not smuggled past a gate; they were let through one. Triage holds this mail correctly,
Stage 2 waits for a confirming event, none ever comes, and after the grace period
`sweep_stale_holds` releases it anyway under the reason it writes down verbatim — *"grace period
elapsed, using confirmation as trigger"*. That is the normal path, not an edge case: 1,753 of
1,768 releases carry it. The system was treating an unanswered request for confirmation as its own
confirmation.

So the release reason cannot be what decides this — 87 of the 94 postable records were released
the same way, every genuine Atlas receiving report among them. What decides it is **who wrote the
message**: evidence of delivery comes from outside Premier, or it comes from a person here signing
for it. See `records_awaiting_confirmation`.

That was relaxed for a while, on the reasoning that readiness ("can this be processed further")
and completeness ("could a receiver line be built from it") are different questions, so a record
with a PO and no proof of delivery could honestly appear on both — listed for a person to finish,
and not withheld from the pipeline while they did.

**Reversed on 2026-08-24, because it does not survive contact with `post_decision`.** Nothing was
being withheld: gate 1 of `post_decision.decide` *is* completeness, so an incomplete record is
refused before a single network call. Run against the eleven such records in Premier's live store,
every one came back `FLAG: the record is incomplete`. So the only thing the overlap achieved was a
Post button, on the page that means "these are ready", which could not post — on eleven of its
forty-one rows.

The rule now: **on Records means postable. Anything with a gap is on `manual_queue`, where the
controls that close gaps live.** Both halves of the invariant hold again, and they hold *by
construction* — this filters on `completeness.is_complete`, that filters on `completeness.gaps`,
and one function decides for both.
"""
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from pipeline import (attachment_ledger, authorship, completeness, delivery_status, email_log,
                      extracted_records_store, mail_overrides)

# A record is ready to process further only if all four hold. Kept as one string so the "ready"
# query and the "blocked" query cannot drift apart — the second is the literal negation.
_READY_CLAUSE = """
    r.status = 'pending'
    AND COALESCE(TRIM(r.po_number), '') <> ''
    AND r.extraction_confidence > 0
    AND r.extraction_source NOT LIKE '%quantity_conflict%'
"""

# The record's message has not been set aside by a person. Written once and applied wherever
# records are read, so the two places that read them cannot disagree about it: `_records_pending`
# (and through it Records, the CSV, the Post button and the edit form) and the record arm of
# `manual_queue`. Expects the records table aliased `r`.
#
# `verdict = 'not_delivery'` only, never `email_log.not_a_delivery`. Triage's flag is a rule's
# guess and is wrong in both directions often enough that acting on it here would silently retire
# real delivery records; this is a person's signed decision about one message.
_NOT_SET_ASIDE = """
    NOT EXISTS (SELECT 1 FROM mail_overrides v
                 WHERE v.email_id = r.source_email_id AND v.verdict = 'not_delivery')
"""


def _rows(conn: sqlite3.Connection, sql: str, params=()) -> List[sqlite3.Row]:
    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.row_factory = prior_factory


@dataclass
class Summary:
    emails: int = 0
    by_category: Dict[str, int] = field(default_factory=dict)
    records_total: int = 0
    records_ready: int = 0
    records_postable: int = 0
    """How many could actually be posted now — `records_ready` filtered by post_decision gate 2.

    Separate from `records_ready` because the two answer different questions and were being read as
    one. `records_ready` counts records with no missing field; on the live store that was 169 while
    **45** could actually post. A screen offering "169 ready" promises work that is not there.
    """
    needs_human: int = 0
    records_awaiting_confirmation: int = 0
    """Records read out of Premier's own mail that nobody has confirmed — the third destination.

    Counted in **records**, not in queue items, which is what makes it worth its own field: the
    queue shows fourteen rows and those rows stand for 1,067 records. A reader asking "where did
    the rest of the records go" is asking this number, and `needs_human` cannot answer it.
    """
    ocr_pages: int = 0
    last_run: Optional[str] = None


@dataclass
class MailRow:
    email_id: str
    subject: str
    sender: str
    origin_sender: Optional[str]
    email_date: str
    category: str
    matched_rule: str
    reason: str
    folder: str
    po_hints: str
    attachment_count: int
    attachments_flagged: int
    ocr_attempted: int
    records: int
    processed_at: str
    source: str = "inbox"
    """Which store this row came from — a key from `MAIL_SOURCES`, not a database path.

    Defaulted rather than required so every existing caller of `mails()` keeps working; only the
    merged view sets it. It is also what a row's message link uses to reopen the mail from the
    store it actually lives in, so it must never be guessed."""

    source_label: str = "Inbox"

    source_folder: str = ""
    """Which mailbox folder the message was read from — `inbox` or `junkemail`.

    Deliberately not the same thing as `source`, which names the SQLite store. Junk and Inbox mail
    land in the same store, so overloading `source` would have made the two unanswerable at once —
    and "which database" is what the message link needs, while "which folder" is what tells someone
    Exchange is filing delivery mail as spam. Empty on rows written before the column."""


@dataclass
class ManualItem:
    kind: str            # "email" | "attachment" | "record"
    ref: str             # subject / filename / "record #123"
    ref_id: Optional[int]
    email_id: str
    subject: str
    when: str
    reason: str          # never blank — a queue row without a stated reason is not actionable
    detail: str = ""
    po_number: str = ""
    """The purchase order this item is about, when it is about exactly one.

    Set for records and left empty for emails and attachments — an email routinely names ten POs
    (`email_log.po_hints`) and picking one of them would be a guess. Empty is also legitimate on a
    record: a missing PO is itself one of the completeness gaps that puts it on this queue.

    Carried as a field rather than left inside `detail`, where it used to live as prose, because
    the page renders it as the control that opens the mail it came from — and parsing a PO back
    out of a sentence to do that would be absurd.
    """
    code: str = ""
    """Why this is here, in one word, decided by whoever put it here.

    `reason` above is the sentence a person reads; this is the same fact in a form a filter can
    count — `no_po`, `needs_ocr`, `qty_conflict`, `corrupt`, `unreadable`, `nothing_extracted`,
    `quarantined`, `error`, `routed`. Classified at the source rather than by matching the prose
    back apart on the way out: the branch that builds the sentence is the one that knows.
    """
    sender: str = ""
    """Who sent the mail this arrived on, for the line under the subject. It was inside `detail`
    as prose (`from X · filed to Y`), which no column could show on its own."""
    filed_to: str = ""
    """The folder the message ended up in — `Routed`, `Errors`, `Quarantine`. Same story as
    `sender`: a fact worth a column, previously buried in a sentence."""
    po_line_number: Optional[int] = None
    """Which line of that purchase order, when it is known.

    A PO alone does not identify a delivery. PO 907514 carries 29 lines that **all** read
    `LOB-900-SI` — a signage package whose lines differ only by description — so its 23 queued
    items each said "PO 907514, LOB-900-SI" and nothing more, which was true of every one of them
    and told a reader nothing. Rendered beside the PO as `907514 : 13`.
    """
    item: str = ""
    """What was delivered — spec and description, the one thing that differs row to row.

    Measured on PO 907514's 23 rows: of the eight fields a queue row carried, **seven were
    identical** and only this varied. It used to be folded into `detail` behind the spec and in
    front of `via <source>`, both repeated on every row of a group, and truncated to fit — so the
    only distinguishing text on the page was the part most likely to be cut off.
    """
    codes: Tuple[str, ...] = ()
    """Every distinct `code` folded into this row, when it stands for more than one thing.

    Empty on an ordinary row, so a reader is `item.codes or (item.code,)`. `code` above stays the
    single word the badge shows; this is the set the reason chips have to match against, because a
    message that was routed to a person *and* carries three attachments nobody could read must be
    findable under `routed` and under `needs_ocr` both. Ordered by the same priority as `code`.
    """
    rolled_up: int = 0
    """How many queue items this row stands for. `0` when it stands only for itself.

    Deliberately not `1` for an unmerged row: the page says "+2 more" off this number, and "one
    thing" and "one thing that happened to be alone in its group" are worth telling apart when
    reading the queue back.
    """


REASON_PRIORITY = (
    "no_po", "qty_conflict", "incomplete", "status_report", "awaiting_confirmation", "needs_ocr",
    "nothing_recognised", "corrupt", "unreadable", "nothing_extracted", "duplicate", "overridden",
    "maybe_advertising", "routed", "quarantined", "error",
)
"""`ManualItem.code` in the order a person would work through them.

Owned here rather than in the UI because merging needs it: when one message contributes several
items, *which* of their codes the row is badged with is a judgement about priority, and priority is
not a rendering decision. `api.ui.routes._REASON_LABELS` puts these same codes in this same order
and adds the words the chips show; it is checked against this tuple by a test so the two cannot
drift apart.
"""

_ROLLUP_PHRASE = {
    "status_report": "holding a status report",
    "awaiting_confirmation": "awaiting confirmation",
    "needs_ocr": "awaiting OCR",
    "nothing_recognised": "read but holding no delivery",
    "corrupt": "corrupt",
    "unreadable": "unreadable",
    "nothing_extracted": "yielding nothing",
    "duplicate": "already recorded",
    "routed": "routed to a person",
    "quarantined": "over attachment limits",
    "error": "failed with an error",
}
"""How a merged row names the problems it is standing in for, after its own sentence.

Adjectival, never a finite verb — the clause is built as `"{n} attachments {phrase}"` and the count
varies, so anything with a verb in it disagrees with itself at n=1. Codes absent here can never
reach a merged row's *rest*: the record-level ones because a group holding a record is left alone,
and `overridden` because its branch excludes every category the other email branches claim — so a
message carries that item or one of theirs, never both, and it is always its group's head.
"""


def _merge_no_record_emails(items: List[ManualItem]) -> List[ManualItem]:
    """One row per message, for messages that produced no record at all.

    An email that nothing could be read out of was landing on the queue once as the message *and*
    once per attachment that defeated its adapter — 1092 rows describing 651 messages, and one
    message with 26 of them. Every one of those rows says the same thing to the person working the
    queue ("this arrived and we got nothing from it") and offers the same single way out ("create
    the record by hand"), so they are one piece of work and now they are one row.

    Groups holding a record are returned untouched. A record is already the unit of work there: it
    exists, it has gaps, and its Fix / Fill / Verify buttons act on that record and no other. Merging
    those would take the only actions on the page and point them at nothing.
    """
    order = {code: rank for rank, code in enumerate(REASON_PRIORITY)}
    groups: Dict[str, List[ManualItem]] = {}
    for item in items:
        groups.setdefault(item.email_id, []).append(item)

    merged: List[ManualItem] = []
    for group in groups.values():
        if len(group) == 1 or any(i.kind == "record" for i in group):
            merged.extend(group)
            continue

        ranked = sorted(group, key=lambda i: (order.get(i.code, len(order)), i.when or ""))
        codes = tuple(dict.fromkeys(i.code for i in ranked))

        # Which of the merged items gets to be the row's sentence.
        #
        # The message's own item leads, because the row stands for a message: ranked by priority
        # alone a routed email carrying an OCR-failed PDF led with the Azure outage, which describes
        # a file and says nothing about why the mail is on this queue.
        #
        # Except when that item is `nothing_extracted`. That is the residual bucket — it means "no
        # branch above claimed this", which is a description of our own failure to classify and not
        # of the message. When the message also carries an attachment nobody could read, that
        # attachment *is* why nothing was extracted, and the cause is worth more at the front of the
        # sentence than the symptom. Priority then decides, and it already ranks every real code
        # above `nothing_extracted`.
        head = next((i for i in ranked
                     if i.kind == "email" and i.code != "nothing_extracted"), ranked[0])

        # Then a count of everything else, by code. Not a list of the other items: 26 filenames is
        # not a reason, and the count is what decides whether this message is worth opening now.
        rest: Dict[tuple, int] = {}
        for other in ranked:
            if other is head:
                continue
            key = (other.code, other.kind)
            rest[key] = rest.get(key, 0) + 1
        clauses = []
        for (code, kind), n in sorted(rest.items(), key=lambda kv: order.get(kv[0][0], len(order))):
            noun = ("attachment" if kind == "attachment" else "flag") + ("" if n == 1 else "s")
            clauses.append(f"{n} {noun} {_ROLLUP_PHRASE.get(code, code)}")
        reason = " · ".join([head.reason] + clauses)

        merged.append(ManualItem(
            kind="email",
            # The subject, never the head item's `ref` — that is a filename when the only thing
            # wrong with a message is its attachments, and a filename is not what the row is about.
            ref=head.subject or head.email_id,
            ref_id=None,
            email_id=head.email_id,
            subject=head.subject,
            # Newest of the group, so the sort below still lands what just happened at the top. The
            # head is chosen by priority, which has nothing to say about time.
            when=max((i.when or "" for i in group), default=""),
            reason=reason,
            detail=head.detail,
            code=head.code,
            codes=codes,
            rolled_up=len(group),
            # The file the sentence is about, when the sentence is about a file. This column is
            # otherwise empty on every message row, and losing the filename was the one real cost
            # of merging: a message whose single problem is `signed-bol.pdf` should still say so
            # without being opened. Only the head's name — 26 filenames is not a column.
            item=head.ref if head.kind == "attachment" else "",
            sender=next((i.sender for i in ranked if i.sender), ""),
            filed_to=next((i.filed_to for i in ranked if i.filed_to), ""),
        ))
    return merged


def summary(conn: sqlite3.Connection) -> Summary:
    by_category = email_log.counts_by_category(conn)
    emails = sum(by_category.values())
    # Superseded rows are excluded. They are a second, poorer read of a document another
    # attachment on the same message states better — kept as evidence, never offered as work — and
    # counting them here would inflate "records" with rows no page will ever show.
    records_total = conn.execute(
        "SELECT COUNT(*) FROM extracted_records WHERE status <> ?",
        (extracted_records_store.SUPERSEDED,)).fetchone()[0]
    # Counted through `records_ready` rather than by repeating `_READY_CLAUSE` here. The header
    # figure and the table under it have to be the same number, and the moment completeness became
    # part of "ready" a bare SQL count started saying 41 over a table showing 30.
    ready = len(records_ready(conn))
    postable = len(records_postable(conn))
    ocr_pages = conn.execute("SELECT COALESCE(SUM(ocr_attempted), 0) FROM email_log").fetchone()[0]
    last_run = conn.execute("SELECT MAX(processed_at) FROM email_log").fetchone()[0]
    return Summary(
        emails=emails,
        by_category=by_category,
        records_total=records_total,
        records_ready=ready,
        records_postable=postable,
        needs_human=len(manual_queue(conn)),
        records_awaiting_confirmation=len(records_awaiting_confirmation(conn)),
        ocr_pages=ocr_pages or 0,
        last_run=last_run,
    )


def mails(conn: sqlite3.Connection) -> List[MailRow]:
    """Every email with its verdict, plus what it actually produced.

    The two correlated counts are the point: a `surface` verdict that yielded zero records is a
    different problem from a `route`, and neither is visible from the verdict alone.
    """
    flagged = tuple(sorted(attachment_ledger.NEEDS_ATTENTION))
    placeholders = ", ".join("?" * len(flagged))
    rows = _rows(conn, f"""
        SELECT e.email_id, e.subject, e.sender, e.origin_sender, e.email_date, e.category,
               e.matched_rule, e.reason, e.folder, e.po_hints, e.attachment_count,
               e.ocr_attempted, e.processed_at, e.source_folder,
               (SELECT COUNT(*) FROM attachment_ledger a
                 WHERE a.email_id = e.email_id
                   AND a.disposition IN ({placeholders}))       AS attachments_flagged,
               (SELECT COUNT(*) FROM extracted_records r
                 WHERE r.source_email_id = e.email_id)          AS records
          FROM email_log e
         ORDER BY e.processed_at DESC, e.id DESC
    """, flagged)
    return [MailRow(
        email_id=r["email_id"], subject=r["subject"], sender=r["sender"],
        origin_sender=r["origin_sender"], email_date=r["email_date"], category=r["category"],
        matched_rule=r["matched_rule"], reason=r["reason"], folder=r["folder"],
        po_hints=r["po_hints"], attachment_count=r["attachment_count"],
        attachments_flagged=r["attachments_flagged"], ocr_attempted=r["ocr_attempted"],
        records=r["records"], processed_at=r["processed_at"],
        source_folder=r["source_folder"] or "",
    ) for r in rows]


# Every store the Mail page reads, newest-preferred first. Two entries today because the `.msg`
# corpus is still the only source of real delivery mail — Premier's receiving mailbox holds nothing
# but internal broadcasts until they set forwarding up.
#
# The `.msg` corpus was removed on 2026-08-12: the live inbox now carries the real traffic, and
# scaffolding that shows test data beside it is worse than no scaffolding. Deleting the `sample`
# entry was the entire removal, as this list was built to allow — the page, the badges and the
# message links all read it.
#
# `sample_state.sqlite3` is not deleted and its rows are not filtered out; they are simply no longer
# read. Restoring the corpus means putting the entry back, nothing else.
MAIL_SOURCES = (
    ("inbox", "Inbox", "PIPELINE_STATE_DB_PATH"),
)


def mails_across_sources(sources=None) -> List[MailRow]:
    """Every processed email from every store, newest first, each row tagged with where it lives.

    Opens each store itself rather than taking a connection: the whole point is that there is more
    than one, and a caller holding a single connection cannot express that. Each is opened
    read-only-in-effect — `mails()` only selects — and closed before the next.

    A store that cannot be opened is skipped rather than raised: a missing corpus file on a fresh
    checkout must not take down the page that would tell you it is missing.
    """
    from config import settings
    from pipeline import state_db

    out: List[MailRow] = []
    for key, label, setting_name in (sources or MAIL_SOURCES):
        path = getattr(settings, setting_name)
        try:
            conn = state_db.get_connection(path)
        except Exception:                                          # noqa: BLE001
            continue
        try:
            for row in mails(conn):
                row.source, row.source_label = key, label
                out.append(row)
        finally:
            conn.close()
    # By the date on the mail, not by when we processed it: a corpus re-run would otherwise lift all
    # 14 sample rows above live mail that is genuinely newer.
    out.sort(key=lambda r: (r.email_date, r.email_id), reverse=True)
    return out


_SUMMARY_TTL_SECONDS = 3.0
"""How long a header summary may be reused. Three seconds, chosen against how the app is used: it
covers the burst of renders that one person clicking through the rail produces, and expires long
before anyone could walk away and come back to a stale figure.

The TTL is the backstop, not the mechanism. `invalidate_summary()` is what keeps the numbers honest
after a write — see there."""

_summary_cache: Tuple[Optional[float], Optional[Tuple], Optional[Summary]] = (None, None, None)
_summary_lock = threading.Lock()


def invalidate_summary() -> None:
    """Forget the cached header summary. **Call this from anything that writes.**

    Every `/ui` POST redirects straight into a GET that redraws the header, so a purely
    time-based cache would show the operator the figures from *before* the action they just took —
    which is the one moment the numbers are being read closely. Dropping the entry on the way out of
    a write means the redirect that follows recomputes, and the TTL only ever serves reads.
    """
    global _summary_cache
    with _summary_lock:
        _summary_cache = (None, None, None)
    # The queue cache is deliberately *not* dropped here. It is keyed on `PRAGMA data_version` and
    # `conn.total_changes`, so the write that prompted this call has already moved its key and the
    # next reader will rebuild on its own. Clearing it as well would throw the other stores' entries
    # away too, and make the page rendered immediately after every action — the one moment the
    # figures are read closely, and the moment this app felt slowest — pay 846 ms to rebuild a queue
    # it could have been handed. `invalidate_manual_queue()` remains for anything that needs it.


def summary_across_sources(sources=None) -> Summary:
    """The header figures, totalled over every store the Mail page lists.

    Added because the strip contradicted the table the moment the two mail pages merged: it read
    "14 emails" from the corpus alone while 26 rows sat underneath it. A header that disagrees with
    the page it heads is worse than no header.

    `summary(conn)` is untouched and still answers for one store — that is what the tests and the
    CLI runner want.

    **Memoised for `_SUMMARY_TTL_SECONDS`,** because this is the most expensive thing a page render
    does and every page renders it. `summary()` cannot be made cheap: `records_ready`,
    `records_postable` and `needs_human` are counted by materialising the lists and taking `len()`,
    and each of those filters in Python (`completeness.is_complete`, `post_decision.can_post_offline`)
    rather than in SQL, so there is no `COUNT(*)` to fall back to. Against the live store that is
    ~700ms per call, of which `manual_queue` building 4,746 dataclasses to be counted is ~430ms.

    A copy is returned, never the cached object: `Summary` is a mutable dataclass with a mutable
    `by_category`, and one caller adding a category to what it thought was its own result would
    corrupt every later reader.
    """
    global _summary_cache
    from config import settings
    from pipeline import state_db

    key = tuple(sources) if sources is not None else None
    now = time.monotonic()
    with _summary_lock:
        at, cached_key, cached = _summary_cache
        if cached is not None and cached_key == key and at is not None \
                and now - at < _SUMMARY_TTL_SECONDS:
            return replace(cached, by_category=dict(cached.by_category))

    total = Summary(by_category={})
    for _key, _label, setting_name in (sources or MAIL_SOURCES):
        try:
            conn = state_db.get_connection(getattr(settings, setting_name))
        except Exception:                                          # noqa: BLE001
            continue
        try:
            part = summary(conn)
        finally:
            conn.close()
        total.emails += part.emails
        total.records_total += part.records_total
        total.records_ready += part.records_ready
        total.records_postable += part.records_postable
        total.needs_human += part.needs_human
        total.records_awaiting_confirmation += part.records_awaiting_confirmation
        total.ocr_pages += part.ocr_pages
        for category, count in part.by_category.items():
            total.by_category[category] = total.by_category.get(category, 0) + count
        # The most recent run across all of them — "last run" of the whole pipeline, not of one file.
        if part.last_run and (total.last_run is None or part.last_run > total.last_run):
            total.last_run = part.last_run

    with _summary_lock:
        _summary_cache = (time.monotonic(), key, total)
    return replace(total, by_category=dict(total.by_category))


_FIXABLE_CLAUSE = """
    r.status IN ('pending', 'failed')
    AND COALESCE(TRIM(r.po_number), '') <> ''
"""
"""A record a person may still correct — deliberately wider than `_READY_CLAUSE`.

The ready clause withholds a record for a quantity conflict and for zero confidence. Neither is a
reason it cannot be *edited*; both are reasons it needs to be. Resolving them is the entire purpose
of the edit form, so resolving the number is what clears the marker (`record_edit.CONFLICT_MARKER`).

Reaching the edit form through `_READY_CLAUSE` made every conflicted record answer 404 — and those
are 44 of the 65 record rows in the queue, its largest group. A PO number is still required,
because the form cannot supply one and every other field is meaningless without it.
"""


def records_fixable(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Records the manual queue lists and a person can still correct."""
    return _records_pending(conn, _FIXABLE_CLAUSE)


def _blocked_from_records(row) -> bool:
    """Whether something other than a missing field keeps this record off the Records page.

    Completeness is not the only way a record can be stuck. `_READY_CLAUSE` also withholds a record
    whose sources disagree on quantity, and one that was read with no confidence at all — neither
    of which is a *gap*, because every field is present. They are disagreements, and a person has
    to settle them.

    Without this, such a record appeared on **neither** page: kept off Records by the clause, and
    skipped by the queue for having nothing missing. Measured on Premier's live store, that was
    **21 records** — every one an Excel tracker line where two sources stated different quantities
    — sitting in the database as work nobody was ever told about. They predate the completeness
    split; the rule that no pending record belongs to neither page had simply stopped being true
    and nothing was checking it.
    """
    source = (row["extraction_source"] or "")
    return "quantity_conflict" in source or (row["extraction_confidence"] or 0) <= 0


def emails_with_a_possible_pod(conn: sqlite3.Connection) -> set:
    """Emails that could yield a proof of delivery, in one query.

    Deliberately a *superset* of what `spitfire_post._pod_for` will accept: any non-inline
    attachment already flagged `is_pod`, plus any PDF, because `_pod_for` re-reads a PDF that
    carries no stored verdict. Erring wide is the safe direction — this decides whether the Post
    button is drawn at all, and hiding a button on a record that would actually post is a worse
    failure than showing one that then refuses.

    Lives here rather than in `api/ui/routes.py`, where it was written, because `records_postable`
    needs the same answer and a view module importing a route module would be circular. One query
    for the whole page, never one per row.
    """
    rows = conn.execute(
        "SELECT DISTINCT email_id FROM attachment_ledger "
        "WHERE COALESCE(is_inline, 0) = 0 "
        "  AND (COALESCE(is_pod, 0) = 1 OR sniffed_kind = 'pdf') "
        # `_pod_for` rule 3: a record read from a stored document attaches that document.
        "UNION "
        "SELECT DISTINCT a.email_id FROM extracted_records r "
        "  JOIN attachment_ledger a ON a.id = r.source_ledger_id "
        "                          AND a.email_id = r.source_email_id "
        " WHERE a.blob_sha256 IS NOT NULL").fetchall()
    return {r[0] for r in rows}


POSTABLE = "postable"
BLOCKED = "blocked"
UNCHECKED = "unchecked"
"""The purchase order is not in the mirror, so gates 5-8 have no answer for this row.

**Not a refusal.** `spitfire_mirror` holds 71 of the purchase orders these records name; against
the rest `verify_record` reports no line matched, which is indistinguishable from a real absence.
`po_verify.mismatch_flags` carries the same warning for the same reason: an absent flag means
"nothing disagrees", never "not checked". Rendering these as blocked would tell a reviewer their
delivery is wrong when all that happened is that nobody has read that order yet.
"""


class LineVerdict(NamedTuple):
    """What the mirror says about one record's purchase-order line."""
    state: str
    reason: str
    line_number: Optional[int]


def mirror_line_verdicts(conn: sqlite3.Connection,
                         rows: Sequence[sqlite3.Row]) -> Dict[int, LineVerdict]:
    """Gates 5-8 for each row, read from the local mirror rather than from Spitfire.

    `records_postable` used to stop after gate 2 and say so: gates 3 and 4 "need I/O, and verifying
    every row live costs about 145 seconds". That is true of a **live** read and was never revisited
    when the mirror arrived. Measured 2026-09-16: these gates over all 385 ready rows, touching 71
    purchase orders, cost **0.04 seconds** — and `api/ui/routes.py` already loads the same mirror
    every render for `po_verify.mismatch_flags`, so the read is paid for either way.

    The verdicts are `POSTABLE`, `BLOCKED` (with the sentence `post_decision.line_refusal` wrote)
    and `UNCHECKED`. Three, not two — see `UNCHECKED`.

    Gate 3 is still not asked here: "has this delivery already posted" keys on the POD hash, which
    means resolving and hashing every proof, and that is not a list-view cost.
    """
    from pipeline import po_verify, post_decision, spitfire_mirror

    lines_by_po: Dict[str, list] = {}
    verdicts: Dict[int, LineVerdict] = {}
    for row in rows:
        po_number = str(row["po_number"] or "").strip()
        if po_number not in lines_by_po:
            lines_by_po[po_number] = spitfire_mirror.lines_for(conn, po_number)
        lines = lines_by_po[po_number]
        if not lines:
            verdicts[int(row["id"])] = LineVerdict(
                UNCHECKED, f"purchase order {po_number} has not been read from Spitfire yet", None)
            continue

        result = po_verify.verify_record(
            po_verify.facts_from_row(row), None, lines,
            post_decision._int_or_none(row["po_line_number"]))
        check = result.matched
        if check is None:
            verdicts[int(row["id"])] = LineVerdict(
                BLOCKED, f"no line on purchase order {po_number} matched this delivery", None)
            continue
        refusal = post_decision.line_refusal(check, po_number)
        verdicts[int(row["id"])] = LineVerdict(
            BLOCKED if refusal else POSTABLE, refusal, check.line_number)
    return verdicts


SPITFIRE_CANCELLED = "C"
"""`DocMasterDetail.Status` for a purchase order Spitfire holds as Canceled.

Read, never written. Setting it is `PATCH /api/document/{id}/Status`, which
`connectors/spitfire_write._DENIED_SUBSTRINGS` refuses by name because the same call marks a
receipt POD Confirmed with no review and no Fixed-Asset Accounting sign-off. Cancelling a
purchase order retires a commitment, so the update stays a person's to make in Spitfire; this
view exists to tell them which ones are outstanding and to notice when the job is done.
"""


def cancellation_worklist(conn: sqlite3.Connection) -> List[dict]:
    """Purchase orders a cancellation notice named, newest first, with what Spitfire holds.

    Triage routes these out of the delivery path at `stage1_triage` rule 0a — "order cancellation
    notice — needs a manual PO update in Spitfire, not a delivery event" — and that is where they
    stopped. Measured 2026-09-16: 66 cancellation notices, 7 of them naming a purchase order, and
    **none** of those 7 marked Canceled in Spitfire. The routing was right and nothing carried it
    the last step, which is what this page is for.

    `done` is read from the mirror rather than remembered here, so a purchase order somebody
    cancelled in Spitfire directly drops off this list at the next refresh without anyone
    telling us. A purchase order the mirror has never seen is `None` — not done, not known.

    `queued` counts records still pending against the order, because a cancelled order with rows
    on the Records page is how goods get received against something nobody is buying any more.
    """
    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT email_id, subject, sender, origin_sender, email_date, po_hints, reason
                 FROM email_log
                WHERE notification_type = 'order_cancellation'
                  AND COALESCE(TRIM(po_hints), '') <> ''
                ORDER BY email_date DESC"""
        ).fetchall()
        status = {str(r["po_number"]).strip(): (r["doc_status"], r["doc_status_label"])
                  for r in conn.execute(
                      "SELECT po_number, doc_status, doc_status_label FROM spitfire_po_index")}
        queued = {str(r["po_number"]).strip(): int(r["n"]) for r in conn.execute(
            "SELECT po_number, COUNT(*) n FROM extracted_records "
            " WHERE status = 'pending' GROUP BY po_number")}
    finally:
        conn.row_factory = prior_factory

    seen: Dict[str, dict] = {}
    for row in rows:
        for po_number in (p.strip() for p in str(row["po_hints"] or "").split(",")):
            # First wins: the query is newest-first, and the newest notice is the one a reviewer
            # should read. An order cancelled twice is still one job.
            if not po_number or po_number in seen:
                continue
            code, label = status.get(po_number, (None, None))
            seen[po_number] = {
                "po_number": po_number,
                "email_id": row["email_id"],
                "subject": row["subject"] or "",
                "sender": (row["origin_sender"] or row["sender"] or ""),
                "cancelled_at": (row["email_date"] or "")[:16],
                "spitfire_status": label or "",
                "done": None if code is None else str(code).strip() == SPITFIRE_CANCELLED,
                "queued": queued.get(po_number, 0),
            }
    return list(seen.values())


def cancellations_without_a_po(conn: sqlite3.Connection) -> int:
    """Cancellation notices naming no purchase order — counted, because they are not actionable.

    59 of the 66 on 2026-09-16. Listing them beside the seven that name an order would bury the
    work; saying how many there are keeps the omission visible rather than silent.
    """
    return int(conn.execute(
        "SELECT COUNT(*) FROM email_log WHERE notification_type = 'order_cancellation' "
        "  AND COALESCE(TRIM(po_hints), '') = ''").fetchone()[0])


def records_postable(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Records that would actually survive the Post button, not merely ones with no gaps.

    `records_ready` means "complete": all five `REQUIRED` fields present. That is necessary and it
    is not sufficient, and the difference is not small — measured on the live store, 169 records
    were complete and **45** could post. The rest have no proof of delivery, no waiver and no body
    evidence, so `post_decision` gate 2 refuses them.

    Both lists are kept because both questions are real. A complete-but-unprovable record must stay
    *visible* on the Records page — seeing the blocker is how a reviewer knows to waive the POD or
    chase a signature — but it must not be *counted* as ready, because that number is read as "work
    that can be done now".

    **Gates 5-8 and the line-collision rule are applied too**, both from the mirror. Measured on
    2026-09-16 this number read 330 while only 108 rows cleared every gate and only 54 survived the
    collision rule — the chip overstated the work by six times, and each of those rows offered a
    button that refused on click. `UNCHECKED` rows are not counted: nothing here can prove they
    post, and a count is a promise.

    **What still overstates, and by how much.** Gate 2 is asked through
    `emails_with_a_possible_pod`, which is a deliberate superset — any non-inline PDF counts as a
    possible proof. Resolving each one to bytes with `spitfire_post._pod_for` costs 0.8-1.2s even
    memoised, because one 4 MB PDF dominates the read, and about 120s across the whole ready set.
    Measured 2026-09-16: of the 56 rows this returns, `decide` accepts 54 and refuses 2 for a proof
    that does not resolve. That residual is left rather than paid for — it is a 4% overstatement
    against the 6x one this function was carrying, and `spitfire_post.pod_blocked` already asks the
    honest question in the confirm dialog, where one delivery's worth of blobs is a fair cost.

    Gate 3 is applied only in the weaker form the page can afford: a record already carrying a
    blocking attempt in `spitfire_post` is dropped. `decide` asks it of the *delivery*, keyed on
    the POD hash, which would mean resolving every proof to bytes — not a list-view cost. The
    weaker form still removes the rows whose proof is on a receipt already and whose real next
    step is the receiver report, which `_post_cell` has always rendered correctly while this
    count contradicted it.
    """
    from pipeline import post_decision, post_ledger

    pod_emails = emails_with_a_possible_pod(conn)
    survived_pod = [row for row in records_ready(conn)
                    if post_decision.can_post_offline(
                        row, has_pod_bytes=row["source_email_id"] in pod_emails)]

    blocking = {attempt.record_id for attempt in post_ledger.latest_by_record(conn)
                if attempt.state in post_ledger.BLOCKING}
    verdicts = mirror_line_verdicts(conn, survived_pod)
    clear = [row for row in survived_pod
             if verdicts[int(row["id"])].state == POSTABLE and int(row["id"]) not in blocking]
    return _without_line_collisions(clear, verdicts)


def _without_line_collisions(rows: Sequence[sqlite3.Row],
                             verdicts: Dict[int, LineVerdict]) -> List[sqlite3.Row]:
    """Drop rows whose delivery puts two records on one purchase-order line.

    `spitfire_post._refuse_line_collisions` is the rule, and it is called rather than restated: a
    receipt line holds one quantity, so two records resolving to one line are both refused and
    neither is summed. The page never consulted it, which is why 108 rows counted as ready while
    the grouped post would accept 54.
    """
    from pipeline import spitfire_post

    by_delivery: Dict[tuple, List[sqlite3.Row]] = {}
    for row in rows:
        by_delivery.setdefault(
            (str(row["po_number"] or "").strip(), row["delivery_id"]), []).append(row)

    kept: List[sqlite3.Row] = []
    for group in by_delivery.values():
        plans = [spitfire_post.LinePlan(record_id=int(row["id"]), ok=True,
                                        line_number=verdicts[int(row["id"])].line_number)
                 for row in group]
        spitfire_post._refuse_line_collisions(plans)
        refused = {plan.record_id for plan in plans if not plan.ok}
        kept.extend(row for row in group if int(row["id"]) not in refused)
    return kept


def confirmed_email_ids(conn: sqlite3.Connection) -> set:
    """Messages a person has confirmed the goods arrived on, in one query.

    Separate from `mail_overrides.ids_with` only in that every caller here wants it once per page
    rather than once per row — the same reasoning as `emails_with_a_possible_pod`.
    """
    return mail_overrides.ids_with(conn, mail_overrides.CONFIRMED)


def _awaits_confirmation(row, confirmed: set) -> bool:
    """Whether this record is a claim Premier made about itself, still unconfirmed.

    Two conditions, and the second is what makes it a queue rather than a wall: the message was
    **written** inside Premier, and nobody has confirmed it yet. Confirming releases every record
    on that message at once, which is the grain a person actually decides at — they are looking at
    one spreadsheet, not at row 604 of it.

    `origin_sender`, never `sender`. Every message in the mailbox is forwarded, so the envelope
    reads `premierpm.com` on Atlas's receiving reports too; eleven of the ninety-four postable
    records are exactly that, and reading the envelope would withhold them. `authorship` carries
    the full reasoning.
    """
    return (authorship.authored_internally(row["origin_sender"])
            and row["source_email_id"] not in confirmed)


def records_awaiting_confirmation(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Records read out of Premier's own mail that nobody has confirmed yet.

    The third destination. These are not incomplete and not in conflict — an expediting report
    states a purchase order, a spec, a quantity and a date, and passes every completeness test
    there is. What it does not state is that anything arrived, and no amount of filling in fields
    will make it say so, which is why this is a separate destination from `manual_queue` rather
    than another kind of gap. There is nothing on these rows for a person to fix; there is only
    something for a person to confirm.

    Every pending record on such a message, not only the ones that would otherwise have been
    postable. `_READY_CLAUSE` is deliberately not applied: a half-read row of an expediting sheet
    is no more a delivery than a fully-read one, and asking somebody to fill in its missing spec
    code would be asking them to finish a receipt for goods nobody says arrived. The queue folds
    exactly this set into its one-row-per-message items, so the count here and the rows there
    describe the same records.

    `failed` is excluded and stays on the queue as itself. A record that failed is a thing that
    went wrong in our software, which is real work whoever wrote the message.

    Ordered like the Records page so the two read the same way.
    """
    confirmed = confirmed_email_ids(conn)
    return [row for row in _records_pending(conn, "r.status = 'pending'")
            if _awaits_confirmation(row, confirmed)]


def records_ready(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Records that can actually be posted — nothing missing, nothing to decide first.

    Two filters, and the second is the one that makes this page honest. `_READY_CLAUSE` is the
    cheap SQL pre-filter; `completeness.is_complete` is the same test `post_decision` gate 1
    applies, so a row here is a row the Post button will accept.

    The completeness half is deliberately **not** folded into `_READY_CLAUSE` as SQL. All five
    `REQUIRED` fields are columns and the clause could be written — but then the rule would exist
    in two places and drift, and `completeness._is_present` carries a subtlety SQL invites you to
    get wrong: **zero is present**. A delivered quantity of 0 is a real statement — a shipment that
    arrived empty, a line cancelled at the dock — and `quantity_received <> 0` would silently hide
    exactly the case a person most needs to see. Filtering in Python keeps one definition. The
    store holds under a hundred pending rows; this is not the expensive part of the page.

    The third filter is `_awaits_confirmation`, and it is a different kind of question from the
    other two: those ask whether the record is *finished*, this asks whether anyone outside Premier
    ever said the goods arrived. A complete record can fail it, and does — 1,067 of them.
    """
    confirmed = confirmed_email_ids(conn)
    return [row for row in _records_pending(conn)
            if completeness.is_complete(row) and not _awaits_confirmation(row, confirmed)]


def _records_pending(conn: sqlite3.Connection,
                     where: Optional[str] = None) -> List[sqlite3.Row]:
    """Records in one projection, narrowed by `where`. Defaults to `_READY_CLAUSE`.

    Split out so the Records page and the edit form read the same columns through the same query,
    and so a caller that wants the *unready* ones does not have to reach past `records_ready` into
    raw SQL to find them.

    **Records read out of a message somebody set aside are excluded here, for every caller.**
    "Not a delivery" is a statement about the whole message, and a record is a claim that goods on
    it arrived — so the claim cannot outlive the verdict. Filtered in this one projection rather
    than in each of the three destinations because that is what closes the *posting* route too:
    `routes.post_report_fragment` loads its row through `records_ready`, so a record that never
    appears here can never be posted, by construction instead of by a second guard somebody has to
    remember. Measured before this clause: 253 records from the 18 set-aside messages were still
    live, 251 of them still counted as awaiting confirmation.

    Derived from the verdict rather than written onto the row, so pressing "This is a delivery"
    brings every one of them back — the same reasoning `email_log.not_a_delivery` is derived under.
    """
    return _rows(conn, f"""
        SELECT r.id, r.po_number, r.po_line_number, r.spec_code, r.parent_spec_code,
               r.sub_spec_suffix, r.item_description, r.quantity_received, r.unit_of_measure,
               r.package_quantity, r.package_uom, r.pod_stated_date, r.carrier_name,
               r.tracking_number, r.received_by, r.delivery_location, r.vendor_name,
               r.shipment_number, r.notification_number, r.extraction_source,
               r.extraction_confidence, r.status, r.source_email_id,
               COALESCE(r.origin, 'auto') AS origin, r.created_by, r.manual_note,
               r.pod_ledger_id, r.pod_source, r.pod_waived_by, r.pod_waived_at,
               r.source_ledger_id,
               COALESCE(e.subject, '') AS email_subject,
               -- Who *wrote* the message, not whose mailbox it came from. `authorship` explains
               -- why the envelope sender is the wrong column and what reading it would cost.
               COALESCE(e.origin_sender, '') AS origin_sender,
               r.delivery_id,
               COALESCE(d.delivery_ref, '')  AS delivery_ref,
               COALESCE(d.delivery_rung, '') AS delivery_rung,
               (SELECT COUNT(*) FROM extracted_records s
                 WHERE s.delivery_id = r.delivery_id) AS delivery_lines
          FROM extracted_records r
          LEFT JOIN email_log e  ON e.email_id = r.source_email_id
          LEFT JOIN deliveries d ON d.id = r.delivery_id
         WHERE {where or _READY_CLAUSE}
           AND {_NOT_SET_ASIDE}
         -- Grouped by delivery, newest delivery first, and by line number within it.
         --
         -- The ordering used to be `r.id DESC` alone, on the reasoning that arrival order cannot
         -- drift and a record extracted a minute ago should greet you. That still holds *between*
         -- deliveries, which is why the outer key is the newest id in the group. What it could not
         -- express is that six rows of one purchase order are one delivery: staged in one pass they
         -- happened to sit together, and after any re-extraction they did not.
         --
         -- Within a delivery the order is the purchase order's own line numbering, so the block
         -- reads the way Spitfire's receipt does rather than the way extraction happened to emit it.
         -- `COALESCE(..., r.id)` is what keeps a row with no delivery in its right place. The
         -- subquery is NULL for those, and a NULL sort key would bunch every ungrouped row at one
         -- end regardless of when it arrived — which is most of a store that has not been
         -- backfilled, and would look like the ordering had simply broken.
         ORDER BY COALESCE((SELECT MAX(s.id) FROM extracted_records s
                             WHERE s.delivery_id IS NOT NULL
                               AND s.delivery_id = r.delivery_id), r.id) DESC,
                  COALESCE(r.delivery_id, -r.id) DESC,
                  COALESCE(r.po_line_number, 999999),
                  r.id
    """)


ATTACHMENT_SEARCH_COLUMNS = (
    "a.filename", "a.sniffed_kind", "a.disposition", "a.claimed_by", "a.pod_po_numbers",
    "a.blob_sha256", "a.sha256", "e.subject", "e.sender", "e.po_hints",
)
"""What a typed search looks in, server-side. The same columns the browser filter reads off
the rendered row, so moving the search across the wire does not change what it finds."""



# --- the rest of one conversation -----------------------------------------------------------------

@dataclass
class ThreadSibling:
    """Another message of the same conversation, and what setting it aside would take with it."""
    email_id: str
    subject: str
    sender: str
    when: str
    records: int
    excluded: str = ""
    """Why this one must not be swept along — empty when it can be. A message somebody already
    ruled on, or built a record from by hand, is still *listed*: hiding it would leave its rows on
    the queue with nothing on screen explaining why."""


_THREAD_PREFIX_RE = re.compile(
    r"^(?:\s*(?:re|fw|fwd|aw|tr|antw)\s*:\s*|\s*\[[^\]]{1,24}\]\s*|\s*\{[^}]{1,24}\}\s*)+",
    re.IGNORECASE,
)

_GENERIC_SUBJECT_LENGTH = 20
"""Below this, a subject with no purchase order behind it is not evidence of anything.

`Delivery confirmation` is what nine unrelated vendors call nine unrelated messages, and gathering
them together would set aside a real delivery on the strength of a shared word."""


def thread_subject(subject: Optional[str]) -> str:
    """A subject with the reply and forward chrome taken off, for comparing one against another.

    `Fwd: FW: [External] RE: Cameo…` and `Cameo…` are the same conversation to a person and two
    different strings to SQL. Every prefix comes off, repeatedly, because real mail carries three
    or four of them stacked up by the time it has been round a property and back.
    """
    text = (subject or "").strip()
    while True:
        shorter = _THREAD_PREFIX_RE.sub("", text).strip()
        if shorter == text:
            return " ".join(shorter.split()).lower()
        text = shorter


def _po_hints(value: Optional[str]) -> set:
    return {part.strip() for part in (value or "").replace(";", ",").split(",") if part.strip()}


def thread_siblings(conn: sqlite3.Connection, email_id: str) -> List[ThreadSibling]:
    """The other messages of this conversation, newest first.

    There is no thread id in `email_log` — Graph has `conversationId` and this pipeline never stored
    it — so membership is established from what is there, and deliberately from *two* things at
    once:

    * the same subject, with the reply and forward prefixes stripped, and
    * a purchase order in common.

    Either alone is wrong in a way that matters. Subject alone gathers nine vendors' "Delivery
    confirmation" into one decision; a shared purchase order alone gathers every message about a PO
    that has been running for four months. A long subject with no purchase order on either side is
    allowed, because a twenty-character subject is specific enough to be a conversation and plenty
    of real threads name no PO in the log.

    Nothing here writes. It answers the question the set-aside page asks — "what else is this?" —
    and every id it returns is checked again before any verdict is written against it.
    """
    prior = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        me = conn.execute(
            "SELECT email_id, subject, po_hints FROM email_log WHERE email_id = ?",
            (email_id,)).fetchone()
        if me is None:
            return []
        subject = thread_subject(me["subject"])
        if not subject:
            return []
        my_pos = _po_hints(me["po_hints"])

        decided = {r["email_id"]: r for r in conn.execute(
            "SELECT email_id, verdict, decided_by FROM mail_overrides")}
        touched = {r["email_id"]: r["who"] for r in conn.execute(
            """SELECT source_email_id AS email_id,
                      COALESCE(MAX(created_by), '') AS who
                 FROM extracted_records
                WHERE origin = 'manual' OR status NOT IN ('pending', 'failed')
                   OR pod_waived_at IS NOT NULL
                GROUP BY source_email_id""")}
        counts = {r["email_id"]: r["n"] for r in conn.execute(
            """SELECT source_email_id AS email_id, COUNT(*) AS n
                 FROM extracted_records
                WHERE status IN ('pending', 'failed')
                GROUP BY source_email_id""")}

        out: List[ThreadSibling] = []
        for row in conn.execute(
                """SELECT email_id, subject, sender, po_hints, processed_at, email_date
                     FROM email_log WHERE email_id <> ?
                    ORDER BY COALESCE(email_date, processed_at) DESC""", (email_id,)):
            if thread_subject(row["subject"]) != subject:
                continue
            theirs = _po_hints(row["po_hints"])
            if my_pos or theirs:
                if not (my_pos & theirs):
                    continue
            elif len(subject) < _GENERIC_SUBJECT_LENGTH:
                continue

            why = ""
            standing = decided.get(row["email_id"])
            if standing is not None and standing["verdict"] != mail_overrides.NOT_DELIVERY:
                why = (f"{standing['decided_by'] or 'somebody'} already decided this one — "
                       f"leave it to them")
            elif row["email_id"] in touched:
                who = touched[row["email_id"]] or "somebody"
                why = f"{who} has already worked on its records by hand"
            out.append(ThreadSibling(
                email_id=row["email_id"], subject=row["subject"] or "",
                sender=row["sender"] or "", when=str(row["email_date"] or row["processed_at"] or ""),
                records=int(counts.get(row["email_id"], 0)), excluded=why))
        return out
    finally:
        conn.row_factory = prior


def attachment_count(conn: sqlite3.Connection, *, include_inline: bool = True,
                     q: str = "", inline_only: bool = False, unread_only: bool = False,
                     since: str = "", until: str = "") -> int:
    """How many rows the current search matches — the total the pager counts against.

    Asked separately from the page itself, because "showing 1-25 of 11,012" is the part that
    tells someone whether what they want is even in the table, and a `LIMIT`ed query cannot
    answer it.
    """
    where, params = _attachment_where(include_inline, q, inline_only=inline_only,
                                      unread_only=unread_only, since=since, until=until)
    return conn.execute(
        "SELECT COUNT(*) FROM attachment_ledger a "
        "LEFT JOIN email_log e ON e.email_id = a.email_id " + where, params).fetchone()[0]


# "Nothing was read out of this file", as SQL. The page used to decide this per row in Python
# (`routes._attachment_state`) and hand the answer to a dropdown in the browser, which meant every
# one of the 29,766 rows had to be rendered before one of them could be filtered out. Same three
# conditions, same union, evaluated where the rows are.
UNREAD_CLAUSE = ("(a.error_type IS NOT NULL OR a.disposition IN ('corrupt', 'empty') "
                 "OR COALESCE(a.blob_sha256, a.sha256, '') = '')")

ATTACHMENT_SORTS = {
    # What a heading may sort by, as a whitelist. The page passes a key from a query string
    # straight in, so this is the boundary: a name that is not here picks the default, and nothing
    # a caller writes ever reaches the ORDER BY.
    "received": "a.first_seen_at",
    "filename": "a.filename COLLATE NOCASE",
    "type": "a.sniffed_kind",
    "records": "a.records_extracted",
    "disposition": "a.disposition",
    "size": "a.size_bytes",
    "subject": "e.subject COLLATE NOCASE",
}


def _attachment_where(include_inline: bool, q: str, *, inline_only: bool = False,
                      unread_only: bool = False, since: str = "", until: str = "") -> tuple:
    """The WHERE shared by the page query and its count, so the two cannot disagree."""
    clauses, params = [], []
    if inline_only:
        clauses.append("COALESCE(a.is_inline, 0) = 1")
    elif not include_inline:
        clauses.append("COALESCE(a.is_inline, 0) = 0")
    if unread_only:
        clauses.append(UNREAD_CLAUSE)
    # Half-open on the upper end via `< date+1day` rather than `<= date`, because `first_seen_at`
    # carries a time: `<= '2026-09-16'` excludes everything that arrived on the 16th.
    if since:
        clauses.append("a.first_seen_at >= ?")
        params.append(since)
    if until:
        clauses.append("a.first_seen_at < datetime(?, '+1 day')")
        params.append(until)
    for term in (q or "").split():
        # Every term must appear somewhere on the row, which is how the browser filter reads
        # too: "fedex pdf" means both, in any column, not the phrase.
        ors = " OR ".join(f"COALESCE({c}, '') LIKE ?" for c in ATTACHMENT_SEARCH_COLUMNS)
        clauses.append(f"({ors})")
        params.extend([f"%{term}%"] * len(ATTACHMENT_SEARCH_COLUMNS))
    return ("WHERE " + " AND ".join(clauses) if clauses else ""), params


def attachments(conn: sqlite3.Connection, *, include_inline: bool = True,
                q: str = "", limit: int = 0, offset: int = 0, inline_only: bool = False,
                unread_only: bool = False, since: str = "", until: str = "",
                sort: str = "", descending: bool = True) -> List[sqlite3.Row]:
    """Every file the system has taken off an email, newest message first.

    The whole ledger, not a subset. `/ui/manual` already shows the ones needing attention and the
    mail dialog shows one message's worth; neither answers "what have we actually got?", which is
    the question that surfaces the things no single-message view can — the same file arriving twice
    under different names, or a 3 MB photograph that produced thirteen records and is still not
    marked as a proof of delivery.

    `include_inline=False` drops signature logos and letterhead. They are genuine attachments and
    are listed by default for that reason, but they outnumber the real ones better than two to one,
    so the page offers a way to put them aside.

    `ordinal` matters as much as `id` here: `/ui/mail/attachment` is keyed by `(email_id, ordinal)`,
    so it is the ordinal — not the ledger id — that lets a row open its own file.

    `limit` and `offset` page the query itself, and `q` filters it. The browser used to do both
    over every row, which meant the server rendered all 11,012 of them to display 25: 25 MB of
    HTML and 4.2 seconds before the browser had even started. `search_box`'s docstring set the
    condition for moving them — "if these tables ever run to thousands of rows that trade changes"
    — and this table is past it.

    `limit=0` keeps the old behaviour of returning everything, which is what the CSV export wants.
    """
    where, params = _attachment_where(include_inline, q, inline_only=inline_only,
                                      unread_only=unread_only, since=since, until=until)
    window = ""
    if limit:
        window = f" LIMIT {int(limit)} OFFSET {int(offset)}"
    # Whitelisted, then interpolated. `ATTACHMENT_SORTS` is the only source of the column name, so
    # what arrives from the query string selects a key and never becomes SQL.
    column = ATTACHMENT_SORTS.get(sort or "", "a.first_seen_at")
    way = "DESC" if descending else "ASC"
    # The trailing keys never change with the sort: they are what keeps one email's attachments
    # together and in their own container order, and what makes the order total so that paging
    # cannot show the same row on two pages.
    order = f"ORDER BY {column} {way}, a.id DESC, a.email_id, a.depth, a.ordinal"
    return _rows(conn, f"""
        SELECT a.id, a.email_id, a.ordinal, a.depth, a.filename, a.sniffed_kind,
               a.declared_content_type, a.size_bytes, a.is_inline, a.disposition,
               a.disposition_detail, a.claimed_by, a.records_extracted, a.error_type,
               a.first_seen_at, COALESCE(a.blob_sha256, a.sha256, '') AS stored_sha,
               COALESCE(a.is_pod, 0) AS is_pod, COALESCE(a.pod_po_numbers, '') AS pod_po_numbers,
               a.pod_delivery_date, a.pod_signed_by,
               COALESCE(e.subject, '') AS email_subject, e.email_date, e.sender,
               -- The two weaker kinds of PO evidence. `pod_po_numbers` above is the file saying
               -- which order it is proof for; these two are the message saying it, and the records
               -- that message produced saying it. Worth showing a reader, not worth attaching a
               -- document to a receipt on — which is why `spitfire_post._pod_for` ignores both.
               COALESCE(e.po_hints, '') AS email_po_hints,
               rec.pos AS pos_via_records
          FROM attachment_ledger a
          LEFT JOIN email_log e ON e.email_id = a.email_id
          -- Grouped once and joined, not asked per row. As a correlated subquery this ran
          -- 29,766 times on Premier's live store -- each execution building its own temp B-tree
          -- for the DISTINCT -- to answer a question with one row per email.
          LEFT JOIN (SELECT source_email_id, GROUP_CONCAT(DISTINCT po_number) AS pos
                       FROM extracted_records GROUP BY source_email_id) rec
                 ON rec.source_email_id = a.email_id
          {where}
         -- Newest first by default, keyed on when we saw the attachment rather than on the
         -- email's own date. `email_date` is the envelope date, and for forwarded mail that is the
         -- forwarding date: twelve of the fourteen corpus messages were forwarded on one afternoon,
         -- so ordering by it collapsed months of deliveries onto a single instant. `first_seen_at`
         -- is stamped by ingest, is monotonic, and cannot be rewritten by whoever forwarded it.
         {order}
         {window}
    """, params)


@dataclass
class PoRow:
    """One purchase order, as the pipeline currently understands it."""
    po_number: str
    status: str
    label: str
    notifications: Dict[str, int]      # notification_type -> count, from `email_log`
    records: int
    qty_received: float
    lines_seen: int                    # distinct po_line_number values the mail stated outright
    released_at: Optional[str]
    release_reason: str
    last_seen: str
    qty_ordered: Optional[float] = None      # None until the Spitfire mirror is populated
    qty_outstanding: Optional[float] = None
    delivery_location: str = ""
    received_by: str = ""
    """Where the goods were received and who signed. The mail states both outright — "Received
    at Example Storage Moving & Storage - Riverside / Received By Jordan T." — and no status word
    can tell a reader whether that was the final destination. The address can."""


@dataclass
class PoEvent:
    """One email, as it appears on a purchase order's timeline."""
    email_id: str
    when: str                    # envelope date — when *we* received it. Ordering and dedupe only.
    subject: str
    notification_type: str
    category: str
    sender: str
    reason: str
    stages: Sequence[str] = ()   # every lifecycle stage this notice proves; empty if it proves none
    origin_sent_at: str = ""     # when the payload was actually sent, from the quoted header

    @property
    def event_on(self) -> str:
        """The date to show for this notice: when it was sent, not when it reached us.

        `when` is kept for ordering and dedupe because it is always present and always comparable.
        It must not be *displayed*: twelve of the fourteen corpus messages are forwards Premier
        sent on one day, so showing `when` put 2026-06-06 on every stage of every purchase order.
        """
        return (self.origin_sent_at or self.when or "")[:10]

    @property
    def furthest(self) -> Optional[str]:
        """The most advanced stage this one notice proves — what the timeline labels it with."""
        on_ladder = [s for s in self.stages if s in delivery_status.LIFECYCLE]
        if on_ladder:
            return max(on_ladder, key=delivery_status.rank)
        return self.stages[0] if self.stages else None


@dataclass
class Stage:
    """One node on the progress bar."""
    key: str
    label: str
    reached: bool
    on: str = ""                 # date of the earliest email proving *this* stage; "" if none
    terminal: bool = False
    stated: bool = False
    """True when `on` is a date the mail states for this event — a warehouse notice's own
    `Received Date:` — rather than the date some email about it was sent. The strongest evidence
    a node can carry, and worth distinguishing: "the goods arrived on the 24th" is a different
    claim from "someone wrote about them on the 1st"."""

    conflict: str = ""
    """Why this node's date cannot be right, in words a reader can act on. Empty when it is fine.

    Set when a node is dated *before* the purchase order itself. That is not something to correct
    silently: either Spitfire's date is not the order date, or the PO really was raised after the
    goods moved (Premier re-raises POs — 908453 was lost and replaced by 911400). Both are worth
    someone's attention, and quietly shuffling the dates until the bar looked plausible would
    destroy the only evidence that either happened."""


@dataclass
class PoTimeline:
    po_number: str
    status: str
    label: str
    stages: List[Stage] = field(default_factory=list)
    events: List[PoEvent] = field(default_factory=list)
    released_at: Optional[str] = None
    release_reason: str = ""
    delivery_location: str = ""
    received_by: str = ""


# `notification_type` -> every lifecycle stage that notice proves. Types absent from this map leave
# the PO `open` rather than guessing: `warehouse_status_report` is the periodic PO Status Report and
# proves nothing about a delivery, and `unknown` proves nothing by definition.
#
# A warehouse inbound notice proves TWO stages, and that is the whole correction. Read one and it is
# plainly a receipt, not a waypoint notice:
#
#     Received Date:  10/09/2025
#     Received at:    Example Storage Moving & Storage - Riverside
#     Received By:    Jordan T.
#
# It records where the goods were received and who signed for them, and it is the event that triggers
# a receiver in Spitfire — Premier's own annotation on these messages is "straightforward, WH rec'd".
# So it proves `delivered`. It also proves `at_warehouse`, because that is demonstrably where they
# went, which keeps the route visible on the progress bar without lying about the status.
#
# The project named in the subject ("Example Hotel Downtown") is what the goods belong to, not a
# destination still to be reached. Treating that as a pending onward leg was the earlier mistake.
_NOTIFICATION_STATUS = {
    "property_confirmation": (delivery_status.DELIVERED,),
    "warehouse_inbound": (delivery_status.AT_WAREHOUSE, delivery_status.DELIVERED),
    # A FINAL_EVENT_TYPE alongside warehouse_inbound in stage 2 — same meaning, different sender.
    "inbound_notification": (delivery_status.AT_WAREHOUSE, delivery_status.DELIVERED),
    "delivered_shipped": (delivery_status.IN_TRANSIT,),
    "vendor_confirmation": (delivery_status.IN_TRANSIT,),
    # Terminal branches. Both are triaged ROUTE, so neither ever reaches `accumulation` — which is
    # why the PO list is sourced from `email_log`. Before that change these POs did not appear at all.
    "order_cancellation": (delivery_status.CANCELLED,),
    "loss_or_claim": (delivery_status.LOSS_OR_CLAIM,),
}

# What each notice calls *itself*, for showing beside what it proves. Kept adjacent to the map
# above so the two cannot drift, and so a new notification type is visibly missing a label.
#
# These are not our lifecycle words and must never be used as them. `delivered_shipped` is
# Authority's "Delivered Notification", and it maps to `in_transit` — see stage1_triage.py:145,
# "carrier delivered to the warehouse; waiting on the matching Inbound notification". Showing that
# phrase where a status belongs would read "Delivered" against goods that are still travelling,
# which is the single most expensive thing this interface could get wrong.
NOTIFICATION_LABELS = {
    "warehouse_inbound": "Inbound Notification",
    "inbound_notification": "Inbound Notification",
    "delivered_shipped": "Delivered Notification",
    "property_confirmation": "Property confirmation",
    "vendor_confirmation": "Vendor confirmation",
    "order_cancellation": "Order cancellation",
    "warehouse_status_report": "PO Status Report",
    "loss_or_claim": "Loss / claim notice",
    "verification_request": "Verification request",
    "unknown": "Unrecognised",
}


def notification_label(notification_type: str) -> str:
    """What the mail calls this notice. Falls back to the raw type made readable, so an unmapped
    one shows up as itself rather than as nothing."""
    return NOTIFICATION_LABELS.get(
        notification_type or "", (notification_type or "unknown").replace("_", " ")
    )


# Vendor -> partnered warehouse -> onward to the property is a real third path, and the 8 August
# meeting asked for it. It is NOT derivable from this mail: a notice for goods staying at the
# warehouse and one for goods moving on next week are byte-for-byte the same shape. It needs a
# second notification for the onward leg, or a marker Premier supplies. Left unsolved on purpose
# rather than guessed at — a wrong guess here is what turns one delivery into two receivers.


def _split_po_hints(value: Optional[str]) -> List[str]:
    """`email_log.po_hints` is a comma-space delimited blob (`'906725, 907665'`), not a key.

    Split and compared exactly rather than matched with LIKE: `LIKE '%2084%'` matches 908491, and a
    purchase-order page showing another order's mail is worse than one showing none.
    """
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _events_by_po(conn: sqlite3.Connection) -> Dict[str, List[PoEvent]]:
    """Every email, indexed by each PO it names.

    Sourced from `email_log`, not `accumulation`, and the difference is not cosmetic. Stage 2 drops
    two whole classes of mail: anything triaged ROUTE never accumulates at all (cancellations and
    loss/claim notices), and a notice arriving after its delivery has been released is discarded as
    a duplicate. On the corpus that is the difference between 2 and 3 events on PO 908491, and
    between 28 and 31 purchase orders overall. A timeline that quietly loses events is worse than
    no timeline.
    """
    by_po: Dict[str, List[PoEvent]] = {}
    seen: Dict[str, set] = {}
    for row in _rows(conn, """
        SELECT email_id, subject, sender, origin_sender, email_date, origin_sent_at,
               notification_type, category, reason, po_hints
          FROM email_log
      ORDER BY email_date, email_id
    """):
        event = PoEvent(
            email_id=row["email_id"],
            when=row["email_date"] or "",
            origin_sent_at=row["origin_sent_at"] or "",
            subject=row["subject"] or "",
            notification_type=row["notification_type"] or "unknown",
            category=row["category"],
            # Every corpus message is forwarded from example-pm.test, so the envelope sender is the
            # same on all of them; the recovered origin is the one that identifies who sent it.
            sender=row["origin_sender"] or row["sender"] or "",
            reason=row["reason"] or "",
            stages=_NOTIFICATION_STATUS.get(row["notification_type"] or "", ()),
        )
        for po_number in _split_po_hints(row["po_hints"]):
            by_po.setdefault(po_number, []).append(event)
            seen.setdefault(po_number, set()).add(event.email_id)

    # `accumulation` as a second source, deduplicated by email id. In a healthy store every
    # accumulated email also has an `email_log` row and this adds nothing — but `email_log` is
    # current-verdict-per-email and can be rewritten or forgotten independently, and a PO vanishing
    # from the delivery view because of a reprocess would be a silent loss. Union, not replace.
    for row in _rows(conn, """
        SELECT po_number, email_id, notification_type, category, received_at
          FROM accumulation
         WHERE COALESCE(TRIM(po_number), '') <> ''
      ORDER BY received_at, email_id
    """):
        po_number = row["po_number"]
        if row["email_id"] in seen.get(po_number, ()):
            continue
        by_po.setdefault(po_number, []).append(PoEvent(
            email_id=row["email_id"],
            when=row["received_at"] or "",
            # No subject or sender: accumulation stores neither as a column, and the alternative —
            # unpacking `payload_json` — would load base64'd attachment bytes to render one line.
            subject="(accumulated; no mail log entry)",
            notification_type=row["notification_type"] or "unknown",
            category=row["category"],
            sender="",
            reason="",
            stages=_NOTIFICATION_STATUS.get(row["notification_type"] or "", ()),
        ))
        seen.setdefault(po_number, set()).add(row["email_id"])

    for events in by_po.values():
        # By when the notice was *sent*, not when it reached us. A forwarded copy can arrive
        # months after a direct one and still be the older event — on PO 908491 the oldest
        # notice arrived last, so "oldest first" was wrong wherever these are shown in order.
        events.sort(key=lambda e: (e.event_on, e.when, e.email_id))
    return by_po

UNREACHABLE_STATUSES = (delivery_status.POD_SUBMITTED, delivery_status.PUSHED_TO_SPITFIRE)
"""Cannot be produced from pipeline state at all — not "none currently have these".

No receipt is staged in this store, and nothing writes to Spitfire. Note the difference from the
demo-side derivation in `api/services/po_status.py`, which cannot reach `at_warehouse`: this one
can, because Premier's real mail carries a `warehouse_inbound` notification type and the synthetic
data never did.

Still the vocabulary — `/ui/po` names both in its caveat, because a page that quietly omits what it
cannot show is worse than one that says so. They are simply not *nodes* on a timeline; see
`VISIBLE_LIFECYCLE`.
"""

VISIBLE_LIFECYCLE = tuple(
    status for status in delivery_status.LIFECYCLE if status not in UNREACHABLE_STATUSES
)
"""The stages a timeline draws. **Delivered is the last one.**

Deliberately derived here rather than by shortening `delivery_status.LIFECYCLE`. That tuple is
indexed by `rank()`, scanned by `rollup()` and iterated by `count_by_status()`, and
`api/services/po_status.py` imports it for the demo surface — removing members there would change
the rollup arithmetic and the demo API as a side effect of a display decision.

Two permanently dead nodes on every purchase order were the "fixed state" problem in its purest
form: they could never light, carried no date, and pushed the node a reader actually wanted —
where are the goods now — into the middle of the bar.
"""


def po_delivery_status(conn: sqlite3.Connection) -> List[PoRow]:
    """Every purchase order the pipeline has heard about, and where it stands.

    Driven from the mail, not from `extracted_records`, and the difference is the point: the corpus
    names 31 POs across its emails but extracts line detail from 3. The rest are property
    confirmations, cancellations and loss/claim notices — someone saying something about an order
    with nothing machine-readable in it. They are the population that most needs a person, so a view
    that started from extracted records would hide exactly the rows worth looking at.

    `qty_ordered` and `qty_outstanding` stay None while `spitfire_po_lines` is empty. They live on
    the purchase order inside Spitfire and no delivery email carries them, so filling them in would
    mean inventing numbers — the same reason the receiver report leaves Order Qty, Net and Final
    blank. Once the Spitfire pull has run they populate here with no further change.
    """
    events_by_po = _events_by_po(conn)
    records = _rows(conn, """
        SELECT po_number,
               COUNT(*) AS n,
               COALESCE(SUM(quantity_received), 0) AS qty,
               COUNT(DISTINCT po_line_number) AS lines_seen,
               MAX(email_date) AS last_seen,
               -- MAX() over a text column picks an arbitrary non-null value, which is what is
               -- wanted: every record on one PO carries the same receipt location and signer.
               MAX(delivery_location) AS delivery_location,
               MAX(received_by) AS received_by
          FROM extracted_records
         WHERE COALESCE(TRIM(po_number), '') <> ''
      GROUP BY po_number
    """)
    released = {r["po_number"]: r for r in _rows(conn, """
        SELECT po_number, MAX(released_at) AS released_at, release_reason
          FROM released_events
      GROUP BY po_number
    """)}
    ordered = {r["po_number"]: r for r in _rows(conn, """
        SELECT po_number,
               COALESCE(SUM(qty_ordered), 0) AS qty_ordered,
               COALESCE(SUM(qty_ordered - qty_received - qty_in_transit), 0) AS qty_outstanding
          FROM spitfire_po_lines
      GROUP BY po_number
    """)}

    by_po: Dict[str, Dict[str, int]] = {}
    last_seen: Dict[str, str] = {}
    for po_number, events in events_by_po.items():
        counts: Dict[str, int] = {}
        for event in events:
            counts[event.notification_type] = counts.get(event.notification_type, 0) + 1
            last_seen[po_number] = max(last_seen.get(po_number, ""), event.when)
        by_po[po_number] = counts

    record_by_po = {r["po_number"]: r for r in records}
    # A PO can have records but no mail naming it outright, or the reverse. Both belong on the page.
    for po_number, row in record_by_po.items():
        by_po.setdefault(po_number, {})
        last_seen[po_number] = max(last_seen.get(po_number, ""), row["last_seen"] or "")

    result: List[PoRow] = []
    for po_number, counts in by_po.items():
        record = record_by_po.get(po_number)
        status = _status_from_events(events_by_po.get(po_number, ()))
        release = released.get(po_number)
        qty = ordered.get(po_number)
        result.append(PoRow(
            po_number=po_number,
            status=status,
            label=delivery_status.STATUS_LABELS[status],
            notifications=counts,
            records=record["n"] if record else 0,
            qty_received=float(record["qty"]) if record else 0.0,
            lines_seen=record["lines_seen"] if record else 0,
            released_at=release["released_at"] if release else None,
            release_reason=release["release_reason"] if release else "",
            last_seen=last_seen.get(po_number, ""),
            qty_ordered=float(qty["qty_ordered"]) if qty else None,
            qty_outstanding=float(qty["qty_outstanding"]) if qty else None,
            delivery_location=(record["delivery_location"] if record else "") or "",
            received_by=(record["received_by"] if record else "") or "",
        ))

    # Most recently heard-about first: the page is read to answer "what is happening now".
    result.sort(key=lambda r: (r.last_seen, r.po_number), reverse=True)
    return result


def _status_from_events(events: Sequence[PoEvent]) -> str:
    """The furthest state this PO's mail proves.

    The *most* advanced notice wins here, which is the opposite of `delivery_status.rollup` and for
    a different reason: these notices describe one shipment's progress through time, not separate
    lines each with their own state. A warehouse-inbound notice does not stop being true because an
    in-transit notice arrived first.

    A terminal outcome overrides regardless of position — a cancelled order is not "in transit".
    """
    stages = [stage for e in events for stage in e.stages]
    if delivery_status.CANCELLED in stages:
        return delivery_status.CANCELLED
    if delivery_status.LOSS_OR_CLAIM in stages:
        return delivery_status.LOSS_OR_CLAIM
    on_ladder = [s for s in stages if s in delivery_status.LIFECYCLE]
    return max(on_ladder, key=delivery_status.rank) if on_ladder else delivery_status.OPEN


def po_timeline(conn: sqlite3.Connection, po_number: str) -> Optional[PoTimeline]:
    """The progress bar for one purchase order, and the mail behind it.

    Returns None when nothing anywhere names this PO — the caller turns that into a 404. An empty
    timeline and an unknown PO must not look the same.

    A stage shows a date only where an email actually proves *that* stage. Reaching "at warehouse"
    implies the goods were in transit, so that node is filled — but if no carrier notice ever
    arrived, it carries no date, because inventing one would be the same failure as inventing an
    order quantity.
    """
    events = sorted(_events_by_po(conn).get(po_number, []),
                    key=lambda e: (e.event_on, e.when, e.email_id))
    has_records = conn.execute(
        "SELECT 1 FROM extracted_records WHERE po_number = ? LIMIT 1", (po_number,)
    ).fetchone()
    if not events and not has_records:
        return None

    status = _status_from_events(events)
    release = _rows(conn, """
        SELECT MAX(released_at) AS released_at, release_reason
          FROM released_events WHERE po_number = ?
    """, (po_number,))
    released_at = release[0]["released_at"] if release else None
    # Where the goods were received and who signed for them. This is what the mail actually states,
    # and it is the only thing that lets a reader decide whether the receipt address was the final
    # destination — a question no status label can answer for them.
    receipt = _rows(conn, """
        SELECT MAX(delivery_location) AS delivery_location, MAX(received_by) AS received_by
          FROM extracted_records WHERE po_number = ?
    """, (po_number,))

    earliest_for_stage: Dict[str, str] = {}
    for event in events:
        # Every stage the notice proves gets the date, not just the furthest one: a warehouse
        # receipt is both the arrival at the warehouse and the delivery, so both nodes carry it.
        for stage in event.stages if event.event_on else ():
            earliest_for_stage.setdefault(stage, event.event_on)

    # The strongest date there is: what the notice itself states happened, rather than when anyone
    # wrote about it. `Received Date: 10/09/2025` in a warehouse notice is the arrival, and it
    # outranks the send date of the mail carrying it.
    stated = _rows(conn, """
        SELECT MIN(pod_stated_date) AS on_date FROM extracted_records
         WHERE po_number = ? AND COALESCE(TRIM(pod_stated_date), '') <> ''
    """, (po_number,))
    stated_on = (stated[0]["on_date"] or "")[:10] if stated else ""
    stated_stages = {delivery_status.DELIVERED, delivery_status.AT_WAREHOUSE}

    # When the purchase order was raised, from Spitfire, joined on the PO number. No mail states
    # this — it is a property of the order, not of any delivery — so before this the node was dated
    # from "the first email we happened to see", which is how PO 908491 came to look ordered a day
    # after it was delivered.
    #
    # `order_date`, NOT `source_date`. `SourceDate` was the obvious candidate and was measured and
    # rejected: 11 of the 17 mirrored POs with line due dates have a line due *before* it, and all
    # three POs the corpus delivers against were received before it. It is populated by
    # `connectors/spitfire.fetch_document_dates` from a named date type, and is NULL until Spitfire
    # credentials exist — in which case Ordered is reached but undated, which is the honest answer.
    ordered = _rows(conn, """
        SELECT order_date FROM spitfire_po_index WHERE po_number = ?
    """, (po_number,))
    ordered_on = ((ordered[0]["order_date"] or "")[:10] if ordered else "")

    terminal = status if status in delivery_status.TERMINAL_BRANCHES else None
    reached_rank = (
        delivery_status.rank(status) if status in delivery_status.LIFECYCLE
        # A terminal PO still shows how far it got before it stopped.
        else max((delivery_status.rank(s) for s in earliest_for_stage
                  if s in delivery_status.LIFECYCLE), default=0)
    )

    stages: List[Stage] = []
    for key in VISIBLE_LIFECYCLE:
        if terminal and delivery_status.rank(key) > reached_rank:
            # Everything past the point it stopped is replaced by the terminal node, so the bar
            # never implies a delivery that is not coming.
            break
        reached = delivery_status.rank(key) <= reached_rank
        # A stated date only belongs on a node the mail actually proved. Putting it on an
        # unreached node would date an arrival that has not happened.
        use_stated = bool(stated_on) and reached and key in stated_stages
        if key == delivery_status.OPEN:
            # Ordered comes from the purchase order or from nowhere. It is never inferred from
            # mail: the PO being *named* proves it exists, not when it was raised.
            on = ordered_on
        elif use_stated:
            on = stated_on
        else:
            on = earliest_for_stage.get(key, "")
        stages.append(Stage(
            key=key,
            label=delivery_status.STATUS_LABELS[key],
            reached=reached,
            on=on,
            stated=use_stated,
            # Reported, not repaired. Shuffling the dates until the bar looked plausible would
            # destroy the only evidence that Spitfire and the mail disagree.
            conflict=(
                f"dated before the purchase order was raised ({ordered_on})"
                if ordered_on and on and reached and key != delivery_status.OPEN and on < ordered_on
                else ""
            ),
        ))
    if terminal:
        stages.append(Stage(
            key=terminal,
            label=delivery_status.STATUS_LABELS[terminal],
            reached=True,
            on=earliest_for_stage.get(terminal, ""),
            terminal=True,
        ))

    return PoTimeline(
        po_number=po_number,
        status=status,
        label=delivery_status.STATUS_LABELS[status],
        stages=stages,
        events=events,
        released_at=released_at,
        release_reason=release[0]["release_reason"] if release and released_at else "",
        delivery_location=(receipt[0]["delivery_location"] if receipt else "") or "",
        received_by=(receipt[0]["received_by"] if receipt else "") or "",
    )


def _attention_code(row) -> str:
    """Which kind of unread this is, in the words of the thing that would fix it.

    Three different problems used to share two labels, and the split was on `error_type` alone:
    anything carrying one read as **"Corrupt"**. So an intact PDF whose OCR call was refused —
    `error_type='OcrServiceUnavailable'`, file perfectly fine — sat under a chip that says the
    sender shipped us a broken document. 104 attachments read that way at once, which is how a
    plain budget ceiling came to look like a wave of corrupt mail.

    `needs_ocr` belongs *here*, on attachments nobody has managed to read yet, not on the `empty`
    bucket where it used to live: `empty` means an adapter read the file successfully and found
    nothing, so OCR has either already run or been declined as inapplicable. Here it means the
    read was never completed, and `python -m tools.reextract` is exactly the remedy.
    """
    if row.disposition == attachment_ledger.SERVICE_UNAVAILABLE:
        return "needs_ocr"
    if row.disposition == attachment_ledger.CORRUPT or row.error_type:
        return "corrupt"
    return "unreadable"


def _record_reasons(row: sqlite3.Row, record_gaps: Optional[completeness.Gaps] = None) -> List[str]:
    """Why this record cannot go forward. Plural because more than one can be true at once, and
    a person fixing it needs all of them, not the first.

    The gap list leads, because it names the cells to fill. This used to open with "confidence
    0.0", which was true of ten of the thirteen live records and told nobody what to do about it.
    """
    reasons = []
    record_gaps = completeness.gaps(row) if record_gaps is None else record_gaps
    if record_gaps.missing_required:
        reasons.append(record_gaps.describe())
    if (row["extraction_confidence"] or 0) <= 0:
        reasons.append("confidence 0.0 — nothing recognisable was read from the source")
    if "quantity_conflict" in (row["extraction_source"] or ""):
        reasons.append("quantity conflict — two sources state different quantities and neither was chosen")
    if row["status"] == "failed":
        detail = (row["comments"] or "").strip()
        reasons.append(f"marked failed: {detail}" if detail else "marked failed")
    return reasons


NOISE_RULE = "rule_5c_internal_noise"


_SET_ASIDE_WHERE = """
      FROM email_log e
 LEFT JOIN mail_overrides v ON v.email_id = e.email_id
     WHERE COALESCE(v.verdict, '') <> 'delivery'
       AND (e.not_a_delivery = 1 OR v.verdict = 'not_delivery')
"""
"""Set aside as not a delivery — by a rule, or by a person, with the person winning either way.

Shared by `filtered_mail` and `filtered_count` so the page and the number it is announced by cannot
disagree about what is on it.

`COALESCE(v.verdict, '')` is load-bearing. With a bare `v.verdict <> 'delivery'`, a message nobody
has overridden compares NULL, which is not true, which drops **every rule-flagged message** — the
whole 152 of them — while the page renders perfectly happily and says nothing is set aside.
"""


def filtered_mail(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Mail identified as not a delivery, so the suppression stays inspectable.

    Kept as its own view rather than folded into `mails()`: the question this answers is "what has
    been taken out of the queue", and it has to be answerable without reading fourteen columns of
    every message ever received.

    Selected on `not_a_delivery` rather than on one rule name. It began as internal chatter only,
    and the set has grown — a freight status notice, an Authority status summary, a scheduled report
    and any thread whose every hop declines to say goods arrived all belong under the same heading.
    Keying on the flag means adding a rule to `stage1_triage.NOT_A_DELIVERY_RULES` is the whole
    change; this query does not move.

    Two populations now, and the page distinguishes them per row. `matched_rule` names the rule that
    set a message aside; `decided_by` names the person, where a person overruled triage in either
    direction. Both are on every row deliberately: a rule can be inspected and a person can be
    asked, and they are not the same kind of accountability.
    """
    return _rows(conn, f"""
        SELECT e.email_id, e.subject, e.sender, e.reason, e.matched_rule, e.processed_at,
               v.decided_by, v.note AS decided_note, v.decided_at
          {_SET_ASIDE_WHERE}
         ORDER BY e.processed_at DESC
    """)


def is_set_aside(conn: sqlite3.Connection, email_id: str) -> bool:
    """Whether this one message currently counts as not a delivery — by rule or by a person.

    Through `_SET_ASIDE_WHERE` and not a hand-written `not_a_delivery = 1`, so the message dialog
    offers the direction the not-a-delivery page would actually list it under. Two definitions of
    "set aside" is exactly how a button comes to say "Not a delivery" about a message already on
    that page.
    """
    return conn.execute(f"SELECT 1 {_SET_ASIDE_WHERE} AND e.email_id = ? LIMIT 1",
                        (email_id,)).fetchone() is not None


def filtered_count(conn: sqlite3.Connection) -> int:
    """How many messages are set aside, for the line on `/ui/manual` that points at them.

    A count rather than `len(filtered_mail(conn))`: that page has no other use for the rows, and
    loading a hundred and fifty of them to print one number is the kind of thing that makes a page
    slow for no visible reason.
    """
    return conn.execute(f"SELECT COUNT(*) {_SET_ASIDE_WHERE}").fetchone()[0]


_queue_cache: dict = {}
_queue_lock = threading.Lock()


def invalidate_manual_queue() -> None:
    """Forget every cached queue. Called by `invalidate_summary`, so writers need know only one."""
    with _queue_lock:
        _queue_cache.clear()


def _queue_key(conn: sqlite3.Connection):
    """Which store this is, and whether anything has changed since we last looked.

    Three cheap reads standing in for "is the cached queue still true":

    * the file, so two stores never share an entry;
    * `PRAGMA data_version`, which SQLite bumps when **another** connection commits;
    * `conn.total_changes`, which counts what **this** connection has written — the case
      `data_version` explicitly does not cover, and the one every test hits when it seeds rows and
      re-reads on the same connection.

    Together they mean a stale queue cannot be served: a write by anybody moves the key.
    """
    try:
        path = ""
        for _seq, name, file in conn.execute("PRAGMA database_list"):
            if name == "main":
                path = file or ""
                break
        if not path:
            # An in-memory database. Every one of them is a *separate* database that reports the
            # same empty file, so a shared key would hand one test's queue to the next: caching
            # them collided 35 tests in `test_read_views` that each pass alone. Nothing in the app
            # uses `:memory:`, so there is no saving to give up here.
            return None
        version = conn.execute("PRAGMA data_version").fetchone()[0]
        return (path, version, conn.total_changes)
    except Exception:                                              # noqa: BLE001
        return None


def manual_queue(conn: sqlite3.Connection) -> List[ManualItem]:
    """The queue, memoised against an exact change key -- with no expiry, deliberately.

    Building it is the most expensive thing this module does — 5 full-table queries, 2 ledger
    scans and a scan of `extracted_records`, then a completeness filter that runs in Python — and
    `/ui/manual` paid for it **twice** on every load: once for its own rows, and again inside
    `summary()`, which counts the queue by taking `len()` of it. Measured on Premier's live store
    at 3,611 items, that was ~1.9s of which half was spent building the same list a second time.

    Caching rather than threading the value through `_chrome` -> `summary_across_sources` ->
    `summary`: those two calls hold *different connections* to the same store, so there is no
    argument to pass, and every other page's header gets the saving too.

    There is no time limit because there is nothing for one to protect against: `_queue_key`
    carries `PRAGMA data_version` and `conn.total_changes`, so *any* write by anybody moves the key
    and the next caller rebuilds. A TTL on top of that would only throw away a correct answer and
    pay 846 ms to compute the same one again -- which is exactly what it was doing. Measured in a
    real browser on 2026-09-16: with a 3 s expiry, `/ui/mails` and `/ui/manual` each took ~3.4 s to
    first byte, because every page load took longer than the window and so always missed.

    The returned list is a fresh one each time, so a caller that sorts or filters it cannot disturb
    the next reader.
    """
    key = _queue_key(conn)
    if key is None:
        return _build_manual_queue(conn)
    with _queue_lock:
        hit = _queue_cache.get(key)
        if hit is not None:
            return list(hit[1])
    items = _build_manual_queue(conn)
    with _queue_lock:
        _queue_cache[key] = (time.monotonic(), items)
        if len(_queue_cache) > 32:                 # unbounded growth across test stores
            for stale in sorted(_queue_cache, key=lambda k: _queue_cache[k][0])[:16]:
                _queue_cache.pop(stale, None)
    return list(items)


def _build_manual_queue(conn: sqlite3.Connection) -> List[ManualItem]:
    """Everything a person has to deal with, from all three levels, each with a stated reason.

    Emails first, then attachments, then records — that is roughly the order in which fixing one
    can dissolve the ones below it.
    """
    items: List[ManualItem] = []
    subjects = email_log.subjects_by_email_id(conn)
    # Attachment rows know their `email_id` and nothing else about the message. They were rendering
    # with an empty Sender and an empty Filed-to, which on a merged row is the whole identity of the
    # thing gone: 57 of these messages reach the queue only through a bad attachment.
    envelopes = email_log.envelope_by_email_id(conn)

    for r in _rows(conn, """
        SELECT email_id, subject, sender, email_date, category, matched_rule, reason,
               folder, error_type, processed_at
          FROM email_log
         WHERE category IN ('route', 'error') OR folder IN ('Errors', 'Quarantine')
         ORDER BY processed_at DESC
    """):
        reason = (r["reason"] or "").strip()
        if not reason:
            reason = f"triaged {r['category']} by {r['matched_rule'] or 'no rule'}"
        if r["folder"] == "Errors":
            reason = f"processing error — {reason}"
        elif r["folder"] == "Quarantine":
            reason = f"attachment limits breached — {reason}"
        items.append(ManualItem(
            kind="email", ref=r["subject"] or r["email_id"], ref_id=None,
            email_id=r["email_id"], subject=r["subject"] or "", when=r["processed_at"],
            reason=reason, detail=f"from {r['sender'] or 'unknown sender'} · filed to {r['folder']}",
            # `maybe_advertising` before the generic `routed`: the row is on this queue
            # *because* nobody could decide whether it is advertising, and "Routed" says
            # none of that. The chip is how a person filters to exactly that decision and
            # clears it in one pass.
            code=("quarantined" if r["folder"] == "Quarantine"
                  else "error" if r["folder"] == "Errors" or r["category"] == "error"
                  else "maybe_advertising"
                  if r["matched_rule"] == "rule_5f_possible_advertising"
                  else "routed"),
            sender=r["sender"] or "", filed_to=r["folder"] or "",
        ))

    # Delivery mail that produced nothing and never will. Triage decided this was a delivery, then
    # no record and no accumulation row came of it — so it appears on no other page and would
    # otherwise be silently lost. Held mail with an accumulation row is excluded: its records are
    # still legitimately pending release.
    # Mail that was correctly *not* accumulated because the delivery it describes had already been
    # released and staged from an earlier message. It has no accumulation row and no record — the
    # same shape as the residual below — so without this branch it inherits that branch's sentence,
    # which says nothing was recovered from it. Everything was recovered from it, once, from the
    # first copy to arrive.
    for r in _rows(conn, """
        SELECT e.email_id, e.subject, e.sender, e.folder, e.processed_at,
               GROUP_CONCAT(DISTINCT s.po_number) AS po_numbers,
               MIN(s.released_at) AS released_at
          FROM email_log e
          JOIN suppressed_notices s ON s.email_id = e.email_id
         WHERE NOT EXISTS (SELECT 1 FROM extracted_records x WHERE x.source_email_id = e.email_id)
           AND NOT EXISTS (SELECT 1 FROM accumulation a WHERE a.email_id = e.email_id)
         GROUP BY e.email_id
         ORDER BY e.processed_at DESC
    """):
        names = [p for p in (r["po_numbers"] or "").split(",") if p]
        pos = ", ".join(names)
        verb = "were" if len(names) > 1 else "was"
        when = (r["released_at"] or "")[:16].replace("T", " ")
        items.append(ManualItem(
            kind="email", ref=r["subject"] or r["email_id"], ref_id=None,
            email_id=r["email_id"], subject=r["subject"] or "", when=r["processed_at"],
            reason=f"the same delivery already came through on an earlier message — "
                   f"PO {pos} {verb} released{' ' + when if when else ''} and staged from it, so "
                   f"this copy was not counted twice",
            detail=f"from {r['sender'] or 'unknown sender'} · filed to {r['folder']}",
            code="duplicate", sender=r["sender"] or "", filed_to=r["folder"] or "",
        ))

    for r in _rows(conn, """
        SELECT e.email_id, e.subject, e.sender, e.category, e.folder, e.processed_at
          FROM email_log e
         WHERE e.category IN ('surface', 'hold')
           AND NOT EXISTS (SELECT 1 FROM extracted_records x WHERE x.source_email_id = e.email_id)
           AND NOT EXISTS (SELECT 1 FROM accumulation a WHERE a.email_id = e.email_id)
           AND NOT EXISTS (SELECT 1 FROM suppressed_notices s WHERE s.email_id = e.email_id)
         ORDER BY e.processed_at DESC
    """):
        items.append(ManualItem(
            kind="email", ref=r["subject"] or r["email_id"], ref_id=None,
            email_id=r["email_id"], subject=r["subject"] or "", when=r["processed_at"],
            reason=f"read as delivery mail ({r['category']}) but nothing was extracted from it — "
                   f"no PO, spec, quantity or POD was recovered from the body or any attachment",
            detail=f"from {r['sender'] or 'unknown sender'} · filed to {r['folder']}",
            code="nothing_extracted", sender=r["sender"] or "", filed_to=r["folder"] or "",
        ))

    # Mail a person has said is a delivery, after triage set it aside as not one.
    #
    # Its own branch because it matches none of the three above by construction: every rule in
    # `stage1_triage.NOT_A_DELIVERY_RULES` files its mail as HIDE, and those branches look at
    # route/error/surface/hold. Without this, calling a message a delivery would take it off the
    # not-a-delivery page and put it *nowhere* — and the whole promise of that action is that the
    # message comes back here, where "Create a record" is.
    #
    # The category and folder exclusions are what stop a doubled row: a routed message a person also
    # calls a delivery is already on this queue through the first branch, and this would say nothing
    # the reason there does not. `_merge_no_record_emails` also relies on that — see its head
    # selection — so relaxing these means a merged row's badge becomes a coin toss.
    #
    # A record existing is what ends it, exactly as for the two branches above. Nothing extracts from
    # an overridden message: the row says a person vouched for it and nothing has been recorded, and
    # the record they build by hand is the answer to that sentence.
    for r in _rows(conn, """
        SELECT e.email_id, e.subject, e.sender, e.folder, e.matched_rule, e.processed_at,
               v.decided_by, v.note AS decided_note
          FROM email_log e
          JOIN mail_overrides v ON v.email_id = e.email_id
         WHERE v.verdict = 'delivery'
           AND e.category NOT IN ('route', 'error', 'surface', 'hold')
           AND e.folder NOT IN ('Errors', 'Quarantine')
           AND NOT EXISTS (SELECT 1 FROM extracted_records x WHERE x.source_email_id = e.email_id)
           AND NOT EXISTS (SELECT 1 FROM accumulation a WHERE a.email_id = e.email_id)
           AND NOT EXISTS (SELECT 1 FROM suppressed_notices s WHERE s.email_id = e.email_id)
         ORDER BY e.processed_at DESC
    """):
        note = (r["decided_note"] or "").strip()
        items.append(ManualItem(
            kind="email", ref=r["subject"] or r["email_id"], ref_id=None,
            email_id=r["email_id"], subject=r["subject"] or "", when=r["processed_at"],
            reason=f"triage set this aside as not a delivery "
                   f"({r['matched_rule'] or 'no rule'}) and {r['decided_by']} says it is one"
                   + (f" — {note}" if note else "")
                   + "; nothing has been recorded from it yet",
            detail=f"from {r['sender'] or 'unknown sender'} · filed to {r['folder']}",
            code="overridden", sender=r["sender"] or "", filed_to=r["folder"] or "",
        ))

    for row in attachment_ledger.list_needing_attention(conn):
        detail = row.disposition_detail or row.error_type or "no further detail recorded"
        items.append(ManualItem(
            kind="attachment", ref=row.filename or f"attachment #{row.id}", ref_id=row.id,
            email_id=row.email_id, subject=subjects.get(row.email_id, ""),
            # `first_seen_at`, not "". These two were the only queue items with no date at all, so
            # the When column was blank on every attachment row — and once the UI gained a date
            # range, a blank date meant the whole attachment category disappeared the moment anyone
            # narrowed by date. The ledger has recorded this all along.
            when=row.first_seen_at or "",
            reason=f"{row.disposition}: {detail}",
            detail=f"{row.sniffed_kind or 'unknown kind'} · {row.size_bytes} bytes",
            # Corrupt is the file being broken; unreadable is everything else that ends with
            # nothing read out of it. They need different fixes — one is chased with the sender,
            # the other with an adapter. And an OCR outage is neither: the file is intact and
            # nobody has read it yet.
            code=_attention_code(row),
            sender=envelopes.get(row.email_id, {}).get("sender", ""),
            filed_to=envelopes.get(row.email_id, {}).get("folder", ""),
        ))

    for row in attachment_ledger.list_silent_on_delivery_mail(conn):
        items.append(ManualItem(
            kind="attachment", ref=row.filename or f"attachment #{row.id}", ref_id=row.id,
            email_id=row.email_id, subject=subjects.get(row.email_id, ""),
            when=row.first_seen_at or "",
            reason=f"{row.claimed_by or 'an adapter'} read it in full and found no delivery in it "
                   f"— no PO, spec or quantity this software recognises. Re-reading will not "
                   f"change that; open it to see whether it is a delivery document at all",
            detail=f"{row.sniffed_kind or 'unknown kind'} · {row.size_bytes} bytes"
                   f" · read by {row.claimed_by or 'no adapter'}",
            code="nothing_recognised",
            sender=envelopes.get(row.email_id, {}).get("sender", ""),
            filed_to=envelopes.get(row.email_id, {}).get("folder", ""),
        ))

    # Every record still in play, then filtered on completeness in Python. The old SQL asked for
    # `NOT (_READY_CLAUSE)`, which meant a record with a PO and any confidence at all was treated
    # as finished — so the three fabric records sat under "Ready to process further" with no
    # delivery date, carrier, tracking or signature on them. Completeness is a field-by-field
    # question (`pipeline.completeness`) and does not express as a WHERE clause worth reading.

    # Which messages a person has already confirmed, read once rather than per row.
    confirmed = confirmed_email_ids(conn)
    # One row per internally-authored message, standing for every record on it. Built as the
    # per-record loop goes, so the loop can skip what it covers. Insertion-ordered, which is
    # `ORDER BY r.id` — the group then takes its place in the sort by its message's own date.
    awaiting_by_email: Dict[str, List[sqlite3.Row]] = {}

    for r in _rows(conn, f"""
        SELECT r.*, COALESCE(e.subject, '') AS email_subject,
               COALESCE(e.origin_sender, '') AS origin_sender,
               COALESCE(e.email_date, '') AS email_when
          FROM extracted_records r
          LEFT JOIN email_log e ON e.email_id = r.source_email_id
         WHERE r.status IN ('pending', 'failed')
           AND {_NOT_SET_ASIDE}
         ORDER BY r.id
    """):
        # A claim Premier made about itself. Collected as one item per message below rather than
        # listed row by row: on the live store 134 messages hold 3,278 of these, one of them 632 on
        # its own, and a queue reading "record #4727, record #4728, …" six hundred times is a queue
        # nobody can work.
        if r["status"] != "failed" and _awaits_confirmation(r, confirmed):
            awaiting_by_email.setdefault(r["source_email_id"], []).append(r)
            continue
        record_gaps = completeness.gaps(r)
        if record_gaps.is_complete and r["status"] != "failed" and not _blocked_from_records(r):
            continue
        items.append(ManualItem(
            kind="record", ref=f"record #{r['id']}", ref_id=r["id"],
            email_id=r["source_email_id"], subject=r["email_subject"], when=r["created_at"],
            reason="; ".join(_record_reasons(r, record_gaps)) or "blocked, reason not classified",
            po_number=(r["po_number"] or "").strip(),
            po_line_number=r["po_line_number"],
            # The item carries what differs row to row; `detail` keeps only where it was read from,
            # which is the same string for every row of one document and so belongs behind the
            # item rather than in front of it.
            item=" · ".join(part for part in (
                r["spec_code"] or "",
                (r["item_description"] or "")[:60],
            ) if part),
            detail=f"via {r['extraction_source']}",
            # Ordered by what a person would fix first. A record can be missing its PO *and* carry
            # a quantity conflict; the PO is the one that has to be settled before the conflict
            # can even be looked at against the right order.
            code=("no_po" if "po_number" in record_gaps.missing_required
                  else "qty_conflict" if "quantity_conflict" in (r["extraction_source"] or "")
                  else "incomplete"),
        ))

    # One row per message Premier wrote itself, standing for all of its records.
    #
    # `kind="email"` and `ref_id=None` on purpose: the thing a person decides here is the message,
    # and the Confirm control acts on the message. It also keeps the partition legible — the record
    # arm of this queue is still exactly the records a person has to *fix*, and these are records
    # nobody can fix, only confirm.
    for email_id, group in awaiting_by_email.items():
        pos = sorted({(r["po_number"] or "").strip() for r in group} - {""})
        shown = ", ".join(pos[:6]) + (f" and {len(pos) - 6} more" if len(pos) > 6 else "")
        items.append(ManualItem(
            kind="email", ref=group[0]["email_subject"] or email_id, ref_id=None,
            email_id=email_id, subject=group[0]["email_subject"] or "",
            when=group[0]["email_when"] or group[0]["created_at"],
            reason=(f"written inside Premier — {len(group)} item line"
                    f"{'' if len(group) == 1 else 's'} read from it, and nothing in it says the "
                    f"goods arrived. Confirm it to send them to Records"),
            detail=(f"from {group[0]['origin_sender']}"
                    + (f" · PO {shown}" if shown else "")),
            code="awaiting_confirmation",
            sender=group[0]["origin_sender"],
            rolled_up=len(group),
        ))

    # Say which of these messages carry one of Premier's own worklists.
    #
    # Re-coded here rather than in each branch because the fact belongs to the *message* and the
    # branches are about six different things that can be wrong with one. An expediting report
    # reaches this queue by at least five separate routes — `awaiting_confirmation` when its
    # confirmation grid produced records, `nothing_recognised` when the workbook held only the
    # tracker, `nothing_extracted`, `routed`, `incomplete` — and every one of those sentences
    # describes a symptom of the same thing: the document says what was ordered, never that it
    # arrived. A person clearing these wants them under one chip, not five.
    #
    # **Record rows keep their own code.** A record is work that exists whatever is later said
    # about the mail it came from, and it is exempt from the set-aside filter below for that same
    # reason — so re-badging it would promise a disposal this page cannot deliver.
    #
    # Attachment rows are re-coded only when the row *is* the workbook. A corrupt PDF riding on the
    # same message is a second, unrelated problem and keeps saying so.
    status_by_email, status_by_ledger = attachment_ledger.status_reports(conn)
    if status_by_email:
        for item in items:
            if item.kind == "record":
                continue
            # A person who has personally said this message *is* a delivery outranks the rule that
            # says its spreadsheet is a tracker. Both can be true at once — Premier forwards an
            # expediting report with a signed POD pasted under it — and overwriting `overridden`
            # here would erase the one code on this queue that records somebody vouching for the
            # mail, leaving them looking at a row that says the opposite of what they decided.
            if item.code == "overridden":
                continue
            if item.kind == "attachment":
                reason = status_by_ledger.get(item.ref_id or -1)
            else:
                reason = status_by_email.get(item.email_id)
            if not reason:
                continue
            item.code = "status_report"
            item.reason = (
                f"one of Premier's own worklists, not a delivery document: {reason}. "
                f"Nothing in it says goods arrived, and no amount of re-reading will change that"
            )

    # A person's answer overrules triage's, in both directions — this is the other half of the
    # branch above. Set aside by hand means gone from here: the message is on the not-a-delivery
    # page now, and leaving it on this queue as well would make the action look like it did nothing.
    #
    # Filtered here rather than in each branch's WHERE so it also covers the two attachment branches.
    # A routed message flagged as chatter would otherwise stay on the queue through its one corrupt
    # attachment, which reads as a broken button rather than as a second problem.
    #
    # **Record rows go too, and used to be exempt.** The exemption was written on the reasoning that
    # a record is work existing whatever anyone later says about the mail, and that this is the only
    # page carrying its Fix/Fill/Verify controls, so hiding it would strand it. That holds while the
    # only way off the queue is finishing the record. It does not hold for a message a person has
    # signed as not delivery mail: there is nothing to fix, because the document never said goods
    # arrived. Leaving the rows made the verdict look broken — a person set aside an expediting
    # report, its subject stayed on the queue, and the honest reason was invisible.
    #
    # Nothing is stranded, because nothing is destroyed: the record keeps its row and its evidence,
    # and "This is a delivery" on `/ui/not-deliveries` brings the message and every record on it
    # straight back here. `_records_pending` carries the same exclusion, which is what keeps these
    # records off Records and away from the Post button rather than merely out of sight.
    set_aside = mail_overrides.ids_with(conn, mail_overrides.NOT_DELIVERY)
    if set_aside:
        items = [i for i in items if i.email_id not in set_aside]

    # Fold the messages nothing came out of into one row each, before the sort so the merged row
    # takes its place by its own date rather than by whichever of its parts happened to be first.
    items = _merge_no_record_emails(items)

    # Newest first, across all three kinds. They were grouped — every email, then every attachment,
    # then every record — which put a message from May above an attachment from this morning and
    # made the top of the queue the oldest thing on it. One list, one order, and the thing that
    # just happened is the thing you land on.
    items.sort(key=lambda item: item.when or "", reverse=True)
    return items
