"""Overruling triage about what a message is, driven the way a person drives it.

`test_mail_overrides.py` holds the rules and `test_read_views.py` holds what the two queues do with
them. This holds the screen between: that the decision has to be signed, that nothing is written
when it is refused, and — the point of the whole feature — that a message put back on the queue
lands there with the existing Create-a-record form on its row.

`/ui/not-deliveries` is deliberately absent from the page-wide sweeps in `test_ui_html.py`. Those
render against a store that holds no set-aside mail, and `html.table` emits a bare
`<p class="empty">` with no headers when a table has no rows — so the properties they assert (a date
range bound to a date column, a search box pointing at a table that exists, every message control
naming its store) are asserted here instead, against a store that has a row.
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from config import settings
from operations import killswitch
from pipeline import email_log, mail_overrides, state_db

SIGNED = {"email_id": "mail-hidden", "to": "delivery", "by": "M Rivera",
          "note": "the signed POD is attached"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A throwaway pipeline store the whole app points at.

    Overriding `deps.get_pipeline_conn` alone is not enough: the async POST handler opens its own
    connection through `deps.pipeline_connection`, for the threading reason its docstring gives.
    Pointing the setting is what makes both paths agree.
    """
    path = tmp_path / "pipeline_state.sqlite3"
    monkeypatch.setattr(settings, "PIPELINE_STATE_DB_PATH", path)

    conn = state_db.get_connection(path)
    # Set aside by a rule: HIDE, which is what every rule in `NOT_A_DELIVERY_RULES` files.
    email_log.record(conn, email_id="mail-hidden", subject="Re: 908491 site logistics",
                     sender="crystal@example-pm.test", category="hide",
                     matched_rule="rule_5d_no_delivery_claim",
                     reason="no hop in this thread states goods arrived", folder="Hidden",
                     processed_at="2026-08-21 09:00:00", po_hints="908491", not_a_delivery=True)
    # On the queue, for the other direction.
    email_log.record(conn, email_id="mail-routed", subject="Weekly project check ins",
                     sender="crystal@example-pm.test", category="route", matched_rule="rule_7",
                     reason="nothing extractable from the body", folder="Routed",
                     processed_at="2026-08-22 09:00:00")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def client(store):
    from api.main import app

    return TestClient(app)


def overrides(store):
    conn = state_db.get_connection(store)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM mail_overrides ORDER BY email_id").fetchall()
    finally:
        conn.close()


# --- the form -----------------------------------------------------------------------------------

def test_the_form_opens_on_a_message(client):
    page = client.get("/ui/mail/verdict?id=mail-hidden&to=delivery")

    assert page.status_code == 200
    assert 'action="/ui/mail/verdict"' in page.text
    assert "Re: 908491 site logistics" in page.text
    assert "rule_5d_no_delivery_claim" in page.text


def test_it_opens_in_the_other_direction_too(client):
    page = client.get("/ui/mail/verdict?id=mail-routed&to=not_delivery")

    assert page.status_code == 200
    assert "Set aside as not a delivery" in page.text


def test_an_unknown_message_is_a_404(client):
    assert client.get("/ui/mail/verdict?id=nobody&to=delivery").status_code == 404


def test_an_unknown_verdict_is_a_400(client):
    """It comes off the query string and decides every word on the page — refused, never guessed."""
    assert client.get("/ui/mail/verdict?id=mail-hidden&to=maybe").status_code == 400


def test_the_form_page_keeps_the_one_script_rule(client):
    page = client.get("/ui/mail/verdict?id=mail-hidden&to=delivery").text

    assert page.count("<script>") == 1
    assert 'src="http' not in page


# --- writing the verdict ------------------------------------------------------------------------

def test_it_refuses_an_unsigned_verdict(client, store):
    posted = client.post("/ui/mail/verdict", data={**SIGNED, "by": "   "})

    assert posted.status_code == 200
    assert "has to be signed" in posted.text
    assert "the signed POD is attached" in posted.text, "what was typed comes back with the refusal"
    assert overrides(store) == []


def test_the_post_refuses_an_unknown_message(client, store):
    assert client.post("/ui/mail/verdict",
                       data={**SIGNED, "email_id": "nobody"}).status_code == 404
    assert overrides(store) == []


def test_the_post_refuses_an_unknown_verdict(client, store):
    assert client.post("/ui/mail/verdict", data={**SIGNED, "to": "maybe"}).status_code == 400
    assert overrides(store) == []


def test_setting_a_message_aside_takes_it_off_the_queue(client):
    posted = client.post("/ui/mail/verdict", follow_redirects=False,
                         data={"email_id": "mail-routed", "to": "not_delivery",
                              "by": "M Rivera", "note": "internal chatter"})

    assert posted.status_code == 303
    # The page the message moved *to*, not the one it came from. Redirecting back to the queue put
    # people on an unchanged-looking page with their search cleared, and a verdict that had been
    # recorded correctly read as a dead button.
    assert posted.headers["location"] == "/ui/not-deliveries"
    assert "Weekly project check ins" not in client.get("/ui/manual").text
    assert "Weekly project check ins" in client.get("/ui/not-deliveries").text


def test_calling_it_a_delivery_puts_it_back_with_a_way_to_record_it(client):
    """The whole feature in one assertion. Nothing is extracted from a reclassified message — the
    existing create form is the exit, and the row has to carry the way to it."""
    posted = client.post("/ui/mail/verdict", follow_redirects=False, data=SIGNED)

    assert posted.status_code == 303
    assert posted.headers["location"] == "/ui/not-deliveries"

    queue = client.get("/ui/manual").text
    assert "Re: 908491 site logistics" in queue
    assert "Set aside in error" in queue
    assert "M Rivera" in queue
    assert "/ui/records/new?email_id=mail-hidden" in queue


def test_pressing_it_twice_is_harmless(client, store):
    for _ in range(2):
        client.post("/ui/mail/verdict", data=SIGNED)

    rows = overrides(store)
    assert len(rows) == 1
    assert rows[0]["decided_by"] == "M Rivera"


def test_the_verdict_survives_the_kill_switch(client, store, monkeypatch):
    """Deliberately unguarded, unlike the create and waive routes. This writes one row nothing in
    the pipeline reads; a stop is exactly when somebody is working out which rule went wrong, and
    blocking the control that records it would block the diagnosis."""
    monkeypatch.setattr(killswitch, "is_stopped", lambda: True)

    client.post("/ui/mail/verdict", data=SIGNED)

    assert len(overrides(store)) == 1


def test_a_get_on_the_write_route_is_not_the_write(client, store):
    """The form and the write share a path and differ only by method — the GET must never write."""
    client.get("/ui/mail/verdict?id=mail-hidden&to=delivery")

    assert overrides(store) == []


# --- the pages the verdict moves messages between -----------------------------------------------

def test_the_queue_offers_the_way_out_of_it(client):
    queue = client.get("/ui/manual").text

    assert "Not a delivery" in queue
    assert "/ui/mail/verdict?id=mail-routed&amp;to=not_delivery" in queue


def test_the_manual_page_points_at_the_new_page_instead_of_listing_it(client):
    queue = client.get("/ui/manual").text

    assert 'href="/ui/not-deliveries"' in queue
    assert 'id="manual-filtered"' not in queue


def test_the_new_page_lists_what_a_rule_set_aside_and_what_a_person_did(client):
    client.post("/ui/mail/verdict", data={"email_id": "mail-routed", "to": "not_delivery",
                                          "by": "M Rivera", "note": "internal chatter"})

    page = client.get("/ui/not-deliveries").text

    assert "Re: 908491 site logistics" in page          # the rule's
    assert "rule_5d_no_delivery_claim" in page
    assert "Weekly project check ins" in page           # the person's
    assert "internal chatter" in page
    assert "M Rivera" in page


def test_the_new_page_is_paged_searchable_and_date_bound(client):
    """The page-wide properties `test_ui_html` asserts for every other page. This one cannot join
    that sweep — its stores hold no set-aside mail, and an empty table renders no headers at all."""
    page = client.get("/ui/not-deliveries").text

    assert 'id="not-deliveries-table"' in page
    assert 'data-pager-for="not-deliveries-table"' in page
    assert 'data-filter="not-deliveries-table"' in page
    assert 'data-from="not-deliveries-table"' in page
    assert 'data-to="not-deliveries-table"' in page
    assert 'data-date="1"' in page, "the When column is what the date range binds to"


def test_every_control_on_the_new_page_that_opens_a_message_names_its_store(client):
    page = client.get("/ui/not-deliveries").text

    for fragment in page.split('data-frag="')[1:]:
        url = fragment.split('"')[0]
        if url.startswith("/ui/mail/verdict"):
            # Not a message being opened — the reclassify form, rendered for the popup. It acts on
            # `mail_overrides`, which is a live-store table and has no corpus counterpart, so there
            # is no store for it to name.
            assert "inline=1" in url
            continue
        assert "src=" in url


def test_the_new_page_keeps_the_one_script_rule(client):
    page = client.get("/ui/not-deliveries").text

    assert page.count("<script>") == 1
    assert 'src="http' not in page


# --- the pre-written reasons --------------------------------------------------------------------
#
# The box was there from the start and people were typing the same four sentences into it. These
# assert the shortcut exists and, more importantly, that it stayed a shortcut: the textarea is still
# the only thing submitted, so a reason nobody anticipated is still just typed.

def test_the_form_offers_the_common_reasons_as_chips(client):
    page = client.get("/ui/mail/verdict?id=mail-routed&to=not_delivery").text

    assert 'data-preset-for="f-note"' in page
    for phrase in ("no PO reference or delivery data anywhere in the thread",
                   "marketing or advertising mail",
                   "internal correspondence, not a delivery notification",
                   "an automated scheduled report, not a delivery notification"):
        assert phrase in page


def test_the_two_directions_offer_different_reasons(client):
    """"Advertising" is not a reason a message *is* a delivery, and offering it there would be a
    menu that has to be read past rather than one that can be pressed."""
    aside = client.get("/ui/mail/verdict?id=mail-routed&to=not_delivery").text
    back = client.get("/ui/mail/verdict?id=mail-hidden&to=delivery").text

    assert "marketing or advertising mail" in aside
    assert "marketing or advertising mail" not in back
    assert "the signed proof of delivery is attached to this message" in back
    assert "the signed proof of delivery is attached to this message" not in aside


def test_a_pressed_reason_is_stored_exactly_as_the_chip_writes_it(client, store):
    """The chips fill the textarea and the textarea is what posts, so the server has no idea a chip
    was involved — which is the property that keeps a typed reason a first-class one."""
    client.post("/ui/mail/verdict",
                data={"email_id": "mail-routed", "to": "not_delivery", "by": "M Rivera",
                      "note": "marketing or advertising mail; no PO reference or delivery data "
                              "anywhere in the thread"})

    assert overrides(store)[0]["note"] == ("marketing or advertising mail; no PO reference or "
                                           "delivery data anywhere in the thread")


def test_a_typed_reason_still_works(client, store):
    """No allowlist anywhere: the presets are a keyboard shortcut, not a vocabulary."""
    client.post("/ui/mail/verdict",
                data={"email_id": "mail-routed", "to": "not_delivery", "by": "M Rivera",
                      "note": "Crystal says this thread was closed out in July"})

    assert overrides(store)[0]["note"] == "Crystal says this thread was closed out in July"


def test_the_chips_do_not_light_the_sidebar(client):
    """`sel`, never `on` — that string is counted across the whole document to check exactly one
    nav entry is active."""
    page = client.get("/ui/mail/verdict?id=mail-routed&to=not_delivery").text

    assert page.count('class="on"') == 1


def test_the_presets_added_no_second_script(client):
    page = client.get("/ui/mail/verdict?id=mail-routed&to=not_delivery").text

    assert page.count("<script>") == 1
    assert "onclick=" not in page


# --- the control inside the message dialog ------------------------------------------------------
#
# Reading the message is where somebody settles which of the two this is, so both ways out are
# offered there. The direction is read off the message rather than off the page the dialog was
# opened over — a fragment is fetched by script from anywhere and cannot know where it was opened.

def test_the_message_dialog_offers_both_ways_out(client):
    frag = client.get("/ui/mail?id=mail-routed&src=inbox").text

    assert "Create a record" in frag
    assert "Not a delivery" in frag
    assert "/ui/mail/verdict?id=mail-routed&amp;to=not_delivery" in frag


def test_the_dialog_on_a_set_aside_message_offers_the_other_direction(client):
    """Opened from Not deliveries, "Not a delivery" would be a button that does nothing. It offers
    the way back instead."""
    frag = client.get("/ui/mail?id=mail-hidden&src=inbox").text

    assert "This is a delivery" in frag
    assert "to=delivery" in frag
    assert "Not a delivery" not in frag


def test_the_dialog_follows_the_verdict_rather_than_the_page(client):
    """The same message, the same URL, before and after a person overrules the rules — the control
    turns round because the message changed, not because a different page asked."""
    before = client.get("/ui/mail?id=mail-routed&src=inbox").text
    assert "Not a delivery" in before

    client.post("/ui/mail/verdict", data={"email_id": "mail-routed", "to": "not_delivery",
                                          "by": "M Rivera", "note": "marketing mail"})

    after = client.get("/ui/mail?id=mail-routed&src=inbox").text
    assert "This is a delivery" in after
    assert "Not a delivery" not in after


def test_the_create_form_pane_still_drops_both_controls(client):
    """`bare=1` exists because that header's Create link points at the page you are already on and
    following it discards everything typed. The verdict link would navigate away from a half-filled
    form for the same reason, so it goes with it."""
    pane = client.get("/ui/mail?id=mail-routed&src=inbox&bare=1").text

    assert "Create a record" not in pane
    assert "/ui/mail/verdict" not in pane


# --- landing back where the person was standing ---------------------------------------------------
# A verdict pressed from a filtered queue used to land on the other page, so the filter, the table
# page and the scroll were gone and the row they were working through had to be found again. The
# page now says where it came from, in a field the script fills and the server checks.


def test_a_verdict_lands_back_on_the_page_it_was_pressed_from(client):
    answer = client.post("/ui/mail/verdict",
                         data=dict(SIGNED, return_to="/ui/manual?all=1"),
                         follow_redirects=False)

    assert answer.status_code == 303
    assert answer.headers["location"] == "/ui/manual?all=1"


def test_without_one_it_lands_where_it_always_did(client):
    """`SIGNED` puts a set-aside message back, so its own landing is the page it just left."""
    answer = client.post("/ui/mail/verdict", data=SIGNED, follow_redirects=False)

    assert answer.status_code == 303
    assert answer.headers["location"] == "/ui/not-deliveries"


@pytest.mark.parametrize("hostile", [
    "//evil.test/ui/manual",          # a browser reads this as another site entirely
    "https://evil.test/ui/manual",
    "/api/records",                   # ours, but not a page
    "/ui/../admin",
    "javascript:alert(1)",
    "",
])
def test_it_never_redirects_anywhere_a_form_merely_asked_for(client, hostile):
    """The field arrives in a POST body, so it is attacker-influenceable exactly as `Referer` is.
    Same three rules, and a refusal falls back to the route's own page rather than failing the
    decision — the verdict is recorded either way."""
    answer = client.post("/ui/mail/verdict", data=dict(SIGNED, return_to=hostile),
                         follow_redirects=False)

    assert answer.status_code == 303
    assert answer.headers["location"] == "/ui/not-deliveries"


def test_the_verdict_is_still_recorded_when_the_return_is_refused(client, store):
    client.post("/ui/mail/verdict", data=dict(SIGNED, return_to="https://evil.test/"),
                follow_redirects=False)

    conn = sqlite3.connect(store)
    try:
        standing = mail_overrides.get(conn, "mail-hidden")
    finally:
        conn.close()
    assert standing is not None and standing.verdict == "delivery"


def test_the_form_carries_the_field_for_the_script_to_fill(client):
    body = client.get("/ui/mail/verdict?id=mail-routed&to=not_delivery").text

    assert 'name="return_to"' in body
    assert 'value=""' in body, "it must arrive empty — the script, not the server, names the page"


# --- deciding without leaving the queue -----------------------------------------------------------
# The same decision, taken in the popup over the page the reader is already on. The rows it retires
# disappear from the table in place, so the filter, the page and the scroll survive it — and the
# rest of the conversation can go with it in the same press, which is what the one-message-at-a-time
# version could not do: 786 sibling messages were still putting 1,308 rows on the queue.


@pytest.fixture
def thread(store):
    """Three messages of one conversation, plus one unrelated message with the same words."""
    conn = state_db.get_connection(store)
    for email_id, subject, hints in [
        ("thread-1", "Cameo Public Space delivery confirmation", "212749"),
        ("thread-2", "RE: Cameo Public Space delivery confirmation", "212749"),
        ("thread-3", "Fwd: FW: Cameo Public Space delivery confirmation", "212749"),
        ("other-1", "Sofitel lobby rugs delivery confirmation", "999001"),
    ]:
        email_log.record(conn, email_id=email_id, subject=subject, sender="v@example-mill.test",
                         category="surface", matched_rule="rule_1a", reason="names a delivery",
                         folder="Processed", processed_at="2026-09-01 09:00:00", po_hints=hints)
    conn.commit()
    conn.close()
    return store


def test_the_form_can_be_asked_for_as_a_fragment_to_open_in_the_popup(client, thread):
    body = client.get("/ui/mail/verdict?id=thread-1&to=not_delivery&inline=1").text

    assert "<script" not in body, "a fragment carries no script of its own"
    assert "<body" not in body and "sidebar" not in body, "it is a fragment, not a page"
    assert 'name="by"' in body and "Set aside" in body


def test_the_fragment_offers_the_rest_of_the_conversation_ticked(client, thread):
    body = client.get("/ui/mail/verdict?id=thread-1&to=not_delivery&inline=1").text

    assert "thread-2" in body and "thread-3" in body
    assert "other-1" not in body, "a different conversation that shares a word is not this one"
    assert body.count("checked") >= 2, "the other messages start ticked"


def test_deciding_answers_the_script_with_what_it_retired(client, thread):
    answer = client.post("/ui/mail/verdict",
                         data={"email_id": "thread-1", "to": "not_delivery", "by": "M Rivera",
                               "note": "an expediting report", "also": ["thread-2", "thread-3"]},
                         headers={"Accept": "application/json"})

    assert answer.status_code == 200
    body = answer.json()
    assert body["ok"] is True
    assert sorted(body["email_ids"]) == ["thread-1", "thread-2", "thread-3"]


def test_the_whole_conversation_is_set_aside_by_one_press(client, thread):
    client.post("/ui/mail/verdict",
                data={"email_id": "thread-1", "to": "not_delivery", "by": "M Rivera",
                      "note": "an expediting report", "also": ["thread-2", "thread-3"]},
                headers={"Accept": "application/json"})

    conn = sqlite3.connect(thread)
    try:
        for email_id in ("thread-1", "thread-2", "thread-3"):
            standing = mail_overrides.get(conn, email_id)
            assert standing is not None and standing.verdict == "not_delivery", email_id
            assert standing.decided_by == "M Rivera"
        assert mail_overrides.get(conn, "other-1") is None
    finally:
        conn.close()


def test_a_message_that_is_not_in_this_conversation_is_refused(client, thread):
    """The ids arrive in a form body. Membership is worked out again on the server, so a crafted
    post cannot set aside mail the page never offered."""
    answer = client.post("/ui/mail/verdict",
                         data={"email_id": "thread-1", "to": "not_delivery", "by": "M Rivera",
                               "also": ["other-1", "mail-routed"]},
                         headers={"Accept": "application/json"})

    assert answer.json()["email_ids"] == ["thread-1"]
    conn = sqlite3.connect(thread)
    try:
        assert mail_overrides.get(conn, "other-1") is None
        assert mail_overrides.get(conn, "mail-routed") is None
    finally:
        conn.close()


def test_an_unsigned_decision_is_refused_in_the_popup_too(client, thread):
    answer = client.post("/ui/mail/verdict",
                         data={"email_id": "thread-1", "to": "not_delivery", "by": ""},
                         headers={"Accept": "application/json"})

    assert answer.status_code == 200
    assert answer.json()["ok"] is False
    assert "name" in answer.json()["problem"].lower()
    conn = sqlite3.connect(thread)
    try:
        assert mail_overrides.get(conn, "thread-1") is None
    finally:
        conn.close()


def test_the_ordinary_form_post_still_redirects_exactly_as_before(client, thread):
    answer = client.post("/ui/mail/verdict",
                         data={"email_id": "thread-1", "to": "not_delivery", "by": "M Rivera"},
                         follow_redirects=False)

    assert answer.status_code == 303
    assert answer.headers["location"] == "/ui/not-deliveries"
