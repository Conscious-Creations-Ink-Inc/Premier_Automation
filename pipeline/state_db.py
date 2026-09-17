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
        # Read-side pragmas. None of them changes what is stored, only how it is reached.
        #
        # `synchronous = NORMAL` is the documented pairing for WAL: a commit still goes to the log,
        # it just stops fsyncing on every one. `temp_store = MEMORY` is the one that matters most
        # here — several read paths plan as `USE TEMP B-TREE FOR ORDER BY`, and those sorts were
        # spilling to disk. `mmap_size` lets the OS page cache carry pages between connections,
        # which a per-request connection cannot do with `cache_size` alone: its cache dies with it.
        # `cache_size` is deliberately modest for that reason — it is per connection, and uvicorn
        # runs sync endpoints on a 40-thread pool.
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -16384")      # 16 MB, per connection
        conn.execute("PRAGMA mmap_size = 268435456")    # 256 MB, shared via the OS page cache

    _ensure_schema_once(conn, db_path)
    return conn


_SCHEMA_READY = set()
"""Paths whose schema this process has already built. See `_ensure_schema_once`."""


def reset_schema_cache() -> None:
    """Forget what `_ensure_schema_once` has built, so the next open rebuilds it.

    For tests, which make and discard stores at the same temporary paths — without this, the
    second store at a reused path would inherit the first one's "already built" marker and come
    back empty.
    """
    _SCHEMA_READY.clear()


def _ensure_schema_once(conn: sqlite3.Connection, db_path) -> None:
    """Build the schema the first time this process opens a given file, and not again.

    `get_connection` used to run the whole of `_ensure_schema` on **every** open: 19 CREATE TABLEs,
    22 CREATE INDEXes, a `PRAGMA table_info` per table in `_LATER_COLUMNS`, and a commit — 43
    statements. Every one of them is `IF NOT EXISTS` and so a no-op after the first, but they are
    not free: a single `/ui/mails` render opens at least four connections, and each open therefore
    took a **write** transaction against a 2.27 GB file that the scheduler thread may be writing at
    the same moment. A read request should not need the write lock.

    This changes nothing about the schema itself — it skips DDL that was already a no-op. The file
    is still built on first open, so a fresh store still renders empty rather than 500ing.

    `:memory:` is never remembered: each such connection is its own private database, so the second
    one would find an empty file and a marker saying it had been built.
    """
    key = None if db_path == ":memory:" else str(db_path)
    if key is not None and key in _SCHEMA_READY:
        return
    _ensure_schema(conn)
    if key is not None:
        _SCHEMA_READY.add(key)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Every table, index and late-added column this store needs. Idempotent by construction."""
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
    # One copy of each message's serialized payload, keyed on the message rather than on the
    # delivery.
    #
    # `accumulation` carries `payload_json` per (po_number, shipment, email) row, so a message
    # naming N purchase orders stored its whole base64 body N times. Measured on Premier's live
    # store: 1,938 MB of payload where 666 MB of distinct content exists — **2.9x** — and one
    # expediting report naming 74 POs held 173 MB by itself. Every copy is byte-identical; the row
    # differs only in which delivery it belongs to.
    #
    # Additive and backward compatible on purpose. Rows written before this table exists keep their
    # payload in `accumulation.payload_json` and are still read from there; new rows write the
    # payload here once and leave that column empty. `_bundle_for_key` coalesces the two, so the
    # data migration that moves the backlog is a space reclaim, never a correctness fix, and can be
    # run whenever a human chooses. See `tools/dedupe_accumulation_payloads.py`.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS accumulation_payload (
            email_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL
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
    # One row per physical delivery: a purchase order, and which arrival of it this is.
    #
    # `extracted_records` deliberately stays the *child* here, one row per item line, because 30
    # read sites across 27 files already select from it. Moving the item columns out would break
    # every one of them at once; adding the parent beside it breaks none, and the shape either way
    # is the same graph — one delivery, N items.
    #
    # `(po_number, delivery_ref)` is unique and is the same key Stage 2 accumulates on, so a
    # delivery has exactly one row here however many messages described it.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS deliveries (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            po_number           TEXT NOT NULL,
            delivery_ref        TEXT NOT NULL,
            delivery_rung       TEXT NOT NULL DEFAULT '',
            shipment_number     TEXT,
            notification_number TEXT,
            source_email_id     TEXT NOT NULL DEFAULT '',
            pod_stated_date     TEXT,
            received_by         TEXT,
            carrier_name        TEXT,
            tracking_number     TEXT,
            delivery_location   TEXT,
            vendor_name         TEXT,
            email_date          TEXT,
            extraction_source   TEXT NOT NULL DEFAULT '',
            status              TEXT NOT NULL DEFAULT 'pending',
            created_at          TEXT NOT NULL,
            updated_at          TEXT,
            UNIQUE (po_number, delivery_ref)
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
        CREATE TABLE IF NOT EXISTS suppressed_notices (
            email_id      TEXT NOT NULL,
            po_number     TEXT NOT NULL,
            delivery_ref  TEXT NOT NULL DEFAULT '',
            released_at   TEXT,
            suppressed_at TEXT NOT NULL,
            reason        TEXT NOT NULL,
            PRIMARY KEY (email_id, po_number, delivery_ref)
        )
    """)
    # A notice correctly *not* accumulated, and why. Shaped like `released_events` above, and
    # deliberately not folded into `accumulation`: `_bundle_for_key` and `sweep_stale_holds` read
    # that table, so a suppression row there would re-enter the release algorithm it was excluded
    # from.
    #
    # Stage 2 skips a notice whose delivery has already been released — correct, and the whole
    # reason one physical delivery does not become two receivers. But the skip wrote nothing except
    # a log line, so the email had no accumulation row and no record, which is exactly the shape
    # `read_views.manual_queue` sweeps into its residual bucket. Two of Premier's messages sat in
    # "Nothing extracted" under the sentence "no PO, spec, quantity or POD was recovered from the
    # body or any attachment" — every clause of which was false. A suppression nobody can inspect
    # is a suppression nobody can trust.
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
    # The inner side of `mail_arrivals._PENDING_WHERE`, which cannot join on `email_id` itself:
    # Graph hands the arrival watch a message id truncated to 255 characters and the pipeline the
    # full one, so the two tables are compared on `substr(email_id, 1, 255)`. An expression the
    # primary key does not cover means a full scan of `email_log` per arrival row — measured on the
    # live store at **925ms against 0.7ms**, on the query `/ui/version` runs every ten seconds in
    # every open tab. With this index the planner goes back to a SEARCH and it is 0.9ms.
    #
    # **Not UNIQUE.** It would be true of every store today, and it would raise inside
    # `get_connection` on the first store that ever holds both a truncated and a full id for one
    # message — taking down every page, every tool and the watch at once. The uniqueness this
    # column has is already enforced by its primary key.
    conn.execute("CREATE INDEX IF NOT EXISTS ix_email_log_id_key "
                 "ON email_log(substr(email_id, 1, 255))")

    # A person's answer to the question `email_log.not_a_delivery` answers by rule, kept beside the
    # machine verdict it overrules. Triage is the only thing that could classify a message, and it is
    # sometimes wrong in both directions — a scheduled report is not a delivery, and neither is every
    # thread a rule read as one.
    #
    # **Its own table rather than a column on `email_log`,** because `email_log.record` is
    # `INSERT OR REPLACE` over a fixed column tuple: anything outside that tuple is wiped the next
    # time the message is processed. `not_a_delivery` is *derived* by `stage1_triage` on every pass
    # and must stay that way; a human decision that vanished on the next run would be worse than no
    # decision at all.
    #
    # `email_id` as the primary key is the reversibility: one current answer per message, and
    # flipping it is an upsert over the same row. Deliberately not history — the same call
    # `email_log` makes above, and "who called this a delivery in August" needs an events table, not
    # this one. No index beyond the key: every read is a point lookup or a scan of tens of rows. No
    # foreign key either — nothing else in this schema declares one, and `forget_emails` deletes in
    # a fixed table order that a live constraint would start caring about.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mail_overrides (
            email_id   TEXT PRIMARY KEY,
            verdict    TEXT NOT NULL,
            decided_by TEXT NOT NULL,
            note       TEXT NOT NULL DEFAULT '',
            decided_at TEXT NOT NULL
        )
    """)

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

    # Everything a reader saw in one document, before it was narrowed to `extracted_records`.
    #
    # `extracted_records` is a claim about a delivery, shaped by what a Spitfire receipt needs. A
    # document is not that shape: an Atlas Warehouse Receiving Report OCRs into nine tables holding
    # `Customer PO #`, `Received by`, `BOL/PRO`, `Total Received` and a 22-row line-item grid, and
    # the extraction mapped one table onto six fields and discarded the rest. The read had worked;
    # the data was dropped on the way out, which looks exactly like never having read it.
    #
    # Keyed on `ledger_id` so a re-read replaces what a document said rather than accumulating
    # opinions, and so the join to the attachment (and through it the email) is free. See
    # `pipeline/parsed_documents.py`.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS parsed_documents (
            ledger_id      INTEGER PRIMARY KEY,
            email_id       TEXT NOT NULL DEFAULT '',
            filename       TEXT NOT NULL DEFAULT '',
            container_path TEXT NOT NULL DEFAULT '',
            adapter        TEXT NOT NULL DEFAULT '',
            raw_text       TEXT NOT NULL DEFAULT '',
            tables_json    TEXT NOT NULL DEFAULT '[]',
            table_count    INTEGER NOT NULL DEFAULT 0,
            row_count      INTEGER NOT NULL DEFAULT 0,
            char_count     INTEGER NOT NULL DEFAULT 0,
            content_sha256 TEXT NOT NULL DEFAULT '',
            parsed_at      TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_parsed_documents_email ON parsed_documents(email_id)")

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
    # PO number -> DocMasterKey, the gap nothing else crosses. Emails name a PO ("912456"); every
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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS spitfire_post (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key   TEXT NOT NULL UNIQUE,
            record_id         INTEGER NOT NULL,
            po_number         TEXT NOT NULL,
            line_number       INTEGER,
            pod_md5           TEXT NOT NULL DEFAULT '',
            state             TEXT NOT NULL,
            detail            TEXT NOT NULL DEFAULT '',
            project_code      TEXT NOT NULL DEFAULT '',
            receipt_key       TEXT NOT NULL DEFAULT '',
            receipt_doc_no    TEXT NOT NULL DEFAULT '',
            pod_file_key      TEXT NOT NULL DEFAULT '',
            report_file_key   TEXT NOT NULL DEFAULT '',
            quantity          REAL,
            actor             TEXT NOT NULL DEFAULT '',
            audit_json        TEXT NOT NULL DEFAULT '',
            claimed_at        TEXT NOT NULL,
            settled_at        TEXT,
            attempts          INTEGER NOT NULL DEFAULT 1,
            last_attempt_at   TEXT
        )
    """)
    # The guard against posting the same delivery twice, and it has to be ours: Spitfire offers
    # nothing to lean on. The catalog does not deduplicate — measured 2026-08-14, the same
    # 37,352-byte PDF uploaded twice produced two fileKeys and two catalog entries — re-sending an
    # attach creates a second row, and `ReceiptInProgressUnits` reads 0.0 on a PO that already has
    # an unapproved receipt against it, so the one field that looks like a duplicate check is
    # blind for the whole approval window.
    #
    # `idempotency_key` is UNIQUE and the row is INSERTed *before* the first call, so the claim is
    # what reserves the work. A crash between claiming and posting leaves a row stuck at CLAIMED,
    # which is recoverable by reading Spitfire back — whereas recording success afterwards would
    # lose the receipt entirely and post it again on the next attempt.
    conn.execute("CREATE INDEX IF NOT EXISTS ix_spitfire_post_record ON spitfire_post(record_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_spitfire_post_po ON spitfire_post(po_number, state)")

    # Every field a person changed by hand, one row per field. `spitfire_post` records what we sent
    # to Premier's ERP; nothing recorded what we changed *before* sending it, so a quantity that
    # arrived one way and posted another had no trail at all between the two. `extracted_records`
    # carries `updated_at` and `created_by`, which say that somebody touched the row and who — not
    # which field, and never what it used to hold.
    #
    # Values are TEXT on both sides, including the numeric ones. This is a log of what was in the
    # box and what replaced it, not a second copy of the column — and a REAL here would make a
    # cleared field and a zero indistinguishable, which is exactly the distinction
    # `completeness._is_present` exists to preserve.
    #
    # Deliberately **not** in `_EMAIL_KEYED_TABLES`: `forget_emails` erases our processing of a
    # message so it can be read again, and the fact that a named person changed a value is not our
    # processing. `extracted_records.id` is AUTOINCREMENT, so ids are never reused and an orphaned
    # edit row can never be misattributed to a later record.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS record_edits (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id  INTEGER NOT NULL,
            field      TEXT    NOT NULL,
            old_value  TEXT,
            new_value  TEXT,
            edited_by  TEXT    NOT NULL,
            edited_at  TEXT    NOT NULL
        )
    """)
    # Composite so "this record's edits, newest first" is answered from the index alone. The edit
    # form asks that once per render and nothing asks anything else of this table.
    conn.execute("CREATE INDEX IF NOT EXISTS ix_record_edits_record "
                 "ON record_edits(record_id, id DESC)")

    _add_missing_columns(conn)
    # These index columns that `_LATER_COLUMNS` adds, so they can only be created once the ALTERs
    # above have run — a store written before those columns existed has neither.
    conn.execute("CREATE INDEX IF NOT EXISTS ix_records_delivery_key "
                 "ON extracted_records(delivery_key)")
    # How the UI collapses twenty ledger rows back into the one receipt they describe — see
    # `group_key` in `_LATER_COLUMNS`.
    conn.execute("CREATE INDEX IF NOT EXISTS ix_spitfire_post_group ON spitfire_post(group_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_email_log_fingerprint ON email_log(fingerprint)")
    # The real uniqueness key for both accumulation tables, replacing declared primary keys that
    # do not hold. Both were `(po_number, shipment_number, …)`, and SQLite treats NULLs as distinct
    # in a unique index — so with no shipment number stated, which is 22 of the 28 released events
    # in Premier's live store, `INSERT OR IGNORE` inserted a second row instead of ignoring it and
    # `_is_released` collapsed every delivery on a PO into one. `delivery_ref` is never NULL
    # (`dedupe.delivery_ref` always returns a value), so these indexes bite where the PKs did not.
    #
    # A unique index rather than a table rebuild: `_LATER_COLUMNS` can add a column but cannot
    # change a primary key, and `INSERT OR IGNORE` honours any unique constraint, not only the PK.
    # Rows written before the column existed carry NULL and so are exempt, which is what lets this
    # be created against an existing store without failing.
    conn.execute("CREATE INDEX IF NOT EXISTS ix_records_delivery_id "
                 "ON extracted_records(delivery_id)")
    # "Which records came off this email?" — asked once per row by the attachments page, and once
    # per email by the manual queue's duplicate and no-record branches. Without it each of those
    # is a full scan of `extracted_records`, so the cost is rows x records: on Premier's live
    # store the attachments page spent 37.9 of its 38 seconds here, and the duplicate query took
    # 4.8s to return three rows. Both are correlated subqueries, which no amount of rewriting the
    # outer query helps.
    conn.execute("CREATE INDEX IF NOT EXISTS ix_records_source_email "
                 "ON extracted_records(source_email_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_accumulation_delivery "
                 "ON accumulation(po_number, delivery_ref, email_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_released_delivery "
                 "ON released_events(po_number, delivery_ref)")
    conn.commit()


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
    # Without this a reprocess leaves the delivery rows behind while their item lines are erased,
    # and the next pass finds a delivery that already exists and appears to have nothing in it.
    ("deliveries", "source_email_id"),
    # Included, unlike `mail_arrivals.acknowledged_at` below, and the difference is worth stating.
    # An acknowledgement says "the mailbox no longer has this", which forgetting our own processing
    # cannot make untrue. An override says "triage classified this wrongly", which is the very thing
    # a reprocess exists to make untrue — the usual reason to reprocess is that a rule was fixed, and
    # that is exactly when somebody had flagged a message by hand as a workaround. A stale
    # `not_delivery` override surviving that would go on hiding real delivery mail for ever, silently.
    # Dropping it costs one re-flag and is loud: the message reappears on the queue, and
    # `forget_emails` reports the row it deleted.
    ("mail_overrides", "email_id"),
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
    "mail_attachment": [
        # Which blob in the content-addressed store this row's bytes are, so `content` need not be
        # a second copy of them. 205 cached rows held 41.8 MB for 30.8 MB of distinct content --
        # inline images at 4.43x, real attachments at 1.001x -- and 81 of the 83 hashes were
        # already on disk. Outlook mints a fresh random filename per message for the same logo, so
        # only the hash finds this.
        #
        # A pointer rather than nothing at all: `mail_view._from_graph` recovers messages ingest
        # never saw, which have no ledger row for `resolve` to fall back to, and that fallback
        # matches on *ordinal* -- `connectors/mailbox.py` skips an attachment whose fetch raised,
        # which shifts every later one. A hash cannot drift that way.
        ("content_sha256", "TEXT"),
    ],
    # Which physical delivery a row is about, from `dedupe.delivery_ref`. Added rather than
    # replacing `shipment_number`, which stays because it is what a person reads and what the A↔B
    # join is stated in — the ref is derived from it when it exists and from four weaker things
    # when it does not. `delivery_rung` records *which* of those answered, so a delivery identified
    # only by its message id can be flagged rather than quietly presented as a join.
    "accumulation": [
        ("delivery_ref", "TEXT"),
        ("delivery_rung", "TEXT"),
    ],
    "released_events": [
        ("delivery_ref", "TEXT"),
        ("delivery_rung", "TEXT"),
    ],
    "extracted_records": [
        # Which delivery this item line belongs to — `deliveries.id`. Nullable because every row
        # written before the parent table existed has no delivery to point at, and because a record
        # a person creates by hand may name one that was never accumulated. Stage 6 backfills them.
        ("delivery_id", "INTEGER"),
        ("po_line_number", "INTEGER"),
        ("received_by", "TEXT"),
        ("package_quantity", "REAL"),
        ("package_uom", "TEXT"),
        ("notification_number", "TEXT"),
        # What the paperwork said was ordered, kept strictly apart from what arrived. See
        # `models.ExtractedRecord.quantity_ordered` — a request grid's `Qty` column lands here, and
        # `quantity_received` stays null until something actually evidences a receipt.
        ("quantity_ordered", "REAL"),
        # --- provenance -----------------------------------------------------------------------
        # How this row came to exist. `auto` is every record Stage 3 staged; `manual` is one a
        # person built from a message the pipeline could not finish. Written once at creation and
        # never edited, so a report regenerated months later still says how the record was made.
        #
        # Deliberately separate from `extraction_source`, which names the *adapter* that read the
        # bytes ("html_table", "ocr", "authority") and is free text. Overloading it would make the
        # origin unqueryable and would break the moment an adapter is renamed.
        ("origin", "TEXT NOT NULL DEFAULT 'auto'"),
        ("created_by", "TEXT"),
        ("manual_note", "TEXT"),
        # --- which file is this record's proof of delivery -------------------------------------
        # `attachment_ledger.id` of the attachment a reviewer chose. Null means nobody chose one
        # and `spitfire_post._pod_for` decides by reading the files, exactly as it always has.
        #
        # This is the only way an image POD can be used: `_pod_for` re-reads PDFs on the request
        # path and skips everything else, so a photographed BOL the ingest-time OCR never flagged
        # is invisible to it until a person points at it.
        ("pod_ledger_id", "INTEGER"),
        # `attachment_ledger.id` of the attachment this record was *read from*. Null for a record
        # read from the email body. When no carrier POD resolves, `spitfire_post._pod_for` attaches
        # this document as the proof: the receipt then carries the paper its numbers came from.
        ("source_ledger_id", "INTEGER"),
        # 'attachment' | 'email_body'. Recorded rather than inferred from `pod_ledger_id` being
        # null, because "nobody has looked yet" and "a person looked and there was nothing to
        # choose" are different states and only the second may be waived.
        ("pod_source", "TEXT"),
        # Who accepted that this delivery may post with no proof document attached, and when.
        #
        # **This is the only thing that lets a record with no POD reach Spitfire.** Automation
        # cannot set it — `post_decision` refuses a POD-less record outright unless a named person
        # has waived it here. Null on every row that existed before this column, so nothing already
        # in the store is retroactively permitted.
        ("pod_waived_by", "TEXT"),
        ("pod_waived_at", "TEXT"),
        # --- duplicate guard -------------------------------------------------------------------
        # The delivery this row describes, hashed. Two rows sharing it are the same physical
        # delivery read twice, whether from two emails or from an email and a person. See
        # `pipeline.dedupe`.
        ("delivery_key", "TEXT"),
    ],
    "spitfire_po_lines": [
        # Added once RelatedItemDetail was read properly: an unapproved receipt sits in
        # ReceiptInProgressUnits, not ReceivedUnits, and a mirror that cannot see it reports a
        # part-received line as fully outstanding.
        ("qty_in_transit", "REAL NOT NULL DEFAULT 0"),
    ],
    "spitfire_post": [
        # Added with the FLAGGED state. A refusal is recorded rather than returned, and pressing
        # Post five times on an unchanged record must leave one row saying "refused, 5x" instead
        # of five rows — so the count and the latest time live on the row itself.
        ("attempts", "INTEGER NOT NULL DEFAULT 1"),
        ("last_attempt_at", "TEXT"),
        # Which *one* attempt to build a receipt these rows were part of. A delivery of twenty item
        # lines is one receipt with twenty rows here — the grain stays per record, because thirty
        # readers key on `record_id` — and this is what says the twenty belong together.
        #
        # Minted per `post_delivery_pod` call, never derived from `delivery_id`: a line fixed after
        # the first receipt posts becomes a *second* receipt on the same delivery, so one delivery
        # legitimately owns several groups over its life. It earns its place in the one case
        # `receipt_key` cannot cover — a group that failed before the receipt existed, where there
        # is no receipt GUID to link the rows by.
        ("group_key", "TEXT"),
    ],
    "attachment_ledger": [
        # Added when ingest started keeping the bytes rather than trusting Outlook to still have
        # them. Null means "not stored" — either a row written before the store existed, or a
        # dropped attachment whose bytes had already been released. See `pipeline.attachment_store`.
        ("blob_sha256", "TEXT"),
        # When this row's bytes were released on purpose, as opposed to never stored or lost.
        # Without it the three are indistinguishable: `verify_attachments` would report every
        # reclaimed logo as `missing_file` and exit 1, and `backfill_attachments` would re-fetch
        # all of them from Outlook and put them straight back.
        ("blob_reclaimed_at", "TEXT"),
        ("blob_stored_at", "TEXT"),
        # Whether this attachment *is* the proof of delivery, decided once at ingest by whichever
        # adapter read it, and by its content rather than its file type. A POD arrives as a PDF, a
        # phone photo, a scan inside a .docx — the type says nothing. What settles it is that the
        # carrier-POD grammar in `parsing/pod.py` recognised the text, whether that text came from
        # a PDF layer or from OCR.
        #
        # Persisted rather than recomputed because the answer for an image costs a paid OCR call:
        # `stage3_extract` already pays it once on the way in, and `spitfire_post._pod_for` must
        # not pay it again every time somebody opens the Records page. The parsed facts ride along
        # so the delivery date and signature can be read back without touching the bytes at all.
        ("is_pod", "INTEGER NOT NULL DEFAULT 0"),
        ("pod_po_numbers", "TEXT NOT NULL DEFAULT ''"),
        ("pod_delivery_date", "TEXT"),
        ("pod_signed_by", "TEXT"),
        # The other question a reader answers on its way past a tabular document: is this a record
        # of goods arriving, or one of Premier's own worklists? `parsing/receipt.classify_grid` has
        # always decided it — the gate in `grid_reader` is what stops an expediting sheet minting
        # receipts — and the verdict was thrown away the instant it was used. So a message carrying
        # `DBR025PB100002 Expediting Report 09.10.2026.xlsx` reached the queue saying only that
        # nothing was extractable from it, which is true and useless: nothing ever will be, because
        # the document is a status tracker and says nothing arrived.
        #
        # `status_report_reason` holds `classify_grid`'s own sentence — "a status tracker — 22
        # lifecycle columns including Estimated Delivery (Date), Target Ship Date" — so the queue
        # can show a person why, in the words of the rule that decided it.
        #
        # Stored rather than derived, unlike `email_log.not_a_delivery`, for one reason: deriving
        # it means reopening the workbook, and `manual_queue` builds four thousand items a render.
        # It stays inspectable because the reason travels with the flag.
        ("is_status_report", "INTEGER NOT NULL DEFAULT 0"),
        ("status_report_reason", "TEXT NOT NULL DEFAULT ''"),
    ],
    "email_log": [
        # When the payload was actually sent, recovered from the quoted `Sent:` header — as
        # distinct from `email_date`, which is the envelope date of the message we received.
        # Twelve of the fourteen corpus files are forwards Premier sent on one day, so dating a
        # delivery timeline from `email_date` put that same day on every stage of every PO.
        ("origin_sent_at", "TEXT"),
        # --- duplicate detection ---------------------------------------------------------------
        # A hash of what the message *says*, not of the envelope it came in. `seen_message_ids`
        # keys on `internetMessageId`, which a vendor re-send or a second expeditor's forward does
        # not share — so the same delivery notification ingests twice and stages two records.
        # See `pipeline.dedupe.fingerprint`.
        ("fingerprint", "TEXT"),
        # The `email_id` of the message this one duplicates. Set rather than the row being dropped:
        # a suppression nobody can inspect is a suppression nobody can trust, which is the same
        # reason the internal-chatter rule lists what it filtered instead of hiding it.
        ("duplicate_of", "TEXT"),
        # A person built a record from this message, so the extraction loop must not stage more
        # from it. Scoped to this one message — never to its thread and never to its PO, because
        # the next mail on the same thread may be a genuinely separate delivery.
        ("handled_manually", "INTEGER NOT NULL DEFAULT 0"),
        # Positively identified as not a delivery notification — see
        # `stage1_triage.NOT_A_DELIVERY_RULES`. Distinct from "unrecognised": a scheduled report and
        # a mail nobody could classify both used to land in the same queue with the same weight, and
        # one arrival of the 2am pay-request report staged 126 records off the back of it.
        ("not_a_delivery", "INTEGER NOT NULL DEFAULT 0"),
        # Which mailbox folder this message was read from — `inbox` or `junkemail`. Distinct from
        # `folder`, which is where the pipeline *put* it afterwards.
        #
        # Recorded because reading Junk without saying so would hide the thing worth knowing.
        # Exchange junked an Authority Inbound Notification for a live PO; pulling it in silently
        # fixes this pipeline and leaves nobody able to see that Premier's tenant is filing real
        # delivery mail as spam, which is where the actual fix belongs. Empty on every row written
        # before the column, and on connectors that have no folders.
        ("source_folder", "TEXT NOT NULL DEFAULT ''"),
    ],
    "mail_arrivals": [
        # Same as `email_log.source_folder`, on the table the fifteen-second watch writes — so a
        # junked message is identifiable on the Mail page from the moment it lands, rather than
        # only once the full pass has read it forty minutes later.
        ("source_folder", "TEXT NOT NULL DEFAULT ''"),
        # When a by-id recovery looked for this message and the mailbox no longer had it.
        #
        # **Not** a "has it been read" flag — that stays a join against `email_log`, because a
        # duplicated status column drifts from the verdicts it claims to summarise. This records
        # something the join cannot know: that we went looking by id and there was nothing there.
        #
        # Without it a deleted message stays on the recovery list for ever. It would be re-fetched
        # every run at one Graph call each, and — worse — the "arrived but never read" count could
        # never reach zero, so the run-health warning built on that count would be permanently
        # lit. A health signal that cannot clear is a health signal nobody reads.
        ("recovery_missing_at", "TEXT"),
        # When a person accepted that this message is lost.
        #
        # Only ever set on a row that already carries `recovery_missing_at`: the action is
        # "I accept this one is gone", not "hide this from me". Mail still sitting in the mailbox
        # cannot be acknowledged away, because the next run will read it and the row will clear
        # itself.
        #
        # It exists because the alternative is a screen nobody can finish. A lost message is a real
        # event — one of Premier's was an urgent purchase-order email — so it must not vanish on
        # its own, and it equally must not sit on the page for ever as work that cannot be done.
        # Acknowledging is the only thing that can end it, and it is a person's call, which is why
        # nothing in the pipeline writes this column.
        ("acknowledged_at", "TEXT"),
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
"""How far the cheap arrival poll has listed **the Inbox**. **Deliberately a different key.**

The arrival poll reads metadata only — it settles nothing, extracts nothing and writes no verdict.
If it advanced `WATERMARK_KEY`, the real pipeline's next listing would start after mail the
pipeline had never read, and that mail would never be processed by anything. The two answer
different questions ("what is there" vs "what have we read") and must never share a marker.

Kept as the legacy key. Every folder now has its own — see `arrivals_watermark_key` — and this
one is what `inbox` falls back to, because the Inbox is the only folder it ever described.
"""


def arrivals_watermark_key(folder: str) -> str:
    """`arrivals_watermark:junkemail`. One marker per source folder, never one shared.

    The arrival poll is a single un-paginated GET per folder — `$top=50`, newest first — so a
    folder that fills its page has mail below the cut that was never listed. A single marker
    advanced to `max()` across folders would move past that mail the moment *any other* folder
    returned something newer, and the overlap window is five minutes, not long enough to save it.
    Per-folder markers make each folder's progress its own business.
    """
    return f"{ARRIVALS_WATERMARK_KEY}:{folder}"


def get_arrivals_watermark(conn: sqlite3.Connection, folder: str) -> Optional[str]:
    """How far this folder has been listed, or None to list its newest page and start there.

    **None is the right answer for a folder we have not read before, and the legacy value is not.**
    When Junk was added its backlog was one message received at 11:28:30Z while the legacy marker
    already stood at 11:42:03Z — inheriting it would have put the one mail that prompted the whole
    change permanently behind the window, and the poll would have reported success. So only
    `inbox` falls back, because `ARRIVALS_WATERMARK_KEY` has only ever meant the Inbox.
    """
    own = get_ingest_state(conn, arrivals_watermark_key(folder))
    if own is not None:
        return own
    return get_ingest_state(conn, ARRIVALS_WATERMARK_KEY) if folder == "inbox" else None


def advance_arrivals_watermark(conn: sqlite3.Connection, folder: str,
                               received_at: Optional[str]) -> None:
    """Move one folder's arrival marker forward, never back. Same monotonic rule as
    `advance_watermark`."""
    if not received_at:
        return
    current = get_arrivals_watermark(conn, folder)
    if current is None or received_at > current:
        set_ingest_state(conn, arrivals_watermark_key(folder), received_at)


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
