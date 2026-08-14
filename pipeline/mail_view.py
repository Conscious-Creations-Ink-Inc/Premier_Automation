"""Recover an email and show it as it actually looked.

The pipeline stores verdicts, not mail. `email_log` has no body column, and the only stored copy of
a body is `accumulation.payload_json` — which covers held mail, and therefore not the routed mail a
person most wants to read. So bodies are recovered from their original source on demand and cached.

Four sources, tried in order:

1. the console's own cache          — instant on any repeat view
2. `accumulation.payload_json`      — free when present, no I/O
3. the original `.msg` file         — for mail that came from the sample corpus
4. Microsoft Graph                  — for live-mailbox mail, by internetMessageId, **GET only**

Nothing here writes to Outlook. There is no move, no mark-as-read, no flag — reading a message in
this console must not change what Premier sees in their own inbox.

**Fidelity.** The message is shown as it was sent: its own HTML, its own CSS, its own inline
images. Nothing is stripped or restyled. Safety comes from the sandbox around it rather than from
rewriting the sender's markup, because a "cleaned up" POD email is no longer evidence of what
arrived.
"""

import base64
import json
import re
import sqlite3
from dataclasses import dataclass, field
from html import escape
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from config import settings
from pipeline import mail_cache, state_db

_CID_RE = re.compile(r"""(src\s*=\s*["']?)cid:([^"'\s>]+)""", re.IGNORECASE)

_IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/tiff")


@dataclass
class Attachment:
    ordinal: int
    filename: str
    content_type: str
    kind: str
    size_bytes: int
    content_id: Optional[str] = None
    is_inline: bool = False
    content: Optional[bytes] = None
    verdict: str = ""
    verdict_detail: str = ""


@dataclass
class Mail:
    email_id: str
    subject: str
    sender: str
    received_at: str
    body_html: Optional[str]
    body_text: Optional[str]
    source: str
    attachments: List[Attachment] = field(default_factory=list)
    tried: List[str] = field(default_factory=list)


# ==========================================================================
# resolution
# ==========================================================================

def resolve(email_id: str, *, db_path=None) -> Mail:
    """Find this message wherever it still exists. Never raises; an unresolved mail comes back
    with `source == ""` and `tried` listing what was attempted.

    `db_path` names which store the row came from, and the search follows it. That matters more
    than it looks: the fallback chain used to try the sample .msg folder for *every* message,
    including live ones, so opening a row from Premier's mailbox scanned and cached 14 sample
    files before giving up. A live message is never in the corpus and a corpus message is never
    in Outlook, so asking is not merely wasteful — it is the sample and live data touching.
    """
    db_path = db_path or settings.PIPELINE_STATE_DB_PATH
    is_sample = str(db_path) == str(settings.SAMPLE_STATE_DB_PATH)
    tried: List[str] = []

    console_conn = _pipeline_conn(db_path)
    try:
        cached = mail_cache.get_cached_mail(console_conn, email_id)
        if cached is not None:
            rows = mail_cache.cached_attachments(console_conn, email_id)
            mail = Mail(
                email_id=email_id, subject=cached["subject"], sender=cached["sender"],
                received_at=cached["received_at"], body_html=cached["body_html"],
                body_text=cached["body_text"], source=cached["source"],
                attachments=[
                    Attachment(ordinal=r["ordinal"], filename=r["filename"],
                               content_type=r["content_type"], kind=r["kind"],
                               size_bytes=r["size_bytes"], content_id=r["content_id"],
                               is_inline=bool(r["is_inline"]))
                    for r in rows
                ],
            )
            _attach_verdicts(mail, db_path)
            return mail
        tried.append("console cache")

        chain = ((_from_accumulation, "held-mail store"),
                 (_from_corpus, "sample .msg files") if is_sample
                 else (_from_graph, "Outlook (read-only)"))
        for loader, label in chain:
            try:
                mail = loader(email_id, db_path)
            except Exception as exc:                            # noqa: BLE001
                tried.append(f"{label} — {type(exc).__name__}: {exc}")
                continue
            if mail is not None:
                mail.source = label
                _cache(console_conn, mail)
                _attach_verdicts(mail, db_path)
                mail.tried = tried
                return mail
            tried.append(label)
    finally:
        console_conn.close()

    return Mail(email_id=email_id, subject="", sender="", received_at="", body_html=None,
                body_text=None, source="", tried=tried)


def _from_accumulation(email_id: str, db_path) -> Optional[Mail]:
    conn = _pipeline_conn(db_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM accumulation WHERE email_id = ? LIMIT 1", (email_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None

    payload = json.loads(row["payload_json"])
    email = payload.get("email") or payload
    attachments = []
    for i, a in enumerate(email.get("attachments") or []):
        raw = a.get("content_bytes")
        content = base64.b64decode(raw) if isinstance(raw, str) and raw else None
        attachments.append(Attachment(
            ordinal=i, filename=a.get("filename") or f"attachment-{i}",
            content_type=a.get("content_type") or "", kind=a.get("sniffed_kind") or "",
            size_bytes=a.get("size_bytes") or (len(content) if content else 0),
            content_id=a.get("content_id"), is_inline=bool(a.get("is_inline")), content=content,
        ))
    return Mail(
        email_id=email_id, subject=email.get("subject") or "",
        sender=payload.get("origin_sender_address") or email.get("sender_address") or "",
        received_at=email.get("received_at") or "", body_html=email.get("body_html"),
        body_text=email.get("body_text"), source="", attachments=attachments,
    )


def _from_corpus(email_id: str, db_path=None) -> Optional[Mail]:
    """Read the sample folder. One scan caches every message in it, so this cost is paid once."""
    from tools.ingest_corpus import DEFAULT_CORPUS

    folder = Path(DEFAULT_CORPUS)
    if not folder.exists():
        return None

    from connectors.msg_file import MsgFileMailbox

    # read_only is not optional: this is Premier's own sample data.
    mailbox = MsgFileMailbox(folder, keep_decorative_images=True, read_only=True)
    found: Optional[Mail] = None
    console_conn = _pipeline_conn(db_path)
    try:
        for raw in mailbox.fetch_new():
            mail = _from_raw_email(raw)
            if raw.email_id == email_id:
                found = mail
            else:
                mail.source = "sample .msg files"
                _cache(console_conn, mail)
    finally:
        console_conn.close()
    return found


def _from_graph(email_id: str, db_path=None) -> Optional[Mail]:
    """Fetch one message by its internetMessageId. Read-only: two GETs and nothing else."""
    import requests

    from operations.inbox import GRAPH_BASE_URL, _token

    mailbox = settings.GRAPH_MAILBOX_ADDRESS
    if not mailbox or not settings.GRAPH_CLIENT_ID:
        return None

    headers = {"Authorization": f"Bearer {_token()}"}
    quoted = email_id.replace("'", "''")
    listing = requests.get(
        f"{GRAPH_BASE_URL}/users/{mailbox}/messages",
        headers=headers,
        params={"$filter": f"internetMessageId eq '{quoted}'",
                "$select": "id,subject,from,receivedDateTime,body,hasAttachments", "$top": 1},
        timeout=30,
    )
    listing.raise_for_status()
    items = listing.json().get("value") or []
    if not items:
        return None
    item = items[0]

    body = item.get("body") or {}
    is_html = (body.get("contentType") or "").lower() == "html"
    mail = Mail(
        email_id=email_id,
        subject=item.get("subject") or "",
        sender=(((item.get("from") or {}).get("emailAddress") or {}).get("address") or ""),
        received_at=str(item.get("receivedDateTime") or "")[:19].replace("T", " "),
        body_html=body.get("content") if is_html else None,
        body_text=None if is_html else body.get("content"),
        source="",
    )

    if item.get("hasAttachments"):
        got = requests.get(
            f"{GRAPH_BASE_URL}/users/{mailbox}/messages/{item['id']}/attachments",
            headers=headers, timeout=60,
        )
        got.raise_for_status()
        for i, a in enumerate(got.json().get("value") or []):
            raw = a.get("contentBytes")
            content = base64.b64decode(raw) if raw else None
            mail.attachments.append(Attachment(
                ordinal=i, filename=a.get("name") or f"attachment-{i}",
                content_type=a.get("contentType") or "", kind="",
                size_bytes=a.get("size") or (len(content) if content else 0),
                content_id=a.get("contentId"), is_inline=bool(a.get("isInline")), content=content,
            ))
    return mail


def _from_raw_email(raw) -> Mail:
    return Mail(
        email_id=raw.email_id, subject=raw.subject or "", sender=raw.sender_address or "",
        received_at=raw.received_at or "", body_html=raw.body_html, body_text=raw.body_text,
        source="",
        attachments=[
            Attachment(ordinal=i, filename=a.filename or f"attachment-{i}",
                       content_type=a.content_type or "", kind=a.sniffed_kind or "",
                       size_bytes=a.size_bytes or len(a.content_bytes or b""),
                       content_id=a.content_id, is_inline=bool(a.is_inline),
                       content=a.content_bytes)
            for i, a in enumerate(raw.attachments or [])
        ],
    )


def _cache(conn: sqlite3.Connection, mail: Mail) -> None:
    mail_cache.cache_mail(
        conn, email_id=mail.email_id, subject=mail.subject, sender=mail.sender,
        received_at=mail.received_at, body_html=mail.body_html, body_text=mail.body_text,
        source=mail.source, cached_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        attachments=[
            {"ordinal": a.ordinal, "filename": a.filename, "content_type": a.content_type,
             "kind": a.kind, "size_bytes": a.size_bytes, "content_id": a.content_id,
             "is_inline": a.is_inline, "content": a.content}
            for a in mail.attachments
        ],
    )


def _attach_verdicts(mail: Mail, db_path=None) -> None:
    """What the pipeline decided about each attachment — read, dropped as a logo, duplicate,
    unreadable. Without it a person cannot tell a missed POD from one correctly ignored."""
    conn = _pipeline_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT filename, sha256, disposition, disposition_detail, records_extracted "
            "FROM attachment_ledger WHERE email_id = ?", (mail.email_id,)
        ).fetchall()
    except Exception:                                           # noqa: BLE001
        return
    finally:
        conn.close()

    by_name: Dict[str, sqlite3.Row] = {r["filename"]: r for r in rows}
    for a in mail.attachments:
        row = by_name.get(a.filename)
        if row is None:
            continue
        a.verdict = row["disposition"] or ""
        detail = row["disposition_detail"] or ""
        if row["records_extracted"]:
            detail = f"{row['records_extracted']} record(s) read" + (f" · {detail}" if detail else "")
        a.verdict_detail = detail


def _pipeline_conn(db_path=None) -> sqlite3.Connection:
    conn = state_db.get_connection(db_path or settings.PIPELINE_STATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ==========================================================================
# render
# ==========================================================================

def render(mail: Mail, *, reason: str = "", remote_images: bool = False,
           attachment_url: str = "/mail/attachment", view_url: str = "", db_path=None) -> str:
    """The message as an HTML fragment, escaped and safe to embed.

    Returns a plain `str`, not either interface's `Raw`: the console and `/ui` each have their own
    marker class and their own escaper, and this module depends on neither. Callers wrap it.

    `attachment_url` is the route that serves attachment bytes. The two interfaces mount it at
    different paths, and every image, PDF and inline `cid:` reference in the fragment points at it,
    so it cannot be hardcoded here.

    `view_url` is the route that *renders* an attachment — the same URL minus the ordinal, which
    `_attachments` appends per row. Left empty the list still draws, with names instead of Open
    controls, so an embedder that has no viewer is not broken by having one.
    """
    e = escape
    if not mail.source:
        attempted = "".join(f"<li>{e(t)}</li>" for t in mail.tried)
        return (
            '<p class="empty">This message could not be found in any source we can still read.'
            f"</p><p class='note'>Tried:</p><ul>{attempted}</ul>"
        )

    header = (
        f'<div class="mail-head"><h3>{e(mail.subject) or "(no subject)"}</h3>'
        f'<p class="note">From {e(mail.sender) or "unknown"}'
        + (f" · {e(mail.received_at)}" if mail.received_at else "")
        + f' · recovered from {e(mail.source)}</p>'
        + (f'<p class="why">{e(reason)}</p>' if reason else "")
        + "</div>"
    )
    return (header + _remote_notice(mail, remote_images)
                    + _body_frame(mail, remote_images, attachment_url)
                    + _attachments(mail, attachment_url, view_url, db_path))


def _remote_notice(mail: Mail, remote_images: bool) -> str:
    """Mail clients block internet images by default and offer to load them, because fetching one
    tells the sender the address is live and the message was opened. This mailbox receives mail
    from outside parties, so the same default applies — but the choice stays with the reader."""
    if remote_images or not mail.body_html:
        return ""
    if not re.search(r"""<img[^>]+src\s*=\s*["']?https?://""", mail.body_html, re.IGNORECASE):
        return ""
    return (
        '<p class="remote-note">Images hosted on the internet are not shown, so that opening this '
        'message does not tell the sender it was read. '
        f'<button class="btn ghost small" type="button" data-load-images="1">Show them</button></p>'
    )


def _body_frame(mail: Mail, remote_images: bool = False,
                attachment_url: str = "/mail/attachment") -> str:
    """The message, exactly as it was sent, inside a sandbox.

    `sandbox` without `allow-scripts` means nothing in the message can execute — that is what makes
    it safe to render the sender's own markup untouched rather than stripping it. `allow-same-origin`
    is present only so the parent page can measure the content and size the frame to it; because
    scripts are disabled it grants the message no capability of its own.

    **`allow-scripts` must never be added here.** With both flags a sandbox is no sandbox.
    """
    if mail.body_html:
        document = _resolve_inline_images(mail, attachment_url)
    elif mail.body_text:
        document = f"<pre style='white-space:pre-wrap;font:13px/1.5 monospace'>{_esc(mail.body_text)}</pre>"
    else:
        return '<p class="empty">This message had no body.</p>'

    # `'self'` covers the message's own images, which are served from /mail/attachment rather than
    # embedded. Remote images stay off unless the reader asks, so opening a message cannot fire a
    # tracking pixel back to the sender. Scripts are blocked in both modes.
    img_src = "'self' data: https: http:" if remote_images else "'self' data:"
    csp = ("<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; "
           f"img-src {img_src}; style-src 'unsafe-inline'; font-src data:\">")

    # Without this, a link with no target of its own navigates *this frame*, replacing the message
    # with whatever loads — and most sites refuse to be framed, so the reader gets an error page
    # and no way back. 490 of the 510 links across the corpus set no target.
    base = "<base target='_blank'>"

    srcdoc = _esc(f"<!doctype html><html><head><meta charset='utf-8'>{csp}{base}</head>"
                      f"<body style='margin:12px'>{document}</body></html>")
    # `allow-popups` lets those links open a tab; `allow-popups-to-escape-sandbox` means the tab is
    # an ordinary page rather than a crippled sandboxed one. Neither grants the message any script.
    return (f'<iframe class="mail-body" '
            f'sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox" '
            f'srcdoc="{srcdoc}" title="Message body"></iframe>')


def _resolve_inline_images(mail: Mail, attachment_url: str) -> str:
    """Point every `cid:` reference at the attachment route so the message's own images render.

    Without this, every logo, screenshot and pasted photograph is a broken-image icon — and in this
    mail a pasted photograph is often the proof of delivery itself.

    These used to be embedded as base64. Two things were wrong with that. It only worked on the
    first, uncached read, because the cached rows deliberately carry no blobs — so the lookup was
    empty and *nothing* was substituted on every subsequent open. And when it did work, one
    photo-heavy message produced a 20 MB response. Referencing the route instead is correct in both
    cases, and lets the browser fetch images in parallel and only as they are scrolled to.
    """
    by_cid: Dict[str, int] = {}
    for a in mail.attachments:
        if a.content_id:
            by_cid[a.content_id.strip("<>").lower()] = a.ordinal

    email_id = _q(mail.email_id)

    def replace(match: "re.Match") -> str:
        prefix, cid = match.group(1), match.group(2).strip("<>").lower()
        ordinal = by_cid.get(cid)
        if ordinal is None:
            return match.group(0)   # the .msg genuinely does not carry this image
        return f"{prefix}{attachment_url}?id={email_id}&n={ordinal}"

    return _CID_RE.sub(replace, mail.body_html or "")


def _attachments(mail: Mail, attachment_url: str, view_url: str = "", db_path=None) -> str:
    """The list under the message: every attachment, each one openable.

    **Every row is an Open control now, whatever the file is.** This list used to carry the preview
    itself, which meant two things: a spreadsheet or a Word document was parsed server-side on every
    single message open, and the ten kinds with no branch — `.msg`, HTML, CSV, decks, archives —
    showed a filename and nothing else. `/ui/mail/attachment/view` renders all fifteen kinds full
    size, so the list's job is now to name them and get out of the way.

    The image thumbnail stays, because for a photographed POD the thumbnail *is* the answer to
    "what is this" and it costs one already-streaming request.
    """
    if not mail.attachments:
        return ""
    e = escape
    blocks = []
    for a in mail.attachments:
        verdict = ""
        if a.verdict:
            tone = {"extracted": "good"}.get(a.verdict, "")
            verdict = _badge(a.verdict.replace("_", " "), tone)
            if a.verdict_detail:
                verdict += f' <span class="muted">{e(a.verdict_detail)}</span>'

        href = f"{attachment_url}?id={_q(mail.email_id)}&n={a.ordinal}"
        open_at = f"{view_url}&n={a.ordinal}" if view_url else ""
        name = (f'<button type="button" class="link-btn att-open" data-frag="{e(open_at)}">'
                f'{e(a.filename)}</button>') if open_at else f"<b>{e(a.filename)}</b>"
        blocks.append(
            f'<div class="att"><div class="att-head">{name}'
            f'<span class="muted">{e(_size(a.size_bytes))} · {e(a.content_type or a.kind or "unknown")}</span>'
            + (f'<button type="button" class="btn ghost small" data-frag="{e(open_at)}">Open</button>'
               if open_at else "")
            + f'<a class="btn ghost small" href="{href}&download=1">Download</a></div>'
            f'<div class="att-verdict">{verdict}</div>{_thumbnail(a, href, open_at)}</div>'
        )
    return f'<h4 class="att-title">Attachments ({len(mail.attachments)})</h4>' + "".join(blocks)


def _thumbnail(a: Attachment, href: str, open_at: str) -> str:
    """A picture, if this is one. Everything else is opened rather than previewed here.

    Deliberately the only preview left in this list. A PDF used to render as a 560px iframe per
    attachment and a workbook as a parsed table, which made opening a message with three
    attachments three parses and three viewers deep before anyone had asked to see any of them.
    """
    if (a.kind or "").lower() == "image" or (a.content_type or "").lower().startswith("image/"):
        img = f'<img class="att-img" src="{escape(href)}" alt="{escape(a.filename or "")}">'
        return (f'<button type="button" class="thumb-btn" data-frag="{escape(open_at)}">{img}</button>'
                if open_at else img)
    return ""


def _size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def _esc(value) -> str:
    """Escape anything, not just `str`.

    The standard library's `escape` raises on anything that is not a string, and the spreadsheet
    preview feeds it raw cell values: ints, floats, datetimes and None. The renderer this module
    was lifted out of coerced any type and turned None into "", and keeping those semantics here is
    what stops a tracker attachment from 500-ing the whole message. `api/ui/html.esc` does the same
    thing for the same reason.
    """
    if value is None:
        return ""
    return escape(value if isinstance(value, str) else str(value))


def _badge(text: str, tone: str = "") -> str:
    """The verdict pill beside an attachment. Emitted as markup rather than borrowing either
    interface's `badge()`, for the same reason `render` returns `str`: this module knows about
    neither. Both stylesheets already carry `.badge`."""
    return f'<span class="badge badge-{_esc(tone or "plain")}">{_esc(text)}</span>'


def _q(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")
