"""The `/ui` route that fills a record's gaps from its proof of delivery.

Every test patches `record_completion.complete`. The route reads through
`read_views.records_ready` against Premier's real pipeline store, so an unpatched call would edit
it — the same reasoning `test_ui_post_route.py` gives for never reaching the real post chain. The
completion logic itself is covered in `test_record_completion.py` against an in-memory store.

Kept in its own file for that reason: the standing guard below patches the shared module object,
which would also disarm the unit tests if they sat beside it.
"""

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.ui import routes as ui_routes
from pipeline import record_completion


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def a_record():
    """A record id the Records page is currently offering. Skips rather than fabricating one: the
    route reads through `read_views.records_ready`, and a hand-inserted row would not exercise the
    same path."""
    from pipeline import read_views, state_db
    conn = state_db.get_connection()
    try:
        rows = read_views.records_ready(conn)
        if not rows:
            pytest.skip("no records in the pipeline store to complete")
        return rows[0]["id"]
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def never_really_complete(monkeypatch):
    """A standing guard for this file. A test that forgets to patch must fail rather than write to
    Premier's store."""
    def refuse(*args, **kwargs):
        raise AssertionError("a test reached the real completion write")
    monkeypatch.setattr(ui_routes.record_completion, "complete", refuse)


def test_an_unknown_record_is_reported(client):
    assert "No such record" in client.post("/ui/records/99999999/complete").text


def test_the_complete_route_is_not_reachable_by_GET(client, a_record):
    """A GET would let a prefetch or a crawler rewrite records nobody asked it to touch — the same
    reasoning Verify and Post are POSTs for."""
    assert client.get(f"/ui/records/{a_record}/complete").status_code == 405


def test_what_was_filled_is_listed_back(client, a_record, monkeypatch):
    """A reviewer is accountable for the receipt this becomes, so values written on their behalf
    are shown to them rather than applied silently."""
    monkeypatch.setattr(ui_routes.record_completion, "complete", lambda conn, row, line=None: (
        record_completion.Completion(
            ok=True, is_complete=True, message="this record is now complete and can be posted",
            applied=["POD date = 2025-09-10 (from the proof of delivery)"])))

    body = client.post(f"/ui/records/{a_record}/complete?line=1").text

    assert "Completed from the POD" in body
    assert "2025-09-10" in body and "from the proof of delivery" in body


def test_a_refusal_is_shown_as_a_warning_and_lists_nothing_applied(client, a_record, monkeypatch):
    monkeypatch.setattr(ui_routes.record_completion, "complete", lambda conn, row, line=None: (
        record_completion.Completion(ok=False, message="purchase order 910634 has no line 99")))

    body = client.post(f"/ui/records/{a_record}/complete?line=99").text

    assert "Not completed" in body
    assert "no line 99" in body
    assert "<ul>" not in body, "nothing was applied, so nothing should be listed"


def test_the_reviewers_chosen_line_reaches_the_completion(client, a_record, monkeypatch):
    """`line` is the one value the route takes from the request, and the only field a delivery
    note cannot supply. If it were dropped the record would silently stay incomplete."""
    seen = {}
    monkeypatch.setattr(ui_routes.record_completion, "complete", lambda conn, row, line=None: (
        seen.update(line=line) or record_completion.Completion(ok=False, message="noted")))

    client.post(f"/ui/records/{a_record}/complete?line=3")

    assert seen["line"] == 3
