import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True, scope="session")
def _never_bill_azure_from_the_test_suite():
    """Force the mock OCR client for the whole suite, regardless of what `.env` holds.

    `build_default_adapters` resolves its OCR client through `build_client()`, which prefers the
    real Azure Document Intelligence client whenever credentials are present — correct for a
    production run, and exactly wrong for tests: every image fixture a test happens to push
    through the pipeline would become a billed page against Premier's quota, silently.

    Tests that mean to exercise a real client construct it explicitly; nothing here stops them.
    """
    previous = os.environ.get("PREMIER_OCR_CLIENT")
    os.environ["PREMIER_OCR_CLIENT"] = "mock"

    from config import settings
    previous_setting = settings.OCR_CLIENT
    settings.OCR_CLIENT = "mock"

    yield

    settings.OCR_CLIENT = previous_setting
    if previous is None:
        os.environ.pop("PREMIER_OCR_CLIENT", None)
    else:
        os.environ["PREMIER_OCR_CLIENT"] = previous


@pytest.fixture(autouse=True, scope="session")
def _the_hold_grace_period_is_the_real_one():
    """Pin `HOLD_GRACE_PERIOD_HOURS` to the shipped rule, whatever `.env` says.

    It is env-overridable so a demo can release held mail immediately
    (`PREMIER_HOLD_GRACE_HOURS=0`). Left unpinned, that override silently rewrites what the suite
    is testing: with it set to 0, `test_property_confirmation_only_releases_after_grace_period`
    and the corpus regression both failed — not because anything was broken, but because the rule
    under test had been changed underneath them by a file that is not even in the repository.

    A test asserting a business rule must read that rule from the code, never from the machine it
    happens to be running on.
    """
    from config import settings
    previous = settings.HOLD_GRACE_PERIOD_HOURS
    settings.HOLD_GRACE_PERIOD_HOURS = 48
    yield
    settings.HOLD_GRACE_PERIOD_HOURS = previous


@pytest.fixture(autouse=True, scope="session")
def _the_suite_never_replays_recorded_spitfire_responses():
    """Pin `SPITFIRE_CASSETTE_MODE` to `off`, whatever `.env` holds.

    Same reasoning as the two fixtures above, and the same failure: a developer working away from
    Premier's office IP sets `SPITFIRE_CASSETTE_MODE=replay` in `.env` so the UI is usable, and
    the whole suite then runs with a transport adapter mounted under both Spitfire clients. Tests
    that mean to exercise a live-shaped path would hit `SpitfireCassetteMiss`, and
    `test_ui_post_route` would get the Offline fragment instead of the outcome it asserts — both
    failing for a reason that is not in the code.

    Tests about the cassette set the mode themselves; nothing here stops them.
    """
    from config import settings
    previous = settings.SPITFIRE_CASSETTE_MODE
    settings.SPITFIRE_CASSETTE_MODE = "off"
    yield
    settings.SPITFIRE_CASSETTE_MODE = previous


@pytest.fixture(autouse=True, scope="session")
def _the_suite_never_logs_in_to_spitfire():
    """Blank every Spitfire credential, whatever `.env` holds.

    `connectors/spitfire_auth.py` re-reads `.env` on each login so that a password change is a
    one-file edit. Without this, a developer's real account would be one stray code path away from
    a live login from the test suite. Tests about authentication set the values themselves.
    """
    from config import settings
    from connectors import spitfire_auth

    names = ("SPITFIRE_UID", "SPITFIRE_PW", "SPITFIRE_SESSION_COOKIE")
    previous = {name: getattr(settings, name) for name in names}
    previous_env_file = spitfire_auth.ENV_FILE
    for name in names:
        setattr(settings, name, None)
    spitfire_auth.ENV_FILE = None
    spitfire_auth.forget_tickets()
    yield
    spitfire_auth.forget_tickets()
    spitfire_auth.ENV_FILE = previous_env_file
    for name, value in previous.items():
        setattr(settings, name, value)


@pytest.fixture(autouse=True, scope="session")
def _the_suite_routes_mail_by_the_synthetic_domains():
    """Pin the mail-routing domain lists to the synthetic `.test` set, whatever `.env` holds.

    Triage decides what an email *is* by which list its sender's domain falls in, so these lists
    are the single most behaviour-defining setting in the suite. They are environment-driven
    because the real values are Premier's operational data and may not live in source (Ashford
    Standards v1.5 §12) — which means a developer with a populated `.env` and a CI runner with
    none would otherwise be running two different test suites against the same fixtures.

    Same reasoning as the fixtures above, and the same failure mode: fixtures are written against
    the synthetic domains, so with the real ones loaded from `.env` every sender falls in no list
    at all and triage rules silently stop firing. Nothing would look broken; the rules would just
    never match.

    Tests about domain configuration set these themselves; nothing here stops them.
    """
    from config import settings

    pinned = {
        "INTERNAL_DOMAINS": ["example-pm.test"],
        "WAREHOUSE_SENDER_DOMAINS": ["example-logistics.test", "example-warehouse.test"],
        "FREIGHT_SENDER_DOMAINS": [
            "fedex.com", "ups.com", "dhl.com", "rxo.com", "oldominion.com", "globaltranz.com"],
        "VENDOR_CONFIRMATION_DOMAINS": [
            "example-interiors.test", "example-tile.test",
            "example-flooring.test", "example-finishes.test"],
        "PROPERTY_DOMAINS": ["example-hotels.test", "example-property.test"],
        "REPORT_SENDER_ADDRESSES": ["reports@example-pm.test"],
    }

    previous = {name: getattr(settings, name) for name in pinned}
    for name, value in pinned.items():
        setattr(settings, name, value)

    # Two modules copy a domain list into a module-level constant at import time, so pinning
    # settings alone would leave them holding whatever `.env` supplied.
    from pipeline.parsing import thread
    from pipeline.vendors import authority

    previous_thread = thread.INTERNAL_DOMAINS
    previous_authority = authority.AUTHORITY_DOMAINS
    thread.INTERNAL_DOMAINS = set(pinned["INTERNAL_DOMAINS"])
    authority.AUTHORITY_DOMAINS = tuple(pinned["WAREHOUSE_SENDER_DOMAINS"])

    yield

    authority.AUTHORITY_DOMAINS = previous_authority
    thread.INTERNAL_DOMAINS = previous_thread
    for name, value in previous.items():
        setattr(settings, name, value)


@pytest.fixture(autouse=True, scope="session")
def _the_suite_never_writes_the_real_run_history():
    """Point the console database at a throwaway file for the whole suite.

    `operations.store` keeps run history and the schedule in `state/console.sqlite3` — the real
    one, the one `/ui/automation` renders. Several tests drive the runner directly to assert what
    it passes downstream (`test_ocr_budget_wiring` is the clearest case: it calls `_run_locked`
    and monkeypatches the ingest function to raise). Driving the runner writes a run row, and with
    nothing redirecting the path those rows landed in production: forty-eight of them by
    2026-09-03, every one showing in Premier's history as a failed run reading
    "RuntimeError: stop here — the call is what is under test".

    The visible mess was the smaller half. `scheduler.next_run_at()` counts the interval from the
    later of the anchor and `store.last_run()`, and `last_run` is simply the newest row whatever
    its trigger — so each test run moved the next *live* run a full hour out. A test suite could
    postpone Premier's automation, and nothing anywhere said so.

    Session-scoped and autouse, so this holds for tests that do not know the console exists —
    which is the point. A test should have to opt *in* to touching production, not remember to opt
    out. Tests wanting their own console DB still pass a path to `get_connection` explicitly.
    """
    from operations import store

    previous = store.CONSOLE_DB_PATH
    with tempfile.TemporaryDirectory(prefix="premier-console-test-") as tmp:
        store.CONSOLE_DB_PATH = Path(tmp) / "console.sqlite3"
        _seed_one_synthetic_run(store)
        try:
            yield
        finally:
            store.CONSOLE_DB_PATH = previous


def _seed_one_synthetic_run(store) -> None:
    """One finished run, so `/ui/automation` renders a populated history table.

    Not decoration. `html.table` renders `<p class="empty">` and **no headers at all** for an empty
    table, so with no runs the page has no `data-date` column marker and
    `test_each_page_has_a_date_range_bound_to_its_date_column` fails on `/ui/automation`.

    That test used to pass only because the suite was reading Premier's real console database,
    which had a thousand runs in it — the exact coupling the redirect above removes. Seeding one
    synthetic row keeps the page realistic without reaching for production data (CLAUDE.md §3).
    """
    conn = store.get_connection()
    try:
        run_id = store.start_run(conn, started_at="2026-01-02 03:04:05",
                                 trigger="scheduled", source=store.SOURCE_SAMPLE)
        store.finish_run(conn, run_id, finished_at="2026-01-02 03:05:05",
                         emails=2, records=1, elapsed_seconds=60.0)
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _the_suite_is_signed_in():
    """Let every existing test past the sign-in gate, without teaching 113 call sites to log in.

    `/ui` and `/api` are guarded at the router (`Depends(auth.require_session)`), so the moment
    that gate went in, every one of the suite's `TestClient` calls would have started collecting a
    303 to `/login` instead of a page. Overriding the dependency once, here, is the same trick
    `_corpus_as_a_test_fixture` uses for the database, and it keeps the guard itself real: it is
    FastAPI's supported override, it exists only inside this process, and nothing reachable over
    HTTP can set it.

    `tests/test_auth.py` pops this override again so that the gate is tested through the front
    door rather than through its own bypass.
    """
    from api import auth
    from api.main import app

    app.dependency_overrides[auth.require_session] = lambda: "test-operator"
    yield
    app.dependency_overrides.pop(auth.require_session, None)
