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
    ("PUT",    "/api/document/abc/items", "an undiscoverable 500; the working call is POST"),
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
