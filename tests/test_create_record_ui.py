"""The create-a-record screen, driven the way a person drives it.

`test_record_create.py` holds the rules; this holds the screen that reaches them — that a refusal
comes back carrying what was typed, that nothing is written when it does, and that the two entry
points into the form exist at all. The form was the missing half of the whole flow: every other
stage worked, and mail the pipeline could not finish had nowhere to go.
"""

import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from api.ui import html
from config import settings
from pipeline import email_log, mail_cache, state_db

FORM = {
    "email_id": "mail-ui", "created_by": "M Rivera",
    "po_number": "908491", "spec_code": "STE-402-LT-B",
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
    email_log.record(conn, email_id="mail-ui", subject="Delivered - 908491 - 11 EA",
                     sender="routing@example-logistics.test", category="route",
                     matched_rule="rule_7", reason="nothing extractable from the body",
                     folder="Routed", processed_at="2026-08-21 09:00:00", po_hints="908491")
    conn.execute(
        """INSERT INTO attachment_ledger
           (id, email_id, depth, ordinal, filename, sniffed_kind, disposition, is_inline,
            sha256, size_bytes, first_seen_at, is_pod, pod_po_numbers)
           VALUES (11, 'mail-ui', 0, 0, 'signed-bol.jpg', 'image', 'extracted', 0,
                   'sha-bol', 12, 'now', 0, '')""")
    # Through `mail_cache` rather than a bare INSERT into `mail_attachment`, because the form now
    # renders the message itself beside the fields. Without a body to resolve, the panel correctly
    # reports the message as unreadable — which is a real state, but not the one these tests are
    # about. This writes `mail_body` too, so `mail_view.resolve` finds it in the cache and stops.
    mail_cache.cache_mail(
        conn, email_id="mail-ui", subject="Delivered - 908491 - 11 EA",
        sender="routing@example-logistics.test", received_at="2026-08-21 09:00:00",
        body_html="<p>PO 908491 &mdash; 11 EA delivered 01 Oct.</p>", body_text=None,
        source="Outlook (read-only)", cached_at="2026-08-21 09:00:00",
        attachments=[{"ordinal": 0, "filename": "signed-bol.jpg", "content_type": "image/jpeg",
                      "kind": "image", "size_bytes": 12, "is_inline": 0,
                      "content": b"\xff\xd8\xff\xe0 photo"}])
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
    assert 'value="908491"' in client.get("/ui/records/new?email_id=mail-ui").text


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


def test_the_way_out_is_in_the_header_not_only_under_the_last_field(client):
    """This page is gone into from one row of a queue, and the sidebar cannot be the way back out:
    Needs a human is the entry already lit, so it reads as where you are. The form's Cancel goes to
    the same place, but it sits below the last field — four sections down past everything you
    decided not to fill in. Both, and the header one is on screen when you land.

    Also asserted on the no-message branch, which does not build a form at all and would otherwise
    be a page with no exit whatsoever.
    """
    for url in ("/ui/records/new?email_id=mail-ui", "/ui/records/new"):
        bar = re.search(r'<div class="bar">.*?</div>', client.get(url).text, re.S)
        assert bar, f"{url}: no header bar"
        assert 'class="back-link"' in bar.group(0), f"{url}: no way back in the header"
        assert 'href="/ui/manual"' in bar.group(0), f"{url}: the back link goes somewhere else"
        # It reads "Back". The destination is in the tooltip and the accessible name instead:
        # spelled out in the link it competes with the title beside it for the same glance.
        assert "Back</a>" in bar.group(0), f"{url}: the link no longer reads Back"
        assert 'aria-label="Back to Needs a human"' in bar.group(0), \
            f"{url}: nothing says where it goes for a reader who cannot see the tooltip"

    # A real link first. The script may take a history step *instead*, because that hands back the
    # queue exactly as it was left — but only when it can show the entry behind this page is that
    # queue. The hazard this rule was written for is still here: a refusal is a POST landing on this
    # same URL, so a blind step back is the form again rather than the queue. What makes it safe is
    # that the step is gated on `entryBefore()`, the recorded previous entry, and not on a guess.
    assert "history.back()" in html._JS
    step = html._JS[html._JS.index("a[data-back-to]"):]
    step = step[:step.index("history.back()")]
    assert "entryBefore()" in step, "the step back is not checked against the previous entry"
    assert "data-back-to" in client.get("/ui/records/new?email_id=mail-ui").text

    # And it stays a per-page choice — nothing else grew one by accident.
    assert 'class="back-link"' not in client.get("/ui/manual").text


# --- the message beside the form ----------------------------------------------------------------
# Five of the six fields can only be answered from the email and the file attached to it. The page
# used to show neither: one button opened the message in the popup, on top of the fields it was
# meant to fill, so filling the form meant opening and closing it once per number.


def test_the_message_is_on_the_page_not_behind_a_button(client):
    """Rendered server-side into the panel, not fetched into it. No flash on first paint, no extra
    round trip, and the message is readable even if the fetch path is broken."""
    page = client.get("/ui/records/new?email_id=mail-ui").text

    assert 'class="split"' in page, "the form and the message are not laid out side by side"
    assert 'class="pane-body"' in page and "data-frag-host" in page
    assert 'iframe class="mail-body"' in page, "the panel arrived empty — nothing rendered into it"
    assert "signed-bol.jpg" in page, "the panel does not list the attachment"
    # The rule this page is most likely to break, re-asserted here because the panel is new markup.
    assert page.count("<script>") == 1
    assert 'src="http' not in page


def test_the_panel_asks_for_the_message_without_its_create_control(client):
    """`bare=1` is not cosmetic. `/ui/mail` normally carries a Create control, and on this page that
    is a link to `/ui/records/new?email_id=…` — the page you are already on. Following it reloads
    the form and silently discards everything typed into it."""
    page = client.get("/ui/records/new?email_id=mail-ui").text
    host = re.search(r'data-frag-host="([^"]+)"', page)

    assert host, "the panel has no host URL, so nothing can render back into it"
    assert "bare=1" in host.group(1)

    bare = client.get("/ui/mail?id=mail-ui&src=inbox&bare=1").text
    assert "/ui/records/new" not in bare, "the bare fragment still offers to create a record"
    assert 'iframe class="mail-body"' in bare, "bare dropped the message along with the control"
    # Without it, the control is still there — the flag is doing the work, not a coincidence.
    assert "/ui/records/new" in client.get("/ui/mail?id=mail-ui&src=inbox").text


def test_choosing_the_proof_shows_it_beside_the_fields_it_fills(client):
    """The POD chooser's Open button sits in the form column, outside the panel, so `_JS` cannot
    work out from its position where it should render — it has to name the target. Deciding which
    file is the proof is the one moment the file and these radios most need to be on screen at
    once, and it used to put the file in the popup covering them."""
    page = client.get("/ui/records/new?email_id=mail-ui").text

    opener = re.search(r'<button[^>]*data-frag="[^"]*attachment/view[^"]*"[^>]*>', page)
    assert opener, "the chooser no longer offers to open the file"
    assert 'data-frag-into="mail-pane"' in opener.group(0)
    assert 'id="mail-pane"' in page, "the named target does not exist on the page"
    assert "data-frag-into" in html._JS, "nothing listens for data-frag-into"


def test_a_refusal_comes_back_with_the_message_still_beside_it(client, store):
    """The refusal path re-renders through the same builder, so this holds automatically — which is
    the point of asserting it. Losing the message on the one screen where you are being told to go
    and re-read it would be the worst moment for it to disappear."""
    refused = client.post("/ui/records/new", data=dict(FORM, spec_code=""))

    assert 'class="errors"' in refused.text
    assert 'class="pane-body"' in refused.text and 'iframe class="mail-body"' in refused.text


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

def test_a_complete_form_creates_a_record_and_lands_back_on_the_message(client, store):
    """Back on the message, not on to Records.

    One notification commonly lists several lines of one delivery, and landing on Records meant
    recording the second line began by finding the message again — on a queue the first line does
    not remove it from. The page it returns to confirms the write and offers the ways to continue.
    """
    created = client.post("/ui/records/new", data=FORM, follow_redirects=False)

    # 303, so the browser follows with a GET: a refresh must not re-submit the form and stage the
    # delivery twice.
    assert created.status_code == 303
    location = created.headers["location"]
    assert location.startswith("/ui/records/new?email_id=mail-ui")
    assert "after=1" in location, "it names the record it just wrote"

    (row,) = records(store)
    assert row["origin"] == "manual"
    assert row["created_by"] == "M Rivera"
    assert row["pod_ledger_id"] == 11


def test_the_records_page_says_the_record_was_made_by_hand(client):
    client.post("/ui/records/new", data=FORM)

    assert "Manual · M Rivera" in client.get("/ui/records").text


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
    """How a reviewer sees why nothing else was staged from a message they marked handled.

    It used to report this *instead of* offering a second record, on the reasoning that one
    delivery needs one record. True of a delivery, false of a message — see the test below.
    """
    client.post("/ui/records/new", data=FORM)
    popup = client.get("/ui/mail?id=mail-ui&src=inbox").text

    assert "created from this message by hand" in popup
    assert "M Rivera" in popup


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

    answer = client.post("/ui/records/1/waive-pod", data={"by": "M Rivera"},
                         follow_redirects=False)

    assert answer.status_code == 303
    assert records(store)[0]["pod_waived_by"] == "M Rivera"


def test_once_accepted_the_row_offers_a_post_that_does_not_claim_a_proof(client, store):
    """"Post POD" on a record with no POD would be a lie about what reaches Spitfire."""
    _an_automatically_staged_record_with_no_pod(client, store)
    client.post("/ui/records/1/waive-pod", data={"by": "M Rivera"})
    page = client.get("/ui/records").text

    assert "Post receipt" in page
    assert "no proof" in page


def test_accepting_twice_does_not_change_who_accepted(client, store):
    """The first name is the one that took the risk."""
    _an_automatically_staged_record_with_no_pod(client, store)
    client.post("/ui/records/1/waive-pod", data={"by": "M Rivera"})

    second = client.get("/ui/records/1/waive-pod").text
    assert "Already accepted" in second and "M Rivera" in second

    client.post("/ui/records/1/waive-pod", data={"by": "Somebody Else"})
    assert records(store)[0]["pod_waived_by"] == "M Rivera"


def test_the_kill_switch_stops_a_waiver_too(client, store, monkeypatch):
    from operations import killswitch

    _an_automatically_staged_record_with_no_pod(client, store)

    monkeypatch.setattr(killswitch, "is_stopped", lambda: True)
    answer = client.post("/ui/records/1/waive-pod", data={"by": "M Rivera"})

    assert "kill switch" in answer.text
    assert records(store)[0]["pod_waived_by"] is None


# --- the second line ----------------------------------------------------------------------------

def test_the_page_confirms_the_record_it_just_wrote(client):
    """`?created=` used to be passed to the Records page, which declares no such parameter and so
    ignored it. Nothing anywhere confirmed the write."""
    landing = client.post("/ui/records/new", data=FORM).text

    assert "Record #1 created from this message" in landing


def test_it_offers_another_line_of_the_same_po_and_a_different_one(client):
    """The two shapes the work takes, as two controls. A single "create another" would have to
    decide between them as the person typed, clearing fields under them when they edited the PO."""
    landing = client.post("/ui/records/new", data=FORM).text

    assert "Add another line to PO 908491" in landing
    assert "same_po=1" in landing
    assert "Record a different PO from this message" in landing


def test_the_way_out_of_the_loop_is_on_the_page(client):
    """Without it this page is a loop with no stated end, and someone who has finished has to reach
    for the browser's back button to say so."""
    landing = client.post("/ui/records/new", data=FORM).text

    assert "Done — back to Needs a human" in landing


def test_another_line_carries_the_delivery_and_blanks_the_line(client):
    """The purchase order, the delivery date and the unit are shared by every line of one delivery.
    The spec, the description and the quantity are the whole of what makes it a second line —
    offering the previous line's values for those invites a duplicate of the row just written."""
    client.post("/ui/records/new", data=FORM)
    form = client.get("/ui/records/new?email_id=mail-ui&after=1&same_po=1").text

    assert re.search(r'name="po_number"[^>]*value="908491"', form)
    assert re.search(r'name="pod_stated_date"[^>]*value="2025-10-01"', form)
    for blanked in ("spec_code", "item_description", "quantity_received"):
        assert not re.search(rf'name="{blanked}"[^>]*value="[^"]+"', form), \
            f"{blanked} must not be carried onto the next line"


def test_a_different_po_starts_from_the_message_not_from_the_last_line(client):
    """Without `same_po` the form is the one it always was — opened on what the pipeline worked out
    about the message, which for a different delivery is the only honest starting point."""
    client.post("/ui/records/new", data=FORM)
    form = client.get("/ui/records/new?email_id=mail-ui").text

    assert "Record #" not in form, "no confirmation banner without `after`"
    assert "Recorded from this message" in form, "but it still says what has been made"


def test_a_message_already_recorded_from_says_so_on_the_queue(client):
    """"Create a record" on a message already carrying two of them reads as though the first two
    did not happen."""
    before = client.get("/ui/manual").text
    assert "Create a record" in before

    client.post("/ui/records/new", data=FORM)
    after = client.get("/ui/manual").text

    assert "Create another record" in after


def test_the_message_popup_offers_the_second_line_too(client):
    """Where somebody works out that a notification listed more than one line: reading it."""
    client.post("/ui/records/new", data=FORM)
    popup = client.get("/ui/mail?id=mail-ui&src=inbox").text

    assert "Add another line to PO 908491" in popup
    assert "Different PO" in popup


def test_a_second_line_of_the_same_delivery_is_accepted(client, store):
    """The whole point. Same PO, same delivery date, a different item on it — and the duplicate
    guard must not mistake that for the same delivery recorded twice."""
    client.post("/ui/records/new", data=FORM)
    second = client.post("/ui/records/new", data=dict(
        FORM, spec_code="STE-402-LT-C", item_description="SHADE, Floor Lamp 2",
        quantity_received="4"))

    assert "already recorded" not in second.text
    rows = records(store)
    assert len(rows) == 2
    assert {r["spec_code"] for r in rows} == {"STE-402-LT-B", "STE-402-LT-C"}
    assert {r["po_number"] for r in rows} == {"908491"}, "both lines are the same purchase order"
