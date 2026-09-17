"""The console's own tiny database: run history, and the schedule settings.

Kept in `state/console.sqlite3`, deliberately separate from `pipeline_state.sqlite3`. Two reasons:
the pipeline database holds mail read from Premier's live mailbox and nothing in a dashboard should
be able to migrate or corrupt it, and keeping them apart means this console can be deleted whole
without leaving a trace in the pipeline's schema.

`email_log` cannot answer "what happened last run" — it is UNIQUE on email_id, so a reprocess
overwrites the row rather than appending. That is why run history lives here instead.
"""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from config import settings

CONSOLE_DB_PATH = settings.STATE_DIR / "console.sqlite3"

# Schedule defaults. `INGEST_ORCHESTRATOR_INTERVAL_MINUTES` has sat in settings.py unused since the
# design document; this is the first thing to honour it.
DEFAULT_INTERVAL_MINUTES = getattr(settings, "INGEST_ORCHESTRATOR_INTERVAL_MINUTES", 20)
SOURCE_SAMPLE = "sample"
SOURCE_MAILBOX = "mailbox"


@dataclass
class Run:
    id: int
    started_at: str
    finished_at: Optional[str]
    trigger: str          # "manual" | "scheduled"
    source: str           # "sample" | "mailbox"
    emails: int
    records: int
    needs_person: int
    ocr_calls: int
    elapsed_seconds: float
    error: Optional[str]
    detail: str           # JSON blob, for anything worth keeping but not worth a column


SETTING_ANCHOR = "schedule_anchor_at"

# The arrival poll: a metadata-only Inbox listing, seconds apart, that records what is *there*
# without reading any of it. Separate settings from the schedule above because they are a different
# job with a different cost — one is a ~40s pipeline pass measured in minutes, the other is a single
# Graph call measured in seconds, and tying them to one interval would mean choosing which to get
# wrong.
DEFAULT_ARRIVAL_SECONDS = 15
MIN_ARRIVAL_SECONDS = 10
"""A floor, like `interval_minutes`' floor of 1. Below this the poll costs more in Graph
throttling than the latency it buys — Premier's mail does not arrive faster than this."""

SETTING_ARRIVALS_ENABLED = "arrivals_enabled"
SETTING_ARRIVALS_SECONDS = "arrivals_interval_seconds"
SETTING_ARRIVALS_LAST_AT = "arrivals_last_poll_at"
SETTING_ARRIVALS_LAST_ERROR = "arrivals_last_error"
SETTING_ARRIVALS_LAST_NEW = "arrivals_last_new"


@dataclass
class ArrivalWatch:
    enabled: bool
    interval_seconds: int
    last_poll_at: Optional[str] = None
    last_error: Optional[str] = None
    last_new: int = 0


@dataclass
class Schedule:
    enabled: bool
    interval_minutes: int
    source: str
    anchor_at: Optional[str] = None
    """When the schedule was last saved. The scheduler counts the interval from the later of this
    and the last run, which is what stops Save from being a Run: without it, enabling a schedule
    on a console whose last run was hours ago made the next run due immediately."""


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open the console database, resolving the default at call time rather than import time.

    The default used to be `db_path: Path = CONSOLE_DB_PATH`, which binds the module constant once
    when the function is defined — so rebinding `store.CONSOLE_DB_PATH` afterwards changed nothing
    and there was no way to point the console at another file. That is why the test suite wrote
    forty-eight rows into Premier's real run history, and why those rows were not merely untidy:
    the scheduler counts its interval from `last_run()`, so every test run silently pushed the next
    live run an hour into the future.

    Resolving here makes the constant the single point of control, and `tests/conftest.py` pins it.
    """
    db_path = db_path or CONSOLE_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at       TEXT NOT NULL,
            finished_at      TEXT,
            trigger          TEXT NOT NULL,
            source           TEXT NOT NULL,
            emails           INTEGER NOT NULL DEFAULT 0,
            records          INTEGER NOT NULL DEFAULT 0,
            needs_person     INTEGER NOT NULL DEFAULT 0,
            ocr_calls        INTEGER NOT NULL DEFAULT 0,
            elapsed_seconds  REAL NOT NULL DEFAULT 0,
            error            TEXT,
            detail           TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);

        CREATE TABLE IF NOT EXISTS setting (
            key    TEXT PRIMARY KEY,
            value  TEXT NOT NULL
        );

        """
    )
    conn.commit()
    return conn


# --- runs -----------------------------------------------------------------

def start_run(conn: sqlite3.Connection, *, started_at: str, trigger: str, source: str) -> int:
    """Record the run before it happens, so a crash mid-run still leaves a visible row.

    A run that vanishes because the process died is exactly the case an operator needs to see.
    """
    cur = conn.execute(
        "INSERT INTO runs (started_at, trigger, source) VALUES (?, ?, ?)",
        (started_at, trigger, source),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    finished_at: str,
    emails: int = 0,
    records: int = 0,
    needs_person: int = 0,
    ocr_calls: int = 0,
    elapsed_seconds: float = 0.0,
    error: Optional[str] = None,
    detail: Optional[dict] = None,
) -> None:
    conn.execute(
        """UPDATE runs SET finished_at=?, emails=?, records=?, needs_person=?, ocr_calls=?,
                           elapsed_seconds=?, error=?, detail=?
           WHERE id=?""",
        (finished_at, emails, records, needs_person, ocr_calls, elapsed_seconds, error,
         json.dumps(detail or {}), run_id),
    )
    conn.commit()


def abandon_unfinished_runs(conn: sqlite3.Connection, *, at: str) -> int:
    """Close out any run still marked in progress, and return how many there were.

    Called once at startup, where the claim is free: this process has just begun, so it owns no
    run, and `_LOCK` lives in memory and cannot outlive the process that held it. A row with no
    `finished_at` at this moment is therefore not a run in progress — it is a run whose process
    died before `finish_run` could be reached, and `_run_locked`'s `finally` cannot help with a
    hard kill.

    Nothing reconciled these, so they accumulated: eleven rows going back to 2026-08-13, each one
    rendering as "running..." in history for ever. The cost is not cosmetic. An operator reading
    the console cannot tell a phantom from the real thing, which is exactly the confusion that let
    a genuinely hung run sit unnoticed for twelve hours.

    `elapsed_seconds` is deliberately left at 0 rather than computed from `started_at`: we do not
    know when the process died, and a fabricated duration is worse than an obvious absence.
    """
    cur = conn.execute(
        """UPDATE runs SET finished_at=?, error=?
           WHERE finished_at IS NULL""",
        (at, "interrupted — the console restarted while this run was in progress"),
    )
    conn.commit()
    return cur.rowcount


def recent_runs(conn: sqlite3.Connection, limit: int = 25) -> List[Run]:
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        Run(
            id=r["id"], started_at=r["started_at"], finished_at=r["finished_at"],
            trigger=r["trigger"], source=r["source"], emails=r["emails"], records=r["records"],
            needs_person=r["needs_person"], ocr_calls=r["ocr_calls"],
            elapsed_seconds=r["elapsed_seconds"], error=r["error"], detail=r["detail"],
        )
        for r in rows
    ]


def last_run(conn: sqlite3.Connection) -> Optional[Run]:
    runs = recent_runs(conn, limit=1)
    return runs[0] if runs else None


# --- settings -------------------------------------------------------------

def get_schedule(conn: sqlite3.Connection) -> Schedule:
    rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM setting")}
    source = rows.get("source", SOURCE_SAMPLE)
    if source not in (SOURCE_SAMPLE, SOURCE_MAILBOX):
        source = SOURCE_SAMPLE
    try:
        interval = int(rows.get("interval_minutes", DEFAULT_INTERVAL_MINUTES))
    except ValueError:
        interval = DEFAULT_INTERVAL_MINUTES
    return Schedule(
        enabled=rows.get("enabled") == "1",
        interval_minutes=max(1, interval),
        source=source,
        anchor_at=rows.get(SETTING_ANCHOR) or None,
    )


def set_schedule(conn: sqlite3.Connection, schedule: Schedule, *, now: str) -> None:
    """`now` is passed in rather than read from the clock so this stays testable, and so the
    anchor is exactly the moment the operator pressed Save."""
    pairs = {
        "enabled": "1" if schedule.enabled else "0",
        "interval_minutes": str(max(1, schedule.interval_minutes)),
        "source": schedule.source,
        SETTING_ANCHOR: now,
    }
    _put(conn, pairs)


def get_arrival_watch(conn: sqlite3.Connection) -> ArrivalWatch:
    """**Off unless someone turned it on.** `rows.get(...) == "1"` is the whole guarantee.

    The standing rule is that no automatic trigger runs anywhere without explicit enablement, and
    this reads Premier's live mailbox on a timer — so a missing key, an unreadable value or a fresh
    database all have to mean "not running", never "default on".
    """
    rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM setting")}
    try:
        seconds = int(rows.get(SETTING_ARRIVALS_SECONDS, DEFAULT_ARRIVAL_SECONDS))
    except ValueError:
        seconds = DEFAULT_ARRIVAL_SECONDS
    try:
        last_new = int(rows.get(SETTING_ARRIVALS_LAST_NEW, 0))
    except ValueError:
        last_new = 0
    return ArrivalWatch(
        enabled=rows.get(SETTING_ARRIVALS_ENABLED) == "1",
        interval_seconds=max(MIN_ARRIVAL_SECONDS, seconds),
        last_poll_at=rows.get(SETTING_ARRIVALS_LAST_AT) or None,
        last_error=rows.get(SETTING_ARRIVALS_LAST_ERROR) or None,
        last_new=last_new,
    )


def set_arrival_watch(conn: sqlite3.Connection, *, enabled: bool, interval_seconds: int) -> None:
    _put(conn, {
        SETTING_ARRIVALS_ENABLED: "1" if enabled else "0",
        SETTING_ARRIVALS_SECONDS: str(max(MIN_ARRIVAL_SECONDS, interval_seconds)),
    })


def record_arrival_poll(conn: sqlite3.Connection, *, at: str, new: int = 0,
                        error: Optional[str] = None) -> None:
    """What the last poll did. **Deliberately not a `runs` row.**

    A run row per poll would write four an hour times sixty and bury every real pipeline run in
    history — the one thing that history exists to make findable. Three settings keys carry
    everything the operations page needs to say whether the watch is alive and healthy.
    """
    _put(conn, {
        SETTING_ARRIVALS_LAST_AT: at,
        SETTING_ARRIVALS_LAST_NEW: str(new),
        SETTING_ARRIVALS_LAST_ERROR: error or "",
    })


def ensure_anchor(conn: sqlite3.Connection, now: str) -> None:
    """Anchor a console that predates the setting, so its first automatic run is an interval away
    rather than instantly due. INSERT OR IGNORE: an existing anchor is never moved by a restart."""
    conn.execute("INSERT OR IGNORE INTO setting (key, value) VALUES (?, ?)", (SETTING_ANCHOR, now))
    conn.commit()


def _put(conn: sqlite3.Connection, pairs: dict) -> None:
    conn.executemany(
        "INSERT INTO setting (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        list(pairs.items()),
    )
    conn.commit()
