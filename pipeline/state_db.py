import sqlite3
from pathlib import Path

from config.settings import PIPELINE_STATE_DB_PATH


def get_connection(db_path: Path = PIPELINE_STATE_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_message_ids (
            email_id TEXT PRIMARY KEY,
            seen_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def is_new_message(conn: sqlite3.Connection, email_id: str, now: str) -> bool:
    """Returns True and records the id if this email hasn't been seen before; False if it has."""
    cursor = conn.execute("SELECT 1 FROM seen_message_ids WHERE email_id = ?", (email_id,))
    if cursor.fetchone() is not None:
        return False
    conn.execute("INSERT INTO seen_message_ids (email_id, seen_at) VALUES (?, ?)", (email_id, now))
    conn.commit()
    return True
