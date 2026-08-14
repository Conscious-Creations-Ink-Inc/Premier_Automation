"""One switch that stops everything, and stays stopped.

Two representations of one fact, deliberately:

* A row in `console.sqlite3`'s `setting` table, so the switch survives a restart. A kill switch
  that forgets when the process bounces is not a kill switch — the operator who pulled it has
  gone home, and the thing they stopped must still be stopped in the morning.
* A `threading.Event` mirroring it, so the check inside the ingest loop costs a memory read
  rather than a SQLite query per email.

The database is the truth on boot (`load`), the Event is the truth thereafter. Both are written
together by `engage`/`release`, so they cannot disagree.

What "stop" means here is *cooperative*, and that is a deliberate choice rather than a
limitation. `should_stop` is checked between emails, at a point where the previous email has been
fully triaged, logged and committed. Killing the thread mid-write would leave a half-settled
email — ledgered attachments with no verdict, or an accumulation row with no log entry — which is
precisely the state nobody can reason about afterwards. So a stop lands within one email rather
than within one instruction, and the database is always consistent when it does.
"""

import sqlite3
import threading
from typing import Optional

from operations import store

SETTING_KEY = "stopped"
SETTING_SINCE = "stopped_at"

_event = threading.Event()
_since: Optional[str] = None


def load(conn: sqlite3.Connection) -> None:
    """Restore the switch from disk. Called once at startup, before the scheduler starts."""
    global _since
    rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM setting")}
    _since = rows.get(SETTING_SINCE)
    if rows.get(SETTING_KEY) == "1":
        _event.set()
    else:
        _event.clear()
        _since = None


def is_stopped() -> bool:
    """Cheap enough to call per email."""
    return _event.is_set()


def stopped_since() -> Optional[str]:
    return _since if _event.is_set() else None


def engage(conn: sqlite3.Connection, now: str) -> None:
    """Stop everything. Sets the Event first: an in-flight run should see this on its next email
    even if the write below is slow or contended."""
    global _since
    _event.set()
    _since = now
    _write(conn, {SETTING_KEY: "1", SETTING_SINCE: now})


def release(conn: sqlite3.Connection) -> None:
    """Let it run again. Clears the flag on disk before the Event, so a crash between the two
    leaves the switch engaged rather than silently released."""
    global _since
    _write(conn, {SETTING_KEY: "0", SETTING_SINCE: ""})
    _since = None
    _event.clear()


def _write(conn: sqlite3.Connection, pairs: dict) -> None:
    conn.executemany(
        "INSERT INTO setting (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        list(pairs.items()),
    )
    conn.commit()


def reset_for_tests() -> None:
    """The Event is module state, so a test that engages the switch would leak into the next."""
    global _since
    _event.clear()
    _since = None
