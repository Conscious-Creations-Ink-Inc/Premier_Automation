"""The create-a-record screen, driven the way a person drives it.

`test_record_create.py` holds the rules; this holds the screen that reaches them — that a refusal
comes back carrying what was typed, that nothing is written when it does, and that the two entry
points into the form exist at all. The form was the missing half of the whole flow: every other
stage worked, and mail the pipeline could not finish had nowhere to go.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from config import settings
from pipeline import email_log, state_db

FORM = {
    "email_id": "mail-ui", "created_by": "M Gutierrez",
    "po_number": "208491", "spec_code": "STE-402-LT-B",
    "item_description": "BASE, Floor Lamp 2", "quantity_received": "11",
    "unit_of_measure": "", "pod_stated_date": "2025-10-01",
    "pod_ledger_id": "11", "note": "Property confirmed by phone.",
}


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A throwaway pipeline store the whole app points at.

    Overriding `deps.get_pipeline_conn` alone is not enough: the async POST handlers open their own
    connection through `deps.pipeline_connection`, for the threading reason its docstring gives.
    Pointing the setting is what makes both paths agree.
    """
    path = tmp_path / "pipeline_state.sqlite3"
    monkeypatch.setattr(settings, "PIPELINE_STATE_DB_PATH", path)

    conn = state_db.get_connection(path)
    email_log.record(conn, email_id="mail-ui", subject="Delivered - 208491 - 11 EA",
                     sender="routing@authoritylogistics.com", category="route",
                     matched_rule="rule_7", reason="nothing extractable from the body",
                     folder="Routed", processed_at="2026-08-21 09:00:00", po_hints="208491")
    conn.execute(
        """INSERT INTO attachment_ledger
           (id, email_id, depth, ordinal, filename, sniffed_kind, disposition, is_inline,
            sha256, size_bytes, first_seen_at, is_pod, pod_po_numbers)
           VALUES (11, 'mail-ui', 0, 0, 'signed-bol.jpg', 'image', 'extracted', 0,
                   'sha-bol', 12, 'now', 0, '')""")
    conn.execute(
        """INSERT INTO mail_attachment
           (email_id, ordinal, filename, content_type, kind, size_bytes, is_inline, content)
           VALUES ('mail-ui', 0, 'signed-bol.jpg', 'image/jpeg', 'image', 12, 0, ?)""",
        (b"\xff\xd8\xff\xe0 photo",))
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def client(store):
    from api.main import app

    return TestClient(app)


def records(store):
    conn = state_db.get_connection(store)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM extracted_records ORDER BY id").fetchall()
    finally:
        conn.close()


# --- the form -----------------------------------------------------------------------------------

def test_the_form_opens_on_a_message(client):
    page = client.get("/ui/records/new?email_id=mail-ui")

    assert page.status_code == 200
    assert 'action="/ui/records/new"' in page.text


def test_it_opens_pre_filled_rather_than_blank(client):
    """Making somebody retype what is already on file is slower and a fresh chance to get it
    wrong. Here the purchase order came from triage, which found it in the subject even though
    extraction produced no record at all."""
    assert 'value="208491"' in client.get("/ui/records/new?email_id=mail-ui").text


def test_the_chooser_offers_the_photograph(client):
    """The case the chooser exists for. `_pod_for` re-reads PDFs only — OCR is a paid call that
    belongs at ingest — so a photographed delivery note is invisible to it until a person points at
    one."""
    assert "signed-bol.jpg" in client.get("/ui/records/new?email_id=mail-ui").text


def test_the_form_page_keeps_the_one_script_rule(client):
    """`test_ui_html` holds this for the pages in its own list; this page is new and long and is
    exactly where a second block would get added."""
    page = client.get("/ui/records/new?email_id=mail-ui").text

    assert page.count("<script>") == 1
    assert 'src="http' not in page


def test_opening_it_on_no_message_explains_rather_than_500s(client):
    page = client.get("/ui/records/new")

    assert page.status_code == 200
    assert "always built from" in page.text


def test_opening_it_on_a_message_that_does_not_exist_is_a_404(client):
    assert client.get("/ui/records/new?email_id=nope").status_code == 404


# --- refusals -----------------------------------------------------------------------------------

def test_a_refusal_names_the_fields_and_keeps_what_was_typed(client, store):
    """Both halves matter. A refusal that discards the form makes somebody retype six fields to fix
    one, and "2 errors" tells them only that something is wrong."""
    refused = client.post("/ui/records/new",
                          data=dict(FORM, spec_code="", pod_stated_date=""))

    assert "spec code" in refused.text and "POD date" in refused.text
    assert 'value="BASE, Floor Lamp 2"' in refused.text
    assert records(store) == []


def test_deciding_nothing_about_the_proof_is_refused(client, store):
    refused = client.post("/ui/records/new", data=dict(FORM, pod_ledger_id=""))

    assert "proof of delivery" in refused.text
    assert records(store) == []


def test_the_kill_switch_stops_a_record_being_staged(client, store, monkeypatch):
    """Nothing here reaches Spitfire, but a stop means the system is not to act — and staging a
    record that the next press of Post would send is acting."""
    from operations import killswitch

    monkeypatch.setattr(killswitch, "is_stopped", lambda: True)
    refused = client.post("/ui/records/new", data=FORM)

    assert "kill switch" in refused.text
    assert records(store) == []


# --- the happy path -----------------------------------------------------------------------------

def test_a_complete_form_creates_a_record_and_lands_on_records(client, store):
    created = client.post("/ui/records/new", data=FORM, follow_redirects=False)

    # 303, so the browser follows with a GET: a refresh on the Records page must not re-submit the
    # form and stage the delivery twice.
    assert created.status_code == 303
    assert created.headers["location"].startswith("/ui/records")

    (row,) = records(store)
    assert row["origin"] == "manual"
    assert row["created_by"] == "M Gutierrez"
    assert row["pod_ledger_id"] == 11


def test_the_records_page_says_the_record_was_made_by_hand(client):
    client.post("/ui/records/new", data=FORM)

    assert "Manual · M Gutierrez" in client.get("/ui/records").text


def test_the_same_delivery_cannot_be_recorded_twice(client, store):
    client.post("/ui/records/new", data=FORM)
    again = client.post("/ui/records/new", data=FORM)

    assert "already recorded" in again.text
    assert len(records(store)) == 1


# --- the way in ---------------------------------------------------------------------------------

def test_the_queue_offers_a_way_out_of_itself(client):
    """Every row on that page is work the pipeline could not finish. Until this existed the page
    could only say so — a person could read the message and had nowhere to put what they learned."""
    page = client.get("/ui/manual").text

    assert "Create a record" in page
    assert "/ui/records/new?email_id=" in page


def test_the_message_itself_offers_it_too(client):
    """Where somebody actually works out that the pipeline missed something: reading the mail, not
    scanning the queue that listed it."""
    popup = client.get("/ui/mail?id=mail-ui&src=inbox").text

    assert "/ui/records/new?email_id=mail-ui" in popup


def test_the_message_reports_back_what_was_made_from_it(client):
    """Rather than inviting a second record for one delivery — and it is how a reviewer sees why
    nothing else was staged from a message they marked handled."""
    client.post("/ui/records/new", data=FORM)
    popup = client.get("/ui/mail?id=mail-ui&src=inbox").text

    assert "created from this message by hand" in popup
    assert "M Gutierrez" in popup


# --- waiving the proof of delivery --------------------------------------------------------------

def _an_automatically_staged_record_with_no_pod(client, store):
    """What extraction produces from an Inbound notification that attaches nothing."""
    client.post("/ui/records/new", data=dict(FORM, pod_ledger_id="none"))
    conn = state_db.get_connection(store)
    conn.execute("UPDATE extracted_records SET origin='auto', pod_waived_by=NULL, "
                 "pod_ledger_id=NULL WHERE id=1")
    conn.commit()
    conn.close()


def test_the_records_page_offers_a_way_out_of_no_pod(client, store):
    """It was a dead end: the cell read "no POD" and nothing on the page could act on it, so every
    delivery stated in an email body was permanently stuck."""
    _an_automatically_staged_record_with_no_pod(client, store)
    page = client.get("/ui/records").text

    assert "Accept anyway" in page
    assert "/ui/records/1/waive-pod" in page


def test_the_page_says_what_accepting_means_before_asking(client, store):
    """A decision with a consequence in Premier's ERP is explained before it is taken, and signed.
    A modal with one button invites the reflex press this must not have."""
    _an_automatically_staged_record_with_no_pod(client, store)
    page = client.get("/ui/records/1/waive-pod").text

    assert "no proof document attached" in page
    assert 'name="by"' in page


def test_accepting_needs_a_name(client, store):
    _an_automatically_staged_record_with_no_pod(client, store)
    refused = client.post("/ui/records/1/waive-pod", data={"by": "  "})

    assert "name" in refused.text
    assert records(store)[0]["pod_waived_by"] is None


def test_an_automatic_record_can_be_accepted_without_a_pod(client, store):
    """The other half of the body-only path: extraction stages these from Inbound notifications
    that attach nothing, and `post_decision` refuses every one."""
    _an_automatically_staged_record_with_no_pod(client, store)

    answer = client.post("/ui/records/1/waive-pod", data={"by": "M Gutierrez"},
                         follow_redirects=False)

    assert answer.status_code == 303
    assert records(store)[0]["pod_waived_by"] == "M Gutierrez"


def test_once_accepted_the_row_offers_a_post_that_does_not_claim_a_proof(client, store):
    """"Post POD" on a record with no POD would be a lie about what reaches Spitfire."""
    _an_automatically_staged_record_with_no_pod(client, store)
    client.post("/ui/records/1/waive-pod", data={"by": "M Gutierrez"})
    page = client.get("/ui/records").text

    assert "Post receipt" in page
    assert "no proof" in page


def test_accepting_twice_does_not_change_who_accepted(client, store):
    """The first name is the one that took the risk."""
    _an_automatically_staged_record_with_no_pod(client, store)
    client.post("/ui/records/1/waive-pod", data={"by": "M Gutierrez"})

    second = client.get("/ui/records/1/waive-pod").text
    assert "Already accepted" in second and "M Gutierrez" in second

    client.post("/ui/records/1/waive-pod", data={"by": "Somebody Else"})
    assert records(store)[0]["pod_waived_by"] == "M Gutierrez"


def test_the_kill_switch_stops_a_waiver_too(client, store, monkeypatch):
    from operations import killswitch

    _an_automatically_staged_record_with_no_pod(client, store)

    monkeypatch.setattr(killswitch, "is_stopped", lambda: True)
    answer = client.post("/ui/records/1/waive-pod", data={"by": "M Gutierrez"})

    assert "kill switch" in answer.text
    assert records(store)[0]["pod_waived_by"] is None
