"""Posting a whole delivery: one receipt, one row per item line.

The failure this exists to prevent is the one Premier already lived through — `deliveries_store`
records it in its own docstring: one notice covering six lines of PO 206725 became six unrelated
records, "each of which went on to create its own Spitfire receipt — eight of them on PO 212559 in
one afternoon". So the assertions here are mostly *counts*: one `create_receipt`, one
`set_line_quantity`, one POD upload, however many lines arrived.

The write client is faked rather than mocked at the HTTP layer, for the reason
`test_spitfire_post.py` gives: what needs proving is the orchestration. The HTTP shapes are pinned
by `test_spitfire_write_allowlist.py` and were proven against the live server.
"""

import itertools
import sqlite3

import pytest

from pipeline import deliveries_store, post_ledger, spitfire_post, state_db

from tests.test_spitfire_post import FakeWriteClient, OfflineReadClient, pod_pdf


PO = "212614"

# Four item lines on one truck. Three share a spec, which is the ordinary shape rather than an edge
# case — 21 of PO 207249's 22 lines share `FIT-900-FIT`, and 93 of the 390 lines in Premier's mirror
# share a spec with a sibling. It is why the receipt row is found by `SCDocItemKey` and never by the
# spec string.
LINES = [
    # (record id, line number, line_key, spec, description, quantity)
    (1, 1, "k1", "LT-03b", "LT-03B Frosted Replacement", 19.0),
    (2, 2, "k2", "FIT-900", "ADJUSTABLE BENCH", 2.0),
    (3, 3, "k3", "FIT-900", "CRUNCH BENCH", 1.0),
    (4, 4, "k4", "FIT-900", "Medicine Ball 4 Kg", 3.0),
]


class GroupWriteClient(FakeWriteClient):
    """`FakeWriteClient` with a receipt that has a row per purchase-order line.

    The base fake returns exactly one hard-coded item, which is right for the single-record path and
    cannot express the thing being tested here. `missing_keys` models the case the grouped path
    handles differently from the single-record one: the order moved underneath us and a line it was
    built from is no longer on the receipt.
    """

    # Class-level, so two clients in one test cannot mint the same key. Spitfire returns a fresh
    # GUID from every create, and a per-instance counter made a second receipt indistinguishable
    # from the first — which is precisely what these tests are here to tell apart.
    _minted = itertools.count(1)

    def __init__(self, *args, missing_keys=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.missing_keys = set(missing_keys)
        self.receipts: list = []
        self.quantity_calls: list = []

    def create_receipt(self, project_id, po_number, receipt_type_key=None):
        super().create_receipt(project_id, po_number)
        key = f"11111111-2222-3333-4444-{next(self._minted):012d}"
        self.receipts.append(key)
        return key

    def read_items(self, doc_key):
        self._maybe_fail("read_items")
        return [{"DocItemNumber": f"{number:04d}",
                 "SourceItemNumber": spec,
                 "RelatedLineDetails": {"SCDocItemKey": line_key, "Subcontract": self.sub_contract},
                 "DocItemTask": [{"ItemTaskKey": f"task-{number}",
                                  "Quantity": self.quantities.get(f"task-{number}", 0.0)}]}
                for _rid, number, line_key, spec, _d, _q in LINES
                if line_key not in self.missing_keys]

    def set_line_quantity(self, doc_key, quantities):
        # Recorded as a whole call, not merged, so "one PATCH for the delivery" is provable.
        self.quantity_calls.append(dict(quantities))
        super().set_line_quantity(doc_key, quantities)


@pytest.fixture
def conn():
    """One delivery of four item lines against one purchase order."""
    c = state_db.get_connection(":memory:")
    c.execute(
        """INSERT INTO spitfire_po_index (po_number, doc_master_key, project_code, refreshed_at)
           VALUES (?, '4b186a21-59be-4c0a-8221-6f20363e6191', 'MRC024PB100003', 'now')""", (PO,))
    for record_id, line_number, line_key, spec, description, quantity in LINES:
        c.execute(
            """INSERT INTO extracted_records
               (id, source_email_id, po_number, spec_code, item_description, vendor_name,
                quantity_received, unit_of_measure, pod_stated_date, received_by, po_line_number,
                email_date, extraction_source, extraction_confidence, created_at, status)
               VALUES (?, 'mail-1', ?, ?, ?, 'Archipelago Lighting', ?, 'EA', '2026-01-20',
                       'J Smith', ?, '2026-01-20', 'test', 1.0, '2026-01-20', 'pending')""",
            (record_id, PO, spec, description, quantity, line_number))
        c.execute(
            """INSERT INTO spitfire_po_lines
               (line_key, po_number, line_number, spec_code, description, unit_of_measure,
                qty_ordered, qty_received, qty_in_transit, cost_code, refreshed_at)
               VALUES (?, ?, ?, ?, ?, 'EA', ?, 0.0, 0.0, 'MAT-FDP', 'now')""",
            (line_key, PO, line_number, spec, description, quantity))
    c.execute(
        """INSERT INTO mail_attachment
           (email_id, ordinal, filename, content_type, kind, size_bytes, is_inline, content)
           VALUES ('mail-1', 0, 'POD.pdf', 'application/pdf', 'pdf', 9, 0, ?)""", (pod_pdf(PO),))
    c.execute(
        """INSERT INTO attachment_ledger
           (email_id, depth, ordinal, filename, sniffed_kind, disposition, is_inline, first_seen_at)
           VALUES ('mail-1', 0, 0, 'POD.pdf', 'pdf', 'extracted', 0, 'now')""")
    c.commit()

    delivery_id = deliveries_store.upsert(
        c, po_number=PO, delivery_ref="shipment:ALS-1", delivery_rung="shipment", now="now",
        facts={"source_email_id": "mail-1", "pod_stated_date": "2026-01-20",
               "received_by": "J Smith"})
    deliveries_store.attach(c, delivery_id, [row[0] for row in LINES])
    return c


def _delivery_of(conn) -> int:
    """The one delivery the fixture built. Looked up rather than stashed on the connection —
    `sqlite3.Connection` takes no attributes."""
    return int(conn.execute("SELECT id FROM deliveries").fetchone()[0])


@pytest.fixture(autouse=True)
def no_chrome(monkeypatch):
    monkeypatch.setattr(spitfire_post.report_pdf, "render", lambda html, **kw: b"%PDF-fake")


def post(conn, client=None, **kwargs):
    return spitfire_post.post_delivery_pod(
        conn, _delivery_of(conn), client=client or GroupWriteClient(sub_contract=PO),
        read_client_factory=OfflineReadClient, **kwargs)


# --- the whole point ----------------------------------------------------------------------------

def test_a_four_line_delivery_makes_one_receipt_not_four(conn):
    client = GroupWriteClient(sub_contract=PO)
    result = post(conn, client)

    assert result.ok, result.message
    assert len(client.receipts) == 1, (
        f"a delivery of {len(LINES)} lines made {len(client.receipts)} receipts — this is the "
        f"eight-receipts-on-PO-212559 failure returning")
    assert client.calls.count("create_receipt") == 1
    assert len(result.posted_lines) == len(LINES)


def test_every_quantity_goes_in_one_patch(conn):
    """One PATCH, one session, one commit. `DELETE /session` is what commits, so a PATCH per line
    would be a commit per line — exactly the half-filled receipt the single call prevents."""
    client = GroupWriteClient(sub_contract=PO)
    post(conn, client)

    assert len(client.quantity_calls) == 1, "the delivery must be one round trip"
    assert client.quantity_calls[0] == {"task-1": 19.0, "task-2": 2.0, "task-3": 1.0, "task-4": 3.0}


def test_each_item_line_keeps_its_own_row_and_quantity(conn):
    """Grouping is a receipt-level change. Nothing is merged and nothing is summed — four item
    lines are four rows on one document, each with its own quantity."""
    client = GroupWriteClient(sub_contract=PO)
    post(conn, client)

    assert client.quantities == {"task-1": 19.0, "task-2": 2.0, "task-3": 1.0, "task-4": 3.0}
    assert len(set(client.quantities)) == len(LINES), "two lines collapsed onto one receipt row"
    assert sum(client.quantities.values()) == sum(row[5] for row in LINES)


def test_three_lines_sharing_a_spec_land_on_three_different_rows(conn):
    """The receipt row is found by `SCDocItemKey`, never by the spec string.

    Three of the four fixture lines share `FIT-900`, which is the ordinary shape: 21 of PO 207249's
    22 lines share one spec. Matching on the spec would put all three onto whichever row came first.
    """
    client = GroupWriteClient(sub_contract=PO)
    post(conn, client)

    shared = {f"task-{number}" for _r, number, _k, spec, _d, _q in LINES if spec == "FIT-900"}
    assert len(shared) == 3
    assert shared.issubset(client.quantities)
    assert [client.quantities[t] for t in sorted(shared)] == [2.0, 1.0, 3.0]


def test_the_pod_is_uploaded_once_for_the_whole_delivery(conn):
    """Four lines share one proof of delivery. The catalog does not deduplicate — uploading the
    same bytes four times would leave four entries in Premier's catalog and four attachments."""
    client = GroupWriteClient(sub_contract=PO)
    post(conn, client)

    assert client.calls.count("upload_file") == 1
    assert client.calls.count("attach_file") == 1
    assert len(client.uploads) == 1


def test_the_route_is_signed_once_for_the_whole_delivery(conn):
    """One receipt, one signature. Signing per line would respond to our own stop four times on
    one document — and the stop is a property of the receipt, not of the item lines on it."""
    client = GroupWriteClient(sub_contract=PO)
    result = post(conn, client)

    assert client.calls.count("sign_off_route_steps") == 1
    assert any("route step" in step for step in result.steps), result.steps


def test_a_delivery_whose_route_cannot_be_signed_still_posts(conn):
    """Four lines, a real receipt and a real POD on it. An unsigned route step is a sentence in
    the result, not a reason to leave the group PARTIAL for somebody to investigate."""
    client = GroupWriteClient(sub_contract=PO, fail_on="sign_off_route_steps")
    result = post(conn, client)

    assert result.ok and result.state == post_ledger.POD_POSTED
    assert any("could not sign off the route" in step for step in result.steps), result.steps

def test_the_ledger_keeps_a_row_per_record_all_naming_one_receipt(conn):
    """The grain stays per record — thirty readers key on `record_id` — while `receipt_key` and
    `group_key` say the rows are one document."""
    result = post(conn)

    attempts = [post_ledger.existing_for_record(conn, row[0])[0] for row in LINES]
    assert {a.state for a in attempts} == {post_ledger.POD_POSTED}
    assert {a.receipt_key for a in attempts} == {result.receipt_key}
    assert {a.group_key for a in attempts} == {result.group_key}
    assert len(post_ledger.for_receipt(conn, result.receipt_key)) == len(LINES)


# --- partial and refusal ------------------------------------------------------------------------

def test_a_line_with_no_row_on_the_receipt_is_dropped_and_the_rest_post(conn):
    """Where the single-record path aborts, the grouped path drops and continues.

    `test_a_receipt_with_no_line_against_the_po_line_is_refused` pins the abort for `post_pod`, and
    that stays right: it has nothing else to do. Here, discarding three good lines to protect one
    already lost would leave a real receipt with every quantity at zero and no way back — `PARTIAL`
    blocks and offers no retry button. Premier's own proc drops unmatchable items too.
    """
    client = GroupWriteClient(sub_contract=PO, missing_keys={"k3"})
    result = post(conn, client)

    assert result.ok
    assert len(client.receipts) == 1
    assert client.quantity_calls[0] == {"task-1": 19.0, "task-2": 2.0, "task-4": 3.0}

    dropped = post_ledger.existing_for_record(conn, 3)[0]
    assert dropped.state == post_ledger.FLAGGED, "a dropped line must not block the next attempt"
    assert not dropped.is_blocking
    assert "no receipt line" in dropped.detail
    assert {a.state for a in (post_ledger.existing_for_record(conn, i)[0] for i in (1, 2, 4))} == {
        post_ledger.POD_POSTED}


def test_a_failure_after_the_receipt_exists_leaves_every_row_partial(conn):
    """All of them, never a mix.

    `FAILED` is deliberately not blocking, so one row settling `FAILED` beside a receipt that exists
    would let the next Post sail past the guard and build a second receipt next to the half-built
    one — the failure `PARTIAL` exists to prevent.
    """
    client = GroupWriteClient(sub_contract=PO, fail_on="set_line_quantity")
    result = post(conn, client)

    assert not result.ok
    states = {post_ledger.existing_for_record(conn, row[0])[0].state for row in LINES}
    assert states == {post_ledger.PARTIAL}


def test_a_failure_before_anything_is_created_leaves_every_row_failed_and_retryable(conn):
    client = GroupWriteClient(sub_contract=PO, fail_on="create_receipt")
    result = post(conn, client)

    assert not result.ok
    attempts = [post_ledger.existing_for_record(conn, row[0])[0] for row in LINES]
    assert {a.state for a in attempts} == {post_ledger.FAILED}
    assert not any(a.is_blocking for a in attempts), "nothing was created, so nothing is orphaned"


def test_an_unlinked_receipt_stops_the_whole_group(conn):
    """`forBatch` is the only thing tying a receipt to its order. Without it the document is an
    orphan, and uploading a proof of delivery onto it would compound the problem."""
    client = GroupWriteClient(sub_contract="999999")
    result = post(conn, client)

    assert not result.ok
    assert "upload_file" not in client.calls
    assert {post_ledger.existing_for_record(conn, row[0])[0].state for row in LINES} == {
        post_ledger.PARTIAL}


def test_posting_a_delivery_twice_creates_no_second_receipt(conn):
    """The second click must not reach Spitfire at all.

    Falling through to `create_receipt` with nothing left to post would leave an **empty** document
    on Premier's instance, and no `DELETE` in this client can remove it.
    """
    post(conn)
    second = GroupWriteClient(sub_contract=PO)
    result = spitfire_post.post_delivery_pod(
        conn, _delivery_of(conn), client=second, read_client_factory=OfflineReadClient)

    assert not result.ok
    assert second.calls == [], "the guard must bite before the network"
    assert second.receipts == []


def test_two_lines_resolving_to_one_po_line_are_refused_not_summed(conn):
    """`set_line_quantity` assigns, so writing both keeps whichever went last; adding them would
    book a number no gate ever approved, because the gates ran per record."""
    conn.execute("UPDATE extracted_records SET po_line_number = 2 WHERE id = 3")
    conn.commit()

    client = GroupWriteClient(sub_contract=PO)
    result = post(conn, client)

    assert result.ok, "the uninvolved lines still post"
    assert "task-2" not in client.quantities and "task-3" not in client.quantities
    assert client.quantities == {"task-1": 19.0, "task-4": 3.0}
    for record_id in (2, 3):
        attempt = post_ledger.existing_for_record(conn, record_id)[0]
        assert attempt.state == post_ledger.FLAGGED
        assert "resolve to purchase order line 0002" in attempt.detail


def test_a_line_held_back_today_becomes_a_second_receipt_tomorrow(conn):
    """A posted receipt is never reopened — Premier may have approved it and `DELETE` is refused."""
    first = GroupWriteClient(sub_contract=PO, missing_keys={"k3"})
    first_result = post(conn, first)

    second = GroupWriteClient(sub_contract=PO)
    second_result = spitfire_post.post_delivery_pod(
        conn, _delivery_of(conn), client=second, read_client_factory=OfflineReadClient)

    assert second_result.ok
    assert second_result.receipt_key != first_result.receipt_key
    assert second.quantity_calls == [{"task-3": 1.0}], "only the line that was held back"
    assert second_result.group_key != first_result.group_key


# --- the report ---------------------------------------------------------------------------------

def test_one_report_covers_the_whole_receipt(conn):
    result = post(conn)
    client = GroupWriteClient(sub_contract=PO)
    report = spitfire_post.post_delivery_report(conn, _delivery_of(conn), client=client)

    assert report.ok, report.message
    assert report.receipt_key == result.receipt_key
    assert client.calls.count("upload_file") == 1, "one report for the delivery, not one per line"
    assert {post_ledger.existing_for_record(conn, row[0])[0].state for row in LINES} == {
        post_ledger.POSTED}


def test_every_record_is_marked_pushed(conn):
    """An unmarked record stays in `records_ready` and the page offers it again."""
    post(conn)
    spitfire_post.post_delivery_report(
        conn, _delivery_of(conn), client=GroupWriteClient(sub_contract=PO))

    conn.row_factory = sqlite3.Row
    states = {r["status"] for r in conn.execute("SELECT status FROM extracted_records")}
    assert states == {"pushed_to_spitfire"}


def test_the_report_cannot_be_posted_before_the_pod(conn):
    client = GroupWriteClient(sub_contract=PO)
    result = spitfire_post.post_delivery_report(conn, _delivery_of(conn), client=client)

    assert not result.ok
    assert client.calls == []
    assert "post the POD first" in result.message
