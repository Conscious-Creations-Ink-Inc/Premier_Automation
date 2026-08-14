"""Read-only views over pipeline_state.sqlite3, shaped for the operations UI.

Query-only and dependency-free on purpose — no FastAPI, no pydantic — so the same three views
back the web pages, the CLI runner's summary, and the tests that assert the pipeline left a
trace. They live here rather than in `api/stores/` because every module there reads the *demo*
database; pipeline state is owned next to `state_db`.

`records_ready` and the record arm of `manual_queue` used to be exact complements — every pending
record on exactly one page, never both. **They overlap now, deliberately.** Readiness answers "can
this be processed further"; completeness (`pipeline.completeness`) answers "could a receiver line
be built from it", and the second is much the stronger test. A record with a PO and no proof of
delivery is ready and incomplete at once, so it appears on both: listed for a person to finish,
and not withheld from the pipeline while they do.

The half of the invariant that still holds is the one that mattered: no pending record belongs to
*neither* page. That would be work nobody was ever told about.
"""
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from pipeline import attachment_ledger, completeness, delivery_status, email_log

# A record is ready to process further only if all four hold. Kept as one string so the "ready"
# query and the "blocked" query cannot drift apart — the second is the literal negation.
_READY_CLAUSE = """
    r.status = 'pending'
    AND COALESCE(TRIM(r.po_number), '') <> ''
    AND r.extraction_confidence > 0
    AND r.extraction_source NOT LIKE '%quantity_conflict%'
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
    needs_human: int = 0
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


def summary(conn: sqlite3.Connection) -> Summary:
    by_category = email_log.counts_by_category(conn)
    emails = sum(by_category.values())
    records_total = conn.execute("SELECT COUNT(*) FROM extracted_records").fetchone()[0]
    ready = conn.execute(f"SELECT COUNT(*) FROM extracted_records r WHERE {_READY_CLAUSE}").fetchone()[0]
    ocr_pages = conn.execute("SELECT COALESCE(SUM(ocr_attempted), 0) FROM email_log").fetchone()[0]
    last_run = conn.execute("SELECT MAX(processed_at) FROM email_log").fetchone()[0]
    return Summary(
        emails=emails,
        by_category=by_category,
        records_total=records_total,
        records_ready=ready,
        needs_human=len(manual_queue(conn)),
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
               e.ocr_attempted, e.processed_at,
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


def summary_across_sources(sources=None) -> Summary:
    """The header figures, totalled over every store the Mail page lists.

    Added because the strip contradicted the table the moment the two mail pages merged: it read
    "14 emails" from the corpus alone while 26 rows sat underneath it. A header that disagrees with
    the page it heads is worse than no header.

    `summary(conn)` is untouched and still answers for one store — that is what the tests and the
    CLI runner want.
    """
    from config import settings
    from pipeline import state_db

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
        total.needs_human += part.needs_human
        total.ocr_pages += part.ocr_pages
        for category, count in part.by_category.items():
            total.by_category[category] = total.by_category.get(category, 0) + count
        # The most recent run across all of them — "last run" of the whole pipeline, not of one file.
        if part.last_run and (total.last_run is None or part.last_run > total.last_run):
            total.last_run = part.last_run
    return total


def records_ready(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Pending records carrying enough to be matched against a PO line downstream."""
    return _rows(conn, f"""
        SELECT r.id, r.po_number, r.po_line_number, r.spec_code, r.parent_spec_code,
               r.sub_spec_suffix, r.item_description, r.quantity_received, r.unit_of_measure,
               r.package_quantity, r.package_uom, r.pod_stated_date, r.carrier_name,
               r.tracking_number, r.received_by, r.delivery_location, r.vendor_name,
               r.shipment_number, r.notification_number, r.extraction_source,
               r.extraction_confidence, r.status, r.source_email_id,
               COALESCE(e.subject, '') AS email_subject
          FROM extracted_records r
          LEFT JOIN email_log e ON e.email_id = r.source_email_id
         WHERE {_READY_CLAUSE}
         ORDER BY r.po_number, r.po_line_number, r.id
    """)


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
    at Crown Worldwide Moving & Storage - Mira Loma / Received By Miguel C." — and no status word
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
    goods moved (Premier re-raises POs — 208453 was lost and replaced by 211400). Both are worth
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
#     Received at:    Crown Worldwide Moving & Storage - Mira Loma
#     Received By:    Miguel C.
#
# It records where the goods were received and who signed for them, and it is the event that triggers
# a receiver in Spitfire — Premier's own annotation on these messages is "straightforward, WH rec'd".
# So it proves `delivered`. It also proves `at_warehouse`, because that is demonstrably where they
# went, which keeps the route visible on the progress bar without lying about the status.
#
# The project named in the subject ("LXR Cameo Beverly Hills") is what the goods belong to, not a
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
    """`email_log.po_hints` is a comma-space delimited blob (`'206725, 207665'`), not a key.

    Split and compared exactly rather than matched with LIKE: `LIKE '%2084%'` matches 208491, and a
    purchase-order page showing another order's mail is worse than one showing none.
    """
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _events_by_po(conn: sqlite3.Connection) -> Dict[str, List[PoEvent]]:
    """Every email, indexed by each PO it names.

    Sourced from `email_log`, not `accumulation`, and the difference is not cosmetic. Stage 2 drops
    two whole classes of mail: anything triaged ROUTE never accumulates at all (cancellations and
    loss/claim notices), and a notice arriving after its delivery has been released is discarded as
    a duplicate. On the corpus that is the difference between 2 and 3 events on PO 208491, and
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
            # Every corpus message is forwarded from premierpm.com, so the envelope sender is the
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
        # months after a direct one and still be the older event — on PO 208491 the oldest
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
    # from "the first email we happened to see", which is how PO 208491 came to look ordered a day
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


def filtered_mail(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Mail deliberately set aside as internal chatter, so the suppression stays inspectable.

    Kept as its own view rather than folded into `mails()`: the question this answers is "what did
    the rule take out of the queue", and it has to be answerable without reading fourteen columns
    of every message ever received.
    """
    return _rows(conn, """
        SELECT email_id, subject, sender, reason, processed_at
          FROM email_log
         WHERE matched_rule = ?
         ORDER BY processed_at DESC
    """, (NOISE_RULE,))


def manual_queue(conn: sqlite3.Connection) -> List[ManualItem]:
    """Everything a person has to deal with, from all three levels, each with a stated reason.

    Emails first, then attachments, then records — that is roughly the order in which fixing one
    can dissolve the ones below it.
    """
    items: List[ManualItem] = []
    subjects = email_log.subjects_by_email_id(conn)

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
        ))

    # Delivery mail that produced nothing and never will. Triage decided this was a delivery, then
    # no record and no accumulation row came of it — so it appears on no other page and would
    # otherwise be silently lost. Held mail with an accumulation row is excluded: its records are
    # still legitimately pending release.
    for r in _rows(conn, """
        SELECT e.email_id, e.subject, e.sender, e.category, e.folder, e.processed_at
          FROM email_log e
         WHERE e.category IN ('surface', 'hold')
           AND NOT EXISTS (SELECT 1 FROM extracted_records x WHERE x.source_email_id = e.email_id)
           AND NOT EXISTS (SELECT 1 FROM accumulation a WHERE a.email_id = e.email_id)
         ORDER BY e.processed_at DESC
    """):
        items.append(ManualItem(
            kind="email", ref=r["subject"] or r["email_id"], ref_id=None,
            email_id=r["email_id"], subject=r["subject"] or "", when=r["processed_at"],
            reason=f"read as delivery mail ({r['category']}) but nothing was extracted from it — "
                   f"no PO, spec, quantity or POD was recovered from the body or any attachment",
            detail=f"from {r['sender'] or 'unknown sender'} · filed to {r['folder']}",
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
        ))

    for row in attachment_ledger.list_silent_on_delivery_mail(conn):
        items.append(ManualItem(
            kind="attachment", ref=row.filename or f"attachment #{row.id}", ref_id=row.id,
            email_id=row.email_id, subject=subjects.get(row.email_id, ""),
            when=row.first_seen_at or "",
            reason=f"read cleanly but produced no records, on mail we read as a delivery — "
                   f"{row.disposition_detail or 'nothing extractable'}",
            detail=f"{row.sniffed_kind or 'unknown kind'} · {row.size_bytes} bytes"
                   f" · read by {row.claimed_by or 'no adapter'}",
        ))

    # Every record still in play, then filtered on completeness in Python. The old SQL asked for
    # `NOT (_READY_CLAUSE)`, which meant a record with a PO and any confidence at all was treated
    # as finished — so the three fabric records sat under "Ready to process further" with no
    # delivery date, carrier, tracking or signature on them. Completeness is a field-by-field
    # question (`pipeline.completeness`) and does not express as a WHERE clause worth reading.
    for r in _rows(conn, """
        SELECT r.*, COALESCE(e.subject, '') AS email_subject
          FROM extracted_records r
          LEFT JOIN email_log e ON e.email_id = r.source_email_id
         WHERE r.status IN ('pending', 'failed')
         ORDER BY r.id
    """):
        record_gaps = completeness.gaps(r)
        if record_gaps.is_complete and r["status"] != "failed":
            continue
        items.append(ManualItem(
            kind="record", ref=f"record #{r['id']}", ref_id=r["id"],
            email_id=r["source_email_id"], subject=r["email_subject"], when=r["created_at"],
            reason="; ".join(_record_reasons(r, record_gaps)) or "blocked, reason not classified",
            po_number=(r["po_number"] or "").strip(),
            # The PO has moved out of this string and into its own column, where it is the control
            # that opens the mail the record was read from.
            detail=" · ".join(part for part in (
                r["spec_code"] or "",
                (r["item_description"] or "")[:60],
                f"via {r['extraction_source']}",
            ) if part),
        ))

    return items
