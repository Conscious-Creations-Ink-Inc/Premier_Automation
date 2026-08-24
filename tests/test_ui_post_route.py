"""The one route in `/ui` that writes to Premier's ERP.

Every test here patches `spitfire_post.post_record`. That is not laziness about coverage — the
chain itself is covered in `test_spitfire_post.py` against a fake client — it is the point: this
route must be provably incapable of reaching Spitfire when the kill switch is engaged or the
runner lock is held, and a test that could reach Spitfire to prove it would be self-defeating.
"""

import threading

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.ui import routes as ui_routes
from operations import killswitch, runner
from pipeline import post_ledger, spitfire_post


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def a_record():
    """A record id the Records page is currently offering. Skips rather than fabricating one: the
    route reads through `read_views.records_ready`, and a hand-inserted row would not exercise the
    same path."""
    from pipeline import read_views, state_db
    # Its own connection rather than `deps.get_pipeline_conn`, which is a generator dependency:
    # taking one value from it leaves the generator to be collected, and its `finally` closes the
    # connection out from under the caller.
    conn = state_db.get_connection()
    try:
        rows = read_views.records_ready(conn)
        if not rows:
            pytest.skip("no records in the pipeline store to post")
        return rows[0]["id"]
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def never_really_post(monkeypatch):
    """A standing guard for this file. If a test forgets to patch, this raises rather than
    creating a receipt on Premier's training instance."""
    def refuse(*args, **kwargs):
        raise AssertionError("a test reached the real post chain")
    monkeypatch.setattr(spitfire_post, "post_pod", refuse)


def test_an_unknown_record_is_reported_not_posted(client):
    response = client.post("/ui/records/99999999/post-pod")
    assert response.status_code == 200
    assert "No such record" in response.text


def test_the_kill_switch_stops_the_post(client, a_record, monkeypatch):
    """The switch exists to stop everything and stay stopped across a restart. A write path that
    ignored it would not be stopped — and this is the only write path there is."""
    monkeypatch.setattr(killswitch, "is_stopped", lambda: True)
    response = client.post(f"/ui/records/{a_record}/post-pod")
    assert "kill switch is engaged" in response.text
    # `never_really_post` would have raised if the chain had been reached.


def test_a_run_in_progress_turns_the_post_away_rather_than_queueing_it(client, a_record,
                                                                      monkeypatch):
    """`runner._LOCK`'s established posture: "a second caller is turned away rather than queued —
    pressing a button twice should be a no-op, not two runs". Two receipts is the failure that
    posture prevents here."""
    monkeypatch.setattr(killswitch, "is_stopped", lambda: False)
    acquired = runner._LOCK.acquire(blocking=False)
    assert acquired, "the lock was already held; another test leaked it"
    try:
        response = client.post(f"/ui/records/{a_record}/post-pod")
    finally:
        runner._LOCK.release()
    assert "mid-run" in response.text


def test_the_lock_is_released_even_when_the_post_raises(client, a_record, monkeypatch):
    """A leaked lock would silently disable both the scheduler and every later post, and the
    symptom — "the automation is mid-run" forever — points nowhere near the cause."""
    monkeypatch.setattr(killswitch, "is_stopped", lambda: False)

    def explode(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(ui_routes.spitfire_post, "post_pod", explode)

    with pytest.raises(RuntimeError):
        client.post(f"/ui/records/{a_record}/post-pod")
    assert runner._LOCK.acquire(blocking=False), "the runner lock was not released"
    runner._LOCK.release()


def test_a_successful_post_says_it_is_not_routed(client, a_record, monkeypatch):
    """The receipt is left In Process on purpose, so the purchase order will go on showing nothing
    received until a human approves it. A green tick that implied otherwise would be a lie, and
    this is the sentence that stops it being one."""
    monkeypatch.setattr(killswitch, "is_stopped", lambda: False)
    monkeypatch.setattr(ui_routes.spitfire_post, "post_pod", lambda conn, row: (
        spitfire_post.PostResult(ok=True, state="posted", message="posted to Spitfire as receipt 0007",
                                 po_number="212614", receipt_doc_no="0007",
                                 steps=["receipt created", "POD attached"])))
    response = client.post(f"/ui/records/{a_record}/post-pod")
    assert "Posted" in response.text
    assert "In Process" in response.text and "not" in response.text
    assert "POD attached" in response.text, "the steps are what a reviewer actually reads"


def test_a_flagged_record_shows_the_reason_verbatim(client, a_record, monkeypatch):
    monkeypatch.setattr(killswitch, "is_stopped", lambda: False)
    monkeypatch.setattr(ui_routes.spitfire_post, "post_pod", lambda conn, row: (
        spitfire_post.PostResult(ok=False, state="flagged",
                                 message="the email says 18 EA and the purchase order says 19 EA")))
    response = client.post(f"/ui/records/{a_record}/post-pod")
    assert "Not posted" in response.text
    assert "18 EA" in response.text and "19 EA" in response.text


def test_an_expired_cookie_is_named_as_such(client, a_record, monkeypatch):
    """Auth is a hand-copied browser ticket that lapses on idle. A generic failure sends people
    hunting for permission problems that do not exist."""
    monkeypatch.setattr(killswitch, "is_stopped", lambda: False)
    monkeypatch.setattr(ui_routes.spitfire_post, "post_pod", lambda conn, row: (
        spitfire_post.PostResult(ok=False, state="session_expired",
                                 message="the sfPMSAuth cookie has expired or was rejected.")))
    response = client.post(f"/ui/records/{a_record}/post-pod")
    assert "Session expired" in response.text


def test_the_post_route_is_not_reachable_by_GET(client, a_record):
    """A GET would let a prefetch, a crawler or a mistyped link create receipts on Premier's ERP.
    The same reasoning the Verify endpoints are POST for, with far higher stakes."""
    assert client.get(f"/ui/records/{a_record}/post-pod").status_code == 405


# --- what the Post cell offers, at each stage ------------------------------------------------
#
# A control whose only possible outcome is a refusal teaches people to ignore refusals, so the two
# refusals knowable without calling Spitfire — no POD to upload, required fields still missing —
# are not drawn as buttons at all. Everything needing a live purchase-order read still refuses on
# click, because pre-judging it would cost a network call per row.


class _Attempt:
    """Enough of a `post_ledger.PostAttempt` for the cell to render."""

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


def test_no_post_button_without_a_proof_of_delivery():
    cell = str(ui_routes._post_cell(None, _complete_row(), has_pod=False))
    assert "post-pod" not in cell
    assert "no POD" in cell


def test_no_post_button_while_required_fields_are_missing():
    cell = str(ui_routes._post_cell(None, _complete_row(received_by=None, pod_stated_date=None),
                                    has_pod=True))
    assert "post-pod" not in cell
    assert "gaps" in cell


def test_a_ready_record_offers_the_pod_first():
    cell = str(ui_routes._post_cell(None, _complete_row(), has_pod=True))
    assert "post-pod/confirm" in cell and "Post POD" in cell
    assert "post-report" not in cell, "the report is a later decision, not an alternative"


def test_once_the_pod_is_posted_the_report_is_offered_and_the_gap_is_named():
    cell = str(ui_routes._post_cell(_Attempt(post_ledger.POD_POSTED), _complete_row(),
                                    has_pod=True))
    assert "post-report/confirm" in cell and "Post report" in cell
    assert "post-pod" not in cell, "the POD is done; offering it again would build a second receipt"
    assert "report pending" in cell, "a half-finished write to an ERP must say so on the row"
    assert "verify-pod" in cell


def test_once_both_are_posted_only_verification_remains():
    cell = str(ui_routes._post_cell(_Attempt(post_ledger.POSTED), _complete_row(), has_pod=True))
    assert "Posted 0007" in cell
    assert "post-pod" not in cell and "post-report" not in cell
    assert "verify-pod" in cell, "proving what landed stays available for ever"


def test_a_flagged_record_keeps_its_button_and_shows_the_reason():
    """Records are refused for things that get fixed. The reason moves onto the cell so a reviewer
    reads it without spending a live purchase-order read to rediscover it."""
    cell = str(ui_routes._post_cell(
        _Attempt(post_ledger.FLAGGED, detail="the email says 18 EA and the PO says 19 EA",
                 attempts=3),
        _complete_row(), has_pod=True))
    assert "post-pod/confirm" in cell
    assert "18 EA" in cell and "tried 3" in cell


def test_a_partial_post_is_never_offered_a_retry():
    """Retrying would create a second receipt beside the half-built one."""
    cell = str(ui_routes._post_cell(_Attempt(post_ledger.PARTIAL), _complete_row(), has_pod=True))
    assert "post-pod" not in cell and "post-report" not in cell


def test_the_confirm_step_writes_nothing(client, a_record, monkeypatch):
    """The dialog that precedes the write must be able to be opened without performing it."""
    monkeypatch.setattr(killswitch, "is_stopped", lambda: False)
    response = client.post(f"/ui/records/{a_record}/post-pod/confirm")
    assert response.status_code == 200
    assert "Post POD to Spitfire" in response.text
    assert "/post-pod" in response.text, "the dialog carries the button that does perform it"


def test_the_confirm_routes_are_not_reachable_by_GET(client, a_record):
    assert client.get(f"/ui/records/{a_record}/post-pod/confirm").status_code == 405
    assert client.get(f"/ui/records/{a_record}/post-report").status_code == 405


def test_verify_pod_reaches_a_record_that_has_already_posted(client, monkeypatch):
    """The only records worth verifying are the ones `records_ready` excludes — `_mark_pushed`
    moves a posted record to `pushed_to_spitfire`. Looking it up through that view made the
    control unreachable for every record it applied to, which a live check caught and this pins."""
    from pipeline import read_views, state_db
    conn = state_db.get_connection()
    try:
        live = {r[0] for r in conn.execute("SELECT id FROM extracted_records")}
        # Only an attempt whose record still exists. A ledger row outlives its record --
        # `tools/reprocess_mail` erases records and leaves posting history alone -- and those
        # dangling rows are a different case, covered by the route's own message.
        posted = [a for a in post_ledger.latest_by_record(conn)
                  if a.state == post_ledger.POSTED and a.record_id in live]
        if not posted:
            pytest.skip("nothing posted in this store still has its record")
        record_id = posted[0].record_id
        assert record_id not in {r["id"] for r in read_views.records_ready(conn)}, \
            "the premise: a posted record is not on the Records page"
    finally:
        conn.close()

    monkeypatch.setattr(ui_routes.spitfire_post, "verify_pod", lambda conn, row: (
        spitfire_post.PostResult(ok=True, state="verified", po_number="212448",
                                 message="the bytes in Spitfire are the bytes we sent.",
                                 steps=["POD found on receipt 0001"])))
    response = client.post(f"/ui/records/{record_id}/verify-pod")
    assert response.status_code == 200
    assert "No such record" not in response.text
    assert "verified" in response.text.lower()
