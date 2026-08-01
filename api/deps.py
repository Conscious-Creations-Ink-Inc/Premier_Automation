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


def get_operator(x_operator: Optional[str] = Header(default=None)) -> str:
    """Who a decision is attributed to. The UI sends `X-Operator`; a request body may override
    it per-decision. Falls back to the configured default so the demo is never blocked on auth,
    which this phase does not have."""
    return (x_operator or "").strip() or config.DEFAULT_OPERATOR
