"""The automation that ran for twelve hours, and the four things that let it.

On 2026-09-03 the console showed a run started at 15:54 still going at 16:16, and the history
behind it was a wall of the same thing: run 1064 held the lock from 02:15 to 14:16, run 923 from
02:11 one day to 14:10 the next — twelve hours and thirty-six. None of them were busy. All of them
were blocked in the Graph token call, which was the one HTTP call in the connector with no timeout,
and each held `runner._LOCK` throughout, so every scheduled run behind it was turned away with "a
run is already in progress".

Four independent defects, each tested here because each alone was enough:

  1. the token call could block for ever                   -> a timeout
  2. nothing bounded a run's wall clock                    -> a ceiling, polled where the kill
                                                              switch is
  3. nothing closed out rows left open by a killed process -> reconciliation at startup
  4. the test suite wrote production run history           -> the console DB is redirected
"""

import re
from pathlib import Path

from config import settings
from operations import runner, store

_META_REFRESH = re.compile(r"""<meta[^>]+http-equiv=["']?refresh""", re.I)
"""Matches the emitted tag, not the words.

A plain `'http-equiv="refresh"' in body` substring check looked equivalent and was not: the page
carries the whole of `_JS` inline, so a *comment* in that script explaining why the tag was
removed satisfied the check and failed the test. An assertion that a comment can break is an
assertion nobody trusts.
"""


# --- 1. the call that hung ------------------------------------------------

def test_the_graph_token_call_cannot_block_for_ever():
    """MSAL is handed an HTTP client, and that client always carries a timeout.

    Asserted against the object actually given to `ConfidentialClientApplication` rather than
    against the setting: the bug was never a wrong number, it was `timeout` being absent — MSAL
    builds its own session and posts without one — which no assertion about settings alone catches.
    """
    from connectors import mailbox

    seen = {}

    class _Session:
        def post(self, *args, **kwargs):
            seen.update(kwargs)

        get = post

    client = mailbox._TimeboxedHttp(settings.GRAPH_AUTH_TIMEOUT_SECONDS)
    original = mailbox._SESSION
    mailbox._SESSION = _Session()
    try:
        client.post("https://login.microsoftonline.com/token")
        assert seen.get("timeout") == settings.GRAPH_AUTH_TIMEOUT_SECONDS

        seen.clear()
        client.get("https://login.microsoftonline.com/token")
        assert seen.get("timeout") == settings.GRAPH_AUTH_TIMEOUT_SECONDS
    finally:
        mailbox._SESSION = original


def test_the_mailbox_hands_msal_the_timeboxed_client():
    """The wiring, separately from the client. `GraphMailbox.__init__` must pass `http_client`;
    without it MSAL falls back to its own untimed session and the fix above is inert."""
    import msal

    from connectors import mailbox

    captured = {}

    class _App:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    original = msal.ConfidentialClientApplication
    msal.ConfidentialClientApplication = _App
    try:
        mailbox.GraphMailbox(tenant_id="t", client_id="c", client_secret="s",
                             mailbox_address="a@b.test")
    finally:
        msal.ConfidentialClientApplication = original

    assert isinstance(captured.get("http_client"), mailbox._TimeboxedHttp), (
        "MSAL was constructed without a timeboxed http_client; it will post without a timeout")


# --- 2. the run that never ended ------------------------------------------

def test_a_run_stops_itself_at_the_ceiling(monkeypatch, tmp_path):
    """The ceiling reaches the ingest pass as `should_stop`, and is true once overrun.

    Driven through `_run_locked` rather than asserted on a constant, because a ceiling is worth
    nothing unless it is actually handed to the thing that polls it.
    """
    import tools.ingest_mailbox as ingest_mailbox

    monkeypatch.setattr(store, "CONSOLE_DB_PATH", tmp_path / "console.sqlite3")
    # -1, not 0. At 0 the deadline is exactly `began`, and the comparison is a strict `>` —
    # so on a coarse monotonic clock a poll in the same tick reads as "not yet past", and the
    # test fails for a reason that has nothing to do with the ceiling. Observed doing that.
    monkeypatch.setattr(settings, "RUN_MAX_MINUTES", -1)  # the deadline is already behind us

    polled = {}

    def fake(**kwargs):
        polled["should_stop"] = kwargs["should_stop"]
        raise RuntimeError("stop here — the call is what is under test")

    monkeypatch.setattr(ingest_mailbox, "run_once", fake)

    try:
        runner._run_locked("test", store.SOURCE_MAILBOX)
    except Exception:                                          # noqa: BLE001
        pass

    assert "should_stop" in polled, "the ingest pass was never handed a stop predicate"
    assert polled["should_stop"]() is True, (
        "past its ceiling the run must ask the ingest pass to stop; a run that cannot be bounded "
        "holds the lock and no scheduled run fires again until someone restarts the process")


def test_the_ceiling_is_not_reported_as_an_operator_stop(monkeypatch, tmp_path):
    """An overrun and a pulled kill switch are different events and must read differently.

    Both leave mail unread, but only one of them is somebody's decision. Collapsed into the same
    text, a run that hangs nightly looks like an operator who keeps pressing Stop.
    """
    import tools.ingest_mailbox as ingest_mailbox

    monkeypatch.setattr(store, "CONSOLE_DB_PATH", tmp_path / "console.sqlite3")
    monkeypatch.setattr(settings, "RUN_MAX_MINUTES", -1)

    class _Summary:
        emails_processed = records_staged = ocr_calls = 0
        orphans = []
        mailbox_address = "a@b.test"
        read_only = True
        would_have_moved = {}
        folders = []

    monkeypatch.setattr(ingest_mailbox, "run_once", lambda **kw: _Summary())

    outcome = runner._run_locked("test", store.SOURCE_MAILBOX)

    assert outcome.error and "ceiling" in outcome.error
    assert "operator" not in outcome.error


# --- 3. the rows that said "running..." for ever ---------------------------

def test_startup_closes_out_runs_whose_process_died(tmp_path):
    """A row with no `finished_at` at startup is a corpse, not a run.

    `_run_locked`'s `finally` writes `finish_run` on every path an exception can take, but not on
    a hard kill — and nothing reconciled the leftovers, so eleven accumulated over three weeks,
    each rendering as "running..." in history for ever. A phantom is indistinguishable on screen
    from the real thing, which is how a genuinely hung run went unnoticed for half a day.
    """
    conn = store.get_connection(tmp_path / "console.sqlite3")
    try:
        dead = store.start_run(conn, started_at="2026-09-03 02:15:07",
                               trigger="scheduled", source="mailbox")
        alive = store.start_run(conn, started_at="2026-09-03 15:00:00",
                                trigger="manual", source="mailbox")
        store.finish_run(conn, alive, finished_at="2026-09-03 15:01:00")

        closed = store.abandon_unfinished_runs(conn, at="2026-09-03 16:30:00")
        assert closed == 1, "only the unfinished row should be touched"

        row = conn.execute("SELECT finished_at, error FROM runs WHERE id=?", (dead,)).fetchone()
        assert row["finished_at"] == "2026-09-03 16:30:00"
        assert "interrupted" in row["error"]

        untouched = conn.execute("SELECT error FROM runs WHERE id=?", (alive,)).fetchone()
        assert untouched["error"] is None, "a run that finished cleanly must not be rewritten"

        assert conn.execute(
            "SELECT COUNT(*) FROM runs WHERE finished_at IS NULL").fetchone()[0] == 0
    finally:
        conn.close()


def test_the_console_reconciles_open_runs_when_the_app_boots():
    """The wiring. The function above is inert unless the lifespan actually calls it."""
    source = Path("api/main.py").read_text(encoding="utf-8")
    assert "abandon_unfinished_runs" in source, (
        "startup must close out open run rows, or they render as 'running...' for ever")


# --- 5. the page that reloaded itself every two seconds --------------------

def test_the_automation_page_never_carries_a_meta_refresh(monkeypatch):
    """Not even while a run is in flight — which is the only case that ever set it.

    `refresh_seconds=2 if running` emitted `<meta http-equiv="refresh" content="2">`, reloading the
    whole page every two seconds for the length of a run. For as long as a hung token call could
    keep `running` true, that was twelve hours of it. Worse, a meta tag reloads unconditionally:
    scroll position, open dialogs and active table filters all go, which is precisely what `_JS`
    consults `safeToReloadWithoutAsking()` to avoid.

    Asserted against the rendered page with `is_running()` forced true, because the regression is
    reintroduced by a one-token edit at the call site and nothing else would catch it.
    """
    from fastapi.testclient import TestClient

    from api.main import app
    from operations import runner

    monkeypatch.setattr(runner, "is_running", lambda: True)

    body = TestClient(app).get("/ui/automation").text

    assert not _META_REFRESH.search(body), (
        "the automation page is auto-reloading itself again; drive progress through the "
        "/ui/version poller, which holds back when someone is mid-read")


def test_the_poller_is_told_when_a_run_is_in_flight(monkeypatch):
    """`/ui/version` must report `running`, and `_JS` must actually consult it.

    Without it the poller falls back to "reload whenever the token moved" — and the token carries
    `email_log`'s count and highest id, which climb continuously during a pass. That would reload
    the page every ten seconds for the whole run: the same defect as the meta refresh, slower.
    """
    from fastapi.testclient import TestClient

    from api.main import app
    from api.ui import html
    from operations import runner

    monkeypatch.setattr(runner, "is_running", lambda: True)
    payload = TestClient(app).get("/ui/version").json()

    assert payload.get("running") is True, "/ui/version must carry the run state"
    assert "v.running" in html._JS, (
        "the poller ignores the run state, so it will auto-reload for the length of every run")


# --- 4. the suite that wrote production history ---------------------------

def test_the_suite_is_not_pointed_at_the_real_console_database():
    """The guard that makes the other three findable next time.

    `conftest` redirects `store.CONSOLE_DB_PATH` for the whole session. If that ever stops working
    — most likely by someone restoring `get_connection`'s import-time default — tests resume
    writing Premier's real run history and, worse, keep pushing the next live run an hour out,
    because `scheduler.next_run_at` counts from `last_run()` whatever that row's trigger was.
    """
    production = (settings.STATE_DIR / "console.sqlite3").resolve()

    assert store.CONSOLE_DB_PATH.resolve() != production, (
        "the test suite is pointed at the production console database")

    conn = store.get_connection()
    try:
        opened = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
    finally:
        conn.close()

    assert opened.resolve() != production, (
        "get_connection() resolved to production despite the redirect — the default is probably "
        "bound at import time again")
