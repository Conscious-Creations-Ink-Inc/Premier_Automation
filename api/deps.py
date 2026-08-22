"""Request-scoped FastAPI dependencies."""
import sqlite3
from typing import Iterator, Optional

from fastapi import Header

from api import config, db


def get_conn() -> Iterator[sqlite3.Connection]:
    """One SQLite connection per request, closed on the way out.

    Deliberately not a module-level singleton: uvicorn runs sync endpoints on a threadpool and
    a sqlite3 connection is not safe to share across threads.
    """
    conn = db.get_demo_connection()
    try:
        yield conn
    finally:
        conn.close()


def get_pipeline_conn() -> Iterator[sqlite3.Connection]:
    """The *pipeline* state database — what the real orchestrator writes.

    Deliberately not `get_conn`, which serves the synthetic demo dashboard. The two schemas
    overlap on `extracted_records`, so a single mixed-up dependency would quietly render demo
    data on the pipeline pages and nobody would be able to tell from the screen.

    `get_connection` creates the file and every table on first open, so the pages render empty
    with a hint before the first run rather than 500ing.

    This is Premier's **live mailbox** store. It was the `.msg` sample store until 2026-08-12,
    back when these pages existed to inspect a corpus run; the corpus has been retired, so every
    page now reads the same database the automation writes when it reads the real inbox. There is
    no second store on screen any more — see `read_views.MAIL_SOURCES`.
    """
    conn = pipeline_connection()
    try:
        yield conn
    finally:
        conn.close()


def pipeline_connection() -> sqlite3.Connection:
    """The same store as `get_pipeline_conn`, opened directly rather than as a dependency.

    For the `async def` POST handlers. FastAPI resolves a sync generator dependency in a worker
    thread while an async handler runs on the event loop, and a sqlite connection is bound to the
    thread that created it — so the pair raises "SQLite objects created in a thread can only be
    used in that same thread" the moment the handler touches it.

    One definition of *which* file, so an async route can never end up reading a different store
    from the page that submitted to it. The caller closes it.
    """
    from config import settings
    from pipeline import state_db

    return state_db.get_connection(settings.PIPELINE_STATE_DB_PATH)


def get_operator(x_operator: Optional[str] = Header(default=None)) -> str:
    """Who a decision is attributed to. The UI sends `X-Operator`; a request body may override
    it per-decision. Falls back to the configured default so the demo is never blocked on auth,
    which this phase does not have."""
    return (x_operator or "").strip() or config.DEFAULT_OPERATOR
