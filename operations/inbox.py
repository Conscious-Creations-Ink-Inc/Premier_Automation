"""A read-only look at the Outlook inbox.

Metadata only — subject, sender, when it arrived, whether it has attachments. The pipeline's own
`GraphMailbox.fetch_new()` would also work, but it eagerly downloads every attachment's bytes,
which is minutes and megabytes just to draw a list.

Nothing here writes. There is no move, no mark-as-read, no folder creation, no delete — the only
HTTP verb used is GET. That is a property of this file, not a setting, so it cannot be switched on
by accident from the dashboard.
"""

import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from config import settings

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
MAX_PAGES = 10
PAGE_SIZE = 50

# Measured against the live tenant: the Graph round-trip is ~3.2s and cannot be made faster from
# this side, so repeat visits are served from memory instead. Short enough that the list is never
# meaningfully stale, and the screen always says how old it is with a Refresh next to it.
CACHE_SECONDS = 60

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "view": None}
_msal_app = None


@dataclass
class InboxMessage:
    received: str
    sender: str
    subject: str
    has_attachments: bool
    internet_message_id: str = ""
    """The stable id, so a row can be opened. Graph's own `id` changes the moment a message is
    moved, which makes it useless as a handle."""

    source_folder: str = ""
    """Which folder listed it — `inbox` or `junkemail`. Carried so "Check now" records the same
    fact the background watch does; a button that reads a narrower mailbox than the watch beside
    it would be its own kind of lie."""


@dataclass
class InboxView:
    configured: bool
    mailbox: str
    messages: List[InboxMessage]
    total: Optional[int] = None
    error: Optional[str] = None
    age_seconds: float = 0.0
    """How old this listing is. Shown on screen — a cached list that cannot be seen to be cached
    is worse than a slow one."""


def load(*, force: bool = False) -> InboxView:
    """Read the inbox, or explain why we could not. Served from memory for `CACHE_SECONDS`."""
    with _lock:
        cached = _cache["view"]
        age = time.monotonic() - _cache["at"]
        if cached is not None and not force and age < CACHE_SECONDS and not cached.error:
            cached.age_seconds = age
            return cached

    view = _load_live()
    with _lock:
        _cache["view"] = view
        _cache["at"] = time.monotonic()
    return view


def _load_live() -> InboxView:
    mailbox = settings.GRAPH_MAILBOX_ADDRESS or ""
    missing = [
        name for name, value in (
            ("GRAPH_TENANT_ID", settings.GRAPH_TENANT_ID),
            ("GRAPH_CLIENT_ID", settings.GRAPH_CLIENT_ID),
            ("GRAPH_CLIENT_SECRET", settings.GRAPH_CLIENT_SECRET),
            ("GRAPH_MAILBOX_ADDRESS", mailbox),
        ) if not value
    ]
    if missing:
        return InboxView(
            configured=False, mailbox=mailbox, messages=[],
            error="Mailbox connection is not configured (" + ", ".join(missing) + ").",
        )

    try:
        token = _token()
        messages: List[InboxMessage] = []
        total = 0
        # Every source folder, for the same reason the ingest reads them all: a delivery
        # notification Exchange filed as junk is still a delivery notification, and "Check now"
        # is the control someone presses precisely when they are looking for a mail that has not
        # turned up.
        for folder in settings.MAILBOX_SOURCE_FOLDERS:
            found, count = _list(token, mailbox, folder)
            messages.extend(found)
            total += count or 0
        messages.sort(key=lambda m: m.received, reverse=True)
        return InboxView(configured=True, mailbox=mailbox, messages=messages, total=total)
    except Exception as exc:                                   # noqa: BLE001 - shown to the user
        return InboxView(configured=True, mailbox=mailbox, messages=[],
                         error=f"{type(exc).__name__}: {exc}")


def _token() -> str:
    """Reuse one msal application, so its token cache survives between requests.

    Building a fresh `ConfidentialClientApplication` per call threw that cache away and cost a
    measured 1.14s of re-authentication on every single page load.
    """
    global _msal_app
    import msal

    with _lock:
        if _msal_app is None:
            _msal_app = msal.ConfidentialClientApplication(
                settings.GRAPH_CLIENT_ID,
                authority=settings.GRAPH_AUTHORITY_TEMPLATE.format(
                    tenant_id=settings.GRAPH_TENANT_ID),
                client_credential=settings.GRAPH_CLIENT_SECRET,
            )
        app = _msal_app

    result = app.acquire_token_silent(settings.GRAPH_SCOPE, account=None)
    if not result:
        result = app.acquire_token_for_client(scopes=settings.GRAPH_SCOPE)
    if "access_token" not in result:
        raise RuntimeError(f"sign-in failed: {result.get('error_description') or result.get('error')}")
    return result["access_token"]


def _list(token: str, mailbox: str,
          folder: str = "inbox") -> Tuple[List[InboxMessage], Optional[int]]:
    import requests

    headers = {"Authorization": f"Bearer {token}"}
    url = f"{GRAPH_BASE_URL}/users/{mailbox}/mailFolders/{folder}/messages"
    params = {
        "$top": PAGE_SIZE,
        "$orderby": "receivedDateTime desc",
        "$select": "receivedDateTime,subject,from,hasAttachments,internetMessageId",
        "$count": "true",
    }
    headers["ConsistencyLevel"] = "eventual"

    messages: List[InboxMessage] = []
    total: Optional[int] = None
    pages = 0
    while url and pages < MAX_PAGES:
        response = requests.get(url, headers=headers, params=params if pages == 0 else None,
                                timeout=30)
        response.raise_for_status()
        payload = response.json()
        if total is None:
            total = payload.get("@odata.count")
        for item in payload.get("value", []):
            sender = (((item.get("from") or {}).get("emailAddress") or {}).get("address") or "")
            messages.append(InboxMessage(
                received=str(item.get("receivedDateTime") or "")[:16].replace("T", " "),
                sender=sender,
                subject=item.get("subject") or "",
                has_attachments=bool(item.get("hasAttachments")),
                internet_message_id=item.get("internetMessageId") or "",
                source_folder=folder,
            ))
        url = payload.get("@odata.nextLink")
        pages += 1

    return messages, total
