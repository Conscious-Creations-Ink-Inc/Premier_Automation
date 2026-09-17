"""The console said everything was fine four different ways while it was not.

Every failure this session rendered as health. A run whose process died showed "running…" for
weeks. A run hung in an untimed token call showed "Running now…" for twelve hours. Fifty test rows
showed as real runs. And a run that read nothing while 24 messages sat unreadable printed
*"No new mail to read — everything in the inbox has already been through the pipeline."*

Then the fix for the last of those introduced a fifth: with the two-second `<meta refresh>` removed,
the page froze mid-run and went on claiming to be working. Run 1085 finished at 17:31:07; the page
still said it was running at 17:49, under copy promising *"this page refreshes itself until it
finishes"*.

These tests hold the line that silence is not health, and that copy describing behaviour has to
match the behaviour.
"""

import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from api.main import app
from operations import runner

_META_REFRESH = re.compile(r"""<meta[^>]+http-equiv=["']?refresh""", re.I)
"""Matches the emitted tag, not the words.

A plain `'http-equiv="refresh"' in body` substring check looked equivalent and was not: the page
carries the whole of `_JS` inline, so a *comment* in that script explaining why the tag was
removed satisfied the check and failed the test. An assertion that a comment can break is an
assertion nobody trusts.
"""


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def running(monkeypatch):
    monkeypatch.setattr(runner, "is_running", lambda: True)


# --- the page must not freeze mid-run -------------------------------------

def test_the_page_says_the_progress_advances_and_that_is_what_it_does(client, running):
    """The copy and the mechanism have to agree.

    This sentence has now outlived two mechanisms, which is the reason the test exists. First the
    meta-refresh went and the page kept promising to "refresh itself until it finishes". Then that
    was corrected to "updates itself once when the run finishes" — true at the time, because the
    only thing left was the ten-second poller reloading on the run's finishing edge — and that in
    turn went stale when the ring started advancing on its own.

    So the assertion is not just that a sentence is present: it is that the page ships the thing the
    sentence promises. A claim about live progress needs the progress block to patch and the
    endpoint to patch it from, or it is the same lie in a third wording.
    """
    body = client.get("/ui/automation").text

    assert "refreshes itself until it finishes" not in body, (
        "the page no longer refreshes on a timer; it must not claim to")
    assert "updates itself once when the run finishes" not in body, (
        "the progress advances continuously now; 'once, at the end' undersells and misleads")
    assert "the progress above advances as it does" in body

    # The mechanism behind the claim.
    assert 'data-prog=' in body, "nothing on the page for the ticker to advance"
    assert "/ui/run-progress" in body, "the page claims live progress but polls nothing for it"
    assert not _META_REFRESH.search(body), "a meta-refresh tag is back on the page"


def test_the_render_tells_the_poller_a_run_is_in_flight(client, running):
    """`data-running` seeds the poller so a finished run is detectable as an *edge*.

    Without it, a page opened during a run and left in a background tab polls nothing while
    hidden — so a run finishing in that window leaves no transition to detect on return, and the
    page keeps claiming to be running.
    """
    assert 'data-running="1"' in client.get("/ui/automation").text


def test_an_idle_page_is_marked_idle(client):
    assert 'data-running="0"' in client.get("/ui/automation").text


def test_the_completion_reload_does_not_depend_on_the_token_changing():
    """The subtle half, and the reason the freeze was not just a missing refresh.

    `live_version_token()` is `boot:newest_email_log_id:count:run_id:arrivals`. `start_run` inserts
    the row — so `run_id` is already in the baseline the page rendered with — and `finish_run` only
    updates it. A run that reads nothing moves none of those parts, so it can begin and end with an
    identical token. A poller waiting for the token to move would never reload.
    """
    from api.ui import html

    js = html._JS
    finish = js.index("justFinished")
    token_guard = js.index("v.token === baseline")

    assert finish < token_guard, (
        "the just-finished branch must be evaluated before the token guard, or a quiet run leaves "
        "the page reading 'Running now...' for ever")
    assert "wasRunning" in js, "completion must be tracked as a transition, not a state"


def test_the_version_endpoint_reports_the_run_state(client, running):
    payload = client.get("/ui/version").json()
    assert payload["running"] is True


# --- a run that read nothing while mail waits is not healthy ---------------

def _status_text(client) -> str:
    return client.get("/ui/automation").text


def test_a_run_that_read_nothing_while_mail_waits_is_reported_as_a_fault(monkeypatch, client):
    """The sentence that hid the bug for days.

    24 messages had arrived and could never be read, and this branch printed that the inbox was
    clear. The count was not new information — `mail_arrivals` minus `email_log` is exactly what
    the Mail page already showed. Nothing consulted it here.
    """
    from api.ui import routes

    monkeypatch.setattr(routes, "_unread_arrivals", lambda: 24)

    class _Run:
        id = 1
        started_at = "2026-09-03 17:30:56"
        finished_at = "2026-09-03 17:31:07"
        trigger, source = "manual", "mailbox"
        emails = records = needs_person = ocr_calls = 0
        elapsed_seconds = 10.98
        error = None
        detail = "{}"

    monkeypatch.setattr(routes.ops_store, "last_run", lambda conn: _Run())

    body = _status_text(client)

    assert "everything in the inbox has already been through the pipeline" not in body, (
        "24 messages are unread; the inbox is not clear")
    assert "Last run read nothing" in body
    assert "24 messages arrived and still has no verdict" in body


def test_an_idle_inbox_is_still_allowed_to_be_good_news(monkeypatch, client):
    """The guard has to stay narrow. A run that found nothing with nothing waiting is the normal,
    healthy outcome, and calling it a fault would train people to ignore the warning."""
    from api.ui import routes

    monkeypatch.setattr(routes, "_unread_arrivals", lambda: 0)

    class _Run:
        id = 1
        started_at = "2026-09-03 17:30:56"
        finished_at = "2026-09-03 17:31:07"
        trigger, source = "manual", "mailbox"
        emails = records = ocr_calls = 0
        needs_person = 3
        elapsed_seconds = 10.98
        error = None
        detail = "{}"

    monkeypatch.setattr(routes.ops_store, "last_run", lambda conn: _Run())

    body = _status_text(client)

    assert "everything in the inbox has already been through the pipeline" in body
    assert "Last run read nothing" not in body
