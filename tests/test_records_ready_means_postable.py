""""Ready" has to mean the Post button will take it.

Three tests were being treated as one, and they are not the same:

  * **complete** — `completeness.REQUIRED`, the five fields only the email can supply.
  * **ready** — complete, plus the SQL pre-filter. What the Records page lists.
  * **postable** — ready, plus `post_decision` gate 2: a proof document, a named waiver, or
    `body_evidence`.

Measured on the live store: 169 records were ready and **45** could actually post.

The defect that prompted this ran the other way and mattered more. `_post_cell` asked whether the
email carried anything that could be a POD, and drew a "waive POD" prompt when it did not. That test
never consults `body_evidence` — Premier's 2026-08-22 route for a delivery stated entirely in the
mail — so **11 records the gate would have accepted** were behind a prompt asking someone to waive a
proof that was not required. Every one was an `authority_inbound` (signer + date) or
`authority_delivered` (carrier + tracking + date): exactly the sources `body_evidence`'s own
docstring says it exists to admit.

The fix is one predicate, `post_decision.can_post_offline`, asked by both the gate and the page. So
what these tests really hold is that there is only one answer to the question.
"""

import sqlite3

import pytest

from pipeline import completeness, post_decision, read_views, state_db


@pytest.fixture
def conn(tmp_path):
    connection = state_db.get_connection(tmp_path / "state.sqlite3")
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


def _row(**over):
    base = {
        "id": 1, "po_number": "210634", "spec_code": "BASE-01",
        "item_description": "Floor lamp", "quantity_received": 4,
        "pod_stated_date": "2026-09-01", "received_by": "", "carrier_name": "",
        "tracking_number": "", "pod_waived_by": "", "pod_ledger_id": None,
    }
    base.update(over)
    return base


# --- the eleven ------------------------------------------------------------

def test_a_signed_delivery_with_no_pod_file_may_post():
    """`authority_inbound`: a signer and a date, nothing attached. Premier accepted this route on
    2026-08-22, and the page was refusing it."""
    assert post_decision.can_post_offline(
        _row(received_by="U ALI"), has_pod_bytes=False) == "signer+date"


def test_a_carrier_reference_with_no_pod_file_may_post():
    """`authority_delivered`: carrier and tracking, checkable against the carrier."""
    assert post_decision.can_post_offline(
        _row(carrier_name="FedEx", tracking_number="7497809572"),
        has_pod_bytes=False) == "carrier+tracking+date"


def test_a_date_on_its_own_is_not_evidence_of_delivery():
    """The guard that keeps the above narrow, and the reason it must stay narrow.

    Premier's own pending-confirmation worklists carry quantities and dates for goods that have not
    arrived — 57 of the 92 corpus records. Admitting a bare date would post receipts for goods still
    in transit, which is worse than any number of unposted records.
    """
    assert post_decision.can_post_offline(_row(), has_pod_bytes=False) == ""


def test_a_pod_file_and_a_waiver_each_still_work():
    assert post_decision.can_post_offline(_row(), has_pod_bytes=True) == "pod"
    assert post_decision.can_post_offline(
        _row(pod_waived_by="A Reviewer"), has_pod_bytes=False) == "waived"


def test_nothing_at_all_may_not_post():
    assert post_decision.can_post_offline(
        _row(pod_stated_date=""), has_pod_bytes=False) == ""


# --- the gate and the page must not drift ---------------------------------

def test_the_gate_asks_the_same_question_the_page_does():
    """The assertion that would have caught the original defect.

    Both sides must route through `can_post_offline`. A second copy of this rule is precisely how
    the page and the gate came to disagree, and the disagreement is invisible until someone finds a
    record that will not post and cannot see why.
    """
    import inspect

    from api.ui import routes

    gate = inspect.getsource(post_decision.decide)
    assert "can_post_offline(row, has_pod_bytes=False" in gate, (
        "gate 2 must ask through the shared predicate")
    # Matched as a prefix, not as the whole call. `document_evidence` added an `origin_sender=`
    # argument on 2026-09-11 and an exact-text assertion failed on a change that did precisely what
    # this test exists to require. What must not change is which predicate is asked.
    assert "origin_sender=origin_sender" in gate, (
        "gate 2 must hand the predicate the sender it resolved, not let it read the row — the two "
        "projections carry different columns and that is how the button and the gate drift apart")

    page = inspect.getsource(routes._post_cell)
    assert "can_post_offline" in page, (
        "the Post cell must ask the shared predicate, not its own POD test")

    counter = inspect.getsource(routes.records_page)
    assert "can_post_offline" in counter, (
        "the delivery 'N to post' badge must agree with the buttons under it")


def test_postable_is_a_subset_of_ready_and_every_row_clears_the_gate(conn):
    """The invariant that keeps the two lists honest against each other."""
    ready = read_views.records_ready(conn)
    postable = read_views.records_postable(conn)

    assert {r["id"] for r in postable} <= {r["id"] for r in ready}

    pod_emails = read_views.emails_with_a_possible_pod(conn)
    for row in postable:
        assert post_decision.can_post_offline(
            row, has_pod_bytes=row["source_email_id"] in pod_emails), (
            f"record {row['id']} is counted postable but the gate would refuse it")


def test_the_summary_reports_both_numbers(conn):
    """One number cannot carry both meanings. "Ready" is read as work that can be done now."""
    summary = read_views.summary(conn)

    assert hasattr(summary, "records_postable")
    assert summary.records_postable <= summary.records_ready


# --- what must stay visible ------------------------------------------------

def test_a_complete_record_that_cannot_post_is_still_shown_somewhere(conn):
    """Counting it honestly must not mean hiding it.

    A complete record with no proof stays on the Records page so a reviewer can see the blocker and
    waive it; a complete record with a quantity conflict is kept off that page by `_READY_CLAUSE`
    and appears on the manual queue instead; a record read out of a message Premier wrote itself
    waits for somebody to confirm the goods arrived. Every pending record belongs to exactly one of
    those three — `read_views` states that as an invariant, and it was once broken for 21 records
    that appeared on none of them.

    All three are named here even though this module's fixture is external mail throughout. An
    invariant asserted over two of three destinations passes for as long as the fixture happens to
    avoid the third, and then stops meaning anything without failing.
    """
    ready_ids = {r["id"] for r in read_views.records_ready(conn)}
    queue_ids = {i.ref_id for i in read_views.manual_queue(conn) if i.kind == "record"}
    waiting_ids = {r["id"] for r in read_views.records_awaiting_confirmation(conn)}

    pending = read_views._rows(
        conn, "SELECT r.* FROM extracted_records r WHERE r.status='pending'")

    for row in pending:
        assert row["id"] in ready_ids or row["id"] in queue_ids or row["id"] in waiting_ids, (
            f"record {row['id']} appears on no destination — not the Records page, not the manual "
            f"queue, not awaiting confirmation")


# --- email-body rows, and the Delivery status page (Premier, 2026-09-15) ---------------------

@pytest.mark.parametrize("missing", completeness.REQUIRED)
def test_a_row_read_from_the_email_body_must_carry_every_required_field(missing):
    """A body row has no document to fall back on, so the email itself must have said all of it."""
    row = _row(extraction_source="freetext", received_by="U ALI", **{missing: None})
    assert not completeness.is_complete(row), f"a body row with no {missing} counted complete"


def test_delivery_status_lists_only_purchase_orders_with_a_final_receipt(conn):
    from pipeline import post_ledger

    for key, po, state in (("k1", "900001", post_ledger.POSTED),
                           ("k2", "900002", post_ledger.POD_POSTED),
                           ("k3", "900003", post_ledger.FLAGGED),
                           ("k4", "900004", post_ledger.PARTIAL)):
        conn.execute("INSERT INTO spitfire_post (idempotency_key, record_id, po_number, state, "
                     "claimed_at) VALUES (?, 1, ?, ?, 'now')", (key, po, state))
    conn.commit()

    assert post_ledger.posted_po_numbers(conn) == {"900001"}
