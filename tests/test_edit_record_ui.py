"""The correct-a-record screen, driven the way a person drives it.

`test_record_edit.py` holds the rules; this holds the screen that reaches them. Until now there was
no HTTP-level test of `/ui/records/{id}/edit` at all — the form existed, was linked from exactly one
page, and every property below was true only by inspection.

Three things are asserted here that cannot be asserted anywhere else:

* **Reachability.** The Records page is where a reviewer looks at records, and it had no way to
  change one. The form was reachable only from the Needs-a-human queue, so correcting something on
  a row you were already looking at meant knowing that a second page existed and finding the row
  again on it.
* **The ledger block.** `records_fixable` asks `status IN ('pending','failed')`, and
  `spitfire_post._mark_pushed` moves a record to `pushed_to_spitfire` only when *every* step
  landed. A record whose receipt and proof are already in Spitfire with the receiver report still
  outstanding is `pending` — so it passed that check, and the form would have edited it.
* **Where Cancel goes.** The form serves two entry points and has to return to whichever it came
  from, which is a redirect built from a query parameter — and the test that it can never be turned
  into somebody else's URL.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from api.stores.extracted_store import EDITABLE_FIELDS
from config import settings
from pipeline import email_log, post_ledger, state_db

RECORD = dict(
    source_email_id="mail-edit", po_number="908491", spec_code="STE-402-LT-B",
    item_description="BASE, Floor Lamp 2", quantity_received=11.0, unit_of_measure="EA",
    package_quantity=41.0, package_uom="CTN", pod_stated_date="2025-10-01",
    carrier_name="Nolan", tracking_number="TRK-1", received_by="U ALI",
    vendor_name="Authority", notification_number="239475", po_line_number=300,
    email_date="2025-10-01", extraction_source="authority_inbound", extraction_confidence=1.0,
    status="pending", created_at="2026-08-21 09:00:00", origin="auto",
)

# What a browser actually submits: every editable box, whether or not it was touched.
FORM = {name: "" for name in EDITABLE_FIELDS}
FORM.update({
    "spec_code": "STE-402-LT-B", "item_description": "BASE, Floor Lamp 2",
    "quantity_received": "11", "unit_of_measure": "EA",
    "package_quantity": "41", "package_uom": "CTN", "pod_stated_date": "2025-10-01",
    "carrier_name": "Nolan", "tracking_number": "TRK-1", "received_by": "U ALI",
    "vendor_name": "Authority", "notification_number": "239475",
    "edited_by": "M Rivera",
})


def _insert(conn, **overrides):
    values = dict(RECORD, **overrides)
    columns = ", ".join(values)
    conn.execute(f"INSERT INTO extracted_records ({columns}) "
                 f"VALUES ({', '.join('?' * len(values))})", tuple(values.values()))
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A throwaway pipeline store the whole app points at.

    Overriding `deps.get_pipeline_conn` alone is not enough — the async POST handler opens its own
    connection through `deps.pipeline_connection`, for the threading reason its docstring gives.
    Pointing the setting is what makes both paths agree. Copied from `test_create_record_ui.py`
    rather than shared, because that file says the same thing about the same trap.
    """
    path = tmp_path / "pipeline_state.sqlite3"
    monkeypatch.setattr(settings, "PIPELINE_STATE_DB_PATH", path)

    conn = state_db.get_connection(path)
    email_log.record(conn, email_id="mail-edit", subject="Delivered - 908491 - 11 EA",
                     sender="routing@example-logistics.test", category="route",
                     matched_rule="rule_7", reason="", folder="Routed",
                     processed_at="2026-08-21 09:00:00", po_hints="908491")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def client(store):
    from api.main import app

    return TestClient(app)


def add_record(store, **overrides):
    conn = state_db.get_connection(store)
    try:
        record_id = _insert(conn, **overrides)
        conn.commit()
        return record_id
    finally:
        conn.close()


def block_with_a_receipt(store, record_id, state=post_ledger.POD_POSTED, doc_no="RCPT-0042"):
    """Put a real receipt in the ledger against this record, report still outstanding.

    Deliberately leaves `status` at `pending`: that is exactly the live shape the status check
    could not see, and flipping the column instead would test a situation that cannot arise.
    """
    conn = state_db.get_connection(store)
    try:
        conn.execute(
            "INSERT INTO spitfire_post (idempotency_key, record_id, po_number, line_number,"
            " pod_md5, state, receipt_doc_no, claimed_at)"
            " VALUES ('key-1', ?, '908491', 300, 'ABC', ?, ?, '2026-08-22 10:00:00')",
            (record_id, state, doc_no))
        conn.commit()
    finally:
        conn.close()


def row(store, record_id):
    conn = state_db.get_connection(store)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM extracted_records WHERE id = ?",
                            (record_id,)).fetchone()
    finally:
        conn.close()


def edits(store, record_id):
    conn = state_db.get_connection(store)
    try:
        return conn.execute("SELECT COUNT(*) FROM record_edits WHERE record_id = ?",
                            (record_id,)).fetchone()[0]
    finally:
        conn.close()


# --- Reachability from the Records page --------------------------------------------------------


def test_every_records_row_offers_a_way_to_correct_it(client, store):
    """The gap this change closes. A reviewer looking at a record could not change it."""
    record_id = add_record(store)
    page = client.get("/ui/records").text
    assert ">Edit<" in page, "the column should exist"
    assert f'href="/ui/records/{record_id}/edit?from=records"' in page


def test_the_edit_control_costs_no_script_and_no_inline_handler(client, store):
    """The page budget is one inline script, and `test_ui_html` counts it across every page. A
    link is what keeps this control inside that budget — and what makes a refusal able to come
    back carrying what was typed."""
    add_record(store)
    page = client.get("/ui/records").text
    assert page.count("<script>") == 1
    assert "onclick=" not in page and 'src="http' not in page


def test_a_row_with_a_receipt_offers_a_reason_instead_of_a_control(client, store):
    """Drawn, disabled, and explaining itself — not silently missing.

    `aria-disabled` rather than the `disabled` attribute: a disabled control is not focusable and
    browsers suppress its tooltip, and here the tooltip carries the entire message. A `<button>`
    rather than a `<span>` because the row-click handler bails on `a,button,input,label`, so a span
    would open the delivery dialog when clicked.
    """
    record_id = add_record(store)
    block_with_a_receipt(store, record_id)
    page = client.get("/ui/records").text
    assert f"/ui/records/{record_id}/edit" not in page
    assert 'aria-disabled="true"' in page
    assert "RCPT-0042" in page, "the tooltip should name the receipt that blocks it"


def test_a_flagged_row_is_still_correctable(client, store):
    """`FLAGGED` is not a reason to withhold the form — it is the reason to offer it. A refusal is
    what an edit exists to fix, and blocking it would close the record's only recovery path."""
    record_id = add_record(store)
    block_with_a_receipt(store, record_id, state=post_ledger.FLAGGED, doc_no="")
    assert f'href="/ui/records/{record_id}/edit?from=records"' in client.get("/ui/records").text
    assert client.get(f"/ui/records/{record_id}/edit?from=records").status_code == 200


# --- The form itself ---------------------------------------------------------------------------


def test_the_form_carries_a_box_for_every_editable_field(client, store):
    """Built from the whitelist, so widening it can never leave a field with no box."""
    record_id = add_record(store)
    page = client.get(f"/ui/records/{record_id}/edit").text
    for name in EDITABLE_FIELDS:
        assert f'id="f-{name}"' in page, f"no box for {name}"
    assert page.count("<script>") == 1


def test_the_two_locked_fields_are_shown_with_their_reasons(client, store):
    """Shown greyed rather than omitted. A form that silently leaves out the two facts a reviewer
    is most likely to want to change reads as an oversight; one that shows them with a reason
    reads as the decision it is."""
    record_id = add_record(store)
    page = client.get(f"/ui/records/{record_id}/edit").text
    assert 'id="f-po_number"' in page and 'id="f-po_line_number"' in page
    assert page.count("fld-ro") >= 2
    assert "different budget line" in page, "PO number should say why it is locked"
    assert "would not suggest a line, it would choose one" in page


def test_a_stored_real_is_not_shown_with_its_trailing_zero(client, store):
    """`41.0` in a box somebody is about to retype is noise, and re-typing it as `41` used to
    register as a correction. Both numeric fields, not only the quantity."""
    record_id = add_record(store)
    page = client.get(f"/ui/records/{record_id}/edit").text
    assert 'id="f-quantity_received" value="11"' in page
    assert 'id="f-package_quantity" value="41"' in page


def test_the_form_shows_what_has_already_been_corrected(client, store):
    """The question `updated_at` could never answer: which field, by whom, and what it used to
    hold. Absent until there is something to show."""
    record_id = add_record(store)
    assert "Already corrected" not in client.get(f"/ui/records/{record_id}/edit").text

    client.post(f"/ui/records/{record_id}/edit", data=dict(FORM, carrier_name="FedEx"),
                follow_redirects=False)
    page = client.get(f"/ui/records/{record_id}/edit").text
    assert "Already corrected" in page
    assert "M Rivera" in page and "Nolan" in page and "FedEx" in page


# --- Saving ------------------------------------------------------------------------------------


def test_a_correction_is_saved_here_and_recorded(client, store):
    record_id = add_record(store)
    response = client.post(f"/ui/records/{record_id}/edit?from=records",
                           data=dict(FORM, carrier_name="FedEx", received_by="A Khan"),
                           follow_redirects=False)
    assert response.status_code == 303
    saved = row(store, record_id)
    assert (saved["carrier_name"], saved["received_by"]) == ("FedEx", "A Khan")
    assert saved["created_by"] == "M Rivera"
    assert edits(store, record_id) == 2


def test_a_refusal_comes_back_carrying_what_was_typed_and_writes_nothing(client, store):
    """The reason this is a page and not a dialog — see `html.form`."""
    record_id = add_record(store)
    response = client.post(f"/ui/records/{record_id}/edit",
                           data=dict(FORM, pod_stated_date="17/08/2026"), follow_redirects=False)
    assert response.status_code == 200
    assert 'class="errors"' in response.text
    assert "17/08/2026" in response.text, "the rejected value should still be in its box"
    assert row(store, record_id)["pod_stated_date"] == "2025-10-01"
    assert edits(store, record_id) == 0


def test_an_unsigned_correction_is_refused(client, store):
    """The name is recorded against the record and against every field, so a hand-typed value is
    never mistaken later for one the machine read."""
    record_id = add_record(store)
    response = client.post(f"/ui/records/{record_id}/edit",
                           data=dict(FORM, carrier_name="FedEx", edited_by=""),
                           follow_redirects=False)
    assert response.status_code == 200 and 'class="errors"' in response.text
    assert row(store, record_id)["carrier_name"] == "Nolan"


def test_a_field_off_the_whitelist_cannot_be_posted_in(client, store):
    """A stale or hand-built form must not reach a column the screen never offered."""
    record_id = add_record(store)
    client.post(f"/ui/records/{record_id}/edit",
                data=dict(FORM, po_number="999999", po_line_number="400",
                          extraction_confidence="0.1", status="pushed_to_spitfire"),
                follow_redirects=False)
    saved = row(store, record_id)
    assert saved["po_number"] == "908491"
    assert saved["po_line_number"] == 300
    assert saved["extraction_confidence"] == 1.0
    assert saved["status"] == "pending"


# --- The ledger block, on both verbs ------------------------------------------------------------


@pytest.mark.parametrize("state", [post_ledger.POD_POSTED, post_ledger.PARTIAL,
                                   post_ledger.CLAIMED])
def test_a_record_the_ledger_blocks_cannot_be_opened_or_saved(client, store, state):
    """The disabled button on the Records page is a courtesy; this is the guarantee. A form left
    open while somebody else posted the row would otherwise still submit."""
    record_id = add_record(store)
    block_with_a_receipt(store, record_id, state=state)

    assert client.get(f"/ui/records/{record_id}/edit").status_code == 404
    response = client.post(f"/ui/records/{record_id}/edit",
                           data=dict(FORM, carrier_name="FedEx"), follow_redirects=False)
    assert response.status_code == 404
    assert row(store, record_id)["carrier_name"] == "Nolan"
    assert edits(store, record_id) == 0


def test_the_refusal_names_the_receipt_rather_than_saying_no_such_record(client, store):
    """"Not found" on a record plainly listed one page earlier is the dash problem again. The page
    says which document exists and what is left to do."""
    record_id = add_record(store)
    block_with_a_receipt(store, record_id)
    page = client.get(f"/ui/records/{record_id}/edit?from=records").text
    assert "RCPT-0042" in page
    assert 'href="/ui/records"' in page, "and it should offer the way back it came from"


# --- Where Cancel goes --------------------------------------------------------------------------


@pytest.mark.parametrize("came_from, destination, label", [
    ("records", "/ui/records", "Records"),
    ("manual", "/ui/manual", "Needs a human"),
    ("", "/ui/manual", "Needs a human"),
])
def test_the_form_returns_where_it_was_opened_from(client, store, came_from, destination, label):
    record_id = add_record(store)
    page = client.get(f"/ui/records/{record_id}/edit?from={came_from}").text
    assert f'href="{destination}"' in page
    assert label in page
    assert f'action="/ui/records/{record_id}/edit?from=' in page, \
        "the next submit has to remember too, or a refusal loses the way back"


def test_a_save_from_records_returns_to_records(client, store):
    """Whether or not the edit closed the last gap. The row they just changed, sitting on the page
    they came from, is the confirmation that it worked."""
    record_id = add_record(store, spec_code=None)     # still incomplete after this edit
    response = client.post(f"/ui/records/{record_id}/edit?from=records",
                           data=dict(FORM, spec_code="", carrier_name="FedEx"),
                           follow_redirects=False)
    assert response.headers["location"] == f"/ui/records?corrected={record_id}"


def test_a_save_from_the_queue_stays_in_the_queue_while_work_remains(client, store):
    record_id = add_record(store, spec_code=None)
    response = client.post(f"/ui/records/{record_id}/edit?from=manual",
                           data=dict(FORM, spec_code="", carrier_name="FedEx"),
                           follow_redirects=False)
    assert response.headers["location"] == "/ui/manual"


def test_a_record_the_queue_completes_moves_to_records(client, store):
    """Unchanged behaviour, asserted because the redirect above now has a second reason to fire and
    the two must not be confused: a completed record has *left* that queue."""
    record_id = add_record(store, spec_code=None)
    response = client.post(f"/ui/records/{record_id}/edit?from=manual",
                           data=dict(FORM, spec_code="STE-402-LT-B"), follow_redirects=False)
    assert response.headers["location"] == f"/ui/records?corrected={record_id}"


def test_the_records_page_says_which_record_was_saved(client, store):
    record_id = add_record(store)
    assert f"Record #{record_id} saved." in client.get(f"/ui/records?corrected={record_id}").text
    assert "saved." not in client.get("/ui/records").text


@pytest.mark.parametrize("hostile", [
    "https://evil.example", "//evil.example", "http://evil.example/ui/records",
    "/api/keys", "..%2f..%2fetc%2fpasswd", "javascript:alert(1)",
])
def test_the_return_parameter_can_never_become_somebody_elses_url(client, store, hostile):
    """A key looked up in a fixed table, never a URL echoed back. Asserted on the `Location`
    header itself, because that is the thing an open redirect would appear in."""
    record_id = add_record(store)
    response = client.post(f"/ui/records/{record_id}/edit?from={hostile}",
                           data=dict(FORM, carrier_name="FedEx"), follow_redirects=False)
    assert response.headers["location"] in ("/ui/manual", f"/ui/records?corrected={record_id}")

    page = client.get(f"/ui/records/{record_id}/edit?from={hostile}").text
    assert "evil.example" not in page and "javascript:" not in page
