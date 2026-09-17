"""Signing in: password hashing, the session cookie, and the dependency that guards every route.

**Standard library only.** `itsdangerous` is not installed, so Starlette's `SessionMiddleware` is
unavailable, and neither `passlib` nor `bcrypt` is present either -- and CLAUDE.md s1 forbids
installing any of them. Everything below is `hashlib`, `hmac` and `secrets`, which is sufficient:
PBKDF2-SHA256 is what `hashlib.pbkdf2_hmac` exists for, and a signed cookie is an HMAC.

The threat this is actually built against is a person on the network reaching a port that can post
receipts into Spitfire. It is not built against an attacker who already has the auth database --
that person has the signing key too, and no cookie scheme survives that.

**No credential literal appears in this file** (CLAUDE.md s3). There is no default account, no
fallback password and no bypass: an empty `app_user` table admits nobody, and `require_session`
fails closed on every error it can encounter.
"""
import base64
import contextvars
import hashlib
import hmac
import secrets
import time
from typing import Optional

from fastapi import Request

from api import auth_store

# PBKDF2 cost. Deliberately a constant rather than a setting: a number that can be turned down from
# the environment is a number that will be, and the stored hash records the cost it was made with
# so raising this later does not lock anybody out -- `verify_password` reads the cost from the hash.
_PBKDF2_ROUNDS = 600_000
_ALGORITHM = "pbkdf2_sha256"
_SECRET_KEY_NAME = "session_secret"


class NotAuthenticated(Exception):
    """Raised by `require_session`. `api.main` turns it into a redirect or a 401.

    An exception rather than a returned response because a FastAPI dependency cannot return one,
    and because failing closed should be the *only* thing a guard can do -- there is no code path
    here that returns "not signed in, carry on".
    """


# --------------------------------------------------------------------------- passwords

def hash_password(password: str, *, rounds: int = _PBKDF2_ROUNDS) -> str:
    """`pbkdf2_sha256$<rounds>$<salt>$<digest>`, salt and digest base64url, no padding.

    The salt is per password and from `secrets`, so two people choosing the same password store
    different rows, and a precomputed table is worth nothing here.
    """
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return f"{_ALGORITHM}${rounds}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of a password against a stored hash.

    `hmac.compare_digest`, never `==`: a byte-by-byte comparison returns sooner for a wrong first
    character than a wrong last one, and that difference is measurable over enough attempts.

    Any malformed, empty or unknown-algorithm hash is a `False`, not an exception -- a corrupt row
    must read as "wrong password", never as "no password required".
    """
    try:
        algorithm, rounds, salt, digest = stored.split("$")
        if algorithm != _ALGORITHM:
            return False
        computed = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), _unb64(salt), int(rounds))
        return hmac.compare_digest(computed, _unb64(digest))
    except Exception:                                              # noqa: BLE001
        return False


# --------------------------------------------------------------------------- session cookie

def session_secret(conn) -> bytes:
    """The key this store's cookies are signed with, minted once on first use.

    In the database rather than an environment variable so that a fresh checkout cannot run with a
    key someone committed, and so that invalidating every session is `DELETE FROM app_secret`.
    """
    existing = auth_store.get_secret(conn, _SECRET_KEY_NAME)
    if existing:
        return _unb64(existing)
    return _unb64(auth_store.put_secret(conn, _SECRET_KEY_NAME, _b64(secrets.token_bytes(32))))


def issue_cookie(conn, username: str, *, ttl_seconds: int) -> str:
    """A signed, self-describing token: `<username>|<expires-at>|<signature>`.

    Self-describing so that checking it costs no database read -- this is checked on every request,
    including the pollers, and a session table would mean a query per poll per open tab.

    The expiry is *inside* the signed payload as well as on the cookie. A cookie's own `Max-Age` is
    a request from the server that the browser is free to ignore; this one is not negotiable.
    """
    expires = int(time.time()) + ttl_seconds
    payload = f"{username}|{expires}"
    return f"{payload}|{_sign(payload, session_secret(conn))}"


def read_cookie(conn, token: str) -> Optional[str]:
    """The username this token proves, or None if it proves nothing."""
    return verify_token(token, session_secret(conn))


def verify_token(token: str, secret: bytes) -> Optional[str]:
    """The same check with the key already in hand, so it needs no database.

    Signature first, then expiry: an expired token whose signature is wrong is a forgery, and
    checking in this order means neither answer tells the holder which it was.
    """
    try:
        username, expires, signature = token.rsplit("|", 2)
        expected = _sign(f"{username}|{expires}", secret)
        if not hmac.compare_digest(signature, expected):
            return None
        if int(expires) < int(time.time()):
            return None
        return username or None
    except Exception:                                              # noqa: BLE001
        return None


_SECRET_CACHE: dict = {}


def forget_cached_secret() -> None:
    """Drop the memoised signing key, after something has rotated it."""
    _SECRET_CACHE.clear()


def cached_secret() -> bytes:
    """This store's signing key, read once per process instead of once per request.

    Checking a cookie is otherwise a database open on every single request -- including the
    `/ui/version` poll every ten seconds in every open tab -- to fetch one 32-byte value that never
    changes while the process runs. Keyed by the store's path, so a test pointing at a fresh
    temporary database gets that database's key and not the last one's.
    """
    from config import settings

    key = str(settings.AUTH_DB_PATH)
    if key not in _SECRET_CACHE:
        conn = auth_store.get_connection()
        try:
            _SECRET_CACHE[key] = session_secret(conn)
        finally:
            conn.close()
    return _SECRET_CACHE[key]


def _sign(payload: str, secret: bytes) -> str:
    return _b64(hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).digest())


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# --------------------------------------------------------------------------- the guard

def current_user(request: Request) -> Optional[str]:
    """Who this request is, or None. Never raises -- `require_session` is what refuses."""
    from config import settings

    token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if not token:
        return None
    return verify_token(token, cached_secret())


async def require_session(request: Request) -> str:
    """Every `/ui` and `/api` route depends on this. Returns the signed-in username.

    Attached once, at the router, rather than to 55 route signatures: a guard that must be repeated
    is a guard that will be left off the route added next month.

    Tests replace it through `app.dependency_overrides` -- see `tests/conftest.py`. That is the only
    supported way past it, it exists only in-process, and it cannot be reached over HTTP.

    **`async def` on purpose, and it matters.** FastAPI runs a *sync* dependency in a worker thread
    with a copy of the context, so `_signed_in.set()` there would mutate the copy and be thrown away
    the moment the thread finished -- the rail rendered no name at all, and the only symptom was a
    missing sign-out control. An async dependency runs in the request's own context, so the value
    reaches the renderer. It can afford to: with `cached_secret()` this does no I/O, only an HMAC.
    """
    username = current_user(request)
    if not username:
        raise NotAuthenticated()
    request.state.username = username
    _signed_in.set(username)
    return username


_signed_in: contextvars.ContextVar = contextvars.ContextVar("premier_signed_in", default="")
"""Who the request being served belongs to.

A context variable rather than a parameter because the alternative is threading `request` through
ten page handlers, `_chrome()` and `page()` purely so the rail can print a name -- and every one of
those signatures exists to describe what the page *shows*, not who is looking. anyio copies the
context into the worker thread a sync endpoint runs on, so this reaches the renderer intact.

Empty when nothing has been set, which is what the tests' dependency override leaves it as. The
rail treats that as "no name to show", never as "signed out": what a page renders must not become a
second, weaker answer to a question `require_session` has already answered."""


def signed_in_user() -> str:
    """The current request's username, or "" if there is none to show."""
    return _signed_in.get()
