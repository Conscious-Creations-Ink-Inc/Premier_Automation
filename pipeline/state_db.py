import sqlite3
from pathlib import Path

from config.settings import PIPELINE_STATE_DB_PATH


def get_connection(db_path=PIPELINE_STATE_DB_PATH) -> sqlite3.Connection:
    """db_path may be a real Path (default) or the special string ":memory:" for tests."""
    if isinstance(db_path, Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_message_ids (
            email_id TEXT PRIMARY KEY,
            seen_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS accumulation (
            po_number TEXT NOT NULL,
            shipment_number TEXT,
            email_id TEXT NOT NULL,
            notification_type TEXT NOT NULL,
            category TEXT NOT NULL,
            received_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (po_number, shipment_number, email_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS released_events (
            po_number TEXT NOT NULL,
            shipment_number TEXT,
            released_at TEXT NOT NULL,
            release_reason TEXT NOT NULL,
            PRIMARY KEY (po_number, shipment_number)
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
