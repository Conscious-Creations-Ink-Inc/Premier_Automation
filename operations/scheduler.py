"""One background thread, two jobs. No scheduler library.

A cron library would be a dependency and a config file for what is genuinely a couple of repeating
jobs. This wakes every few seconds and asks each of them whether it is due.

The two are deliberately not one job with one interval, because their costs differ by two orders of
magnitude:

  * **the run** — the full ingest pass. Downloads every attachment, sniffs, parses and may spend
    money on OCR; measured at about forty seconds. Scheduled in *minutes*, and it holds
    `runner._LOCK` so a manual run and a scheduled one can never overlap.
  * **the arrival watch** — a single metadata-only Graph listing that records what is *in* the
    mailbox without reading any of it; a few hundred milliseconds. Scheduled in *seconds*, and it
    must not touch the runner's lock: a fifteen-second read that could be blocked behind a
    forty-second pass would not be a fifteen-second read.

Both are off until someone enables them, and both are skipped entirely while the kill switch is
engaged — the switch is the operator's, and no schedule is consulted against it.

Settings are re-read on every tick, so changing an interval or turning either off in the UI takes
effect immediately — no restart, and no need to signal the thread.
"""

import threading
from datetime import datetime, timedelta
from typing import Optional

from operations import killswitch, runner, store

_TICK_SECONDS = 5

_thread: Optional[threading.Thread] = None
_stop = threading.Event()

_last_arrival_tick: Optional[datetime] = None
"""In memory, not in the database. The watch is a cache-warmer for a table that is itself
idempotent, so an extra poll after a restart costs one Graph call and nothing else — whereas
persisting a due-time would mean a write every fifteen seconds for ever."""


def start() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="console-scheduler", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=2)


def next_run_at() -> Optional[datetime]:
    """When the next automatic run is due, or None if the schedule is off or not yet anchored.

    Counted from the *later* of the save and the last run. Both directions matter: measuring only
    from the last run made Save itself a trigger — enable a schedule on a console whose last run
    was hours ago and it fired within five seconds, unprompted, against Premier's live mailbox.
    Measuring only from the save would let a run that just finished be immediately re-due.
    """
    conn = store.get_connection()
    try:
        schedule = store.get_schedule(conn)
        if not schedule.enabled:
            return None
        last = store.last_run(conn)
        marks = [t for t in (
            _parse(schedule.anchor_at),
            _parse(last.started_at) if last else None,
        ) if t is not None]
        if not marks:
            return None      # unanchored; app startup anchors it, so this is a boot-time gap only
        return max(marks) + timedelta(minutes=schedule.interval_minutes)
    finally:
        conn.close()


def next_arrival_poll_at() -> Optional[datetime]:
    """When the next arrival poll is due, or None if the watch is off.

    Counted from the last poll this process made. On a fresh start there is no last poll, so the
    first one is due immediately — which is the behaviour you want from a watch (start looking now)
    and emphatically not the behaviour you want from a run, which is why `next_run_at` anchors
    instead. A poll reads metadata and settles nothing; a run reads Premier's mail.
    """
    conn = store.get_connection()
    try:
        watch = store.get_arrival_watch(conn)
    finally:
        conn.close()
    if not watch.enabled:
        return None
    if _last_arrival_tick is None:
        return datetime.now()
    return _last_arrival_tick + timedelta(seconds=watch.interval_seconds)


def _loop() -> None:
    while not _stop.wait(_TICK_SECONDS):
        try:
            if killswitch.is_stopped():
                continue     # the switch is the operator's; the schedule is not consulted at all
            # The watch first, and in its own try below, so that a Graph outage cannot stop the
            # ingest schedule from firing — they fail independently because they are independent.
            _tick_arrivals()
            if _due():
                schedule_source = _source()
                runner.run(trigger="scheduled", source=schedule_source)
        except Exception:                                      # noqa: BLE001
            # A scheduler that dies on one bad tick is worse than one that keeps trying; the
            # failure is already recorded as a run row with its error text.
            continue


def _tick_arrivals() -> None:
    """Poll for new mail if the watch is on and due. Never raises, never blocks on the run lock.

    The kill switch is checked here as well as in `_loop`. Not redundant: this is the function that
    reaches Premier's mailbox, so the guarantee belongs on it rather than on one of its callers —
    anything that ever calls it directly gets the same answer.
    """
    global _last_arrival_tick

    if killswitch.is_stopped():
        return

    due_at = next_arrival_poll_at()
    if due_at is None or datetime.now() < due_at:
        return

    # Stamped before the call, not after. A poll that hangs for its full thirty-second timeout would
    # otherwise be re-due on every five-second tick behind it, queueing polls against a mailbox that
    # is already not answering.
    _last_arrival_tick = datetime.now()

    from operations import arrivals

    outcome = arrivals.poll_once()
    if outcome.skipped:
        return                       # a poll was already running; nothing to report

    conn = store.get_connection()
    try:
        store.record_arrival_poll(
            conn, at=_last_arrival_tick.strftime("%Y-%m-%d %H:%M:%S"),
            new=outcome.new, error=outcome.error,
        )
    finally:
        conn.close()


def _due() -> bool:
    due_at = next_run_at()
    return due_at is not None and datetime.now() >= due_at


def _source() -> str:
    conn = store.get_connection()
    try:
        return store.get_schedule(conn).source
    finally:
        conn.close()


def _parse(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None
