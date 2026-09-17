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
import base64
import csv
import io
import os
import posixpath
import sqlite3
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape as _escape
from typing import Dict, Optional
from urllib.parse import parse_qs, quote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from api import auth, deps
from api.forms import form_list as _form_list, form_values as _form_values
from api.stores import extracted_store
from api.ui import html
from config import settings
from connectors import spitfire_cassette
from operations import inbox as inbox_reader
from operations import arrivals, killswitch, runner, scheduler
from operations import store as ops_store
from pipeline import (attachment_bytes, attachment_ledger, attachment_view, completeness,
                      deliveries_store,
                      delivery_status, email_log, mail_arrivals, mail_cache, mail_overrides,
                      mail_view, po_verify,
                      post_decision, post_ledger, read_views, receipt_log, record_completion,
                      record_create,
                      record_edit, spitfire_post, state_db)

router = APIRouter(prefix="/ui", tags=["ui"], include_in_schema=False)

_BOOT = str(os.getpid())
"""This worker's identity, mixed into the version token so a restart reloads every open tab.

The rest of the token is derived from the database, which means it moves when there is new
*data* and never when there is new *code* — so before this, a code change reached the server but
an open tab kept rendering the old markup until someone pressed refresh, which read as "the
change didn't work". The pid changes on every respawn, and `run_api.py` respawns the whole
process on every edit, so one supervised restart now reloads what the browser is showing.
"""

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
    # The third element is the card's tone, and it is the badge ramp's, not a new one: a `surface`
    # figure and a `surface` pill in the table below it are the same green. `alert` is spent once,
    # on the only figure that means a person has to do something — and only while it is not zero.
    tones = {"surface": "good", "hold": "warn", "error": "warn"}
    pairs = [("emails", s.emails, "")]
    pairs += [(category, s.by_category[category], tones.get(category, ""))
              for category in order if s.by_category.get(category)]
    pairs += [
        ("records", s.records_total, ""),
        # `records_postable`, not `records_ready`. "Ready" is read as "work that can be done now",
        # and `records_ready` only means "no missing field" — 169 of those against 45 that could
        # actually post. The complete-but-unprovable ones stay visible on the Records page with
        # their blocker; they are simply not counted as ready any more.
        ("ready to post", s.records_postable, "good"),
        ("complete", s.records_ready, ""),
        # Records read from Premier's own mail that nobody has confirmed. In records, not queue
        # rows — the queue shows one row per message and those rows stand for thousands of records,
        # so "need a human" cannot answer "where did the other records go".
        ("awaiting confirmation", s.records_awaiting_confirmation, ""),
        ("need a human", s.needs_human, "alert" if s.needs_human else ""),
        ("OCR pages", s.ocr_pages, ""),
    ]
    return {
        "header": html.stats(pairs),
        # Who is looking, for the rail's sign-out control. Read from the request's context rather
        # than passed in, so that none of the ten page handlers calling `_chrome` has to take a
        # `request` it makes no other use of. See `auth.signed_in_user`.
        "user": auth.signed_in_user(),
        # Clipped to the minute, and the `T` dropped. It reads in the sidebar now (see
        # `html.page`), where `2026-08-17T17:41:18.142282+00:00` would wrap to three lines to
        # deliver six digits of precision nobody has ever wanted from it.
        "footer": (f"Last run {s.last_run[:16].replace('T', ' ')}" if s.last_run else "Never run."),
        **_sidebar_counts(s),
    }


# The 500-row render cap that used to live here is gone, along with `_cap()` and the `?all=1`
# parameter that went with it. It capped what was *sent*, never what existed, and paid for that with
# a note under every long table reading "The 500 most recent of 2,373 are loaded — the search and
# filters cover these 500" — an admission, printed on the page, that the search box above it was
# answering about a slice. Every table now ships every row it stands for.
#
# What the cap was really guarding against is handled properly now: the response is gzipped
# (`api/main.py`), and the one table too large to send at all is paged in SQL instead
# (`read_views.attachments`, which has taken `q`/`limit`/`offset` since it was written).


def _sidebar_counts(summary=None) -> dict:
    """Just the sidebar's queue figure, for a page that builds its own header.

    Automation and Report show four figures of their own rather than the eight-chip strip, so they
    cannot take `_chrome()` wholesale — and because they took none of it, they were the two pages
    with no queue badge and no alert card at all. A rail that changes shape depending on which page
    you are on reads as a bug in the rail, so the count is separable from the header it used to
    arrive with.

    `summary` is the one `_chrome()` already holds. **It must be passed when there is one.** This
    was called bare from inside `_chrome()`, so every page that took the eight-chip header ran the
    whole cross-store scan *twice* to render one screen — measured at 696ms and then 572ms against
    the live store, for a single integer the first scan had already computed. That is more than half
    the render time of every page in the app, spent on nothing, and it is exactly the duplication
    `_chrome`'s own docstring says it exists to prevent.

    The parameter stays optional because Automation and Report genuinely have no summary to pass;
    they are the two callers this function was written for.
    """
    s = summary if summary is not None else read_views.summary_across_sources()
    return {"counts": {"/ui/manual": s.needs_human}}


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def index():
    return RedirectResponse("/ui/mails", status_code=307)


@router.get("/mails", response_class=HTMLResponse)
def mails_page(refresh: int = 0, checked: str = "",
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
        # Two kinds of unread, and they are not the same news. One is waiting for the next run; the
        # other has left the mailbox, so no run will ever read it and the row saying "waiting for
        # the next run" is a promise nothing can keep. `recovery_missing_at` is what tells them
        # apart — a by-id lookup went looking on that date and the mailbox did not have it.
        #
        # Worded as what that lookup actually established rather than as a flat "gone": the
        # evidence is one probe on one day, which is exactly how `mark_missing` describes itself.
        if a.recovery_missing_at:
            verdict = html.badge("not in the mailbox", "error")
            # The way this row ends. Without it the queue cannot reach zero: no run can read a
            # message the mailbox does not have, so nothing but a person can close it out.
            why = html.tag(
                "span",
                html.muted(f"looked for on {(a.recovery_missing_at or '')[:10]} "
                           f"and not found — no run can read it now. "),
                html.button_form("/ui/mails/acknowledge", "Accept it is lost",
                                 cls="btn ghost small", hidden_fields={"email_id": a.email_id}),
            )
        else:
            verdict = html.badge("not read yet", "hold")
            why = html.muted("waiting for the next run")
        rows.append([
            _source_cell("inbox", "Inbox", a.source_folder),
            _stamp((a.received_at or "")[:16].replace("T", " ")),
            _subject_cell(a.subject, a.sender),
            verdict, html.muted("—"),
            why, html.muted("—"),
            "yes" if a.has_attachments else html.muted("0"),
            html.muted("—"), html.muted("0"), html.muted("—"),
        ])

    for m in mails_list:
        attachments = str(m.attachment_count)
        if m.attachments_flagged:
            attachments = html.Raw(f"{m.attachment_count} " + str(html.muted(f"({m.attachments_flagged} flagged)")))
        rows.append([
            _source_cell(m.source, m.source_label, m.source_folder),
            _stamp(m.email_date[:16]),
            # Truncated, with the whole thing on hover. Subject and Why are the only free-text
            # columns — left full they wrap to seven lines and every row stands 140px tall, which
            # is what a dense grid exists to avoid.
            _subject_cell(m.subject, m.sender, via=m.origin_sender),
            html.badge(m.category, m.category), _stamp(m.matched_rule),
            _clipped(m.reason, 48), m.po_hints or html.muted("—"),
            attachments, m.records, m.ocr_attempted or html.muted("0"), m.folder,
        ])
    # Built from the rows actually on the page, never a fixed list. A verdict nothing carries is a
    # choice whose only possible outcome is an empty table, and `hide` is absent far more often
    # than it is present.
    verdicts = [(v, v) for v in sorted({m.category for m in mails_list if m.category})]
    # The arrivals' own cells read "not read yet" or "not in the mailbox" rather than a category,
    # so each needs its own entry — and only when a row actually carries it. Offering "not read
    # yet" while every unread row had in fact left the mailbox gave a filter whose only possible
    # result was an empty table.
    if any(not a.recovery_missing_at for a in pending):
        verdicts.append(("not read yet", "not read yet"))
    if any(a.recovery_missing_at for a in pending):
        verdicts.append(("not in the mailbox", "not in the mailbox"))
    # One entry above the verdicts standing for all of them that mean a person still has to look.
    # Asking "what is waiting on me" is the question this page gets asked most, and answering it
    # by picking each verdict in turn and adding up is not answering it.
    #
    # "not in the mailbox" is deliberately NOT in the triage queue: nobody can action it — the
    # message is gone — and a queue that contains work that cannot be done is one people abandon.
    waiting = [v for v, _ in verdicts if v in ("hold", "error", "not read yet")]
    views = ([("|".join(waiting), "My triage queue")] if waiting else []) + verdicts

    body = html.section(
        "",
        _unprocessed_note(pending=len(pending),
                          gone=sum(1 for a in pending if a.recovery_missing_at),
                          checked=checked),
        html.tag(
            "div",
            html.search_box("mail-table",
                            placeholder="Search PO, subject, sender, verdict…",
                            label="Search mail"),
            html.date_filter("mail-table", label="received date", presets=True),
            html.choice_filter("mail-table", views, label="View",
                               all_label="All mail", boxed=True),
            class_="controls",
        ),
        html.table(
            ["Source", "Received", "Subject", "Verdict", "Rule", "Why", "POs",
             "Attachments", "Records", "OCR", "Filed to"],
            rows, empty=_EMPTY_HINT, table_id="mail-table", page_size=25, pane=True,
            date_column="Received", choice_column="Verdict",
            # **Not capped, deliberately** — the one list page that ships every row.
            # `_cap` orders newest-first, and this is the page people come to when they are
            # looking for a message from a while ago; a cap here answers "no mail matches"
            # for mail that is sitting in the table's own CSV. It is also the cheapest of the
            # heavy pages to render, so it is the one where the cap would buy least.
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
    )
    return html.page(
        "Mail", "/ui/mails", body,
        subtitle="Every email through Stage 1, newest first. One row per email, always. "
                 "Click a row to read the message and its attachments.",
        actions=[_export_mail(), _check_now()],
        **_chrome(conn))


_MAIL_CSV_COLUMNS = ("Source", "Read from", "Received", "Subject", "From", "Origin", "Verdict",
                     "Rule", "Why", "POs", "Attachments", "Flagged", "Records", "OCR", "Filed to",
                     "Email id")
"""`Read from` is the mailbox folder, `Filed to` is where the pipeline put it afterwards. Both,
because "this arrived in Junk and we filed it as Processed" is one row's whole story and either
column alone tells half of it."""


@router.post("/mails/check")
def mails_check():
    """Read the mailbox now, then send the browser back to a page built from the result.

    **The redirect is the fix, not a nicety.** `mails_page` assembles its table before it evaluates
    anything below it, so the previous design — a GET that read Graph part-way down the render —
    wrote what it found into the database and then drew a table that had already been built. The
    press appeared to do nothing; a second press showed the mail the first one had fetched. Doing
    the work first and redirecting means the GET that follows starts from a database that already
    has the rows.

    Routed through `arrivals.poll_once(deep=True)` rather than reading the mailbox here, so a press
    takes the same lock as the fifteen-second watch. Two presses, or a press landing on a tick, are
    now refused rather than run concurrently — the old path took no lock at all and could walk the
    mailbox twice at once. It also advances the same watermark and is recorded as the same kind of
    event, so pressing this leaves the watch ahead instead of invisible to it.

    Never raises: `poll_once` returns its failure, and the outcome rides back in the query string
    because a 303 cannot carry a body.
    """
    # Waits for the lock rather than bouncing off it. The background watch ticks every fifteen
    # seconds and holds this for a few, so without the wait a large share of presses came back
    # "already running, this press did nothing" — a button that refuses the person who pressed it
    # because a timer got there first is the same dead button, differently worded.
    outcome = arrivals.poll_once(deep=True, wait_seconds=25)

    if outcome.skipped:
        result = "busy"
    elif not outcome.ok:
        result = f"error:{outcome.error or 'unknown'}"
    else:
        result = f"ok:{outcome.listed}/{outcome.new}"

    # Recorded exactly as `scheduler._tick_arrivals` records a scheduled poll. Without this the
    # "Last checked …" line on both pages ignored the button that had just checked, so pressing it
    # left the screen still saying the mailbox had not been looked at for four minutes.
    try:
        conn = ops_store.get_connection()
        try:
            ops_store.record_arrival_poll(
                conn, at=_now(), new=outcome.new,
                error=None if outcome.ok else (outcome.error or "unknown"))
        finally:
            conn.close()
    except Exception:                                              # noqa: BLE001
        pass                      # the check itself succeeded; failing to log it must not 500

    return RedirectResponse(f"/ui/mails?checked={quote(result)}", status_code=303)


@router.post("/mails/acknowledge")
async def mails_acknowledge(request: Request):
    """Accept that one lost message is lost, so it stops being counted and shown.

    The id travels in the form body rather than the path: a Message-ID is up to 255 characters of
    angle brackets and `@`, and putting that in a URL segment means escaping it at both ends for no
    gain.

    `mail_arrivals.acknowledge` refuses anything not already marked missing, so this cannot be used
    to dismiss mail that is merely waiting — that clears itself on the next run, and hiding it would
    turn a self-clearing row into an invisible one.
    """
    form = await _form_values(request)
    email_id = str(form.get("email_id") or "")
    if email_id:
        conn = _live_conn()
        try:
            mail_arrivals.acknowledge(conn, email_id, _now())
        finally:
            conn.close()
    return RedirectResponse(_safe_return(str(form.get("return_to") or ""), "/ui/mails"),
                            status_code=303)


@router.get("/mails.csv")
def mails_csv(conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> Response:
    """The Mail table as a file, whole and untruncated.

    Read-only, like the page: the same `mails_across_sources()` the table is built from, with the
    values full-length rather than clipped to a column width, and the arrivals nothing has read
    yet in the same place they sit on screen — above the verdicts, marked as unread rather than
    given one.

    Values go through `_csv_safe`. Every cell here is a string a mail server chose, and a
    spreadsheet treats one that opens with `=` as a formula to run.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(_MAIL_CSV_COLUMNS)
    for a in mail_arrivals.pending(conn):
        writer.writerow(_csv_safe([
            "inbox", a.source_folder, (a.received_at or "")[:16].replace("T", " "),
            a.subject, a.sender, "",
            "not read yet", "", "waiting for the next run", "",
            "yes" if a.has_attachments else "0", "", "", "0", "", a.email_id,
        ]))
    for m in read_views.mails_across_sources():
        writer.writerow(_csv_safe([
            m.source_label, m.source_folder, m.email_date[:16], m.subject, m.sender,
            m.origin_sender or "",
            m.category, m.matched_rule, m.reason, m.po_hints, m.attachment_count,
            m.attachments_flagged, m.records, m.ocr_attempted, m.folder, m.email_id,
        ]))
    # A BOM, so Excel opens a UTF-8 file as UTF-8 instead of as the local codepage — without it
    # every non-ASCII character in a subject line arrives as mojibake.
    return Response(
        buffer.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="premier-mail.csv"',
                 "X-Content-Type-Options": "nosniff"},
    )


def _csv_safe(values) -> list:
    """Cells a spreadsheet will not execute.

    Excel and Sheets both treat a cell opening with `=`, `+`, `-` or `@` as a formula, and every
    string in this file is text a mail server sent — the same untrusted input the rest of this
    module escapes on the way into HTML. A leading apostrophe makes it text again.
    """
    out = []
    for value in values:
        text = "" if value is None else str(value)
        out.append("'" + text if text[:1] in ("=", "+", "-", "@") else text)
    return out


def _gap_badge(row) -> html.Raw:
    """Whether a receiver line could be built from this record, and if not, what is stopping it.

    Deliberately not a confidence score. "0.50" says nothing a person can act on; "4 gaps" with
    `missing: vendor, PO line #, POD date, received-by` on hover names the cells to fill.

    A record with gaps also gets the control that closes them, in the same cell that names them.
    The badge alone told a reviewer what was wrong and offered nothing to do about it, and the
    fields it names are exactly the ones the attached proof of delivery can answer.
    """
    row_gaps = completeness.gaps(row)
    if row_gaps.is_complete:
        advisory = row_gaps.describe(advisory=True)
        return html.tag("span", html.badge("Complete", "good"), title=advisory or "every field present")
    return html.tag(
        "span",
        html.badge(f"{row_gaps.count} gaps", "hold"),
        " ",
        html.verify_button(f"/ui/records/{row['id']}/complete", "Fill",
                           title=f"Fill {row_gaps.describe()} from the attached proof of delivery",
                           small=True, ghost=True),
        title=row_gaps.describe(advisory=True))


def _mm(value, flags, field):
    """A cell that disagrees with Spitfire, with the reason on hover.

    A span rather than a class on the `<td>`: `html.table` wraps every cell identically and has no
    per-cell styling, and `token`/`muted`/`badge` already mark cells this way.
    """
    reason = (flags or {}).get(field)
    if not reason:
        return value
    return html.tag("span", value, class_="mm", title=reason)


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


def _stamp(value) -> html.Raw:
    """An identifier — a timestamp, a rule name — in the column's own monospaced face.

    Two columns of these sit side by side on Mail, and in a proportional face `2026-08-17T17:41`
    over `2026-08-12T14:08` does not line up digit for digit, so a column of times cannot be read
    as a column. The prose columns beside them stay in the text face.
    """
    return html.tag("span", html.when(value), class_="mono")


def _source_cell(source: str, source_label: str, source_folder: str = "") -> html.Raw:
    """Where this message came from: which store, and — when it is not the Inbox — which folder.

    The two are different questions and the column answers whichever one is informative. `source`
    names the SQLite store, which is what a message link needs; `source_folder` names the mailbox
    folder, which is what tells somebody Exchange is filing warehouse mail as spam.

    Junk wins the cell when it applies, because it is the rarer and more urgent fact. Inbox mail
    keeps the store badge exactly as before, so the common row is unchanged.
    """
    if (source_folder or "").lower() == "junkemail":
        return html.badge("junk", "junk")
    return html.badge(source_label, source)


def _short_address(address: str, limit: int = 20) -> str:
    """`Rahulconsciouscreations@outlook.com` → `Rahul…@outlook.com`.

    The domain is kept whole and the local part gives way, because which mailbox this came from is
    the part that identifies a sender at a glance — and a truncation that cut the domain off would
    make every address on the page end in the same meaningless prefix.

    The full address is still in the cell's `title`, which is also where the search box looks.
    """
    text = (address or "").strip()
    if len(text) <= limit or "@" not in text:
        return text
    local, _, domain = text.partition("@")
    keep = max(3, limit - len(domain) - 2)
    if len(local) <= keep:
        return text
    return local[:keep] + "…@" + domain


def _subject_cell(subject, sender, via: str = "") -> html.Raw:
    """Subject over sender, in one cell.

    They were two columns. One long address set the width of the sender column, which left the
    subject — the thing anyone actually scans this table for — squeezed beside it, and the pair
    took a third of the grid between them. Stacked, the subject gets the width and the sender is
    still there to be read.

    `via` is the recovered origin of a forward: every corpus message is a `Fw:` from an internal
    expeditor, so the envelope sender is example-pm.test on all of them and the origin is the one
    that means anything.
    """
    subject_text = (subject or "").strip() or "(no subject)"
    shown = subject_text if len(subject_text) <= 52 else subject_text[:52].rstrip() + "…"
    line = _short_address(sender)
    full = (sender or "").strip()
    if via and via.strip().lower() != (sender or "").strip().lower():
        line = f"{line} (via {_short_address(via)})"
        full = f"{full} (via {via})"
    return html.tag(
        "div",
        html.tag("span", shown, class_="subj nw", title=subject_text),
        html.tag("span", line or "—", class_="from nw", title=full or "no sender"),
        class_="cell-subject",
    )


def _export_mail() -> html.Raw:
    """The table someone is looking at, as a file they can keep.

    Every row and every column, not the page on screen: an export that silently stopped at fifty
    rows would be worse than none, because nothing about the file would say it was partial.
    """
    return html.tag("a", html.DOWNLOAD_ICON, "Export", href="/ui/mails.csv", class_="btn small",
                    title="Download every row of this table as CSV")


def _unread_arrivals() -> int:
    """How many messages the watch has seen that the pipeline has recorded no verdict for.

    Its own helper because the automation page needs the same number the Mail page shows, and a
    run's health cannot be judged without it: `emails == 0` means "idle inbox" or "the run could
    not reach the mail", and only this distinguishes them.

    Never raises and answers 0 when it cannot tell. A status card must degrade to saying less, not
    to failing — but note 0 therefore means "no evidence of a problem", not "no problem".
    """
    try:
        conn = _live_conn()
        try:
            # `recoverable_count`, not `pending_count`: a message the mailbox no longer has
            # is unread and always will be, and counting it here would light this warning for
            # ever. Health is about what is still actionable.
            return mail_arrivals.recoverable_count(conn)
        finally:
            conn.close()
    except Exception:                                              # noqa: BLE001
        return 0


def _unprocessed_note(pending: int = 0, gone: int = 0, checked: str = "") -> html.Raw:
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

    **Nothing on this page talks to Graph any more, in any render.** The explicit re-read is
    `POST /ui/mails/check`, which does the work and redirects — see `_check_now`. `?refresh=1` is
    accepted and ignored: it was the old spelling, it is in browser history and bookmarks, and
    every automatic reload of a tab that still carried it paid for another full mailbox walk.
    """
    # The count comes first on every branch, including the ones that go on to explain that the
    # watch is off or broken. It was originally reported only when the watch was healthy, which
    # meant the page fell silent about mail it was already showing at exactly the moment something
    # was wrong — the moment that number matters most.
    # **The headline counts only what is still waiting to be read**, which is what "unprocessed"
    # has always meant here: work the automation has yet to do. A message that has left the mailbox
    # is not waiting for anything — we looked for it by id, the mailbox did not have it, and no run
    # can ever read it. Counting it as unprocessed made the figure unclearable, and a number that
    # can never reach zero is one people stop reading, which is the whole reason `recoverable_count`
    # draws this distinction for the Automation page.
    #
    # It is still *said*, in its own clause, because it is a loss and one of them was an urgent
    # purchase-order email. Silently dropping it to make a zero would be the dishonest half of this
    # change; the honest half is only that a loss is not a queue.
    waiting_count = pending - gone
    if waiting_count:
        text = (f"{waiting_count} message{_s(waiting_count)} arrived and not yet read by the "
                f"pipeline. ")
    else:
        text = "Nothing waiting to be read. "

    # A press just happened, and it *is* the last check — so it replaces the standing "Last checked
    # N ago" rather than being announced beside it.
    checked_clause = _checked_clause(checked) if checked else None
    watch = _arrival_watch()
    if checked_clause is not None:
        state = checked_clause
    elif not watch.enabled:
        state = html.muted("The new-mail watch is off, so this only updates when a run happens. ")
    elif watch.last_error:
        state = html.tag("span", f"The watch could not read the mailbox: {watch.last_error} ",
                         class_="err")
    elif watch.last_poll_at:
        state = html.muted(f"Last checked {_ago(watch.last_poll_at)}. ")
    else:
        state = html.muted("Not checked yet. ")

    # After the state, not before: "nothing waiting" is the answer to the question this line is
    # asked, and the loss is the footnote to it rather than the headline.
    lost = (html.muted(f"{gone} message{_s(gone)} "
                       f"{'was' if gone == 1 else 'were'} lost before anything read "
                       f"{'it' if gone == 1 else 'them'} — filter to "
                       f"“not in the mailbox” to see which. ")
            if gone else html.Raw(""))

    return html.tag("p", text, state, lost,
                    # "Check now" is no longer here — it acts on the page, so it lives beside the
                    # page title (`_check_now`). "Turn the watch on" stays, because it only makes
                    # sense next to the sentence saying the watch is off.
                    (html.tag("a", "Turn the watch on", href="/ui/automation",
                              class_="btn ghost small", style="margin-left:4px")
                     if not watch.enabled else html.Raw("")),
                    class_="note")


def _check_now() -> html.Raw:
    """The deep re-read, as a page-level action rather than a link inside a paragraph.

    **A POST, not a link, and the reason is a bug rather than etiquette.** It was
    `GET /ui/mails?refresh=1`: the same page, rendered from a live mailbox read. But `mails_page`
    builds its table *before* it evaluates the note that performs that read, so anything the read
    discovered was written to the database and then rendered into a table assembled a moment
    earlier. Press it, wait half a minute, see nothing new; press it again and the mail appears.

    A POST that redirects fixes it by construction — the GET that follows starts from a database
    that already has the rows — and it takes the URL with it. `?refresh=1` used to stay in the
    address bar, so every later reload of that tab, including the automatic ones, paid for another
    full mailbox walk.

    `data-busy-label` is read by `_JS`: the button disables itself and says what it is doing, for
    what can be twenty seconds of otherwise unexplained silence.
    """
    return html.button_form("/ui/mails/check", "Check now", cls="btn primary small",
                            busy_label="Checking the mailbox…")


def _checked_clause(outcome: str):
    """What the last press of "Check now" did — **a clause, not a paragraph.**

    It replaces the "Last checked …" half of `_unprocessed_note`, because that is exactly what it
    is: the most recent check, reported by the thing that performed it. Rendered as its own line it
    sat directly above a sentence ending "Last checked just now", so the page said the same thing
    twice in two different voices.

    Describes the *mailbox read* only — how much was listed and how much was new here. It does not
    claim anything was processed: a metadata check records that a message exists, and the run is
    what reads it.
    """
    kind, _, detail = outcome.partition(":")
    if kind == "busy":
        return html.muted("A check was already running, so this press did nothing. ")
    if kind == "error":
        # `err`, not the muted grey a failure used to share with every ordinary note. A mailbox we
        # cannot read is the one thing on this page someone has to act on.
        return html.tag("span", f"Could not read the mailbox: {detail} ", class_="err")
    if kind == "ok":
        listed, _, new = detail.partition("/")
        if new == "0":
            return html.muted(f"Checked just now: {listed} listed, nothing new. ")
        return html.muted(f"Checked just now: {new} new of {listed} listed, "
                          f"read by the next run. ")
    return None


def _int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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


def _delivery_marker(row, seen: set, shown: dict) -> html.Raw:
    """A count of the items on this delivery, once per delivery rather than once per line.

    Six rows of PO 906725 are one truck. Before deliveries existed there was nothing on the page
    that said so, and the six read as six separate things to receive — which is exactly how they
    were posted, as six Spitfire receipts.

    **It counts the rows on this page, not the rows on the delivery**, and says so when the two
    differ. The first version counted the delivery and produced `22 items` above a single visible
    line: PO 907249 carries 22, but 21 of them are quantity conflicts that `_READY_CLAUSE` holds
    back, so the badge described a block the reader could not see and made the page look broken.
    `1 of 22 items` is the honest form, and it is also the more useful one — it is the only place
    on this page that says the rest of the delivery is waiting somewhere else.

    Drawn only on the first row of each block, which works because `records_ready` orders by
    delivery. Silent for a one-line delivery with nothing held back: `1 item` is noise. Silent too
    for a row with no delivery, where an unqualified badge would claim a grouping never made.
    """
    delivery_id = row["delivery_id"]
    if not delivery_id or delivery_id in seen:
        return html.Raw("")
    seen.add(delivery_id)

    here = int(shown.get(delivery_id, 0))
    total = int(row["delivery_lines"] or 0)
    held = max(total - here, 0)
    if here < 2 and not held:
        return html.Raw("")

    rung = str(row["delivery_rung"] or "")
    how = {"shipment": "shipment number", "notice": "notification number",
           "pod": "the proof of delivery", "date": "the delivery date"}.get(
               rung, "the message it arrived on")
    if held:
        label = f"{here} of {total} items"
        why = (f"One delivery of {total} item lines, identified by {how}. "
               f"{held} of them are not on this page — held back as a quantity conflict, "
               f"already posted, or failed. Look on Needs a human.")
    else:
        label = f"{here} items"
        why = f"One delivery of {total} item lines, identified by {how}"
    return html.tag("span", label, class_="delivery-tag", title=why)


@router.get("/records", response_class=HTMLResponse)
def records_page(corrected: Optional[int] = Query(None),
                 settled: Optional[int] = Query(None),
                 waived: Optional[int] = Query(None),
                 conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    records = read_views.records_ready(conn)
    # One call, indexed in memory — not a query per row. The record's own `status` column is not
    # shown: every pending record reads "pending" until stages 4-7 run, so it would be 25 identical
    # cells. Where the purchase order actually stands is the question this page could not answer.
    delivery = {p.po_number: p for p in read_views.po_delivery_status(conn)}
    # Which cells disagree with Spitfire, read from the mirror in one pass. No network: verifying
    # every row live costs about 145 seconds, which is not a page render. An unflagged cell means
    # nothing disagrees, not that nothing was checked — Verify is still the live answer.
    flags = po_verify.mismatch_flags(conn, records)
    # What has already been sent to Spitfire, in one pass. Spitfire itself cannot answer this —
    # `ReceiptInProgressUnits` reads 0.0 against an unapproved receipt — so the ledger is the only
    # source, and without it the page would offer Post on a row that is already a receipt.
    posted = {a.record_id: a for a in post_ledger.latest_by_record(conn)}
    # One query, not one per row: which emails carry something that could be a POD.
    pod_emails = _emails_with_a_possible_pod(conn)
    # What the purchase order says about each row's line — gates 5-8, off the same mirror `flags`
    # above already reads, so no extra network and no extra document reads. 0.04s for 385 rows.
    # Three answers, not two: `UNCHECKED` means that order has never been read from Spitfire, which
    # is not the same as the delivery being wrong, and must never render as a refusal.
    line_verdicts = read_views.mirror_line_verdicts(conn, records)
    rows = []
    # Which deliveries have already had their marker drawn. Rows arrive grouped, so the first row
    # of a block gets "N items" and the rest get nothing — a count repeated on every line of a
    # six-line delivery reads as six deliveries of six items each.
    seen_deliveries = set()
    # Counted over the rows this page is actually drawing, so the badge and the block agree.
    shown_per_delivery = Counter(r["delivery_id"] for r in records if r["delivery_id"])
    # Which row of each delivery block carries the Post control. Computed here rather than by
    # reusing `seen_deliveries`, which `_delivery_marker` fills as it draws: the Post cell is the
    # third column and the delivery badge is the fifth, so a set shared between them would be
    # written by whichever ran first and the two would disagree about which row is the first.
    heads_delivery = {}
    for r in records:
        if r["delivery_id"] and r["delivery_id"] not in heads_delivery:
            heads_delivery[r["delivery_id"]] = r["id"]
    # What each delivery block still needs, counted across the block rather than read off its first
    # row. A delivery part-posted before grouping existed — PO 907249's line 2 — leaves that row
    # finished while nineteen neighbours have never been posted, and reading the control off it
    # offered "Post report" and no way to post the nineteen.
    to_post = Counter()
    report_due = set()
    verify_target: Dict[int, int] = {}
    for r in records:
        did = r["delivery_id"]
        if not did:
            continue
        attempt = posted.get(r["id"])
        if attempt is not None and attempt.state == post_ledger.POD_POSTED:
            report_due.add(did)
            # The first line of the block that actually reached a receipt. Aggregated here with
            # `to_post` and `report_due` for the same reason they are: read off the first row
            # instead, and the control describes a different record from the badge beside it.
            verify_target.setdefault(did, int(r["id"]))
        if attempt is not None and attempt.is_blocking:
            continue          # posted, in flight, or half-built: not something to post again
        # The same certain-and-cheap refusals `_post_cell` declines to draw a button for. A button
        # whose only outcome is a refusal teaches people to ignore refusals — and a *count* that
        # disagrees with the buttons under it is worse, because "Post 4 of 6" is read as a promise.
        # Through `can_post_offline` so the badge and the cell cannot drift apart.
        if not post_decision.can_post_offline(
                r, has_pod_bytes=r["source_email_id"] in pod_emails):
            continue
        if completeness.gaps(r).missing_required:
            continue
        # And what the order itself says. Without this the block promised "Post 16 lines" over rows
        # the purchase order had already received in full, or that matched no line at all — the
        # count that read 330 across this page while 56 could post.
        verdict = line_verdicts.get(r["id"])
        if verdict is not None and verdict.state == read_views.BLOCKED:
            continue
        to_post[did] += 1
    for r in records:
        package = f"{_num(r['package_quantity'])} {r['package_uom'] or ''}".strip()
        po = delivery.get(r["po_number"])
        mm = flags.get(r["id"])
        rows.append([
            r["id"],
            # Second and third columns, not last. This table is twenty columns wide and scrolls
            # sideways, so
            # a Verify cell on the far right is a control nobody can reach without already knowing
            # it is there — which is exactly what happened. It reads this row's purchase order back
            # out of Spitfire and shows those quantities against the ones the email stated. The row
            # still opens the delivery bar: the script ignores clicks landing on a button inside a
            # row, so the two do not fight.
            html.verify_button(f"/ui/records/{r['id']}/verify", "Verify",
                               title=f"Check PO {r['po_number']} against Spitfire", small=True),
            # The write. Same control as Verify — a `data-verify` button the one inline script
            # posts — because these pages are pinned to a single script block and a second one
            # would fail `test_ui_html.py`. Its label carries the outcome where there is one, so a
            # posted row reads "Posted" rather than inviting a second attempt that the ledger
            # would only refuse.
            _post_cell(posted.get(r["id"]), r, has_pod=r["source_email_id"] in pod_emails,
                       line_verdict=line_verdicts.get(r["id"]),
                       delivery=(DeliveryCell(
                           delivery_id=int(r["delivery_id"]),
                           is_first=heads_delivery.get(r["delivery_id"]) == r["id"],
                           lines=int(shown_per_delivery.get(r["delivery_id"], 1)),
                           to_post=int(to_post.get(r["delivery_id"], 0)),
                           report_due=r["delivery_id"] in report_due,
                           verify_record_id=verify_target.get(r["delivery_id"]))
                           if r["delivery_id"] else None)),
            # Correct this row by hand. Beside Verify and Post rather than at the far right, for
            # the reason the comment above gives about this table's width — and next to them
            # specifically, so "what may I do to this row" is answered in one glance instead of
            # three places. A plain link, not a `data-verify` button: what a reviewer types has to
            # survive a refusal, and re-rendering a page does that with no script at all.
            _edit_cell(r, posted.get(r["id"])),
            # Whether a receiver line could be built from this row, in front of the row itself.
            # It is the question this page exists to answer, and it used to be the sixth column.
            _gap_badge(r),
            # Two destinations, deliberately distinguishable rather than one link that does a
            # surprising thing: the number goes to the purchase order, the envelope opens the email
            # this line was read from, so the two can be compared.
            html.Raw(str(html.tag("a", r["po_number"], href=f"/ui/po/{r['po_number']}")) + " "
                     + str(html.mail_link(r["source_email_id"], "✉",
                                          title="Open the email this line was read from"))
                     + str(_delivery_marker(r, seen_deliveries, shown_per_delivery))),
            html.badge(po.label, po.status) if po else html.muted("—"),
            _mm(_stamp(r["spec_code"]), mm, "spec"),
            _clipped(r["item_description"], 46),
            _mm(_num(r["quantity_received"]), mm, "qty"),
            _mm(r["unit_of_measure"] or html.muted("—"), mm, "uom"),
            _stamp(r["tracking_number"]),
            r["received_by"] or html.muted("—"),
            r["po_line_number"] if r["po_line_number"] is not None else html.muted("—"),
            package or html.muted("—"), _stamp(r["pod_stated_date"]),
            r["carrier_name"] or html.muted("—"),
            f"{r['extraction_confidence']:.2f}",
            html.origin_badge(r["origin"], r["created_by"]),
            r["extraction_source"],
            # The row itself opens the delivery bar, so the message keeps its own control here
            # rather than being unreachable from this page. It was a plain `<a href>` to the
            # fragment endpoint, which navigated away to an unstyled partial with no way back —
            # and carried no `src`, so it resolved against the corpus and reported itself missing.
            html.mail_link(r["source_email_id"], r["email_subject"][:40] or "(no subject)"),
        ])
    # Which rows still need a person, as a value the filter can match. It cannot be read off the
    # Complete cell: that cell holds a badge, a Fill button and a tooltip, so its rendered text is
    # "3 gaps Fill" rather than anything a dropdown could name.
    complete_flags = [completeness.gaps(r).is_complete for r in records]
    choice_values = ["complete" if flag else "gaps" for flag in complete_flags]
    views = []
    if not all(complete_flags):
        views.append(("gaps", "My triage queue"))
    if any(complete_flags):
        views.append(("complete", "Complete"))

    body = html.section(
        "",
        # A save that redirects here used to arrive silently, so the only confirmation was finding
        # the row again in a table of twenty-five. `?corrected=` was already being set and read by
        # nothing.
        html.banner(f"Record #{corrected} saved.", kind="good") if corrected else "",
        # Same defect, same fix, twice more. `?waived=` has been set by `waive_pod` since it was
        # written and read by nothing, so accepting a delivery with no proof returned somebody to a
        # table of twenty-five rows with no sign it had worked.
        html.banner(f"The proof for delivery #{settled} is settled — its lines are ready to post.",
                    kind="good") if settled else "",
        html.banner(f"Record #{waived} accepted without a proof of delivery.",
                    kind="good") if waived else "",
        html.tag(
            "div",
            html.search_box("records-table",
                            # Short enough to survive the field, which is a third of the width it
                            # was. The hint is not a contract: the filter matches the whole row and
                            # every `title` on it, carrier and tracking among them.
                            placeholder="Search PO, spec, description…",
                            label="Search records"),
            html.date_filter("records-table", label="POD date", presets=True),
            html.choice_filter("records-table", views, label="View",
                               all_label="All records", boxed=True),
            class_="controls",
        ),
        html.table(
            ["#", "Verify", "Post", "Edit", "Complete", "PO", "Status", "Spec", "Description",
             "Qty", "Unit", "Tracking", "Received by", "Line", "Package", "POD date", "Carrier",
             "Conf", "Origin", "Source", "From email"],
            rows, empty=_EMPTY_HINT, table_id="records-table", page_size=25, pane=True,
            date_column="POD date", no_sort=("Verify", "Post", "Edit"),
            num_columns=("Qty", "Conf"),
            # The tick is what makes "Verify all against Spitfire" mean "verify these five".
            select_ids=[r["id"] for r in records], select_noun="record",
            choice_values=choice_values,
            frag_urls=[f"/ui/po/{quote(r['po_number'])}/bar" for r in records],
            frag_title="Show this delivery's progress",
            # One unit per delivery rather than per row: searching a purchase order returns its
            # whole block, and a page boundary cannot fall between an item and the delivery it
            # arrived on. A line with no delivery yet is its own group, so it is never swept into
            # a neighbouring one.
            group_keys=[str(r["delivery_id"] or f"row-{r['id']}") for r in records],
        ),
    )
    # The Receiver report sheet used to sit under this table, with its own search, its own pager and
    # its own .xlsx download. It was the same report over the same data as `/ui/report` — see that
    # route's docstring, which said one of the two should go and that the choice was Premier's.
    # Removed here on 2026-08-22 by that decision; the sidebar entry is the one that survived.
    return html.page(
        "Records", "/ui/records", body,
        subtitle="Pending records carrying a PO, non-zero confidence and no quantity conflict. "
                 "Verify reads the purchase order from Spitfire.",
        actions=[
            _export_records(),
            html.verify_button(
                "/ui/records/verify", "Verify all against Spitfire", primary=True,
                selection="records-table",
                title="Read every listed purchase order from Spitfire and compare quantities"),
        ],
        **_chrome(conn))


def _export_records() -> html.Raw:
    """Every listed record, as a file. See `_export_mail`."""
    return html.tag("a", html.DOWNLOAD_ICON, "Export", href="/ui/records.csv", class_="btn small",
                    title="Download every row of this table as CSV")


_RECORDS_CSV_COLUMNS = ("#", "Complete", "PO", "Status", "Spec", "Description", "Qty", "Unit",
                        "Tracking", "Received by", "Line", "Package", "POD date", "Carrier",
                        "Conf", "Origin", "Created by", "Source", "From email", "Posted")


@router.get("/records.csv")
def records_csv(conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> Response:
    """The Records table as a file, whole and untruncated — see `mails_csv`.

    Complete is the gap count rather than a badge, and Posted says what the ledger holds, which is
    the two on-screen columns a file cannot carry as controls.
    """
    records = read_views.records_ready(conn)
    delivery = {p.po_number: p for p in read_views.po_delivery_status(conn)}
    posted = {a.record_id: a for a in post_ledger.latest_by_record(conn)}
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(_RECORDS_CSV_COLUMNS)
    for r in records:
        row_gaps = completeness.gaps(r)
        po = delivery.get(r["po_number"])
        sent = posted.get(r["id"])
        writer.writerow(_csv_safe([
            r["id"],
            "complete" if row_gaps.is_complete else f"{row_gaps.count} gaps: {row_gaps.describe()}",
            r["po_number"], po.label if po else "", r["spec_code"], r["item_description"],
            _num(r["quantity_received"]), r["unit_of_measure"], r["tracking_number"],
            r["received_by"],
            "" if r["po_line_number"] is None else r["po_line_number"],
            f"{_num(r['package_quantity'])} {r['package_uom'] or ''}".strip(),
            r["pod_stated_date"], r["carrier_name"], f"{r['extraction_confidence']:.2f}",
            r["origin"], r["created_by"], r["extraction_source"], r["email_subject"],
            # The ledger's own word for where the attempt got to — `posted`, `blocked`, `pending`.
            # It is the only source: Spitfire reads 0.0 against an unapproved receipt.
            sent.state if sent else "",
        ]))
    return Response(
        buffer.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="premier-records.csv"',
                 "X-Content-Type-Options": "nosniff"},
    )


# --- Verify against Spitfire ------------------------------------------------------------------
#
# POST, and deliberately so. These read the purchase order out of Spitfire and refresh the local
# mirror with what comes back, which is an effect — a GET would let a prefetch or a crawler start
# fifty ERP reads nobody asked for. The reply is an HTML fragment for the shared dialog, the same
# contract `/ui/po/{po_number}/bar` uses.


@router.post("/records/{record_id}/verify", response_class=HTMLResponse)
def verify_record_fragment(record_id: int, line: Optional[int] = None,
                           conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    """One record's comparison. `line` is a reviewer overriding which PO line to compare against,
    posted by the alternatives table inside the popup — the same endpoint, so the dialog refills
    in place rather than the reader losing the record they were reading."""
    row = _fixable_record_or_none(conn, record_id)
    if row is None:
        return str(html.tag("p", "No such record.", class_="empty"))
    rows = [row]
    result = po_verify.verify_records(conn, rows, chosen_line=line)[0]

    # Keep what it worked out. Verifying used to resolve the line, render it, and forget it, so a
    # record could be checked against line 0001 all day and still be refused for having no line.
    # `apply_verification` writes only an exact spec match whose quantity agrees, and only into an
    # empty field — a reviewer's own choice, and a description-only guess, are both left alone.
    kept = record_completion.apply_verification(conn, rows[0], result)
    if not kept.ok:
        return str(_verification(result))

    fresh = [r for r in (_fixable_record_or_none(conn, record_id),) if r is not None]
    banner = html.banner(f"{kept.applied[0]} — {kept.message}", kind="good")
    if not fresh:
        return str(banner) + str(_verification(result))
    return str(banner) + str(_verification(
        po_verify.verify_records(conn, fresh, chosen_line=line)[0]))


@router.post("/records/{record_id}/complete", response_class=HTMLResponse)
def complete_record_fragment(record_id: int, line: Optional[int] = None,
                             conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    """Fill this record's gaps from its own proof of delivery, then show the comparison.

    Writes to our SQLite and to nothing else — `pipeline/record_completion.py` reads the delivery
    date and the signature off the attached POD rather than taking them from a form, so the record
    and the file posted beside it cannot disagree. `line` is the reviewer's choice, the one field
    a delivery note cannot supply.

    A POST for the same reason Verify is: it changes stored data, and a GET would let a prefetch
    do it. The reply is the verification fragment so the dialog shows the effect immediately.
    """
    row = _fixable_record_or_none(conn, record_id)
    if row is None:
        return str(html.tag("p", "No such record.", class_="empty"))

    outcome = record_completion.complete(conn, row, line=line)

    parts = [html.tag("h3", "Completed from the POD" if outcome.ok else "Not completed"),
             html.tag("p", outcome.message, class_="" if outcome.ok else "warn")]
    if outcome.applied:
        parts.append(html.tag("ul", *[html.tag("li", a) for a in outcome.applied]))

    if outcome.ok:
        # Re-read: the row in hand is stale the moment `complete` writes, and the comparison below
        # has to be built from what is now stored — including, in the good case, a record that has
        # just become complete and moved to the Records page.
        fresh = [r for r in (_fixable_record_or_none(conn, record_id),) if r is not None]
        if fresh:
            parts.append(html.Raw(str(_verification(
                po_verify.verify_records(conn, fresh, chosen_line=line)[0]))))
    return "".join(str(p) for p in parts)


# --- Post to Spitfire -------------------------------------------------------------------------
#
# THE ONLY ROUTE IN THIS FILE THAT WRITES TO PREMIER'S ERP. Everything else here reads.
# `tests/test_operations_readonly.py` scans this module for write verbs and exempts this handler
# by name; that exemption is the reviewable act, and widening it would void the guarantee for the
# whole file. See `connectors/spitfire_write.py` for what may be issued and what is refused.
#
# It creates a receipt, hangs the POD and the receiver report on it, and links it to the purchase
# order. It does **not** route: creating the receipt already stages three real Premier employees,
# and dispatching to them is Premier's decision, not a button's.


def _record_or_none(conn, record_id: int):
    """A record the *write* stages may act on — one the Records page is currently offering.

    Still `records_ready`, and now stronger for it: that list means *postable*, so a record with a
    gap cannot be reached by the Post button at all rather than reaching `post_decision` and being
    refused at gate 1. The fixable-but-not-postable case has its own lookup,
    `_fixable_record_or_none` — the two must not be swapped, and the difference is exactly the one
    a reviewer feels: Fill works on an incomplete record, Post does not.
    """
    rows = [r for r in read_views.records_ready(conn) if r["id"] == record_id]
    return rows[0] if rows else None


def _edit_block_reason(attempt) -> str:
    """Why this record may no longer be corrected by hand, or `""` if it may.

    Read off the **ledger**, not off `status`. `spitfire_post._mark_pushed` moves a record to
    `pushed_to_spitfire` only when every step landed — receipt, POD, report, read-back — so a row
    carrying a real receipt with the receiver report still outstanding is `pending`, is on the
    Records page, and passes `records_fixable`. Judging this by status therefore offered a
    correction on a delivery Premier's ERP already holds a document for.

    `FLAGGED` is deliberately not a reason. A refusal is the thing an edit exists to fix, and
    blocking it would close the only recovery path the record has.
    """
    if attempt is None or not attempt.is_blocking:
        return ""
    if attempt.state == post_ledger.POD_POSTED:
        named = attempt.receipt_doc_no or (attempt.receipt_key or "")[:8]
        return (f"Receipt {named or 'for this delivery'} is already in Spitfire carrying this "
                f"delivery's proof. Correcting the record now would make it disagree with a "
                f"document Premier holds. Only the receiver report is still to send.")
    if attempt.state == post_ledger.PARTIAL:
        return ("A half-built receipt for this row exists in Spitfire. It needs a person before "
                "anything else about this record is changed.")
    if attempt.state == post_ledger.CLAIMED:
        since = (attempt.claimed_at or "")[:10]
        return (f"A post claimed this row{f' on {since}' if since else ''} and has not settled. "
                f"Editing while a write is in flight would change what is being sent.")
    return (f"This record has been posted to Spitfire ({attempt.state}) and is no longer "
            f"something a correction can change.")


def _edit_cell(row, attempt) -> html.Raw:
    """Correct this record by hand, or say why it can no longer be corrected.

    A `<button aria-disabled>` rather than a `<span>` for the blocked case, and the choice is not
    cosmetic. The row-click handler in `html._JS` bails on any click landing inside
    `a,button,input,label`; a span is in none of those, so clicking a *dead* control would open the
    delivery fragment dialog, and widening that selector would break the test that pins it.
    `aria-disabled` rather than `disabled` because a disabled control is not focusable and its
    `title` is suppressed — and here the tooltip is the entire message.
    """
    reason = _edit_block_reason(attempt)
    if not reason:
        return html.tag("a", "Edit", href=f"/ui/records/{row['id']}/edit?from=records",
                        class_="btn small",
                        title="Correct this record's fields. Saved here, never sent to Spitfire.")
    return html.tag("button", "Edit", type="button", class_="btn small",
                    aria_disabled="true", title=reason)


def _fixable_record_or_none(conn, record_id: int):
    """A record a person may still work on — complete or not.

    Deliberately **not** `records_ready`. That list now means "postable", so the moment
    completeness became part of it, resolving Verify and Fill through it made both controls
    unreachable for exactly the records that need them: a record with a gap would answer
    "No such record" to the one button that closes gaps.

    This is the second time that shape has bitten. `_any_record_or_none` below exists because
    reading Verify POD through `records_ready` made it unreachable for every record it applied to,
    once posting moved a record out of that view. Same trap, different filter.

    Scoped to `records_fixable` rather than the whole table: a posted record is not fixable, and
    editing one would change what a receipt already sent to Premier says it received. Wider than
    `records_ready` in the other direction — a quantity conflict and a zero confidence are reasons
    a record *needs* correcting, not reasons it cannot be.
    """
    rows = [r for r in read_views.records_fixable(conn) if r["id"] == record_id]
    return rows[0] if rows else None


def _editable_record_or_none(conn, record_id: int):
    """A record the edit form may open, and why not when it may not: `(row, reason)`.

    Narrower than `_fixable_record_or_none`, and only for the edit form. Verify and Fill go on
    working on a record the ledger blocks — Verify writes nothing a receipt could contradict, and
    Fill takes its values off the proof of delivery rather than from a person — so tightening the
    shared helper would have withdrawn two controls that were doing no harm.
    """
    row = _fixable_record_or_none(conn, record_id)
    if row is None:
        return None, ""
    reason = _edit_block_reason(post_ledger.latest_for_record(conn, record_id))
    return (None, reason) if reason else (row, "")


def _any_record_or_none(conn, record_id: int):
    """Any record, posted or not.

    `records_ready` deliberately excludes a record once it has posted — `_mark_pushed` moves it to
    `pushed_to_spitfire` — which is right for the write stages and wrong for verification: the only
    records worth verifying are exactly the ones that have left that view. Reading it through
    `records_ready` made Verify POD unreachable for every record it applied to.
    """
    prior = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM extracted_records WHERE id = ?", (record_id,)).fetchone()
    finally:
        conn.row_factory = prior


def _write_refused() -> Optional[str]:
    """The refusals that apply to *every* write to Premier's ERP, or None to go ahead.

    Shared by the per-record and the per-delivery routes rather than copied into each. These are
    not per-route policy — they are the reason a stop is a stop — and a second copy is how one of
    them quietly stops applying to the newer path.
    """
    if killswitch.is_stopped():
        return str(_post_outcome_fragment(
            ok=False, heading="Stopped",
            message="the kill switch is engaged — release it before posting to Spitfire."))

    # Refused here rather than deeper down, and deliberately before `post_ledger.claim` ever runs.
    # `connectors/spitfire_cassette` raises on the write itself as a backstop, but that raise would
    # land inside `post_pod`'s try block — after the claim — leaving a `CLAIMED` row, which the
    # ledger reads as "a receipt may exist on training somewhere". Nothing was sent and nothing
    # should be recorded as if it might have been.
    if spitfire_cassette.writes_refused():
        return str(_post_outcome_fragment(
            ok=False, heading="Offline",
            message=("Spitfire can only be written to from Premier's office network, and this "
                     "session is replaying recorded responses. Verifying and reading still work; "
                     "posting does not.")))
    return None


def _run_locked(action, headings, *args):
    """Run one write with the single-writer lock held, and render its outcome.

    A second caller is turned away rather than queued, so a double-submit is a no-op rather than two
    receipts. The ledger is the durable half of the same guard, for a restart between clicks.
    """
    if not runner._LOCK.acquire(blocking=False):
        return str(_post_outcome_fragment(
            ok=False, heading="Busy",
            message="the automation is mid-run — wait for it to finish and try again."))
    try:
        result = action(*args)
    finally:
        runner._LOCK.release()

    heading = headings.get(result.state, "Posted" if result.ok else "Failed")
    return str(_post_outcome_fragment(ok=result.ok, heading=heading, message=result.message,
                                      result=result))


def _write_guarded(conn, record_id: int, action, headings):
    """Run one Spitfire write for one record, behind the guards every write here keeps."""
    refusal = _write_refused()
    if refusal is not None:
        return refusal

    row = _record_or_none(conn, record_id)
    if row is None:
        return str(html.tag("p", "No such record.", class_="empty"))

    return _run_locked(action, headings, conn, row)


def _delivery_write_guarded(conn, delivery_id: int, action, headings):
    """Run one Spitfire write for one whole delivery, behind the same guards.

    Deliberately the same three, in the same order, reached through the same two helpers: the kill
    switch, the offline refusal, then the lock. This is the route that creates a receipt carrying
    twenty item lines, so it is the last place a guard should be re-implemented slightly differently.
    """
    refusal = _write_refused()
    if refusal is not None:
        return refusal

    if deliveries_store.get(conn, delivery_id) is None:
        return str(html.tag("p", "No such delivery.", class_="empty"))

    return _run_locked(action, headings, conn, delivery_id)


_POST_HEADINGS = {"flagged": "Not posted", "session_expired": "Session expired",
                  "partial": "Partly posted — needs a person",
                  post_ledger.POD_POSTED: "Proof of delivery posted"}


@router.post("/records/{record_id}/post-pod/confirm", response_class=HTMLResponse)
def post_pod_confirm_fragment(record_id: int,
                              conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    """Say what is about to be created, then offer the button that creates it.

    A POST that only reads. The dialog script always POSTs (see `html.verify_button`), and this
    codebase already has the shape — `spitfire_write.find_documents` is documented as "a read,
    despite being a POST".

    The confirmation exists because this is the one control in the application that writes to
    Premier's ERP, and it used to fire on the first click with nothing said first.
    """
    row = _record_or_none(conn, record_id)
    if row is None:
        return str(html.tag("p", "No such record.", class_="empty"))
    quantity = f"{row['quantity_received']} {row['unit_of_measure'] or ''}".strip()
    return str(html.Raw("".join(str(p) for p in [
        html.tag("h3", f"Post the proof of delivery for PO {row['po_number']}?"),
        html.tag("p", html.Raw(
            f"This creates a receipt on purchase order <strong>{_escape(row['po_number'])}</strong> "
            f"for <strong>{_escape(quantity)}</strong> of "
            f"<strong>{_escape(row['spec_code'] or '—')}</strong>, uploads the proof of delivery, "
            f"checks the catalog's hash against ours, and attaches it.")),
        html.tag("p", html.Raw(
            "The receiver report is <strong>not</strong> posted by this step — it becomes a "
            "separate button once the proof of delivery is on the receipt.")),
        html.tag("p", "Nothing is routed. The receipt is left In Process for a person to approve.",
                 class_="sub"),
        html.verify_button(f"/ui/records/{record_id}/post-pod", "Post POD to Spitfire",
                           title="Create the receipt and attach the proof of delivery"),
    ])))


@router.post("/records/{record_id}/post-pod", response_class=HTMLResponse)
def post_pod_fragment(record_id: int,
                      conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    """Create the receipt and put the POD on it.

    Synchronous, and for several seconds — five calls and an upload. A background job would need
    polling, polling needs script, and `tests/test_ui_html.py` pins these pages to exactly one
    inline block. One person posting one record at a time can wait.
    """
    return _write_guarded(conn, record_id, spitfire_post.post_pod, _POST_HEADINGS)


@router.post("/records/{record_id}/post-report/confirm", response_class=HTMLResponse)
def post_report_confirm_fragment(record_id: int,
                                 conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    row = _record_or_none(conn, record_id)
    if row is None:
        return str(html.tag("p", "No such record.", class_="empty"))
    attempt = spitfire_post._awaiting_report_attempt(conn, record_id)
    if attempt is None:
        return str(_post_outcome_fragment(
            ok=False, heading="Nothing to add a report to",
            message=("the proof of delivery has not been posted for this record — post it first, "
                     "and the report becomes available on the receipt it creates.")))
    where = attempt.receipt_doc_no or attempt.receipt_key[:8]
    return str(html.Raw("".join(str(p) for p in [
        html.tag("h3", f"Post the receiver report onto receipt {where}?"),
        html.tag("p", html.Raw(
            f"The proof of delivery is already on this receipt. This builds the receiver report "
            f"for PO <strong>{_escape(row['po_number'])}</strong>, uploads it, attaches it beside "
            f"the POD, links the purchase order and any pay requests, and reads the receipt back "
            f"to confirm both files are on it.")),
        html.tag("p", "Nothing is routed. The receipt stays In Process.", class_="sub"),
        html.verify_button(f"/ui/records/{record_id}/post-report", "Post report to Spitfire",
                           title="Build and attach the receiver report"),
    ])))


@router.post("/records/{record_id}/post-report", response_class=HTMLResponse)
def post_report_fragment(record_id: int,
                         conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    return _write_guarded(conn, record_id, spitfire_post.post_report, _POST_HEADINGS)


@router.post("/records/{record_id}/verify-pod", response_class=HTMLResponse)
def verify_pod_fragment(record_id: int,
                        conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    """Re-read the POD from Spitfire and re-compare its hash. Reads only, so no kill switch and no
    lock: refusing to *check* what was written while a run is in progress would be perverse."""
    row = _any_record_or_none(conn, record_id)
    if row is None:
        # The ledger row outlives its record: `tools/reprocess_mail` erases extracted records so
        # mail can be read again, and deliberately leaves the posting history alone. So a receipt
        # can be listed here with nothing left locally to compare against — which is a different
        # thing from a bad link, and saying "no such record" alone sent people looking for one.
        return str(_post_outcome_fragment(
            ok=False, heading="Cannot verify",
            message=("this receipt's record is no longer in our store — it was erased when the "
                     "mail was reprocessed. The receipt still exists in Spitfire; there is simply "
                     "no local copy of the POD left to compare its hash against.")))
    result = spitfire_post.verify_pod(conn, row)
    heading = {"verified": "Proof of delivery verified",
               "flagged": "Not verified"}.get(result.state, "Could not verify")
    return str(_post_outcome_fragment(ok=result.ok, heading=heading, message=result.message,
                                      result=result))


def _by_receipt(attempts) -> list:
    """Ledger rows collapsed into the receipts they describe, newest first.

    The ledger keeps one row per item line — thirty readers key on `record_id`, and a mixed group
    (seventeen posted, three flagged) is only expressible at that grain. A person reading these
    sections is looking for a *document*, so the collapse happens here, in the presentation, for the
    same reason `post_ledger.blocked()` leaves its grouping to the page.

    A row with no `receipt_key` is its own group: it describes an attempt that never became a
    document, and merging those together would invent a receipt that does not exist.
    """
    groups: "OrderedDict[str, list]" = OrderedDict()
    for attempt in attempts:
        key = attempt.receipt_key or f"no-receipt-{attempt.id}"
        groups.setdefault(key, []).append(attempt)
    return list(groups.values())


def _lines_cell(group) -> html.Raw:
    """How many item lines one receipt carries, and which records they came from."""
    records = ", ".join(str(a.record_id) for a in group[:8])
    if len(group) > 8:
        records += f", +{len(group) - 8} more"
    return html.tag("span", f"{len(group)} line{'s' if len(group) != 1 else ''}",
                    title=f"record{'s' if len(group) != 1 else ''} {records}")


def _posted_receipts(conn: sqlite3.Connection) -> list:
    """Everything that reached Spitfire, each with the control that proves it is still there.

    It has to live here rather than on the Records page, and that is not a layout preference: a
    posted record leaves `records_ready` the moment `_mark_pushed` runs, so the Records page cannot
    show it at all. Without this section the verification had nowhere to be clicked from — which is
    the state the feature was in until a live check caught it.
    """
    done = [a for a in post_ledger.latest_by_record(conn) if a.state == post_ledger.POSTED]
    if not done:
        return []
    # One row per **receipt**, not per record. A delivery of twenty item lines is one receipt with
    # twenty ledger rows, and listing them all would draw the same receipt number twenty times and
    # bury every other receipt under it. The grain of this table is what a person went to Spitfire
    # to look at, which is a document.
    receipts = _by_receipt(done)
    rows = [[html.badge(group[0].receipt_doc_no or group[0].receipt_key[:8], "good"),
             group[0].po_number,
             html.when((group[0].settled_at or group[0].claimed_at or "")[:16]),
             _lines_cell(group),
             html.verify_button(f"/ui/records/{group[0].record_id}/verify-pod", "Verify POD",
                                title="Re-read the POD from Spitfire and re-check its hash",
                                small=True, ghost=True)] for group in receipts]
    return [html.section(
        f"Posted to Spitfire ({len(receipts)})",
        html.table(["Receipt", "Purchase order", "Posted", "Lines", "Proof"], rows,
                   empty="Nothing has been posted.", table_id="posted-receipts",
                   page_size=25, date_column="Posted"),
        note=("Verify POD re-reads the receipt from Spitfire and re-compares the catalog's hash "
              "against the file we sent — it answers both 'is it still attached' and 'are the "
              "bytes still ours', which a successful upload never proved on its own. "
              "Each receipt is left In Process until a person approves it."),
    )]


def _awaiting_report(conn: sqlite3.Connection) -> list:
    """Receipts that carry a proof of delivery and no receiver report.

    A state the atomic post could not produce and the split can: somebody posted the POD and has
    not posted the report. Not a fault — but a real document sitting in Premier's ERP, half built,
    and the difference between "normal resting state" and "forgotten" is only whether anyone is
    told. So it is listed, for the same reason refusals are.
    """
    waiting = post_ledger.awaiting_report(conn)
    if not waiting:
        return []
    # One row per receipt — see `_posted_receipts`. Twenty rows reading "receipt 0007" would make
    # one unfinished document look like twenty.
    receipts = _by_receipt(waiting)
    rows = [[html.badge(group[0].receipt_doc_no or group[0].receipt_key[:8], "warn"),
             group[0].po_number,
             _lines_cell(group),
             html.when((group[0].settled_at or group[0].claimed_at or "")[:16])]
            for group in receipts]
    return [html.section(
        f"Proof of delivery posted, report outstanding ({len(receipts)})",
        html.table(["Receipt", "Purchase order", "Lines", "POD posted"], rows,
                   empty="Nothing is waiting.", table_id="awaiting-report",
                   page_size=25, date_column="POD posted"),
        note=("The receipt exists in Spitfire with the proof of delivery attached. Press "
              "Post report on the record to finish it — the receipt stays In Process either way, "
              "so nothing is routed and no one is emailed."),
    )]


def _stranded_posts(conn: sqlite3.Connection) -> list:
    """Claims whose chain died mid-flight, and which nothing else in this product shows.

    `CLAIMED` is not a resting state — a row still at it means the process was killed between
    reserving the work and recording what happened, and Spitfire may be holding a receipt our
    records do not know is finished. Record 234 on PO 912560 sat like that from 26 August: the
    receipt, the POD upload and the attach had all succeeded, and because `CLAIMED` is in
    `BLOCKING`, the Post button was not drawn, "Post report" and "Verify POD" both answered that
    nothing had been posted, and posting again said "still in flight" about a process that had
    been dead for a day.

    It appeared in no queue. `blocked()` lists only `FLAGGED` and `awaiting_report()` only
    `POD_POSTED`, so the sole trace anywhere was the word "Posting…" in one cell of one table.
    A half-finished write to Premier's ERP is the last thing that should be discoverable only by
    noticing it.
    """
    stranded = post_ledger.stranded(conn)
    if not stranded:
        return []

    rows = []
    for attempt in stranded:
        rows.append([
            html.Raw(f"#{attempt.record_id}"),
            attempt.po_number or html.muted("—"),
            attempt.receipt_doc_no or (html.muted("not created")
                                       if not attempt.receipt_key else attempt.receipt_key[:8]),
            html.when((attempt.claimed_at or "")[:16]),
            ("a receipt exists and may be unfinished" if attempt.receipt_key
             else "nothing was created before it died"),
        ])
    return [html.section(
        f"Posts that died mid-flight ({len(stranded)})",
        html.table(["Record", "PO", "Receipt", "Claimed", "What may be outstanding"],
                   rows, empty="Nothing is stranded.", table_id="stranded-posting",
                   page_size=25, date_column="Claimed"),
        note=("These are stuck: the record shows “Posting…” and every way out of it refuses, "
              "because the state that blocks a duplicate post also blocks the repair. Run "
              "`python -m tools.settle_stranded_posts --dry-run` — it reads each receipt back "
              "from Spitfire and settles the row as whatever is actually on it."),
    )]


def _blocked_from_posting(conn: sqlite3.Connection) -> list:
    """Records the gate refused, grouped by the reason it gave.

    Grouped rather than listed because the shape of the answer is lopsided and a flat list hides
    it: `received_by` is missing on 27 of 29 live records, and twenty-seven identical sentences
    read as twenty-seven separate problems when they are one data question for Premier. A count
    against a reason says which single thing to fix first.

    Reasons that carry figures — "the email says 18 EA and the purchase order says 19 EA" — are
    each their own group, correctly: those are genuinely separate problems and each needs its own
    look.
    """
    blocked = post_ledger.blocked(conn)
    if not blocked:
        return []

    by_reason: "OrderedDict[str, list]" = OrderedDict()
    for attempt in sorted(blocked, key=lambda a: a.detail):
        by_reason.setdefault(attempt.detail or "no reason recorded", []).append(attempt)

    rows = []
    for reason, attempts in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        pos = sorted({a.po_number for a in attempts if a.po_number})
        rows.append([
            html.badge(str(len(attempts)), "hold"),
            reason,
            ", ".join(pos[:6]) + ("…" if len(pos) > 6 else "") or html.muted("—"),
            html.when((attempts[0].last_attempt_at or attempts[0].claimed_at or "")[:16]),
        ])
    return [html.section(
        f"Blocked from posting ({len(blocked)})",
        html.table(["Records", "Why the post was refused", "Purchase orders", "Last tried"],
                   rows, empty="Nothing is blocked.", table_id="blocked-posting",
                   page_size=25, date_column="Last tried"),
        note=("Nothing was sent to Spitfire for these. The Post button stays live on the Records "
              "page — fix what the reason names and press it again; there is nothing to clear "
              "here. A record that has since posted drops off this list on its own."),
    )]


def _emails_with_a_possible_pod(conn: sqlite3.Connection) -> set:
    """Delegates to `read_views`, which owns this query so `records_postable` can ask it too.

    Kept as a name here because the page reads better for it, and because a second copy of the
    query was exactly how the Post button and the posting gate came to disagree.
    """
    return read_views.emails_with_a_possible_pod(conn)


def _post_status(attempt, record, *, has_pod: bool = True, line_verdict=None) -> html.Raw:
    """What happened to one item line, with no control on it.

    Drawn on every row of a delivery block except the first. The state is still per row — each item
    line is its own row on the receipt and can be flagged on its own while its neighbours post — but
    the *control* belongs to the block, because the receipt does.
    """
    if attempt is not None and attempt.state == post_ledger.POSTED:
        return html.muted(f"Posted {attempt.receipt_doc_no}".rstrip())
    if attempt is not None and attempt.state == post_ledger.POD_POSTED:
        where = attempt.receipt_doc_no or attempt.receipt_key[:8]
        return html.Raw(str(html.muted(f"on receipt {where}".rstrip())) + " "
                        + str(html.badge("report pending", "warn")))
    if attempt is not None and attempt.state == post_ledger.PARTIAL:
        return html.badge("Partial", "warn")
    if attempt is not None and attempt.state == post_ledger.CLAIMED:
        since = (attempt.claimed_at or "")[:10]
        return html.muted(f"Posting… (since {since})" if since else "Posting…")
    if attempt is not None and attempt.state == post_ledger.FLAGGED:
        # The reason rides on the badge itself rather than on an empty element beside it: an
        # invisible span holding the only explanation is unreachable by mouse and by screen reader.
        # Acting on it means posting the delivery, and that button is on the first row of the block.
        again = f" · tried {attempt.attempts}×" if attempt.attempts > 1 else ""
        return html.tag("span", html.badge("blocked", "hold"),
                        title=f"Last refused: {attempt.detail}{again}")

    # The order's own verdict, from the mirror, asked first and for the same reason the gate-2
    # comment below gives: a different answer here would have the block's first row offering Post
    # while its siblings said the line was already received.
    if line_verdict is not None and line_verdict.state == read_views.BLOCKED:
        return html.Raw(str(_clipped(line_verdict.reason, 44)))

    # The same predicate `_post_cell` uses. These are the *other* rows of a delivery block, so a
    # different answer here would have the first row offering Post while its siblings said "no POD"
    # about the same delivery.
    if not post_decision.can_post_offline(record, has_pod_bytes=has_pod):
        # Pointed at the delivery, not the record, wherever the row belongs to one. A delivery is
        # received as one receipt, so its proof is one decision; sending a reviewer to the
        # per-record page from a block of thirty-two rows would ask for thirty-two signatures, and
        # the reflex press is what the signed page exists to prevent. The per-record page stays for
        # records that belong to no delivery, which is what it was built for.
        delivery_id = _maybe(record, "delivery_id")
        where = (f"/ui/deliveries/{int(delivery_id)}/proof" if delivery_id
                 else f"/ui/records/{record['id']}/waive-pod")
        return html.Raw(
            str(html.muted("no POD")) + " "
            + str(html.tag("a", "Accept anyway", class_="btn ghost small", href=where,
                           title="Pick the file that proves this delivery, or record that there "
                                 "is none and it may be posted without one")))
    gaps = completeness.gaps(record)
    if gaps.missing_required:
        return html.muted(f"{len(gaps.missing_required)} gaps")
    return html.muted("with delivery")


def _delivery_head_cell(delivery, attempt, record, *, has_pod: bool, offline: bool,
                        verify, line_verdict=None) -> html.Raw:
    """The controls for a whole delivery block, drawn on its first row.

    **Aggregated over the block, never read off the row that happens to come first.** That row may
    itself be finished while its neighbours have never been posted at all — which is not a corner
    case but the ordinary state of every delivery that was part-posted before grouping existed. PO
    907249 is the live example: line 2 was posted on its own, so the head row sat at `POD_POSTED`
    and the cell offered only "Post report", leaving the other nineteen lines of the same truck with
    no way to be posted at all. A dead end, and one that only appears on exactly the deliveries this
    feature was built to rescue.

    So the two questions are asked of the delivery: is anything still waiting to be posted, and does
    a receipt of this delivery still owe its report. Both can be true at once, and then both buttons
    are drawn — they act on two different receipts, which is correct: a line held back is never
    added to a receipt that already exists.
    """
    # What happened to *this* line, when the controls do not already say it. `POD_POSTED` is
    # omitted because the report button and its badge convey exactly that.
    own = ""
    if attempt is not None and attempt.state in (post_ledger.POSTED, post_ledger.PARTIAL,
                                                 post_ledger.CLAIMED):
        own = str(_post_status(attempt, record, has_pod=has_pod, line_verdict=line_verdict))

    if offline:
        return html.Raw(" ".join(part for part in (str(html.muted("offline")), own) if part))

    controls = []
    if delivery.to_post:
        controls.append(str(html.verify_button(
            f"/ui/deliveries/{delivery.delivery_id}/post-pod/confirm",
            f"Post {delivery.to_post} line{'s' if delivery.to_post != 1 else ''}",
            title=(f"Create one receipt on PO {record['po_number']} carrying the "
                   f"{delivery.to_post} line(s) of this delivery that are ready to post"),
            small=True, ghost=True)))
    if delivery.report_due:
        controls.append(str(html.verify_button(
            f"/ui/deliveries/{delivery.delivery_id}/post-report/confirm", "Post report",
            title="Add the receiver report to this delivery's receipt", small=True, ghost=True)))
        controls.append(str(verify))
        controls.append(str(html.badge("report pending", "warn")))

    if not controls:
        # Nothing to offer. Fall back to the per-row account — "no POD · Accept anyway", "N gaps" —
        # so the head row still says *why* its block has no button.
        return _post_status(attempt, record, has_pod=has_pod,
                            line_verdict=line_verdict)
    return html.Raw(" ".join(part for part in controls + [own] if part))


@dataclass
class DeliveryCell:
    """Where a row sits in its delivery block, for the Post column.

    A delivery is received as one receipt, so it gets **one** Post control — drawn on the first row
    of the block, the same place `_delivery_marker` draws its count. Every other row of the block
    shows what happened and offers no button, because a per-row button is how twenty item lines
    became twenty receipts.

    `to_post` and `report_due` are counted over the **whole block**, never read off the row that
    happens to come first — see `_delivery_head_cell` for what that cost.
    """
    delivery_id: int
    is_first: bool
    lines: int
    """How many rows of this delivery are on this page. What the delivery-count badge says."""

    to_post: int = 0
    """How many of those could plausibly post now — nothing blocking them, a POD or a waiver, and
    no missing required field. The number the button offers. Not how many will *land*: that needs
    the live purchase-order read the confirm dialog pays for, and the dialog is where the
    "16 of 20" breakdown belongs."""

    report_due: bool = False
    """Some receipt of this delivery carries a proof of delivery and no receiver report yet."""

    verify_record_id: Optional[int] = None
    """Which record's POD the block's Verify button re-reads. **Not the head row's own.**

    `verify_pod` resolves an attempt by `record_id`, and the row that happens to come first need
    not be one that posted. On PO 207249 it was record 163 — refused at gate 6 for a line with
    nothing outstanding — sitting at the head of a block whose other 19 lines are on receipt 0002.
    The head row therefore offered a Verify POD whose only possible answer was "nothing has been
    posted to Spitfire for this record yet", about a receipt that plainly exists.

    There is one POD per receipt, so any posted line of the block answers for all of them."""


def _why_postable(record, reason: str) -> str:
    """Plain English for whichever of the four routes permits posting without a POD file.

    The tooltip used to read "accepted by {waived_by}" unconditionally, which was true while a
    waiver was the only way to reach it. Body evidence can reach it now, so on those records the
    sentence would have credited a named person with a decision nobody made — and on the ones
    where the waiver field is empty it would simply have trailed off.
    """
    if reason == "waived":
        return f"accepted by {str(_maybe(record, 'pod_waived_by') or '').strip()}"
    if reason == "signer+date":
        return f"the mail states it was signed for by {str(_maybe(record, 'received_by') or '').strip()}"
    if reason == "carrier+tracking+date":
        return (f"the mail states {str(_maybe(record, 'carrier_name') or '').strip()} "
                f"tracking {str(_maybe(record, 'tracking_number') or '').strip()}")
    if reason == "document+date":
        # Named rather than folded into the sentence below, for the same reason the waiver is: a
        # person reading why a receipt may post with no proof attached needs to know whether that
        # rests on a document somebody outside Premier wrote or on a colleague's signature.
        return (f"a delivery document from outside Premier states "
                f"{str(_maybe(record, 'pod_stated_date') or '').strip()}")
    return "the delivery is stated in the mail itself"

def _post_cell(attempt, record, *, has_pod: bool = True,
               delivery: Optional[DeliveryCell] = None,
               line_verdict=None) -> html.Raw:
    """The Post control for one row, at whichever of the two stages it has reached.

    Posting is two steps a person takes separately — the proof of delivery, then the receiver
    report — so this renders one button at a time and nothing once both are done. A control whose
    only possible outcome is a refusal is not offered at all: it teaches people to ignore
    refusals, and the two cases where that is knowable without calling Spitfire (no POD to upload,
    required fields still missing) are cheap to check here.

    What is *not* pre-judged is anything needing a live purchase-order read — an over-receive, a
    quantity that moved. Those still refuse on click, and the reason lands on the cell afterwards.

    `delivery` says this row belongs to a delivery block, which is received as one receipt. The
    control then belongs to the block rather than the row: the first row carries it and points at
    the delivery routes, and the rest report their state without a button. Left `None` — a record
    with no delivery, or a test — every button is the per-record one it always was.
    """
    if delivery is not None and not delivery.is_first:
        # A row in the middle of a block. It still says what happened to it, because each row is
        # posted as its own receipt line and can be flagged on its own, but it offers no control:
        # the delivery's one Post button is on the first row.
        return _post_status(attempt, record, has_pod=has_pod,
                            line_verdict=line_verdict)

    # Where this cell's controls point, and what they offer. A delivery is received as one receipt,
    # so its buttons address the delivery; a record with no delivery keeps the per-record routes it
    # always had.
    where = (f"/ui/deliveries/{delivery.delivery_id}" if delivery
             else f"/ui/records/{record['id']}")
    lines = delivery.lines if delivery else 1
    post_label = f"Post {lines} lines" if delivery and lines > 1 else "Post POD"

    # Verify POD re-reads one file's hash out of the catalog. For a lone record that is this row;
    # for a delivery block it is whichever line actually reached the receipt, because the head row
    # need not be one of them — see `DeliveryCell.verify_record_id`.
    verify_id = (delivery.verify_record_id if delivery and delivery.verify_record_id
                 else record["id"])
    verify = html.verify_button(
        f"/ui/records/{verify_id}/verify-pod", "Verify POD",
        title="Re-read the POD from Spitfire and re-check its hash", small=True, ghost=True)

    # Offline is a third certain-and-cheap refusal, alongside "no POD" and "N gaps" below: no write
    # can reach Spitfire from off Premier's network, so no Post button is drawn. What already
    # happened still shows — a receipt posted last week is a fact, not a control — and Verify POD
    # survives because re-checking a stored file's hash reads from the catalog, which replays.
    offline = spitfire_cassette.writes_refused()

    if delivery is not None:
        return _delivery_head_cell(delivery, attempt, record, has_pod=has_pod, offline=offline,
                                   line_verdict=line_verdict,
                                   verify=verify)

    if attempt is not None and attempt.state == post_ledger.POSTED:
        return html.Raw(str(html.muted(f"Posted {attempt.receipt_doc_no}".rstrip()))
                        + " " + str(verify))

    if attempt is not None and attempt.state == post_ledger.POD_POSTED:
        # The state the split introduced: a real receipt in Premier's ERP carrying proof of
        # delivery and no report. Named on the row rather than left to be inferred from a missing
        # button, because a half-finished write to an ERP is not something to discover later.
        # Offline, the "report pending" badge is the half that still matters — it says a real
        # receipt is unfinished — so it stays and only the button it belongs to goes.
        report = html.muted("offline") if offline else html.verify_button(
            f"{where}/post-report/confirm", "Post report",
            title=f"Add the receiver report to receipt {attempt.receipt_doc_no}".rstrip(),
            small=True, ghost=True)
        return html.Raw(
            str(report) + " " + str(verify) + " " + str(html.badge("report pending", "warn")))

    if attempt is not None and attempt.state == post_ledger.PARTIAL:
        # Deliberately not a button. Retrying would create a second receipt beside the half-built
        # one, which is the failure the ledger exists to prevent.
        return html.badge("Partial", "warn")
    if attempt is not None and attempt.state == post_ledger.CLAIMED:
        # Dated, because "Posting…" on its own reads as live and this state is often anything but:
        # `CLAIMED` never settles itself, so a chain killed mid-flight leaves it here for ever. The
        # day it was claimed is what separates a post running right now from one abandoned in
        # August, and it is the difference between waiting and going to look.
        since = (attempt.claimed_at or "")[:10]
        return html.muted(f"Posting… (since {since})" if since else "Posting…")

    # Nothing posted yet. Three refusals are certain and cheap to know, so no button is drawn for
    # them — the cell says which one instead, so the diagnosis stays where the reviewer is looking.
    if offline:
        return html.muted("offline")

    # The same question `post_decision` gate 2 asks, asked through the same function. This used to
    # be `if not has_pod and not waived_by` — which never considered `body_evidence`, Premier's
    # 2026-08-22 route for a delivery stated entirely in the mail. Eleven records the gate would
    # have accepted were shown a prompt asking someone to waive a proof that was not required.
    #
    # What the purchase order says comes first, from the mirror: a row the order itself refuses
    # says why rather than offering a control whose only outcome is that refusal. `UNCHECKED`
    # deliberately falls through and keeps its button — an order nobody has read yet is not a
    # reason to refuse a delivery.
    if line_verdict is not None and line_verdict.state == read_views.BLOCKED:
        return html.Raw(str(_clipped(line_verdict.reason, 44)))

    may_post = post_decision.can_post_offline(record, has_pod_bytes=has_pod)
    if not may_post:
        # Still not a Post button. Automation may not decide that a receipt can go to Premier's ERP
        # with no proof behind it; a named person may, on a page that says so. What changed is only
        # that this is now the genuinely last resort rather than the first thing offered.
        return html.Raw(
            str(html.muted("no POD")) + " "
            + str(html.tag("a", "Accept anyway", class_="btn ghost small",
                           href=f"/ui/records/{record['id']}/waive-pod",
                           title="Record that this delivery has no proof document and may be "
                                 "posted without one")))

    gaps = completeness.gaps(record)
    if gaps.missing_required:
        return html.muted(f"{len(gaps.missing_required)} gaps")

    if attempt is not None and attempt.state == post_ledger.FLAGGED:
        # The button stays: whatever blocked it may since have been fixed, and the ledger will let
        # the post through the moment it is. What changes is that the reason is on the cell, so a
        # reviewer reads it without a click — and without the live purchase-order read a click
        # costs. The attempt count is there because "refused 6x" is the signal that somebody keeps
        # trying something that needs a different fix.
        again = f" · tried {attempt.attempts}×" if attempt.attempts > 1 else ""
        return html.Raw(
            str(html.verify_button(
                f"{where}/post-pod/confirm", post_label,
                title=f"Last refused: {attempt.detail}{again}", small=True, ghost=True))
            + " " + str(html.badge("blocked", "hold")))

    if not has_pod:
        return html.Raw(
            str(html.verify_button(
                f"{where}/post-pod/confirm",
                f"Post {lines} lines" if delivery and lines > 1 else "Post receipt",
                # Names whichever route permits this, because there are now three and they are
                # not interchangeable: a person accepted it, or the mail itself states a signer or
                # a carrier reference. Reading "accepted by" on a record nobody accepted would put
                # a name against a judgement that was never made.
                title=(f"Create a receipt on PO {record['po_number']}. It will carry no proof of "
                       f"delivery — {_why_postable(record, may_post)}."),
                small=True, ghost=True))
            + " " + str(html.badge("no proof", "warn")))

    return html.verify_button(
        f"{where}/post-pod/confirm", post_label,
        title=(f"Create one receipt on PO {record['po_number']} carrying this delivery's "
               f"{lines} item lines, and attach the proof of delivery" if delivery and lines > 1
               else f"Create a receipt on PO {record['po_number']} and attach the proof of "
                    f"delivery"),
        small=True, ghost=True)


def _maybe(row, name):
    """One column that may be absent — a row built by a test, or read before the column existed."""
    try:
        return row[name]
    except (IndexError, KeyError, TypeError):
        return getattr(row, name, None)


def _post_outcome_fragment(*, ok: bool, heading: str, message: str,
                           result=None, extra=None) -> html.Raw:
    """What the dialog shows afterwards.

    The steps are listed even on success, because "posted" alone does not tell a reviewer that the
    pay requests were linked or that one of them was not — and on a partial post the list is the
    only record of how far it got that a person will actually read.

    `extra` is markup already built by the caller, placed between the message and the steps. A
    delivery post uses it for the per-line breakdown: which item lines went onto the receipt and
    which did not, which is a table rather than a sentence.
    """
    parts = [html.tag("h3", heading),
             html.tag("p", message, class_="" if ok else "warn")]
    if extra is not None:
        parts.append(extra)
    if result is not None and result.receipt_doc_no:
        parts.append(html.tag("p", html.Raw(
            f"Receipt {_escape(result.receipt_doc_no)} on purchase order "
            f"{_escape(result.po_number)}. It is left <strong>In Process</strong> and has not "
            f"been routed, so the purchase order will show nothing received until it is approved.")
        ))
    if result is not None and result.steps:
        parts.append(html.tag("ul", *[html.tag("li", s) for s in result.steps]))
    return html.Raw("".join(str(p) for p in parts))




# --- posting a whole delivery -------------------------------------------------------------------
# One receipt per (purchase order, delivery), carrying one row per item line. The per-record routes
# above stay for records that belong to no delivery; these are what the Records page offers on a
# delivery block, and they are why a truck of twenty lines stops becoming twenty receipts.


def _line_table(lines, *, posted: bool) -> html.Raw:
    """The item lines going onto a receipt, or the ones being left off it, as a small table.

    Shown before anything is created, because this dialog is the last moment a person can catch a
    line matched to the wrong purchase-order row — after the click there is a permanent document.
    """
    rows = []
    for line in lines:
        rows.append([
            (f"{line.line_number:04d}" if line.line_number is not None else html.muted("—")),
            _clipped(line.description, 44),
            (f"{po_verify.fmt_qty(line.quantity)} {line.unit_of_measure}".strip()
             if posted else html.muted("—")),
            html.muted("") if posted else _clipped(line.reason, 96),
        ])
    headers = (["Line", "Description", "Qty", ""] if posted
               else ["Line", "Description", "", "Why not"])
    return html.table(headers, rows, empty="none")


def _pod_way_out(delivery_id: int, blocked: int) -> html.Raw:
    """The link out of a dialog that has just refused every line for having no proof.

    Before this, that dialog was the end of the road: it said none of the thirty-two lines could
    post, gave the reason, and offered nothing — while the escape hatch existed the whole time, one
    page away, drawn only on rows the page did not believe had a proof. Measured 2026-09-10, the
    page believed wrongly about 91 of 107 rows.

    A plain anchor, not a `data-verify` button: this navigates to a page rather than swapping a
    fragment, and the one inline script only intercepts the handful of `data-` attributes it owns.
    """
    return html.Raw(str(html.tag(
        "p",
        html.tag("a", f"Settle the proof for {blocked} line{'s' if blocked != 1 else ''}",
                 class_="btn", href=f"/ui/deliveries/{delivery_id}/proof",
                 title="Pick the file on this message that proves the delivery, or accept that "
                       "there is none"))))


def _delivery_confirm(delivery_id: int, conn, *, stage: str) -> str:
    """Say exactly what is about to be created, then offer the button that creates it.

    A POST that only reads — the dialog script always POSTs, and this codebase already has the
    shape. It runs the real gates rather than guessing, so the counts here are the counts that will
    happen; that costs one live purchase-order read, which is the same read the single-record
    confirm has always paid and is why it is not done on page render.
    """
    delivery = deliveries_store.get(conn, delivery_id)
    if delivery is None:
        return str(html.tag("p", "No such delivery.", class_="empty"))
    po_number = str(delivery["po_number"] or "")

    rows = spitfire_post.delivery_rows(conn, delivery_id)
    if not rows:
        return str(_post_outcome_fragment(
            ok=False, heading="Nothing to post",
            message=("no line of this delivery is ready — they are held back as quantity "
                     "conflicts, incomplete, or already posted.")))

    plans, _ = spitfire_post.plan_delivery(conn, rows)
    going = [p for p in plans if p.ok]
    skipped = [p for p in plans if not p.ok]

    blocked_on_pod = [p for p in skipped if p.blocked_on_pod]
    if not going:
        return str(_post_outcome_fragment(
            ok=False, heading="Nothing to post",
            message=(f"none of the {len(plans)} lines on this delivery can be posted right now. "
                     f"Each reason is below."),
            extra=html.Raw(str(_line_table(skipped, posted=False))
                           + (str(_pod_way_out(delivery_id, len(blocked_on_pod)))
                              if blocked_on_pod else ""))))

    parts = [
        html.tag("h3", f"Post {len(going)} of {len(plans)} item lines to one receipt?"),
        html.tag("p", html.Raw(
            f"This creates <strong>one</strong> receipt on purchase order "
            f"<strong>{_escape(po_number)}</strong> carrying "
            f"<strong>{len(going)}</strong> item line"
            f"{'s' if len(going) != 1 else ''}, uploads the proof of delivery once, checks the "
            f"catalog's hash against ours, and attaches it.")),
        _line_table(going, posted=True),
    ]
    if skipped:
        parts += [
            html.tag("p", html.Raw(
                f"<strong>{len(skipped)}</strong> line{'s' if len(skipped) != 1 else ''} "
                f"will <strong>not</strong> be on this receipt. Fixing one later puts it on a "
                f"second receipt — a posted receipt is never reopened.")),
            _line_table(skipped, posted=False),
        ]
        # The half-blocked delivery has the same dead end as the wholly-blocked one, for fewer
        # lines and with nothing on screen to say so. Offered here too rather than only above,
        # because "16 of 20" is exactly the shape in which the other four get forgotten.
        if blocked_on_pod:
            parts.append(_pod_way_out(delivery_id, len(blocked_on_pod)))
    parts += [
        html.tag("p", "The receiver report is not posted by this step — it becomes a separate "
                      "button once the proof of delivery is on the receipt.", class_="sub"),
        html.tag("p", "Nothing is routed. The receipt is left In Process for a person to approve.",
                 class_="sub"),
        html.verify_button(f"/ui/deliveries/{delivery_id}/post-pod",
                           f"Post {len(going)} lines to Spitfire",
                           title=f"Create one receipt on PO {po_number} for {len(going)} item "
                                 f"lines and attach the proof of delivery"),
    ]
    return str(html.Raw("".join(str(part) for part in parts)))


@router.post("/deliveries/{delivery_id}/post-pod/confirm", response_class=HTMLResponse)
def post_delivery_pod_confirm_fragment(
        delivery_id: int, conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    return _delivery_confirm(delivery_id, conn, stage="pod")


@router.post("/deliveries/{delivery_id}/post-pod", response_class=HTMLResponse)
def post_delivery_pod_fragment(
        delivery_id: int, conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    """Create one receipt for the delivery and put every ready line and the POD on it.

    Synchronous, and for several seconds. A background job would need polling, polling needs script,
    and `tests/test_ui_html.py` pins these pages to exactly one inline block. It is also *fewer*
    calls than posting the same lines one at a time, which is what this replaces.
    """
    return _delivery_write_guarded(conn, delivery_id, spitfire_post.post_delivery_pod,
                                   _POST_HEADINGS)


@router.post("/deliveries/{delivery_id}/post-report/confirm", response_class=HTMLResponse)
def post_delivery_report_confirm_fragment(
        delivery_id: int, conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    delivery = deliveries_store.get(conn, delivery_id)
    if delivery is None:
        return str(html.tag("p", "No such delivery.", class_="empty"))

    attempts = spitfire_post.awaiting_report_group(conn, delivery_id)
    if not attempts:
        return str(_post_outcome_fragment(
            ok=False, heading="Nothing to add a report to",
            message=("the proof of delivery has not been posted for this delivery — post it "
                     "first, and the report becomes available on the receipt it creates.")))

    where = attempts[0].receipt_doc_no or attempts[0].receipt_key[:8]
    return str(html.Raw("".join(str(part) for part in [
        html.tag("h3", f"Post the receiver report onto receipt {where}?"),
        html.tag("p", html.Raw(
            f"The proof of delivery is already on this receipt. This builds "
            f"<strong>one</strong> receiver report covering all "
            f"<strong>{len(attempts)}</strong> item line"
            f"{'s' if len(attempts) != 1 else ''} on it, uploads it, attaches it beside the POD, "
            f"links purchase order <strong>{_escape(str(delivery['po_number'] or ''))}</strong> "
            f"and any pay requests, and reads the receipt back to confirm both files are on it.")),
        html.tag("p", "Nothing is routed. The receipt stays In Process.", class_="sub"),
        html.verify_button(f"/ui/deliveries/{delivery_id}/post-report",
                           "Post report to Spitfire",
                           title="Build and attach the receiver report for the whole delivery"),
    ])))


@router.post("/deliveries/{delivery_id}/post-report", response_class=HTMLResponse)
def post_delivery_report_fragment(
        delivery_id: int, conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    return _delivery_write_guarded(conn, delivery_id, spitfire_post.post_delivery_report,
                                   _POST_HEADINGS)


@router.post("/records/verify", response_class=HTMLResponse)
def verify_all_fragment(ids: str = "",
                        conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    """Every listed record against Spitfire — or only the ticked ones, if `ids` names any.

    The selection arrives as a query string rather than a posted form because the control that
    sends it is the shared `data-verify` button, which the one inline script POSTs; see
    `html.verify_button(selection=...)`. It narrows what is verified and can never widen it: an id
    that is not on this page is not in `records_ready` and is dropped, so a crafted `?ids=` reaches
    nothing the page was not already offering.
    """
    rows = read_views.records_ready(conn)
    if not rows:
        return str(html.tag("p", "There are no records to verify.", class_="empty"))
    wanted = {piece.strip() for piece in ids.split(",") if piece.strip()}
    if wanted:
        rows = [r for r in rows if str(r["id"]) in wanted]
        if not rows:
            return str(html.tag("p", "None of the selected records are still listed here.",
                                class_="empty"))
    results = po_verify.verify_records(conn, rows)

    # Same rule as the single-record route, applied across the page: an exact spec match whose
    # quantity agrees is written onto the record, anything weaker is left for a person. Doing this
    # here as well as there is the difference between "Verify all" being a report and being the
    # step that actually makes records postable.
    by_id = {r["id"]: r for r in rows}
    kept = [outcome for outcome in
            (record_completion.apply_verification(conn, by_id[v.record_id], v)
             for v in results if v.record_id in by_id)
            if outcome.ok]

    banner = ""
    if kept:
        complete_now = sum(1 for outcome in kept if outcome.is_complete)
        banner = str(html.banner(
            f"{len(kept)} purchase-order line(s) recorded from an exact spec match — "
            f"{complete_now} record(s) are now complete. Lines matched on description alone were "
            f"left for a person.", kind="good"))
    return banner + str(_verification_summary(results))


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
        parts.append(_matched_line(result.matched, result.vendor_name,
                                   result.record_vendor_name))
    parts.append(html.tag("ul", *[html.tag("li", note) for note in result.notes],
                          class_="vfindings"))
    if result.matched and not result.matched.spec_resolved:
        parts.append(_line_alternatives(result))
    return html.tag("div", *parts, class_="vrec")


def _line_alternatives(result) -> html.Raw:
    """The other receivable lines on this purchase order, offered only when the spec did not
    resolve the match.

    A description match is a weaker claim than a spec match — the popup already says so — but
    saying it without offering anything to check leaves a reader with a warning and no way to act
    on it. These buttons re-run the same comparison against a line the reader picks.

    Deliberately not shown when the spec resolved the line. A chooser on a confident match invites
    second-guessing the one signal that is actually reliable, and every extra line offered is a
    chance to pick the wrong one.
    """
    lines = getattr(result, "line_options", None) or []
    others = [l for l in lines
              if result.matched is None or l.line_number != result.matched.line_number]
    if not others:
        return html.Raw("")

    rows = []
    for opt in others:
        rows.append(html.tag(
            "tr",
            html.tag("td", f"{opt.line_number:04d}" if opt.line_number is not None else "—"),
            html.tag("td", opt.spec_code or html.muted("—")),
            html.tag("td", opt.description or html.muted("—"), class_="t"),
            html.tag("td", f"{po_verify.fmt_qty(opt.qty_ordered)} {opt.unit_of_measure}",
                     class_="n"),
            html.tag("td", f"{po_verify.fmt_qty(opt.qty_outstanding)} {opt.unit_of_measure}",
                     class_="n"),
            html.tag("td", html.verify_button(
                f"/ui/records/{result.record_id}/verify?line={opt.line_number}",
                "Compare",
                title=f"Compare this record against line {opt.line_number:04d} instead",
                small=True, ghost=True)),
        ))
    return html.tag(
        "details",
        html.tag("summary", f"Compare against a different line ({len(others)} other"
                            f"{'s' if len(others) != 1 else ''} on this PO)"),
        html.tag("table",
                 html.tag("thead", html.tag("tr", *[html.tag("th", h) for h in
                          ("Line", "Spec", "Description", "Ordered", "Outstanding", "")])),
                 html.tag("tbody", *rows), class_="vq"),
        class_="valt")


def _matched_line(check, vendor_name: str = "", record_vendor: str = "") -> html.Raw:
    """The comparison table. Two columns because they are two different claims.

    Spec, description and vendor come first because they are what a reader is checking — whether
    this is the same *item* — and the quantities only mean anything once that is settled. The
    earlier version of this table opened on Quantity and showed the description once, truncated to
    90 characters in the heading, which put the numbers first and hid the thing they depend on.

    Descriptions are not truncated here. Spitfire's run to a thousand characters and carry the
    model numbers and finishes that distinguish two otherwise identical lines — cutting them is
    what would make two different items look like one.

    Rows are tinted only where the two sides can actually be compared. A row with nothing on one
    side is neither agreement nor disagreement, and colouring it either way would assert something
    the data does not say.
    """
    line_label = f"line {check.line_number:04d}" if check.line_number is not None else "a line"
    if getattr(check, "reviewer_chose", False):
        how = f"A reviewer chose {line_label}"
    elif check.spec_resolved:
        how = f"Spec {check.spec_code or '—'} resolved {line_label}"
    else:
        how = f"Description resolved {line_label}"
    heading = html.tag("p", how, class_="note")

    def row(label, email_cell, po_cell, state="", numeric=True):
        kind = "n" if numeric else "t"
        return html.tag("tr",
                        html.tag("th", label),
                        html.tag("td", email_cell, class_=f"{kind} {state}".strip()),
                        html.tag("td", po_cell, class_=kind))

    def agreement(ours, theirs):
        """Tint only when both sides said something. Compared case- and space-insensitively:
        `STE-402-LT-B` and `ste-402-lt-b ` are the same claim, and flagging them as a difference
        would train a reader to ignore the colour."""
        a, b = (ours or "").strip().casefold(), (theirs or "").strip().casefold()
        if not a or not b:
            return ""
        return "same" if a == b else "diff"

    def name_agreement(ours, theirs):
        """Vendor names, where a legal suffix is not a disagreement.

        Live example, PO 908491: the mail says `Light Annex`, the purchase order says
        `Light Annex, LLC`. Marking that red is the same mistake `UOM_ALIASES` exists to prevent —
        a warning that fires on a difference that is not one teaches a reader to ignore the colour,
        and then the real mismatch goes past unnoticed.

        One side being a prefix of the other is treated as agreement. Anything else is left
        untinted rather than called a difference: two vendor names that merely look unalike may
        still be the same company trading under another name, and this screen does not know.
        """
        a, b = (ours or "").strip().casefold().rstrip(".,"), (theirs or "").strip().casefold().rstrip(".,")
        if not a or not b:
            return ""
        return "same" if a == b or a.startswith(b) or b.startswith(a) else ""

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

    record_spec = getattr(check, "record_spec", None)
    record_desc = getattr(check, "record_description", None)

    # When the parent spec resolved the line, the two codes genuinely differ but the match is still
    # exact — on the parent. Showing the bare sub-spec against the line's code would tint the row
    # red and contradict the note beside it, which says the line is right.
    if getattr(check, "matched_on_parent", False):
        spec_cell = html.Raw(f"{html.esc(record_spec or '')} "
                             f"{html.tag('span', f'(parent {check.spec_code})', class_='muted')}")
        spec_state = "same"
    else:
        spec_cell = record_spec or html.muted("—")
        spec_state = agreement(record_spec, check.spec_code)

    body = [
        row("Spec", spec_cell, check.spec_code or html.muted("—"), spec_state, numeric=False),
        row("Description", record_desc or html.muted("—"), check.description or html.muted("—"),
            "", numeric=False),
        row("Vendor", record_vendor or html.muted("—"), vendor_name or html.muted("—"),
            name_agreement(record_vendor, vendor_name), numeric=False),
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
                                   html.tag("th", "On the purchase order (in Spitfire)"))),
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


# --- Creating a record by hand ------------------------------------------------------------------
#
# The path for mail the pipeline could not finish: an image-only body, a portal link instead of an
# attachment, a property reply saying "Yes ma'am, this was received!" and naming no purchase order.
# Premier's own staff complete those here rather than the work being dropped.
#
# A whole page rather than a dialog fragment, deliberately. A refused submission has to come back
# carrying what the reviewer typed, and re-rendering a page does that with no JavaScript at all —
# these pages spend exactly one inline script and it is not for this. It is also a long form with
# an attachment chooser in it, which a modal would crush.
#
# Nothing here writes to Spitfire. It writes one row to our own store, and that row then goes
# through exactly the same eight gates as every extracted record.

_UNIT_HINT = ("Optional — taken from the purchase order line when it is left blank. "
              "Fill it only if the email states a unit the order does not.")



def _mandatory_fields(values, missing=()) -> list:
    """The five facts only the delivery notification can supply. Everything else is derived.

    Rendered from `completeness.REQUIRED` rather than from a list written out here, so shrinking or
    widening that tuple changes the form with it. The two lists silently disagreeing is how a form
    starts collecting a field nothing needs, or stops collecting one that blocks every post.
    """
    labels = {
        "po_number": ("PO number", "The purchase order in Spitfire these goods were ordered on."),
        "spec_code": ("Spec code", "As Premier names the item — STE-402-LT-B, GR-350a-WTF."),
        "item_description": ("Description", "What arrived, in the words the paperwork uses."),
        "quantity_received": ("Quantity received",
                              "Items, not packages. A header saying 41 CTN against a line saying "
                              "11 EA means eleven items in forty-one cartons — record the eleven."),
        "pod_stated_date": ("Delivery date",
                            "The day the goods actually arrived, as the proof or the message "
                            "states it. Not the day the email was sent."),
    }
    out = []
    for name in completeness.REQUIRED:
        label, hint = labels.get(name, (completeness.LABELS.get(name, name), ""))
        out.append(html.field(name, label, values.get(name, ""), required=True, hint=hint))
    return out


def _pod_chooser(conn, email_id: str, chosen, *, note: str = "") -> html.Raw:
    """Which file on this message is the proof of delivery — or an explicit statement that none is.

    Every non-inline attachment is offered whatever its type or disposition. The three cases this
    exists for are all files the automatic rules skip: a photographed delivery note (they re-read
    PDFs only, because OCR is a paid call), a POD naming another purchase order (rejected outright),
    and a proof that is not a carrier POD at all.

    A file whose bytes were never kept is shown disabled rather than hidden. "The POD is that one
    and we no longer have it" is a different problem from "there was no POD", and only the first is
    ours.
    """
    options = []
    for row in record_create.choosable_attachments(conn, email_id):
        usable = record_create.has_bytes(conn, dict(row, email_id=email_id))
        marks = []
        if row["is_pod"]:
            marks.append(html.badge("reads as a POD", "pod"))
        if row["pod_po_numbers"]:
            marks.append(html.muted(f" names PO {row['pod_po_numbers']}"))
        if not usable:
            marks.append(html.badge("bytes not stored", "error"))
        label = html.Raw(
            str(html.tag("b", row["filename"] or "(unnamed)")) + " "
            + str(html.muted(f"· {row['sniffed_kind'] or 'unknown'} · {_num(row['size_bytes'])} B"))
            + " " + " ".join(str(m) for m in marks))
        options.append(html.tag(
            "div",
            html.radio("pod_ledger_id", str(row["id"]), label,
                       checked=(str(chosen or "") == str(row["id"]))),
            # The viewer, not the raw route: it renders markup we built, or a sandboxed iframe with
            # no `allow-scripts`. Choosing a proof means looking at it first.
            #
            # `data-frag-into` sends it to the panel on the right instead of the popup. This button
            # sits in the left column, outside the panel, so `_JS` cannot work that out from where
            # it is — it has to be named. Deciding which file is the proof is the one moment the
            # file and these radios most need to be on screen together.
            html.tag("button", "Open", type="button", class_="btn ghost small",
                     data_frag=(f"/ui/mail/attachment/view?id={quote(email_id)}"
                                f"&n={row['ordinal']}&src={html.DEFAULT_MAIL_SOURCE}"),
                     data_frag_into="mail-pane",
                     data_frag_title=f"Attachment — {row['filename'] or 'file'}"),
            class_="pod-option"))

    none_note = ("Most Authority Inbound notifications state the whole delivery in a table in the "
                 "body and attach nothing. Choosing this means the receipt in Spitfire will carry "
                 "no proof document — only the receiver report — and your name is recorded against "
                 "that decision.")
    options.append(html.radio(
        "pod_ledger_id", "none",
        html.Raw(str(html.tag("b", "No attachment — the message body is the proof"))),
        checked=(str(chosen or "") == "none"), hint=none_note))

    return html.section(
        "Proof of delivery",
        *options,
        # Overridable because the same chooser now serves two pages that owe the reader different
        # sentences. The create form cannot proceed without an answer here; the delivery page can
        # be left alone entirely, and telling somebody a record "cannot be created" on a page that
        # creates nothing is the kind of small lie that teaches people to stop reading notes.
        note=note or ("Pick the file that proves this delivery, or say there is none. Nothing is "
                      "assumed: a record cannot be created until one of these is chosen."),
    )


def _recorded_so_far(email_id: str, made) -> html.Raw:
    """What this message has yielded so far, and the two ways to add to it.

    Two buttons rather than one form that guesses. The work takes exactly two shapes — the next
    line of the delivery just recorded, or a different purchase order named in the same message —
    and they want opposite things from the form: one carries the PO, the date and the unit forward,
    the other must not carry anything. A single "create another" would have to decide which as the
    person typed, clearing fields under them when they edited the PO.

    The last record created is the one another line is measured from: a message listing three POs
    is worked one PO at a time, so "the most recent" is the delivery in hand.
    """
    latest = made[-1]
    po = (latest["po_number"] or "").strip()
    listed = ", ".join(f"#{r['id']}" for r in made)
    who = ", ".join(sorted({str(r["created_by"] or "somebody") for r in made}))
    return html.section(
        "Recorded from this message",
        html.tag("p", html.muted(f"{len(made)} so far — {listed}, by {who}.")),
        html.tag(
            "div",
            html.tag("a", f"Add another line to PO {po}" if po else "Add another line",
                     class_="btn", href=f"/ui/records/new?email_id={quote(email_id)}"
                                        f"&after={latest['id']}&same_po=1",
                     title="Same purchase order, same delivery date — a different item on it"),
            " ",
            html.tag("a", "Record a different PO from this message", class_="btn ghost",
                     href=f"/ui/records/new?email_id={quote(email_id)}",
                     title="A separate delivery that this same message reports"),
            " ",
            # The way out. Without it this page is a loop with no stated end, and the person who
            # has finished has to reach for the browser's back button to say so.
            html.tag("a", "Done — back to Needs a human", class_="btn ghost",
                     href="/ui/manual"),
            class_="controls",
        ),
    )


def _create_form(conn, email_id: str, *, values=None, chosen=None, created_by="",
                 note_text="", problem=None, after=None, same_po=False) -> str:
    """The form itself, rendered fresh or re-rendered after a refusal carrying what was typed.

    `after` is the record just created from this message: it turns the page into the confirmation
    for that write *and* the start of the next one. A delivery notification routinely lists several
    lines, and the form used to end at a redirect to Records — so recording the second line meant
    finding the message again on a queue the first line had not removed it from.
    """
    if values is not None:
        seeded = values
    elif same_po and after is not None:
        seeded = record_create.line_seed(conn, after)
    else:
        seeded = record_create.prefill(conn, email_id)
    values = seeded

    parts = []
    if problem is not None:
        parts.append(html.errors(problem.message, problem.missing))
    elif after is not None:
        parts.append(html.banner(
            f"Record #{after} created from this message."
            + (f" This is another line of PO {values['po_number']} — the purchase order, the "
               f"delivery date and the unit are carried over; what the line *is* is not."
               if same_po and values.get("po_number") else
               " Anything below is a fresh record from the same message."),
            kind="good"))

    parts.append(html.section(
        "What arrived",
        *_mandatory_fields(values),
        html.field("unit_of_measure", "Unit", values.get("unit_of_measure", ""),
                   hint=_UNIT_HINT),
        note=("These five are the facts only the delivery notification can carry. Vendor, PO line "
              "number and who signed for the goods are deliberately not asked for — they come from "
              "the purchase order and from the proof of delivery, which already hold them."),
    ))

    parts.append(_pod_chooser(conn, email_id, chosen))

    parts.append(html.section(
        "You",
        html.field("created_by", "Your name", created_by, required=True,
                   hint="Recorded against this record for good, and printed in the Receiver "
                        "column of the report."),
        html.textarea("note", "Note (optional)", note_text,
                      hint="Anything the next person needs to know about why this was entered by "
                           "hand."),
    ))

    body = html.form("/ui/records/new", html.tag("input", type="hidden", name_="email_id",
                                                 value=email_id),
                     *parts, submit="Create the record", cancel="/ui/manual",
                     cancel_label="Back to Needs a human")
    made = record_create.records_from(conn, email_id)
    if made:
        body = html.Raw(str(body) + str(_recorded_so_far(email_id, made)))
    # The message itself, beside the form rather than in the popup over it. `bare=1` for the reason
    # `_mail_fragment_html` gives: the Create control that header would carry links back to this
    # very page, and following it throws away everything typed.
    host = html.mail_url(email_id, "", html.DEFAULT_MAIL_SOURCE) + "&bare=1"
    pane = html.mail_pane(
        host,
        _mail_fragment_html(email_id, src=html.DEFAULT_MAIL_SOURCE, bare=True),
        note="Every field on the left comes from this message or from the proof attached to it. "
             "Open an attachment and it opens here, beside the form, not over it.",
    )
    # The way out, in the header where it is visible on arrival. The form's Cancel goes to the same
    # place but sits below the last field, so on this form it is four sections down.
    return html.page("Create a record", "/ui/manual", html.split(body, pane),
                     back="/ui/manual", back_label="Needs a human", **_chrome(conn))


@router.get("/records/new", response_class=HTMLResponse)
def new_record_form(email_id: str = "", after: Optional[int] = None, same_po: bool = False,
                    conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    """Open the form on one message, pre-filled with whatever was already worked out about it.

    `after` says a record was just created and names it; `same_po` says the next one is another
    line of that same delivery rather than a different purchase order. Both are declared, unlike
    the `?created=` the Records page has never read — a query parameter no handler names is not a
    feature, it is a redirect writing into the void.
    """
    if not email_id:
        return html.page("Create a record", "/ui/manual",
                         html.section("Create a record",
                                      html.tag("p", "Open this from a message on the Needs a "
                                                    "human page — a record is always built from "
                                                    "one.", class_="empty")),
                         back="/ui/manual", back_label="Needs a human", **_chrome(conn))
    if conn.execute("SELECT 1 FROM email_log WHERE email_id = ?", (email_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="no such message")
    return _create_form(conn, email_id, after=after, same_po=same_po)


@router.post("/records/new", response_class=HTMLResponse)
async def create_record(request: Request):
    """Create it, or come back saying exactly what was wrong.

    The kill switch is honoured even though nothing here reaches Spitfire: a stop means the system
    is not to act, and staging a record that the next press of Post would send is acting.

    The connection is opened here rather than through `Depends(deps.get_pipeline_conn)`, matching
    the other POST handlers in this file. A sync dependency is resolved in a worker thread while an
    async handler runs on the event loop, and a sqlite connection is bound to the thread that
    created it — so the pair raises "SQLite objects created in a thread can only be used in that
    same thread" the moment the handler touches it.
    """
    posted = await _form_values(request)
    email_id = posted.get("email_id", "")
    created_by = posted.get("created_by", "")
    pod_ledger_id = posted.get("pod_ledger_id", "")
    note = posted.get("note", "")
    values = {name: posted.get(name, "") for name in
              ("po_number", "spec_code", "item_description", "quantity_received",
               "unit_of_measure", "pod_stated_date")}
    conn = deps.pipeline_connection()
    try:
        if killswitch.is_stopped():
            stopped = record_create.Created(
                ok=False,
                message="the kill switch is engaged — release it before recording anything.")
            return HTMLResponse(_create_form(conn, email_id, values=values, chosen=pod_ledger_id,
                                             created_by=created_by, note_text=note,
                                             problem=stopped))

        chosen = None if pod_ledger_id in ("", "none") else _int_or_none(pod_ledger_id)
        result = record_create.create(
            conn, email_id=email_id, created_by=created_by, values=values,
            pod_ledger_id=chosen, waive_pod=(pod_ledger_id == "none"), note=note)

        if not result.ok:
            return HTMLResponse(_create_form(conn, email_id, values=values, chosen=pod_ledger_id,
                                             created_by=created_by, note_text=note,
                                             problem=result))
    finally:
        conn.close()
    # 303: the browser must follow with a GET, or a refresh on the Records page re-submits the form
    # and stages the delivery twice.
    #
    # Back to the message, not on to Records. One notification commonly lists several lines, and
    # landing on Records meant the second line began by finding the message again. The page it
    # returns to confirms the write and offers the two ways there are to continue — another line of
    # the same PO, or a different one — with the way out beside them.
    return RedirectResponse(
        f"/ui/records/new?email_id={quote(email_id)}&after={result.record_id}", status_code=303)


def _int_or_none(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# --- Accepting a delivery that has no proof ------------------------------------------------------
#
# A page rather than a dialog, for the same reason the create form is one: this is a decision with
# a consequence in Premier's ERP, it has to be explained before it is taken, and it has to be
# signed. A modal with one button invites the reflex press this must not have.


def _waive_page(conn, record_id: int, *, by: str = "", problem: str = "") -> str:
    row = _any_record_or_none(conn, record_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such record")

    already = str(_maybe(row, "pod_waived_by") or "").strip()
    if already:
        body = html.section(
            "Already accepted",
            html.tag("p", f"Record #{record_id} was accepted without a proof of delivery by "
                          f"{already}. That decision stands — the first name is the one that took "
                          f"the risk."),
            html.tag("p", html.tag("a", "Back to Records", href="/ui/records", class_="btn")),
        )
        return html.page("Accept without a proof", "/ui/records", body, **_chrome(conn))

    parts = []
    if problem:
        parts.append(html.errors(problem))
    parts.append(html.section(
        f"Record #{record_id} — {row['po_number']}",
        html.tag("p", f"{_num(row['quantity_received'])} {row['unit_of_measure'] or ''} of "
                      f"{row['spec_code'] or 'this item'}, delivered "
                      f"{row['pod_stated_date'] or 'on an unstated date'}.", class_="lede"),
        html.tag("p", html.mail_link(row["source_email_id"], "Read the message first",
                                     title="What this record was built from")),
        note=("No file on this message could be read as a proof of delivery. Most Authority "
              "Inbound notifications are like this: the whole delivery is stated in a table in "
              "the body and nothing is attached."),
    ))
    parts.append(html.section(
        "What accepting means",
        html.tag("ul",
                 html.tag("li", "A receipt is created in Spitfire against this purchase order, "
                                "with no proof document attached to it."),
                 html.tag("li", "The receiver report is still built and attached, so the receipt "
                                "is not empty — but Premier's ERP will hold nothing showing the "
                                "goods arrived."),
                 html.tag("li", "The email stays here as the audit trail, and your name is "
                                "recorded against this decision for good.")),
        html.form(f"/ui/records/{record_id}/waive-pod",
                  html.field("by", "Your name", by, required=True,
                             hint="Recorded on the record and carried into the ledger entry for "
                                  "the post."),
                  submit="Accept without a proof of delivery",
                  cancel="/ui/records", cancel_label="Back to Records"),
        note="Nothing is sent to Spitfire by this page. It unblocks the Post button, which is "
             "still a separate press.",
    ))
    return html.page("Accept without a proof", "/ui/records", *parts, **_chrome(conn))


@router.get("/records/{record_id}/waive-pod", response_class=HTMLResponse)
def waive_pod_form(record_id: int,
                   conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    """Explain what accepting a POD-less delivery means, and ask who is accepting it."""
    return _waive_page(conn, record_id)


@router.post("/records/{record_id}/waive-pod", response_class=HTMLResponse)
async def waive_pod(record_id: int, request: Request):
    """Accept that a record has no proof of delivery and may post anyway.

    The other half of the body-only path. Extraction stages these from Inbound notifications that
    attach nothing, `post_decision` refuses every one, and without this nothing could ever unblock
    them — while automation still must never make that call for itself.

    Writes nothing outbound. It is the control that lets a write happen later, so it is guarded
    like one, and the name it records is what the ledger will carry.
    """
    posted = await _form_values(request)
    by = posted.get("by", "").strip()
    conn = deps.pipeline_connection()
    try:
        if killswitch.is_stopped():
            return HTMLResponse(_waive_page(
                conn, record_id, by=by,
                problem="the kill switch is engaged — release it before accepting anything."))
        result = record_create.waive_pod(conn, record_id, by=by)
        if not result.ok:
            return HTMLResponse(_waive_page(conn, record_id, by=by, problem=result.message))
    finally:
        conn.close()
    return RedirectResponse(
        _safe_return(posted.get("return_to", ""), f"/ui/records?waived={record_id}"),
        status_code=303)


# --- Settling the proof for a whole delivery -----------------------------------------------------
#
# The way out of the dead end. A delivery whose lines gate 2 refuses used to offer a "Post N lines"
# button, a dialog saying none of them could post, and nothing else — measured 2026-09-10, that was
# 91 of the 107 rows the Records page believed had a proof, across 24 deliveries.
#
# Two outcomes, one page, because they answer the same question and a person should see both before
# choosing: nominate a file on the message as the proof, or accept that there is none. The chooser
# is `_pod_chooser`, unchanged from the create form, so the vocabulary a reviewer learns in one
# place works in the other and the two cannot drift on what may be picked.
#
# A page and not a dialog, for the reason the section above gives: this is signed, it has a
# consequence in Premier's ERP, and one press here covers every line of a truck.


def _delivery_proof_page(conn, delivery_id: int, *, by: str = "", problem: str = "",
                         chosen=None, done: str = "") -> str:
    delivery = deliveries_store.get(conn, delivery_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail="no such delivery")
    po_number = str(delivery["po_number"] or "")

    blocked = spitfire_post.pod_blocked(conn, spitfire_post.delivery_rows(conn, delivery_id))
    parts = []
    if problem:
        parts.append(html.errors(problem))
    if done:
        parts.append(html.banner(done, "good"))

    if not blocked:
        parts.append(html.section(
            f"Delivery #{delivery_id} — PO {po_number}",
            html.tag("p", "Every line of this delivery already has a proof of delivery, or a "
                          "reason it does not need one. There is nothing to decide here.",
                     class_="lede"),
            html.tag("p", html.tag("a", "Back to Records", href="/ui/records", class_="btn")),
        ))
        return html.page("Proof of delivery", "/ui/records", *parts, **_chrome(conn))

    # Grouped by message, because attachments belong to a message and not to a delivery. Usually one
    # group: `deliveries_store` keys a delivery on (purchase order, delivery reference) and its
    # docstring is explicit that one delivery may be described by several messages, so the case has
    # to be drawn rather than assumed away. Offering email A's files against email B's records would
    # draw a trap `choose_delivery_pod` then refuses.
    groups = OrderedDict()
    for block in blocked:
        groups.setdefault(str(_maybe(block.row, "source_email_id") or ""), []).append(block)

    parts.append(html.section(
        f"Delivery #{delivery_id} — PO {po_number}",
        html.tag("p", f"{len(blocked)} of this delivery's lines cannot be posted because nothing "
                      f"on the message was read as a proof of delivery. Settle that here and the "
                      f"Post button on Records will offer them.", class_="lede"),
        note=("This does not promise these lines will post. It removes the one refusal a person "
              "can remove — the purchase order is still read and checked when you press Post."),
    ))

    for email_id, blocks in groups.items():
        rows = [[str(_maybe(b.row, "spec_code") or "") or html.muted("—"),
                 _clipped(str(_maybe(b.row, "item_description") or ""), 52),
                 f"{_num(_maybe(b.row, 'quantity_received'))} "
                 f"{_maybe(b.row, 'unit_of_measure') or ''}".strip(),
                 str(_maybe(b.row, "pod_stated_date") or "")] for b in blocks]
        parts.append(html.section(
            f"{len(blocks)} line{'s' if len(blocks) != 1 else ''} on this message",
            html.tag("p", html.mail_link(email_id, "Read the message first",
                                         title="What these records were built from")),
            html.tag("p", blocks[0].reason, class_="warn"),
            html.scroll_block(html.table(["Spec", "Description", "Qty", "POD date"], rows,
                                         empty="none")),
            note="Exactly the lines this decision covers. Nothing else on the delivery moves.",
        ))
        parts.append(html.section(
            "What accepting means",
            html.tag("ul",
                     html.tag("li", f"One receipt is created in Spitfire against purchase order "
                                    f"{po_number} carrying these {len(blocks)} item lines."),
                     html.tag("li", "If you pick a file, it is uploaded to that receipt as the "
                                    "proof. If you say there is none, the receipt carries the "
                                    "receiver report and nothing else, and Premier's ERP will hold "
                                    "nothing showing the goods arrived."),
                     html.tag("li", "The email stays here as the audit trail, and your name is "
                                    "recorded against each of these records for good."),
                     html.tag("li", html.tag("b", f"This is one signature covering "
                                                  f"{len(blocks)} lines."))),
            html.form(
                f"/ui/deliveries/{delivery_id}/proof",
                html.tag("input", type="hidden", name_="email_id", value=email_id),
                _pod_chooser(conn, email_id, chosen,
                             note="Pick the file that proves this delivery, or say there is none. "
                                  "A real document on the receipt is always the better answer — "
                                  "waiving is the last resort."),
                html.field("by", "Your name", by,
                           hint="Required to accept a delivery with no proof. Recorded on each "
                                "record and carried into the ledger entry for the post."),
                submit="Settle the proof for these lines",
                cancel="/ui/records", cancel_label="Back to Records"),
            note="Nothing is sent to Spitfire by this page. It unblocks the Post button, which is "
                 "still a separate press.",
        ))

    return html.page("Proof of delivery", "/ui/records", *parts, **_chrome(conn))


@router.get("/deliveries/{delivery_id}/proof", response_class=HTMLResponse)
def delivery_proof_form(delivery_id: int,
                        conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    """Which lines of this delivery have no proof, and the two things a person may do about it."""
    return _delivery_proof_page(conn, delivery_id)


@router.post("/deliveries/{delivery_id}/proof", response_class=HTMLResponse)
async def delivery_proof(delivery_id: int, request: Request):
    """Nominate a file as the proof for these lines, or accept that they have none.

    **The list of records is recomputed here and never read off the form.** The browser sends only
    the message, the choice and the name; which lines that covers is resolved again from the store,
    so a stale page cannot sign for a line that has since moved and a crafted POST cannot widen the
    decision. `record_create` intersects the ids against the delivery a second time.

    Writes nothing outbound. It is the control that lets a write happen later, so it is guarded like
    one — the same kill-switch check `waive_pod` keeps, for the same reason.
    """
    values = await _form_values(request)
    by = values.get("by", "").strip()
    email_id = values.get("email_id", "").strip()
    choice = values.get("pod_ledger_id", "").strip()

    conn = deps.pipeline_connection()
    try:
        if killswitch.is_stopped():
            return HTMLResponse(_delivery_proof_page(
                conn, delivery_id, by=by, chosen=choice,
                problem="the kill switch is engaged — release it before accepting anything."))
        if not choice:
            return HTMLResponse(_delivery_proof_page(
                conn, delivery_id, by=by,
                problem="pick the file that proves this delivery, or say explicitly that there "
                        "is none."))

        blocked = spitfire_post.pod_blocked(conn, spitfire_post.delivery_rows(conn, delivery_id))
        record_ids = [b.record_id for b in blocked
                      if str(_maybe(b.row, "source_email_id") or "") == email_id]

        if choice == "none":
            result = record_create.waive_delivery_pod(
                conn, delivery_id, by=by, record_ids=record_ids)
        else:
            result = record_create.choose_delivery_pod(
                conn, delivery_id, ledger_id=_int_or_none(choice) or 0, email_id=email_id,
                record_ids=record_ids)
        if not result.ok:
            return HTMLResponse(_delivery_proof_page(conn, delivery_id, by=by, chosen=choice,
                                                     problem=result.message))
    finally:
        conn.close()
    return RedirectResponse(
        _safe_return(values.get("return_to", ""), f"/ui/records?settled={delivery_id}"),
        status_code=303)


# --- Overruling triage about what a message is ---------------------------------------------------
#
# Triage was the only opinion in the system about whether a message is a delivery notification, and
# the table listing what it set aside invited people to "open it and say so" with nothing to say it
# with. This is the something.
#
# A page rather than a one-click button, for the reason the waiver page is one: the decision changes
# which queue a message lives in, the next person to read the row needs to know who decided and why,
# and a bare button on a table row invites the reflex press.

_NOT_DELIVERY_REASONS = (
    ("No PO or delivery data", "no PO reference or delivery data anywhere in the thread"),
    ("Advertising", "marketing or advertising mail"),
    ("Internal mail", "internal correspondence, not a delivery notification"),
    ("Scheduled report", "an automated scheduled report, not a delivery notification"),
    ("Order confirmation", "an order confirmation or acknowledgement — the goods have not shipped"),
    ("Carrier status", "a carrier status or tracking update — nothing has arrived yet"),
    ("Meeting or invite", "a calendar invite or meeting mail"),
    ("Nothing arrived", "this thread is about a delivery still to come, not one that happened"),
)
"""The reasons a message gets set aside, worded once so nobody types them again.

Drawn from what the rules themselves say and from what people were typing into this box: the
`rule_5c_internal_noise`, `rule_0c_report_sender` and `rule_5d_no_delivery_claim` populations are
the first, third, fourth and eighth of these between them, and marketing mail is the commonest thing
`rule_7_unknown` drops on the queue with no rule able to name it.

Two strings each, and the difference matters. The first is what fits on a button and can be read at
a glance; the second is what ends up stored, and it has to still mean something to somebody reading
the row back with none of this screen in front of them — "Advertising" on its own is a category,
"marketing or advertising mail" is a sentence about this message.
"""

_DELIVERY_REASONS = (
    ("POD attached", "the signed proof of delivery is attached to this message"),
    ("Body states arrival", "the body states the goods arrived, with a date"),
    ("Confirmed with property", "the property confirmed the delivery directly"),
    ("Rule too broad", "the rule matched on the sender, but this thread is a real delivery"),
    ("Missed PO", "it does carry a PO reference the rules did not pick up"),
)
"""The other direction, and deliberately a shorter list. Overruling a rule to say a message *is* a
delivery is the rarer decision and the one worth a sentence of its own more often, so these are
starting points rather than the eight-way menu above."""


_CONFIRMED_REASONS = (
    ("Property confirmed", "the property confirmed these items were received"),
    ("Warehouse confirmed", "the warehouse confirmed receipt of these items"),
    ("POD seen", "I have seen the signed proof of delivery for these items"),
    ("Checked in Spitfire", "the receipt is visible against these lines in Spitfire"),
)
"""Why somebody is willing to sign for goods a Premier-written message only claims.

Every one names **who** confirmed it or **what** was seen, because that is the whole content of
the decision. A reason like "looks right" would record that a person clicked, which the timestamp
already says.
"""


_VERDICT_COPY = {
    mail_overrides.NOT_DELIVERY: {
        "title": "Not a delivery",
        "active": "/ui/manual",
        "origin": "/ui/manual",
        "origin_label": "Needs a human",
        # Where a *successful* verdict lands, as distinct from `origin`, which is where Cancel and
        # the back link go — the page you came from. Sending the redirect to `origin` put people
        # back on the queue with their search cleared and the total unchanged (it rises on its own
        # from live ingest), so a verdict that had been recorded correctly looked like a dead
        # button. Landing on the page the message moved to is the confirmation.
        "landing": "/ui/not-deliveries",
        "submit": "Set aside as not a delivery",
        "reasons": _NOT_DELIVERY_REASONS,
        "lede": "Say this message is not a delivery notification.",
        "means": ("It leaves the queue and appears on Not deliveries, listed with your name "
                  "against it.",
                  "Every record read out of this message is retired with it — off the queue, off "
                  "Records, and not postable. Nothing is deleted.",
                  "You can put it back at any time from that page, records and all."),
    },
    mail_overrides.DELIVERY: {
        "title": "This is a delivery",
        "active": "/ui/not-deliveries",
        "origin": "/ui/not-deliveries",
        "origin_label": "Not deliveries",
        "submit": "Put this back on the queue",
        "reasons": _DELIVERY_REASONS,
        "lede": "Say this message is a delivery notification after all.",
        "means": ("It returns to Needs a human, badged Set aside in error, with your name and "
                  "reason on the row.",
                  "Nothing is extracted from it automatically. The message was never accumulated, "
                  "so there is no delivery to hang a record on — Create a record on that row is "
                  "how the record gets made, and the form opens on whatever was already read from "
                  "this message.",
                  "It leaves the queue once a record exists."),
    },
    mail_overrides.CONFIRMED: {
        "title": "Confirm the goods arrived",
        "active": "/ui/manual",
        "origin": "/ui/manual",
        "origin_label": "Needs a human",
        "submit": "Confirm and send to Records",
        "reasons": _CONFIRMED_REASONS,
        "lede": "Say the goods this message lists were actually received.",
        "means": ("Every record read from this message moves to Records, where it can be "
                  "verified against Spitfire and posted.",
                  "Nothing is posted by confirming. This says the delivery happened; the Post "
                  "button still decides whether a receipt can be built, and still refuses an "
                  "incomplete record.",
                  "Your name and reason stay on it, and you can withdraw the confirmation from "
                  "the same row."),
    },
}
"""Everything that differs between the three directions, so the page itself does not branch.

The third is not a variation on the other two. They answer *what kind of message is this* — and a
Premier-written expediting report is unambiguously delivery mail by that test, which is why it was
reaching Records. This one answers *did the goods arrive*, which is a question about the world that
no rule reading the message can settle."""


def _thread_offer(conn, email_id: str, to: str) -> tuple:
    """The rest of this conversation, as a tick list, and the ids it offers.

    Only when setting mail aside. Putting a message *back* on the queue, or confirming goods
    arrived, is a statement about that message: a reply saying "it landed" says nothing about the
    fourteen quotes above it in the thread, and sweeping them along would be inventing decisions
    nobody took.
    """
    if to != mail_overrides.NOT_DELIVERY:
        return html.Raw(""), []

    siblings = read_views.thread_siblings(conn, email_id)
    if not siblings:
        return html.Raw(""), []

    offer = [s for s in siblings if not s.excluded]
    rows = []
    for sibling in siblings:
        label = [html.tag("span", _clipped(sibling.subject or "(no subject)", 78),
                          class_="thread-subject"),
                 html.tag("span", f"{sibling.sender} · {html.when(sibling.when[:16])}"
                                  + (f" · {sibling.records} record"
                                     f"{'' if sibling.records == 1 else 's'}"
                                     if sibling.records else ""),
                          class_="thread-meta")]
        if sibling.excluded:
            rows.append(html.tag("li", html.tag("div", *label, class_="thread-text"),
                                 html.tag("span", sibling.excluded, class_="thread-held"),
                                 class_="thread-row held"))
            continue
        rows.append(html.tag("li", html.tag(
            "label",
            html.tag("input", type="checkbox", name_="also", value=sibling.email_id,
                     checked="checked"),
            html.tag("div", *label, class_="thread-text")), class_="thread-row"))

    carried = sum(s.records for s in offer)
    heading = (f"Also set aside the other {len(offer)} message"
               f"{'' if len(offer) == 1 else 's'} in this conversation"
               + (f" ({carried} record{'' if carried == 1 else 's'})" if carried else ""))
    return html.tag(
        "div",
        html.tag("p", heading, class_="thread-head"),
        html.tag("ul", *rows, class_="thread-list"),
        html.tag("p", "Replies and forwards of the same subject, sharing a purchase order. Each "
                      "keeps its own verdict and can be put back on its own.", class_="hint"),
        class_="thread-offer"), [s.email_id for s in offer]


def _verdict_fragment(conn, email_id: str, to: str, *, by: str = "", note: str = "",
                      problem: str = "") -> str:
    """The same decision, rendered for the popup over the page the reader is already on.

    Not a second implementation of the page: same copy, same guards, same two fields, same thread
    list. What it leaves out is the page — heading bar, sidebar, the "what this means" essay — so
    the decision sits over the queue rather than replacing it.
    """
    copy = _VERDICT_COPY.get(to)
    if copy is None:
        raise HTTPException(status_code=400, detail="unknown verdict")
    row = email_log.get(conn, email_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such message")

    offer, _ = _thread_offer(conn, email_id, to)
    standing = mail_overrides.get(conn, email_id)
    parts = [html.errors(problem) if problem else html.Raw("")]
    if standing is not None:
        held = _VERDICT_COPY[standing.verdict]["title"].lower()
        parts.append(html.banner(
            f"{standing.decided_by} already marked this “{held}”. Submitting below replaces that.",
            kind="warn"))
    parts.append(html.tag("p", copy["lede"], class_="lede"))
    parts.append(html.tag("p", _clipped(row.subject or email_id, 96), class_="thread-subject"))
    # The one consequence that is not obvious from the button, kept even in the short form: this is
    # what makes the rows vanish from the table underneath.
    parts.append(html.tag("p", copy["means"][0], class_="hint"))
    parts.append(html.form(
        "/ui/mail/verdict",
        html.tag("input", type="hidden", name_="email_id", value=email_id),
        html.tag("input", type="hidden", name_="to", value=to),
        html.field("by", "Your name", by, required=True,
                   hint="Recorded against the message and shown on the row."),
        html.note_presets("note", copy["reasons"]),
        html.textarea("note", "Why", note,
                      hint="Optional, and the most useful thing on the row in three months."),
        offer,
        submit=copy["submit"]))
    return "".join(str(part) for part in parts)


def _verdict_page(conn, email_id: str, to: str, *, by: str = "", note: str = "",
                  problem: str = "") -> str:
    """The confirm form, in whichever direction `to` names.

    One page for both, because they are the same decision with the sign flipped: same guards, same
    two fields, same table row moving between the same two pages. Two of these would be one of them
    copied, and the copy is where a guard stops applying.

    **Deliberately without `_waive_page`'s "Already accepted" dead end.** A waiver is irreversible
    and the first name is the one that took the risk; a verdict about what a message *is* can be
    wrong and is meant to be corrected. So an existing verdict is shown in a banner and the form
    still renders — re-submitting is how a name or a reason gets fixed.
    """
    copy = _VERDICT_COPY.get(to)
    if copy is None:
        # Off the query string, and it decides every word on this page. Refused before anything is
        # read rather than defaulted to a direction the person did not ask for.
        raise HTTPException(status_code=400, detail="unknown verdict")

    row = email_log.get(conn, email_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such message")

    parts = []
    if problem:
        parts.append(html.errors(problem))

    standing = mail_overrides.get(conn, email_id)
    if standing is not None:
        held = _VERDICT_COPY[standing.verdict]["title"].lower()
        parts.append(html.banner(
            f"{standing.decided_by} already marked this “{held}” on "
            f"{(standing.decided_at or '')[:16].replace('T', ' ')}"
            + (f" — {standing.note}" if standing.note else "")
            + ". Submitting below replaces that.",
            kind="warn"))

    parts.append(html.section(
        row.subject or email_id,
        html.tag("p", copy["lede"], class_="lede"),
        html.tag("p", f"From {row.sender or 'an unknown sender'}, ",
                 html.when((row.processed_at or "")[:16]), "."),
        html.tag("p", "Triage read it as ", _stamp(row.matched_rule or "no rule"), ": ",
                 row.reason or "no reason recorded", "."),
        html.tag("p", html.mail_link(email_id, "Read the message first",
                                     reason=row.reason or "",
                                     title="What this decision is about")),
        note="Triage decides this for every message from the sender, the thread and what is "
             "attached. It is sometimes wrong, and this is the only thing that can say so.",
    ))

    parts.append(html.section(
        "What this means",
        html.tag("ul", *[html.tag("li", line) for line in copy["means"]]),
        html.form("/ui/mail/verdict",
                  html.tag("input", type="hidden", name_="email_id", value=email_id),
                  html.tag("input", type="hidden", name_="to", value=to),
                  html.field("by", "Your name", by, required=True,
                             hint="Recorded against this message and shown on the row, so the "
                                  "next person knows who to ask."),
                  # Above the box, not inside it. They write into the same textarea the form
                  # submits, so a reason nobody anticipated is still just typed, and two of them
                  # is two clauses rather than a choice between them.
                  html.note_presets("note", copy["reasons"]),
                  html.textarea("note", "Why", note,
                                hint="Optional. Press the reasons above to fill this in, add your "
                                     "own, or both — it is the most useful thing on the row when "
                                     "somebody reads it back in three months."),
                  submit=copy["submit"],
                  cancel=copy["origin"], cancel_label=f"Back to {copy['origin_label']}"),
        note="Nothing is sent to Spitfire by this page, and no record is created or destroyed. It "
             "moves one message between two lists.",
    ))
    return html.page(copy["title"], copy["active"], *parts,
                     back=copy["origin"], back_label=copy["origin_label"], **_chrome(conn))


@router.get("/mail/verdict", response_class=HTMLResponse)
def mail_verdict_form(id: str = "", to: str = "", inline: int = 0,
                      conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    """Explain what reclassifying a message does, and ask who is doing it and why.

    `id` rather than a path segment: a Message-ID is up to 255 characters of angle brackets and `@`,
    and putting one in a URL segment means escaping it at both ends for nothing — the same reason
    `mails_acknowledge` carries it in a form body.

    `inline=1` asks for the form alone, to open in the message popup over whichever queue the
    reader is standing on. The page form stays exactly where it was — it is what a browser with no
    script gets, and what a refused submission re-renders.
    """
    if inline:
        return _verdict_fragment(conn, id, to)
    return _verdict_page(conn, id, to)


@router.post("/mail/verdict", response_class=HTMLResponse)
async def set_mail_verdict(request: Request):
    """Record the verdict and send the browser back to the page the row just left.

    **The kill switch is deliberately not honoured here**, unlike `create_record` and `waive_pod`.
    Those are guarded because they stage or unblock something the next press of Post would send to
    Premier's ERP, and a stop means the system is not to act. This writes one row that only two view
    functions read: nothing in the pipeline consults it, no record is created, and the one path
    onward from a reclassified message — Create a record — checks the switch itself. The precedent
    for a pure human annotation is `mails_acknowledge`, also unguarded. And a stop is when people
    are working out what went wrong; blocking the one control that records "triage got this wrong"
    would block the diagnosis while the incident is live.

    The connection is opened here rather than through `Depends`, matching the other POST handlers:
    a sync dependency resolves on a worker thread while an async handler runs on the event loop, and
    a sqlite connection belongs to the thread that made it.
    """
    posted = await _form_values(request)
    email_id = posted.get("email_id", "")
    to = posted.get("to", "")
    by = posted.get("by", "").strip()
    note = posted.get("note", "")
    # Which of the conversation's other messages were left ticked. Multi-valued, so it cannot come
    # from `_form_values`, which keeps one value per key.
    also = _form_list(await request.body(), "also")
    # The popup asks for an answer it can act on; a browser form asks for a page.
    wants_json = "application/json" in (request.headers.get("accept") or "")

    if to not in mail_overrides.VERDICTS:
        raise HTTPException(status_code=400, detail="unknown verdict")

    conn = deps.pipeline_connection()
    decided = []
    try:
        if not email_id or email_log.get(conn, email_id) is None:
            # Not a silent no-op, unlike `mails_acknowledge` — its SQL guard makes a miss harmless,
            # where this would write a verdict about a message that does not exist.
            raise HTTPException(status_code=404, detail="no such message")
        if not by:
            problem = ("Your name is required — this decision is recorded against the message "
                       "and has to be signed.")
            if wants_json:
                return JSONResponse({"ok": False, "problem": problem})
            return HTMLResponse(_verdict_page(conn, email_id, to, by=by, note=note,
                                              problem=problem))
        # No "already in that state" refusal. The write is an upsert on one row, so a second press,
        # a browser retry or a corrected spelling all land as the same single verdict.
        mail_overrides.set_verdict(conn, email_id=email_id, verdict=to, decided_by=by, note=note,
                                   at=_now())
        decided.append(email_id)

        if also:
            # The ids arrive in a form body, so membership is worked out again here rather than
            # trusted. A page cannot nominate mail it was never offered: `_thread_offer` is the
            # same call that drew the tick list, and anything not in it is dropped silently — the
            # decision the person did take is still recorded.
            _, offered = _thread_offer(conn, email_id, to)
            together = f"set aside with “{_clipped(email_log.get(conn, email_id).subject or '', 60)}”"
            for other in also:
                if other not in offered:
                    continue
                mail_overrides.set_verdict(
                    conn, email_id=other, verdict=to, decided_by=by,
                    note="; ".join(filter(None, [note, together])), at=_now())
                decided.append(other)
    finally:
        conn.close()

    if wants_json:
        return JSONResponse({"ok": True, "verdict": to, "email_ids": decided})

    # Back where the person was standing, which is the page the row has just left. Deliberately not
    # `_back_to(request)`: on this POST the referer is the confirm form itself, so that would send
    # them straight back onto the form they just submitted.
    copy = _VERDICT_COPY[to]
    # Back to the queue they were standing on, when the page said which one that was. The landing
    # page below is what a verdict with no script lands on, and stays the fallback.
    return RedirectResponse(
        _safe_return(posted.get("return_to", ""), copy.get("landing", copy["origin"])),
        status_code=303)


# --- Correcting a record by hand ----------------------------------------------------------------
#
# The other half of "create a record". A message with no record gets the create form; a record that
# already exists and is missing something gets this one — the same layout, over what it already
# holds. Before it, a queue row could only offer `Fill`, which reads the delivery date and the
# signature off the proof and cannot know a spec code, and `Verify`, which compares against the
# purchase order and writes nothing unless the spec matches exactly. Seventeen of the twenty-one
# incomplete records are missing precisely that spec code.

_EDIT_LABELS = {
    "spec_code": ("Spec code", "As Premier names the item — STE-402-LT-B, GR-350a-WTF. Read it "
                               "off the purchase order if the delivery note does not say."),
    "item_description": ("Description", "What arrived, in the words the paperwork uses."),
    "quantity_received": ("Quantity received",
                          "Items, not packages. A header saying 41 CTN against a line saying "
                          "11 EA means eleven items in forty-one cartons — record the eleven."),
    "unit_of_measure": ("Unit", "EA, YD, SF — as the purchase order states it."),
    "package_quantity": ("Packages", "How many cartons, pallets or rolls the items came in. The "
                                     "count on the delivery header, not the quantity above."),
    "package_uom": ("Package unit", "CTN, PLT, ROLL — what the packages are, not what is in them."),
    "pod_stated_date": ("Delivery date",
                        "The day the goods arrived, as YYYY-MM-DD. Not the day the email was sent."),
    "carrier_name": ("Carrier", "Who moved it. Blank is legitimate on a warehouse Inbound notice, "
                                "where the goods never left the 3PL's own network."),
    "tracking_number": ("Tracking number", "The carrier's own reference for the shipment."),
    "received_by": ("Received by", "Who signed for it. Prefer the name on the proof of delivery "
                                   "over your own — Fill reads it off the POD without asking."),
    "vendor_name": ("Vendor", "Who supplied it. The receiver report fills this from the purchase "
                              "order when it is blank, so type one only to correct a wrong name."),
    "notification_number": ("Notification number",
                            "The warehouse's own inbound reference, where the message carries one. "
                            "Not the purchase order number."),
}

_EDIT_GROUPS = (
    ("What arrived", ("spec_code", "item_description", "quantity_received", "unit_of_measure",
                      "package_quantity", "package_uom"),
     "Everything already known is filled in. Change only what is wrong or missing."),
    ("How and when it arrived", ("pod_stated_date", "carrier_name", "tracking_number",
                                 "received_by"),
     "What the proof of delivery states. Fill takes the date and the signature off the POD itself "
     "and is the better route when there is one attached."),
    ("Who it came from", ("vendor_name", "notification_number"),
     "Blank is normal here — the receiver report fills the vendor from the purchase order."),
)
"""The editable fields in three named groups rather than one flat run of twelve boxes.

A form long enough to scroll is a form whose last field nobody reads, and these three questions —
what, when, from whom — are the ones a reviewer is actually answering. Built from
`extracted_store.EDITABLE_FIELDS` rather than replacing it: anything in the whitelist and not named
here is still rendered, in a final catch-all group, so widening the whitelist can never silently
produce a field with no box.
"""

_EDIT_RETURNS = {
    "manual": ("/ui/manual", "Needs a human"),
    "records": ("/ui/records", "Records"),
}
_EDIT_RETURN_DEFAULT = "manual"


def _edit_return(value) -> str:
    """Where Cancel, the header's Back link and a saved correction go — chosen from a fixed set.

    A key, never a URL. This form is reachable from two places and has to return to whichever it
    came from; the smallest thing that does that and cannot become an open redirect is a dictionary
    lookup with a default. Nothing a caller sends ever reaches a `Location` header or an `href` —
    only the two literals above do.
    """
    key = str(value or "").strip()
    return key if key in _EDIT_RETURNS else _EDIT_RETURN_DEFAULT


def _edit_form(conn, record_id: int, *, values=None, edited_by="", problem=None,
               return_to=_EDIT_RETURN_DEFAULT):
    """The record's own values, editable. `None` when there is no such record to correct."""
    row = _fixable_record_or_none(conn, record_id)
    if row is None:
        return None

    return_to = _edit_return(return_to)
    destination, back_label = _EDIT_RETURNS[return_to]

    if values is None:
        values = {name: ("" if row[name] is None else str(row[name]))
                  for name in extracted_store.EDITABLE_FIELDS}
        # Trailing `.0` is what a REAL column renders as, and it is noise in a box somebody is
        # about to retype. Both numeric fields, not just the quantity: `package_quantity` is the
        # same column type and reads `41.0` for the same reason.
        for name in ("quantity_received", "package_quantity"):
            if values.get(name, "").endswith(".0"):
                values[name] = values[name][:-2]
    values = dict(values)
    values["po_number"] = row["po_number"] or ""

    gaps = completeness.gaps(row)
    parts = []
    if problem is not None:
        parts.append(html.errors(problem.message, problem.errors))
    elif gaps.missing_required:
        parts.append(html.banner(f"This record is {gaps.describe()}.", kind="warn"))

    if record_edit.CONFLICT_MARKER in (row["extraction_source"] or ""):
        parts.append(html.banner(
            "Two sources stated different quantities for this delivery and neither was chosen. "
            "Setting the quantity here settles it — the record is then treated as any other.",
            kind="warn"))

    def _box(name):
        label, hint = _EDIT_LABELS.get(name, (completeness.LABELS.get(name, name), ""))
        return html.field(name, label, values.get(name, ""),
                          required=name in completeness.REQUIRED, hint=hint)

    # Two fields nobody may change, shown rather than hidden. A form that silently omits the two
    # facts a reviewer is most likely to want to correct reads as an oversight; one that shows them
    # greyed with a reason reads as a decision, which is what it is.
    parts.append(html.section(
        "What this record is against",
        html.field("po_number", "PO number", values.get("po_number", ""), readonly=True,
                   source="not editable here",
                   hint="Moving a delivery to a different purchase order would move its receipt "
                        "to a different budget line. That is a bigger act than a correction, so "
                        "it is not one this form can make."),
        html.field("po_line_number", "PO line #",
                   "" if row["po_line_number"] is None else str(row["po_line_number"]),
                   readonly=True, source="not editable here",
                   hint="Which line of the purchase order this is booked against. Typing a number "
                        "here would not suggest a line, it would choose one — posting stops "
                        "matching on spec code and books against whatever was typed, so a "
                        "transposed digit would satisfy the very check meant to catch it. Press "
                        "Verify and pick from the alternatives table instead, where each line's "
                        "description and outstanding quantity are shown."),
        note="Both come from the purchase order, and neither is something a correction may move."))

    grouped = set()
    for title, names, note in _EDIT_GROUPS:
        boxes = [_box(n) for n in names if n in extracted_store.EDITABLE_FIELDS]
        grouped.update(names)
        if boxes:
            parts.append(html.section(title, *boxes, note=note))
    # Anything whitelisted and not named in `_EDIT_GROUPS` — so widening the whitelist can
    # never produce a field the form quietly declines to show.
    rest = [_box(n) for n in extracted_store.EDITABLE_FIELDS if n not in grouped]
    if rest:
        parts.append(html.section("Other fields", *rest))

    parts.append(html.section(
        "You",
        html.field("edited_by", "Your name", edited_by, required=True,
                   hint="Recorded against this record and against every field you change, so a "
                        "hand-typed value is never mistaken later for one the machine read."),
    ))

    past = record_edit.history(conn, record_id)
    if past:
        parts.append(html.section("Already corrected", html.tag(
            "ul",
            *[html.tag("li",
                       html.muted(f"{e['edited_at']} · {e['edited_by']} · "),
                       f"{completeness.LABELS.get(e['field'], e['field'])}: "
                       f"{e['old_value'] or '(blank)'} → {e['new_value'] or '(blank)'}")
              for e in past],
            class_="plain-list"),
            note="Every field a person has changed on this record, newest first."))

    body = html.form(f"/ui/records/{record_id}/edit?from={return_to}", *parts,
                     submit="Save the correction",
                     cancel=destination, cancel_label=f"Back to {back_label}")
    email_id = row["source_email_id"] or ""
    if email_id:
        host = html.mail_url(email_id, "", html.DEFAULT_MAIL_SOURCE) + "&bare=1"
        body = html.split(body, html.mail_pane(
            host, _mail_fragment_html(email_id, src=html.DEFAULT_MAIL_SOURCE, bare=True),
            note="The message this record was read from. Open an attachment and it opens here, "
                 "beside the form, not over it."))

    return html.page(f"Correct record #{record_id}", destination, body,
                     back=destination, back_label=back_label, **_chrome(conn))


def _no_edit_page(conn, record_id: int, reason: str, return_to: str) -> str:
    """Why this record cannot be corrected, as a page rather than a bare 404 body."""
    destination, back_label = _EDIT_RETURNS[_edit_return(return_to)]
    return html.page(
        "Nothing to correct", destination,
        html.section("Nothing to correct",
                     html.tag("p", reason or
                              f"Record #{record_id} is not one this page can change — it has "
                              f"either been posted to Spitfire already or no longer exists.",
                              class_="note")),
        back=destination, back_label=back_label, **_chrome(conn))


@router.get("/records/{record_id}/edit", response_class=HTMLResponse)
def edit_record_form(record_id: int, from_: str = Query(_EDIT_RETURN_DEFAULT, alias="from"),
                     conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> HTMLResponse:
    row, reason = _editable_record_or_none(conn, record_id)
    if row is None:
        return HTMLResponse(status_code=404,
                            content=_no_edit_page(conn, record_id, reason, from_))
    return HTMLResponse(_edit_form(conn, record_id, return_to=from_))


@router.post("/records/{record_id}/edit", response_class=HTMLResponse)
async def edit_record(record_id: int, request: Request):
    """Save the correction, or come back carrying what was typed.

    Re-rendering the page on a refusal is what keeps a rejected value on screen with no JavaScript
    — the same reasoning the create form gives for being a page rather than a dialog.
    """
    posted = await _form_values(request)
    return_to = _edit_return(request.query_params.get("from", ""))
    conn = deps.pipeline_connection()
    try:
        row, reason = _editable_record_or_none(conn, record_id)
        if row is None:
            # The same refusal the GET gives, and it must be here too: the disabled button on the
            # Records page is a courtesy, and a form left open while somebody else posted the row
            # would otherwise still submit.
            return HTMLResponse(status_code=404,
                                content=_no_edit_page(conn, record_id, reason, return_to))
        fields = {name: posted.get(name, "") for name in extracted_store.EDITABLE_FIELDS}
        result = record_edit.apply(conn, row, fields, edited_by=posted.get("edited_by", ""))
        if not result.ok:
            return HTMLResponse(_edit_form(conn, record_id, values=fields,
                                           edited_by=posted.get("edited_by", ""),
                                           problem=result, return_to=return_to))
        # Back where the reviewer came from. A record corrected from Records belongs on Records
        # whether or not the edit closed its last gap — the row they just changed sitting
        # there is the confirmation that it worked. From the queue, a record that is now complete
        # has *left* that queue, so it goes to Records for the same reason.
        target = (f"/ui/records?corrected={record_id}"
                  if return_to == "records" or result.is_complete else "/ui/manual")
        # Unless the page said where it came from. `return_to` above is the *kind* of page this
        # edit was opened from (`?from=`); this is the exact one, filter and all.
        target = _safe_return(posted.get("return_to", ""), target)
    finally:
        conn.close()
    return RedirectResponse(target, status_code=303)


@router.get("/manual", response_class=HTMLResponse)
def manual_page(conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    """Everything a person has to deal with, in one queue, newest first.

    It was three tables — emails, then attachments, then records — which put a message from May
    above an attachment from this morning and made the top of the page the oldest thing on it. The
    three kinds are still distinguishable (the View dropdown narrows to one), but they are one
    queue because that is what they are: a list of work, in the order it arrived.
    """
    items = read_views.manual_queue(conn)
    # How many records a person has already built from each message, counted once for the page. Per
    # row it would be one query per row, and this queue is thousands of rows long.
    # Positional, because this connection's row_factory is whatever the last caller left it as.
    made_per_email = {
        row[0]: row[1] for row in conn.execute(
            "SELECT source_email_id, COUNT(*) FROM extracted_records "
            "WHERE origin = 'manual' GROUP BY source_email_id")}

    rows, kinds, reasons = [], [], []
    for i in items:
        rows.append([
            _arrival_cell(i),
            # The short reason is the whole sentence's headline, and the sentence is on hover.
            # A column of paragraphs cannot be scanned, and scanning is what a queue is for.
            #
            # A merged row badges the message's own reason and counts the rest. Naming the second
            # reason instead of counting them would be a third badge on some rows and not others,
            # and the number is what says whether the message is one problem or nine.
            html.tag("span", html.badge(_REASON_LABELS.get(i.code, "Needs a look"),
                                        _REASON_TONES.get(i.code, "plain")),
                     *((" ", html.muted(f"+{i.rolled_up - 1} more"))
                       if i.rolled_up > 1 else ()),
                     title=i.reason),
            _stamp(i.when[:16].replace("T", " ")),
            i.filed_to or html.muted("—"),
            # What was delivered. Its own column because it is the only thing that differs between
            # the 23 rows of a signage package — the other seven fields on those rows are
            # identical, so without this the page shows one row repeated 23 times.
            html.tag("span", (i.item or "—")[:58], class_="nw", title=i.item or "") if i.item
            else html.muted("—"),
            _po_cell(i),
            # The way out of this queue, and it differs by what the row *is*.
            #
            # A message or an attachment has no record yet, so the way out is to create one. A
            # record row already exists and cannot be created again — it needs its gaps closed,
            # which is what Fill and Verify do. That distinction used to be a dash: record rows,
            # 44 of the 62 on this page, were listed with **no action at all**, on the reasoning
            # that the fix lived on the Records page. It did — right up until a record with gaps
            # stopped appearing there, at which point the dash was the whole story and the gaps
            # were unreachable.
            #
            # A link for Create (a page to land on, harmless to prefetch); `verify_button` for the
            # other two, which POST because they write.
            _queue_action(i, made_per_email),
        ])
        kinds.append(i.kind)
        # Every code on the row, space-separated, matched as tokens by the chip filter. A merged
        # message badged `routed` still has to appear when someone presses `Needs OCR`, because the
        # OCR failures are why they are looking and the message is now the only row holding them.
        reasons.append(" ".join(i.codes or (i.code,)))

    # Counted per code, so a row that carries two is counted under both. These therefore no longer
    # sum to the number of rows — `html.reason_filter` says so on hover.
    counts = Counter(code for i in items for code in (i.codes or (i.code,)))
    # The queue alone. The chatter table used to sit at the bottom of this page and be searched
    # from here with it — "did a real delivery get filed as chatter?" is a question that brings
    # people here — but it has its own page now, and its own search box on it.
    searched = ["manual-table"]
    body = html.section(
        "",
        html.tag(
            "div",
            html.search_box(searched, placeholder="Search PO, subject, sender, reason…",
                            label="Search everything needing a person"),
            html.date_filter(searched, label="date it arrived", presets=True),
            html.choice_filter("manual-table",
                               # "Messages", not "Emails": a message that yielded nothing is now one
                               # row standing for its bad attachments too, so this option answers
                               # "which messages need me" rather than "which rows are emails". The
                               # attachment rows that survive are the ones on mail that *did*
                               # produce records, which is a narrower thing than it used to be and
                               # is named as such.
                               [(kind, label) for kind, label in
                                (("email", "Messages"),
                                 ("attachment", "Attachments on recorded mail"),
                                 ("record", "Records"))
                                if kind in kinds],
                               label="View", all_label="Everything waiting", boxed=True),
            class_="controls",
        ),
        # Every reason, including the ones at zero — see `html.reason_filter`.
        html.reason_filter("manual-table",
                           [(code, label, counts.get(code, 0))
                            for code, label in _REASON_LABELS.items()]),
        html.table(
            # No Detail column: it is the line under the subject already. It was both, and every
            # record row read "via pdf:carrier_pod" twice, a few centimetres apart.
            ["What arrived", "Reason", "When", "Filed to", "Item", "PO", ""],
            rows, empty=_EMPTY_HINT, table_id="manual-table", page_size=25, pane=True,
            date_column="When", no_sort=("",),
            # Every row, as this page has always sent. It is the argument that eventually
            # retired the cap everywhere: this is the page people search by hand for a message
            # from weeks ago, the search box only ever sees what was sent, and a capped table
            # answered "No rows match — 500 hidden" for a 299-row message that was simply older
            # than the newest 500. Measured 2026-09-15, capping saved well under a second of load
            # (3.1 s → 3.9 s for 3,782 rows) because `manual_queue` builds every item either way.
            choice_values=kinds, reason_values=reasons,
            # So a bare number in the search box means "this PO", not "this text appears
            # somewhere in the row" — see `termMatches` in html.py.
            po_values=[i.po_number for i in items],
            # Which message each row came from, so a verdict taken in the popup can take its rows
            # off this page without reloading it — the message's own row, its attachments and every
            # record read out of it, all at once.
            email_ids=[i.email_id for i in items],
            # `frag_urls`, not `mail_ids`: the convenience path cannot express `src`, and without
            # it `_store_for` resolves to the retired `.msg` corpus — so every row on this page
            # reported its message missing, having searched a folder of test files for Premier's
            # live mail.
            frag_urls=[html.mail_url(i.email_id, i.reason) for i in items],
            frag_title="Open this message",
        ),
    )
    sections = [body]
    sections.extend(_awaiting_report(conn))
    sections.extend(_posted_receipts(conn))
    sections.extend(_stranded_posts(conn))
    sections.extend(_blocked_from_posting(conn))
    sections.append(_filtered_pointer(conn))
    return html.page(
        "Needs a human", "/ui/manual", *sections,
        subtitle="Triaged to a person, quarantined, or failed to process.",
        **_chrome(conn))


_REASON_LABELS = OrderedDict((
    ("no_po", "No PO"),
    ("qty_conflict", "Qty conflict"),
    ("incomplete", "Incomplete"),
    ("status_report", "Status report"),
    ("awaiting_confirmation", "Awaiting confirmation"),
    ("needs_ocr", "Needs OCR"),
    ("nothing_recognised", "Nothing recognised"),
    ("corrupt", "Corrupt"),
    ("unreadable", "Unreadable"),
    ("nothing_extracted", "Nothing extracted"),
    ("duplicate", "Duplicate"),
    ("overridden", "Set aside in error"),
    ("maybe_advertising", "Maybe advertising"),
    ("routed", "Routed"),
    ("quarantined", "Quarantined"),
    ("error", "Error"),
))
"""`ManualItem.code` in the words the chips and the Reason column use.

Ordered by what a person would work through first — the records that are nearly there, then the
files nothing could read, then the mail that was only ever routed to a person. The order is the
order of the chips, so it is a statement about priority and not just about layout.
"""

_REASON_TONES = {
    "no_po": "hold", "qty_conflict": "hold", "incomplete": "hold",
    # Plain, like `maybe_advertising`: nothing failed. The document was read perfectly and is
    # simply not a delivery document, and the only thing left is for somebody to say so.
    "status_report": "plain",
    "needs_ocr": "route", "nothing_recognised": "hold",
    "corrupt": "error", "unreadable": "error",
    "nothing_extracted": "hold", "duplicate": "plain",
    # Waiting work, not a failure: a person has personally said this message is a delivery and
    # nothing has been recorded from it yet.
    "overridden": "hold",
    # Plain, not a warning colour: nothing has gone wrong, somebody just has to say which
    # of the two it is.
    "maybe_advertising": "plain",
    "routed": "plain", "quarantined": "error", "error": "error",
}


def _po_cell(item) -> html.Raw:
    """The purchase order, and the line on it once that is known: `907514 : 13`.

    A PO on its own does not identify a delivery. PO 907514 carries 29 lines that all read
    `LOB-900-SI`, so its 23 queued rows each said "907514" and nothing that distinguished them —
    correctly, since all 23 really are on that order. The line is what says *which item*, and it is
    also what Spitfire needs to post the receipt against the right budget line.

    Still the control that opens the mail the record was read from, so the link text changes and
    nothing else does.
    """
    if not item.po_number:
        return html.muted("—")
    label = item.po_number
    if item.po_line_number is not None:
        label = f"{item.po_number} : {item.po_line_number}"
    return html.mail_link(item.email_id, label, reason=item.reason)


def _arrival_cell(item) -> html.Raw:
    """What arrived, over who or what it came from.

    Same shape as the Mail page's subject cell and for the same reason: the sender was a column of
    its own, one long address set its width, and the subject — the thing anyone scans for — was
    squeezed beside it.
    """
    what = (item.subject or item.ref or "").strip() or "(no subject)"
    shown = what if len(what) <= 48 else what[:48].rstrip() + "…"
    under = f"from {_short_address(item.sender)}" if item.sender else item.detail
    return html.tag(
        "div",
        html.tag("span", shown, class_="subj nw", title=what),
        html.tag("span", (under or "—")[:52], class_="from nw", title=item.detail or ""),
        class_="cell-subject",
    )


def _queue_action(item, made=None) -> html.Raw:
    """What this queue row offers a person: create a record, or close an existing record's gaps.

    Fill takes what the proof of delivery already asserts — the delivery date, who signed for it —
    rather than asking anyone to retype it, so the record and the file posted beside it cannot
    disagree. Verify opens the comparison against the purchase order, which is where a spec code or
    a quantity gets corrected from the order itself.

    Both are the same controls the Records page carries; the record simply is not on that page any
    more, so they had to come here with it.
    """
    if item.code == "awaiting_confirmation":
        # Records already exist on this message — a great many of them — so Create a record is the
        # one thing this row must not offer. The question here is not what is missing, it is
        # whether what the message claims actually happened, and only a person can answer it.
        return html.Raw(str(_verdict_link(
            item.email_id, mail_overrides.CONFIRMED, "Confirm",
            f"Confirm the goods arrived and send {item.rolled_up} record"
            f"{'' if item.rolled_up == 1 else 's'} to Records")) + " " + str(_verdict_link(
                item.email_id, mail_overrides.NOT_DELIVERY, "Not a delivery",
                "Say this message is not a delivery notification, and take it off this queue")))
    if item.kind != "record" or not item.ref_id:
        # Two ways out of a message row, and they are the two answers to the same question. Either
        # it is a delivery and the record is what is missing, or it is not one and it should never
        # have been here — and until now only the first of those could be said.
        create = _create_link(item.email_id, made.get(item.email_id, 0) if made else 0)
        if not item.email_id:
            return create
        return html.Raw(str(create) + " " + str(_verdict_link(
            item.email_id, mail_overrides.NOT_DELIVERY, "Not a delivery",
            "Say this message is not a delivery notification, and take it off this queue")))
    # `Fix` leads, and `Fill` follows it, because that is the order they are useful in. Measured on
    # this queue: of the twenty-one incomplete records, seventeen are missing a spec code, sixteen
    # a description and sixteen a quantity — **none of which any proof of delivery contains**. Fill
    # can only supply the delivery date and the signature, which covers fourteen. Leading with the
    # button that cannot answer the commonest question is what made these rows look unfixable.
    return html.Raw(
        str(html.tag("a", "Fix", href=f"/ui/records/{item.ref_id}/edit?from=manual",
                     class_="btn small",
                     title="Open this record and correct what is missing"))
        + " "
        + str(html.verify_button(f"/ui/records/{item.ref_id}/complete", "Fill", small=True,
                                 ghost=True,
                                 title="Take the delivery date and signature off the proof"))
        + " "
        + str(html.verify_button(f"/ui/records/{item.ref_id}/verify", "Verify", small=True,
                                 ghost=True, title="Compare against the purchase order")))


def _create_link(email_id, made: int = 0) -> html.Raw:
    """"Create a record" for one message, or nothing if there is no message to build it from.

    `made` is how many records a person has already built from this message. It only changes the
    words: the destination is the same form either way, and "Create a record" on a message already
    carrying two of them reads as though the first two did not happen.
    """
    if not email_id:
        return html.muted("—")
    return html.tag("a", "Create another record" if made else "Create a record",
                    class_="btn ghost small",
                    href=f"/ui/records/new?email_id={quote(str(email_id))}",
                    title=(f"{made} already recorded from this message by hand — add another"
                           if made else
                           "Record this delivery by hand, from what this message says"))


def title_for_frag(verdict: str) -> str:
    """The popup's heading for a reclassify form — the same words as the page's own title."""
    return _VERDICT_COPY.get(verdict, {}).get("title", "This message")


def _verdict_link(email_id, verdict: str, label: str, title: str) -> html.Raw:
    """The way to the confirm form for overruling triage about one message.

    A link and not a `button_form`, because the destination is a page to land on and is harmless for
    a prefetch to follow — the write is behind that page's own POST. The id rides in the query
    string, never a path segment, for the reason `mails_acknowledge` gives: a Message-ID is up to
    255 characters of angle brackets and `@`.
    """
    url = f"/ui/mail/verdict?id={quote(str(email_id))}&to={quote(verdict)}"
    # `data-frag` opens the same form in the popup already over the queue, so the decision is taken
    # without the page going anywhere and the rows it retires leave the table in place. The `href`
    # is unchanged and is what a browser with no script follows.
    return html.tag("a", label, class_="btn ghost small", title=title, href=url,
                    data_frag=f"{url}&inline=1", data_frag_title=title_for_frag(verdict))


def _filtered_pointer(conn: sqlite3.Connection) -> html.Raw:
    """Where the set-aside mail went, in one line.

    The table itself used to sit here, at the bottom of a page whose whole subject is work waiting.
    It was never work — nothing on it is queued, and its own note says so — and at 152 rows it was
    the longest thing on the page. It now has a page of its own, where the rule that set each
    message aside and the person who disagreed can both be read, and where either can be changed.

    The count is still here because "is a real delivery being filed as chatter?" is a question that
    brings people to this page, and a link with no number on it does not invite anyone to look.
    """
    total = read_views.filtered_count(conn)
    return html.section(
        "Not a delivery mail",
        html.tag("p",
                 f"{total} message{'' if total == 1 else 's'} set aside as not a delivery — by a "
                 f"triage rule, or by a person. Not queued, not deleted, and not counted as work. ",
                 html.tag("a", "Open the list", href="/ui/not-deliveries"),
                 class_="note"),
    )


@router.get("/not-deliveries", response_class=HTMLResponse)
def not_deliveries_page(conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    """Everything taken out of the queue as not a delivery notification, and by whom.

    Twelve of the fourteen messages this began with were all-associates broadcasts and calendar
    invites, each stating "no PO reference found anywhere in the thread". Every one was true, and
    together they buried the two entries that were real work. They are listed and not deleted
    because a suppression nobody can inspect is a suppression nobody can trust.

    Two populations, on purpose. A rule set most of these aside and is named on the row; a person
    set the rest aside and is named on theirs. Both columns stay because a rule can be inspected and
    a person can be asked, which are not the same kind of accountability — and either can be
    overturned from the last column.
    """
    filtered = read_views.filtered_mail(conn)
    rows = []
    for m in filtered:
        who = (m["decided_by"] or "").strip()
        rows.append([
            _clipped(m["subject"] or m["email_id"], 58),
            m["sender"] or html.muted("—"),
            html.when((m["processed_at"] or "")[:16]),
            _stamp(m["matched_rule"] or ""),
            # The person's reason when there is one: they were looking at the message, and the rule
            # they overruled has already said its piece in the column before this.
            _clipped(m["decided_note"] or m["reason"], 66),
            (html.tag("span", html.badge(who, "hold"),
                      title=f"set aside by hand on "
                            f"{(m['decided_at'] or '')[:16].replace('T', ' ')}")
             if who else html.muted("triage")),
            _verdict_link(m["email_id"], mail_overrides.DELIVERY, "This is a delivery",
                          "Put this message back on the queue as a real delivery"),
        ])
    body = html.section(
        f"Not a delivery mail ({len(filtered)})",
        html.tag(
            "div",
            html.search_box(["not-deliveries-table"],
                            placeholder="Search subject, sender, rule, name…",
                            label="Search set-aside mail"),
            html.date_filter(["not-deliveries-table"], label="date it arrived", presets=True),
            class_="controls",
        ),
        html.table(["Subject", "From", "When", "Rule", "Why it was set aside", "Set aside by", ""],
                   rows, empty="Nothing has been set aside.", table_id="not-deliveries-table",
                   page_size=25, pane=True, date_column="When", no_sort=("",),
                   # Every row — see the same note on `manual_page`. 1,269 rows are 1.7 MB and
                   # 0.19 s here; capping saved nothing and hid the mail people come to look for.
                   frag_urls=[html.mail_url(m["email_id"], m["decided_note"] or m["reason"] or "")
                              for m in filtered],
                   # A message put back on the queue leaves this page in the same press, without
                   # the reader losing their search of 1,269 rows.
                   email_ids=[m["email_id"] for m in filtered],
                   frag_title="Open this message"),
        note="Not queued, not deleted, and not counted as work. A triage rule identified most of "
             "these as something other than a delivery notification: internal chatter, a carrier "
             "status notice, a scheduled report, or a thread whose every hop declines to say goods "
             "arrived. The rule that decided it is named on each row, and a person may have "
             "overruled it either way. If a real delivery is here, the rule is wrong — open it, "
             "then say so with the last column.",
    )
    return html.page(
        "Not a delivery mail", "/ui/not-deliveries", body,
        subtitle="Set aside by a triage rule or by a person, and never deleted.",
        **_chrome(conn))


@router.get("/cancellations", response_class=HTMLResponse)
def cancellations_page(conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    """Purchase orders a cancellation notice named, and whether Spitfire has been told.

    Triage already routes these out of the delivery path — rule 0a, "needs a manual PO update in
    Spitfire, not a delivery event" — and that is where they stopped. Measured 2026-09-16: 66
    notices, 7 naming a purchase order, none of those 7 marked Canceled in Spitfire.

    **This page does not write to Spitfire, and that is deliberate.** Cancelling a purchase order
    is `PATCH /api/document/{id}/Status`, which `connectors/spitfire_write._DENIED_SUBSTRINGS`
    refuses by name: the same call marks a receipt POD Confirmed with no review and no Fixed-Asset
    Accounting sign-off. Receiving goods records something that happened; cancelling retires a
    commitment, so it stays a person's to make. `done` is read back from the mirror, so an order
    cancelled directly in Spitfire leaves this list on its own.

    **The notice is evidence, not a verdict.** `CANCELLATION_RE` is a keyword match over subject
    and body, and two of the sixty-six are Cintas out-of-office replies on a thread whose subject
    says FINAL NOTICE — they name two purchase orders that Spitfire still holds as Committed with
    69 pending records between them. Which is exactly why the notice's subject and sender are
    columns here rather than a hidden reason: a reviewer must be able to see that it was an
    automatic reply before acting on it.
    """
    work = read_views.cancellation_worklist(conn)
    unnamed = read_views.cancellations_without_a_po(conn)

    rows = []
    for item in work:
        if item["done"] is True:
            state = html.badge("Canceled in Spitfire", "good")
        elif item["done"] is False:
            state = html.tag("span", html.badge(item["spitfire_status"] or "open", "hold"),
                             title="Spitfire still holds this order — the cancellation has not "
                                   "been applied")
        else:
            state = html.tag("span", html.muted("not read yet"),
                             title="this purchase order has never been read from Spitfire, so "
                                   "nothing here knows what it holds")
        rows.append([
            html.token(item["po_number"]),
            html.when(item["cancelled_at"]),
            _clipped(item["subject"], 62),
            _clipped(item["sender"], 30),
            state,
            (html.tag("a", f"{item['queued']} queued", class_="btn ghost small",
                      href=f"/ui/records?q={item['po_number']}",
                      title="records still pending against this order — a cancelled order with "
                            "rows on Records is how goods get received against something nobody "
                            "is buying any more")
             if item["queued"] else html.muted("—")),
        ])

    outstanding = sum(1 for item in work if item["done"] is not True)
    body = html.section(
        f"Cancellation notices ({outstanding} outstanding of {len(work)})",
        html.table(["PO", "Notice arrived", "Subject", "From", "Spitfire", "Still queued"],
                   rows, empty="No cancellation notice names a purchase order.",
                   table_id="cancellations-table", page_size=25, pane=True,
                   date_column="Notice arrived", no_sort=("Still queued",),
                   frag_urls=[html.mail_url(item["email_id"], item["subject"]) for item in work],
                   email_ids=[item["email_id"] for item in work],
                   frag_title="Open this notice"),
        note="Read-only. Nothing here is sent to Spitfire: cancelling a purchase order retires a "
             "commitment rather than recording something that happened, and the API call that "
             "does it is the same one that would mark a receipt approved without review — so it "
             "is refused by name in the write client. Open the notice, check it actually says the "
             "order is cancelled, then close the order out in Spitfire. The row clears itself "
             "once the mirror shows it Canceled."
             + (f" {unnamed} further notice(s) name no purchase order and are not listed — there "
                f"is nothing to act on until somebody reads them." if unnamed else ""),
    )
    return html.page(
        "Cancellations", "/ui/cancellations", body,
        subtitle="Orders a cancellation notice named, and whether Spitfire has been told.",
        **_chrome(conn))


@router.get("/po", response_class=HTMLResponse)
def po_page(conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    """Where each purchase order with a final receipt in Spitfire stands.

    Only purchase orders whose final receipt was submitted (Premier, 2026-09-15). Every other PO the mail mentions is still
    reachable from Records and from `/ui/po/{po}`.
    """
    pos = _posted_pos(conn)
    rows = []
    for p in pos:
        notices = ", ".join(
            f"{t.replace('_', ' ')}" + (f" ×{n}" if n > 1 else "") for t, n in sorted(p.notifications.items())
        )
        # The reason, with the moment it happened on hover. Both were in the cell, stacked, which
        # made every row on the page three lines tall to carry a timestamp that is already the
        # subject of the Last heard column beside it.
        released = (
            html.tag("span", p.release_reason or "released", class_="muted nw",
                     title=f"Released {p.released_at[:16]}")
            if p.released_at else html.muted("—")
        )
        rows.append([
            html.tag("a", p.po_number, href=f"/ui/po/{p.po_number}"),
            html.badge(p.label, p.status),
            _clipped(notices, 46),
            p.records or html.muted("0"),
            p.lines_seen or html.muted("—"),
            _num(p.qty_received) or html.muted("—"),
            # None, not 0 — the Spitfire mirror is empty, and a zero here would read as
            # "nothing was ordered" rather than "we have not been told". They fill in once the
            # Spitfire read has mirrored the PO lines.
            _num(p.qty_ordered) if p.qty_ordered is not None else html.muted("—"),
            _num(p.qty_outstanding) if p.qty_outstanding is not None else html.muted("—"),
            released,
            _stamp(p.last_seen[:16]),
        ])

    # Built from the statuses actually on the page, like Mail's verdicts — offering a status
    # nothing carries is a filter whose only possible outcome is an empty table. The cell holds the
    # label, so that is what the option has to match.
    labels = sorted({p.label for p in pos if p.label})
    # Everything that has not arrived, plus loss-or-claim, which never will and needs a claim filed
    # against the carrier. Cancelled is left out on purpose: there is nothing to chase.
    waiting_statuses = {delivery_status.STATUS_LABELS[s]
                        for s in (delivery_status.OPEN, delivery_status.IN_TRANSIT,
                                  delivery_status.AT_WAREHOUSE, delivery_status.LOSS_OR_CLAIM)}
    waiting = [label for label in labels if label in waiting_statuses]
    views = ([("|".join(waiting), "My triage queue")] if waiting else [])
    views += [(label, label) for label in labels]

    body = html.section(
        "",
        html.tag(
            "div",
            html.search_box("po-table", placeholder="Search PO, status, date…",
                            label="Search purchase orders"),
            html.date_filter("po-table", label="last heard", presets=True),
            html.choice_filter("po-table", views, label="View",
                               all_label="All purchase orders", boxed=True),
            class_="controls",
        ),
        html.table(
            ["PO", "Status", "Notifications", "Records", "Lines", "Received",
             "Ordered", "Outstanding", "Released", "Last heard"],
            rows, empty=_EMPTY_HINT, table_id="po-table", page_size=25, pane=True,
            date_column="Last heard", choice_column="Status",
            num_columns=("Records", "Lines", "Received", "Ordered", "Outstanding"),
        ),
    )
    return html.page(
        "Delivery status", "/ui/po", body,
        subtitle="Purchase orders with a final receipt submitted to Spitfire. Status is inferred "
                 "from the notifications received, not recorded.",
        actions=[_export_po()],
        **_chrome(conn))


def _posted_pos(conn: sqlite3.Connection) -> list:
    """`po_delivery_status`, narrowed to purchase orders with a final receipt in Spitfire."""
    posted = post_ledger.posted_po_numbers(conn)
    return [p for p in read_views.po_delivery_status(conn) if str(p.po_number).strip() in posted]


def _export_po() -> html.Raw:
    """Every purchase order this page knows about, as a file. See `_export_mail`."""
    return html.tag("a", html.DOWNLOAD_ICON, "Export", href="/ui/po.csv", class_="btn small",
                    title="Download every row of this table as CSV")


_PO_CSV_COLUMNS = ("PO", "Status", "Notifications", "Records", "Lines", "Received", "Ordered",
                   "Outstanding", "Released", "Released at", "Last heard")


@router.get("/po.csv")
def po_csv(conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> Response:
    """The Delivery status table as a file, whole and untruncated — see `mails_csv`.

    Two columns where the page has one: the release reason is what the cell shows and the moment it
    happened is what its `title` carries, and a file has no hover.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(_PO_CSV_COLUMNS)
    for p in _posted_pos(conn):
        notices = ", ".join(
            f"{t.replace('_', ' ')}" + (f" ×{n}" if n > 1 else "")
            for t, n in sorted(p.notifications.items())
        )
        writer.writerow(_csv_safe([
            p.po_number, p.label, notices, p.records, p.lines_seen, p.qty_received,
            "" if p.qty_ordered is None else p.qty_ordered,
            "" if p.qty_outstanding is None else p.qty_outstanding,
            p.release_reason if p.released_at else "",
            p.released_at[:16] if p.released_at else "",
            p.last_seen[:16],
        ]))
    return Response(
        buffer.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="premier-delivery-status.csv"',
                 "X-Content-Type-Options": "nosniff"},
    )


def _size(value) -> str:
    """Bytes a person can read at a glance. The corpus runs from a 1 KB logo to a 3 MB photograph,
    and `3236138` in a column does not communicate the difference."""
    size = int(value or 0)
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


def _po_evidence(row) -> html.Raw:
    """Which purchase order(s) this attachment belongs to, and on whose word.

    Three sources, and the difference between them is the point rather than a detail:

      * **the file itself** names the PO — `parse_pod` read it out of the document. This is the
        only one strong enough to hang the file on a receipt in Premier's ERP, and the only one
        `spitfire_post._pod_for` will act on.
      * **the email** names it (`email_log.po_hints`). The message is about that order; the
        attachment merely arrived with it.
      * **the records** extracted from that message name it. Weakest of the three — one tracker
        spreadsheet reaches five purchase orders this way, on nothing but the envelope.

    Only the strongest tier present is shown in full. The rest collapse to a count with the detail
    on hover, because at fifty-five rows a column that lists everything is a column nobody reads.
    """
    def split(value):
        return [p.strip() for p in str(value or "").replace(";", ",").split(",") if p.strip()]

    in_file = split(row["pod_po_numbers"])
    on_email = [p for p in split(row["email_po_hints"]) if p not in in_file]
    via_records = [p for p in split(row["pos_via_records"])
                   if p not in in_file and p not in on_email]

    if in_file:
        weaker = len(on_email) + len(via_records)
        parts = [html.token(", ".join(in_file))]
        if weaker:
            parts.append(html.muted(f" +{weaker} more on the email"))
        return html.Raw("".join(str(p) for p in parts))
    if on_email:
        shown = ", ".join(on_email[:2]) + (f" +{len(on_email) - 2}" if len(on_email) > 2 else "")
        return html.tag("span", html.muted(f"email: {shown}"),
                        title=f"the email names {', '.join(on_email)}; the file itself names none")
    if via_records:
        shown = ", ".join(via_records[:2]) + (f" +{len(via_records) - 2}"
                                              if len(via_records) > 2 else "")
        return html.tag("span", html.muted(f"records: {shown}"),
                        title=(f"records from this email name {', '.join(via_records)}; neither "
                               f"the file nor the email names a purchase order"))
    return html.muted("—")


_ATTACHMENT_DISPOSITIONS = {
    "extracted": "good",
    "dropped_decorative": "hide",
    "dropped_duplicate": "hold",
    "corrupt": "error",
    "empty": "hold",
    "not_dispatched": "plain",
}


_ATTACHMENT_VIEWS = {
    "file": "Real files",
    "inline": "Inline images",
    "all": "All attachments",
}
"""Which rows the Attachments page loads. A view, not a filter: the browser never receives the
rows a view leaves out, which is the entire point — see `attachments_page`."""


def _attachment_view_links(current: str, counts: dict, place: dict) -> html.Raw:
    """The three row sets, each with how many rows it holds, current one marked.

    Printed rather than hidden in a dropdown because the default no longer shows everything, and a
    page that quietly holds back 27,393 rows owes the reader both the number and the way to them.

    `place` carries every other control's setting into the link. These used to be bare
    `?view=inline`, which silently threw away the search, the sort and the date range the moment
    you changed view -- so looking for one filename across both sets meant typing it again.
    `page` is deliberately dropped: page 400 of one view is not page 400 of another.
    """
    parts = []
    for value, label in _ATTACHMENT_VIEWS.items():
        count = counts.get(value)
        text = f"{label} ({count:,})" if count is not None else label
        if value == current:
            parts.append(html.tag("span", text, class_="chip chip-on", aria_current="true"))
        else:
            href = "/ui/attachments" + html.query_string(
                {**place, "view": value if value != "file" else "", "page": ""})
            parts.append(html.tag("a", text, href=href, class_="chip"))
    return html.Raw(" ".join(str(p) for p in parts))


_ATTACHMENT_DAYS = (("Today", "0"), ("7d", "7"), ("30d", "30"), ("All", ""))


def _attachment_date_links(days: str, place: dict) -> html.Raw:
    """Today / 7d / 30d / All, as links that narrow the query.

    These were `html.date_filter` chips, which hide rows the browser is already holding. On a table
    the browser only ever sees 25 rows of, that control could not do anything at all -- it would
    have narrowed the current page and reported nothing about the rest. Same four choices, asked of
    the database.
    """
    chips = []
    for label, value in _ATTACHMENT_DAYS:
        if value == days:
            chips.append(html.tag("span", label, class_="chip chip-on", aria_current="true"))
        else:
            href = "/ui/attachments" + html.query_string({**place, "days": value, "page": ""})
            chips.append(html.tag("a", label, href=href, class_="chip"))
    return html.tag("div", *chips, class_="chips")


def _since_for(days: str) -> str:
    """The `first_seen_at` floor for a preset, or "" for all of time.

    Date-only, and compared as a string: `first_seen_at` is ISO-8601, so `>= '2026-09-16'` is every
    instant on the 16th and after. "0" means today, which is a floor of today's date -- not of
    "now", which would exclude everything that arrived this morning.
    """
    if not days:
        return ""
    try:
        back = int(days)
    except ValueError:
        return ""
    return (datetime.now() - timedelta(days=back)).strftime("%Y-%m-%d")


@router.get("/attachments", response_class=HTMLResponse)
def attachments_page(view: str = "file", q: str = "", page: int = 1, size: int = 25,
                     sort: str = "", dir: str = "desc", state: str = "", days: str = "",
                     conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> str:
    """Every file taken off every email, with what we know about each one.

    The only page that answers "what have we actually got?". `/ui/manual` lists the attachments
    needing attention and the mail dialog lists one message's worth; neither can show that the same
    bytes arrived twice under different filenames, which is exactly what the two FedEx PDFs on the
    910634 thread do.

    **This table is searched, sorted and paged by the database, not by the browser**, and it is the
    only one in the app that is. Every other page ships every row it stands for, so a filter in the
    browser is filtering everything there is. This one cannot: signature logos do not merely
    outnumber the real files, they are 27,393 of 29,766 on Premier's live store, and sending all of
    them so the browser could hide all but 25 cost 25 MB of HTML and 4.2 seconds before the page
    began to paint.

    What that used to buy was a cap and an apology — the newest 500 rows and a note underneath
    reading "the search and filters cover these 500". This asks the server instead, so the search
    box covers all 29,766 and the pager says where in them you are. Nothing is hidden, and nothing
    has to be loaded to be found.

    Every one of these parameters is validated against a whitelist before it reaches a query;
    `sort` in particular picks a key in `read_views.ATTACHMENT_SORTS` and never becomes SQL.
    """
    view = view if view in _ATTACHMENT_VIEWS else "file"
    size = size if size in html.SERVER_PAGE_SIZES else 25
    sort = sort if sort in read_views.ATTACHMENT_SORTS else ""
    state = state if state == "unread" else ""
    days = days if days in {value for _label, value in _ATTACHMENT_DAYS} else ""
    descending = dir != "asc"
    page = max(1, page)

    narrowing = {
        "q": q,
        "include_inline": view in ("all", "inline"),
        "inline_only": view == "inline",
        "unread_only": state == "unread",
        "since": _since_for(days),
    }
    matched = read_views.attachment_count(conn, **narrowing)
    # Clamp before querying rather than after: `?page=99999` should show the last page, not an
    # empty table that still claims 29,766 rows exist.
    pages = max(1, -(-matched // size))
    page = min(page, pages)
    rows_in = read_views.attachments(conn, limit=size, offset=(page - 1) * size,
                                     sort=sort, descending=descending, **narrowing)

    rows = []
    for a in rows_in:
        pod = (html.Raw(str(html.badge("POD", "pod")) + " "
                        + str(html.token(a["pod_po_numbers"] or "unnamed")))
               if a["is_pod"] else html.muted("—"))
        asserts = " · ".join(x for x in (a["pod_delivery_date"], a["pod_signed_by"]) if x)
        stored = (html.token(a["stored_sha"][:10]) if a["stored_sha"]
                  else html.badge("not stored", "error"))
        # `open_url`, never `view`. This was named `view`, which is also this page's own `?view=`
        # parameter — so every row overwrote it, and after the loop the view tabs and the "Load all"
        # button were built from the last attachment's viewer URL: the button went to
        # `/ui/attachments?view=/ui/mail/attachment/view…&all=1`. Found 2026-09-15, when the search
        # readout started offering that same link.
        open_url = f"/ui/mail/attachment/view?id={quote(a['email_id'])}&n={a['ordinal']}&src=inbox"
        download = (f"/ui/mail/attachment?id={quote(a['email_id'])}&n={a['ordinal']}"
                    f"&src=inbox&download=1")
        rows.append([
            a["id"],
            _stamp((a["email_date"] or a["first_seen_at"] or "")[:16]),
            # Filename over what it weighs and what became of it. Size was its own column and is
            # not worth one: nobody scans a table by kilobytes, but "1.2 MB · decorative" beside a
            # filename is the whole story of that row in six words.
            _file_cell(a, open_url),
            html.muted((a["sniffed_kind"] or "unknown").upper()),
            _po_evidence(a),
            a["records_extracted"] or html.muted("0"),
            stored,
            html.badge("inline", "hide") if a["is_inline"] else html.muted("—"),
            pod,
            _clipped(asserts, 26),
            html.badge((a["disposition"] or "").replace("_", " "),
                       _ATTACHMENT_DISPOSITIONS.get(a["disposition"], "plain")),
            a["claimed_by"] or html.muted("—"),
            html.mail_link(a["email_id"], _clipped(a["email_subject"] or "(no subject)", 34),
                           title="Open the email this arrived on"),
            # View before Download, and both visible. The filename opens the file too, but a
            # filename rendered as text carries no affordance — the page read as download-only
            # even though the in-page viewer was there all along. The envelope is the third of the
            # same kind: the file, the bytes, the message it came on.
            html.Raw(
                str(html.tag("button", "View", type="button", class_="btn ghost small",
                             data_frag=open_url, title="Open this file in the page"))
                + " "
                + str(html.tag("a", "Download", href=download, class_="btn ghost small"))
                + " "
                + str(html.mail_link(a["email_id"], "✉", cls="btn ghost small",
                                     title="Open the email this arrived on"))),
        ])

    total_attachments = read_views.attachment_count(conn)
    real_attachments = read_views.attachment_count(conn, include_inline=False)

    # What every control other than itself is currently set to, so each one carries the rest along.
    # Pressing Search must not drop the view you were in, and sorting must not drop your search.
    place = {"view": view if view != "file" else "", "q": q, "size": size if size != 25 else "",
             "sort": sort, "dir": "" if descending else "asc", "state": state, "days": days}

    def sort_url(key: str) -> str:
        # Clicking the column you are already sorted by turns it around; clicking another starts
        # that one at its natural end -- newest first for a date, A-Z for a name.
        turning = key == sort or (not sort and key == "received")
        way = "asc" if (turning and descending) else ("" if turning else _ATTACHMENT_SORT_START[key])
        return "/ui/attachments" + html.query_string({**place, "sort": key, "dir": way, "page": ""})

    body = html.section(
        "",
        html.tag(
            "div",
            html.server_search("/ui/attachments", q, place, table_id="attachments-table",
                               placeholder="Search filename, PO, subject, sender…",
                               label="Search attachments"),
            _attachment_date_links(days, place),
            _attachment_state_links(state, place),
            _attachment_view_links(view, place=place, counts={
                "file": real_attachments,
                "inline": total_attachments - real_attachments,
                "all": total_attachments,
            }),
            class_="controls",
        ),
        html.table(
            ["#", "Received", "Filename", "Type", "PO", "Records", "Stored", "Inline", "POD",
             "POD says", "Disposition", "Read by", "From mail", ""],
            rows, empty=_no_attachments_hint(q, state, days),
            table_id="attachments-table", pane=True, num_columns=("Records",),
            # `page_size=0`: no client pager. The browser holds 25 rows, so there is nothing for it
            # to page through -- `server_pager` below is what moves between slices.
            page_size=0,
            sort_urls={"Received": sort_url("received"), "Filename": sort_url("filename"),
                       "Type": sort_url("type"), "Records": sort_url("records"),
                       "Disposition": sort_url("disposition"),
                       "From mail": sort_url("subject")},
            sorted_by=(_ATTACHMENT_SORT_HEADINGS.get(sort or "received", ""),
                       "desc" if descending else "asc"),
            frag_urls=[f"/ui/mail/attachment/view?id={quote(a['email_id'])}"
                       f"&n={a['ordinal']}&src=inbox" for a in rows_in],
            frag_title="Open this attachment",
        ),
        html.server_pager("/ui/attachments", place, table_id="attachments-table",
                          page=page, size=size, total=matched, unit="attachment"),
    )
    return html.page(
        "Attachments", "/ui/attachments", body,
        subtitle="Everything downloaded from the receiving mailbox, one row per file.",
        actions=[_export_attachments()],
        **_chrome(conn))


_DISPOSITION_WORDS = {
    "extracted": "read",
    "dropped_decorative": "decorative",
    "dropped_duplicate": "duplicate",
    "corrupt": "corrupt",
    "empty": "empty",
    "not_dispatched": "not read",
}
"""The disposition in one word, for the line under a filename.

Deliberately shorter than the Disposition column's own wording, which stays `dropped duplicate` in
full — this one sits in a cell with a filename above it and has to be read at a glance, not parsed.
"""


def _file_cell(a, view_url: str) -> html.Raw:
    """The filename, over what it weighs and what became of it.

    The filename opens the file. The row does too, but a filename that is not clickable reads as
    inert, and this is the cell a person aims at.
    """
    name = a["filename"] or "(unnamed)"
    shown = name if len(name) <= 46 else name[:46].rstrip() + "…"
    detail = _DISPOSITION_WORDS.get(a["disposition"] or "", "")
    sub = _size(a["size_bytes"]) + (f" · {detail}" if detail else "")
    return html.tag(
        "div",
        html.tag("button", shown, type="button", class_="link-btn subj nw",
                 data_frag=view_url, title=f"Open {name}"),
        html.tag("span", sub, class_="from nw",
                 title=a["disposition_detail"] or "how this file was handled"),
        class_="cell-subject",
    )


_ATTACHMENT_SORT_START = {
    # Which end a column starts at when you first click it. A date means "newest first"; a name
    # means A-Z. Getting this wrong is not an error, only an extra click every single time.
    "received": "", "records": "", "size": "",
    "filename": "asc", "type": "asc", "disposition": "asc", "subject": "asc",
}

_ATTACHMENT_SORT_HEADINGS = {
    "received": "Received", "filename": "Filename", "type": "Type", "records": "Records",
    "disposition": "Disposition", "subject": "From mail", "size": "Filename",
}


def _attachment_state_links(state: str, place: dict) -> html.Raw:
    """"Everything" / "Nothing could be read", as links rather than a dropdown.

    It was a `choice_filter`, which matches against `data-choice-value` on rows the browser is
    holding -- so it could only ever narrow the 25 rows on screen. As a link it narrows the query,
    and the count it produces is the count of every unreadable file in the ledger.
    """
    options = (("", "Everything"), ("unread", "Nothing could be read"))
    links = [
        html.tag("span", label, class_="chip on") if value == state else
        html.tag("a", label, class_="chip",
                 href="/ui/attachments" + html.query_string({**place, "state": value, "page": ""}))
        for value, label in options
    ]
    return html.tag("div", *links, class_="chips")


def _no_attachments_hint(q: str, state: str, days: str = "") -> str:
    """Why the table is empty, in terms of what was asked -- never a bare "nothing here".

    An empty table after a search is a different fact from an empty ledger, and saying the second
    when the first is true is how somebody concludes the file never arrived.
    """
    if q and state:
        return f"No unreadable attachment matches {q!r}."
    if q:
        return f"No attachment matches {q!r}."
    if state:
        return "Every attachment in this range was read."
    if days:
        return "Nothing arrived in this period."
    return "No attachments have been downloaded yet."


# `_attachment_state` lived here: it read each row in Python and handed the answer to a dropdown
# in the browser, which meant all 29,766 rows had to be rendered before one could be filtered out.
# The same union is now `read_views.UNREAD_CLAUSE`, evaluated in SQL where the rows are, and the
# control that drives it is `_attachment_state_links`.


def _export_attachments() -> html.Raw:
    """Every file the ledger knows about. See `_export_mail`."""
    return html.tag("a", html.DOWNLOAD_ICON, "Export", href="/ui/attachments.csv",
                    class_="btn small", title="Download every row of this table as CSV")


_ATTACHMENTS_CSV_COLUMNS = ("#", "Received", "Filename", "Type", "Size", "PO", "Records", "Stored",
                            "Inline", "POD", "POD PO numbers", "POD date", "POD signed by",
                            "Disposition", "Disposition detail", "Read by", "Error",
                            "From mail", "Sender")


@router.get("/attachments.csv")
def attachments_csv(conn: sqlite3.Connection = Depends(deps.get_pipeline_conn)) -> Response:
    """The Attachments table as a file, whole and untruncated — see `mails_csv`.

    Size in bytes rather than "1.2 MB": the page rounds it for reading, and a spreadsheet is where
    someone goes to add it up.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(_ATTACHMENTS_CSV_COLUMNS)
    for a in read_views.attachments(conn):
        writer.writerow(_csv_safe([
            a["id"], (a["email_date"] or a["first_seen_at"] or "")[:16], a["filename"],
            a["sniffed_kind"], a["size_bytes"],
            a["pod_po_numbers"] or a["email_po_hints"] or a["pos_via_records"] or "",
            a["records_extracted"], a["stored_sha"],
            "yes" if a["is_inline"] else "", "yes" if a["is_pod"] else "",
            a["pod_po_numbers"], a["pod_delivery_date"], a["pod_signed_by"],
            (a["disposition"] or "").replace("_", " "), a["disposition_detail"],
            a["claimed_by"], a["error_type"], a["email_subject"], a["sender"],
        ]))
    return Response(
        buffer.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="premier-attachments.csv"',
                 "X-Content-Type-Options": "nosniff"},
    )


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


def _mail_fragment_html(email_id: str, *, src: str = "", reason: str = "", images: bool = False,
                        bare: bool = False) -> str:
    """One message rendered as a bare fragment: header, sandboxed body frame, attachment list.

    Shared by the popup route below and by the panel beside the Create-a-record form, which renders
    it into the page rather than fetching it. One function so the two can never drift on which
    store an attachment is looked for in — the `src` threading below is the whole reason that
    matters.

    `bare` drops the Create control. It exists for the create page: that header offers a link to
    `/ui/records/new?email_id=…`, which on that page is the page you are already on, and following
    it silently discards everything typed into the form.
    """
    if not email_id:
        return '<p class="empty">No message was requested.</p>'
    db_path = _store_for(src)
    mail = mail_view.resolve(email_id, db_path=db_path)
    # Offered from the message itself, because that is where somebody works out that the pipeline
    # missed something — reading the mail, not scanning the queue that listed it. It used to say
    # what had already been made from this message *instead of* offering a second record, on the
    # reasoning that one delivery needs one record. That is true of a delivery and false of a
    # message: a notification lists the lines of a delivery, and sometimes several POs. So it now
    # says what has been made and offers both ways to add to it.
    header = "" if bare else _create_from_mail(email_id)
    return header + mail_view.render(
        mail, reason=reason, remote_images=images,
        # `src` is carried through so an attachment link inside the popup looks in the same store
        # the message itself came from.
        attachment_url=f"/ui/mail/attachment?src={quote(src)}" if src else "/ui/mail/attachment",
        # The viewer route, minus the ordinal — `_attachments` appends `&n=` per row. Carries `src`
        # for the same reason `attachment_url` does: an attachment must be looked for in the store
        # its message came from.
        view_url=f"/ui/mail/attachment/view?id={quote(email_id)}&src={quote(src)}",
        db_path=db_path,
    )


@router.get("/mail", response_class=HTMLResponse)
def mail_fragment(id: str = "", reason: str = "", images: int = 0, src: str = "",
                  bare: int = 0) -> str:
    """The popup's contents. Fetched by the page, never navigated to.

    Resolution falls back through the cache, the accumulated payload, and finally the `.msg`
    files — the last of which is how ROUTE mail (cancellations, loss and claim notices) is
    recoverable at all, since it never accumulates.
    """
    return _mail_fragment_html(id, src=src, reason=reason, images=bool(images), bare=bool(bare))


def _verdict_from_mail(email_id: str, set_aside: bool) -> html.Raw:
    """The reclassify control for the message dialog, pointing whichever way this message is not.

    Direction comes from the message's own state rather than from the page the dialog was opened
    over, and that is what makes one control correct everywhere it appears: on the queue it offers
    "Not a delivery", on the not-a-delivery page it offers "This is a delivery", and on the Mail
    page it offers whichever of those the message is not already. A dialog is fetched by script from
    any page and cannot be told where it was opened from, so asking the message is not a shortcut —
    it is the only answer that cannot be wrong.
    """
    if set_aside:
        return _verdict_link(email_id, mail_overrides.DELIVERY, "This is a delivery",
                             "Put this message back on the queue as a real delivery")
    return _verdict_link(email_id, mail_overrides.NOT_DELIVERY, "Not a delivery",
                         "Say this message is not a delivery notification, and take it off "
                         "the queue")


def _create_from_mail(email_id: str) -> str:
    """The Create control, the reclassify control, and what has already been built from this message.

    Only offered for live mail: a record has to carry a `source_email_id` the rest of the system
    can resolve, and the retired corpus store is not that. `_store_for` resolves an unknown `src`
    to the corpus on purpose, so this asks the live store directly rather than trusting the caller.

    Both controls, side by side, because reading the message is where somebody settles which of them
    this is. Sending them back to the row to press the other one is asking them to find it again on
    a page of five hundred, having just closed the only thing that answered the question.
    """
    conn = deps.pipeline_connection()
    try:
        known = conn.execute("SELECT 1 FROM email_log WHERE email_id = ?", (email_id,)).fetchone()
        if known is None:
            return ""
        made = record_create.records_from(conn, email_id)
        verdict = _verdict_from_mail(email_id, read_views.is_set_aside(conn, email_id))
    finally:
        conn.close()

    if made:
        who = ", ".join(sorted({str(r["created_by"] or "somebody") for r in made}))
        numbers = ", ".join(f"#{r['id']}" for r in made)
        latest = made[-1]
        po = (latest["po_number"] or "").strip()
        return str(html.tag(
            "p",
            html.muted(f"{len(made)} record(s) created from this message by hand ({numbers}, "
                       f"by {who})."),
            " ",
            html.tag("a", f"Add another line to PO {po}" if po else "Add another line",
                     class_="btn ghost small",
                     href=f"/ui/records/new?email_id={quote(email_id)}"
                          f"&after={latest['id']}&same_po=1",
                     title="Same purchase order, same delivery date — a different item on it"),
            " ",
            html.tag("a", "Different PO", class_="btn ghost small",
                     href=f"/ui/records/new?email_id={quote(email_id)}",
                     title="A separate delivery that this same message reports"),
            " ",
            html.tag("a", "Records", href="/ui/records", class_="btn ghost small"),
            " ", verdict,
            class_="mail-created"))
    return str(html.tag("p", _create_link(email_id), " ", verdict, class_="mail-created"))


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
        # Read while the connection is open, and only when there is something to explain.
        why = attachment_bytes.missing_reason(conn, id, n) if found is None else None
    finally:
        conn.close()

    # A miss is still a miss. 19 of the corpus's 72 attachments were dropped at ingest — decorative
    # signature images, duplicates by content hash — and serving a 0-byte 200 as `image/png` gives
    # the browser something it cannot render and no reason why. The popup shows the ledger's own
    # verdict beside each attachment; this just refuses to pretend there are bytes.
    if found is None:
        return _no_bytes_response(why, download)

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


_BLANK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAXpeqz8AAAAASUVORK5CYII=")
"""A 1x1 fully transparent PNG, 68 bytes. See `_no_bytes_response`."""


def _no_bytes_response(why, download: int) -> Response:
    """How to say "there are no bytes", in the register the asker is using.

    A signature logo whose bytes were deliberately released is the one case worth answering with a
    picture. `mail_view._resolve_inline_images` points every `cid:` in a stored message body at
    this route, and the frame's CSP is `img-src 'self' data:` with no remote fallback -- so a 404
    there is a broken-image icon in the middle of somebody's signature block, on a message where
    nothing is actually wrong. A transparent pixel lets the body lay out as its sender intended.

    Everything else keeps the refusal, unchanged and deliberate:

    * `download=1` is a person asking for the file. Handing them a blank pixel named as their
      document would be a lie with a filename on it.
    * a non-inline decorative row, an unknown ordinal, and every other disposition -- `corrupt`,
      `service_unavailable`, `dropped_oversize` -- mean something a reader needs to know. The
      popup prints the ledger's own verdict beside each attachment; this must not contradict it.

    `X-Attachment-Placeholder` is how an operator, a log and a test tell a blank pixel from real
    bytes without parsing the body. `no-store` because restoring the blob, or setting
    `PREMIER_STORE_DECORATIVE=1`, must take effect on the next load rather than after a cache
    expires.
    """
    refusal = Response(
        content=b"This attachment was not retained - see the verdict beside it.",
        status_code=404, media_type="text/plain")
    if download or why is None:
        return refusal
    disposition, is_inline = why
    if disposition != attachment_ledger.DROPPED_DECORATIVE or not is_inline:
        return refusal
    return Response(
        content=_BLANK_PNG,
        media_type="image/png",
        headers={
            # Our file, not the sender's: naming it after their attachment would make a saved copy
            # claim to be the document.
            "Content-Disposition": 'inline; filename="placeholder.png"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; object-src 'none'",
            "Cache-Control": "no-store",
            "X-Attachment-Placeholder": attachment_ledger.DROPPED_DECORATIVE,
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

# `_form_values` and `_form_list` moved to `api/forms.py` when the sign-in page needed them too.
# The names are kept as aliases so the eight call sites above read unchanged.


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

    Prefixed with `_BOOT`, so a restarted server also counts as "something new to look at" — that
    is what makes a code change visible in an already-open tab.

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
    return f"{_BOOT}:{newest}:{count}:{run_id}:{arrivals}"


@router.get("/version")
def version_endpoint() -> dict:
    """What the page poller asks, ten seconds at a time. See `_JS` in `api/ui/html.py`.

    `running` rides along because the poller has to behave differently during a run, and asking
    a second endpoint for it would double a request that exists precisely because it is cheap.

    Why the poller needs it at all: `live_version_token()` includes `email_log`'s row count and
    highest id, both of which climb continuously while a pass writes. So during a run the token
    changes on essentially every poll, and a poller that reloads whenever the token moved would
    reload every ten seconds for the length of the run — re-creating, more slowly, the two-second
    meta refresh that was just removed from `/ui/automation`. `runner.is_running()` is a memory
    read of a plain flag; it costs nothing to answer.
    """
    return {"token": live_version_token(), "running": runner.is_running()}


@router.get("/run-progress")
def run_progress_endpoint() -> dict:
    """Where the run in progress has got to. Polled every couple of seconds while one is running.

    **Separate from `/ui/version`, and the difference is the whole point: this touches no database
    at all.** `live_version_token()` opens two connections and runs `mail_arrivals`'
    pending anti-join; that is affordable once every ten seconds and not five times as often. Every
    field here is a read of a plain dict in this process (`runner._STATE`, `runner._PROGRESS`), so
    the endpoint costs microseconds and can be polled as fast as the ring needs to move.

    This exists because the progress ring had no way to advance. A `<meta http-equiv="refresh">`
    used to reload `/ui/automation` every two seconds, and removing it — correctly, it threw away
    scroll position, open dialogs and table filters hundreds of times per run — left nothing in its
    place. `/ui/version` carries no progress, and the poller in `_JS` deliberately refuses to reload
    while a run is in flight. So for the length of a multi-minute run the ring, the phase and the
    count were a snapshot from page load that never moved.

    `elapsed_seconds` is computed here rather than in the browser: the client's clock is not ours,
    and "started 2 minutes ago" rendered from a mismatched clock is worse than no number.
    """
    running = runner.is_running()
    since = runner.running_since()
    elapsed = None
    if since:
        try:
            elapsed = max(0, int((datetime.now() - datetime.strptime(
                since, "%Y-%m-%d %H:%M:%S")).total_seconds()))
        except (ValueError, TypeError):
            elapsed = None
    progress = runner.progress() if running else {}
    return {
        "running": running,
        "since": since,
        "elapsed_seconds": elapsed,
        "phase": progress.get("phase", ""),
        "label": html.progress_label(progress.get("phase", "")),
        "done": progress.get("done", 0),
        "total": progress.get("total", 0),
        "note": progress.get("note", ""),
        # How long since the run last showed any sign of life. A slow read keeps this near zero,
        # a hung one watches it climb — which is the difference an operator needs while deciding
        # whether to press Stop, and before `runner.reap_if_stuck` decides for them.
        "silent_seconds": (None if not running or runner.seconds_since_activity() is None
                           else int(runner.seconds_since_activity())),
        "stop_requested": runner.stop_requested(),
    }


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
        # Says what the page actually does, which for a while it did not. This used to promise
        # "this page refreshes itself until it finishes" — written when a `<meta refresh>` reloaded
        # every two seconds. That refresh is gone, and for a time the sentence outlived it: the
        # page froze mid-run and went on claiming to be working long after the run had ended.
        # Copy describing behaviour has to be changed with the behaviour.
        detail = ("The automation is working through the mail"
                  + (f", started {_ago(since)}" if since else "")
                  + ". You can leave this page — it keeps going, and the progress above advances "
                    "as it does.")
    elif last is None:
        tone, headline = "", "Not run yet"
        detail = "Press Run now to read the delivery emails and build the receiver report."
    elif last.error:
        tone, headline = "bad", f"Last run failed {_ago(last.started_at)}"
        detail = last.error
    elif last.emails == 0 and _unread_arrivals():
        # A run that read nothing while mail is *known* to be waiting is not a healthy run, and
        # this branch used to call it one — "everything in the inbox has already been through the
        # pipeline" was printed while 24 messages sat unreadable, one of them an urgent PO email.
        #
        # The count is not new information: `mail_arrivals` minus `email_log` is exactly what the
        # Mail page already displays. Nothing consulted it here, so the console's own summary was
        # the thing hiding the fault. Silence is not health.
        waiting = _unread_arrivals()
        tone, headline = "bad", f"Last run read nothing, {_ago(last.started_at)}"
        detail = (f"{waiting} message{_s(waiting)} arrived and still has no verdict, so this run "
                  "reading nothing is a fault rather than an idle inbox. See Mail for which.")
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

    # Rendered on every visit, not only during a run, and hidden when idle. A run started in another
    # tab — or by the schedule — can then be shown by the ticker in `_JS` writing into markup that is
    # already here, instead of the page needing a reload it has good reasons to refuse.
    progress = runner.progress() if running else {}

    # **`—`, not `0`, while a run is in flight.** `ops_store.start_run` inserts the run row *before*
    # the pass, with every count defaulting to zero, and `last_run()` is that row — so for the whole
    # of a multi-minute run this strip sat under the words "Running now…" asserting `0 emails read /
    # 0 receiver lines / 0 need a person / 0s`. Four confident zeroes about work in progress read as
    # a run that is finding nothing, which is the opposite of what is happening. An em dash says
    # "not known yet", and the ring above is what carries the truth meanwhile.
    def figure(value):
        return html.muted("—") if running else value

    status = html.card(
        # The refusal sits above the status, not instead of it: it explains what this press did,
        # while the card below still answers what the automation is doing.
        html.tag("p", refused, class_="err") if refused else html.Raw(""),
        html.progress_ring(progress.get("phase", ""), progress.get("done", 0),
                           progress.get("total", 0), progress.get("note", ""),
                           hidden=not running),
        html.tag("p", detail, class_="note", data_run_detail="1"),
        html.stat_row([
            (figure(last.emails if last else 0), "emails read"),
            (figure(last.records if last else 0), "receiver lines"),
            (figure(last.needs_person if last else 0), "need a person"),
            (figure(f"{last.elapsed_seconds:g}s" if last else "—"), "time taken"),
        ]),
        html.tag("div",
                 html.button_form("/ui/automation/run", "Run now"),
                 # Beside Run now rather than instead of it, and a separate thing from "Stop
                 # automation" in the sidebar: that one engages the kill switch, which survives a
                 # restart and pauses the schedule. This stops the pass in progress and nothing else.
                 #
                 # **Always drawn, greyed out while idle.** It was hidden when nothing was running,
                 # and the first thing anyone did was come looking for it between runs and conclude
                 # it did not exist. A control you can see but not press says "nothing to stop";
                 # a control that is not there says nothing at all.
                 html.tag("span",
                          html.button_form("/ui/automation/run/stop",
                                           "Stopping…" if runner.stop_requested()
                                           else "Stop this run",
                                           busy_label="Stopping…",
                                           disabled=not running or runner.stop_requested(),
                                           title=("Stops the run in progress. Nothing is running "
                                                  "right now." if not running else
                                                  "Stops the run in progress; the schedule "
                                                  "carries on.")),
                          data_run_stop="1"),
                 style="margin-top:18px;display:flex;gap:10px;flex-wrap:wrap"),
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
                     **_sidebar_counts(),
                     # **Never.** This was `2 if running else 0` — a `<meta http-equiv="refresh">`
                     # reloading the whole page every two seconds for the entire length of a run,
                     # to advance the progress ring.
                     #
                     # Two problems, and the second is the one that decided it. A run is minutes
                     # long, so that is hundreds of full reloads; and while the token call could
                     # hang (see `settings.GRAPH_AUTH_TIMEOUT_SECONDS`) `running` stayed true for
                     # twelve hours at a stretch, so the page hammered itself for half a day.
                     #
                     # More importantly it reloaded *unconditionally*, throwing away scroll
                     # position, open dialogs and active table filters — the exact etiquette `_JS`
                     # already works out in `safeToReloadWithoutAsking()` before it reloads for new
                     # mail. A meta tag cannot consult any of that. So the progress refresh now
                     # goes through that same poller, which holds back and offers the pill instead
                     # of reloading under someone who is reading.
                     #
                     # The cost, accepted deliberately: the progress ring no longer advances on its
                     # own. That was the whole of what the two-second reload bought.
                     refresh_seconds=0)


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


@router.post("/automation/run/stop")
def automation_run_stop():
    """Stop the run in progress, and only that.

    Not guarded by the kill switch and not taking the run lock — the run is holding it, and a stop
    that waited for the lock would wait for the very run it is meant to end. The run notices at its
    next Graph call or its next email; if it has not let go within
    `settings.RUN_STOP_GRACE_SECONDS`, the scheduler's watchdog abandons it and frees the automation.
    """
    runner.request_stop()
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


def _safe_return(value: str, default: str) -> str:
    """Where a form asked to be sent afterwards, if it is allowed to ask.

    The value is filled in by the page script with the list the person came from, so a verdict
    pressed from Needs a human lands back on Needs a human rather than on the page the route
    happens to name — which is what made a filtered queue reset to five hundred rows.

    It is still a value off a form body, so it is held to exactly the rules `_back_to` applies to
    `Referer`: our own pages only, under `/ui/`, and never a protocol-relative `//host` that a
    browser reads as somewhere else entirely. Anything else falls back to the route's own
    destination rather than being refused — a bad return is not worth failing a recorded decision.
    """
    path = (value or "").strip()
    if not path.startswith("/ui/") or path.startswith("//") or "://" in path or "\\" in path:
        return default
    # `/ui/../admin` passes every test above and is `/admin` by the time a browser has resolved it,
    # so the prefix has to be checked against the *normalised* path rather than the typed one.
    if urlparse(path).path != posixpath.normpath(urlparse(path).path):
        return default
    return path


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
        # Reworded when the sheet moved here from Records on 2026-08-22, because the card it
        # replaced still said all three columns were unreachable — untrue since 2026-08-21, when
        # Order Qty and Net began filling from the mirrored purchase order.
        html.tag("p", "Order Qty comes from the purchase order and fills in once that PO has been "
                      "read from Spitfire; Net is Order Qty minus Received and follows it. Both "
                      "stay blank until then rather than showing a number nothing supports. Final "
                      "is always blank — it is a flag a person sets in Spitfire and cannot be "
                      "worked out from quantities. The Receiver column says who took delivery, or "
                      "how the record was made when nobody signed.",
                 class_="note", style="margin:0"),
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
    #
    # The search box and the group pager came with the sheet when it moved off Records on
    # 2026-08-22. Without them this modal was a single unbroken sheet of every purchase order, so
    # "which line did Spitfire disagree about" meant scrolling — the two controls are the reason
    # the sheet is usable at all, and dropping the duplicate should not have cost them.
    preview = html.modal(
        "preview", "Receiver report",
        html.Raw(
            str(html.search_box("receipt-sheet", placeholder="Search PO, vendor, spec, receiver…",
                                label="Search the receiver report"))
            # Paged by purchase order, not by row, and `plain` because the sheet brings its own row
            # styling. Ten POs at a time: a page boundary inside a PO would split the block someone
            # is holding beside Spitfire's own Receipt Log, and that block is the whole point of the
            # sheet. "All" is one press away in the pager, and the .xlsx download is never paged.
            + str(html.scroll_block(html.Raw(receipt_log.to_html(report)),
                                    table_id="receipt-sheet", page_size=10, unit="group",
                                    plain=True))
            + str(html.tag("p", "Laid out like Spitfire's own Receipt Log and grouped by PO "
                                "number, so the two can be compared line by line. Shown ten "
                                "purchase orders at a time — set Rows to All, or download the "
                                ".xlsx, to get the whole sheet at once.", class_="note"))
        ))
    mailbox = settings.GRAPH_MAILBOX_ADDRESS or "the receiving mailbox"
    return html.page("Receiver report", "/ui/report", summary, blank_note, *attention, preview,
                     subtitle=f"Built from {mailbox} · generated {report.generated_at}",
                     **_sidebar_counts())


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
