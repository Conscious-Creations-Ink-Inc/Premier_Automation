"""The fast path: notice mail arrived, show it, and enrich it later.

Mail took twenty minutes or more to appear on screen because it only entered the store when a full
ingest pass finished, and that pass takes about forty seconds so it is scheduled in minutes. These
tests hold the split that fixes it — a metadata-only poll writing `mail_arrivals` in milliseconds,
and the expensive pass stamping `enriched_at` when it eventually catches up.

Two of them guard against damage rather than regression: the arrival poll must never advance the
ingest watermark, and the watch must never default to on.
"""

import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from operations import arrivals, killswitch, scheduler, store as ops_store
from pipeline import mail_arrivals, state_db


@pytest.fixture
def conn():
    connection = state_db.get_connection(":memory:")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def console_db(tmp_path, monkeypatch):
    path = tmp_path / "console.sqlite3"
    monkeypatch.setattr(ops_store, "CONSOLE_DB_PATH", path)
    monkeypatch.setattr(ops_store.get_connection, "__defaults__", (path,))
    killswitch.reset_for_tests()
    yield path
    killswitch.reset_for_tests()


def arrival(email_id="<a@x>", *, received="2026-08-13T09:00:00Z", subject="Delivered",
            sender="wh@vendor.com", has_attachments=False, first_seen="2026-08-13 09:00:01",
            enriched=None):
    return mail_arrivals.Arrival(
        email_id=email_id, received_at=received, sender=sender, subject=subject,
        has_attachments=has_attachments, first_seen_at=first_seen, enriched_at=enriched,
    )


# --------------------------------------------------------------------- store --

def test_recording_the_same_message_twice_counts_it_once(conn):
    """The poll re-lists an overlap window every time, so the same message arrives in several
    consecutive polls. A "new" count that counted those would make the operations page report new
    mail four times a minute for ever."""
    assert mail_arrivals.record(conn, [arrival()], now="2026-08-13 09:00:01") == 1
    assert mail_arrivals.record(conn, [arrival()], now="2026-08-13 09:00:16") == 0
    assert len(mail_arrivals.pending(conn)) == 1


def test_first_seen_survives_a_re_listing(conn):
    """`first_seen_at` is when *we* first knew, which is what makes "how far behind is the
    automation" answerable. Re-stamping it on every poll would reset that to now for ever."""
    mail_arrivals.record(conn, [arrival(first_seen="2026-08-13 09:00:01")], now="x")
    mail_arrivals.record(conn, [arrival(first_seen="2026-08-13 11:30:00")], now="x")
    assert mail_arrivals.get(conn, "<a@x>").first_seen_at == "2026-08-13 09:00:01"


def test_a_re_listing_updates_the_subject(conn):
    """A subject can legitimately change under us — an edited draft, a different Graph projection.
    The identity is the message id; everything else is refreshed."""
    mail_arrivals.record(conn, [arrival(subject="Delivered")], now="x")
    mail_arrivals.record(conn, [arrival(subject="Delivered — corrected")], now="x")
    assert mail_arrivals.get(conn, "<a@x>").subject == "Delivered — corrected"


def test_marking_enriched_never_invents_an_arrival(conn):
    """Corpus runs and mail read before this table existed have no arrival row. Creating one would
    claim the watch saw a message it never did."""
    mail_arrivals.mark_enriched(conn, "<never-seen@x>", "2026-08-13 09:05:00")
    assert mail_arrivals.get(conn, "<never-seen@x>") is None


def log_verdict(conn, email_id="<a@x>"):
    """What the orchestrator writes when it settles an email. This — not `enriched_at` — is what
    takes an arrival out of "not read yet"."""
    from pipeline import email_log

    email_log.record(conn, email_id=email_id, category="surface", folder="Processed",
                     processed_at="2026-08-13 09:05:00")


def test_a_verdict_takes_a_row_out_of_pending(conn):
    mail_arrivals.record(conn, [arrival()], now="x")
    assert mail_arrivals.pending_count(conn) == 1
    log_verdict(conn)
    assert mail_arrivals.pending_count(conn) == 0


def test_pending_asks_email_log_not_the_stamp(conn):
    """**The bug the first live poll found.** The very first poll listed 22 messages the pipeline
    had processed days earlier, and every one rendered "not read yet" — because `enriched_at` is
    only ever set by a *future* run, and those runs had already happened.

    A one-off backfill would have fixed that morning and nothing else: the same drift returns
    whenever a store is copied, an arrival is re-listed, or the watch is switched on against a
    mailbox that already has history. Asking `email_log` cannot drift, because it is the table
    that actually holds the verdict.
    """
    mail_arrivals.record(conn, [arrival()], now="x")
    log_verdict(conn)
    assert mail_arrivals.get(conn, "<a@x>").enriched_at is None, "no stamp — this is the point"
    assert mail_arrivals.pending_count(conn) == 0
    assert mail_arrivals.pending(conn) == []


def test_the_stamp_alone_does_not_hide_an_unprocessed_message(conn):
    """The converse, and the safer direction to get wrong: a stray stamp on a message with no
    verdict must not take it off the page."""
    mail_arrivals.record(conn, [arrival()], now="x")
    mail_arrivals.mark_enriched(conn, "<a@x>", "2026-08-13 09:05:00")
    assert mail_arrivals.pending_count(conn) == 1


def test_enrichment_is_not_re_stamped(conn):
    """The pipeline can process a message more than once. The first read is when it stopped being
    unread, and moving the stamp later would misreport how long it waited."""
    mail_arrivals.record(conn, [arrival()], now="x")
    mail_arrivals.mark_enriched(conn, "<a@x>", "2026-08-13 09:05:00")
    mail_arrivals.mark_enriched(conn, "<a@x>", "2026-08-13 18:00:00")
    assert mail_arrivals.get(conn, "<a@x>").enriched_at == "2026-08-13 09:05:00"


# ------------------------------------------------------------------- version --

def test_the_signature_moves_on_arrival_and_again_on_enrichment(conn):
    """This is the whole real-time mechanism: the ten-second poller in `_JS` compares this string.
    It has to move twice — once when the row appears, once when its "not read yet" badge clears."""
    empty = mail_arrivals.version_signature(conn)

    mail_arrivals.record(conn, [arrival()], now="x")
    arrived = mail_arrivals.version_signature(conn)
    assert arrived != empty

    log_verdict(conn)
    assert mail_arrivals.version_signature(conn) != arrived


def test_the_version_endpoint_still_answers_without_touching_graph(monkeypatch):
    from operations import inbox as inbox_reader

    def boom(*args, **kwargs):
        raise AssertionError("/ui/version reached Graph")

    monkeypatch.setattr(inbox_reader, "load", boom)
    monkeypatch.setattr(inbox_reader, "_token", boom)
    body = TestClient(app_module().app).get("/ui/version").json()
    assert body["token"]


def app_module():
    from api import main
    return main


# ----------------------------------------------------------------- watermark --

def test_the_arrival_poll_has_its_own_watermark(conn):
    """**The most damaging mistake available here.** The arrival poll settles nothing — it lists
    metadata and writes no verdict. If it advanced the ingest watermark, the pipeline's next
    listing would start *after* mail the pipeline had never read, and that mail would never be
    processed by anything. Silently."""
    state_db.advance_arrivals_watermark(conn, "2026-08-13T12:00:00Z")
    assert state_db.get_arrivals_watermark(conn) == "2026-08-13T12:00:00Z"
    assert state_db.get_watermark(conn) is None
    assert state_db.ARRIVALS_WATERMARK_KEY != state_db.WATERMARK_KEY


def test_the_arrival_watermark_never_moves_backwards(conn):
    state_db.advance_arrivals_watermark(conn, "2026-08-13T12:00:00Z")
    state_db.advance_arrivals_watermark(conn, "2026-08-01T00:00:00Z")
    assert state_db.get_arrivals_watermark(conn) == "2026-08-13T12:00:00Z"


def test_the_listing_window_is_backdated(conn):
    """Mail is stamped by the server and indexed a moment later, so a window starting exactly at
    the last-seen timestamp can step over a message."""
    assert arrivals._window_start(None) is None
    assert arrivals._window_start("2026-08-13T12:00:00Z") == "2026-08-13T11:55:00Z"
    assert arrivals._window_start("not a date") is None


# ---------------------------------------------------------------------- poll --

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.captured = None

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _graph_returning(monkeypatch, value, seen):
    import requests

    def fake_get(url, headers=None, params=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        return _FakeResponse({"value": value})

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(arrivals.inbox_reader, "_token", lambda: "token")


def test_a_poll_records_what_it_lists_and_advances_only_its_own_watermark(tmp_path, monkeypatch):
    seen = {}
    _graph_returning(monkeypatch, [{
        "internetMessageId": "<live@premier>",
        "receivedDateTime": "2026-08-13T09:00:00Z",
        "subject": "PO 208491 delivered",
        "from": {"emailAddress": {"address": "wh@vendor.com"}},
        "hasAttachments": True,
    }], seen)
    monkeypatch.setattr(arrivals.settings, "GRAPH_TENANT_ID", "t", raising=False)
    monkeypatch.setattr(arrivals.settings, "GRAPH_CLIENT_ID", "c", raising=False)
    monkeypatch.setattr(arrivals.settings, "GRAPH_CLIENT_SECRET", "s", raising=False)
    monkeypatch.setattr(arrivals.settings, "GRAPH_MAILBOX_ADDRESS", "r@premierpm.com",
                        raising=False)

    db = tmp_path / "live.sqlite3"
    outcome = arrivals.poll_once(db_path=db)
    assert outcome.ok and outcome.new == 1

    conn = state_db.get_connection(db)
    try:
        row = mail_arrivals.get(conn, "<live@premier>")
        assert row.subject == "PO 208491 delivered"
        assert row.has_attachments is True
        assert row.enriched_at is None
        assert state_db.get_arrivals_watermark(conn) == "2026-08-13T09:00:00Z"
        assert state_db.get_watermark(conn) is None, "the ingest watermark must not move"
    finally:
        conn.close()


def test_a_poll_asks_for_metadata_only_and_one_page(tmp_path, monkeypatch):
    """Asking for `body` here is what makes the ingest listing heavy, and none of it would be used.
    Ten sequential pages at ~3.2s each is what made the old page load cost half a minute."""
    seen = {}
    _graph_returning(monkeypatch, [], seen)
    for name in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET"):
        monkeypatch.setattr(arrivals.settings, name, "x", raising=False)
    monkeypatch.setattr(arrivals.settings, "GRAPH_MAILBOX_ADDRESS", "r@premierpm.com",
                        raising=False)

    arrivals.poll_once(db_path=tmp_path / "live.sqlite3")
    assert "body" not in seen["params"]["$select"]
    assert seen["params"]["$top"] == arrivals.PAGE_SIZE
    assert "/mailFolders/Inbox/messages" in seen["url"]


def test_a_message_without_a_stable_id_is_skipped(tmp_path, monkeypatch):
    """Graph's own `id` changes the moment a message moves, so `internetMessageId` is the only
    thing that can be deduped on. Without it there is nothing to record against."""
    seen = {}
    _graph_returning(monkeypatch, [{"id": "volatile", "receivedDateTime": "2026-08-13T09:00:00Z"}],
                     seen)
    for name in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET"):
        monkeypatch.setattr(arrivals.settings, name, "x", raising=False)
    monkeypatch.setattr(arrivals.settings, "GRAPH_MAILBOX_ADDRESS", "r@premierpm.com",
                        raising=False)

    outcome = arrivals.poll_once(db_path=tmp_path / "live.sqlite3")
    assert outcome.ok and outcome.new == 0


def test_a_poll_that_cannot_reach_graph_reports_instead_of_raising(tmp_path, monkeypatch):
    """A watch that dies on one bad tick is worse than one that keeps trying, and the error has to
    reach the operations page rather than vanishing."""
    import requests

    def boom(*args, **kwargs):
        raise RuntimeError("Graph is unreachable")

    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr(arrivals.inbox_reader, "_token", lambda: "token")
    for name in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET"):
        monkeypatch.setattr(arrivals.settings, name, "x", raising=False)
    monkeypatch.setattr(arrivals.settings, "GRAPH_MAILBOX_ADDRESS", "r@premierpm.com",
                        raising=False)

    outcome = arrivals.poll_once(db_path=tmp_path / "live.sqlite3")
    assert not outcome.ok
    assert "Graph is unreachable" in outcome.error


def test_an_unconfigured_mailbox_says_which_settings_are_missing(tmp_path, monkeypatch):
    for name in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET",
                 "GRAPH_MAILBOX_ADDRESS"):
        monkeypatch.setattr(arrivals.settings, name, "", raising=False)
    outcome = arrivals.poll_once(db_path=tmp_path / "live.sqlite3")
    assert not outcome.ok and "GRAPH_TENANT_ID" in outcome.error


# --------------------------------------------------------------------- watch --

def test_the_watch_is_off_until_someone_turns_it_on(console_db):
    """The standing rule is that no automatic trigger runs anywhere without explicit enablement.
    This one reads Premier's live mailbox on a timer, so a fresh database must mean "not running"."""
    conn = ops_store.get_connection()
    try:
        assert ops_store.get_arrival_watch(conn).enabled is False
    finally:
        conn.close()
    assert scheduler.next_arrival_poll_at() is None


def test_the_interval_has_a_floor(console_db):
    conn = ops_store.get_connection()
    try:
        ops_store.set_arrival_watch(conn, enabled=True, interval_seconds=1)
        assert ops_store.get_arrival_watch(conn).interval_seconds == ops_store.MIN_ARRIVAL_SECONDS
    finally:
        conn.close()


def test_saving_the_watch_from_the_page_persists_it(console_db):
    from api.main import app

    with TestClient(app) as client:
        response = client.post("/ui/automation/arrivals",
                               data={"enabled": "1", "interval_seconds": "20"},
                               follow_redirects=False)
    assert response.status_code == 303, response.text

    conn = ops_store.get_connection()
    try:
        saved = ops_store.get_arrival_watch(conn)
    finally:
        conn.close()
    assert saved.enabled is True and saved.interval_seconds == 20


def test_the_kill_switch_stops_the_watch(console_db, monkeypatch):
    """The switch is the operator's. No schedule is consulted against it, and the guarantee sits on
    the function that reaches the mailbox rather than on one of its callers."""
    conn = ops_store.get_connection()
    try:
        ops_store.set_arrival_watch(conn, enabled=True, interval_seconds=10)
        killswitch.engage(conn, "2026-08-13 09:00:00")
    finally:
        conn.close()

    polled = []
    monkeypatch.setattr(arrivals, "poll_once", lambda **kw: polled.append(1))
    scheduler._tick_arrivals()
    assert polled == [], "a stopped console still polled Premier's mailbox"


def test_the_watch_polls_once_it_is_enabled_and_running(console_db, monkeypatch):
    """The other half of the test above: with the switch released and the watch on, a tick must
    actually reach the poller — otherwise the assertion above passes for the wrong reason."""
    conn = ops_store.get_connection()
    try:
        ops_store.set_arrival_watch(conn, enabled=True, interval_seconds=10)
    finally:
        conn.close()

    polled = []
    monkeypatch.setattr(scheduler, "_last_arrival_tick", None)
    monkeypatch.setattr(
        arrivals, "poll_once",
        lambda **kw: polled.append(1) or arrivals.PollOutcome(ok=True, new=1))
    scheduler._tick_arrivals()
    assert polled == [1]


def test_a_poll_does_not_write_a_run_row(console_db, monkeypatch):
    """Four polls a minute would bury every real pipeline run in history — the one thing history
    exists to make findable."""
    conn = ops_store.get_connection()
    try:
        ops_store.record_arrival_poll(conn, at="2026-08-13 09:00:00", new=2)
        assert ops_store.recent_runs(conn) == []
        watch = ops_store.get_arrival_watch(conn)
    finally:
        conn.close()
    assert watch.last_poll_at == "2026-08-13 09:00:00"
    assert watch.last_new == 2


def test_the_watch_does_not_take_the_run_lock():
    """A fifteen-second metadata read that could be blocked behind a forty-second pipeline pass
    would not be a fifteen-second read."""
    from operations import runner

    assert arrivals._lock is not runner._LOCK


# ----------------------------------------------------------------- the page --

@pytest.fixture
def mail_page(tmp_path, monkeypatch):
    """`/ui/mails` reading a throwaway live store, with Graph wired to explode.

    Both halves matter. The temporary database keeps the test off Premier's real files, and the
    exploding Graph stub proves the arrival rows come from SQLite rather than from a listing.
    """
    from api import deps
    from api.main import app
    from operations import inbox as inbox_reader
    from pipeline import read_views

    path = tmp_path / "live.sqlite3"

    def _live_conn():
        connection = state_db.get_connection(path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def boom(*args, **kwargs):
        raise AssertionError("the mail page reached Graph while rendering")

    app.dependency_overrides[deps.get_pipeline_conn] = _live_conn
    monkeypatch.setattr(read_views, "MAIL_SOURCES", (("inbox", "Inbox", "PIPELINE_STATE_DB_PATH"),))
    monkeypatch.setattr(inbox_reader, "load", boom)
    monkeypatch.setattr(inbox_reader, "_token", boom)
    try:
        yield path, TestClient(app)
    finally:
        app.dependency_overrides.pop(deps.get_pipeline_conn, None)


def test_an_arrival_shows_on_the_mail_page_before_anything_has_read_it(mail_page):
    """The point of the whole exercise: the row is on screen without a run having happened, and it
    is visibly a different kind of row — no verdict, no rule, no counts. Claiming a verdict for
    mail nobody has read would be the one dishonest thing this page could do."""
    path, client = mail_page
    conn = state_db.get_connection(path)
    try:
        mail_arrivals.record(conn, [arrival(subject="PO 208491 delivered")], now="x")
    finally:
        conn.close()

    body = client.get("/ui/mails").text
    assert "PO 208491 delivered" in body
    assert "not read yet" in body
    assert "arrived and not yet read by the pipeline" in body


def test_an_arrival_the_pipeline_has_read_stops_being_listed_as_waiting(mail_page):
    """The row does not vanish — it becomes the verdict row underneath. What goes away is the
    "not read yet" badge, which is the second live update a message makes."""
    path, client = mail_page
    conn = state_db.get_connection(path)
    try:
        mail_arrivals.record(conn, [arrival(subject="PO 208491 delivered")], now="x")
        log_verdict(conn)
    finally:
        conn.close()

    body = client.get("/ui/mails").text
    assert "not read yet" not in body
    assert "Nothing unprocessed" in body


def test_the_mail_page_is_searchable_over_an_arrival(mail_page):
    """An arrival is a row like any other, so the filter added alongside this reaches it too."""
    path, client = mail_page
    conn = state_db.get_connection(path)
    try:
        mail_arrivals.record(conn, [arrival()], now="x")
    finally:
        conn.close()

    body = client.get("/ui/mails").text
    assert 'data-filter="mail-table"' in body
    assert 'id="mail-table"' in body


# ------------------------------------------------------------------- forget --

def test_forgetting_an_email_unstamps_its_arrival_rather_than_deleting_it(conn):
    """Forgetting means it will be ingested again, so "in the mailbox, not read" is exactly true
    afterwards. Deleting would hide the message until a future poll re-listed it — and the
    arrivals watermark has usually moved past it by then, so for older mail that poll never comes."""
    mail_arrivals.record(conn, [arrival()], now="x")
    mail_arrivals.mark_enriched(conn, "<a@x>", "2026-08-13 09:05:00")

    state_db.forget_emails(conn, ["<a@x>"])

    row = mail_arrivals.get(conn, "<a@x>")
    assert row is not None, "the arrival must survive — the message is still in the mailbox"
    assert row.enriched_at is None
    assert mail_arrivals.pending_count(conn) == 1
