"""Recording Spitfire's responses, and replaying them with no network.

The thing worth proving is that the seam is invisible. `connectors/spitfire_cassette` mounts a
transport adapter *under* both clients, so everything they do — the allowlist, the read client's
retry-a-500-once rule, the audit log, the 401 handling — has to keep working around a response
that came off disk. These tests drive the real `SpitfireReadClient` and `SpitfireWriteClient`
rather than the store in isolation, because a store that round-trips perfectly while the clients
bypass it would pass and prove nothing.
"""

import json

import pytest
import requests

from config import settings
from connectors import spitfire_cassette
from connectors.spitfire import SpitfireReadClient
from connectors.spitfire_write import SpitfireWriteClient

PO_BODY = b'[{"SourceItemNumber":"EXT-925-AC","ItemQuantity":2.0,"DocItemNumber":"0001"}]'


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A cassette store in a temp directory, with the mode left at `off` until a test says."""
    monkeypatch.setattr(settings, "SPITFIRE_CASSETTE_DIR", tmp_path)
    return spitfire_cassette.CassetteStore(tmp_path)


@pytest.fixture
def replaying(monkeypatch):
    monkeypatch.setattr(settings, "SPITFIRE_CASSETTE_MODE", "replay")


def response(content: bytes = PO_BODY, status: int = 200) -> requests.Response:
    made = requests.Response()
    made.status_code = status
    made._content = content
    made.headers["Content-Type"] = "application/json; charset=utf-8"
    return made


# --- the key ------------------------------------------------------------------------------------

def test_query_argument_order_does_not_change_the_key(store):
    """`create_receipt` builds `?forProject=…&forBatch=…`; nothing guarantees a caller or a
    redirect preserves that order, and two keys for one call is a miss that looks like a bug."""
    assert (store.key("POST", "/api/document/x", "forProject=P&forBatch=212559")
            == store.key("POST", "/api/document/x", "forBatch=212559&forProject=P"))


def test_a_different_search_body_is_a_different_call(store):
    """`resolve_po` posts a QueryFilters body to one path per project. Keying on the path alone
    would make every PO in a project share one cassette — the first one recorded answering for
    all of them."""
    assert (store.key("POST", "/api/project/X/docs", body=b'{"po":"206725"}')
            != store.key("POST", "/api/project/X/docs", body=b'{"po":"208491"}'))


def test_a_blank_query_argument_is_not_dropped(store):
    """Spitfire's own URLs carry empty args; treating `?a=` as `?` would collide two real calls."""
    assert store.key("GET", "/api/x", "a=") != store.key("GET", "/api/x", "")


# --- round trip ---------------------------------------------------------------------------------

def test_a_recorded_response_replays_byte_identical(store):
    store.record("GET", "/api/document/abc/items", response())
    played = store.replay(store.lookup("GET", "/api/document/abc/items"))

    assert played.content == PO_BODY
    assert played.json()[0]["SourceItemNumber"] == "EXT-925-AC"
    assert played.status_code == 200


def test_a_recorded_failure_replays_as_that_failure(store):
    """A 500 must replay as a 500. sfPMS returns 500 for an invented GUID rather than 404, and the
    read client retries a 500 once — flattening it to 200 would exercise a path that cannot happen
    against the real server."""
    store.record("GET", "/api/document/nope", response(b'{"ThisStatus":500}', status=500))
    assert store.replay(store.lookup("GET", "/api/document/nope")).status_code == 500


def test_a_replayed_response_says_it_was_replayed(store):
    """Nothing in the codebase reads this header. It exists so "where did this figure come from"
    is answerable by a person reading a captured exchange after the fact."""
    store.record("GET", "/api/x", response())
    played = store.replay(store.lookup("GET", "/api/x"))
    assert played.headers["X-Spitfire-Cassette"] == "replay"
    assert played.headers["X-Spitfire-Cassette-Recorded-At"]


def test_a_corrupt_cassette_is_a_miss_not_a_crash(store):
    """The caller's own error names the call; a JSONDecodeError naming a sha1 does not."""
    store.record("GET", "/api/x", response())
    only = next(iter(store.root.glob("*.json")))
    only.write_text("{ this is not json", encoding="utf-8")
    assert store.lookup("GET", "/api/x") is None


# --- mounting -----------------------------------------------------------------------------------

def test_off_mounts_nothing(monkeypatch):
    """The default, and the whole basis of the claim that this feature changes no behaviour: with
    the mode off the session keeps the adapters `requests` gave it."""
    monkeypatch.setattr(settings, "SPITFIRE_CASSETTE_MODE", "off")
    client = SpitfireReadClient()
    assert not any(isinstance(a, spitfire_cassette.CassetteAdapter)
                   for a in client._session.adapters.values())


def test_an_unrecognised_mode_is_treated_as_off(monkeypatch):
    """A typo in `.env` must not silently put somebody into replay and have them read recorded
    figures as live ones."""
    monkeypatch.setattr(settings, "SPITFIRE_CASSETTE_MODE", "relpay")
    assert spitfire_cassette.mode() == "off"
    assert spitfire_cassette.writes_refused() is False


def test_replay_mounts_the_adapter(replaying):
    client = SpitfireReadClient()
    assert any(isinstance(a, spitfire_cassette.CassetteAdapter)
               for a in client._session.adapters.values())


# --- replay through the real client ---------------------------------------------------------

def test_a_read_replays_with_no_network(store, replaying):
    store.record("GET", "/api/document/abc", response(b'{"DocNo":"206725"}'))
    assert SpitfireReadClient()._get_json("/api/document/abc") == {"DocNo": "206725"}


def test_a_miss_names_the_call_it_could_not_answer(store, replaying):
    """The message is a to-do list for the next time somebody is on the office network."""
    with pytest.raises(spitfire_cassette.SpitfireCassetteMiss) as raised:
        SpitfireReadClient()._get_json("/api/document/never-recorded")

    message = str(raised.value)
    assert "GET /api/document/never-recorded" in message
    assert "spitfire_record_cassettes" in message


def test_the_site_prefix_is_not_part_of_the_key(store, replaying, monkeypatch):
    """`SPITFIRE_BASE_URL` ends in `/Training`. Keying on the full URL would make every cassette
    useless the day Premier moves us to production, which is the opposite of the point."""
    store.record("GET", "/api/document/abc", response())
    monkeypatch.setattr(settings, "SPITFIRE_BASE_URL",
                        "https://live.remingtonhotels.com/Production")
    assert SpitfireReadClient()._get_json("/api/document/abc")


# --- writes -------------------------------------------------------------------------------------

def test_a_write_refuses_while_replaying(replaying):
    """Never replayed, by design: a recorded `create_receipt` hands back one DocMasterKey for
    every delivery, so two receipts would collide on one key and the ledger would record a
    receipt number that looks real."""
    client = SpitfireWriteClient(session_cookie="not-a-real-ticket")
    with pytest.raises(spitfire_cassette.SpitfireOffline):
        client.add_line("abc", description="x", quantity=1.0)


def test_a_read_back_on_the_write_client_still_replays(store, replaying):
    """`_ALLOWED_READBACKS` replays while `_ALLOWED_WRITES` refuses — which is what keeps
    `verify_pod` working offline. Re-checking a stored file's hash needs no network."""
    store.record("GET", "/api/document/abc/attachments", response(b'[{"DocKey":"file-1"}]'))
    client = SpitfireWriteClient(session_cookie="not-a-real-ticket")
    assert client.read_attachments("abc") == [{"DocKey": "file-1"}]


@pytest.mark.parametrize("method,path,expected", [
    ("POST", "/api/document/abc/items", True),
    ("GET", "/api/document/abc/items", False),          # the read-back on the same path
    ("POST", "/api/document/abc/attachments", True),
    ("GET", "/api/document/abc/attachments", False),
    ("PATCH", "/api/document/abc/Title", True),
    ("POST", "/api/catalog/upload", True),
    ("GET", "/api/catalog/abc/versions", False),        # verify_upload
    ("POST", "/api/project/MRC024PB100003/docs", False),  # a search, despite the verb
    ("POST", "/api/document/00000000-0000-0000-0000-000000000000"
             "/0c9a537a-3c41-4d16-ab9f-130ef69ea6c8", True),   # create_receipt
])
def test_which_calls_count_as_writes(method, path, expected):
    """The split that decides what refuses offline. `GET /items` and `POST /items` are the same
    path and opposite answers, which is the case worth pinning."""
    assert spitfire_cassette.is_write(method, path) is expected


# --- recording ----------------------------------------------------------------------------------

def test_record_mode_returns_the_live_response_and_keeps_a_copy(store, monkeypatch):
    """Recording must not change what the caller gets — the sweep runs against the real ERP and a
    response altered on the way through would mirror altered PO lines."""
    monkeypatch.setattr(settings, "SPITFIRE_CASSETTE_MODE", "record")
    live = response(b'{"DocNo":"212559"}')
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send",
                        lambda self, request, **kw: live)

    client = SpitfireReadClient()
    assert client._get_json("/api/document/abc") == {"DocNo": "212559"}
    assert store.lookup("GET", "/api/document/abc").body == b'{"DocNo":"212559"}'


def test_recording_never_stores_a_write(store, monkeypatch):
    """A recorded write is the one thing that must never end up in the store — replaying it later
    is the failure this whole design avoids."""
    monkeypatch.setattr(settings, "SPITFIRE_CASSETTE_MODE", "record")
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send",
                        lambda self, request, **kw: response(b'"a-doc-key"'))

    SpitfireWriteClient(session_cookie="x").add_line("abc", description="d", quantity=1.0)
    assert store.count() == 0


def test_auto_prefers_the_recording_over_the_network(store, monkeypatch):
    """Office mode. A hit must not reach the network, or a sweep would be re-run on every page
    load and the point of recording is lost."""
    monkeypatch.setattr(settings, "SPITFIRE_CASSETTE_MODE", "auto")
    store.record("GET", "/api/document/abc", response(b'{"DocNo":"from-disk"}'))

    def explode(self, request, **kw):
        raise AssertionError("auto reached the network on a cassette hit")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", explode)
    assert SpitfireReadClient()._get_json("/api/document/abc") == {"DocNo": "from-disk"}


def test_the_stored_envelope_is_readable_by_a_person(store):
    """Bodies are hex so one store holds JSON and catalog bytes without a per-entry decision, but
    the metadata beside them has to stay legible — this is the file somebody opens when a figure
    on screen looks wrong."""
    store.record("GET", "/api/document/abc/items", response())
    envelope = json.loads(next(iter(store.root.glob("*.json"))).read_text(encoding="utf-8"))

    assert envelope["method"] == "GET"
    assert envelope["path"] == "/api/document/abc/items"
    assert envelope["status"] == 200
    assert envelope["recorded_at"]
    assert bytes.fromhex(envelope["body_hex"]) == PO_BODY
