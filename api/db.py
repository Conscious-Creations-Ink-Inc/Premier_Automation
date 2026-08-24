"""The dashboard's SQLite database.

Reuses `pipeline.state_db.get_connection()` so the demo file is created with the real
`extracted_records` schema — that's what lets `extracted_records_store.mark_matched` /
`mark_failed` be reused verbatim for approve/cancel. On top of that we add the tables that
stages 4-7 will own once they're built for real: PO lines, match verdicts, the human decision
audit trail, staged receipts, and the vendor chase template.

Like `state_db`, `db_path` may be a real Path (default) or ":memory:" for tests.
"""
import sqlite3

from api import config
from pipeline import state_db

_DEMO_TABLES = (
    # Candidate set for reconciliation — mirrors pipeline.models.POLine.
    """
    CREATE TABLE IF NOT EXISTS po_lines (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        po_number     TEXT NOT NULL,
        line_number   INTEGER NOT NULL,
        line_key      TEXT NOT NULL UNIQUE,
        spec_code     TEXT NOT NULL,
        description   TEXT NOT NULL,
        vendor_name   TEXT NOT NULL,
        unit_of_measure TEXT NOT NULL,
        qty_ordered   REAL NOT NULL,
        qty_received  REAL NOT NULL DEFAULT 0,
        -- Unapproved receipts. POLine and POLineSchema have carried this since the Spitfire read
        -- landed; this table did not, so every row read back 0.0 and outstanding quantity was
        -- overstated by exactly the in-flight amount — the double-receive the field exists to stop.
        qty_in_transit REAL NOT NULL DEFAULT 0,
        cost_code     TEXT NOT NULL,
        project_code  TEXT NOT NULL,
        project_name  TEXT NOT NULL,
        line_status   TEXT NOT NULL,
        expected_date TEXT,
        ship_to       TEXT,
        assigned_agent TEXT,
        pay_terms     TEXT
    )
    """,
    # One row per synthetic email + the triage verdict it would have received. Powers the
    # extracted-records context, the delivery report buckets and the inbox organizer.
    """
    CREATE TABLE IF NOT EXISTS demo_emails (
        email_id       TEXT PRIMARY KEY,
        received_at    TEXT NOT NULL,
        sender_address TEXT NOT NULL,
        sender_domain  TEXT NOT NULL,
        subject        TEXT NOT NULL,
        body_snippet   TEXT,
        notification_type TEXT NOT NULL,
        triage_category   TEXT NOT NULL,
        matched_rule   TEXT NOT NULL,
        reason         TEXT NOT NULL DEFAULT '',
        status_keyword TEXT NOT NULL,
        po_number      TEXT,
        has_attachment INTEGER NOT NULL DEFAULT 0,
        proposed_folder TEXT NOT NULL,
        keep_in_inbox  INTEGER NOT NULL DEFAULT 0,
        organized_folder TEXT,
        moved_at       TEXT
    )
    """,
    # The reconciliation verdict for one extracted record (mock stages 4-5).
    """
    CREATE TABLE IF NOT EXISTS match_results (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        extracted_record_id INTEGER NOT NULL UNIQUE,
        po_line_id          INTEGER,
        po_signal           INTEGER NOT NULL DEFAULT 0,
        spec_signal         INTEGER NOT NULL DEFAULT 0,
        desc_signal         INTEGER NOT NULL DEFAULT 0,
        desc_score          REAL NOT NULL DEFAULT 0,
        signals_matched     INTEGER NOT NULL DEFAULT 0,
        confidence          TEXT NOT NULL,
        missing_fields      TEXT NOT NULL DEFAULT '',
        flagged             INTEGER NOT NULL DEFAULT 0,
        flag_reason         TEXT NOT NULL DEFAULT '',
        route_target        TEXT NOT NULL,
        review_status       TEXT NOT NULL,
        notes               TEXT NOT NULL DEFAULT '',
        created_at          TEXT NOT NULL,
        updated_at          TEXT
    )
    """,
    # Human audit trail — who decided what, when, and why. Never overwritten.
    """
    CREATE TABLE IF NOT EXISTS review_decisions (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        match_result_id     INTEGER NOT NULL,
        extracted_record_id INTEGER NOT NULL,
        decision            TEXT NOT NULL,
        decided_by          TEXT NOT NULL,
        decided_at          TEXT NOT NULL,
        reason              TEXT NOT NULL DEFAULT '',
        resulting_status    TEXT NOT NULL,
        po_line_id          INTEGER,
        receipt_id          INTEGER
    )
    """,
    # Mock stage 6 output — what would be pushed into Spitfire.
    """
    CREATE TABLE IF NOT EXISTS staged_receipts (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        extracted_record_id INTEGER NOT NULL,
        po_line_id          INTEGER NOT NULL,
        shipment_number     TEXT,
        purchase_order      TEXT NOT NULL,
        item_number         TEXT NOT NULL,
        item_description    TEXT NOT NULL,
        vendor              TEXT NOT NULL,
        carrier_name        TEXT,
        pro_number          TEXT,
        quantity            REAL NOT NULL,
        quantity_types      TEXT NOT NULL DEFAULT 'EA',
        act_delivery_date   TEXT NOT NULL,
        delivery_location   TEXT,
        comments            TEXT,
        created_by          TEXT NOT NULL,
        created_at          TEXT NOT NULL
    )
    """,
    # Single-row template used to chase vendors whose mails lack the fields we need.
    """
    CREATE TABLE IF NOT EXISTS vendor_template (
        id             INTEGER PRIMARY KEY CHECK (id = 1),
        subject        TEXT NOT NULL,
        body           TEXT NOT NULL,
        schedule_hours INTEGER NOT NULL DEFAULT 24,
        enabled        INTEGER NOT NULL DEFAULT 0,
        updated_at     TEXT,
        updated_by     TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vendor_send_log (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        extracted_record_id INTEGER,
        po_number           TEXT,
        sent_to             TEXT NOT NULL,
        subject             TEXT NOT NULL,
        rendered_body       TEXT NOT NULL,
        sent_at             TEXT NOT NULL,
        sent_by             TEXT NOT NULL,
        status              TEXT NOT NULL DEFAULT 'mock_sent'
    )
    """,
)


# Columns added after the first demo databases were created. `CREATE TABLE IF NOT EXISTS` leaves
# an existing table exactly as it was, so a developer with a database from before the column
# existed gets a 500 on the first read — the store selects `*` and builds a POLine by name, and the
# missing key raises. The pipeline solves this with `state_db._LATER_COLUMNS`; these tables are
# owned here, so their migration lives here too.
_DEMO_LATER_COLUMNS = {
    "po_lines": [("qty_in_transit", "REAL NOT NULL DEFAULT 0")],
}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _DEMO_LATER_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, sql_type in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


def get_demo_connection(db_path=None) -> sqlite3.Connection:
    """Opens (creating if needed) the dashboard database, with both the pipeline's own tables
    and the dashboard's. Callers own the connection and should close it — the FastAPI
    dependency in `api.deps` does that per request."""
    conn = state_db.get_connection(config.DEMO_DB_PATH if db_path is None else db_path)
    for statement in _DEMO_TABLES:
        conn.execute(statement)
    # After the CREATEs, not before: a brand-new database has no tables to alter until they exist.
    _add_missing_columns(conn)
    conn.commit()
    return conn


def is_seeded(conn: sqlite3.Connection) -> bool:
    """True once the demo generator has run — used to seed on first startup only."""
    return conn.execute("SELECT 1 FROM po_lines LIMIT 1").fetchone() is not None
