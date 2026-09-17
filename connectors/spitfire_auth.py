"""Where a Spitfire credential comes from, and the one login every client in the process shares.

**How authentication works.** sfPMS has no bearer token and no API key we hold. `POST /api/Account`
takes `SiteLogin {UID, PW, IsHashed, tzOffset}` and answers with `Set-Cookie`: `sfPMSAuth` (the
FormsAuthentication ticket — the thing that actually authenticates), plus `sfSession` and
`sfSettings`. Every later request carries those cookies and Spitfire authorises it against that
user's own permissions. A browser does exactly the same, which is why a hand-copied `sfPMSAuth`
also works — it is just a ticket someone else logged in for. Proven with the dedicated account on
Training 2026-09-15: login OK, `GET /api/account/session` true.

**Two modes, chosen from configuration alone:**

* `login`  — `SPITFIRE_UID` and `SPITFIRE_PW` are set. The code logs in, and logs in again when the
  ticket lapses on idle. This is the mode for Training *and* Production; only `.env` differs.
* `cookie` — no UID/PW, but `SPITFIRE_SESSION_COOKIE` is set. A borrowed browser ticket that cannot
  be renewed. Development only.

Login wins when both are present, so a stale cookie left in `.env` can never shadow the account.

**Changing a credential means editing `.env` and nothing else.** `credentials()` re-reads the file
every time it is asked, and it is asked on every fresh login — so a new password is picked up the
next time the ticket lapses, or immediately on restart. Where there is no `.env` (a deployed host
with secrets injected as environment variables) it falls back to `config.settings`.

**One ticket per process.** The warehouse sweep opens a client per worker thread; if each logged
in, eight threads would hold eight sessions for one account. Tickets are cached here per
(host, user) under a lock, so the first login is reused and a lapse triggers a single re-login.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

from dotenv import dotenv_values

from config import settings

LOGIN = "login"
COOKIE = "cookie"
NONE = "none"

ENV_FILE: Optional[Path] = settings.BASE_DIR / ".env"
"""Re-read on every `credentials()` call. The test suite sets this to None so a developer's real
account in `.env` can never reach a test."""

_lock = threading.RLock()
_tickets: Dict[Tuple[str, str], Dict[str, str]] = {}


@dataclass(frozen=True)
class Credentials:
    uid: str
    pw: str

    def __repr__(self) -> str:          # never let a password reach a log line or a traceback
        return f"Credentials(uid={self.uid!r}, pw='***')"


def _current(name: str) -> Optional[str]:
    """`.env` as it is on disk now, else what `settings` loaded at startup."""
    if ENV_FILE is not None and ENV_FILE.exists():
        value = (dotenv_values(ENV_FILE).get(name) or "").strip()
        if value:
            return value
    value = getattr(settings, name, None)
    return str(value).strip() if value else None


def credentials() -> Optional[Credentials]:
    """The account to log in with, or None when either half is missing."""
    uid, pw = _current("SPITFIRE_UID"), _current("SPITFIRE_PW")
    return Credentials(uid, pw) if uid and pw else None


def session_cookie() -> Optional[str]:
    return _current("SPITFIRE_SESSION_COOKIE")


def mode() -> str:
    if credentials():
        return LOGIN
    if session_cookie():
        return COOKIE
    return NONE


# --- the shared ticket --------------------------------------------------------------------------

def lock() -> threading.RLock:
    """Held around a login, so concurrent lapses produce one `POST /api/Account`, not one each."""
    return _lock


def cached_ticket(base_url: str, uid: str) -> Optional[Dict[str, str]]:
    with _lock:
        ticket = _tickets.get((base_url.rstrip("/").lower(), uid.lower()))
        return dict(ticket) if ticket else None


def store_ticket(base_url: str, uid: str, cookies: Dict[str, str]) -> None:
    with _lock:
        _tickets[(base_url.rstrip("/").lower(), uid.lower())] = dict(cookies)


def forget_tickets() -> None:
    with _lock:
        _tickets.clear()


def auth_ticket_value(base_url: Optional[str] = None) -> Optional[str]:
    """The `sfPMSAuth` value for tools that build their own `requests.Session`: a fresh login when
    an account is configured, else the configured cookie, else None."""
    if mode() == LOGIN:
        return login_ticket(base_url).get("sfPMSAuth")
    return session_cookie()


def login_ticket(base_url: Optional[str] = None) -> Dict[str, str]:
    """A live ticket for the configured account: reused if still valid, renewed if it lapsed.

    This is how the write client gets authenticated without being able to log in itself —
    `POST /api/Account` stays off `_ALLOWED_WRITES` and goes through the read connector, where it
    has always been allowlisted.
    """
    from connectors.spitfire import SpitfireReadClient    # late: spitfire imports this module

    client = SpitfireReadClient(base_url=base_url)
    if not client.login_mode:
        raise RuntimeError("no Spitfire login is configured: set SPITFIRE_UID and SPITFIRE_PW in .env")
    client.ensure_session()
    return client.ticket_cookies()
