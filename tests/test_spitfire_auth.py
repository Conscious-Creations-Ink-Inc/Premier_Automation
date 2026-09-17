"""Authenticating with the Spitfire account rather than a hand-copied cookie.

A fake sfPMS stands in for the server at the `requests.Session.request` seam: `POST /api/Account`
issues an `sfPMSAuth` ticket onto the caller's cookie jar exactly as the real one does with
`Set-Cookie`, `GET /api/account/session` answers whether the ticket on the request is still live,
and anything else 401s without one. Nothing here reaches the network.
"""

import threading

import pytest
import requests

from config import settings
from connectors import spitfire_auth, spitfire_cassette
from connectors.spitfire import SpitfireReadClient
from connectors.spitfire_write import SpitfireSessionExpired, SpitfireWriteClient

BASE = "https://spitfire.invalid/Training"
HOST = "spitfire.invalid"


class FakeSpitfire:
    def __init__(self, password="right"):
        self.password = password
        self.live = set()
        self.logins = []          # the UID/PW pairs each login was attempted with
        self.calls = []
        self._n = 0
        self._lock = threading.Lock()

    def expire_all(self):
        self.live.clear()

    def _ticket(self, session):
        return next((c.value for c in session.cookies if c.name == "sfPMSAuth"), None)

    @staticmethod
    def _response(status, payload=None):
        r = requests.Response()
        r.status_code = status
        r._content = (b"" if payload is None else requests.compat.json.dumps(payload).encode())
        r.encoding = "utf-8"
        return r

    def __call__(self, session, method, url, **kwargs):
        path = url[len(BASE):] if url.startswith(BASE) else url
        self.calls.append((method.upper(), path))
        if method.upper() == "POST" and path == "/api/Account":
            body = kwargs.get("json") or {}
            with self._lock:
                self.logins.append((body.get("UID"), body.get("PW")))
                if body.get("PW") != self.password:
                    return self._response(401, {"ThisReason": "Invalid"})
                self._n += 1
                token = f"ticket-{self._n}"
                self.live.add(token)
            session.cookies.set("sfPMSAuth", token, domain=HOST, path="/")
            session.cookies.set("sfSession", f"s-{self._n}", domain=HOST, path="/")
            return self._response(200, True)
        if path == "/api/account/session":
            return self._response(200, self._ticket(session) in self.live)
        if self._ticket(session) not in self.live:
            return self._response(401, {"ThisReason": "Not authenticated"})
        if path == "/api/session/who":
            return self._response(200, {"EMail": "svc@example.invalid", "FullName": "Svc"})
        return self._response(200, {})


@pytest.fixture
def server(monkeypatch):
    fake = FakeSpitfire()
    monkeypatch.setattr(requests.Session, "request",
                        lambda self, method, url, **kw: fake(self, method, url, **kw))
    monkeypatch.setattr(settings, "SPITFIRE_BASE_URL", BASE)
    monkeypatch.setattr(settings, "SPITFIRE_CASSETTE_MODE", "off")
    # Blank here too, not only in the root conftest: a test elsewhere that reloads `settings`
    # would otherwise hand these tests the real account from `.env`.
    monkeypatch.setattr(spitfire_auth, "ENV_FILE", None)
    for name in ("SPITFIRE_UID", "SPITFIRE_PW", "SPITFIRE_SESSION_COOKIE"):
        monkeypatch.setattr(settings, name, None)
    spitfire_auth.forget_tickets()
    yield fake
    spitfire_auth.forget_tickets()


@pytest.fixture
def account(monkeypatch):
    monkeypatch.setattr(settings, "SPITFIRE_UID", "svc-user")
    monkeypatch.setattr(settings, "SPITFIRE_PW", "right")


# --- which credential wins ----------------------------------------------------------------------

def test_the_account_wins_over_a_stale_cookie_left_in_config(server, account, monkeypatch):
    monkeypatch.setattr(settings, "SPITFIRE_SESSION_COOKIE", "stale-browser-ticket")
    assert spitfire_auth.mode() == spitfire_auth.LOGIN
    reader = SpitfireReadClient()
    assert reader.login_mode and not reader.cookie_mode
    assert SpitfireWriteClient().login_mode


def test_the_cookie_is_still_used_when_no_account_is_configured(server, monkeypatch):
    monkeypatch.setattr(settings, "SPITFIRE_SESSION_COOKIE", "browser-ticket")
    assert spitfire_auth.mode() == spitfire_auth.COOKIE
    assert SpitfireReadClient().cookie_mode
    assert not SpitfireWriteClient().login_mode


# --- logging in, sharing, renewing --------------------------------------------------------------

def test_a_read_logs_in_and_then_rides_the_ticket(server, account):
    client = SpitfireReadClient()
    assert client.read("/api/document/abc").status_code == 200
    assert client.read("/api/document/abc").status_code == 200
    assert len(server.logins) == 1


def test_clients_in_one_process_share_a_single_login(server, account):
    SpitfireReadClient().ensure_session()
    SpitfireReadClient().ensure_session()
    SpitfireWriteClient().whoami()
    assert len(server.logins) == 1


def test_concurrent_clients_do_not_each_log_in(server, account):
    errors = []

    def work():
        try:
            SpitfireReadClient().ensure_session()
        except Exception as exc:            # noqa: BLE001 — surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(server.logins) == 1


def test_a_lapsed_ticket_is_renewed_by_logging_in_again(server, account):
    client = SpitfireReadClient()
    client.ensure_session()
    server.expire_all()
    assert client.read("/api/document/abc").status_code == 200
    assert len(server.logins) == 2


def test_a_wrong_password_says_where_to_fix_it(server, monkeypatch):
    monkeypatch.setattr(settings, "SPITFIRE_UID", "svc-user")
    monkeypatch.setattr(settings, "SPITFIRE_PW", "wrong")
    with pytest.raises(RuntimeError, match="SPITFIRE_PW in .env"):
        SpitfireReadClient().ensure_session()


# --- a credential change is an .env edit and nothing else ---------------------------------------

def test_a_changed_password_in_env_is_used_on_the_next_login(server, monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("SPITFIRE_UID=svc-user\nSPITFIRE_PW=old\n", encoding="utf-8")
    monkeypatch.setattr(spitfire_auth, "ENV_FILE", env)
    server.password = "old"

    client = SpitfireReadClient()
    client.ensure_session()

    # The password is rotated in Spitfire and in .env; the running process is not restarted.
    server.password = "new"
    server.expire_all()
    env.write_text("SPITFIRE_UID=svc-user\nSPITFIRE_PW=new\n", encoding="utf-8")

    assert client.read("/api/document/abc").status_code == 200
    assert server.logins == [("svc-user", "old"), ("svc-user", "new")]


def test_env_file_values_win_over_what_was_loaded_at_startup(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("SPITFIRE_UID=from-file\nSPITFIRE_PW=file-pw\n", encoding="utf-8")
    monkeypatch.setattr(spitfire_auth, "ENV_FILE", env)
    monkeypatch.setattr(settings, "SPITFIRE_UID", "from-startup")
    monkeypatch.setattr(settings, "SPITFIRE_PW", "startup-pw")
    assert spitfire_auth.credentials().uid == "from-file"


def test_without_an_env_file_injected_settings_are_used(monkeypatch, tmp_path):
    """A deployed host with secrets injected as environment variables has no .env at all."""
    monkeypatch.setattr(spitfire_auth, "ENV_FILE", tmp_path / "absent.env")
    monkeypatch.setattr(settings, "SPITFIRE_UID", "injected")
    monkeypatch.setattr(settings, "SPITFIRE_PW", "injected-pw")
    assert spitfire_auth.credentials().uid == "injected"


# --- the write client ---------------------------------------------------------------------------

def test_the_write_client_renews_before_a_chain(server, account):
    writer = SpitfireWriteClient()
    writer.whoami()
    server.expire_all()
    assert writer.whoami() == "svc@example.invalid"
    assert len(server.logins) == 2
    # And it never issued the login itself: that stays off the write allowlist.
    assert all(r.path != "/api/Account" for r in writer.audit_log)


def test_a_401_mid_chain_stops_and_does_not_log_in_again(server, account):
    writer = SpitfireWriteClient()
    writer.whoami()
    server.expire_all()
    with pytest.raises(SpitfireSessionExpired, match="part-way through"):
        writer._request("GET", "/api/document/abc")
    assert len(server.logins) == 1


# --- secrets ------------------------------------------------------------------------------------

def test_the_password_never_appears_in_a_repr_or_the_audit_log(server, account):
    assert "right" not in repr(spitfire_auth.credentials())
    client = SpitfireReadClient()
    client.ensure_session()
    assert "right" not in repr(client.audit_log)


def test_the_login_is_never_recorded_to_a_cassette(monkeypatch):
    monkeypatch.setattr(settings, "SPITFIRE_CASSETTE_MODE", spitfire_cassette.RECORD)

    class Store:
        def record(self, *args, **kwargs):
            raise AssertionError("the login request was recorded")

    sent = requests.Response()
    sent.status_code = 200
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", lambda self, req, **kw: sent)
    adapter = spitfire_cassette.CassetteAdapter(store=Store())
    request = requests.Request("POST", f"{BASE}/api/Account", json={"UID": "u", "PW": "p"}).prepare()
    assert adapter.send(request) is sent


def test_the_login_refuses_while_replaying(monkeypatch):
    monkeypatch.setattr(settings, "SPITFIRE_CASSETTE_MODE", spitfire_cassette.REPLAY)
    adapter = spitfire_cassette.CassetteAdapter(store=object())
    request = requests.Request("POST", f"{BASE}/api/Account", json={"UID": "u", "PW": "p"}).prepare()
    with pytest.raises(spitfire_cassette.SpitfireOffline):
        adapter.send(request)
