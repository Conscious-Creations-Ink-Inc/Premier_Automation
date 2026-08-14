import sqlite3
from pathlib import Path
from typing import Dict, Optional, Sequence

from config.settings import PIPELINE_STATE_DB_PATH, SAMPLE_STATE_DB_PATH

# The two stores. These strings match console.store.SOURCE_* so a source saved in the console
# maps straight to a file without a translation table.
STORE_MAILBOX = "mailbox"
STORE_SAMPLE = "sample"


def path_for(source: str):
    """The one place a source name becomes a file.

    Raises rather than defaulting. A typo that quietly resolved to the live store is exactly the
    failure this split exists to prevent — better a stack trace than sample rows on a receiver
    report for Premier's real purchase orders.
    """
    if source == STORE_SAMPLE:
        return SAMPLE_STATE_DB_PATH
    if source == STORE_MAILBOX:
        return PIPELINE_STATE_DB_PATH
    raise ValueError(f"unknown store {source!r} — expected {STORE_MAILBOX!r} or {STORE_SAMPLE!r}")


def get_connection(db_path=PIPELINE_STATE_DB_PATH) -> sqlite3.Connection:
    """db_path may be a real Path (default) or the special string ":memory:" for tests."""
    if isinstance(db_path, Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
    # The console reads this file while a scheduled run writes it. Under the default rollback
    # journal a writer blocks readers and one of them dies with "database is locked" — which
    # surfaces as a 500 on the report page mid-run. console/store.py has always done this.
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")   # per-connection, so it must be set every open
    if db_path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")  # persisted in the file header; idempotent
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_message_ids (
            email_id TEXT PRIMARY KEY,
            seen_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ingest_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_log (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id            TEXT NOT NULL UNIQUE,
            subject             TEXT NOT NULL DEFAULT '',
            sender              TEXT NOT NULL DEFAULT '',
            origin_sender       TEXT,
            email_date          TEXT NOT NULL DEFAULT '',
            origin_sent_at      TEXT,
            notification_type   TEXT,
            category            TEXT NOT NULL,
            matched_rule        TEXT NOT NULL DEFAULT '',
            reason              TEXT NOT NULL DEFAULT '',
            po_hints            TEXT NOT NULL DEFAULT '',
            shipment_hint       TEXT,
            notification_number TEXT,
            attachment_count    INTEGER NOT NULL DEFAULT 0,
            ocr_attempted       INTEGER NOT NULL DEFAULT 0,
            folder              TEXT NOT NULL,
            error_type          TEXT,
            processed_at        TEXT NOT NULL
        )
    """)
    # One row per email, holding the verdict Stage 1 reached and where the mail was filed.
    # `attachment_ledger.orphans()` proves no attachment is lost; nothing proved no *email* was.
    # A ROUTE or HIDE mail exits before extraction and, with no attachments, leaves no ledger row
    # either — so the two categories that most need a person were the two the database could not
    # show. UNIQUE(email_id) makes this current-verdict-per-email, not history: a reprocess after
    # `forget_message` overwrites its row. Anything wanting "what did we decide last Tuesday"
    # needs a separate events table, not this one.
    conn.execute("CREATE INDEX IF NOT EXISTS ix_email_log_category ON email_log(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_email_log_processed ON email_log(processed_at)")

    # Recovered mail, cached so a message is fetched from its original source once rather than on
    # every click. `source` records which of the four sources answered, because when a body looks
    # wrong the first question is where it came from.
    #
    # These live in the pipeline store, not in a cache file of their own, so a message is cached
    # beside the mail it was recovered from. Sample and live mail are separated by file rather than
    # by a column everywhere else in this system (`path_for`), and a shared mail cache would be the
    # one place that stopped being true.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mail_body (
            email_id     TEXT PRIMARY KEY,
            subject      TEXT NOT NULL DEFAULT '',
            sender       TEXT NOT NULL DEFAULT '',
            received_at  TEXT NOT NULL DEFAULT '',
            body_html    TEXT,
            body_text    TEXT,
            source       TEXT NOT NULL DEFAULT '',
            cached_at    TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mail_attachment (
            email_id      TEXT NOT NULL,
            ordinal       INTEGER NOT NULL,
            filename      TEXT NOT NULL DEFAULT '',
            content_type  TEXT NOT NULL DEFAULT '',
            kind          TEXT NOT NULL DEFAULT '',
            size_bytes    INTEGER NOT NULL DEFAULT 0,
            content_id    TEXT,
            is_inline     INTEGER NOT NULL DEFAULT 0,
            content       BLOB,
            PRIMARY KEY (email_id, ordinal)
        )
    """)

    # What is *in* the mailbox, as opposed to what has been *read* from it. One row per message the
    # cheap arrival poll has seen, holding only what a listing returns: no body, no attachments, no
    # verdict.
    #
    # Deliberately not a row in `email_log`. Every row there carries a triage verdict — category,
    # matched_rule, reason, folder — which drives the badges, the category counts and the receiver
    # report; a verdict-less placeholder would make all of those read "unknown" for mail nobody has
    # actually judged. Worse, the orchestrator settles each email with `INSERT OR REPLACE`
    # (email_log.record), so the poller and the pipeline would be racing for one primary key. Two
    # tables, joined on the page, keeps the verdict store honest about what it knows.
    #
    # `enriched_at` is set when the pipeline finally logs the same email, which is what lets the
    # Mail page stop badging a row "not read yet" without asking Graph anything.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mail_arrivals (
            email_id        TEXT PRIMARY KEY,
            received_at     TEXT NOT NULL DEFAULT '',
            sender          TEXT NOT NULL DEFAULT '',
            subject         TEXT NOT NULL DEFAULT '',
            has_attachments INTEGER NOT NULL DEFAULT 0,
            first_seen_at   TEXT NOT NULL,
            enriched_at     TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mail_arrivals_received ON mail_arrivals(received_at DESC)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mail_arrivals_pending ON mail_arrivals(enriched_at)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS spitfire_po_index (
            po_number         TEXT PRIMARY KEY,
            doc_master_key    TEXT NOT NULL,
            project_code      TEXT NOT NULL DEFAULT '',
            project_name      TEXT NOT NULL DEFAULT '',
            doc_status        TEXT NOT NULL DEFAULT '',
            doc_status_label  TEXT NOT NULL DEFAULT '',
            source_date       TEXT,
            vendor_name       TEXT NOT NULL DEFAULT '',
            vendor_email      TEXT,
            ship_to           TEXT,
            assigned_agent    TEXT,
            pay_terms_prose   TEXT,
            tax_lines_skipped INTEGER NOT NULL DEFAULT 0,
            refreshed_at      TEXT NOT NULL
        )
    """)
    # PO number -> DocMasterKey, the gap nothing else crosses. Emails name a PO ("212456"); every
    # Spitfire read endpoint is keyed by GUID, and resolving one costs a site-wide search or, in
    # the worst case, a walk of every project's document list. Caching it is the difference
    # between one lookup per PO ever and one per delivery notification.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS spitfire_po_lines (
            line_key        TEXT PRIMARY KEY,
            po_number       TEXT NOT NULL,
            line_number     INTEGER NOT NULL DEFAULT 0,
            spec_code       TEXT NOT NULL DEFAULT '',
            description     TEXT NOT NULL DEFAULT '',
            vendor_name     TEXT NOT NULL DEFAULT '',
            unit_of_measure TEXT NOT NULL DEFAULT '',
            qty_ordered     REAL NOT NULL DEFAULT 0,
            qty_received    REAL NOT NULL DEFAULT 0,
            qty_in_transit  REAL NOT NULL DEFAULT 0,
            cost_code       TEXT NOT NULL DEFAULT '',
            project_code    TEXT NOT NULL DEFAULT '',
            project_name    TEXT NOT NULL DEFAULT '',
            line_status     TEXT NOT NULL DEFAULT '',
            expected_date   TEXT,
            ship_to         TEXT,
            assigned_agent  TEXT,
            pay_terms       TEXT,
            refreshed_at    TEXT NOT NULL
        )
    """)
    # `line_key` is Spitfire's DocItemKey — a GUID, unique across every document, so it is the
    # primary key rather than (po_number, line_number). Sub-parts really are separate lines:
    # STE-402-LT-B and STE-402-LT-SH are lines 300 and 301 of the same PO, and any key that
    # collapsed them would fabricate a quantity conflict on every shipment that splits.
    # Quantities are REAL because a PO line can order 202.5 YD.
    conn.execute("CREATE INDEX IF NOT EXISTS ix_po_lines_po ON spitfire_po_lines(po_number)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_po_lines_spec ON spitfire_po_lines(po_number, spec_code)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_po_lines_number ON spitfire_po_lines(po_number, line_number)")
    # The three match tiers, in order: exact line number, exact spec code, then fuzzy description
    # (which scans the PO's lines and needs only ix_po_lines_po).

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


# email_id lives on all of these. `extracted_records` names the column differently, and
# `released_events` has no email column at all — see below.
_EMAIL_KEYED_TABLES = (
    ("seen_message_ids", "email_id"),
    ("email_log", "email_id"),
    ("attachment_ledger", "email_id"),
    ("accumulation", "email_id"),
    ("extracted_records", "source_email_id"),
)


def forget_emails(conn: sqlite3.Connection, email_ids: Sequence[str]) -> Dict[str, int]:
    """Erase every trace of the named emails, so they can be processed again from scratch.

    The plural of `forget_message`, and the honest version of what a "reset" is for: re-running a
    fixed set of mail. It used to be `DELETE FROM` six tables, which was equivalent only for as
    long as the database held nothing but the corpus.

    Sample and live mail now live in separate files (`path_for`), so a corpus reset can no longer
    reach Premier's real mail by accident. This stays scoped by email id regardless: it is also
    the primitive `tools/split_state.py` uses to prune each store to its own rows, where deleting
    the wrong set would mean re-reading Premier's mailbox from Graph.

    `released_events` is the one table with no email column — it is keyed
    `(po_number, shipment_number)`. Its keys are read from `accumulation` *before* those rows go,
    and only keys no other email still claims are released. Miss this and the re-run is silently
    wrong in a way that looks like success: Stage 2 sees the delivery as already released, logs
    "duplicate notice", and stages nothing.

    Returns rows deleted per table, so a caller can report what it actually did.
    """
    ids = list(dict.fromkeys(email_ids))    # de-duplicated, order preserved for a stable report
    if not ids:
        return {}

    placeholders = ", ".join("?" * len(ids))
    deleted: Dict[str, int] = {}

    keys = conn.execute(
        f"SELECT DISTINCT po_number, shipment_number FROM accumulation "
        f"WHERE email_id IN ({placeholders})", ids,
    ).fetchall()

    for table, column in _EMAIL_KEYED_TABLES:
        cursor = conn.execute(
            f"DELETE FROM {table} WHERE {column} IN ({placeholders})", ids
        )
        deleted[table] = cursor.rowcount

    released = 0
    for po_number, shipment_number in keys:
        # Another email still accumulating against this key means the delivery is not ours alone
        # to un-release.
        still_claimed = conn.execute(
            "SELECT 1 FROM accumulation WHERE po_number = ? AND shipment_number IS ? LIMIT 1",
            (po_number, shipment_number),
        ).fetchone()
        if still_claimed:
            continue
        released += conn.execute(
            "DELETE FROM released_events WHERE po_number = ? AND shipment_number IS ?",
            (po_number, shipment_number),
        ).rowcount
    deleted["released_events"] = released

    # `mail_arrivals` is pointedly absent from `_EMAIL_KEYED_TABLES`. It records that a message is
    # *in the mailbox*, which forgetting our own processing of it does not change — so the row is
    # un-stamped, not deleted. Deleting would take the message off the Mail page until a future
    # poll re-listed it, and for anything older than the arrivals watermark that poll never comes.
    from pipeline import mail_arrivals
    deleted["mail_arrivals"] = mail_arrivals.clear_enrichment(conn, ids, commit=False)

    conn.commit()
    return deleted


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
    "spitfire_po_lines": [
        # Added once RelatedItemDetail was read properly: an unapproved receipt sits in
        # ReceiptInProgressUnits, not ReceivedUnits, and a mirror that cannot see it reports a
        # part-received line as fully outstanding.
        ("qty_in_transit", "REAL NOT NULL DEFAULT 0"),
    ],
    "attachment_ledger": [
        # Added when ingest started keeping the bytes rather than trusting Outlook to still have
        # them. Null means "not stored" — either a row written before the store existed, or a
        # dropped attachment whose bytes had already been released. See `pipeline.attachment_store`.
        ("blob_sha256", "TEXT"),
        ("blob_stored_at", "TEXT"),
    ],
    "email_log": [
        # When the payload was actually sent, recovered from the quoted `Sent:` header — as
        # distinct from `email_date`, which is the envelope date of the message we received.
        # Twelve of the fourteen corpus files are forwards Premier sent on one day, so dating a
        # delivery timeline from `email_date` put that same day on every stage of every PO.
        ("origin_sent_at", "TEXT"),
    ],
    "spitfire_po_index": [
        # When the purchase order was raised, from `/api/document/{id}/dates` — a *named* date
        # type, unlike `source_date`.
        #
        # Deliberately a second column rather than a reinterpretation of `source_date`, because
        # `SourceDate` was measured and rejected: 11 of the 17 mirrored POs carrying line due
        # dates have a line due BEFORE their `source_date`, and all three POs the corpus delivers
        # against were received before it. Goods due before the order exists is incoherent, so
        # whatever `SourceDate` means, it is not the order date. Left mirrored and unused rather
        # than deleted so the comparison stays reproducible.
        ("order_date", "TEXT"),
    ],
}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _LATER_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, sql_type in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


WATERMARK_KEY = "mailbox_watermark"
"""The newest `receivedDateTime` this store has settled, ISO-8601 UTC."""

ARRIVALS_WATERMARK_KEY = "arrivals_watermark"
"""How far the cheap arrival poll has listed. **Deliberately a different key.**

The arrival poll reads metadata only — it settles nothing, extracts nothing and writes no verdict.
If it advanced `WATERMARK_KEY`, the real pipeline's next listing would start after mail the
pipeline had never read, and that mail would never be processed by anything. The two answer
different questions ("what is there" vs "what have we read") and must never share a marker.
"""


def get_arrivals_watermark(conn: sqlite3.Connection) -> Optional[str]:
    return get_ingest_state(conn, ARRIVALS_WATERMARK_KEY)


def advance_arrivals_watermark(conn: sqlite3.Connection, received_at: Optional[str]) -> None:
    """Move the arrival marker forward, never back. Same monotonic rule as `advance_watermark`."""
    if not received_at:
        return
    current = get_arrivals_watermark(conn)
    if current is None or received_at > current:
        set_ingest_state(conn, ARRIVALS_WATERMARK_KEY, received_at)


def get_ingest_state(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM ingest_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_ingest_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO ingest_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def get_watermark(conn: sqlite3.Connection) -> Optional[str]:
    """How far through the mailbox we have read, so a poll can ask the server for less.

    Distinct from `seen_message_ids`, which is what makes re-processing impossible. This is a
    *cost* optimisation and nothing else: it narrows the server-side listing so a poll does not
    page through the entire Inbox to find the two messages that arrived since the last one.

    It also lifts a ceiling. Nothing is ever moved out of Premier's Inbox — every run is
    `read_only=True` — so the listing grows for ever, while `MAX_PAGES_PER_POLL` stops it at a
    thousand messages, oldest first. Past that point new mail would simply never be reached.
    """
    return get_ingest_state(conn, WATERMARK_KEY)


def advance_watermark(conn: sqlite3.Connection, received_at: Optional[str]) -> None:
    """Move the watermark forward, never back.

    Called only after a run has finished without error. A run that dies half way must re-read the
    same window next time, so the caller does not advance it on the way through.
    """
    if not received_at:
        return
    current = get_watermark(conn)
    if current is None or received_at > current:
        set_ingest_state(conn, WATERMARK_KEY, received_at)


def has_seen(conn: sqlite3.Connection, email_id: str) -> bool:
    """Has this email already been processed? Pure read — records nothing."""
    return conn.execute(
        "SELECT 1 FROM seen_message_ids WHERE email_id = ?", (email_id,)
    ).fetchone() is not None


def seen_ids(conn: sqlite3.Connection) -> set:
    """Every settled email id, as a set, for handing to a mailbox so it can skip them on the wire.

    One query instead of one per message, and the whole point is that the caller can make the
    decision *before* asking the connector for a body and its attachments. This mailbox holds
    tens of ids; if it ever holds millions, this becomes a bounded window, not a full read.
    """
    return {row[0] for row in conn.execute("SELECT email_id FROM seen_message_ids")}


def mark_seen(conn: sqlite3.Connection, email_id: str, now: str) -> None:
    """Record the id so this email is never processed again.

    Separate from `has_seen` because *when* this happens matters. The two used to be one call
    made while filtering, which meant an email was marked processed before it had been — a crash
    or Ctrl-C mid-poll left mail permanently skipped, recoverable only by naming its id to
    `forget_message`. The orchestrator now calls this once the email has reached a terminal
    verdict and that verdict is in `email_log`.

    `INSERT OR IGNORE` because a re-mark is not an error: the caller settles each email once, but
    settling twice must not raise inside a failure path.
    """
    conn.execute(
        "INSERT OR IGNORE INTO seen_message_ids (email_id, seen_at) VALUES (?, ?)", (email_id, now)
    )
    conn.commit()


def is_new_message(conn: sqlite3.Connection, email_id: str, now: str) -> bool:
    """Check-and-record in one step. Retained for callers that genuinely want both — prefer
    `has_seen` + `mark_seen` where the work between them can fail."""
    if has_seen(conn, email_id):
        return False
    mark_seen(conn, email_id, now)
    return True
