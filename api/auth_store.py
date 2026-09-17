"""Who may sign in, and the key their session cookie is signed with.

A separate SQLite file (`settings.AUTH_DB_PATH`), for the reasons set out on that setting. Nothing
here imports the pipeline, and the pipeline never imports this: the two stores stay independent so
that reading one tells you nothing about the other.

**No password is stored.** `app_user.password_hash` holds a PBKDF2-SHA256 digest in the format
`api.auth.hash_password` produces, which is not reversible. Nothing in this module logs, echoes or
returns a password, and no default account is created by importing it -- an empty store admits
nobody. `tools/create_admin.py` is what puts the first row in.
"""
import sqlite3
from pathlib import Path
from typing import Optional

from config import settings

_SCHEMA_READY = set()


def get_connection(db_path=None) -> sqlite3.Connection:
    """Open the auth store, creating the file and its two tables on first use.

    Same `IF NOT EXISTS` bootstrap the rest of the app uses, and the same once-per-process memo as
    `pipeline.state_db` -- an auth check happens on every single request, and it must not cost a
    write transaction.
    """
    path = Path(db_path) if db_path is not None else Path(settings.AUTH_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    key = str(path)
    if key not in _SCHEMA_READY:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_user (
                username      TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                last_login_at TEXT
            )
        """)
        # One row, key = 'session_secret'. Kept here rather than in an environment variable so that
        # signing out everybody is a delete of one row, and so a fresh checkout cannot accidentally
        # run with a shared or committed key. See `api.auth.session_secret`.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_secret (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.commit()
        _SCHEMA_READY.add(key)
    return conn


def reset_schema_cache() -> None:
    """Forget which auth stores this process has built. For tests, which make many."""
    _SCHEMA_READY.clear()


def user_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM app_user").fetchone()[0]


def find_user(conn: sqlite3.Connection, username: str) -> Optional[sqlite3.Row]:
    """The row for this username, or None. Case-insensitive: people do not remember the case they
    signed up with, and 'Admin' failing where 'admin' works reads as a broken password."""
    return conn.execute(
        "SELECT * FROM app_user WHERE username = ? COLLATE NOCASE", (username.strip(),)).fetchone()


def create_user(conn: sqlite3.Connection, *, username: str, password_hash: str, now: str) -> None:
    """Add an account. The caller hashes; this module never sees a password.

    `INSERT` rather than `INSERT OR REPLACE`: silently overwriting an existing account's password
    is how an account takeover looks when it is spelled as a convenience.
    """
    conn.execute(
        "INSERT INTO app_user (username, password_hash, created_at) VALUES (?, ?, ?)",
        (username.strip(), password_hash, now))
    conn.commit()


def set_password(conn: sqlite3.Connection, *, username: str, password_hash: str) -> None:
    conn.execute("UPDATE app_user SET password_hash = ? WHERE username = ? COLLATE NOCASE",
                 (password_hash, username.strip()))
    conn.commit()


def rotate_session_secret(conn: sqlite3.Connection) -> None:
    """Throw away the signing key, so every cookie issued under it stops proving anything.

    A session cookie here is self-describing and checked without a database read, which is what
    makes it cheap -- but it also means a cookie cannot be revoked one at a time. Changing a
    password must not leave the old session working, so the key it was signed with goes, and the
    next request mints a new one. With a single administrator that is exactly "sign me out
    everywhere"; with several it signs everyone out, which is the safe direction to err.
    """
    conn.execute("DELETE FROM app_secret WHERE key = 'session_secret'")
    conn.commit()


def record_login(conn: sqlite3.Connection, *, username: str, now: str) -> None:
    conn.execute("UPDATE app_user SET last_login_at = ? WHERE username = ? COLLATE NOCASE",
                 (now, username.strip()))
    conn.commit()


def get_secret(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM app_secret WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def put_secret(conn: sqlite3.Connection, key: str, value: str) -> str:
    """Store a secret only if one is not already there, and return whichever now stands.

    `INSERT OR IGNORE` then read back, rather than write-then-return: two workers starting at once
    would otherwise each write their own session key and invalidate the other's cookies.
    """
    conn.execute("INSERT OR IGNORE INTO app_secret (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    return get_secret(conn, key) or value
