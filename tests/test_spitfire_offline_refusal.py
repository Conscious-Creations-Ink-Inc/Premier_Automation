"""What the application does when Spitfire cannot be written to.

Spitfire answers only from Premier's office IP. Off it, `SPITFIRE_CASSETTE_MODE=replay` makes
reads work from recorded responses — and every write has to refuse. The two things worth proving
are that the refusal reaches a person as a sentence rather than a stack trace, and that **it
leaves nothing behind in the ledger**.

That second one is the whole reason the gate sits in `_write_guarded` rather than deeper down.
`spitfire_post.post_pod` constructs its write client *after* `post_ledger.claim`, so a refusal
raised at the socket would land inside the try block with a claim already written — leaving a
`CLAIMED` row, which the ledger reads as "a receipt may exist on training somewhere" and a human
then has to go and disprove. Nothing was sent; nothing should be recorded as if it might have been.
"""

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.ui import routes as ui_routes
from config import settings
from connectors import spitfire_cassette
from connectors.spitfire_write import SpitfireWriteClient
from pipeline import post_ledger, spitfire_post, state_db


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def replaying(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SPITFIRE_CASSETTE_MODE", "replay")
    monkeypatch.setattr(settings, "SPITFIRE_CASSETTE_DIR", tmp_path)


@pytest.fixture(autouse=True)
def never_really_post(monkeypatch):
    """The standing guard `test_ui_post_route` keeps, for the same reason: if the gate fails open,
    this raises instead of creating a receipt on Premier's training instance."""
    def refuse(*args, **kwargs):
        raise AssertionError("a write reached the post chain while offline")
    monkeypatch.setattr(spitfire_post, "post_pod", refuse)
    monkeypatch.setattr(spitfire_post, "post_report", refuse)


@pytest.fixture
def a_record():
    conn = state_db.get_connection()
    try:
        from pipeline import read_views
        rows = read_views.records_ready(conn)
        if not rows:
            pytest.skip("no records in the pipeline store to post")
        return rows[0]["id"]
    finally:
        conn.close()


# --- the route ----------------------------------------------------------------------------------

def test_posting_offline_is_refused_in_words(client, a_record, replaying):
    response = client.post(f"/ui/records/{a_record}/post-pod")

    assert response.status_code == 200
    assert "Offline" in response.text
    assert "office network" in response.text


def test_the_report_step_is_refused_the_same_way(client, a_record, replaying):
    assert "Offline" in client.post(f"/ui/records/{a_record}/post-report").text


def test_a_refused_post_leaves_no_ledger_row(client, a_record, replaying):
    """The point of gating in `_write_guarded`. A `CLAIMED` row left by a refusal is worse than
    no row: it blocks the record, and it says a receipt may exist that never did."""
    conn = state_db.get_connection()
    try:
        before = len(post_ledger.existing_for_record(conn, a_record))
        client.post(f"/ui/records/{a_record}/post-pod")
        assert len(post_ledger.existing_for_record(conn, a_record)) == before
    finally:
        conn.close()


def test_online_the_gate_is_not_in_the_way(client, a_record, monkeypatch):
    """With the mode off — the default — the branch is dead and the route behaves as it always
    has. This is the test that says the feature costs nothing when unused."""
    monkeypatch.setattr(settings, "SPITFIRE_CASSETTE_MODE", "off")
    monkeypatch.setattr(spitfire_post, "post_pod",
                        lambda conn, row, **kw: spitfire_post.PostResult(
                            ok=True, state=post_ledger.POD_POSTED, message="posted", record_id=1))

    response = client.post(f"/ui/records/{a_record}/post-pod")
    assert "Offline" not in response.text
    assert "posted" in response.text


# --- the Post cell ------------------------------------------------------------------------------

class _Attempt:
    def __init__(self, state, receipt_doc_no="0007", detail="", attempts=1):
        self.state = state
        self.receipt_doc_no = receipt_doc_no
        self.detail = detail
        self.attempts = attempts


def _complete_row(**overrides):
    row = {"id": 5, "po_number": "212614", "spec_code": "LT-03b",
           "item_description": "LT-03B Frosted", "vendor_name": "Archipelago",
           "quantity_received": 19.0, "unit_of_measure": "EA", "pod_stated_date": "2026-01-20",
           "received_by": "J Smith", "po_line_number": 1, "source_email_id": "mail-1"}
    row.update(overrides)
    return row


def test_no_post_button_is_drawn_offline(replaying):
    """A third certain-and-cheap refusal, alongside "no POD" and "N gaps": a control whose only
    possible outcome is a refusal teaches people to ignore refusals."""
    cell = str(ui_routes._post_cell(None, _complete_row(), has_pod=True))
    assert "post-pod" not in cell
    assert "offline" in cell


def test_a_half_finished_receipt_still_says_so_offline(replaying):
    """The button goes; the badge stays. `POD_POSTED` means a real receipt in Premier's ERP is
    carrying a proof of delivery and no report, and that fact does not depend on where the
    reviewer is sitting."""
    cell = str(ui_routes._post_cell(_Attempt(post_ledger.POD_POSTED), _complete_row(),
                                    has_pod=True))
    assert "post-report" not in cell
    assert "report pending" in cell
    assert "offline" in cell


def test_what_already_posted_still_reads_as_posted_offline(replaying):
    """A receipt created last week is a fact, not a control."""
    cell = str(ui_routes._post_cell(_Attempt(post_ledger.POSTED), _complete_row(), has_pod=True))
    assert "Posted 0007" in cell


def test_verifying_a_pod_survives_offline(replaying):
    """`verify_pod` re-reads the catalog and re-compares a hash — both allowlisted read-backs, so
    both replay. Refusing to *check* what was written would be perverse."""
    cell = str(ui_routes._post_cell(_Attempt(post_ledger.POSTED), _complete_row(), has_pod=True))
    assert "verify-pod" in cell


# --- the backstop underneath --------------------------------------------------------------------

def test_the_client_itself_refuses_even_if_the_gate_is_bypassed(replaying):
    """Whatever the UI does, the transport will not carry a write. `tools/` scripts and any future
    unattended path get the same answer, and it names the reason."""
    client = SpitfireWriteClient(session_cookie="not-a-real-ticket")

    with pytest.raises(spitfire_cassette.SpitfireOffline) as raised:
        client.create_receipt("MRC024PB100003", "212559")
    assert "office network" in str(raised.value)
