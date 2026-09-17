"""The delivery routes: the ones that create a receipt carrying a whole truck.

These write to Premier's ERP, so they keep the same three guards as the per-record routes and in the
same order — kill switch, offline, single-writer lock. That is asserted here rather than assumed,
because `_delivery_write_guarded` is a second door onto the same room and a guard that applies to
only one of two doors is not a guard.

Every test patches the post chain. As `test_ui_post_route.py` puts it, a test that could reach
Spitfire to prove the route cannot would be self-defeating — and the stakes are higher here: one
call creates a document carrying every item line of a delivery.
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
def a_delivery():
    """A delivery id with at least one line the Records page is offering.

    Skips rather than fabricating one: the route reads through `read_views.records_ready`, and a
    hand-inserted row would not exercise the same path.
    """
    from pipeline import read_views, state_db
    conn = state_db.get_connection()
    try:
        for row in read_views.records_ready(conn):
            if row["delivery_id"]:
                return int(row["delivery_id"])
        pytest.skip("no delivery in the pipeline store with a postable line")
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def never_really_post(monkeypatch):
    """The same standing guard `test_ui_post_route.py` keeps, for the same reason."""
    def refuse(*args, **kwargs):
        raise AssertionError("a test reached the real post chain")
    for name in ("post_pod", "post_report", "post_delivery_pod", "post_delivery_report"):
        monkeypatch.setattr(spitfire_post, name, refuse)


class _Result:
    """What `post_delivery_pod` returns, reduced to what the fragment reads."""
    def __init__(self, ok=True, state=post_ledger.POD_POSTED):
        self.ok = ok
        self.state = state
        self.message = "4 item lines posted to receipt 0007 on PO 212614"
        self.receipt_doc_no = "0007"
        self.po_number = "212614"
        self.steps = ["receipt created", "quantities set on 4 lines in one patch"]


def test_an_unknown_delivery_is_reported_not_posted(client):
    response = client.post("/ui/deliveries/99999999/post-pod")
    assert response.status_code == 200
    assert "No such delivery" in response.text


def test_the_kill_switch_stops_the_delivery_post(client, a_delivery, monkeypatch):
    monkeypatch.setattr(killswitch, "is_stopped", lambda: True)
    monkeypatch.setattr(spitfire_post, "post_delivery_pod",
                        lambda *a, **k: pytest.fail("the kill switch did not stop the post"))

    response = client.post(f"/ui/deliveries/{a_delivery}/post-pod")
    assert response.status_code == 200
    assert "kill switch" in response.text


def test_a_run_in_progress_turns_the_delivery_post_away(client, a_delivery, monkeypatch):
    """Turned away, not queued: a queued second caller is a second receipt."""
    monkeypatch.setattr(killswitch, "is_stopped", lambda: False)
    monkeypatch.setattr(ui_routes.spitfire_cassette, "writes_refused", lambda: False)
    monkeypatch.setattr(spitfire_post, "post_delivery_pod",
                        lambda *a, **k: pytest.fail("the lock did not hold the post off"))

    runner._LOCK.acquire()
    try:
        response = client.post(f"/ui/deliveries/{a_delivery}/post-pod")
    finally:
        runner._LOCK.release()
    assert response.status_code == 200
    assert "mid-run" in response.text


def test_the_lock_is_released_even_when_the_delivery_post_raises(client, a_delivery, monkeypatch):
    """A lock left held by a failure would wedge every later post, including the per-record ones."""
    monkeypatch.setattr(killswitch, "is_stopped", lambda: False)
    monkeypatch.setattr(ui_routes.spitfire_cassette, "writes_refused", lambda: False)

    def explode(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(spitfire_post, "post_delivery_pod", explode)

    with pytest.raises(RuntimeError):
        client.post(f"/ui/deliveries/{a_delivery}/post-pod")

    assert runner._LOCK.acquire(blocking=False), "the lock was not released"
    runner._LOCK.release()


def test_being_offline_refuses_before_anything_is_claimed(client, a_delivery, monkeypatch):
    """Refused at the route, not at the socket. The cassette raises on the write itself as a
    backstop, but that raise lands after the claim — leaving rows the ledger reads as "a receipt
    may exist somewhere"."""
    monkeypatch.setattr(killswitch, "is_stopped", lambda: False)
    monkeypatch.setattr(ui_routes.spitfire_cassette, "writes_refused", lambda: True)
    monkeypatch.setattr(spitfire_post, "post_delivery_pod",
                        lambda *a, **k: pytest.fail("offline did not refuse the post"))

    response = client.post(f"/ui/deliveries/{a_delivery}/post-pod")
    assert response.status_code == 200
    assert "office network" in response.text


@pytest.mark.parametrize("path", ["post-pod", "post-report", "post-pod/confirm",
                                  "post-report/confirm"])
def test_the_delivery_routes_are_not_reachable_by_GET(client, path):
    """A write must not be one link away from a crawler, a prefetch or a pasted URL."""
    response = client.get(f"/ui/deliveries/1/{path}")
    assert response.status_code == 405


def test_a_successful_delivery_post_says_it_is_not_routed(client, a_delivery, monkeypatch):
    """Creating a receipt stages three real Premier employees. Nothing is sent — but the person who
    pressed the button has to be told the receipt is theirs to approve."""
    monkeypatch.setattr(killswitch, "is_stopped", lambda: False)
    monkeypatch.setattr(ui_routes.spitfire_cassette, "writes_refused", lambda: False)
    monkeypatch.setattr(spitfire_post, "post_delivery_pod",
                        lambda conn, delivery_id: _Result())

    response = client.post(f"/ui/deliveries/{a_delivery}/post-pod")
    assert response.status_code == 200
    assert "In Process" in response.text
    assert "not been routed" in response.text


# --- the cell -----------------------------------------------------------------------------------
# `_post_cell` with no `delivery` is the per-record control it always was; those six cases are
# pinned in `test_ui_post_route.py` and must keep passing untouched.

def _record(**over):
    row = {"id": 41, "po_number": "212614", "pod_waived_by": "", "spec_code": "LT-03b",
           "item_description": "LT-03B", "quantity_received": 19.0, "unit_of_measure": "EA",
           "pod_stated_date": "2026-01-20", "received_by": "J Smith"}
    row.update(over)
    return row


def test_only_the_first_row_of_a_delivery_carries_the_post_button(monkeypatch):
    monkeypatch.setattr(ui_routes.spitfire_cassette, "writes_refused", lambda: False)
    head = ui_routes.DeliveryCell(delivery_id=7, is_first=True, lines=20, to_post=20)
    rest = ui_routes.DeliveryCell(delivery_id=7, is_first=False, lines=20, to_post=20)

    first = str(ui_routes._post_cell(None, _record(), delivery=head))
    other = str(ui_routes._post_cell(None, _record(id=42), delivery=rest))

    assert "/ui/deliveries/7/post-pod/confirm" in first
    assert "Post 20 lines" in first
    assert "data-verify" not in other, "a per-row Post is how 20 lines became 20 receipts"


def test_the_delivery_button_counts_what_is_ready_not_what_is_shown(monkeypatch):
    """It offers the lines that could post, not every line in the block. What will actually *land*
    needs the live purchase-order read the confirm dialog pays for."""
    monkeypatch.setattr(ui_routes.spitfire_cassette, "writes_refused", lambda: False)
    cell = str(ui_routes._post_cell(None, _record(), delivery=ui_routes.DeliveryCell(
        delivery_id=7, is_first=True, lines=20, to_post=16)))
    assert "Post 16 lines" in cell


def test_a_part_posted_delivery_still_offers_to_post_the_rest(monkeypatch):
    """The dead end this exists to prevent, and it was real.

    PO 207249's line 2 was posted on its own before grouping existed, so the block's first row sits
    at `POD_POSTED`. Reading the control off that row alone showed only "Post report" — and the
    nineteen lines of the same truck that had never been posted had no button anywhere on the page.
    """
    monkeypatch.setattr(ui_routes.spitfire_cassette, "writes_refused", lambda: False)

    class Posted:
        state = post_ledger.POD_POSTED
        receipt_doc_no = "0001"
        receipt_key = "abcdefgh-1111"
        detail = ""
        attempts = 1
        claimed_at = "2026-08-31"

    cell = str(ui_routes._post_cell(Posted(), _record(), delivery=ui_routes.DeliveryCell(
        delivery_id=15, is_first=True, lines=20, to_post=19, report_due=True)))

    assert "/ui/deliveries/15/post-pod/confirm" in cell, "no way to post the other 19 lines"
    assert "Post 19 lines" in cell
    # and the receipt that already exists still gets its report
    assert "/ui/deliveries/15/post-report/confirm" in cell
    assert "report pending" in cell


def test_a_block_with_nothing_ready_offers_no_post_button(monkeypatch):
    """A control whose only possible outcome is a refusal is not drawn — the rule `_post_cell`
    already kept per row, now kept per block.

    The fixture clears `received_by` explicitly. `_record()` carries a signer and a date, which is
    *body evidence*: Premier accepted on 2026-08-22 that a delivery stated entirely in the mail may
    post without a proof document, so a block built from it is not blocked at all. This test was
    passing for the wrong reason — it described rows that "lack a POD" while holding rows that had
    proof, just not an attached one.
    """
    monkeypatch.setattr(ui_routes.spitfire_cassette, "writes_refused", lambda: False)
    cell = str(ui_routes._post_cell(
        None, _record(received_by=None, carrier_name=None, tracking_number=None),
        has_pod=False,
        delivery=ui_routes.DeliveryCell(delivery_id=15, is_first=True, lines=20, to_post=0)))

    assert "post-pod/confirm" not in cell
    assert "no POD" in cell
    assert "Accept anyway" in cell, "the head row must still say how to unblock its block"


def test_a_block_whose_delivery_is_stated_in_the_mail_does_offer_post(monkeypatch):
    """The counterpart. A signer and a date, no attachment: the block posts.

    Held per block as well as per row because the two used different tests until now, so the head
    row of a body-evidence delivery could offer Post while its own siblings said "no POD" about the
    same goods.
    """
    monkeypatch.setattr(ui_routes.spitfire_cassette, "writes_refused", lambda: False)
    cell = str(ui_routes._post_cell(
        None, _record(), has_pod=False,
        delivery=ui_routes.DeliveryCell(delivery_id=15, is_first=True, lines=20, to_post=20)))

    assert "post-pod/confirm" in cell
    assert "Accept anyway" not in cell, "nothing needs waiving; the mail states the delivery"


def test_a_record_with_no_delivery_keeps_the_per_record_route(monkeypatch):
    monkeypatch.setattr(ui_routes.spitfire_cassette, "writes_refused", lambda: False)
    cell = str(ui_routes._post_cell(None, _record(), delivery=None))
    assert "/ui/records/41/post-pod/confirm" in cell
    assert "/ui/deliveries/" not in cell


def test_a_middle_row_still_reports_what_happened_to_it(monkeypatch):
    """No button, but not silent: each item line is its own row on the receipt and can be flagged
    on its own while its neighbours post."""
    monkeypatch.setattr(ui_routes.spitfire_cassette, "writes_refused", lambda: False)
    rest = ui_routes.DeliveryCell(delivery_id=7, is_first=False, lines=20)

    class Attempt:
        state = post_ledger.FLAGGED
        detail = "this would over-receive"
        attempts = 2
        receipt_doc_no = ""
        receipt_key = ""
        claimed_at = "2026-08-31"

    cell = str(ui_routes._post_cell(Attempt(), _record(id=42), delivery=rest))
    assert "blocked" in cell
    assert "over-receive" in cell
    assert "data-verify" not in cell


# --- Verify POD on the head row ----------------------------------------------------------------
#
# Found on PO 207249, 2026-09-16. Record 163 headed a block whose other 19 lines were on receipt
# 0002, but 163 itself had been refused at gate 6 and never posted. The head row drew the block's
# "Post report" and "report pending" — correctly — beside a Verify POD pointing at its own record,
# whose only possible answer was "nothing has been posted to Spitfire for this record yet".

def test_verify_pod_on_a_head_row_targets_a_line_that_actually_posted(monkeypatch):
    monkeypatch.setattr(ui_routes.spitfire_cassette, "writes_refused", lambda: False)
    head = ui_routes.DeliveryCell(delivery_id=7, is_first=True, lines=20, report_due=True,
                                  # the head row's own record never reached a receipt
                                  verify_record_id=146)

    cell = str(ui_routes._post_cell(None, _record(id=163), delivery=head))

    assert "/ui/records/146/verify-pod" in cell
    assert "/ui/records/163/verify-pod" not in cell, (
        "the head row need not be one of the lines that posted")


def test_a_lone_record_still_verifies_its_own_pod(monkeypatch):
    """No delivery block, so there is no other line it could mean."""
    monkeypatch.setattr(ui_routes.spitfire_cassette, "writes_refused", lambda: False)

    class _Posted:
        state = post_ledger.POSTED
        receipt_doc_no = "0004"
        receipt_key = "k" * 36
        detail = ""
        attempts = 1
        claimed_at = "2026-08-31"

    cell = str(ui_routes._post_cell(_Posted(), _record(id=99)))
    assert "/ui/records/99/verify-pod" in cell


def test_a_block_with_nothing_posted_does_not_borrow_another_rows_receipt(monkeypatch):
    """`verify_record_id` is None when no line of the block reached a receipt, and the button then
    falls back to this row — which is honest, because there is no receipt to point at."""
    monkeypatch.setattr(ui_routes.spitfire_cassette, "writes_refused", lambda: False)
    head = ui_routes.DeliveryCell(delivery_id=7, is_first=True, lines=3, to_post=3)

    cell = str(ui_routes._post_cell(None, _record(id=163), delivery=head))

    assert "/ui/records/163/verify-pod" in cell or "verify-pod" not in cell
