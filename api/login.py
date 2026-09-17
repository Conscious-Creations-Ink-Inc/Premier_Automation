"""The sign-in page. The only routes in the app that do not require a session.

Kept on its own router, outside the `Depends(auth.require_session)` that guards `/ui` and `/api`,
because a login page behind a login gate is a redirect loop. That is the whole reason this is a
separate module: the exemption is one small file you can read in full, rather than a condition
inside the guard that has to be reasoned about.
"""
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from api import auth, auth_store
from api.forms import form_values
from api.ui import html
from config import settings

router = APIRouter(tags=["auth"], include_in_schema=False)

_GENERIC_FAILURE = "That username and password do not match."
"""One message for both "no such user" and "wrong password". Telling them apart tells an attacker
which usernames exist, and tells the person who mistyped nothing they can act on."""


def _safe_next(target: Optional[str]) -> str:
    """Where to go after signing in -- but only somewhere inside this app.

    `?next=` is attacker-controlled: left unchecked it turns the login page into an open redirect,
    where a link that really does sign you in then lands you on someone else's copy of it. Anything
    carrying a scheme or a host is discarded, as is anything not starting with a single `/`
    (`//evil.test` is a protocol-relative URL, not a local path).
    """
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/ui"
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return "/ui"
    return target


def _cookie_kwargs(request: Request) -> dict:
    """`Secure` only where it would not lock the app out of its own front door.

    This app is served over plain HTTP on 127.0.0.1. A `Secure` cookie is never sent over http://,
    so setting it unconditionally would mean signing in successfully and arriving back at the login
    page for ever. It is set whenever the request did arrive over TLS, so putting this behind a
    proxy later tightens it automatically.
    """
    return {
        "httponly": True,                 # unreadable from JavaScript, so an XSS cannot take it
        "samesite": "lax",                # a cross-site POST arrives without it -- the CSRF defence
        "secure": request.url.scheme == "https",
        "path": "/",
    }


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "", error: str = "", reset: int = 0):
    conn = auth_store.get_connection()
    try:
        first_run = auth_store.user_count(conn) == 0
    finally:
        conn.close()
    if not first_run and auth.current_user(request):
        # Already signed in. Showing the form again would invite a pointless second sign-in, and
        # 303 rather than 307 so the browser lands on it with a GET.
        return RedirectResponse(_safe_next(next), status_code=303)
    return HTMLResponse(html.login_page(next=_safe_next(next), error=error, first_run=first_run,
                                        can_set_up=first_run and _is_local(request),
                                        resetting=bool(reset) and not first_run
                                        and _is_local(request),
                                        can_reset=not first_run and _is_local(request)))


@router.post("/login")
async def sign_in(request: Request) -> RedirectResponse:
    """Fields come from `form_values`, not from `Form(...)` parameters.

    Declaring a `Form()` parameter makes FastAPI import python-multipart at the moment the route is
    defined, and this app deliberately does not have it -- see `api/forms.py`. With the library
    absent, a `Form()` here does not fail at request time, it fails at import time and the whole
    app stops booting.
    """
    posted = await form_values(request)
    username = str(posted.get("username") or "")
    password = str(posted.get("password") or "")
    next = str(posted.get("next") or "")
    conn = auth_store.get_connection()
    try:
        row = auth_store.find_user(conn, username)
        if row is None or not auth.verify_password(password, row["password_hash"]):
            # Deliberately no logging of the attempt's password, and no hint about which half was
            # wrong. The username is not echoed back into the URL either -- it would be reflected
            # into the page and sit in the browser's history.
            return RedirectResponse(f"/login?next={_q(_safe_next(next))}&error=1", status_code=303)
        auth_store.record_login(conn, username=row["username"],
                                now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        token = auth.issue_cookie(conn, row["username"],
                                  ttl_seconds=settings.SESSION_TTL_HOURS * 3600)
    finally:
        conn.close()
    response = RedirectResponse(_safe_next(next), status_code=303)
    response.set_cookie(settings.SESSION_COOKIE_NAME, token,
                        max_age=settings.SESSION_TTL_HOURS * 3600, **_cookie_kwargs(request))
    return response


@router.post("/login/set-up")
async def first_admin(request: Request) -> RedirectResponse:
    """Create the very first account, from the browser, on the machine the app runs on.

    Gated on two conditions that must **both** hold, checked here and not only in the template that
    offers the form: there is no account yet, and the request came from loopback. The first makes
    this unreachable the moment an account exists; the second means it was never reachable from
    another machine. `tools/create_admin.py` is the equivalent without either condition, and is the
    auditable path.
    """
    posted = await form_values(request)
    username = str(posted.get("username") or "")
    password = str(posted.get("password") or "")
    confirm = str(posted.get("confirm") or "")
    conn = auth_store.get_connection()
    try:
        if auth_store.user_count(conn) != 0 or not _is_local(request):
            return RedirectResponse("/login", status_code=303)
        username = (username or "").strip()
        if not username or len(password) < 12 or password != confirm:
            return RedirectResponse("/login?error=setup", status_code=303)
        auth_store.create_user(conn, username=username,
                               password_hash=auth.hash_password(password),
                               now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        token = auth.issue_cookie(conn, username, ttl_seconds=settings.SESSION_TTL_HOURS * 3600)
    finally:
        conn.close()
    response = RedirectResponse("/ui", status_code=303)
    response.set_cookie(settings.SESSION_COOKIE_NAME, token,
                        max_age=settings.SESSION_TTL_HOURS * 3600, **_cookie_kwargs(request))
    return response


@router.post("/login/reset")
async def reset_password(request: Request) -> RedirectResponse:
    """Set a new password for an existing account, from the browser, without the old one.

    **Gated on loopback, checked here and not only in the template that offers the form** — the
    same two-place rule `first_admin` follows. Off this machine the route does nothing and says
    nothing about why.

    Why no verification of the old password: it cannot be verified by anyone. `app_user` stores a
    PBKDF2-SHA256 digest and nothing else, so a forgotten password has no recovery path, only a
    replacement. And `tools/create_admin.py --reset-password` already does exactly this with no
    check beyond being able to run it on this machine — so on a loopback-only binding this grants
    no power that local access did not already carry. `api.config.API_HOST` binds 127.0.0.1;
    if that ever becomes `0.0.0.0`, `_is_local` is what keeps this from becoming a remote takeover
    of the only administrator account.

    The session secret is rotated, so every cookie signed with the old key stops working. A
    password changed because it may have leaked has not been changed at all if the session it
    leaked from keeps running — the same reasoning as the reset path in `tools/create_admin.py`.
    """
    posted = await form_values(request)
    username = str(posted.get("username") or "").strip()
    password = str(posted.get("password") or "")
    confirm = str(posted.get("confirm") or "")
    conn = auth_store.get_connection()
    try:
        if not _is_local(request):
            return RedirectResponse("/login", status_code=303)
        if not username or len(password) < 12 or password != confirm:
            return RedirectResponse("/login?reset=1&error=reset", status_code=303)
        if auth_store.find_user(conn, username) is None:
            # Same shape as a bad password, and for the same reason the sign-in page gives one
            # message for both: telling them apart tells a caller which usernames exist.
            return RedirectResponse("/login?reset=1&error=reset", status_code=303)
        auth_store.set_password(conn, username=username,
                                password_hash=auth.hash_password(password))
        auth_store.rotate_session_secret(conn)
        auth.forget_cached_secret()
        # Signed in on the way out, so a reset lands where the person was going rather than back
        # at a form they have just satisfied. Issued *after* the rotation, so it is signed with
        # the new key.
        token = auth.issue_cookie(conn, username, ttl_seconds=settings.SESSION_TTL_HOURS * 3600)
    finally:
        conn.close()
    response = RedirectResponse("/ui", status_code=303)
    response.set_cookie(settings.SESSION_COOKIE_NAME, token,
                        max_age=settings.SESSION_TTL_HOURS * 3600, **_cookie_kwargs(request))
    return response


@router.post("/logout")
def sign_out(request: Request) -> RedirectResponse:
    """POST, never GET. A link that signs you out can be triggered by anything that prefetches it --
    a chat client unfurling the URL, the browser itself -- and the sidebar's control is a form."""
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(settings.SESSION_COOKIE_NAME, path="/")
    return response


def _is_local(request: Request) -> bool:
    return (request.client.host if request.client else "") in ("127.0.0.1", "::1", "localhost")


def _q(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="/?=&")

