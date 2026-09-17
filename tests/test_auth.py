"""The sign-in gate: that it holds, that it fails closed, and that it cannot be talked around.

No password in this file is a real credential (CLAUDE.md s3) -- every one is invented here and
never leaves the temporary store the fixture builds.
"""
import re
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import auth, auth_store, login
from api.main import app
from config import settings

PASSWORD = "a-sufficiently-long-test-password"
WRONG = "not-the-right-password"
USERNAME = "test-operator"


@pytest.fixture
def signed_out(tmp_path, monkeypatch):
    """A TestClient with the gate genuinely in force and one account in a throwaway store.

    The suite-wide fixture in `conftest.py` overrides `require_session` so the other 113 client
    call sites do not each have to sign in. Here that override is removed again: these are the
    tests of the guard itself, and a guard tested through its own bypass is not tested.
    """
    monkeypatch.setattr(settings, "AUTH_DB_PATH", tmp_path / "auth.sqlite3")
    auth_store.reset_schema_cache()
    app.dependency_overrides.pop(auth.require_session, None)
    conn = auth_store.get_connection()
    auth_store.create_user(conn, username=USERNAME, password_hash=auth.hash_password(PASSWORD),
                           now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    conn.close()
    with TestClient(app, follow_redirects=False) as client:
        yield client


# --------------------------------------------------------------------------- the gate

@pytest.mark.parametrize("path", ["/ui", "/ui/mails", "/ui/records", "/ui/attachments",
                                  "/ui/manual", "/ui/po", "/ui/automation", "/ui/report"])
def test_a_page_is_not_served_to_a_stranger(signed_out, path):
    response = signed_out.get(path)
    assert response.status_code == 303, path
    assert response.headers["location"].startswith("/login"), path


def test_the_redirect_remembers_where_you_were_going(signed_out):
    response = signed_out.get("/ui/attachments?view=all")
    assert "next=" in response.headers["location"]
    assert "attachments" in response.headers["location"]


def test_a_json_route_is_refused_rather_than_redirected(signed_out):
    """A 303 to an HTML page is a useless answer to a fetch(). The `/ui/version` poller runs on
    every open tab, and a redirect would have it parsing a login page as JSON for ever."""
    response = signed_out.get("/ui/version", headers={"accept": "application/json"})
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")


def test_a_write_is_refused_too(signed_out):
    """The gate exists because these routes post receipts into a financial system of record."""
    response = signed_out.post("/ui/records/1/verify", data={"by": "nobody"})
    assert response.status_code in (303, 401)
    assert not response.cookies.get(settings.SESSION_COOKIE_NAME)


def test_the_login_page_itself_is_reachable(signed_out):
    assert signed_out.get("/login").status_code == 200


def test_the_health_check_stays_open(signed_out):
    """So a supervisor can tell "the app is down" from "the app will not talk to you"."""
    assert signed_out.get("/healthz").status_code == 200


# --------------------------------------------------------------------------- signing in

def test_the_right_password_signs_you_in(signed_out):
    response = signed_out.post("/login", data={"username": USERNAME, "password": PASSWORD,
                                               "next": "/ui/records"})
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/records"
    assert response.cookies.get(settings.SESSION_COOKIE_NAME)


def test_and_then_the_pages_open(signed_out):
    signed_out.post("/login", data={"username": USERNAME, "password": PASSWORD})
    assert signed_out.get("/ui/mails").status_code == 200


def test_the_wrong_password_does_not(signed_out):
    response = signed_out.post("/login", data={"username": USERNAME, "password": WRONG})
    assert response.status_code == 303
    assert "/login" in response.headers["location"]
    assert not response.cookies.get(settings.SESSION_COOKIE_NAME)


def test_an_unknown_user_is_refused_the_same_way_as_a_wrong_password(signed_out):
    """Two different answers here is an oracle for which usernames exist."""
    missing = signed_out.post("/login", data={"username": "nobody", "password": PASSWORD})
    wrong = signed_out.post("/login", data={"username": USERNAME, "password": WRONG})
    assert missing.status_code == wrong.status_code
    assert missing.headers["location"] == wrong.headers["location"]


def test_signing_out_clears_the_cookie(signed_out):
    signed_out.post("/login", data={"username": USERNAME, "password": PASSWORD})
    assert signed_out.get("/ui/mails").status_code == 200
    signed_out.post("/logout")
    assert signed_out.get("/ui/mails").status_code == 303


def test_the_session_cookie_is_httponly_and_samesite(signed_out):
    response = signed_out.post("/login", data={"username": USERNAME, "password": PASSWORD})
    header = response.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=lax" in header


# --------------------------------------------------------------------------- open redirect

@pytest.mark.parametrize("target", ["https://evil.test/steal", "//evil.test/steal",
                                    "http://evil.test", "javascript:alert(1)"])
def test_the_next_parameter_cannot_leave_the_app(signed_out, target):
    """Otherwise a link that really does sign you in lands you on somebody else's copy of it."""
    response = signed_out.post("/login", data={"username": USERNAME, "password": PASSWORD,
                                               "next": target})
    assert response.headers["location"] == "/ui", target


# --------------------------------------------------------------------------- the primitives

def test_a_password_is_never_stored_in_the_clear(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_DB_PATH", tmp_path / "auth.sqlite3")
    auth_store.reset_schema_cache()
    conn = auth_store.get_connection()
    auth_store.create_user(conn, username="a", password_hash=auth.hash_password(PASSWORD),
                           now="2026-09-16 00:00:00")
    conn.close()
    assert PASSWORD.encode() not in Path(tmp_path / "auth.sqlite3").read_bytes()


def test_the_same_password_hashes_differently_every_time():
    """Per-password salt, so one stolen hash never matches another row."""
    assert auth.hash_password(PASSWORD) != auth.hash_password(PASSWORD)


@pytest.mark.parametrize("broken", ["", "not-a-hash", "md5$1$x$y",
                                    "pbkdf2_sha256$notanumber$x$y", "pbkdf2_sha256$1$x"])
def test_an_unreadable_hash_reads_as_wrong_password_not_as_no_password(broken):
    """The direction of this failure is the whole point: a corrupt row must lock people out, not
    let them in."""
    assert auth.verify_password(PASSWORD, broken) is False


def test_a_tampered_cookie_proves_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_DB_PATH", tmp_path / "auth.sqlite3")
    auth_store.reset_schema_cache()
    conn = auth_store.get_connection()
    token = auth.issue_cookie(conn, "admin", ttl_seconds=60)
    assert auth.read_cookie(conn, token) == "admin"
    assert auth.read_cookie(conn, token.replace("admin", "root", 1)) is None
    assert auth.read_cookie(conn, "admin|99999999999|" + token.rsplit("|", 1)[1]) is None
    assert auth.read_cookie(conn, "garbage") is None
    conn.close()


def test_an_expired_cookie_stops_working(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_DB_PATH", tmp_path / "auth.sqlite3")
    auth_store.reset_schema_cache()
    conn = auth_store.get_connection()
    assert auth.read_cookie(conn, auth.issue_cookie(conn, "admin", ttl_seconds=-1)) is None
    conn.close()


def test_the_auth_store_never_touches_the_pipeline(tmp_path, monkeypatch):
    """Signing in must not open the 2.12 GB live store, and must not be able to read mail."""
    monkeypatch.setattr(settings, "AUTH_DB_PATH", tmp_path / "auth.sqlite3")
    auth_store.reset_schema_cache()
    conn = auth_store.get_connection()
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.close()
    assert tables == {"app_user", "app_secret"}


def test_the_rail_shows_who_you_are_and_the_way_out(signed_out):
    """Proves the context variable survives the hop onto the thread a sync endpoint runs on.

    `auth.signed_in_user()` is read in `_chrome`, several frames and one threadpool away from the
    dependency that sets it. If anyio did not copy the context across, this would render an empty
    name and nobody would notice until someone tried to sign out.
    """
    signed_out.post("/login", data={"username": USERNAME, "password": PASSWORD})
    body = signed_out.get("/ui/mails").text
    assert f"Signed in as {USERNAME}" in body
    assert 'action="/logout"' in body


def test_there_is_no_way_to_sign_out_with_a_bare_link(signed_out):
    """A GET that signs you out fires on any prefetch or link unfurl."""
    signed_out.post("/login", data={"username": USERNAME, "password": PASSWORD})
    assert signed_out.get("/logout").status_code in (404, 405)
    assert signed_out.get("/ui/mails").status_code == 200


# --------------------------------------------------------------------------- the reveal button

def test_every_password_box_has_an_eye(signed_out):
    """Typing a 12-character password blind, twice, into a box that will only say "those did not
    match" is the worst part of creating an account. Both boxes get their own toggle."""
    body = signed_out.get("/login").text
    assert body.count('class="reveal"') == 1
    assert 'aria-controls="f-password"' in body


def test_the_eye_is_never_a_submit_button():
    """A `<button>` inside a form submits it unless told otherwise. An eye that signs you in on
    the way past -- with a half-typed password -- is worse than having no eye."""
    from api.ui import html

    for markup in (html.login_page(), html.login_page(first_run=True, can_set_up=True)):
        for button in re.findall(r"<button[^>]*class=\"reveal\"[^>]*>", markup):
            assert 'type="button"' in button, button


def test_the_setup_form_toggles_each_box_on_its_own():
    """Two boxes, two buttons, each naming its own input. One shared toggle would reveal the
    password while you are checking the repeat, which is the opposite of what it is for."""
    from api.ui import html

    markup = html.login_page(first_run=True, can_set_up=True)
    assert markup.count('class="reveal"') == 2
    controls = re.findall(r'data-reveal="([^"]+)"', markup)
    assert sorted(controls) == ["f-confirm", "f-password"]


def test_the_login_page_still_carries_only_its_own_small_script():
    """It gained one for the eye -- there is no CSS-only way to turn a password box into a text
    box -- but it must not have picked up the app's main script, which polls a route a signed-out
    visitor is refused, nor any inline handler."""
    from api.ui import html

    markup = html.login_page()
    assert markup.count("<script") == 1
    assert "<script src" not in markup
    assert html._JS not in markup, "the login page must not load the gated app's script"
    # Nothing is fetched from this page at all. Named as the call rather than as the path, because
    # the script's own comment explains *why* it does not poll `/ui/version` and would match.
    assert "fetch(" not in markup, "the signed-out page must not call anything"
    for handler in ("onclick=", "onload=", "onerror=", "javascript:"):
        assert handler not in markup, handler


def test_the_password_is_still_a_password_when_the_page_arrives():
    """Revealed is a thing you ask for, never the state the page comes back in."""
    from api.ui import html

    markup = html.login_page()
    assert 'type="password"' in markup
    # Read off the buttons themselves. A bare search for `aria-pressed="true"` also finds the CSS
    # rule that styles the pressed state, which is not a button and is always in the document.
    buttons = re.findall(r"<button[^>]*class=\"reveal\"[^>]*>", markup)
    assert buttons
    for button in buttons:
        assert 'aria-pressed="false"' in button, button


def test_the_eye_stays_the_size_of_an_eye():
    """It was 300px wide, stretched across the whole password box, with the icon parked in the
    middle of the typed text.

    `.login-card button { width:100% }` — written for the Create account button — also matched the
    reveal button and beat `.reveal` on specificity. Nothing in the markup changes when this
    happens, so no rendered-HTML assertion can catch it; the rule itself has to be asserted.
    Measured in Chrome afterwards: the button is 34px at the field's right edge.
    """
    from api.ui import html

    assert ".login-card button.btn {" in html._LOGIN_CSS, \
        "the full-width rule must name the submit button, not every button on the card"
    assert ".login-card button {" not in html._LOGIN_CSS
    assert "min-width:34px; max-width:34px" in html._REVEAL_CSS, \
        "the eye must not be stretchable by a later rule"


# --------------------------------------------------------------------------- resetting a password
#
# A forgotten password has no recovery path — `app_user` stores a PBKDF2-SHA256 digest and nothing
# else — so the only route is replacement. `tools/create_admin.py --reset-password` already does
# this with no check beyond being able to run it on the machine, and these routes are the same
# power through the browser, held to the same condition: loopback, checked in `api.login` and not
# only in the template that offers the form.

NEW_PASSWORD = "a-brand-new-password"


@pytest.fixture
def on_this_machine(tmp_path, monkeypatch):
    """`signed_out`, but the request genuinely arrives from loopback.

    `TestClient` reports its host as `testclient`, which `_is_local` correctly rejects — so the
    default client cannot exercise these routes at all. Giving it a real 127.0.0.1 address tests
    the actual predicate rather than a stand-in for it.
    """
    monkeypatch.setattr(settings, "AUTH_DB_PATH", tmp_path / "auth.sqlite3")
    auth_store.reset_schema_cache()
    app.dependency_overrides.pop(auth.require_session, None)
    conn = auth_store.get_connection()
    auth_store.create_user(conn, username=USERNAME, password_hash=auth.hash_password(PASSWORD),
                           now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    conn.close()
    with TestClient(app, follow_redirects=False, client=("127.0.0.1", 50000)) as client:
        yield client


def test_a_stranger_cannot_reset_a_password(signed_out, monkeypatch):
    """The guard that matters. `api.config.API_HOST` binds 127.0.0.1 today; if it ever became
    0.0.0.0 this is what stands between a stranger and the only administrator account."""
    monkeypatch.setattr(login, "_is_local", lambda request: False)

    response = signed_out.post("/login/reset", data={
        "username": USERNAME, "password": NEW_PASSWORD, "confirm": NEW_PASSWORD})

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    conn = auth_store.get_connection()
    try:
        assert auth.verify_password(PASSWORD, auth_store.find_user(conn, USERNAME)["password_hash"])
    finally:
        conn.close()


def test_resetting_replaces_the_password_and_signs_you_in(on_this_machine):
    response = on_this_machine.post("/login/reset", data={
        "username": USERNAME, "password": NEW_PASSWORD, "confirm": NEW_PASSWORD})

    assert response.status_code == 303
    assert response.headers["location"] == "/ui", "a reset lands signed in, not back at the form"
    assert settings.SESSION_COOKIE_NAME in response.cookies

    conn = auth_store.get_connection()
    try:
        stored = auth_store.find_user(conn, USERNAME)["password_hash"]
    finally:
        conn.close()
    assert auth.verify_password(NEW_PASSWORD, stored)
    assert not auth.verify_password(PASSWORD, stored), "the old password must stop working"


def test_a_reset_ends_every_session_signed_with_the_old_key(on_this_machine):
    """A password changed because it may have leaked has not been changed at all if the session it
    leaked from keeps running."""
    conn = auth_store.get_connection()
    try:
        before = auth.issue_cookie(conn, USERNAME, ttl_seconds=3600)
    finally:
        conn.close()

    on_this_machine.post("/login/reset", data={
        "username": USERNAME, "password": NEW_PASSWORD, "confirm": NEW_PASSWORD})

    conn = auth_store.get_connection()
    try:
        assert auth.read_cookie(conn, before) is None
    finally:
        conn.close()


def test_a_reset_that_does_not_add_up_changes_nothing(on_this_machine):
    """Mismatched, too short, and an account that does not exist — all one answer, because saying
    which one failed says whether that username exists."""
    for payload in ({"username": USERNAME, "password": NEW_PASSWORD, "confirm": "different"},
                    {"username": USERNAME, "password": "short", "confirm": "short"},
                    {"username": "no-such-person", "password": NEW_PASSWORD,
                     "confirm": NEW_PASSWORD}):
        response = on_this_machine.post("/login/reset", data=payload)
        assert response.status_code == 303
        assert response.headers["location"] == "/login?reset=1&error=reset", payload

    conn = auth_store.get_connection()
    try:
        assert auth.verify_password(PASSWORD, auth_store.find_user(conn, USERNAME)["password_hash"])
    finally:
        conn.close()


def test_the_reset_form_is_offered_only_on_this_machine(on_this_machine, monkeypatch):
    assert "/login?reset=1" in on_this_machine.get("/login").text

    monkeypatch.setattr(login, "_is_local", lambda request: False)
    page = on_this_machine.get("/login").text
    assert "/login?reset=1" not in page, (
        "a link to a route that would refuse reads as the app being broken")
    assert "/login/reset" not in page

def test_the_reset_form_never_asks_for_the_old_password(on_this_machine):
    """It could not check one. Asking would imply it had, which is worse than not asking."""
    page = on_this_machine.get("/login?reset=1").text
    assert "Reset the password" in page
    assert 'action="/login/reset"' in page
    assert "current-password" not in page
