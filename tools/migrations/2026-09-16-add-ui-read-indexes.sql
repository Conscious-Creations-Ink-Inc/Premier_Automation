-- Indexes for the read paths behind /ui. No table is created, altered or dropped; no row is
-- written. Every statement is additive and reversible by the DOWN section at the foot of this file.
--
-- WRITTEN, NOT RUN — CLAUDE.md s4. A human runs this against a backed-up database.
--
--   1. Stop the app.                 (it holds WAL connections to this file)
--   2. Back up state/pipeline_state.sqlite3, and verify the backup opens.
--   3. .venv/Scripts/python.exe -c "import sqlite3;c=sqlite3.connect(r'state\pipeline_state.sqlite3');
--                                   c.executescript(open(r'tools\migrations\2026-09-16-add-ui-read-indexes.sql').read())"
--   4. Restart the app.
--
-- Expect it to take a few seconds. Measured on a byte-for-byte copy of Premier's live store
-- (2.12 GB, 2026-09-16): every index below built in under 200 ms, and together they add ~3 MB.
--
-- ---------------------------------------------------------------------------------------------
-- VERIFIED EFFECT, on that copy. Timings are the best of three runs; item counts are identical
-- before and after, which is the point — this changes how rows are found, never which rows.
--
--   read_views.manual_queue()                 846 ms  ->  156 ms   (3,611 items both ways)
--   manual_queue's NOT EXISTS(accumulation)   110 ms  ->    0 ms
--   attachments page, newest 25                42 ms  ->    0 ms
--   _events_by_po, accumulation scan           10 ms  ->    3 ms
--   _events_by_po, email_log scan               7 ms  ->    3 ms
--   records counted by status                   5 ms  ->    0 ms
--
-- manual_queue is the one that matters: `summary()` counts the queue by building it and taking
-- len(), and every page in the app renders that summary.
-- ---------------------------------------------------------------------------------------------

-- ============================== UP ==============================

-- The worst plan in the codebase. read_views.manual_queue asks
--     NOT EXISTS (SELECT 1 FROM accumulation a WHERE a.email_id = e.email_id)
-- in three separate branches (read_views.py:1663, 1686, 1724). `accumulation` had no index on
-- `email_id` alone -- it is the *last* column of both existing indexes, so neither can seek on it
-- -- and SQLite therefore planned:
--     CORRELATED SCALAR SUBQUERY 1
--       SCAN a USING COVERING INDEX ux_accumulation_delivery
-- a full index scan, once per outer row. Now:
--     SEARCH a USING COVERING INDEX ix_accumulation_email (email_id=?)
CREATE INDEX IF NOT EXISTS ix_accumulation_email
    ON accumulation(email_id);

-- The attachments page's own sort order (read_views.attachments, ORDER BY a.first_seen_at DESC,
-- a.id DESC). Without it every load planned `USE TEMP B-TREE FOR ORDER BY` over 29,766 rows --
-- and with temp_store left at its default, that sort spilled to disk. The DESC in the index
-- matches the DESC in the query so the scan can walk it directly.
CREATE INDEX IF NOT EXISTS ix_ledger_first_seen
    ON attachment_ledger(first_seen_at DESC, id DESC);

-- _events_by_po (read_views.py:1181) scans all of email_log ordered by (email_date, email_id) to
-- render a single purchase order. Both columns in the index, so it covers the sort outright:
--     SCAN email_log USING COVERING INDEX ix_email_log_date
CREATE INDEX IF NOT EXISTS ix_email_log_date
    ON email_log(email_date, email_id);

-- The same function's second scan (read_views.py:1208). Deliberately a *covering* index carrying
-- all five columns that query selects, not just the two it sorts on.
--
-- This one is not only about the sort. `accumulation.payload_json` holds 2.03 GB of the 2.12 GB
-- file, and it sits at column 6 -- so reaching any row's columns means walking that row's overflow
-- page chain. The five columns below all live *before* payload_json and fit entirely in the index,
-- so the query now never touches the table:
--     SCAN accumulation USING COVERING INDEX ix_accumulation_events
-- Cheap to hold: accumulation is only 2,341 rows.
CREATE INDEX IF NOT EXISTS ix_accumulation_events
    ON accumulation(received_at, email_id, po_number, notification_type, category);

-- `WHERE r.status IN ('pending','failed')` (read_views.py:1795) was a full scan of 5,279 rows.
CREATE INDEX IF NOT EXISTS ix_records_status
    ON extracted_records(status);

-- Grouping and lookup by purchase order, in po_verify and _records_pending. Same reasoning.
CREATE INDEX IF NOT EXISTS ix_records_po
    ON extracted_records(po_number);

-- `deliveries` carries only the autoindex on (po_number, delivery_ref), so every status filter is
-- a full scan. Small table today; this keeps it from becoming the next one of these.
CREATE INDEX IF NOT EXISTS ix_deliveries_status
    ON deliveries(status, created_at);

-- ANALYZE so the planner has real selectivity figures for the new indexes rather than guessing.
-- Writes only to the sqlite_stat1 table; no user data is touched.
ANALYZE;

-- ============================= DOWN =============================
-- To reverse, run these seven statements. Dropping an index cannot lose data -- the worst case is
-- that the queries above return to the plans and timings recorded at the top of this file.
--
--   DROP INDEX IF EXISTS ix_accumulation_email;
--   DROP INDEX IF EXISTS ix_ledger_first_seen;
--   DROP INDEX IF EXISTS ix_email_log_date;
--   DROP INDEX IF EXISTS ix_accumulation_events;
--   DROP INDEX IF EXISTS ix_records_status;
--   DROP INDEX IF EXISTS ix_records_po;
--   DROP INDEX IF EXISTS ix_deliveries_status;
--   ANALYZE;
