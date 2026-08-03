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
            po_line_number        INTEGER,
            received_by           TEXT,
            package_quantity      REAL,
            package_uom           TEXT,
            notification_number   TEXT,
            status                TEXT NOT NULL DEFAULT 'pending',
            created_at            TEXT NOT NULL,
            updated_at            TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS attachment_ledger (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id              TEXT NOT NULL,
            parent_id             INTEGER,
            depth                 INTEGER NOT NULL DEFAULT 0,
            ordinal               INTEGER NOT NULL DEFAULT 0,
            container_path        TEXT NOT NULL DEFAULT '',
            filename              TEXT NOT NULL DEFAULT '',
            declared_content_type TEXT,
            sniffed_kind          TEXT NOT NULL,
            sniff_reason          TEXT NOT NULL DEFAULT '',
            sha256                TEXT NOT NULL DEFAULT '',
            size_bytes            INTEGER NOT NULL DEFAULT 0,
            content_id            TEXT,
            is_inline             INTEGER NOT NULL DEFAULT 0,
            triage_category       TEXT,
            claimed_by            TEXT,
            records_extracted     INTEGER NOT NULL DEFAULT 0,
            disposition           TEXT NOT NULL,
            disposition_detail    TEXT NOT NULL DEFAULT '',
            error_type            TEXT,
            review_status         TEXT NOT NULL DEFAULT 'none',
            first_seen_at         TEXT NOT NULL,
            last_updated_at       TEXT,
            UNIQUE (email_id, depth, ordinal, sha256)
        )
    """)
    # The uniqueness key is (email, depth, ordinal, sha) rather than (email, sha): one corpus
    # message carries two byte-identical PODs saved under different filenames, and both slots
    # must appear in the ledger even though only one of them is extracted.
    conn.execute("CREATE INDEX IF NOT EXISTS ix_ledger_email ON attachment_ledger(email_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_ledger_disposition ON attachment_ledger(disposition, review_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_ledger_sha ON attachment_ledger(sha256)")

    _add_missing_columns(conn)
    conn.commit()
    return conn


def forget_message(conn: sqlite3.Connection, email_id: str) -> None:
    """Drop the seen-marker so a message can be ingested again.

    `is_new_message` records an id permanently, so an attachment we could not read today would
    never be retried after the reader that could read it is written. This is what makes the
    ledger's `reprocess` action possible instead of the ledger being a graveyard.
    """
    conn.execute("DELETE FROM seen_message_ids WHERE email_id = ?", (email_id,))
    conn.commit()


# Columns added after the first stores were created. `CREATE TABLE IF NOT EXISTS` leaves an
# existing table untouched, so a database created before these fields existed would silently
# drop them on write — which is exactly what a line number, the most valuable field the
# Authority format gives us, must never do.
_LATER_COLUMNS = {
    "extracted_records": [
        ("po_line_number", "INTEGER"),
        ("received_by", "TEXT"),
        ("package_quantity", "REAL"),
        ("package_uom", "TEXT"),
        ("notification_number", "TEXT"),
    ],
}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _LATER_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, sql_type in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


def is_new_message(conn: sqlite3.Connection, email_id: str, now: str) -> bool:
    """Returns True and records the id if this email hasn't been seen before; False if it has."""
    cursor = conn.execute("SELECT 1 FROM seen_message_ids WHERE email_id = ?", (email_id,))
    if cursor.fetchone() is not None:
        return False
    conn.execute("INSERT INTO seen_message_ids (email_id, seen_at) VALUES (?, ?)", (email_id, now))
    conn.commit()
    return True
