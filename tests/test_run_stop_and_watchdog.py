"""A run that can be stopped, and an automation that no run can hold for ever.

On 2026-09-15 run 1179 sat on "Reading the mailbox" for over twelve minutes. Three things let it,
each tested here because each alone was enough:

  1. the mailbox read was one uninterruptible stretch     -> a stop check before every Graph call
  2. Graph's `Retry-After` was obeyed with no upper bound  -> a cap
  3. the scheduler ran the automation on its own thread,   -> runs go on their own thread, and a
     so a blocked run froze the new-mail watch and left       watchdog on the scheduler's abandons
     nothing awake to notice                                  a run that has gone silent

And the control that was missing altogether: a way to stop the run in progress without engaging
the kill switch, which also pauses the schedule.
"""

import threading
import time

import pytest

from config import settings
from connectors import mailbox as mailbox_module
from operations import killswitch, runner, scheduler, store
from tests.test_graph_mailbox import FakeRequests, FakeResponse, graph, message  # noqa: F401


@pytest.fixture
def console_db(tmp_path, monkeypatch):
    path = tmp_path / "console.sqlite3"
    monkeypatch.setattr(store, "CONSOLE_DB_PATH", path)
    monkeypatch.setattr(store.get_connection, "__defaults__", (path,))
    killswitch.reset_for_tests()
    runner.reset_for_tests()
    yield path
    killswitch.reset_for_tests()
    runner.reset_for_tests()


def _wait_for(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# --- 1. the read that could not be interrupted ---------------------------------------------------

def _three_messages():
    return FakeRequests({
        "/mailFolders/": FakeResponse({"value": [
            message(id="m1", internetMessageId="<one@x>"),
            message(id="m2", internetMessageId="<two@x>"),
            message(id="m3", internetMessageId="<three@x>"),
        ]}),
    })


def test_a_stop_reaches_the_mailbox_read_between_messages(graph, monkeypatch):
    """The read used to download every message and attachment before the run's own stop check was
    ever reached. Now it asks before each Graph call, and returns what it already has."""
    monkeypatch.setattr(mailbox_module, "_SESSION", _three_messages())
    box = graph(folders=("inbox",))

    read = []
    box.on_activity = lambda note: read.append(note)
    box.should_stop = lambda: len([n for n in read if n != "listing inbox"]) >= 2

    emails = box.fetch_new()

    assert [e.email_id for e in emails] == ["<one@x>"], (
        "the stop must land before the next message is fetched, not after the whole mailbox")


def test_a_stop_is_not_swallowed_as_a_malformed_message(graph, monkeypatch):
    """Each message is fetched inside a catch-all, so one bad message cannot abort a poll. A stop
    raised from inside a message's attachment download must not be logged as a skip there and have
    the read carry straight on to the next message."""
    monkeypatch.setattr(mailbox_module, "_SESSION", _three_messages())
    box = graph(folders=("inbox",))

    fetched = []

    def to_raw(msg, headers, source_folder=""):
        fetched.append(msg["id"])
        raise mailbox_module.StopRequested()

    monkeypatch.setattr(box, "_to_raw_email", to_raw)

    assert box.fetch_new() == []
    assert fetched == ["m1"], "after a stop, no further message may be fetched"


def test_every_graph_call_while_reading_is_reported_as_activity(graph, monkeypatch):
    """The watchdog measures silence off these. A read that is slow but alive must keep them
    coming, or a busy mailbox would be abandoned for being busy."""
    monkeypatch.setattr(mailbox_module, "_SESSION", _three_messages())
    box = graph(folders=("inbox",))
    seen = []
    box.on_activity = seen.append

    box.fetch_new()

    assert seen[0] == "listing inbox"
    assert len(seen) == 4, f"one listing call and one per message, got {seen}"


def test_a_mailbox_with_no_hooks_behaves_exactly_as_before(graph, monkeypatch):
    """The arrival watch, the tools and every existing test build a mailbox and never set these."""
    monkeypatch.setattr(mailbox_module, "_SESSION", _three_messages())
    assert len(graph(folders=("inbox",)).fetch_new()) == 3


# --- 2. the sleep nobody could reach -------------------------------------------------------------

def test_a_throttled_graph_call_cannot_sleep_past_the_cap():
    """urllib3 obeys `Retry-After` as given. One response naming an hour held a run inside a single
    call for an hour."""
    class _Response:
        headers = {"Retry-After": "3600"}

    retry = mailbox_module._capped_retry_class()(total=3, respect_retry_after_header=True)
    assert retry.get_retry_after(_Response()) == float(settings.GRAPH_RETRY_AFTER_CAP_SECONDS)


def test_the_graph_session_is_built_on_the_capped_retry():
    """The cap is worth nothing unless it is what the session actually mounts."""
    session = mailbox_module._graph_session()
    retry = session.get_adapter("https://graph.microsoft.com").max_retries
    assert type(retry).__name__ == "CappedRetry"


# --- 3. "Stop this run" ------------------------------------------------------------------------------

class _Summary:
    emails_processed = records_staged = ocr_calls = 0
    orphans = []
    msg_files = 0
    folders = []


def test_stop_this_run_reaches_the_ingest_pass_without_the_kill_switch(console_db, monkeypatch):
    """A separate thing from the sidebar's Stop automation, and it has to read differently in
    history: nobody paused the schedule."""
    import tools.ingest_corpus as ingest_corpus

    monkeypatch.setattr(runner, "_needs_person", lambda db_path: 0)
    polled = {}

    def fake(**kwargs):
        assert runner.request_stop() is True
        polled["answer"] = kwargs["should_stop"]()
        return _Summary()

    monkeypatch.setattr(ingest_corpus, "run_once", fake)

    outcome = runner.run("manual", store.SOURCE_SAMPLE)

    assert polled["answer"] is True, "the ingest pass must be told to stop"
    assert killswitch.is_stopped() is False, "stopping one run must not pause the automation"
    assert outcome.error and "Automation page" in outcome.error
    assert "operator" not in outcome.error, "that wording belongs to the kill switch"


def test_there_is_nothing_to_stop_when_idle(console_db):
    assert runner.request_stop() is False


def test_a_new_run_is_not_born_already_stopped(console_db, monkeypatch):
    """The cancel flag belongs to one run. A stop pressed for the last one must not end the next."""
    import tools.ingest_corpus as ingest_corpus

    monkeypatch.setattr(runner, "_needs_person", lambda db_path: 0)
    answers = []

    def fake(**kwargs):
        answers.append(kwargs["should_stop"]())
        if len(answers) == 1:
            runner.request_stop()
        return _Summary()

    monkeypatch.setattr(ingest_corpus, "run_once", fake)

    runner.run("manual", store.SOURCE_SAMPLE)
    second = runner.run("manual", store.SOURCE_SAMPLE)

    assert answers == [False, False]
    assert second.error is None


# --- 4. the watchdog ---------------------------------------------------------------------------------

def _hung_run(monkeypatch):
    """A run blocked inside one call, the way 1179 was. Returns the Event that lets it wake up."""
    import tools.ingest_corpus as ingest_corpus

    monkeypatch.setattr(runner, "_needs_person", lambda db_path: 0)
    wake = threading.Event()

    def hung(**kwargs):
        wake.wait(timeout=10)
        return _Summary()

    monkeypatch.setattr(ingest_corpus, "run_once", hung)
    return wake


def _open_run_row():
    conn = store.get_connection()
    try:
        return conn.execute("SELECT id, finished_at, error FROM runs ORDER BY id DESC").fetchone()
    finally:
        conn.close()


def test_a_silent_run_is_abandoned_and_the_automation_is_freed(console_db, monkeypatch):
    wake = _hung_run(monkeypatch)
    assert runner.start_background("scheduled", store.SOURCE_SAMPLE).ok
    assert _wait_for(lambda: runner._STATE.get("run_id") is not None), "the run never started"

    # Healthy so far: nothing to reap while it is still talking.
    assert runner.reap_if_stuck() is None

    monkeypatch.setattr(settings, "RUN_STALL_MINUTES", 0)
    runner._HEARTBEAT["at"] = time.monotonic() - 5
    try:
        reason = runner.reap_if_stuck()
        assert reason and "abandoned" in reason and "no activity" in reason
        assert runner.is_running() is False
        assert runner._LOCK.locked() is False, "the lock must be free for the next run"

        row = _open_run_row()
        assert row[1] is not None and "abandoned" in row[2], (
            "history must say the run was abandoned, not show it running for ever")
    finally:
        wake.set()


def test_an_abandoned_run_that_wakes_up_cannot_trample_its_replacement(console_db, monkeypatch):
    """The fence. The abandoned thread is still in its call; when it comes back it must not release
    a lock that now belongs to another run, clear that run's banner, or rewrite its own history row
    into a clean finish that hides the hang."""
    wake = _hung_run(monkeypatch)
    assert runner.start_background("scheduled", store.SOURCE_SAMPLE).ok
    assert _wait_for(lambda: runner._STATE.get("run_id") is not None)
    abandoned_id = runner._STATE["run_id"]

    monkeypatch.setattr(settings, "RUN_STALL_MINUTES", 0)
    runner._HEARTBEAT["at"] = time.monotonic() - 5
    assert runner.reap_if_stuck()

    # A replacement starts and holds the lock...
    replacement = threading.Event()
    import tools.ingest_corpus as ingest_corpus
    monkeypatch.setattr(ingest_corpus, "run_once",
                        lambda **kw: (replacement.wait(timeout=10), _Summary())[1])
    assert runner.start_background("manual", store.SOURCE_SAMPLE).ok
    assert _wait_for(lambda: runner._STATE.get("run_id") not in (None, abandoned_id))

    # ...then the abandoned one wakes up and finishes.
    wake.set()
    time.sleep(0.5)
    try:
        assert runner.is_running() is True, "the zombie cleared the replacement's running flag"
        assert runner._LOCK.locked() is True, "the zombie released the replacement's lock"

        conn = store.get_connection()
        try:
            error = conn.execute("SELECT error FROM runs WHERE id = ?", (abandoned_id,)).fetchone()[0]
        finally:
            conn.close()
        assert error and "abandoned" in error, "the zombie overwrote its abandoned history row"
    finally:
        replacement.set()
    assert _wait_for(lambda: not runner.is_running())


def test_a_stop_that_is_not_honoured_is_abandoned_after_the_grace(console_db, monkeypatch):
    """The button cannot be a request somebody watches fail."""
    wake = _hung_run(monkeypatch)
    assert runner.start_background("manual", store.SOURCE_SAMPLE).ok
    assert _wait_for(lambda: runner._STATE.get("run_id") is not None)

    assert runner.request_stop() is True
    assert runner.reap_if_stuck() is None, "a fresh stop gets its grace period"

    monkeypatch.setattr(settings, "RUN_STOP_GRACE_SECONDS", 0)
    runner._HEARTBEAT["stop_requested_at"] = time.monotonic() - 5
    try:
        reason = runner.reap_if_stuck()
        assert reason and "Automation page" in reason and "abandoned" in reason
        assert runner.is_running() is False
    finally:
        wake.set()


def test_a_run_abandoned_before_it_took_the_lock_does_not_start(console_db, monkeypatch):
    """Requested, then written off before its thread got going: starting after that would be a run
    nobody is tracking, under a generation the watchdog has already given up on."""
    generation = runner._GENERATION[0]
    runner._GENERATION[0] += 1            # the reaper ran in between
    outcome = runner.run("scheduled", store.SOURCE_SAMPLE, generation)
    assert outcome.skipped is True
    assert runner._LOCK.locked() is False


# --- 5. the scheduler stays awake --------------------------------------------------------------------

def _one_tick(monkeypatch):
    class _OnceThenStop:
        def __init__(self):
            self.calls = 0

        def wait(self, seconds):
            self.calls += 1
            return self.calls > 1

    monkeypatch.setattr(scheduler, "_stop", _OnceThenStop())


def test_the_scheduler_starts_runs_on_their_own_thread(console_db, monkeypatch):
    """`runner.run` on the scheduler thread is what froze the new-mail watch behind run 1179."""
    calls = []
    monkeypatch.setattr(runner, "run", lambda *a, **k: calls.append("run"))
    monkeypatch.setattr(runner, "start_background",
                        lambda *a, **k: calls.append("start_background"))
    monkeypatch.setattr(runner, "reap_if_stuck", lambda: calls.append("reap"))
    monkeypatch.setattr(scheduler, "_tick_arrivals", lambda: None)
    monkeypatch.setattr(scheduler, "_due", lambda: True)
    monkeypatch.setattr(scheduler, "_source", lambda: store.SOURCE_SAMPLE)
    _one_tick(monkeypatch)

    scheduler._loop()

    assert "run" not in calls
    assert calls == ["reap", "start_background"]


def test_the_watchdog_still_runs_while_the_kill_switch_is_engaged(console_db, monkeypatch):
    """A stuck run is exactly when somebody pulls the switch."""
    calls = []
    monkeypatch.setattr(runner, "reap_if_stuck", lambda: calls.append("reap"))
    monkeypatch.setattr(runner, "start_background", lambda *a, **k: calls.append("start"))
    monkeypatch.setattr(scheduler, "_tick_arrivals", lambda: calls.append("arrivals"))
    killswitch._event.set()
    _one_tick(monkeypatch)

    scheduler._loop()

    assert calls == ["reap"]


# --- 6. the control on the page ----------------------------------------------------------------------

def _stop_button(page: str) -> str:
    import re
    found = re.search(r'<span data-run-stop="1">.*?</span>\s*</form>|<span data-run-stop="1">.*?</form>',
                      page, re.S)
    assert found, "the Stop this run control is missing from the page"
    return found.group(0)


def test_the_stop_button_is_always_on_the_page_and_only_pressable_during_a_run(console_db, monkeypatch):
    """It used to be hidden while idle, and the first question asked about it was where it was."""
    from fastapi.testclient import TestClient

    from api.main import app

    client = TestClient(app)
    idle = _stop_button(client.get("/ui/automation").text)
    assert "Stop this run" in idle
    assert "disabled" in idle, "with nothing running it is shown, but cannot be pressed"

    monkeypatch.setattr(runner, "is_running", lambda: True)
    busy = _stop_button(client.get("/ui/automation").text)
    assert "disabled" not in busy


def test_pressing_stop_this_run_asks_the_runner_and_returns_to_automation(console_db, monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app

    pressed = []
    monkeypatch.setattr(runner, "request_stop", lambda: pressed.append(True) or True)

    response = TestClient(app).post("/ui/automation/run/stop", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/automation"
    assert pressed == [True]
    assert killswitch.is_stopped() is False


def test_the_progress_feed_says_how_long_the_run_has_been_silent(console_db, monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app

    monkeypatch.setattr(runner, "is_running", lambda: True)
    monkeypatch.setattr(runner, "seconds_since_activity", lambda: 42.7)
    monkeypatch.setattr(runner, "stop_requested", lambda: True)

    feed = TestClient(app).get("/ui/run-progress").json()

    assert feed["silent_seconds"] == 42
    assert feed["stop_requested"] is True
