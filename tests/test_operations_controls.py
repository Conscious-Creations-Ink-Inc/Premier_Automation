"""The controls an operator actually presses: Save, Run now, Stop.

Grouped in one file because they are one story — Save always 500'd, the schedule treated saving as
a trigger, the lock could leak, and there was no way to stop anything. Each of those is a separate
regression below.

These controls moved from the standalone console on port 8500 onto `/ui/automation` when the three
UIs were collapsed into one. The logic did not change; only the routes did.
"""

import sqlite3
import threading
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from operations import killswitch, runner, scheduler, store


@pytest.fixture
def console_db(tmp_path, monkeypatch):
    """A throwaway operations database. `get_connection` takes a path, so redirecting the module
    constant keeps every caller — routes, scheduler, killswitch — on the same temporary file."""
    path = tmp_path / "console.sqlite3"
    monkeypatch.setattr(store, "CONSOLE_DB_PATH", path)
    monkeypatch.setattr(store.get_connection, "__defaults__", (path,))
    killswitch.reset_for_tests()
    yield path
    killswitch.reset_for_tests()


# --------------------------------------------------------------------- Save --

def test_saving_the_schedule_does_not_500(console_db):
    """The regression that started this: `await request.form()` raised AssertionError because
    python-multipart is absent, so every Save returned 500 and no setting was ever written."""
    from api.main import app

    with TestClient(app) as client:
        response = client.post(
            "/ui/automation/schedule",
            data={"enabled": "1", "interval_minutes": "20", "source": "mailbox"},
            follow_redirects=False,
        )
    assert response.status_code == 303, response.text

    conn = store.get_connection()
    try:
        saved = store.get_schedule(conn)
    finally:
        conn.close()
    assert saved.enabled is True
    assert saved.source == "mailbox"
    assert saved.interval_minutes == 20


def test_python_multipart_stays_out_of_requirements():
    """Parsing the body by hand is the fix; re-adding the library would also enable file-upload
    parsing app-wide, in an app whose whole guarantee is that it only reads."""
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text("utf-8")
    assert "multipart" not in text.lower()


# ----------------------------------------------------------------- Schedule --

def test_saving_is_not_a_trigger(console_db):
    """Enabling a schedule used to fire a live Graph run within five seconds, because
    `next_run_at` returned "now" whenever there was no prior run."""
    now = datetime(2026, 8, 8, 12, 0, 0)
    conn = store.get_connection()
    try:
        store.set_schedule(
            conn,
            store.Schedule(enabled=True, interval_minutes=20, source="sample"),
            now=now.strftime("%Y-%m-%d %H:%M:%S"),
        )
    finally:
        conn.close()

    due = scheduler.next_run_at()
    assert due == now + timedelta(minutes=20)
    # Sanity: anchored to the save, not to the epoch. Compared against the fixture's own `now` —
    # comparing against the wall clock made this a time bomb that passed only on the day it was
    # written, because `now` here is a fixed date in the past.
    assert due > now - timedelta(days=1)


def test_a_stale_run_does_not_make_a_fresh_schedule_due(console_db):
    """Counting from the last run alone would let a run from days ago make a just-saved schedule
    instantly due — the same bug from the other direction."""
    conn = store.get_connection()
    try:
        store.start_run(conn, started_at="2026-08-01 09:00:00", trigger="manual", source="sample")
        saved_at = datetime(2026, 8, 8, 12, 0, 0)
        store.set_schedule(
            conn,
            store.Schedule(enabled=True, interval_minutes=20, source="sample"),
            now=saved_at.strftime("%Y-%m-%d %H:%M:%S"),
        )
    finally:
        conn.close()

    assert scheduler.next_run_at() == saved_at + timedelta(minutes=20)


def test_ensure_anchor_never_moves_an_existing_anchor(console_db):
    conn = store.get_connection()
    try:
        store.ensure_anchor(conn, "2026-08-08 12:00:00")
        store.ensure_anchor(conn, "2026-08-09 18:00:00")   # a later restart
        assert store.get_schedule(conn).anchor_at == "2026-08-08 12:00:00"
    finally:
        conn.close()


# --------------------------------------------------------------------- Lock --

def test_a_failure_before_the_run_does_not_leak_the_lock(console_db, monkeypatch):
    """`store.get_connection()` and `start_run()` used to sit between the lock acquire and the
    try block. A locked database there stranded the lock forever, while `is_running()` still
    reported idle — the page looked fine and silently did nothing, permanently."""
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "start_run", boom)
    outcome = runner.run(trigger="manual", source="sample")

    assert outcome.ok is False
    assert "database is locked" in (outcome.error or "")
    assert runner.is_running() is False
    acquired = runner._LOCK.acquire(blocking=False)
    assert acquired, "the lock leaked"
    runner._LOCK.release()


def test_an_unknown_source_is_refused_without_leaking_the_lock(console_db):
    outcome = runner.run(trigger="manual", source="not-a-store")
    assert outcome.ok is False
    assert "unknown store" in (outcome.error or "")
    assert runner._LOCK.acquire(blocking=False)
    runner._LOCK.release()


# -------------------------------------------------------------- Kill switch --

def test_engaged_switch_refuses_to_start_a_run(console_db):
    conn = store.get_connection()
    try:
        killswitch.engage(conn, "2026-08-08 12:00:00")
    finally:
        conn.close()

    outcome = runner.run(trigger="manual", source="sample")
    assert outcome.skipped is True
    assert "stopped" in (outcome.error or "").lower()
    assert runner._LOCK.acquire(blocking=False), "a refused run must not hold the lock"
    runner._LOCK.release()


def test_the_switch_survives_a_restart(console_db):
    """A stop that forgets when the process bounces is not a stop."""
    conn = store.get_connection()
    try:
        killswitch.engage(conn, "2026-08-08 12:00:00")
    finally:
        conn.close()

    killswitch.reset_for_tests()          # simulate a fresh process
    assert killswitch.is_stopped() is False

    conn = store.get_connection()
    try:
        killswitch.load(conn)             # what app startup does
    finally:
        conn.close()
    assert killswitch.is_stopped() is True
    assert killswitch.stopped_since() == "2026-08-08 12:00:00"


def test_release_clears_it_everywhere(console_db):
    conn = store.get_connection()
    try:
        killswitch.engage(conn, "2026-08-08 12:00:00")
        killswitch.release(conn)
        killswitch.reset_for_tests()
        killswitch.load(conn)
    finally:
        conn.close()
    assert killswitch.is_stopped() is False
    assert killswitch.stopped_since() is None


def test_the_scheduler_does_not_tick_while_stopped(console_db, monkeypatch):
    """The switch is checked before the schedule is even consulted."""
    called = []
    monkeypatch.setattr(runner, "run", lambda **kw: called.append(kw))
    monkeypatch.setattr(scheduler, "_due", lambda: True)

    conn = store.get_connection()
    try:
        killswitch.engage(conn, "2026-08-08 12:00:00")
    finally:
        conn.close()

    stop = threading.Event()
    monkeypatch.setattr(scheduler, "_stop", stop)
    monkeypatch.setattr(scheduler, "_TICK_SECONDS", 0.01)
    thread = threading.Thread(target=scheduler._loop, daemon=True)
    thread.start()
    stop.wait(0.1)
    stop.set()
    thread.join(timeout=2)

    assert called == [], "the scheduler ran while the kill switch was engaged"


# ------------------------------------------------ Kill switch, through the UI --

def test_the_stop_control_is_on_every_page_and_the_banner_follows_it(console_db):
    """The switch is in the sidebar, so it is reachable from wherever an operator happens to be —
    and the banner has to appear everywhere too, or a page opened before the stop keeps looking
    like a page where things still run."""
    from api.main import app

    with TestClient(app) as client:
        for path in ("/ui/mails", "/ui/records", "/ui/po", "/ui/report"):
            body = client.get(path).text
            assert 'action="/ui/automation/stop"' in body, path
            assert '<div class="stopped-banner">' not in body, path

        assert client.post("/ui/automation/stop", follow_redirects=False).status_code == 303

        for path in ("/ui/mails", "/ui/records", "/ui/po", "/ui/report"):
            body = client.get(path).text
            assert '<div class="stopped-banner">' in body, path
            assert 'action="/ui/automation/resume"' in body, path
            assert 'action="/ui/automation/stop"' not in body, path

        assert client.post("/ui/automation/resume", follow_redirects=False).status_code == 303
        assert '<div class="stopped-banner">' not in client.get("/ui/mails").text


def test_stopping_refuses_a_run_rather_than_only_redrawing(console_db):
    """The banner is cosmetic; this is the part that matters. Asserted in-process because the
    switch keeps in-memory state alongside the row — checking it from another process would load
    a fresh module that has not read the database and would wrongly report the run allowed."""
    from api.main import app

    with TestClient(app) as client:
        client.post("/ui/automation/stop", follow_redirects=False)
        outcome = runner.run(trigger="manual", source="sample")
        assert outcome.skipped is True
        assert "stopped" in (outcome.error or "").lower()
        client.post("/ui/automation/resume", follow_redirects=False)


@pytest.mark.parametrize("referer, expected", [
    ("http://testserver/ui/report", "/ui/report"),          # ours: honoured
    ("", "/ui/automation"),                                 # absent: default
    ("https://evil.example/ui/report", "/ui/automation"),   # another origin's choice of our path
    ("http://testserver//evil.example/", "/ui/automation"),  # protocol-relative Location
    ("http://testserver/api/admin/reset", "/ui/automation"),  # outside /ui
])
def test_stop_returns_only_to_a_page_it_can_trust(console_db, referer, expected):
    """`Referer` is attacker-influenced. It decides where the operator lands after pressing Stop,
    so it is validated rather than echoed."""
    from api.main import app

    with TestClient(app) as client:
        response = client.post("/ui/automation/resume",
                               headers={"Referer": referer} if referer else {},
                               follow_redirects=False)
        assert response.headers["location"] == expected


def test_the_orchestrator_stops_between_emails():
    """Cancellation lands on a committed boundary, never mid-email: the loop checks before
    starting the next message, so nothing is left half-settled."""
    from pipeline import ingest_orchestrator

    conn = None
    try:
        conn = __import__("pipeline.state_db", fromlist=["x"]).get_connection(":memory:")
        staged = ingest_orchestrator.process_new_mail(
            mailbox=_EmptyMailbox(), conn=conn, should_stop=lambda: True,
        )
    finally:
        if conn is not None:
            conn.close()
    assert staged == 0


class _EmptyMailbox:
    def fetch_new(self, skip_ids=None, since=None):
        return []

    def mark_processed(self, *args, **kwargs):     # pragma: no cover - never reached
        raise AssertionError("this must never move mail")


# ------------------------------------------------------- Background running --

def test_starting_a_run_returns_before_the_run_finishes(console_db, monkeypatch):
    """The button used to hold the browser for the whole pass — about thirty seconds against the
    live mailbox — and only then redirect. `is_running()` and the "Running now…" banner it drives
    existed the whole time and could never be reached, because the only thing that started a run
    blocked until the flag was back to False."""
    started = threading.Event()
    release = threading.Event()

    def slow(trigger, source):
        started.set()
        release.wait(timeout=5)
        return runner.RunOutcome(ok=True)

    monkeypatch.setattr(runner, "_run_locked", slow)

    outcome = runner.start_background(trigger="manual", source="sample")
    try:
        assert outcome.ok is True
        assert started.wait(timeout=5), "the run never started"
        # The point of the whole change: still going, and we are already back.
        assert runner.is_running() is True
        assert runner.running_since() is not None
    finally:
        release.set()

    for _ in range(50):
        if not runner.is_running():
            break
        time.sleep(0.1)
    assert runner.is_running() is False, "the flag never cleared"


def test_a_second_press_while_running_is_refused_and_does_not_strand_the_flag(console_db, monkeypatch):
    """`start_background` sets the running flag itself, before spawning, so the redirect cannot
    outrun it. That means the early `skipped` return — the one path `run()` takes without entering
    its own try/finally — has to clear the flag, or one refused press disables the button for the
    life of the process."""
    release = threading.Event()

    def slow(trigger, source):
        release.wait(timeout=5)
        return runner.RunOutcome(ok=True)

    monkeypatch.setattr(runner, "_run_locked", slow)
    first = runner.start_background(trigger="manual", source="sample")
    try:
        assert first.ok is True
        second = runner.start_background(trigger="manual", source="sample")
        assert second.ok is False and second.skipped is True
        assert "already in progress" in (second.error or "")
        assert runner.is_running() is True, "the refusal cleared the running run's flag"
    finally:
        release.set()

    for _ in range(50):
        if not runner.is_running():
            break
        time.sleep(0.1)
    assert runner.is_running() is False


def test_an_engaged_kill_switch_refuses_to_start_a_background_run(console_db):
    conn = store.get_connection()
    try:
        killswitch.engage(conn, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        outcome = runner.start_background(trigger="manual", source="sample")
        assert outcome.ok is False and outcome.skipped is True
        assert runner.is_running() is False, "a refused start must not look like a running one"
    finally:
        killswitch.release(conn)
        conn.close()


def test_two_simultaneous_presses_start_exactly_one_run(console_db, monkeypatch):
    """The race the sequential test cannot reach.

    `start_background` used to read `is_running()` and set the flag as two separate steps. FastAPI
    serves sync endpoints on a threadpool, so two clicks a millisecond apart both saw "idle" and
    both spawned. One won `_LOCK` and ran; the other came back `skipped` and cleared `_STATE` out
    from under it — so a run in progress reported itself idle, the banner vanished and the page
    stopped refreshing. Which is precisely what "I press the button and nothing happens" looks like.
    """
    started = threading.Barrier(3, timeout=5)
    release = threading.Event()
    runs = []

    def slow(trigger, source):
        runs.append(trigger)
        release.wait(timeout=5)
        return runner.RunOutcome(ok=True)

    monkeypatch.setattr(runner, "_run_locked", slow)

    outcomes = []

    def press():
        started.wait()
        outcomes.append(runner.start_background(trigger="manual", source="sample"))

    threads = [threading.Thread(target=press) for _ in range(2)]
    for t in threads:
        t.start()
    started.wait()
    for t in threads:
        t.join(timeout=5)

    try:
        assert sorted(o.ok for o in outcomes) == [False, True], "exactly one press must win"
        assert runner.is_running() is True, "the winning run's flag was cleared by the loser"
    finally:
        release.set()

    for _ in range(50):
        if not runner.is_running():
            break
        time.sleep(0.1)
    assert runner.is_running() is False
    assert len(runs) == 1, f"the run body executed {len(runs)} times, expected once"
