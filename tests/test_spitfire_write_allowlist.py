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
    ("POST",  f"/api/document/{EMPTY_GUID}/{RECEIPT_TYPE}?forProject=P&forBatch=212614"),
    ("PATCH", "/api/document/abc/Title"),
    ("POST",  "/api/document/abc/items"),
    ("POST",  "/api/document/abc/attachments"),
    ("GET",   "/api/document/abc"),
    ("GET",   "/api/document/abc/items"),
    ("GET",   "/api/document/abc/attachments"),
    ("GET",   "/api/catalog/key/versions"),
    ("GET",   "/api/session/who"),
    ("POST",  "/api/project/MRC024PB100003/docs"),
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


def test_a_missing_cookie_is_named_rather_than_guessed_at(monkeypatch):
    """Auth is a hand-copied browser ticket that lapses on idle. `POST /api/Account` would let this
    class log in with a password instead — and must not: there is no service account, the
    credential in .env belongs to a named human and carries AdminLevel 31, and silently
    re-authenticating a *write* client turns an expiry into an unattended write."""
    monkeypatch.setattr(spitfire_write.settings, "SPITFIRE_SESSION_COOKIE", None)
    with pytest.raises(spitfire_write.SpitfireSessionExpired):
        spitfire_write.SpitfireWriteClient(base_url="https://example.invalid")


def test_the_login_endpoint_is_not_reachable_from_the_write_connector():
    """Belt and braces on the above. `POST /api/Account` is a real, working endpoint that the read
    connector is allowed to call; here it is off the list, so even a caller that wanted to
    re-authenticate mid-post cannot."""
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
