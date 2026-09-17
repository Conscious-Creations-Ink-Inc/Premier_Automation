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
* A run is bounded by `settings.RUN_MAX_MINUTES`, polled at that same boundary. The lock is held
  for the whole of a run, so an unbounded run is not a slow run — it is an automation that never
  runs again until someone restarts the process.
* A run can be stopped on its own (`request_stop`) without engaging the kill switch, which also
  pauses the schedule. The stop reaches the mailbox read between Graph calls, not only between
  emails.
* **No run can hold the automation for ever.** Everything above is cooperative — it works only if
  the run reaches a point that asks — so `reap_if_stuck` watches from outside, off a heartbeat that
  every Graph call and every email refreshes. A run silent for `RUN_STALL_MINUTES`, or one that
  ignored a stop for `RUN_STOP_GRACE_SECONDS`, is abandoned: its history row is closed saying so and
  the lock is freed. The abandoned thread is fenced off by a generation number — it cannot write
  progress, its history row, or release a lock that is no longer its own, and its next stop check
  answers yes.
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

_CANCEL = threading.Event()
""""Stop this run" — for the run in progress only. Deliberately not the kill switch: that one stays
engaged across restarts and pauses the schedule, and an operator stopping one stuck pass should not
have to remember to resume the automation afterwards."""

_HEARTBEAT = {"at": None, "stop_requested_at": None}
"""`time.monotonic()` of the last sign of life, and of the last stop request.

Monotonic rather than wall clock so a clock change cannot fake a stall or hide one."""

_GENERATION = [0]
"""Bumped when a run is abandoned. A run remembers the value it started under, and every write it
would make on its own behalf — progress, its history row, releasing the lock — checks it first. A
thread that was abandoned while blocked and wakes up later finds it has been superseded and stands
down instead of trampling the run that replaced it."""

_THIS_RUN = threading.local()
"""The generation the run on this thread was requested under, handed from `run` to `_run_locked`.

A thread-local rather than a third argument so `_run_locked(trigger, source)` keeps the shape the
concurrency tests substitute, and so the value cannot leak between two runs on two threads."""

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
    _HEARTBEAT["at"] = time.monotonic()


def reset_for_tests() -> None:
    """Module state, like `killswitch`'s Event — a test that abandons a run would leak into the next."""
    with _STATE_LOCK:
        _CANCEL.clear()
        _HEARTBEAT["at"] = None
        _HEARTBEAT["stop_requested_at"] = None
        _STATE["running"] = False
        _STATE["since"] = None
        _STATE["run_id"] = None
        _PROGRESS.update({"phase": "", "done": 0, "total": 0, "note": ""})
        if _LOCK.locked():
            _LOCK.release()


def seconds_since_activity() -> Optional[float]:
    """How long the run in progress has been silent, or None if nothing is running.

    Shown on the console so a slow run and a stuck one look different while it is happening, not
    only afterwards in history."""
    if not _STATE["running"] or _HEARTBEAT["at"] is None:
        return None
    return max(0.0, time.monotonic() - _HEARTBEAT["at"])


def stop_requested() -> bool:
    """Whether "Stop this run" has been pressed for the run in progress."""
    return bool(_STATE["running"]) and _CANCEL.is_set()


def request_stop() -> bool:
    """Ask the run in progress to stop. Returns False if there was nothing to stop.

    Never blocks and never takes `_LOCK` — the run is holding it. The run notices at its next Graph
    call or its next email; if it has not let go within `settings.RUN_STOP_GRACE_SECONDS`,
    `reap_if_stuck` abandons it, so the button cannot turn into a request somebody watches fail.
    """
    with _STATE_LOCK:
        if not _STATE["running"]:
            return False
        if not _CANCEL.is_set():
            _HEARTBEAT["stop_requested_at"] = time.monotonic()
        _CANCEL.set()
        return True


def reap_if_stuck() -> Optional[str]:
    """Abandon the run in progress if it has stopped responding. Returns the reason, or None.

    Called from the scheduler's tick, which no longer runs the automation on its own thread — so
    this keeps being called while a run is blocked, which is the only time it is needed.

    **What abandoning does and does not do.** It closes the run's history row with the reason, bumps
    the generation and frees the lock, so the next scheduled run or a press of Run now can start. It
    does not kill the thread: Python cannot, and a thread killed between two of an email's commits
    would leave exactly the half-settled email `killswitch` exists to avoid. The thread is left
    blocked in whatever call it was in, fenced off by the generation, and its own next stop check
    ends it. Nothing is marked seen until an email settles, so whatever it had not finished is read
    again by the next run.
    """
    now = time.monotonic()
    with _STATE_LOCK:
        if not _STATE["running"]:
            return None
        last = _HEARTBEAT["at"]
        silent = (now - last) if last is not None else 0.0
        asked = _HEARTBEAT["stop_requested_at"]
        phase = _PROGRESS.get("phase") or "starting"

        if asked is not None and now - asked > settings.RUN_STOP_GRACE_SECONDS:
            reason = (f"stopped from the Automation page, and abandoned when it had not let go "
                      f"{settings.RUN_STOP_GRACE_SECONDS}s later (it was {phase}); the mail it had "
                      f"not reached is still unread")
        elif silent > settings.RUN_STALL_MINUTES * 60:
            reason = (f"abandoned — no activity for {silent / 60:.0f} minutes while {phase}; the "
                      f"automation was freed to run again and the mail it had not reached is still "
                      f"unread")
        else:
            return None

        run_id = _STATE.get("run_id")
        started = _STATE.get("since")
        _GENERATION[0] += 1
        _CANCEL.set()
        _STATE["running"] = False
        _STATE["since"] = None
        _STATE["run_id"] = None
        _HEARTBEAT["at"] = None
        _HEARTBEAT["stop_requested_at"] = None
        _PROGRESS.update({"phase": "", "done": 0, "total": 0, "note": ""})
        if _LOCK.locked():
            _LOCK.release()

    if run_id is not None:
        try:
            conn = store.get_connection()
            try:
                store.finish_run(
                    conn, run_id,
                    finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    elapsed_seconds=round((datetime.now() - started).total_seconds(), 2)
                    if started else 0.0,
                    error=reason, detail={"abandoned": True, "phase": phase},
                )
            finally:
                conn.close()
        except Exception:                                      # noqa: BLE001
            # The automation is already free; a history row that could not be written must not
            # undo that. The row is closed as interrupted at the next startup regardless.
            pass
    return reason


def run(trigger: str, source: str, generation: Optional[int] = None) -> RunOutcome:
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

    with _STATE_LOCK:
        if generation is None:
            # Called directly rather than through `start_background`: this call is the request, so
            # it starts clean — a stop pressed for some earlier run is not a stop for this one.
            generation = _GENERATION[0]
            _CANCEL.clear()
            _HEARTBEAT["stop_requested_at"] = None
        elif generation != _GENERATION[0]:
            # Requested, then abandoned before this thread ever got the lock. Starting now would be
            # a run nobody is watching, under a generation the reaper has already written off.
            _LOCK.release()
            return RunOutcome(ok=False, skipped=True, error="This run was abandoned before it began.")
        _HEARTBEAT["at"] = time.monotonic()

    _THIS_RUN.generation = generation
    try:
        return _run_locked(trigger, source)
    except Exception as exc:                                   # noqa: BLE001
        # Only reachable if opening the console DB or writing the run row failed, so there is no
        # run row carrying this error. POST /run does not catch, so raising would 500 the console.
        return RunOutcome(ok=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        _THIS_RUN.generation = None
        with _STATE_LOCK:
            if generation == _GENERATION[0]:
                _STATE["running"] = False
                _STATE["since"] = None
                _STATE["run_id"] = None
                _HEARTBEAT["at"] = None
                _HEARTBEAT["stop_requested_at"] = None
                _PROGRESS.update({"phase": "", "done": 0, "total": 0, "note": ""})
                _LOCK.release()
            # Otherwise this run was abandoned: `reap_if_stuck` already cleared the state and freed
            # the lock, and another run may be holding it now. Releasing here would free *theirs*.


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
        _CANCEL.clear()
        _HEARTBEAT["at"] = time.monotonic()
        _HEARTBEAT["stop_requested_at"] = None
        generation = _GENERATION[0]

    threading.Thread(target=_run_and_settle, args=(trigger, source, generation),
                     name="automation-run", daemon=True).start()
    return RunOutcome(ok=True)


def _run_and_settle(trigger: str, source: str, generation: Optional[int] = None) -> None:
    """`run()` on a thread, with the one flag it cannot clear itself.

    `run()` owns `_STATE` from inside its `try/finally` — but only once it holds the lock. The
    early `skipped` return, when another run got there first, never enters that block. Since
    `start_background` set the flag before spawning, that path would leave it stuck on `True`
    forever and every later press would be refused with "a run is already in progress" against no
    run at all.

    Only this thread's own claim is cleared: `start_background` is the only thing that sets the
    flag without holding `_LOCK`, so a `skipped` return here means *we* set it and never used it.
    """
    outcome = run(trigger, source, generation)
    if outcome.skipped:
        with _STATE_LOCK:
            # Only if nothing has replaced this claim since. An abandoned claim was already cleared
            # by the reaper, and the flag may now belong to a run that started after it.
            if generation is None or generation == _GENERATION[0]:
                _STATE["running"] = False
                _STATE["since"] = None


def _run_locked(trigger: str, source: str, generation: Optional[int] = None) -> RunOutcome:
    if generation is None:
        generation = getattr(_THIS_RUN, "generation", None)
    if generation is None:
        generation = _GENERATION[0]

    def current() -> bool:
        """Still the run the console is tracking, rather than one it has abandoned."""
        return generation == _GENERATION[0]

    def on_progress(phase: str, done: int, total: int, note: str = "") -> None:
        # Fenced: an abandoned run that wakes up must not paint its progress, or refresh the
        # heartbeat, over the run that replaced it.
        if current():
            _record_progress(phase, done, total, note)

    db_path = state_db.path_for(source)   # raises on anything but "sample"/"mailbox"
    conn = store.get_connection()
    started = datetime.now()
    run_id = store.start_run(
        conn, started_at=started.strftime("%Y-%m-%d %H:%M:%S"), trigger=trigger, source=source
    )
    if current():
        _STATE["running"] = True
        _STATE["since"] = started
        _STATE["run_id"] = run_id
    on_progress("starting", 0, 0, "")
    began = time.monotonic()

    emails = records = ocr_calls = needs_person = 0
    error: Optional[str] = None
    detail: dict = {}

    deadline = began + settings.RUN_MAX_MINUTES * 60

    def should_stop() -> bool:
        """The kill switch, or the run's own wall-clock ceiling.

        Both are cooperative and land at the same committed boundary between emails, so an
        overrun stops with the database consistent and the remaining mail merely unseen — the
        same guarantee `killswitch` documents, extended to a run that is taking too long.

        This exists because the lock is held for the whole of a run: a run with no ceiling is not
        just a slow run, it is an automation that never runs again. Runs 1064 and 923 blocked in
        Graph auth for twelve and thirty-six hours and every scheduled tick behind them was
        turned away with "a run is already in progress".
        """
        return (killswitch.is_stopped() or _CANCEL.is_set() or not current()
                or time.monotonic() > deadline)

    try:
        if source == store.SOURCE_MAILBOX:
            from tools.ingest_mailbox import run_once as mailbox_run
            summary = mailbox_run(ocr=settings.OCR_CLIENT, read_only=True, db_path=db_path,
                                  max_ocr_pages=settings.OCR_PAGES_PER_RUN,
                                  should_stop=should_stop,
                                  on_progress=on_progress)
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
                                 max_ocr_pages=settings.OCR_PAGES_PER_RUN,
                                 should_stop=should_stop,
                                 on_progress=on_progress)
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
        elif _CANCEL.is_set() and current():
            # "Stop this run", as distinct from the kill switch above: the schedule carries on, and
            # history should not suggest anyone paused the automation.
            error = ("stopped from the Automation page; the schedule carries on and the mail this "
                     "run had not reached is read by the next one")
        elif getattr(summary, "ocr_quota_exhausted", False):
            # Ranked above the ceiling and below an operator stop, and stated as one fact about
            # the account rather than left to be inferred from the ledger. When this went
            # unnamed, 580 attachments were filed individually as `service_unavailable` reading
            # "HTTP 403: Out of call volume quota" — which looks like 580 broken documents and is
            # actually one exhausted subscription. The mail is unread, not unreadable.
            error = ("OCR quota exhausted — the account has no call volume left this period. "
                     "Attachments after that point were not read; raise the tier, then recover "
                     "them with tools.reextract")
        elif time.monotonic() > deadline:
            # Third distinct outcome, and the one worth reading closely: nobody asked for this and
            # nothing failed, the pass simply did not fit in its ceiling. Left unnamed it would
            # read as a clean short run, which is how a stuck run stayed invisible for half a day.
            error = (f"stopped after the {settings.RUN_MAX_MINUTES}-minute ceiling; "
                     "the mail it had not reached is still unread")

    except Exception as exc:                                  # noqa: BLE001 - surfaced to the UI
        error = f"{type(exc).__name__}: {exc}"
        detail["traceback"] = traceback.format_exc()[-2000:]
    finally:
        # Its own try, because a failure writing the run row must not cost us the close below —
        # nor, via the outer `finally`, the lock.
        try:
            if not current():
                # `reap_if_stuck` already closed this row saying the run was abandoned, and why.
                # Overwriting it now would replace that with a clean-looking finish and hide
                # exactly the hang the row exists to record.
                return RunOutcome(ok=False, emails=emails, records=records,
                                  error="abandoned while it was stuck")
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
