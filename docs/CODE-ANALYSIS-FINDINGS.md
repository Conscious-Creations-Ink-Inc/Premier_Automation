# Code Analysis Findings — 2026-08-02

End-to-end trace of the orchestrator (stages 1–3 executed against `sample_data/`, full test
suite run: 90 passed / 3 skipped). Findings marked **[NEW]** are not in the tech-debt register
of `PREMIER_AUTOMATION.md`.

> **Status update, 2026-08-03.** The ingest-and-parse work closed C1, C2, C3, C4, C6, C10, C13
> and go-live blockers 1–3 and 5, and added the `.msg` intake the verdict below says does not
> exist. See "Resolved" at the end of this file for what changed and what each fix now rests on.
> Everything not listed there is still open. Suite is now 185 passed / 3 skipped, and
> `python -m tools.run_corpus` scores the 14 real June messages 14/14 on triage, 7/7 on lines
> parsed and 7/7 on records staged.

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
- ~~`frontend/package.json` `gen:api` writes `src/types/api.d.ts` but the real file is
  hand-written `src/types.ts` — the two will drift.~~ **Moot 11 Aug 2026** — the React app was
  deleted when the three UIs were collapsed into one. No generated types remain.
- ~~`api/main.py` never mounts `frontend/dist`.~~ **Moot 11 Aug 2026** — there is no build output
  to mount; the UI is server-rendered at `/ui`.

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

---

## Resolved — 2026-08-03 (ingest and parsing)

All of the below were verified against Premier's real June corpus
(`Documents/Premier/5,8 june`), not against invented samples. Run `python -m tools.run_corpus`
to reproduce; it writes a scored report to `D:\Premier\dev_reports\corpus_regression.md`.

**New capability**

- **`.msg` intake exists** — `connectors/msg_file.py` implements the same `Mailbox` interface as
  `GraphMailbox`, so the corpus exercises the production code path rather than a parallel one.
  Recurses into nested messages (the 5-Star thread's attached Delivered Notification carries the
  FedEx PODs), strips the NULs `extract_msg` leaves on filenames, and dedupes attachments by
  content hash — which collapses the two byte-identical PODs saved under different names.
- **`pipeline/parsing/`** — one shared token grammar (PO, spec, quantity/UOM, dates, shipment,
  tracking), boilerplate stripping, thread splitting, content sniffing, table classification,
  carrier-POD and confirmation-grid parsing. Each stage previously carried its own regex.
- **`pipeline/vendors/authority.py`** — the Class A/B grammars, deterministic, no fuzzy matching.

**Findings closed**

- **C1** — `ExtractionSource.only_po` plus `_belongs_to_event` filter records to the event's PO.
  A two-PO notice now stages each line once.
- **C2** — `MsgFileMailbox.mark_processed` tracks the source path by the id it issued rather than
  assuming filename equals id. `read_only=True` makes a regression run leave the folder untouched.
- **C3** — `pod_stated_date`, `carrier_name`, `tracking_number` and `delivery_location` are
  populated from the Authority header block, the carrier POD and the tracker's own columns.
  `received_by`, `po_line_number`, `package_quantity`/`package_uom` and `notification_number`
  were added alongside; `state_db` and `extracted_records_store` carry them, and
  `test_models.py` now fails if a model field has no column.
- **C4** — `tokens.PO_LABELLED_RE` is case-insensitive and covers `PO#`, `P.O.#`, `PO:`,
  `Purchase Order`, and comma/`+`-joined lists under one label.
- **C6** — sender lists replaced with domains observed in real headers, and triage no longer
  keys on domain at all: the rule key is (sender local part, subject grammar), resolved on the
  origin hop recovered from the quoted chain.
- **C10** — a failing adapter no longer aborts the cascade.
- **C13** — dispatch is by byte sniff, so the corpus's `mimetype=None` tracker and phone photos
  reach an adapter. HEIC is covered.

**Go-live blockers**

1. C6 — done. 2. C1 — done. 3. C3 — done. 5. `.msg` ingestion — done; **no scheduler or
production entry point yet** (`run_pipeline.py` is still a stub). Blocker 4 (stages 4–7 existing
only as demo mocks in `api/services/`) is untouched.

**Still open**

C5, C7, C8, C9, C11, C12, C14–C16, the dead-settings list, and everything under
"Structural". Two behaviours are deliberate rather than fixed:

- OCR yields nothing for photographed PODs because no OCR client is provisioned. Those messages
  route to a person with a reason that says so (`rule_5b_image_only_evidence`), instead of being
  dismissed as "no PO found".
- A Delivered notice with no matching Inbound is held and released by the grace sweep. Premier
  still owes us a written rule on whether that should become a receiver — the corpus contains
  one such case, annotated "straightforward, WH rec'd".
