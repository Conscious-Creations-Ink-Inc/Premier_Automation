# Code Analysis Findings — 2026-08-02

End-to-end trace of the orchestrator (stages 1–3 executed against `sample_data/`, full test
suite run: 90 passed / 3 skipped). **Report only — none of these are fixed yet.** Findings
marked **[NEW]** are not in the tech-debt register of `PREMIER_AUTOMATION.md`.

## Verdict

Stages 1→3 (ingest → triage → accumulate → extract) are genuinely implemented and work.
Stages 4–7 are one-line stubs; their logic exists only as mocks in `api/services/` running on
synthetic demo data. There is no production entry point (`run_pipeline.py` is a stub) and no
scheduler. `.msg` Outlook-file parsing does not exist anywhere — ingestion is Graph API or
hand-written JSON fixtures only.

## Correctness bugs

- **C1 [NEW] Multi-PO emails duplicate every extracted record, once per PO** —
  `pipeline/ingest_orchestrator.py:158-164` re-runs the full adapter cascade over all sources
  for each released `DeliveryEvent` and never filters records by `event.key.po_number`.
  Reproduced: 1 email with rows for 2 POs → 4 staged records (expected 2). Double-receipt risk.
- **C2 [NEW] `LocalFolderMailbox.mark_processed` silently no-ops** unless the filename equals
  the `email_id` — `connectors/mailbox.py:63-65`. Shipped fixtures don't follow that naming, so
  `Processed/`/`Routed/` end up empty and files are re-scanned.
- **C3 [NEW] `pod_stated_date`, `carrier_name`, `tracking_number`, `delivery_location` are
  hardcoded `None` in every adapter** (`stage3_extract/base.py:183-187` etc.), yet
  `api/services/reconcile.py:25` requires `pod_stated_date` and `decisions.py:184-189` builds
  the Spitfire receipt from carrier/tracking. Every real record would flag "missing POD date"
  and stage a receipt with no carrier/PRO.
- **C4 [NEW] `PO_TOKEN_REGEX` is case-sensitive and format-brittle**
  (`config/settings.py:27`): `po 213987`, `PO# 213987`, `P.O. 213987`, `PO: 213987` all miss →
  fall through to rule 6 → exception route.
- **C5 [NEW] Graph folder-scoped `id` used as the dedupe key** (`connectors/mailbox.py:132`)
  instead of `internetMessageId` (not even in `$select`). Moving a message changes its id →
  reprocessing loops possible.
- **C6 Triage sender domains are invented placeholders** (`config/settings.py:22-26`). Against
  the real mailbox nearly all mail falls to rule 6 → ROUTE. Biggest go-live blocker.
- **C7 [NEW] `medium` (2-signal) matches auto-approve** — `api/services/reconcile.py:139`
  omits `"medium"` from the flagged set, contradicting the module docstring and API description.

## Error handling / durability

- **C8 One malformed email aborts the whole poll** — mailbox `fetch_new` has no per-item
  try/except and is called outside the orchestrator's per-email try
  (`ingest_orchestrator.py:138`). Also `settings.SAMPLE_ATTACHMENTS_DIR` points at
  `sample_data/attachments/`, which does not exist.
- **C9 Emails are marked seen before they are processed** (`state_db.py:75-76` commits during
  fetch). A crash between fetch and accumulate loses those emails permanently.
- **C10 [NEW] An adapter exception aborts the remaining cascade** rather than falling through
  to the next adapter (`ingest_orchestrator.py:55-60`), contrary to its own docstring.
- **C11 Graph pagination missing** — `$top=50`, `@odata.nextLink` ignored
  (`connectors/mailbox.py:120-123`). Silent loss above 50 messages/poll.
- **C12 [NEW] `itemAttachment` dropped** — only `fileAttachment` accepted
  (`connectors/mailbox.py:148`); forwarded emails lose their payload with no log line.
- **C13 [NEW] Narrow OCR image allowlist** (`ocr_adapter.py:18`) — no jpg/heic/webp/octet-stream
  fallback; dispatch is content-type-only, filename never consulted.

## Config / startup

- **C14 `.env.example` incomplete** — missing `API_HOST`, `API_PORT`, `CORS_ORIGINS`,
  `DEFAULT_OPERATOR` (all defaulted, no crash).
- **C15 `.env` self-contradicts** ("leave blank" comment vs populated mailbox) and holds a live
  Graph client secret in plaintext (gitignored, but see also the unprotected copy in
  `D:\Premier\graph_probe\.env`).
- **C16 Hardcoded Tesseract path** (`config/settings.py:66`) — reason 3 tests skip.
- **C18 README was materially false** (fixed 2026-08-02 by the structure cleanup).

## Dead / unused

- Settings with zero code references: `SAMPLE_EMAILS_DIR`, `SAMPLE_ATTACHMENTS_DIR`,
  `PO_LINES_SEED_FILE`, `SPITFIRE_MOCK_DB_PATH`, `STALE_HOLD_THRESHOLD_DAYS`,
  `CBD_ADR_RECEIPT_WINDOW_DAYS`, `ANTHROPIC_MODEL`, `INGEST_ORCHESTRATOR_INTERVAL_MINUTES`,
  `MATCH_ORCHESTRATOR_SCHEDULE`.
- `sample_data/po_lines.json` is `[]` and never read.
- Default OCR client is the Mock, which returns an empty result — image/scan attachments
  silently extract nothing out of the box. `RealDocumentIntelligenceClient` raises
  `NotImplementedError` (Azure not provisioned); same for the AI fallback
  (`ai_fallback.py:16`, disabled via `ENABLE_AI_FALLBACK=False`).
- `frontend/package.json` `gen:api` writes `src/types/api.d.ts` but the real file is
  hand-written `src/types.ts` — the two will drift.
- `api/main.py` never mounts `frontend/dist`.

## Structural (addressed by the 2026-08-02 cleanup)

Untracked `api/`/`frontend/`/`docs/`/`tests/api/` committed; `.gitignore` extended
(node_modules, dist, sqlite, editors); `pyproject.toml` added (pytest config moved from
pytest.ini); requirements split runtime vs dev; 5 empty test files deleted; docs binaries moved
to `docs/assets/`; README rewritten. **Not** done (deliberately — no code edits): moving mock
stages 4–7 from `api/services/` into `pipeline/`, deduping the four `_query` helpers in
`api/stores/`, splitting `api/schemas.py` (353 lines) or `reconciliation_store.py` (3 tables),
per-vendor config registry.

## Go-live blockers (in order)

1. C6 — real sender domains
2. C1 — multi-PO duplication
3. C3 — adapters never produce POD date / carrier / tracking
4. Stages 4–7 don't exist in the pipeline layer (only as demo mocks)
5. No entry point / scheduler; no `.msg` ingestion if Outlook-file intake is required
