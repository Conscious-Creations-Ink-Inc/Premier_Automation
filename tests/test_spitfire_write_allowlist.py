"""What the write connector may and may not issue against Premier's ERP.

`tests/test_operations_readonly.py` does not cover this and should not be made to: it guards
Premier's *mailbox* against Graph writes, and it deliberately exempts `@router.post(...)` because
an inbound route is not an outbound call. Spitfire's guarantee is a different one, enforced in a
different place, and this is where it is held.

The refusals below are not stylistic. Each names something that reaches real people or real
approval state on a live ERP, and each was found by probing the server rather than by reading a
specification.
"""

import pytest

from connectors import spitfire_write
from connectors.spitfire_write import EMPTY_GUID, SpitfireWriteViolation, is_allowed

RECEIPT_TYPE = "0c9a537a-3c41-4d16-ab9f-130ef69ea6c8"


@pytest.mark.parametrize("method,path", [
    ("POST",  "/api/catalog/upload?xm=catalog"),
    ("POST",  f"/api/document/{EMPTY_GUID}/{RECEIPT_TYPE}?forProject=P&forBatch=912614"),
    ("PATCH", "/api/document/abc/Title"),
    ("POST",  "/api/document/abc/items"),
    ("POST",  "/api/document/abc/attachments"),
    ("GET",   "/api/document/abc"),
    ("GET",   "/api/document/abc/items"),
    ("GET",   "/api/document/abc/attachments"),
    ("GET",   "/api/catalog/key/versions"),
    ("GET",   "/api/session/who"),
    ("POST",  "/api/project/PRJ001PB100003/docs"),
])
def test_the_posting_chain_is_permitted(method, path):
    assert is_allowed(method, path), f"{method} {path} is part of the receipt chain"


@pytest.mark.parametrize("method,path,why", [
    ("POST",   "/api/document/abc/route/apply",
     "dispatches the approval chain and emails Cassie Breaux, Kendyl Shrogin and Tina Tran"),
    ("POST",   "/api/document/abc/route/perform", "the same, by another name"),
    ("PATCH",  "/api/document/abc/Status",
     "marks the receipt POD Confirmed with no approval, bypassing the FAA sign-off"),
    ("GET",    "/api/session/logout",
     "a GET that destroys the hand-captured ticket the whole run depends on"),
    ("DELETE", "/api/document/abc", "none of the 53 DELETEs has ever been issued"),
    ("DELETE", "/api/document/abc/attachments", "as above"),
    ("POST",   "/api/contact", "an empty body creates a blank contact; there is no validation"),
    ("POST",   "/api/system/supportcase", "files a case with Spitfire Management, an outside party"),
    ("PUT",    "/api/document/abc/items",
     "501 Not Implemented. The v23 schema documents it as 'Updates the item' and names the "
     "alternative in its own description: PatchDocData, i.e. PATCH /session/changes"),
    ("PUT",    "/api/document/abc/attachments", "as above"),
    ("POST",   "/api/document/abc/date", "leaks raw SQL — FK_xsfDocDates_xsfDocDateType"),
])
def test_the_dangerous_calls_are_refused(method, path, why):
    assert not is_allowed(method, path), f"{method} {path} must be refused: {why}"


@pytest.mark.parametrize("path", [
    "/api/document/abc/next",
    f"/api/document/{EMPTY_GUID}/next",
    "/api/document/from/abc",
])
def test_the_create_template_cannot_be_widened_into_next_or_from(path):
    """`POST /api/document/{parent}/{typeKey}` is two wildcards, and as a plain template it also
    matches `/next` — which creates a document with an all-zero DocTypeKey — and `/from/{id}`,
    which silently copies one. Both were created by accident during the 13 August write probe from
    empty-body calls that were expected to be rejected and were not. Creation is matched by shape
    instead: null-GUID parent, real GUID type."""
    assert not is_allowed("POST", path)


def test_the_client_refuses_before_opening_a_socket():
    """The check is in `_request`, ahead of the network, so a mistake is a stack trace rather than
    something Premier has to notice afterwards."""
    client = spitfire_write.SpitfireWriteClient(base_url="https://example.invalid",
                                                session_cookie="not-a-real-ticket")
    with pytest.raises(SpitfireWriteViolation):
        client._request("POST", "/api/document/abc/route/apply")


def test_no_credentials_at_all_is_named_rather_than_guessed_at(monkeypatch):
    """With neither an account nor a cookie configured the client refuses at construction, before
    anything could be posted. (With an account it rides the shared login from
    `connectors/spitfire_auth.py`; see tests/test_spitfire_auth.py.)"""
    monkeypatch.setattr(spitfire_write.settings, "SPITFIRE_SESSION_COOKIE", None)
    monkeypatch.setattr(spitfire_write.settings, "SPITFIRE_UID", None)
    monkeypatch.setattr(spitfire_write.settings, "SPITFIRE_PW", None)
    with pytest.raises(spitfire_write.SpitfireSessionExpired):
        spitfire_write.SpitfireWriteClient(base_url="https://example.invalid")


def test_the_login_endpoint_is_not_reachable_from_the_write_connector():
    """`POST /api/Account` is a real, working endpoint that the read connector is allowed to call;
    here it is off the list, so the write client can only obtain a ticket from the shared login
    before a chain, and a caller that wanted to re-authenticate mid-post cannot."""
    assert not is_allowed("POST", "/api/Account")


# --- the document-edit session ------------------------------------------------------------------
#
# The only route that can set a quantity on a receipt line. `POST /items` appends a row whose
# every linking field is discarded on insert, and `PUT /items` answers 501.


DOC = "11111111-2222-3333-4444-555555555555"


@pytest.mark.parametrize("method,path", [
    ("GET",    "/api/document/abc/session"),
    ("GET",    "/api/document/abc/session?freshenData=true"),
    ("PATCH",  "/api/document/abc/session/changes"),
    ("DELETE", f"/api/document/{DOC}/session?sessionID=xyz"),
])
def test_the_edit_session_is_permitted(method, path):
    assert is_allowed(method, path)


def test_the_delete_exception_requires_a_real_document_key():
    """Deliberately stricter than the rest of the allowlist, which accepts any segment in a
    wildcard. This is the one DELETE the connector may issue, so it is matched by shape and not by
    position: a real GUID in the document slot, and `session` as the last word."""
    assert not is_allowed("DELETE", "/api/document/abc/session")
    assert is_allowed("DELETE", f"/api/document/{DOC}/session")


@pytest.mark.parametrize("path", [
    "/api/document/11111111-2222-3333-4444-555555555555",
    "/api/document/11111111-2222-3333-4444-555555555555/items",
    "/api/document/11111111-2222-3333-4444-555555555555/attachments",
    "/api/document/11111111-2222-3333-4444-555555555555/items/all",
    "/api/document/11111111-2222-3333-4444-555555555555/session/changes",
    "/api/session/document/11111111-2222-3333-4444-555555555555",
])
def test_releasing_a_session_is_the_only_permitted_delete(path):
    """`DELETE /api/document/{id}/session` commits an edit and removes nothing. It is matched by
    shape — four segments, a real GUID, ending in `session` — so it cannot be widened into any of
    the DELETEs below, each of which destroys something on a live ERP."""
    assert not is_allowed("DELETE", path)


def test_setting_a_quantity_drops_the_inherited_session_before_taking_its_own(monkeypatch):
    """The subtlest failure in this chain, and one nothing in a response reveals.

    `GET /session` does not create a session, it returns the one in force — and creating a receipt
    and titling it leaves one behind. A change staged into that inherited session is silently
    discarded. Measured on training 2026-08-22: two receipts built identically, one patched through
    the inherited session and one through a session taken after releasing it. Every call in both
    answered 200. The first read back 0.0, the second 4.0.
    """
    calls = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return "session-guid"

    client = spitfire_write.SpitfireWriteClient(base_url="https://example.invalid",
                                                session_cookie="ticket")
    monkeypatch.setattr(client, "_request",
                        lambda method, path, **kw: (calls.append((method, path)), Response())[1])

    client.set_line_quantity("abc", {"task-1": 4.0})

    verbs = [m for m, _ in calls]
    assert verbs[0] == "GET" and "session" in calls[0][1], "must look for an inherited session"
    assert verbs[1] == "DELETE", "must release it before taking one of its own"
    assert verbs.count("DELETE") == 2, "one release on the way in, one commit on the way out"
    assert verbs[-1] == "DELETE", "the commit is the last thing that happens"
    assert "PATCH" in verbs and calls[verbs.index("PATCH")][1].endswith("/session/changes")


def test_a_quantity_is_sent_as_a_plain_decimal_string(monkeypatch):
    """`DocFieldChange.Data` is a string. A whole number carries no trailing `.0`, which is how
    sfPMS's own client sends it."""
    sent = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return "session-guid"

    client = spitfire_write.SpitfireWriteClient(base_url="https://example.invalid",
                                                session_cookie="ticket")

    def capture(method, path, **kw):
        if method == "PATCH":
            sent.extend(kw.get("json") or [])
        return Response()

    monkeypatch.setattr(client, "_request", capture)
    client.set_line_quantity("abc", {"task-1": 4.0, "task-2": 2.5})

    assert [c["Data"] for c in sent] == ["4", "2.5"]
    assert {c["DataMember"] for c in sent} == {"DocItemTask"}
    assert {c["DataField"] for c in sent} == {"Quantity"}
    assert [c["InstanceKey"] for c in sent] == ["task-1", "task-2"]


def test_nothing_is_sent_when_there_are_no_quantities(monkeypatch):
    """No session is opened for an empty change set — taking an edit lock to do nothing would
    leave one to be cleaned up for no reason."""
    calls = []
    client = spitfire_write.SpitfireWriteClient(base_url="https://example.invalid",
                                                session_cookie="ticket")
    monkeypatch.setattr(client, "_request", lambda *a, **k: calls.append(a))

    client.set_line_quantity("abc", {})
    assert calls == []


# --- signing our own route steps -----------------------------------------------------------------
#
# Measured on receipt 0002, 2026-09-17: sequence 1 was `Reached` and offered `A,H,P`, while
# sequence 5 had no `Reached` timestamp and offered `C,D,P,G` — no `A`. Spitfire refuses a response
# on a stop the route has not arrived at, and signing sequence 1 is what makes sequence 5 arrive.
# So the client signs one stop, re-reads, and signs whatever became reachable. A single pass looks
# like it worked and leaves sequence 5 Pending, which is the bug these hold.

OUR_KEY = "bf4a2531-074a-43e9-9deb-9a589127ded9"
THEIR_KEY = "4a533d19-42d1-4ea5-a389-e3a9f93a699b"

_CAN_RESPOND = [{"CommandName": "CanEditRouteResponseCode", "Enabled": True}]


def _step(sequence, *, user=OUR_KEY, status="P", reached="2026-09-16T11:24:29.647",
          choices="A,H,P", commands=_CAN_RESPOND):
    return {"Sequence": sequence, "UserKey": user, "Status": status, "Reached": reached,
            "Choices": choices, "MenuCommands": commands,
            "RouteID": f"route-{sequence}"}


class _RouteServer:
    """A Spitfire whose route advances only when a stop is signed, as the real one does."""

    def __init__(self, steps):
        self.steps = {s["Sequence"]: dict(s) for s in steps}
        self.reads = 0
        self.signed = []

    def read_route(self, doc_key):
        self.reads += 1
        return [dict(s) for s in self.steps.values()]

    def sign(self, route_id):
        sequence = int(str(route_id).rsplit("-", 1)[1])
        self.steps[sequence]["Status"] = "A"
        self.signed.append(sequence)
        # Signing ours is what lets the route reach the next stop — the behaviour the 2026-09-11
        # dispatch showed when seq 10 gained a `Reached` timestamp after seq 5 was signed.
        nxt = min((s for s in self.steps if s > sequence), default=None)
        if nxt is not None:
            self.steps[nxt]["Reached"] = "2026-09-17T09:00:00.000"
            self.steps[nxt]["Choices"] = "A,H,P,R,B"


def _client(server):
    """A real `SpitfireWriteClient` with only its HTTP edges replaced."""
    client = spitfire_write.SpitfireWriteClient.__new__(spitfire_write.SpitfireWriteClient)
    client.session_user_key = lambda: OUR_KEY
    client.read_route = lambda doc_key: server.read_route(doc_key)
    client._release_session = lambda doc_key, session_id="": None
    client._json_or_raise = lambda response, what: "s" * 36
    client._request = lambda *a, **k: None

    def _commit(doc_key, session_id, changes, *, note, failure):
        for change in changes:
            assert change["DataMember"] == "DocRoute", change
            assert change["DataField"] == "Status", "only Status ever moves"
            assert change["Data"] == "A"
            server.sign(change["InstanceKey"])

    client._commit_changes = _commit
    return client


def test_signing_seq_1_then_seq_5_when_5_was_not_reachable_at_first():
    """Receipt 0002's exact shape: seq 1 actionable, seq 5 not reached and offering no `A`."""
    server = _RouteServer([
        _step(1),
        _step(5, reached="", choices="C,D,P,G"),
        _step(10, user=THEIR_KEY, reached="", choices="C,D,P,G"),
    ])

    signed = _client(server).sign_off_route_steps("doc-1")

    assert server.signed == [1, 5], "one pass would have signed 1 and left 5 Pending"
    assert signed == ["route step 1 signed off", "route step 5 signed off"]
    assert server.reads > 1, "the route has to be read again for seq 5 to become reachable"


def test_a_route_with_only_one_stop_of_ours_signs_once_and_stops():
    server = _RouteServer([_step(1), _step(10, user=THEIR_KEY, reached="", choices="C,D,P,G")])

    signed = _client(server).sign_off_route_steps("doc-1")

    assert server.signed == [1]
    assert signed == ["route step 1 signed off"]


def test_a_stop_of_ours_the_route_has_not_reached_is_never_signed():
    server = _RouteServer([_step(1, reached="", choices="C,D,P,G")])

    assert _client(server).sign_off_route_steps("doc-1") == [
        "no route step of ours is reached and unsigned"]
    assert server.signed == []


def test_the_sentinel_date_does_not_count_as_reached():
    """Spitfire writes `0001-01-01T00:00:00` for never, and as a non-empty string it reads truthy.
    Treating it as a real timestamp inverts the whole gate."""
    server = _RouteServer([_step(1, reached="0001-01-01T00:00:00")])

    assert _client(server).sign_off_route_steps("doc-1") == [
        "no route step of ours is reached and unsigned"]
    assert server.signed == []


def test_somebody_elses_stop_is_never_signed_however_reachable():
    server = _RouteServer([_step(10, user=THEIR_KEY)])

    assert _client(server).sign_off_route_steps("doc-1") == [
        "no route step of ours is reached and unsigned"]
    assert server.signed == []


def test_a_stop_spitfire_does_not_offer_is_left_alone():
    """`CanEditRouteResponseCode` absent, or `A` missing from the choices. If the server does not
    offer the capability we do not invent it."""
    for step in (_step(1, commands=[]), _step(1, choices="C,D,P,G")):
        server = _RouteServer([step])
        assert _client(server).sign_off_route_steps("doc-1") == [
            "no route step of ours is reached and unsigned"]
        assert server.signed == []


def test_an_already_signed_route_is_not_signed_again():
    """Re-posting a receipt must not re-write an acted row."""
    server = _RouteServer([_step(1, status="A"), _step(5, status="A")])

    assert _client(server).sign_off_route_steps("doc-1") == [
        "no route step of ours is reached and unsigned"]
    assert server.signed == []


def test_the_pass_limit_holds_if_the_server_never_records_the_signature():
    """The loop's exit depends on the server moving `Status`. If it never does, the bound is what
    stops this signing the same row for ever."""
    server = _RouteServer([_step(1)])
    server.sign = lambda route_id: server.signed.append(1)      # accepts, records nothing

    client = _client(server)
    signed = client.sign_off_route_steps("doc-1")

    assert len(server.signed) == spitfire_write.SpitfireWriteClient.MAX_SIGN_PASSES
    assert len(signed) == spitfire_write.SpitfireWriteClient.MAX_SIGN_PASSES
