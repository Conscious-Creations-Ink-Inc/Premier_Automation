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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS extracted_records (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            source_email_id       TEXT NOT NULL,
            po_number             TEXT,
            shipment_number       TEXT,
            spec_code             TEXT,
            parent_spec_code      TEXT,
            sub_spec_suffix       TEXT,
            item_description      TEXT,
            vendor_name           TEXT,
            carrier_name          TEXT,
            tracking_number       TEXT,
            quantity_received     REAL,
            unit_of_measure       TEXT,
            pod_stated_date       TEXT,
            email_date            TEXT NOT NULL,
            delivery_location     TEXT,
            comments              TEXT,
            extraction_source     TEXT NOT NULL,
            extraction_confidence REAL NOT NULL,
            raw_snippet           TEXT,
            status                TEXT NOT NULL DEFAULT 'pending',
            created_at            TEXT NOT NULL,
            updated_at            TEXT
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
