"""Every page Premier sees. This is the only interface; there is no second one.

**Every page reads one database — `state/pipeline_state.sqlite3`, written by reading Premier's own
mailbox.** Nothing on screen is test data.

That was not always true. Until 2026-08-12 the Mail, Records, Needs-a-human and Delivery-status
pages read `state/sample_state.sqlite3`, the 14 `.msg` files used to develop against, and the Mail
page listed both stores side by side with a source badge on each row. The corpus was retired when
the live inbox started carrying real traffic. `sample_state.sqlite3` is not deleted and is still the
fixture `tests/test_ui_html.py` renders against — it is simply no longer a product surface. The one
place that decides is `read_views.MAIL_SOURCES`; the one place pages get a connection is
`deps.get_pipeline_conn`.

The rule that made the split worth having still holds and is now trivially satisfied: the receiver
report is evidence, and evidence that mixes test data with Premier's real receiving mail is worth
nothing.

Read-only except the four `POST /ui/automation/*` controls, all of which redirect so a refresh never
re-submits. There are no filters, no sorting controls and no per-row actions.
"""
import sqlite3
from datetime import datetime
from urllib.parse import parse_qs, quote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from api import deps
from api.ui import html
from config import settings
from operations import inbox as inbox_reader
from operations import killswitch, runner, scheduler
from operations import store as ops_store
from pipeline import (attachment_bytes, attachment_view, completeness, delivery_status,
                      mail_arrivals, mail_cache, mail_view, po_verify, read_views, receipt_log,
                      state_db)

router = APIRouter(prefix="/ui", tags=["ui"], include_in_schema=False)

_EMPTY_HINT = (
    "Nothing has been read from the mailbox yet. Press “Run now” on Automation, or run "
    "`python -m tools.ingest_mailbox` (read-only) for the full CLI output. Mail already sitting "
    "in the inbox is not processed until a run happens."
)


def _chrome(conn: sqlite3.Connection) -> dict:
    """The stat strip, the footer and the sidebar's queue badge, from one read.

    Returned as kwargs for `html.page(**_chrome(conn))`. It is one function rather than three
    because all three want the same summary, and asking for it separately meant every page ran that
    scan two or three times to render one screen.

    Totalled across every store, not just the one the caller happens to hold. The Mail page lists
    both, and a header reading "14 emails" over 26 rows is worse than no header. It also makes the
    sidebar's queue badge count the live mailbox's items, which genuinely do need a person.

    `conn` is still taken so the signature does not churn at ten call sites, and because a page that
    could not open the other stores should still render from the one it has.
    """
    s = read_views.summary_across_sources()
    order = ("surface", "hold", "route", "hide", "error")
    pairs = [("emails", s.emails)]
    pairs += [(category, s.by_category[category]) for category in order if s.by_category.get(category)]
    pairs += [
        ("records", s.records_total),
        ("ready", s.records_ready),
        ("need a human", s.needs_human),
        ("OCR pages", s.ocr_pages),
    ]
    return {
        "header": html.stats(pairs),
        "footer": f"Last run {s.last_run}" if s.last_run else "Never run.",
        "counts": {"/ui/manual": s.needs_human},
    }


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def index():
    return RedirectResponse("/ui/mails", status_code=307)


@router.get("/mails", response_class=HTMLResponse)
def mails_page(refresh: int = 0,
               conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    """Every processed email, in one table, read from Premier's mailbox.

    The Source column looks redundant now that there is only one source, and it is kept on purpose.
    This was two pages — Mails over the `.msg` corpus and Inbox over the live mailbox — which read
    as the same page because neither said where its mail came from. The column is what made that
    legible, and it is what a second source would need again the day the four legacy employee
    mailboxes are connected and run in parallel with this one.
    """
    rows = []
    mails_list = read_views.mails_across_sources()

    # Mail that has landed but nothing has read yet, newest first and above the verdicts. This is
    # the real-time half: the arrival watch writes these seconds after they arrive, while the row
    # below each of them cannot exist until a ~40s ingest pass has run. They are visibly a
    # different kind of row — no verdict, no rule, no counts — because claiming a verdict for mail
    # nobody has read would be the one dishonest thing this page could do.
    pending = mail_arrivals.pending(conn)
    for a in pending:
        rows.append([
            html.badge("inbox", "inbox"),
            html.when((a.received_at or "")[:16].replace("T", " ")),
            _clipped(a.subject, 52), a.sender,
            html.badge("not read yet", "hold"), html.muted("—"),
            html.muted("waiting for the next run"), html.muted("—"),
            "yes" if a.has_attachments else html.muted("0"),
            html.muted("—"), html.muted("0"), html.muted("—"),
        ])

    for m in mails_list:
        sender = m.sender
        if m.origin_sender and m.origin_sender != m.sender:
            # Every corpus message is a Fw: from an internal expeditor, so the envelope sender is
            # premierpm.com on all of them; the recovered origin is the one that means anything.
            sender = html.Raw(html.esc(m.sender) + " " + str(html.muted(f"(via {m.origin_sender})")))
        attachments = str(m.attachment_count)
        if m.attachments_flagged:
            attachments = html.Raw(f"{m.attachment_count} " + str(html.muted(f"({m.attachments_flagged} flagged)")))
        rows.append([
            html.badge(m.source_label, m.source),
            html.when(m.email_date[:16]),
            # Truncated, with the whole thing on hover. A thirteenth column arrived with the source
            # badge, and these two are the only free-text ones — left full they wrap to seven lines
            # and every row stands 140px tall, which is what a dense grid exists to avoid.
            _clipped(m.subject, 52), sender,
            html.badge(m.category, m.category), html.token(m.matched_rule),
            _clipped(m.reason, 48), m.po_hints or html.muted("—"),
            attachments, m.records, m.ocr_attempted or html.muted("0"), m.folder,
        ])
    body = html.section(
        "Mail, and what the orchestrator decided about it",
        _unprocessed_note(refresh=bool(refresh), pending=len(pending)),
        html.search_box("mail-table",
                        placeholder="Search PO, subject, sender, verdict…",
                        label="Search mail"),
        html.date_filter("mail-table", label="received date"),
        html.table(
            ["Source", "Received", "Subject", "From", "Verdict", "Rule", "Why", "POs",
             "Attachments", "Records", "OCR", "Filed to"],
            rows, empty=_EMPTY_HINT, table_id="mail-table", page_size=50,
            date_column="Received",
            # `src` is what reopens a message from the store it actually lives in. Without it every
            # row would be looked up in the corpus and live mail would report itself missing.
            #
            # An arrival is openable even though nothing has processed it: `mail_view.resolve`
            # falls through to Graph on a cache miss and fetches that one message. So the row shows
            # up seconds after the mail lands and can be read immediately, while the attachments,
            # triage and extraction behind it wait for the next run.
            frag_urls=(
                [f"/ui/mail?id={quote(a.email_id)}&reason=&src={quote(html.DEFAULT_MAIL_SOURCE)}"
                 for a in pending]
                + [f"/ui/mail?id={quote(m.email_id)}&reason={quote(m.reason or '')}"
                   f"&src={quote(m.source)}" for m in mails_list]
            ),
            frag_title="Open this message",
        ),
        note="Every email that has been through Stage 1, newest first, read from Premier's "
             "receiving mailbox. One row per email, always. Click a row to read the message and "
             "its attachments.",
    )
    return html.page("Mail", "/ui/mails", body, **_chrome(conn))


def _gap_badge(row) -> html.Raw:
    """Whether a receiver line could be built from this record, and if not, what is stopping it.

    Deliberately not a confidence score. "0.50" says nothing a person can act on; "4 gaps" with
    `missing: vendor, PO line #, POD date, received-by` on hover names the cells to fill.
    """
    row_gaps = completeness.gaps(row)
    if row_gaps.is_complete:
        advisory = row_gaps.describe(advisory=True)
        return html.tag("span", html.badge("Complete", "good"), title=advisory or "every field present")
    return html.tag("span", html.badge(f"{row_gaps.count} gaps", "hold"),
                    title=row_gaps.describe(advisory=True))


def _clipped(value, limit: int):
    """Long free text on one line, with the full value on hover.

    Both halves matter. Truncating alone did nothing: a 13-column table squeezes the free-text
    columns to about 90px, so even 70 characters wrapped to seven lines and every row stood 140px
    tall. `nw` stops the wrap, which makes the column claim its natural width and the table scroll
    inside `.scroll` — which is the trade a dense grid should make, and why the sidebar collapses.

    The whole string stays in the DOM as the `title`, so nothing is lost.
    """
    text = (value or "").strip()
    if not text:
        return html.muted("—")
    if len(text) <= limit:
        return html.tag("span", text, class_="nw")
    return html.tag("span", text[:limit].rstrip() + "…", title=text, class_="nw")


def _unprocessed_note(refresh: bool = False, pending: int = 0) -> html.Raw:
    """Mail sitting in the mailbox that nothing has read yet.

    The one thing the table of verdicts cannot show: it lists what was *processed*, so a message
    that arrived and was never ingested is invisible in it. That gap is how you would learn the
    automation has fallen behind, and it becomes the important number the day Premier switches
    forwarding on.

    **This used to call Graph, inside the render.** `operations.inbox.load()` walks up to ten pages
    sequentially at a measured ~3.2s each, cached for sixty seconds in process — so roughly once a
    minute, opening the Mail page meant waiting on Microsoft before a single row was drawn, and a
    restart made every first visit pay it. The count now comes from `mail_arrivals`, which the
    arrival watch keeps current in the background; it is a SQLite read of an indexed column.

    `?refresh=1` is kept and is now the only path that talks to Graph from this page: an explicit,
    user-pressed deep re-read for when someone wants to know *right now* rather than within the
    watch's interval.
    """
    if refresh:
        return _deep_refresh_note()

    # The count comes first on every branch, including the ones that go on to explain that the
    # watch is off or broken. It was originally reported only when the watch was healthy, which
    # meant the page fell silent about mail it was already showing at exactly the moment something
    # was wrong — the moment that number matters most.
    if pending:
        text = f"{pending} message{_s(pending)} arrived and not yet read by the pipeline. "
    else:
        text = "Nothing unprocessed. "

    watch = _arrival_watch()
    if not watch.enabled:
        state = html.muted("The new-mail watch is off, so this only updates when a run happens. ")
    elif watch.last_error:
        state = html.tag("span", f"The watch could not read the mailbox: {watch.last_error} ",
                         class_="err")
    elif watch.last_poll_at:
        state = html.muted(f"Last checked {_ago(watch.last_poll_at)}. ")
    else:
        state = html.muted("Not checked yet. ")

    return html.tag("p", text, state,
                    html.tag("a", "Check now", href="/ui/mails?refresh=1",
                             class_="btn ghost small", style="margin-left:4px"),
                    (html.tag("a", "Turn the watch on", href="/ui/automation",
                              class_="btn ghost small", style="margin-left:6px")
                     if not watch.enabled else html.Raw("")),
                    class_="note")


def _deep_refresh_note() -> html.Raw:
    """The one Graph call left on this page, and only when someone asks for it by pressing a link.

    Lists the mailbox directly rather than trusting the watch, and records anything it finds — so
    pressing this is also how you populate the arrivals table without waiting for, or enabling, the
    background watch.
    """
    try:
        result = inbox_reader.load(force=True)
    except Exception as exc:                                       # noqa: BLE001
        return html.tag("p", f"Could not read the mailbox to check for unprocessed mail: {exc}",
                        class_="note")
    if result.error or not result.configured:
        return html.tag("p", f"Could not read the mailbox: {result.error or 'not configured'}",
                        class_="note")

    conn = _live_conn()
    try:
        seen = {r[0] for r in conn.execute("SELECT email_id FROM email_log")}
        now = _now()
        mail_arrivals.record(conn, [
            mail_arrivals.Arrival(
                email_id=m.internet_message_id, received_at=m.received, sender=m.sender,
                subject=m.subject, has_attachments=m.has_attachments, first_seen_at=now,
                enriched_at=now if (m.internet_message_id or "") in seen else None,
            )
            for m in result.messages if m.internet_message_id
        ], now=now)
    finally:
        conn.close()

    pending = [m for m in result.messages if (m.internet_message_id or "") not in seen]
    if not pending:
        text = f"Nothing unprocessed in {result.mailbox}. "
    else:
        text = (f"{len(pending)} message{_s(len(pending))} in {result.mailbox} "
                f"not yet processed. ")
    return html.tag("p", text,
                    html.tag("a", "Refresh", href="/ui/mails?refresh=1",
                             class_="btn ghost small", style="margin-left:4px"),
                    class_="note")


def _arrival_watch():
    """The watch's settings and last result. Never raises — this decorates a page, it is not the
    page."""
    try:
        conn = ops_store.get_connection()
        try:
            return ops_store.get_arrival_watch(conn)
        finally:
            conn.close()
    except Exception:                                              # noqa: BLE001
        return ops_store.ArrivalWatch(enabled=False,
                                      interval_seconds=ops_store.DEFAULT_ARRIVAL_SECONDS)


@router.get("/records", response_class=HTMLResponse)
def records_page(conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    records = read_views.records_ready(conn)
    # One call, indexed in memory — not a query per row. The record's own `status` column is not
    # shown: every pending record reads "pending" until stages 4-7 run, so it would be 25 identical
    # cells. Where the purchase order actually stands is the question this page could not answer.
    delivery = {p.po_number: p for p in read_views.po_delivery_status(conn)}
    rows = []
    for r in records:
        package = f"{_num(r['package_quantity'])} {r['package_uom'] or ''}".strip()
        po = delivery.get(r["po_number"])
        rows.append([
            r["id"],
            # Second column, not last. This table is eighteen columns wide and scrolls sideways, so
            # a Verify cell on the far right is a control nobody can reach without already knowing
            # it is there — which is exactly what happened. It reads this row's purchase order back
            # out of Spitfire and shows those quantities against the ones the email stated. The row
            # still opens the delivery bar: the script ignores clicks landing on a button inside a
            # row, so the two do not fight.
            html.verify_button(f"/ui/records/{r['id']}/verify", "Verify",
                               title=f"Check PO {r['po_number']} against Spitfire", small=True),
            # Two destinations, deliberately distinguishable rather than one link that does a
            # surprising thing: the number goes to the purchase order, the envelope opens the email
            # this line was read from, so the two can be compared.
            html.Raw(str(html.tag("a", r["po_number"], href=f"/ui/po/{r['po_number']}")) + " "
                     + str(html.mail_link(r["source_email_id"], "✉",
                                          title="Open the email this line was read from"))),
            html.badge(po.label, po.status) if po else html.muted("—"),
            _gap_badge(r),
            r["po_line_number"] if r["po_line_number"] is not None else html.muted("—"),
            html.token(r["spec_code"]), (r["item_description"] or "")[:70],
            _num(r["quantity_received"]), r["unit_of_measure"] or html.muted("—"),
            package or html.muted("—"), html.when(r["pod_stated_date"]),
            r["carrier_name"] or html.muted("—"), html.token(r["tracking_number"]),
            r["received_by"] or html.muted("—"), f"{r['extraction_confidence']:.2f}",
            r["extraction_source"],
            # The row itself opens the delivery bar, so the message keeps its own control here
            # rather than being unreachable from this page. It was a plain `<a href>` to the
            # fragment endpoint, which navigated away to an unstyled partial with no way back —
            # and carried no `src`, so it resolved against the corpus and reported itself missing.
            html.mail_link(r["source_email_id"], r["email_subject"][:40] or "(no subject)"),
        ])
    body = html.section(
        "Ready to process further",
        html.search_box("records-table",
                        placeholder="Search PO, spec, description, carrier, tracking…",
                        label="Search records"),
        html.date_filter("records-table", label="POD date"),
        html.table(
            ["#", "Verify", "PO", "Delivery", "Complete", "Line", "Spec", "Description", "Qty",
             "UOM", "Package", "POD date", "Carrier", "Tracking", "Received by", "Conf", "Source",
             "From email"],
            rows, empty=_EMPTY_HINT, table_id="records-table", page_size=50,
            date_column="POD date", no_sort=("Verify",),
            frag_urls=[f"/ui/po/{quote(r['po_number'])}/bar" for r in records],
            frag_title="Show this delivery's progress",
        ),
        note=f"{len(records)} pending record(s) carrying a PO, non-zero confidence and no "
             f"cross-source quantity conflict — which is all being *ready* has ever meant. The "
             f"Complete column is the stronger test: it asks whether a receiver line could actually "
             f"be built from the row, and anything short of that is also listed on the manual page "
             f"with the missing fields named. Nothing here is withheld from the pipeline for being "
             f"incomplete. Click a row to see where that delivery stands. Verify reads the purchase "
             f"order from Spitfire and shows what it holds against what the email said — it states "
             f"the figures and does not decide whether they are acceptable.",
        action=html.verify_button(
            "/ui/records/verify", "Verify all against Spitfire",
            title="Read every listed purchase order from Spitfire and compare quantities"),
    )

    # The same builder the operations console uses, so the sheet on screen here and the file
    # Premier downloads from either place cannot drift apart.
    report = receipt_log.build(conn)
    sheet = html.section(
        "Receiver report",
        # A real button, not a styled link: it produces a file, which is an action.
        html.Raw(
            '<form method="get" action="/ui/records/receiver.xlsx" style="margin:0 0 12px">'
            '<button class="btn" type="submit">Download .xlsx</button></form>'
        ),
        html.search_box("receipt-sheet", placeholder="Search PO, vendor, spec, receiver…",
                        label="Search the receiver report"),
        # Paged by purchase order, not by row, and `plain` because the sheet brings its own row
        # styling. Ten POs at a time: a page boundary inside a PO would split the block someone is
        # holding beside Spitfire's own Receipt Log, and that block is the whole point of the sheet.
        # "All" is one press away in the pager, and the .xlsx download is never paged.
        html.scroll_block(html.Raw(receipt_log.to_html(report)), table_id="receipt-sheet",
                          page_size=10, unit="group", plain=True),
        note="Laid out like Spitfire's own Receipt Log and grouped by PO number, so the two can be "
             "compared line by line. Shown ten purchase orders at a time — set 'per page' to All, "
             "or download the .xlsx, to get the whole sheet at once. Order Qty, Net and Final are "
             "deliberately blank — they live on the purchase order inside Spitfire and no delivery "
             "email carries them, so filling them in would mean inventing numbers.",
    )
    return html.page("Records", "/ui/records", body, sheet,
                     **_chrome(conn))


# --- Verify against Spitfire ------------------------------------------------------------------
#
# POST, and deliberately so. These read the purchase order out of Spitfire and refresh the local
# mirror with what comes back, which is an effect — a GET would let a prefetch or a crawler start
# fifty ERP reads nobody asked for. The reply is an HTML fragment for the shared dialog, the same
# contract `/ui/po/{po_number}/bar` uses.


@router.post("/records/{record_id}/verify", response_class=HTMLResponse)
def verify_record_fragment(record_id: int,
                           conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    rows = [r for r in read_views.records_ready(conn) if r["id"] == record_id]
    if not rows:
        return str(html.tag("p", "No such record.", class_="empty"))
    result = po_verify.verify_records(conn, rows)[0]
    return str(_verification(result))


@router.post("/records/verify", response_class=HTMLResponse)
def verify_all_fragment(conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    rows = read_views.records_ready(conn)
    if not rows:
        return str(html.tag("p", "There are no records to verify.", class_="empty"))
    results = po_verify.verify_records(conn, rows)
    return str(_verification_summary(results))


def _verification_summary(results) -> html.Raw:
    """Every record at once. Anything with a finding is put first.

    Sorted rather than filtered: a reader who presses "Verify all" is entitled to see that the
    other ten records were checked and had nothing to report. Hiding them would leave the popup
    looking like a list of problems with no denominator.
    """
    ordered = sorted(results, key=lambda r: (not r.has_finding, r.po_number))
    compared = sum(1 for r in results if r.matched and r.matched.record_quantity is not None)
    unreadable = sum(1 for r in results if r.error)
    not_found = sum(1 for r in results if not r.po_found and not r.error)

    lead = (f"{len(results)} record(s) checked · {compared} carried a quantity to compare · "
            f"{len(results) - compared} did not · {not_found} purchase order(s) not found")
    if unreadable:
        lead += f" · {unreadable} could not be read from Spitfire"

    stale = sorted({r.read_at[:16] for r in ordered
                    if r.source == po_verify.SOURCE_MIRROR and r.read_at})
    parts = [html.tag("p", lead + ".", class_="note")]
    if stale:
        parts.append(html.banner(
            "Some of these could not be read from Spitfire just now and are shown from the local "
            f"mirror, last refreshed {', '.join(stale)}. Those figures may be out of date."
        ))
    parts.extend(_verification(result) for result in ordered)
    return html.tag("div", *parts)


def _verification(result) -> html.Raw:
    """One record's answer: what the email said, what the purchase order holds, and the plain
    differences between them.

    No verdict. The screen states figures and lets the reader judge, because the rule that would
    decide — whether a quantity equal to the amount ordered on a line Spitfire already shows as
    received is acceptable — is still an open question with Premier, and a green tick would be this
    software inventing the answer.
    """
    parts = []
    if result.error:
        parts.append(html.banner(
            f"Could not read Spitfire live: {result.error}",
            kind="bad",
        ))
    if result.source == po_verify.SOURCE_MIRROR:
        when = (result.read_at or "").replace("T", " ")[:16] or "an earlier run"
        parts.append(html.banner(
            f"Showing the last mirrored copy of this purchase order instead, read {when}. "
            f"These figures may be out of date."
        ))

    headline = f"PO {result.po_number}"
    detail = " · ".join(p for p in [result.vendor_name, result.doc_status_label,
                                    f"ordered {result.order_date}" if result.order_date else ""] if p)
    read_note = ("Read live from Spitfire" if result.source == po_verify.SOURCE_LIVE
                 else "From the local mirror")
    when = (result.read_at or "").replace("T", " ")[:16]
    parts.append(html.tag("h3", headline, f" — record {result.record_id}"))
    parts.append(html.tag("p", detail or "—",
                          html.tag("span", f" · {read_note}{f', {when}' if when else ''}.",
                                   class_="muted"),
                          class_="note"))

    if result.matched:
        parts.append(_matched_line(result.matched))
    parts.append(html.tag("ul", *[html.tag("li", note) for note in result.notes],
                          class_="vfindings"))
    return html.tag("div", *parts, class_="vrec")


def _matched_line(check) -> html.Raw:
    """The comparison table. Two columns because they are two different claims.

    The quantity row is tinted only where the two sides can actually be compared — a row with no
    email quantity is neither agreement nor disagreement, and colouring it either way would assert
    something the data does not say.
    """
    line_label = f"line {check.line_number:04d}" if check.line_number is not None else "a line"
    heading = html.tag(
        "p",
        f"Spec {check.spec_code or '—'} resolved {line_label}" if check.spec_resolved
        else f"Description resolved {line_label}",
        html.tag("span", f" — {check.description[:90]}", class_="muted"),
        class_="note",
    )

    def row(label, email_cell, po_cell, state=""):
        return html.tag("tr",
                        html.tag("th", label),
                        html.tag("td", email_cell, class_=f"n {state}".strip() or None),
                        html.tag("td", po_cell, class_="n"))

    qty_state = ""
    if check.qty_agrees is True:
        qty_state = "same"
    elif check.qty_agrees is False:
        qty_state = "diff"
    uom_state = ""
    if check.uom_agrees is True:
        uom_state = "same"
    elif check.uom_agrees is False:
        uom_state = "diff"

    body = [
        row("Quantity",
            f"{po_verify.fmt_qty(check.record_quantity)} {check.record_uom or ''}".strip(),
            f"{po_verify.fmt_qty(check.qty_ordered)} ordered", qty_state),
        row("Unit", check.record_uom or "—", check.unit_of_measure or "—", uom_state),
        row("Already received", html.muted("—"),
            f"{po_verify.fmt_qty(check.qty_received)} {check.unit_of_measure}"),
        row("In transit", html.muted("—"),
            f"{po_verify.fmt_qty(check.qty_in_transit)} {check.unit_of_measure}"),
        row("Outstanding", html.muted("—"),
            f"{po_verify.fmt_qty(check.qty_outstanding)} {check.unit_of_measure}"),
    ]
    table = html.tag(
        "table",
        html.tag("thead", html.tag("tr", html.tag("th", ""), html.tag("th", "From the email"),
                                   html.tag("th", "On the purchase order"))),
        html.tag("tbody", *body),
        class_="vq",
    )
    return html.tag("div", heading, table)


@router.get("/records/receiver.xlsx")
def receiver_download(conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)):
    """The receiver report as a workbook. A GET that builds bytes and writes nothing."""
    report = receipt_log.build(conn)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    return Response(
        content=receipt_log.to_xlsx(report),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="Receiver_Report_{stamp}.xlsx"'},
    )


@router.get("/manual", response_class=HTMLResponse)
def manual_page(conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    items = read_views.manual_queue(conn)
    groups = (
        ("email", "Emails", "Triaged to a person, quarantined, or failed to process."),
        ("attachment", "Attachments", "Received but not readable — each with the disposition recorded against it."),
        ("record", "Records", "Extracted, but missing what downstream matching needs."),
    )
    sections = []
    # One search box above every table on this page. Someone chasing a PO number wants it found in
    # whichever queue it landed in, not in the one they happened to point at — see `html.search_box`.
    # The chatter table is included on purpose: "did a real delivery get filed as chatter?" is
    # exactly the question that brings someone to this page, and it is the last place they would
    # think to look separately.
    present = [f"manual-{kind}" for kind, _t, _n in groups if any(i.kind == kind for i in items)]
    if read_views.filtered_mail(conn):
        present.append("manual-filtered")
    if present:
        sections.append(html.section(
            "Find something",
            html.search_box(present, placeholder="Search PO, subject, sender, reason…",
                            label="Search everything needing a person"),
            html.date_filter(present, label="date it arrived"),
            note="Searches every table on this page at once, including the filtered chatter below.",
        ))
    for kind, title, note in groups:
        group = [i for i in items if i.kind == kind]
        rows = [[
            i.ref,
            # The PO opens the email the record was read from, so the two can be compared. It is
            # also the identifier a person actually works with — the What column says
            # "record #126", which is a row id nobody outside the database can use.
            html.mail_link(i.email_id, i.po_number, reason=i.reason) if i.po_number
            else html.muted("—"),
            i.subject[:45] or html.muted("—"), html.when(i.when[:16]),
            i.reason, i.detail or html.muted("—"),
        ] for i in group]
        sections.append(html.section(
            f"{title} ({len(group)})",
            # `frag_urls`, not `mail_ids`: the convenience path cannot express `src`, and without
            # it `_store_for` resolves to the retired `.msg` corpus — so every row on this page
            # reported its message missing, having searched a folder of test files for Premier's
            # live mail.
            html.table(["What", "PO", "Email", "When", "Why it needs a person", "Detail"],
                       rows, empty="Nothing in this group.",
                       table_id=f"manual-{kind}", page_size=25, date_column="When",
                       frag_urls=[html.mail_url(i.email_id, i.reason) for i in group],
                       frag_title="Open this message"),
            note=note,
        ))
    if not items:
        # One "nothing here" panel in place of three empty groups — but *keeping* whatever came
        # before it. This used to rebuild the list from scratch, which also threw away the search
        # box above; on a page whose queues are empty but whose chatter table is not, that left a
        # table nobody could search and no sign that anything had gone missing.
        sections = sections[:-len(groups)] + [
            html.section("Needs a human", html.table([], [], empty=_EMPTY_HINT))
        ]
    sections.append(_filtered_section(conn))
    return html.page("Needs a human", "/ui/manual", *sections,
                     **_chrome(conn))


def _filtered_section(conn: sqlite3.Connection) -> html.Raw:
    """Mail triage set aside as internal chatter, shown rather than hidden.

    Twelve of the fourteen emails in this queue were all-associates broadcasts and calendar
    invites, each stating "no PO reference found anywhere in the thread". Every one of them was
    true, and together they buried the two entries that were real work.

    They are listed here, not deleted, because a suppression rule nobody can inspect is a rule
    nobody can trust — and this is the table that shows a delivery wrongly filed as chatter.
    """
    filtered = read_views.filtered_mail(conn)
    rows = [[
        _clipped(m["subject"] or m["email_id"], 60),
        m["sender"] or html.muted("—"),
        html.when((m["processed_at"] or "")[:16]),
        _clipped(m["reason"], 70),
    ] for m in filtered]
    return html.section(
        f"Filtered as internal chatter ({len(filtered)})",
        html.table(["Subject", "From", "When", "Why it was set aside"], rows,
                   empty="Nothing has been filtered.", table_id="manual-filtered", page_size=25,
                   date_column="When",
                   frag_urls=[html.mail_url(m["email_id"], m["reason"] or "") for m in filtered],
                   frag_title="Open this message"),
        note="Not queued, not deleted. Internal mail with no PO, nothing readable attached and no "
             "delivery vocabulary anywhere in the thread. If a real delivery appears here, the rule "
             "is wrong — open it and say so.",
    )


@router.get("/po", response_class=HTMLResponse)
def po_page(conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    """Where each purchase order stands, from what the mail has said about it so far."""
    pos = read_views.po_delivery_status(conn)
    rows = []
    for p in pos:
        notices = ", ".join(
            f"{t.replace('_', ' ')}" + (f" ×{n}" if n > 1 else "") for t, n in sorted(p.notifications.items())
        )
        released = (
            html.Raw(str(html.when(p.released_at[:16])) + " " + str(html.muted(p.release_reason)))
            if p.released_at else html.muted("—")
        )
        rows.append([
            html.tag("a", p.po_number, href=f"/ui/po/{p.po_number}"),
            html.badge(p.label, p.status),
            notices or html.muted("—"),
            p.records or html.muted("0"),
            p.lines_seen or html.muted("—"),
            _num(p.qty_received) or html.muted("—"),
            # None, not 0 — the Spitfire mirror is empty, and a zero here would read as
            # "nothing was ordered" rather than "we have not been told".
            _num(p.qty_ordered) if p.qty_ordered is not None else html.muted("—"),
            _num(p.qty_outstanding) if p.qty_outstanding is not None else html.muted("—"),
            released,
            html.when(p.last_seen[:16]),
        ])

    unreachable = ", ".join(
        delivery_status.STATUS_LABELS[s] for s in read_views.UNREACHABLE_STATUSES
    )
    body = html.section(
        "Where each purchase order stands",
        html.search_box("po-table", placeholder="Search PO, status, date…",
                        label="Search purchase orders"),
        html.date_filter("po-table", label="last heard"),
        html.table(
            ["PO", "Status", "Notifications", "Records", "Lines", "Received",
             "Ordered", "Outstanding", "Released", "Last heard"],
            rows, empty=_EMPTY_HINT, table_id="po-table", page_size=50,
            date_column="Last heard",
        ),
        note=f"{len(pos)} purchase order(s) the pipeline has heard about, most recent first. Status "
             f"is inferred from the notifications received, not recorded — {unreachable} cannot be "
             f"reached at all yet, because no receipt is staged here and nothing writes to Spitfire.",
    )
    caveat = html.section(
        "Why Ordered and Outstanding are empty",
        html.tag(
            "p",
            "They live on the purchase order inside Spitfire and no delivery email carries them, so "
            "filling them in would mean inventing numbers. They populate here once the Spitfire read "
            "has run and mirrored the PO lines — no change to this page is needed.",
            class_="note",
        ),
    )
    return html.page("Delivery status", "/ui/po", body, caveat,
                     **_chrome(conn))


@router.get("/po/{po_number}", response_class=HTMLResponse)
def po_detail_page(po_number: str,
                   conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    """One purchase order: how far it has got, and the mail that says so."""
    timeline = read_views.po_timeline(conn, po_number)
    if timeline is None:
        # Nothing anywhere names this PO. An unknown order and one we have simply heard nothing
        # about must not render the same — a blank progress bar would imply the latter.
        raise HTTPException(status_code=404, detail=f"No such purchase order: {po_number}.")

    released = ""
    if timeline.released_at:
        released = (f"Released for processing {timeline.released_at[:16]} — "
                    f"{timeline.release_reason}.")
    progress = html.section(
        f"PO {timeline.po_number}",
        _where_it_is(timeline),
        html.stepper(timeline.stages),
        note=released or "Not yet released for processing.",
    )

    event_rows = [[
        # The date the notice was *sent*, not the date it reached us. On forwarded mail those are
        # months apart, and the envelope date is what once put one date on every stage.
        html.when(e.event_on),
        # What the mail calls the notice, beside what it proves — never instead of it. Authority's
        # "Delivered Notification" proves `in_transit`, so showing that phrase as the status would
        # read "Delivered" against goods still travelling.
        html.badge(read_views.notification_label(e.notification_type), e.category),
        e.subject[:70] or html.muted("(no subject)"), e.sender,
        html.muted(", ".join(delivery_status.STATUS_LABELS[s] for s in e.stages)) if e.stages
        else html.muted("proves no delivery stage"),
    ] for e in timeline.events]
    evidence = html.section(
        f"The mail behind it ({len(timeline.events)})",
        html.search_box("po-events", placeholder="Search subject, sender, notice…",
                        label="Search the mail behind this PO"),
        html.date_filter("po-events", label="date sent"),
        html.table(["Sent", "Notice", "Subject", "From", "What it proves"], event_rows,
                   empty="No mail names this purchase order.",
                   table_id="po-events", page_size=25, date_column="Sent",
                   mail_ids=[(e.email_id, e.reason) for e in timeline.events]),
        note="Every email naming this PO, oldest first — including notices that arrived after the "
             "delivery was released, which the accumulator drops as duplicates and which would "
             "otherwise be invisible.",
    )

    line_rows = [[
        r["id"], r["po_line_number"] if r["po_line_number"] is not None else html.muted("—"),
        html.token(r["spec_code"]), (r["item_description"] or "")[:60],
        _num(r["quantity_received"]), r["unit_of_measure"] or html.muted("—"),
        html.when(r["pod_stated_date"]), r["received_by"] or html.muted("—"),
    ] for r in read_views.records_ready(conn) if r["po_number"] == po_number]
    lines = html.section(
        f"Lines extracted ({len(line_rows)})",
        html.search_box("po-lines", placeholder="Search spec, description, receiver…",
                        label="Search extracted lines"),
        html.date_filter("po-lines", label="POD date"),
        html.table(["#", "Line", "Spec", "Description", "Qty", "UOM", "POD date", "Received by"],
                   line_rows, empty="Nothing has been extracted from this PO's mail yet.",
                   table_id="po-lines", page_size=25, date_column="POD date"),
    )

    return html.page(f"PO {po_number}", "/ui/po", progress, evidence, lines,
                     **_chrome(conn))


def _where_it_is(timeline) -> html.Raw:
    """The headline: where these goods are now, and since when.

    First thing on the page because it is the question being asked. The bar underneath shows how it
    got there and what has not happened; neither answers "where is it" without being read across.

    The date comes from the furthest *reached* node, so it is the date of the thing that actually
    happened rather than of the newest mail — a duplicate notice arriving later must not appear to
    move the goods again.
    """
    reached = [s for s in timeline.stages if s.reached and s.on]
    since = f"since {reached[-1].on}" if reached else "no dated evidence yet"
    parts = [
        html.badge(timeline.label, timeline.status),
        html.tag("b", since),
        html.tag("span", _receipt_line(timeline), class_="muted"),
    ]
    # Reported here rather than left for someone to spot on the bar: a date that cannot be right is
    # a question for a person, and it is the kind of thing a reader scanning a status will miss.
    conflicts = [s for s in timeline.stages if s.conflict]
    if conflicts:
        parts.append(html.tag(
            "span",
            f"Check in Spitfire: {conflicts[0].label} is {conflicts[0].conflict}.",
            class_="conflict",
        ))
    return html.tag("p", *parts, class_="where")


def _receipt_line(timeline_or_row) -> str:
    """Where the goods were received and who signed for them.

    The mail states both outright, and this is the only thing that lets a reader judge whether the
    receipt address was the final destination — "Delivered" cannot answer that on its own.
    """
    where = (timeline_or_row.delivery_location or "").strip()
    who = (timeline_or_row.received_by or "").strip()
    if where and who:
        return f"Received by {who} at {where}."
    if where:
        return f"Received at {where}."
    if who:
        return f"Received by {who}."
    return "No receipt location has been stated in the mail."


@router.get("/po/{po_number}/bar", response_class=HTMLResponse)
def po_bar_fragment(po_number: str,
                    conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    """Just the progress bar for one PO — what the Records page opens on a row click."""
    timeline = read_views.po_timeline(conn, po_number)
    if timeline is None:
        return '<p class="empty">No such purchase order.</p>'
    # Same headline as the full page, from the same builder — the popup is where most people meet
    # a delivery, and it answering "where is it" differently from the page it links to would be
    # its own small lie.
    return (
        str(html.tag("div", html.tag("h3", f"PO {timeline.po_number}"), class_="mail-head"))
        + str(_where_it_is(timeline))
        + str(html.stepper(timeline.stages))
        + str(html.tag("p", html.tag("a", "Open the full purchase order",
                                     href=f"/ui/po/{timeline.po_number}"), class_="note"))
    )


# `src` values a message link may carry, mapped to the store to look in. Driven off
# `read_views.MAIL_SOURCES` so the Mail page's source keys and this lookup cannot disagree — they
# did briefly, and the symptom was live mail reporting itself missing because `src=inbox` fell
# through to the corpus.
#
# `live` is kept as an alias for `inbox`: the receiver report's rows use it, and old links exist.
def _store_for(src: str):
    """Which database a message lives in.

    Two stores, deliberately kept apart: the `.msg` corpus used for testing, and Premier's live
    mailbox. An unknown, missing or crafted value falls back to the corpus, so a bad query string
    can never reach the live store.
    """
    by_key = {key: setting for key, _label, setting in read_views.MAIL_SOURCES}
    by_key.setdefault("live", "PIPELINE_STATE_DB_PATH")
    setting_name = by_key.get(src)
    if not setting_name:
        return settings.SAMPLE_STATE_DB_PATH
    return getattr(settings, setting_name)


@router.get("/mail", response_class=HTMLResponse)
def mail_fragment(id: str = "", reason: str = "", images: int = 0, src: str = "") -> str:
    """The popup's contents. Fetched by the page, never navigated to.

    Resolution falls back through the cache, the accumulated payload, and finally the `.msg`
    files — the last of which is how ROUTE mail (cancellations, loss and claim notices) is
    recoverable at all, since it never accumulates.
    """
    if not id:
        return '<p class="empty">No message was requested.</p>'
    db_path = _store_for(src)
    mail = mail_view.resolve(id, db_path=db_path)
    return mail_view.render(
        mail, reason=reason, remote_images=bool(images),
        # `src` is carried through so an attachment link inside the popup looks in the same store
        # the message itself came from.
        attachment_url=f"/ui/mail/attachment?src={quote(src)}" if src else "/ui/mail/attachment",
        # The viewer route, minus the ordinal — `_attachments` appends `&n=` per row. Carries `src`
        # for the same reason `attachment_url` does: an attachment must be looked for in the store
        # its message came from.
        view_url=f"/ui/mail/attachment/view?id={quote(id)}&src={quote(src)}",
        db_path=db_path,
    )


@router.get("/mail/attachment/view", response_class=HTMLResponse)
def mail_attachment_view(id: str = "", n: int = 0, src: str = "", child: str = "") -> str:
    """One attachment, opened full size inside the message popup.

    **This route never serves the attachment's own bytes.** It returns markup this application
    built — escaped tables, decoded text — or, for markup we cannot re-emit, a `srcdoc` iframe with
    no `allow-scripts`. `/ui/mail/attachment` below keeps its stricter rule for the raw file, and
    this did not relax it: the viewer made reading possible without opening that door.

    `child` walks into containers by index path (`0.2` = third member of the first member), so a
    `.msg` inside a `.zip` is reachable. It is parsed defensively because it arrives from a query
    string, and bounded by `MAX_CONTAINER_DEPTH` inside `attachment_view.child_at`.
    """
    db_path = _store_for(src)
    conn = state_db.get_connection(db_path)
    conn.row_factory = sqlite3.Row
    try:
        found = attachment_bytes.resolve(conn, id, n)
        verdict = _attachment_verdict(conn, id, n, found.filename if found else "")
    finally:
        conn.close()

    if found is None:
        return html.attachment_viewer_missing(mail_url=html.mail_url_for(id, src))

    content, filename = found.content, found.filename
    content_type = found.content_type
    path = _child_path(child)
    if path:
        walked = attachment_view.child_at(content, filename, content_type, path)
        if walked is None:
            return html.attachment_viewer_missing(mail_url=html.mail_url_for(id, src),
                                                  message="That item is not inside this attachment.")
        content, filename, content_type = walked
        verdict = ""      # a container's child has no ledger row of its own to quote

    base = f"/ui/mail/attachment?id={quote(id)}&n={n}" + (f"&src={quote(src)}" if src else "")
    child_prefix = (f"/ui/mail/attachment/view?id={quote(id)}&n={n}"
                    + (f"&src={quote(src)}" if src else "")
                    + f"&child={quote('.'.join(str(p) for p in path) + '.' if path else '')}")
    panel = attachment_view.render(content, filename, content_type,
                                   attachment_url=base, child_url=child_prefix)
    return html.attachment_viewer(
        filename=filename, size=len(content), label=panel.label, verdict=verdict,
        body=html.Raw(panel.html), download_url=base + "&download=1",
        mail_url=html.mail_url_for(id, src),
        # Only the top-level attachment can be downloaded: a container's child has no bytes of its
        # own on the raw route, and offering a link that 404s is worse than not offering one.
        downloadable=not path,
    )


def _child_path(raw: str) -> list:
    """`"0.2"` → `[0, 2]`. Anything malformed is no path at all, never a partial one."""
    parts = [p for p in (raw or "").split(".") if p != ""]
    try:
        return [int(p) for p in parts]
    except ValueError:
        return []


def _attachment_verdict(conn, email_id: str, ordinal: int, filename: str) -> str:
    """What the ledger made of this attachment, shown beside it in the viewer."""
    try:
        row = conn.execute(
            "SELECT disposition, disposition_detail FROM attachment_ledger "
            " WHERE email_id = ? AND (ordinal = ? OR (? <> '' AND filename = ?)) "
            " ORDER BY CASE WHEN ordinal = ? THEN 0 ELSE 1 END LIMIT 1",
            (email_id, ordinal, filename or "", filename or "", ordinal),
        ).fetchone()
    except Exception:                                              # noqa: BLE001
        return ""
    if not row:
        return ""
    detail = row["disposition_detail"] or ""
    return f"{row['disposition']}{' — ' + detail if detail else ''}"


@router.get("/mail/attachment")
def mail_attachment(id: str = "", n: int = 0, download: int = 0, src: str = "") -> Response:
    """One attachment's bytes.

    These are files sent to Premier by outside parties, so they are treated as hostile: `nosniff`
    always, and only images and PDFs may render inline. Everything else downloads — an HTML
    attachment rendered in this origin would be script running as trusted page code, and the corpus
    contains exactly such an attachment.

    **That policy is deliberately unchanged by the attachment viewer.** `/ui/mail/attachment/view`
    renders a `.html` or a `.csv` readably, but it does so by building our own escaped markup or a
    scriptless sandbox — never by handing the browser attacker bytes under an attacker content
    type. This route is the door that stays shut; the viewer did not widen it.
    """
    conn = state_db.get_connection(_store_for(src))
    conn.row_factory = sqlite3.Row
    try:
        # Not `mail_cache` alone. The click cache holds bytes only for messages somebody has
        # already opened, and on Premier's live store 18 of 35 cached rows have none — every one of
        # them sitting on disk in `attachment_store` the whole time. `resolve` reads the cache
        # first and then the content-addressed store, which is what the server-side preview path
        # has always done; the two disagreeing is why an image 404'd beside a spreadsheet that
        # rendered.
        found = attachment_bytes.resolve(conn, id, n)
    finally:
        conn.close()

    # A miss is still a miss. 19 of the corpus's 72 attachments were dropped at ingest — decorative
    # signature images, duplicates by content hash — and serving a 0-byte 200 as `image/png` gives
    # the browser something it cannot render and no reason why. The popup shows the ledger's own
    # verdict beside each attachment; this just refuses to pretend there are bytes.
    if found is None:
        return Response(content=b"This attachment was not retained - see the verdict beside it.",
                        status_code=404, media_type="text/plain")

    content_type = (found.content_type or "").lower()
    kind = (found.kind or "").lower()
    inline_ok = (
        not download
        and (content_type.startswith("image/") or content_type == "application/pdf"
             or kind in ("image", "pdf"))
    )
    if not inline_ok:
        content_type = "application/octet-stream"

    filename = (found.filename or "attachment").replace('"', "")
    return Response(
        content=found.content,
        media_type=content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'{"inline" if inline_ok else "attachment"}; '
                                   f'filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; object-src 'none'",
        },
    )


def _num(value) -> str:
    """Quantities are REAL in SQLite, so 11 arrives as 11.0. Showing '11.0 EA' where the slip
    says '11 EA' invites a reader to wonder which one the pipeline actually read."""
    if value is None:
        return ""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


# ==========================================================================
# The operations pages, and the live mailbox
#
# Everything below came across from the standalone console on port 8500 when the three interfaces
# were collapsed into one. The logic did not move — it still lives in `operations/` — only the
# routes and the rendering did.
#
# The important line these pages must not cross: they read Premier's LIVE store, while every page
# above reads the `.msg` corpus. The two databases are never joined, and a page that mixed them
# would make the receiver report unusable as evidence.
# ==========================================================================

_MAX_FORM_BYTES = 64 * 1024


async def _form_values(request: Request) -> dict:
    """Parse an urlencoded form body without python-multipart.

    `await request.form()` cannot be used: Starlette asserts python-multipart is importable *before*
    it looks at the content type at all, so a plain urlencoded body — which is all an HTML form
    sends — raises AssertionError when the library is absent. That is what made every Save return
    500 and no schedule ever get written.

    Parsing the body directly keeps the dependency out, which also keeps multipart *upload* parsing
    structurally unreachable in an app whose entire guarantee is that it only reads.
    `tests/test_operations_controls.py` asserts the library stays out of requirements.txt.
    """
    if not request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
        return {}
    if int(request.headers.get("content-length") or 0) > _MAX_FORM_BYTES:
        return {}
    parsed = parse_qs((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def live_version_token() -> str:
    """A cheap string that changes whenever there is something new to look at.

    Deliberately reads nothing but `email_log`'s count and highest id, the arrivals signature, and
    the last run id. All are indexed reads costing well under a millisecond, which is what makes it
    safe to poll every ten seconds from every open tab.

    Explicitly *not* the Graph inbox listing. That is a ~3.2s round trip (`operations.inbox`) and
    polling it would cost more than the automation it is watching.

    **`mail_arrivals` is what makes this real-time.** `email_log` only changes when a full ingest
    pass finishes, which is minutes apart because the pass takes about forty seconds — so for the
    whole time this token watched `email_log` alone, the browser was faithfully asking every ten
    seconds about a number that could not move any faster than the thing it was waiting for. The
    arrivals signature moves within seconds of mail landing, and again when the pipeline reads it.

    Never raises. A version endpoint that 500s would make every page think it had new mail for ever.
    """
    try:
        conn = _live_conn()
    except Exception:                                              # noqa: BLE001
        return "unavailable"
    try:
        count, newest = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM email_log").fetchone()
        arrivals = mail_arrivals.version_signature(conn)
    except Exception:                                              # noqa: BLE001
        return "unavailable"
    finally:
        conn.close()

    try:
        ops_conn = ops_store.get_connection()
        try:
            last = ops_store.last_run(ops_conn)
        finally:
            ops_conn.close()
        run_id = last.id if last else 0
    except Exception:                                              # noqa: BLE001
        run_id = 0
    return f"{newest}:{count}:{run_id}:{arrivals}"


@router.get("/version")
def version_endpoint() -> dict:
    """What the page poller asks, ten seconds at a time. See `_JS` in `api/ui/html.py`."""
    return {"token": live_version_token()}


def _live_conn() -> sqlite3.Connection:
    """A connection to the LIVE store. The corpus lives in its own file and is reached through
    `deps.get_pipeline_conn`; nothing on these pages may open it."""
    conn = state_db.get_connection(settings.PIPELINE_STATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _s(count) -> str:
    return "" if count == 1 else "s"


def _ago(timestamp: str) -> str:
    """'2 hours ago' reads faster than a timestamp when the question is 'is this current?'."""
    try:
        then = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return f"at {timestamp}"
    seconds = (datetime.now() - then).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{int(seconds // 60)} minutes ago"
    if seconds < 172800:
        hours = int(seconds // 3600)
        return f"{hours} hour{_s(hours)} ago"
    days = int(seconds // 86400)
    return f"{days} day{_s(days)} ago"


@router.get("/automation", response_class=HTMLResponse)
def automation_page(refused: str = "") -> str:
    conn = ops_store.get_connection()
    try:
        schedule = ops_store.get_schedule(conn)
        last = ops_store.last_run(conn)
        # 200, not 25. The table was capped at 25 with nothing on screen saying so, which is the
        # worst way to lose data: "the automation has not run since Tuesday" and "the automation
        # ran forty times since Tuesday and you are looking at the newest twenty-five" render
        # identically. Now the pager holds the page size and the count says how many there are.
        runs = ops_store.recent_runs(conn, limit=200)
    finally:
        conn.close()

    running = runner.is_running()
    if running:
        tone, headline = "warn", "Running now…"
        since = runner.running_since()
        detail = ("The automation is working through the mail"
                  + (f", started {_ago(since)}" if since else "")
                  + ". You can leave this page — it keeps going, and this page refreshes itself "
                    "until it finishes.")
    elif last is None:
        tone, headline = "", "Not run yet"
        detail = "Press Run now to read the delivery emails and build the receiver report."
    elif last.error:
        tone, headline = "bad", f"Last run failed {_ago(last.started_at)}"
        detail = last.error
    elif last.emails == 0:
        # Zeroes read as a failure. A run that found nothing new is the normal, healthy outcome
        # once the inbox has been read once, and saying so is the difference between "it worked"
        # and "the button is broken".
        tone, headline = "good", f"Last run {_ago(last.started_at)}"
        detail = ("No new mail to read — everything in the inbox has already been through the "
                  "pipeline. "
                  + (f"{last.needs_person} item{_s(last.needs_person)} still need a person."
                     if last.needs_person else "Nothing needs a person."))
    else:
        tone, headline = "good", f"Last run {_ago(last.started_at)}"
        detail = (f"Read {last.emails} email{_s(last.emails)} and produced "
                  f"{last.records} receiver line{_s(last.records)}. "
                  + (f"{last.needs_person} need a person." if last.needs_person
                     else "Nothing needs a person."))

    progress = runner.progress() if running else None

    status = html.card(
        # The refusal sits above the status, not instead of it: it explains what this press did,
        # while the card below still answers what the automation is doing.
        html.tag("p", refused, class_="err") if refused else html.Raw(""),
        (html.progress_ring(progress["phase"], progress["done"], progress["total"],
                            progress.get("note", ""))
         if progress else html.Raw("")),
        html.tag("p", detail, class_="note"),
        html.stat_row([
            (last.emails if last else 0, "emails read"),
            (last.records if last else 0, "receiver lines"),
            (last.needs_person if last else 0, "need a person"),
            (f"{last.elapsed_seconds:g}s" if last else "—", "time taken"),
        ]),
        html.button_form("/ui/automation/run", "Run now", style="margin-top:18px"),
        title=headline,
        tone=tone,
    )

    next_at = scheduler.next_run_at()
    next_line = (f"Next automatic run at {next_at:%H:%M} on {next_at:%d %b}."
                 if next_at else "Automatic running is off.")
    schedule_card = html.card(
        html.tag(
            "form",
            html.tag(
                "div",
                # `name_` not `name`: a trailing underscore is how `tag()` renders an attribute
                # whose name would otherwise collide with its own first parameter.
                html.tag("div",
                         html.tag("input", type="checkbox", id="en", name_="enabled", value="1",
                                  checked="checked" if schedule.enabled else None),
                         html.tag("label", "Run automatically", for_="en", style="margin:0"),
                         class_="check"),
                html.tag("div",
                         html.tag("label", "Every (minutes)", for_="iv"),
                         html.tag("input", type="number", id="iv", name_="interval_minutes",
                                  min="1", max="1440", value=schedule.interval_minutes)),
                html.tag("div",
                         html.tag("label", "Read from", for_="src"),
                         html.tag("select",
                                  html.tag("option", "Sample delivery emails", value="sample",
                                           selected="selected" if schedule.source == ops_store.SOURCE_SAMPLE else None),
                                  html.tag("option", "Live Outlook inbox (read-only)", value="mailbox",
                                           selected="selected" if schedule.source == ops_store.SOURCE_MAILBOX else None),
                                  id="src", name_="source")),
                html.tag("div", html.tag("button", "Save", type="submit", class_="btn ghost")),
                class_="row",
            ),
            html.tag("p", next_line, class_="note", style="margin:14px 0 0"),
            method="post", action="/ui/automation/schedule",
        ),
        title="Schedule",
        note="Mail is only ever read. Nothing is moved, filed or marked in Outlook.",
    )

    watch = _arrival_watch()
    next_poll = scheduler.next_arrival_poll_at()
    if watch.last_error:
        watch_line = f"Last check failed: {watch.last_error}"
    elif watch.last_poll_at:
        watch_line = (f"Last checked {_ago(watch.last_poll_at)}, finding {watch.last_new} new "
                      f"message{_s(watch.last_new)}."
                      + (f" Next check at {next_poll:%H:%M:%S}." if next_poll else ""))
    else:
        watch_line = "Not checked yet." if watch.enabled else "The watch is off."
    watch_card = html.card(
        html.tag(
            "form",
            html.tag(
                "div",
                html.tag("div",
                         html.tag("input", type="checkbox", id="aw", name_="enabled", value="1",
                                  checked="checked" if watch.enabled else None),
                         html.tag("label", "Watch for new mail", for_="aw", style="margin:0"),
                         class_="check"),
                html.tag("div",
                         html.tag("label", "Every (seconds)", for_="as"),
                         html.tag("input", type="number", id="as", name_="interval_seconds",
                                  min=str(ops_store.MIN_ARRIVAL_SECONDS), max="600",
                                  value=watch.interval_seconds)),
                html.tag("div", html.tag("button", "Save", type="submit", class_="btn ghost")),
                class_="row",
            ),
            html.tag("p", watch_line, class_="note", style="margin:14px 0 0"),
            method="post", action="/ui/automation/arrivals",
        ),
        title="New-mail watch",
        note="A metadata-only check — subject, sender, date — so mail shows on the Mail page "
             "within seconds of arriving instead of waiting for the next run. It reads no "
             "attachments and sends nothing to OCR, so it costs nothing to run often. The run "
             "above is still what actually processes the mail.",
    )

    rows = [[
        html.when(r.started_at),
        html.badge("scheduled" if r.trigger == "scheduled" else "by hand",
                   "info" if r.trigger == "scheduled" else ""),
        "Sample emails" if r.source == ops_store.SOURCE_SAMPLE else "Live inbox",
        r.emails, r.records, r.needs_person,
        f"{r.elapsed_seconds:g}s" if r.finished_at else html.muted("running…"),
        html.tag("span", r.error, class_="err") if r.error else html.muted("—"),
    ] for r in runs]
    history = html.card(
        html.search_box("run-history", placeholder="Search date, trigger, source, error…",
                        label="Search run history"),
        html.date_filter("run-history", label="run date"),
        html.table(
            ["Started", "How", "Read from", "Emails", "Receiver lines", "Need a person",
             "Took", "Problem"],
            rows, empty="No runs yet. Press Run now above.",
            table_id="run-history", page_size=25, date_column="Started",
        ),
        title="Run history",
    )

    return html.page("Automation", "/ui/automation", status, schedule_card, watch_card, history,
                     subtitle="Run the automation, or have it run itself.",
                     # Only while a run is in flight, so the page stops reloading the moment it
                     # settles. `running` was read once above, before the render, so a run that
                     # finishes mid-render still leaves one last refresh to show the result.
                     #
                     # Two seconds, not five: this is what advances the progress ring, and a run
                     # of this length redrawing three times total would not read as progress.
                     refresh_seconds=2 if running else 0)


@router.post("/automation/run")
def automation_run():
    conn = ops_store.get_connection()
    try:
        source = ops_store.get_schedule(conn).source
    finally:
        conn.close()
    # Starts the run and returns; the page redraws showing "Running now…" instead of hanging for
    # the thirty seconds a live-mailbox pass takes, and stays usable while it finishes.
    outcome = runner.start_background(trigger="manual", source=source)

    # A refused press must say so. The outcome used to be discarded, so pressing the button during
    # a run — a thirty-second window, so not a rare accident — redirected in silence and looked
    # exactly like a button that does nothing. A 303 cannot carry a body, hence the query string.
    if outcome.skipped:
        return RedirectResponse(f"/ui/automation?refused={quote(outcome.error or 'busy')}",
                                status_code=303)
    return RedirectResponse("/ui/automation", status_code=303)


@router.post("/automation/schedule")
async def automation_schedule(request: Request):
    form = await _form_values(request)
    try:
        interval = int(str(form.get("interval_minutes", ops_store.DEFAULT_INTERVAL_MINUTES)))
    except ValueError:
        interval = ops_store.DEFAULT_INTERVAL_MINUTES
    source = str(form.get("source", ops_store.SOURCE_SAMPLE))

    conn = ops_store.get_connection()
    try:
        # `now` anchors the schedule, so saving is only ever saving: the first automatic run is one
        # interval from this moment, not immediately. Enabling a schedule used to fire a live Graph
        # read within five seconds.
        ops_store.set_schedule(conn, ops_store.Schedule(
            enabled=form.get("enabled") == "1",
            interval_minutes=max(1, min(1440, interval)),
            source=source if source in (ops_store.SOURCE_SAMPLE, ops_store.SOURCE_MAILBOX)
            else ops_store.SOURCE_SAMPLE,
        ), now=_now())
    finally:
        conn.close()
    return RedirectResponse("/ui/automation", status_code=303)


@router.post("/automation/arrivals")
async def automation_arrivals(request: Request):
    """Turn the new-mail watch on or off, and set how often it looks.

    Separate from `/automation/schedule` because they are separate jobs with costs two orders of
    magnitude apart — a metadata listing measured in milliseconds against a pipeline pass measured
    in tens of seconds. One form saving both would mean one of them gets the wrong interval.

    Enabling this is the explicit act the standing no-auto-trigger rule asks for: nothing polls
    Premier's mailbox until someone presses Save here with the box ticked.
    """
    form = await _form_values(request)
    try:
        seconds = int(str(form.get("interval_seconds", ops_store.DEFAULT_ARRIVAL_SECONDS)))
    except ValueError:
        seconds = ops_store.DEFAULT_ARRIVAL_SECONDS

    conn = ops_store.get_connection()
    try:
        ops_store.set_arrival_watch(
            conn,
            enabled=form.get("enabled") == "1",
            interval_seconds=max(ops_store.MIN_ARRIVAL_SECONDS, min(600, seconds)),
        )
    finally:
        conn.close()
    return RedirectResponse("/ui/automation", status_code=303)


@router.post("/automation/stop")
def automation_stop(request: Request):
    """The kill switch. Deliberately does not take `runner._LOCK` — the whole point is that it
    answers while a run is holding it.

    Returns to wherever it was pressed, because it is in the sidebar on every page and bouncing an
    operator to Automation from the middle of the receiver report would lose their place.
    """
    conn = ops_store.get_connection()
    try:
        killswitch.engage(conn, _now())
    finally:
        conn.close()
    return RedirectResponse(_back_to(request), status_code=303)


@router.post("/automation/resume")
def automation_resume(request: Request):
    conn = ops_store.get_connection()
    try:
        killswitch.release(conn)
    finally:
        conn.close()
    return RedirectResponse(_back_to(request), status_code=303)


def _back_to(request: Request) -> str:
    """The /ui page the request came from, or Automation if it cannot be trusted.

    `Referer` is attacker-influenced and is never echoed into a redirect as given. Three checks,
    and all three are needed:

    * the origin must be ours — otherwise a page on another site can choose which of our paths the
      operator lands on after pressing Stop;
    * the path must be under `/ui/`, so `/api/...` and the docs are not reachable this way;
    * `//host` is rejected outright, because a browser reads a Location beginning `//` as a
      protocol-relative URL to somewhere else entirely.
    """
    parsed = urlparse(request.headers.get("referer") or "")
    if parsed.netloc and parsed.netloc != request.headers.get("host"):
        return "/ui/automation"
    path = parsed.path or ""
    if path.startswith("/ui/") and not path.startswith("//"):
        return path
    return "/ui/automation"


@router.get("/inbox", response_class=HTMLResponse)
def inbox_page():
    """Gone — Mail is the one list of everything read from the inbox.

    A redirect rather than a deletion: this was a sidebar entry for weeks and the URL is the kind
    of thing that ends up in a bookmark or a message to Premier. 307 rather than 301 so nothing
    caches the move permanently while the shape of these pages is still settling.
    """
    return RedirectResponse("/ui/mails", status_code=307)


@router.get("/report", response_class=HTMLResponse)
def report_page() -> str:
    """The receiver report, built from the LIVE mailbox.

    ⚠️ **This page and the sheet on /ui/records are now the same report over the same data.** They
    were the same builder run over two different stores — corpus here, live mail there — and that
    was the entire reason they were separate pages. The corpus was retired on 2026-08-12, so the
    distinction has collapsed and one of the two should go. Left in place deliberately rather than
    removed in passing: it is a sidebar entry Premier has been shown, and deciding which of the two
    names survives is theirs.
    """
    conn = _live_conn()
    try:
        report = receipt_log.build(conn)
        report.empty_message = _empty_report_message(conn)
        queue = read_views.manual_queue(conn)
    finally:
        conn.close()

    summary = html.card(
        html.stat_row([
            (len(report.purchase_orders), "purchase orders"),
            (report.line_count, "lines"),
            (report.receipt_count, "deliveries recorded"),
            (len(queue), "need a person"),
        ]),
        # The empty message sits *above* the buttons rather than replacing them. It used to
        # replace them, which meant the one state where someone most wants the file — "this is
        # empty, prove it to me" — was the one state with no way to get it. The workbook is valid
        # either way: header block, column layout, and the explanation carried inside the sheet.
        html.tag("p", report.empty_message, class_="empty") if report.is_empty else html.Raw(""),
        html.tag("p",
                 "The 14 test emails produce a report with purchase orders in it — that one is on ",
                 html.tag("a", "Records", href="/ui/records"),
                 ". This page is the live mailbox, and the two are never mixed.",
                 class_="note") if report.is_empty else html.Raw(""),
        html.tag("div",
                 html.tag("button", "Preview report", type="button", class_="btn",
                          data_open="preview"),
                 html.tag("a", "Download Excel", href="/ui/report.xlsx", class_="btn ghost"),
                 class_="row", style="margin-top:18px"),
        title="What the delivery emails told us",
        note="Laid out like Spitfire's Receipt Log and grouped by PO number, so the two can be "
             "compared line by line. Built from Premier's live receiving mailbox — the sample "
             "emails used for testing are kept in a separate database and never appear here.",
    )

    blank_note = html.card(
        html.tag("p", "Order Qty, Net and Final are deliberately blank. They live on the purchase "
                      "order inside Spitfire and no delivery email carries them, so filling them "
                      "in would mean inventing numbers. They will populate once the connection to "
                      "Spitfire is in place.", class_="note", style="margin:0"),
        title="Why three columns are empty",
    )

    groups = (
        ("email", "Emails", "Could not be processed automatically."),
        ("attachment", "Attachments", "Arrived but could not be read."),
        ("record", "Lines", "Read, but missing something needed to match them to a purchase order."),
    )
    attention = []
    present = [k for k, _t, _n in groups if any(i.kind == k for i in queue)]
    if present:
        attention.append(html.card(
            html.search_box([f"report-{k}" for k in present],
                            placeholder="Search subject, reason, detail…",
                            label="Search everything needing attention"),
            html.date_filter([f"report-{k}" for k in present], label="date it arrived"),
            title="Find something",
            note="Searches all the tables below at once.",
        ))
    for kind, title, note in groups:
        items = [i for i in queue if i.kind == kind]
        if not items:
            continue
        rows = [[i.subject[:70] or html.muted("—"), html.when(i.when[:16]), i.reason,
                 i.detail or html.muted("—")] for i in items]
        attention.append(html.card(
            html.table(
                ["What arrived", "When", "Why it needs you", "Detail"], rows,
                table_id=f"report-{kind}", page_size=25, date_column="When",
                frag_urls=[f"/ui/mail?id={quote(i.email_id or '')}"
                           f"&reason={quote(i.reason or '')}&src=live" for i in items],
                frag_title="Open this message",
            ),
            title=f"{title} — {len(items)}",
            note=note + "  Click any row to read the message and see what was attached.",
        ))
    if not attention:
        attention = [html.card(html.tag("p", "Nothing needs your attention.", class_="empty"),
                               title="Needs attention")]

    # `to_html` returns a plain escaped string so it can serve any caller; wrap it here.
    preview = html.modal("preview", "Receiver report", html.Raw(receipt_log.to_html(report)))
    mailbox = settings.GRAPH_MAILBOX_ADDRESS or "the receiving mailbox"
    return html.page("Receiver report", "/ui/report", summary, blank_note, *attention, preview,
                     subtitle=f"Built from {mailbox} · generated {report.generated_at}")


@router.get("/report.xlsx")
def report_download() -> Response:
    conn = _live_conn()
    try:
        report = receipt_log.build(conn)
        report.empty_message = _empty_report_message(conn)
    finally:
        conn.close()
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    return Response(
        content=receipt_log.to_xlsx(report),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="Receiver_Report_{stamp}.xlsx"'},
    )


def _empty_report_message(conn) -> str:
    """Why the report is empty, in the reader's terms.

    "Nothing yet" is only true before the first run. Afterwards the honest and much more common
    answer is that mail *was* read and none of it was a delivery — which is the current state of
    Premier's receiving mailbox, and is not a failure of anything. Saying so plainly is the
    difference between a screen that looks broken and one that says what it is waiting for.
    """
    mailbox = settings.GRAPH_MAILBOX_ADDRESS or "the receiving mailbox"
    try:
        seen = read_views.summary(conn)
    except Exception:                                          # noqa: BLE001
        return "No purchase orders yet."
    if not seen.emails:
        return f"Nothing has been read from {mailbox} yet. Go to Automation and press Run now."
    return (
        f"{seen.emails} message(s) have been read from {mailbox}, and none of them was a delivery "
        "notification — so there is correctly nothing to receive yet. Delivery mail is not being "
        "forwarded to this mailbox yet; once it is, lines will appear here on their own. Anything "
        "that was read and set aside is listed below."
    )
