"""`tools/spitfire_route_dispatch` must never be able to delete anything from Spitfire.

The tool exists because a receipt's approval route needed dispatching. An earlier version reached
"dispatch without notifying Premier staff" by *stripping* the staged approvers — three real people
were removed from a live receipt's route and their `RouteID`s destroyed, which no later call can
undo. The refusal below is what stops that shape of mistake recurring, so it is pinned here rather
than left to the docstring.

It is checked on the verb before the request is built, so it holds for every path — not just the
`/route` one that caused the incident.
"""

import pytest

from tools.spitfire_e2e_test import Run
from tools.spitfire_route_dispatch import NoDeleteHarness, RouteDeleteRefused

BASE = "https://training.example.test/Training"


def _harness() -> NoDeleteHarness:
    return NoDeleteHarness(BASE, "cookie-value", write=True, run=Run())


GUID = "1c8ffa70-72b8-4b6e-934a-7ea148899f9c"


@pytest.mark.parametrize("path", [
    f"/api/document/{GUID}/route",             # the call that caused the incident
    f"/api/document/{GUID}/attachments",
    f"/api/document/{GUID}/comments",
    f"/api/catalog/{GUID}",
    f"/api/document/{GUID}/session/changes",   # near-miss: must NOT match the carve-out
    f"/api/document/{GUID}/session/end",       # ditto
    "/api/document/abc/session",               # not a GUID, so not the release call
    f"/api/document/{GUID}/session/extra/bit",
])
def test_delete_is_refused_on_every_path(path):
    with pytest.raises(RouteDeleteRefused):
        _harness().call("DELETE", path)


@pytest.mark.parametrize("verb", ["delete", "Delete", "DELETE"])
def test_refusal_is_case_insensitive(verb):
    with pytest.raises(RouteDeleteRefused):
        _harness().call(verb, "/api/document/abc/route")


def test_refusal_happens_before_any_socket_is_opened(monkeypatch):
    """The guard must not depend on the request failing — it has to fire first.

    `Harness.call` is replaced with something that fails loudly if reached, so a refusal that
    happened only because the host does not resolve would not pass this test.
    """
    def explode(*args, **kwargs):  # pragma: no cover - reaching this is the failure
        raise AssertionError("a DELETE reached the transport")

    monkeypatch.setattr("tools.spitfire_e2e_test.Harness.call", explode)
    with pytest.raises(RouteDeleteRefused):
        _harness().call("DELETE", "/api/document/abc/route")


def test_session_release_is_the_one_permitted_delete(monkeypatch):
    """`DELETE /api/document/{guid}/session` must get through — nothing else can commit a change.

    `PUT /api/document/{id}/route` answers **501** on this build (measured 2026-09-11,
    `{"ThisStatus":501,"ThisReason":null}`), so the edit session is the only way to write a route
    response at all, and releasing the session is what commits it — `POST /session/end` returns 200
    and does not. The call removes no data; it drops an edit lock.
    """
    seen = []
    monkeypatch.setattr("tools.spitfire_e2e_test.Harness.call",
                        lambda self, method, path, **kw: seen.append((method, path)))

    _harness().call("DELETE", f"/api/document/{GUID}/session")
    _harness().call("DELETE", f"/api/document/{GUID}/session?sessionID={GUID}")

    assert len(seen) == 2, "both forms of the release call must reach the transport"
    assert all(m == "DELETE" for m, _ in seen)


def test_the_carve_out_is_shape_matched_not_substring():
    """A substring test for 'session' would let `/route?note=session` through, and `/session/changes`
    is one path segment away from the release call. Both must fail."""
    from tools.spitfire_route_dispatch import _is_session_release

    assert _is_session_release(f"/api/document/{GUID}/session")
    assert _is_session_release(f"/api/document/{GUID}/session?sessionID=x")
    assert not _is_session_release(f"/api/document/{GUID}/session/changes")
    assert not _is_session_release(f"/api/document/{GUID}/route?note=session")
    assert not _is_session_release(f"/api/document/{GUID}/route")
    assert not _is_session_release("/api/session")


def test_the_restore_payload_matches_the_staged_chain():
    """The three approvers Spitfire stages on a Receipt, so a stripped route can be rebuilt.

    Captured from CC-TEST receipt 1c0a70f5-…, which still carries the chain intact. Pinned because
    a wrong UserKey here would route a real document to the wrong person.
    """
    from tools.spitfire_route_dispatch import (PREMIER_ROUTEES, ROUTEE_SEQUENCE, ROUTEE_STAGE,
                                               ROUTEE_STATUS, ROUTEE_VIA)

    assert [r["name"] for r in PREMIER_ROUTEES] == ["Cassie Breaux", "Kendyl Shrogin", "Tina Tran"]
    assert {r["UserKey"] for r in PREMIER_ROUTEES} == {
        "4a533d19-42d1-4ea5-a389-e3a9f93a699b",
        "33404380-08a6-45a6-aa82-9c0087cf911a",
        "a2f136b9-b77c-4a23-bd3c-cadfd9f0bd7b",
    }
    # Sequence is mandatory — POST /route without it answers "You must specify Sequence".
    assert (ROUTEE_STAGE, ROUTEE_SEQUENCE, ROUTEE_VIA, ROUTEE_STATUS) == (1, 10, "W", "G")
