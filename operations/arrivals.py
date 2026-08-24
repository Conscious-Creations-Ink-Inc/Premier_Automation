"""The fast half of reading the mailbox: notice that mail arrived, and nothing else.

Premier's complaint was that mail took a long time to appear on screen. Three delays stacked up,
and only one of them was ever about the network:

  * new mail entered the store only when a *full* ingest pass finished, and that is scheduled in
    minutes because the pass takes about forty seconds;
  * the pass is slow because it downloads every attachment's bytes before recording anything;
  * the Mail page itself then made a ten-page Graph walk while rendering.

So this module does the one cheap thing the expensive path was hiding: a single metadata-only
listing — subject, sender, date, has-attachments — written straight to `mail_arrivals`. No body, no
attachments, no OCR, no triage. That is a few hundred milliseconds, which is what makes a
fifteen-second interval affordable when a forty-second pass is not.

**It reads. It never writes to the mailbox.** The only HTTP verb here is GET. There is no move, no
mark-as-read, no folder creation and no `$value` fetch. That is a property of this file rather than
a flag on it, exactly as in `operations/inbox.py`, so it cannot be switched on from a dashboard.

**It never advances the ingest watermark.** It has its own
(`state_db.ARRIVALS_WATERMARK_KEY`). Sharing one would let a poll that merely *listed* a message
move the marker the real pipeline reads from, and that mail would then never be processed by
anything — the worst available failure here, and a silent one.

The seam for webhooks is `record()`. Graph change notifications need a public HTTPS endpoint, which
Premier does not have today; when they do, the handler resolves the notification's (volatile)
message id to an `internetMessageId` and calls this same function. Nothing else has to change.
"""

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from config import settings
from operations import inbox as inbox_reader
from pipeline import mail_arrivals, state_db

GRAPH_BASE_URL = inbox_reader.GRAPH_BASE_URL

PAGE_SIZE = 50
"""One page, not ten. `operations/inbox.py` walks up to `MAX_PAGES = 10` sequentially at a measured
~3.2s each, which is most of why a cold Mail page took half a minute. A watch running every fifteen
seconds cannot fall fifty messages behind between polls; if it somehow does, the watermark stays
put and the next poll picks the rest up."""

OVERLAP_MINUTES = 5
"""Backdate the listing window, the same trick `stage1_ingest.listing_window_start` uses with a
wider margin. Mail is stamped by the server and can be indexed a moment after it is stamped, so a
window starting exactly at the last-seen timestamp can step over a message. Re-listing five minutes
of metadata costs nothing — `record()` is an upsert."""

_lock = threading.Lock()
"""Serialises polls against each other only. It is deliberately **not** `runner._LOCK`: a
fifteen-second metadata read must never be blocked by, or block, a forty-second pipeline pass. That
lock exists to stop two pipeline runs overlapping, and this is not a pipeline run."""


@dataclass
class PollOutcome:
    ok: bool
    new: int = 0
    listed: int = 0
    skipped: bool = False
    error: Optional[str] = None
    elapsed_seconds: float = 0.0


def poll_once(*, db_path=None) -> PollOutcome:
    """List what has arrived since the arrivals watermark and record it. Never raises.

    A watch that dies on one bad tick is worse than one that keeps trying, and the error is carried
    back so the operations page can show it rather than the poll failing silently.
    """
    if not _lock.acquire(blocking=False):
        return PollOutcome(ok=False, skipped=True, error="A poll is already in progress.")
    began = time.monotonic()
    try:
        return _poll_locked(db_path or settings.PIPELINE_STATE_DB_PATH)
    except Exception as exc:                                   # noqa: BLE001 - shown to the user
        return PollOutcome(ok=False, error=f"{type(exc).__name__}: {exc}",
                           elapsed_seconds=time.monotonic() - began)
    finally:
        _lock.release()


def _poll_locked(db_path) -> PollOutcome:
    began = time.monotonic()
    missing = _missing_config()
    if missing:
        return PollOutcome(ok=False,
                           error="Mailbox connection is not configured (" + ", ".join(missing) + ").")

    conn = state_db.get_connection(db_path)
    try:
        since = _window_start(state_db.get_arrivals_watermark(conn))
        listed = _list(inbox_reader._token(), settings.GRAPH_MAILBOX_ADDRESS, since)
        new = record(conn, listed)
        # Only after the rows are committed. Advancing first and then failing to write would skip
        # this window for ever, which is the same class of bug as sharing the ingest watermark.
        state_db.advance_arrivals_watermark(
            conn, max((a.received_at for a in listed if a.received_at), default=None))
    finally:
        conn.close()

    return PollOutcome(ok=True, new=new, listed=len(listed),
                       elapsed_seconds=time.monotonic() - began)


def record(conn, arrivals: List[mail_arrivals.Arrival]) -> int:
    """The seam. A poll calls this; a future webhook handler calls exactly this."""
    return mail_arrivals.record(conn, arrivals, now=_now())


def _missing_config() -> List[str]:
    return [
        name for name, value in (
            ("GRAPH_TENANT_ID", settings.GRAPH_TENANT_ID),
            ("GRAPH_CLIENT_ID", settings.GRAPH_CLIENT_ID),
            ("GRAPH_CLIENT_SECRET", settings.GRAPH_CLIENT_SECRET),
            ("GRAPH_MAILBOX_ADDRESS", settings.GRAPH_MAILBOX_ADDRESS),
        ) if not value
    ]


def _window_start(watermark: Optional[str]) -> Optional[str]:
    """The watermark, backdated. `None` on the first ever poll, which lists the newest page only —
    the point of this table is what is arriving now, not a backfill of the whole Inbox."""
    if not watermark:
        return None
    try:
        seen = datetime.strptime(watermark, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (seen - timedelta(minutes=OVERLAP_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _list(token: str, mailbox: str, since: Optional[str]) -> List[mail_arrivals.Arrival]:
    """One GET. `$select` is metadata only — asking for `body` here is what makes the ingest
    listing heavy, and none of it would be used."""
    import requests

    params = {
        "$top": PAGE_SIZE,
        "$orderby": "receivedDateTime desc",
        "$select": "receivedDateTime,subject,from,hasAttachments,internetMessageId",
    }
    if since:
        params["$filter"] = f"receivedDateTime ge {since}"
    response = requests.get(
        f"{GRAPH_BASE_URL}/users/{mailbox}/mailFolders/Inbox/messages",
        headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30,
    )
    response.raise_for_status()

    now = _now()
    arrivals = []
    for item in response.json().get("value", []):
        email_id = item.get("internetMessageId") or ""
        if not email_id:
            # Without the stable id there is nothing to dedupe on, and Graph's own `id` changes the
            # moment a message moves. Skipping is right: the full ingest pass will still see it.
            continue
        arrivals.append(mail_arrivals.Arrival(
            email_id=email_id,
            received_at=str(item.get("receivedDateTime") or ""),
            sender=(((item.get("from") or {}).get("emailAddress") or {}).get("address") or ""),
            subject=item.get("subject") or "",
            has_attachments=bool(item.get("hasAttachments")),
            first_seen_at=now,
            enriched_at=None,
        ))
    return arrivals


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
