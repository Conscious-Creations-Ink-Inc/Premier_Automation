"""Runs the automation and records what happened.

One lock for the whole console, so a scheduled run and a button press can never overlap on the
same SQLite file. A second caller is turned away rather than queued — pressing a button twice
should be a no-op, not two runs.

Safety, deliberate and not configurable from the UI:

* The live-mailbox path always passes `read_only=True`, and nothing here calls `mark_processed`.
  No mail is moved, filed or marked, ever.
* OCR uses the **configured** client (`settings.OCR_CLIENT`, `auto` by default, which resolves to
  Azure when credentials are present). This was hardcoded to the mock, on the reasoning that real
  OCR costs money per page and an unattended loop is the wrong place to spend it. That reasoning
  was sound while nothing ran unattended, and it had a cost: photographed PODs came back
  `disposition=empty` — indistinguishable on screen from an image that genuinely held nothing —
  so five multi-megabyte delivery photos were never actually read. What replaces it is the page
  budget: `tools.ingest_mailbox.run_once` wraps whatever it is handed in `BudgetedOcrClient`,
  which counts pages and *raises* past the cap, so a budget stop is recorded against the
  attachment's ledger row as a stated reason instead of looking blank. Do not bypass that wrapper.
* Each source writes its own database, chosen here once via `state_db.path_for`. Sample runs
  cannot reach the live store and vice versa.
* Every run polls the kill switch between emails, so a stop lands on a committed boundary.
"""

import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from config import settings
from operations import killswitch, store
from pipeline import state_db

_LOCK = threading.Lock()
_STATE = {"running": False, "since": None}

_PROGRESS = {"phase": "", "done": 0, "total": 0, "note": ""}
"""Where the run in progress has got to, written by the orchestrator's `on_progress` callback.

Deliberately a plain dict of primitives with no lock. It is written by one thread and read by
request threads that only ever render it, so a reader seeing `done` from one moment and `total`
from the next is a progress bar one frame stale — not a correctness problem, and not worth
contending a lock on every page render to avoid."""

_STATE_LOCK = threading.Lock()
"""Guards the check-and-set in `start_background`.

Not the same thing as `_LOCK`, which is held for the *duration* of a run. This one is held for a
few microseconds so that "is one running?" and "mark one as running" cannot be split by another
thread — FastAPI serves sync endpoints on a threadpool, so two clicks a millisecond apart really do
run concurrently."""


@dataclass
class RunOutcome:
    ok: bool
    skipped: bool = False
    emails: int = 0
    records: int = 0
    error: Optional[str] = None


def is_running() -> bool:
    return bool(_STATE["running"])


def running_since() -> Optional[str]:
    """When the run in progress started, as the console's own timestamp format, or None if idle."""
    started = _STATE["since"]
    return started.strftime("%Y-%m-%d %H:%M:%S") if started else None


def progress() -> dict:
    """`{phase, done, total}` for the run in progress. A copy, so a renderer cannot mutate it."""
    return dict(_PROGRESS)


def _record_progress(phase: str, done: int, total: int, note: str = "") -> None:
    _PROGRESS["phase"] = phase
    _PROGRESS["done"] = done
    _PROGRESS["total"] = total
    _PROGRESS["note"] = note


def run(trigger: str, source: str) -> RunOutcome:
    """Run once. `trigger` is "manual" or "scheduled"; `source` is "sample" or "mailbox".

    Nothing but the lock lives out here. Everything that can raise is inside `_run_locked`, so
    `finally: _LOCK.release()` cannot be skipped — previously `store.get_connection()` and
    `store.start_run()` sat between the acquire and the try, and a locked database there leaked
    the lock permanently while `is_running()` still cheerfully reported idle.
    """
    if killswitch.is_stopped():
        return RunOutcome(ok=False, skipped=True,
                          error="Automation is stopped. Press Resume to allow runs again.")
    if not _LOCK.acquire(blocking=False):
        return RunOutcome(ok=False, skipped=True, error="A run is already in progress.")
    try:
        return _run_locked(trigger, source)
    except Exception as exc:                                   # noqa: BLE001
        # Only reachable if opening the console DB or writing the run row failed, so there is no
        # run row carrying this error. POST /run does not catch, so raising would 500 the console.
        return RunOutcome(ok=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        _STATE["running"] = False
        _STATE["since"] = None
        _record_progress("", 0, 0, "")
        _LOCK.release()


def start_background(trigger: str, source: str) -> RunOutcome:
    """Kick a run off and return at once, leaving it to finish on its own thread.

    A live-mailbox pass takes about thirty seconds, and `run()` does not return until it is done —
    so the button that starts one held the browser for the whole pass and only then redirected. The
    console has always been able to *show* a run in progress (`is_running()` drives the "Running
    now…" banner, and run history renders "running…" for any row without a `finished_at`); there
    was simply no way to reach that state, because the only thing that started a run blocked until
    `is_running()` was false again.

    The returned `RunOutcome` describes *starting*, not the run: `ok=True` means it is under way.

    `_STATE["running"]` is set here rather than in the thread on purpose. The caller redirects
    immediately, and the redirect reliably beats a freshly-spawned thread — so the page would
    redraw reading "idle" for a run that had just begun, which is exactly the confusion this is
    meant to remove.
    """
    if killswitch.is_stopped():
        return RunOutcome(ok=False, skipped=True,
                          error="Automation is stopped. Press Resume to allow runs again.")

    # Check and set together. Read separately, two near-simultaneous presses both saw "idle", both
    # spawned, and the one that then lost the race for `_LOCK` came back `skipped` and cleared
    # `_STATE` out from under the run that had actually started — the banner vanished, the page
    # stopped refreshing, and a run in progress reported itself idle.
    with _STATE_LOCK:
        if _STATE["running"]:
            return RunOutcome(ok=False, skipped=True, error="A run is already in progress.")
        _STATE["running"] = True
        _STATE["since"] = datetime.now()

    threading.Thread(target=_run_and_settle, args=(trigger, source),
                     name="automation-run", daemon=True).start()
    return RunOutcome(ok=True)


def _run_and_settle(trigger: str, source: str) -> None:
    """`run()` on a thread, with the one flag it cannot clear itself.

    `run()` owns `_STATE` from inside its `try/finally` — but only once it holds the lock. The
    early `skipped` return, when another run got there first, never enters that block. Since
    `start_background` set the flag before spawning, that path would leave it stuck on `True`
    forever and every later press would be refused with "a run is already in progress" against no
    run at all.

    Only this thread's own claim is cleared: `start_background` is the only thing that sets the
    flag without holding `_LOCK`, so a `skipped` return here means *we* set it and never used it.
    """
    outcome = run(trigger, source)
    if outcome.skipped:
        with _STATE_LOCK:
            _STATE["running"] = False
            _STATE["since"] = None


def _run_locked(trigger: str, source: str) -> RunOutcome:
    db_path = state_db.path_for(source)   # raises on anything but "sample"/"mailbox"
    conn = store.get_connection()
    started = datetime.now()
    run_id = store.start_run(
        conn, started_at=started.strftime("%Y-%m-%d %H:%M:%S"), trigger=trigger, source=source
    )
    _STATE["running"] = True
    _STATE["since"] = started
    _record_progress("starting", 0, 0, "")
    began = time.monotonic()

    emails = records = ocr_calls = needs_person = 0
    error: Optional[str] = None
    detail: dict = {}

    try:
        if source == store.SOURCE_MAILBOX:
            from tools.ingest_mailbox import run_once as mailbox_run
            summary = mailbox_run(ocr=settings.OCR_CLIENT, read_only=True, db_path=db_path,
                                  should_stop=killswitch.is_stopped,
                                  on_progress=_record_progress)
            detail = {
                "mailbox": summary.mailbox_address,
                "read_only": summary.read_only,
                "would_have_moved": len(summary.would_have_moved or {}),
                "folders": summary.folders,
            }
        else:
            from tools.ingest_corpus import run_once as corpus_run
            # `reset` forgets only this corpus's own emails first. Without it a second run
            # processes nothing — seen ids are permanent — and the screen would look broken.
            summary = corpus_run(ocr=settings.OCR_CLIENT, reset=True, db_path=db_path,
                                 should_stop=killswitch.is_stopped,
                                 on_progress=_record_progress)
            detail = {"source_files": summary.msg_files, "folders": summary.folders}

        emails = summary.emails_processed
        records = summary.records_staged
        ocr_calls = summary.ocr_calls
        detail["orphan_attachments"] = summary.orphans
        detail["database"] = getattr(db_path, "name", str(db_path))
        needs_person = _needs_person(db_path)
        if killswitch.is_stopped():
            # Distinguishable in history from a run that simply found nothing: the operator did
            # this, and the remaining mail is still unseen rather than lost.
            error = "stopped by operator"

    except Exception as exc:                                  # noqa: BLE001 - surfaced to the UI
        error = f"{type(exc).__name__}: {exc}"
        detail["traceback"] = traceback.format_exc()[-2000:]
    finally:
        # Its own try, because a failure writing the run row must not cost us the close below —
        # nor, via the outer `finally`, the lock.
        try:
            store.finish_run(
                conn, run_id,
                finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                emails=emails, records=records, needs_person=needs_person, ocr_calls=ocr_calls,
                elapsed_seconds=round(time.monotonic() - began, 2),
                error=error, detail=detail,
            )
        finally:
            conn.close()

    return RunOutcome(ok=error is None, emails=emails, records=records, error=error)


def _needs_person(db_path) -> int:
    """How many items a person has to look at in the store this run just wrote.

    `db_path` is not optional. Hardcoding the live store here is what made the console report
    "14 need a person" after a 14-email sample run: 12 of those were live-mailbox rows from days
    earlier, and the sample run's history row inherited a count belonging to the other store.
    """
    import sqlite3

    from pipeline import read_views

    try:
        conn = state_db.get_connection(db_path)
        conn.row_factory = sqlite3.Row
        try:
            return len(read_views.manual_queue(conn))
        finally:
            conn.close()
    except Exception:                                          # noqa: BLE001
        # A count is not worth failing a run that otherwise succeeded.
        return 0
