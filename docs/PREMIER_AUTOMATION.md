# Premier Receiver Automation — Project Handbook

**The single source of truth for this project.**

| | |
|---|---|
| **Owner** | Rahul (Tech Lead) |
| **Last reviewed** | 2026-07-23 |
| **Status** | Phase 1 — in progress, ~51% complete |
| **Classification** | Internal — Conscious Creations / Premier. Contains no credentials. |

> **Evidence labels:** ✅ CONFIRMED · 📊 CALCULATED · 📌 ESTIMATED · ❓ NOT AVAILABLE · 🚧 BLOCKER · 💡 RECOMMENDATION

---

## Contents

1. [Executive summary](#1-executive-summary)
2. [Purpose & business problem](#2-purpose--business-problem)
3. [Team & ownership](#3-team--ownership)
4. [Current status](#4-current-status)
5. [The blocker](#5-the-blocker)
6. [Technology stack](#6-technology-stack)
7. [Project structure](#7-project-structure)
8. [Architecture](#8-architecture)
9. [Features & modules](#9-features--modules)
10. [API reference](#10-api-reference)
11. [Database](#11-database)
12. [Security review](#12-security-review)
13. [Performance review](#13-performance-review)
14. [Code quality & technical debt](#14-code-quality--technical-debt)
15. [Testing & DevOps](#15-testing--devops)
16. [Completion analysis](#16-completion-analysis)
17. [Roadmap & sprint plan](#17-roadmap--sprint-plan)
18. [Costs & budget](#18-costs--budget)
19. [Risk register](#19-risk-register)
20. [Production readiness](#20-production-readiness)
21. [Health scores](#21-health-scores)
22. [Runbook](#22-runbook)
23. [Open questions](#23-open-questions)
24. [Future roadmap](#24-future-roadmap)

---

## 1. Executive summary

Premier Receiver Automation is a seven-stage Python pipeline that reads a dedicated Microsoft 365
mailbox, triages delivery emails, extracts PO / spec / quantity from any document format, matches
to the exact Spitfire PO line, creates the receiver, attaches the proof of delivery (POD), and
routes exceptions to Fixed-Asset Accounting.

**Stages 1–3 are real, tested and committed. Stages 4–7 are one-line stubs.** The reason is not
engineering capacity — **Spitfire REST/SQL access and a test environment have not been granted** 🚧.
Every stub names the design document that specifies it; the team stopped exactly at the access
boundary and pivoted to building an interim reconciliation dashboard (`api/` + `frontend/`) that
*simulates* stages 4–7 against synthetic data, so the client can sign off on the matching and
exception UX before the real integration exists. That was a sound call.

| Headline | Value |
|---|---|
| Overall Phase-1 completion | **📊 51%** (SoW effort model) |
| Project code | ~7,000 LOC (2,900 committed + **5,303 uncommitted**) |
| Tests | **90 passed, 3 skipped, 0 failed** ✅ (93 collected, 50s) |
| API endpoints | **20** — all implemented, all demo-layer |
| Current external spend | **$0.00 / month** ✅ |
| Critical blocker | Spitfire API/SQL + test environment 🚧 |
| Overall health | **68 / 100** |
| Production ready | ❌ **No — 22/100** |

**The biggest non-technical risk is not the blocker — it is that 5,303 lines of finished work sit
uncommitted on one laptop**, while the SoW promises all source in Premier's Azure DevOps.

---

## 2. Purpose & business problem

✅ CONFIRMED from the signed Statement of Work (30 June 2026):

Premier records a receiver in Spitfire for every item it procures, tying each delivery to its PO
and a POD. Today this is done by hand across roughly **40 projects, thousands of orders and around
3,000 vendors**.

**Three concrete problems:**

1. **Email noise.** One delivery usually generates about three emails — shipping notice, delivery
   notice, warehouse inbound — often from two different companies. Only the inbound should create a
   receiver. This is exactly what defeated a previous automation attempt: a logistics feed sent
   every status change and the engine made a receiver for each.
2. **Format chaos.** Structured warehouse HTML, native-text PDF receipts, scanned or photographed
   PODs with no text layer, and one-line free-text confirmations from a property.
3. **The missing pay-request link.** Without it, reporting treats an item as not received and
   depreciation never starts — the gap behind Premier's manual quarterly catch-up. Making the link
   automatically at write-time is core Phase-1 scope.

**Target users**

| User | What they get |
|---|---|
| Premier expediting / purchasing agents | Receivers created without manual keying; only genuine exceptions reach them |
| Fixed-Asset Accounting | Remains the final approval gate — the automation never removes that control |
| Premier finance | Pay-request links made at write-time, so depreciation starts on time |
| Conscious Creations ops | Exception dashboard for triage and audit |

**Phase-1 scope** ✅ — dedicated receiving mailbox; parsing warehouse / 3PL inbound notifications;
extraction of PO, spec, description and quantity; matching to the correct PO line and building the
source table; re-use of the existing engine to create the receiver and attach the POD;
wait-and-combine; automated POD validation with exceptions routed to FAA.

**Out of scope for Phase 1** ✅ — lost or damaged items and claims; returns and reversals (Spitfire
cannot reverse a receiver); no-PO retail orders; uneven split shipments; direct-to-property
deliveries needing on-site confirmation — **an estimated 5–10% of orders**.

❓ **Phase-1 KPI:** the SoW contains **no stated target percentage** of auto-created receivers. The
nearest contractual proxy is that 5–10% out-of-scope figure (implying ~90–95% coverage intent) plus
the prose "automate the clean majority."
💡 **This is a contractual gap** — an acceptance criterion the client could later define
unilaterally. Agree a number with Joe and Nikunj before go-live.

---

## 3. Team & ownership

✅ CONFIRMED:

| Role | Person | Notes |
|---|---|---|
| Owner / client relationship | **Nikunj Agarwal** (Conscious Creations) | Signs off releases jointly with Joe |
| Tech Lead | **Rahul** | Sole committer — see risk R3 |
| Sub-lead | **Anuransh** | Remote branch `origin/Anuranshdev` exists |
| Premier counterpart | **Joe Higginbotham** | Spitfire / SQL owner; grants all access |
| Fixed-Asset Accounting owner | ❓ NOT AVAILABLE | Named as a role in the SoW; no individual confirmed |

**Module ownership** 📊 cannot be derived: `git shortlog -sne --all` shows **13 commits, 100%
authored by Rahul**. Remote branches `Anuranshdev`, `Rahuldev`, `dev`, `test` exist on `origin`,
but `main` has a single contributor.

🚧 **Bus factor = 1.** This directly contradicts the SoW's own commitment that *"the solution is
owned by Premier and maintainable beyond any one person."*

---

## 4. Current status

### What is complete ✅

- `config/settings.py` — every tunable in one file (103 LOC)
- `pipeline/models.py` — dataclasses for all seven stages (219 LOC)
- `pipeline/state_db.py` — four tables, idempotent DDL
- **Stage 1** ingest + triage (7-rule chain)
- **Stage 2** accumulate (wait-and-combine, fire-once, 48h stale sweep)
- **Stage 3** extract (six adapters: HTML, PDF, DOCX, OCR, Excel, free text)
- `pipeline/ingest_orchestrator.py` — Stages 1→2→3 end to end
- `connectors/mailbox.py` — real Graph connector + local-folder test double
- **`api/` + `frontend/`** — the interim reconciliation dashboard

### What is blocked 🚧

| Component | State |
|---|---|
| `pipeline/stage4_match.py` | 1-line stub |
| `pipeline/stage5_verify.py` | 1-line stub |
| `pipeline/stage6_build.py` | 1-line stub |
| `pipeline/stage7_route.py` | 1-line stub |
| `pipeline/match_orchestrator.py` | 1-line stub |
| `connectors/spitfire.py` | 1-line stub |
| `run_pipeline.py` | 1-line stub |

All seven are blocked on the same dependency. Each stub names the design document that specifies
it — this is a paused workstream, not abandoned work.

### The interim dashboard

While blocked, the team built a FastAPI + React reconciliation dashboard that simulates stages 4–7
against synthetic data. This is **Phase-2 scope in the SoW** ("smart helpers — confidence scoring,
an exception dashboard") delivered early. It does not advance the Phase-1 percentage, but it
materially de-risks Stage 4:

💡 **`api/services/reconcile.py` is roughly 80% of `pipeline/stage4_match.py`.** It imports the
pipeline's own `settings.DESC_MATCH_THRESHOLD` and RapidFuzz, scores the same three signals, maps
signals → confidence identically, and has eight unit tests. **Port it; do not rewrite it.**

---

## 5. The blocker

🚧 **Spitfire REST/SQL access and a non-production test environment have not been granted.**

| Dependency | Status |
|---|---|
| Cloud-AI approval (Claude + Azure Document Intelligence) | ✅ **Granted** |
| Dedicated receiving mailbox | ✅ Set up by Joe |
| Graph app registration + `Mail.ReadWrite` | ✅ Granted 2026-07-20 |
| Graph **mailbox address** | 🚧 Not yet provided |
| Graph **Application Access Policy** (scope to one mailbox) | 🚧 Not confirmed |
| Graph `Mail.Send` | 🚧 Not granted |
| **Spitfire Swagger + least-privilege service account** | 🚧 **PENDING** |
| **Spitfire published SQL / cache-sync query (spec → GUID)** | 🚧 **PENDING** |
| **Non-production Spitfire test environment** | 🚧 **PENDING** |
| Access to the existing receiver engine (extend, not rebuild) | 🚧 PENDING |
| Historical email + receiver dataset (Cameo) for back-testing | 🚧 PENDING |

The SoW anticipated this: *"Any waiting on outstanding access sits alongside this window rather
than inside it."* That is exactly what has happened.

---

## 6. Technology stack

### Backend / pipeline

| Layer | Choice | Version | Status |
|---|---|---|---|
| Language | Python | 3.12 | ✅ |
| Mail | `msal` + `requests` → Graph API v1.0 | unpinned | ✅ |
| HTML parse | BeautifulSoup4 + lxml | 4.15 | ✅ |
| PDF parse | pdfplumber (+ pypdfium2) | 5.12 | ✅ |
| Excel | openpyxl | unpinned | ✅ |
| Word | python-docx | unpinned | ✅ |
| OCR (dev) | pytesseract → local Tesseract | — | ✅ dev only |
| OCR (prod) | Azure AI Document Intelligence | — | 🚧 `NotImplementedError` |
| Fuzzy match | RapidFuzz | unpinned | ✅ |
| LLM | Anthropic Claude | — | 🚧 `NotImplementedError`, SDK not installed |
| State | SQLite 3 (stdlib) | — | ✅ |
| ERP | Spitfire (SFPMS) REST + SQL | — | 🚧 stub |

### Dashboard
FastAPI + Pydantic v2 · Uvicorn 0.51 · Starlette 1.3 · httpx 0.28 · SQLite. ✅ working.

### Frontend
React 19 · TypeScript 5.7 · Vite 6 · TanStack Query 5.62 · React Router 7.1 · Tailwind CSS 4 ·
Recharts 3.1 · lucide-react. ✅ builds clean, `dist/` present.

🚩 **Every Python dependency is unpinned** — no `==`, no lockfile. A transitive break will not be
reproducible. The frontend has `package-lock.json` and is fine.

---

## 7. Project structure

```
Premier_Automation/
├── config/settings.py        103 LOC — ALL tunables, one file
├── connectors/
│   ├── mailbox.py            185 — Mailbox ABC + LocalFolder + Graph ✅
│   └── spitfire.py             1 — 🚧 STUB
├── pipeline/
│   ├── models.py             219 — dataclasses for all 7 stages ✅
│   ├── state_db.py            77 — 4 tables, idempotent DDL ✅
│   ├── stage1_ingest.py       25 ✅   stage1_triage.py 111 ✅
│   ├── stage2_accumulate.py  191 ✅   extracted_records_store.py 67 ✅
│   ├── stage3_extract/       711 across 8 files ✅
│   ├── ingest_orchestrator.py 175 — Stages 1→2→3 ✅
│   ├── stage4_match.py … stage7_route.py   4 × 1 LOC — 🚧 STUBS
│   └── match_orchestrator.py   1 — 🚧 STUB
├── api/                    ~1,600 — FastAPI dashboard  (UNCOMMITTED)
├── frontend/src/           ~1,700 — React SPA          (UNCOMMITTED)
├── tests/                    ~900 committed + ~440 uncommitted
├── sample_data/              2 email fixtures + empty po_lines.json
├── state/                    SQLite files (gitignored)
└── docs/                     workflow.html, workflow.png, this handbook
```

✅ **Layering is clean:** `pipeline/` never imports `api/`. The dependency arrow points one way:
`frontend → api → pipeline → config`, with `connectors` feeding `pipeline`.

---

## 8. Architecture

### 8.1 System overview

```
┌──────────────────┐
│ Microsoft 365    │  Dedicated receiving mailbox
│ receiving inbox  │  (separate from the invoice mailbox)
└────────┬─────────┘
         │ Graph API v1.0, app-only (client credentials)
         ▼
┌────────────────────────────────────────────────────────────┐
│  INGEST ORCHESTRATOR   (every 20 min — not yet scheduled)  │
│  Stage 1 → Stage 2 → Stage 3                               │
└────────┬───────────────────────────────────────────────────┘
         ▼
┌──────────────────────────┐
│ extracted_records table  │  status = 'pending'
│ (SQLite today)           │  ← THE STAGE 3 / STAGE 4 HAND-OFF
└────────┬─────────────────┘
         ▼
╔════════════════════════════════════════════════════════════╗
║  🚧 MATCH ORCHESTRATOR — Stages 4,5,6,7 — DO NOT EXIST     ║
║     Blocked on Spitfire REST/SQL + test environment        ║
╚════════════════════════════════════════════════════════════╝
         ▼
┌──────────────────┐        ┌─────────────────────────────────┐
│ Spitfire (SFPMS) │◄───────│ Premier's existing receiver     │
│ receiver + POD   │        │ engine (Python → SQL → Spitfire)│
│ + pay-request    │        │ — extend, do not rebuild        │
└──────────────────┘        └─────────────────────────────────┘
```

Running alongside, sharing models and thresholds but never the same database:

```
┌──────────────────┐  /api proxy  ┌──────────────────┐   ┌────────────────────────┐
│ React SPA :5173  │─────────────▶│ FastAPI :8000    │──▶│ demo_dashboard.sqlite3 │
│ 7 pages          │              │ 20 endpoints     │   │ (synthetic data only)  │
└──────────────────┘              └──────────────────┘   └────────────────────────┘
```

### 8.2 Pipeline data flow

```
Graph Inbox → Stage 1 Ingest (dedupe on message id)
            → Stage 1 Triage (7 rules, first match wins)
   ├── HIDE    → folder "Hidden"    (noise, discarded)
   ├── ROUTE   → folder "Routed"    (human exception queue)
   └── SURFACE / HOLD → Stage 2 Accumulate  (key = PO + shipment)
          → fire ONCE on the true final event, or after a 48h grace period
          → Stage 3 Extract (adapter cascade over body + EVERY attachment)
               → reconcile_cross_source_duplicates()
               → extracted_records, status='pending'
```

Every processed email is moved out of Inbox into `Hidden` / `Routed` / `Processed` / `Errors`, so
the Inbox only ever holds genuinely new mail and any category can be audited by folder.

### 8.3 Stage 1 — Ingest & triage ✅

`fetch_new_emails()` filters through `state_db.is_new_message()`, which records the Graph message
id in `seen_message_ids`. An email already seen is never reprocessed, even if a later folder move
fails.

🚩 **Known defect:** `GraphMailbox.fetch_new()` requests `$top=50` and does **not** follow
`@odata.nextLink`. A backlog larger than 50 is silently truncated on every poll.

**The 7 rules:**

| Order | Rule | Condition | Category | Type |
|---|---|---|---|---|
| 1 | `rule_0_order_cancellation` | cancellation keywords | **ROUTE** | `order_cancellation` |
| 2 | `rule_1_warehouse_table` | warehouse domain **and** `<table>` in body | **SURFACE** | `warehouse_inbound` |
| 3 | `rule_2_freight_status` | freight domain, status words, no inventory words | **HIDE** | `delivered_shipped` |
| 4 | `rule_3_warehouse_no_table` | warehouse domain **and** PO token | **SURFACE** | `inbound_notification` |
| 5 | `rule_5_vendor_confirmation` | vendor domain **and** PO token | **HOLD** | `vendor_confirmation` |
| 6 | `rule_4_property_reply` | PO token, ≤200 words, no table | **HOLD** | `property_confirmation` |
| 7 | `rule_6_unknown` | anything else | **ROUTE** | `unknown` |

📌 **Deliberate deviation from the design doc:** rule 5 runs *before* rule 4. The docstring
explains why — otherwise a known vendor domain would always be caught by the generic short-reply
rule and never reach its own, more specific rule. `matched_rule` keeps the doc's original
numbering for traceability. This is exemplary discipline.

**Category → folder map:**

| Category | Meaning | Folder |
|---|---|---|
| `HIDE` | Pure noise, correctly discarded | `Hidden` |
| `SURFACE` | A true receiving event | `Processed` |
| `HOLD` | Confirmation to pair with a delivery | `Processed` |
| `ROUTE` | Needs a person | `Routed` |
| *(exception raised)* | Triage/accumulate threw | `Errors` |

### 8.4 Stage 2 — Accumulate ✅

**Purpose:** one delivery generates ~3 emails; create exactly one receiver.
**Key:** `AccumulationKey(po_number, shipment_number)`.

```
for each po_number in triaged.extracted_po_hints:
    if already in released_events for this key:  →  log duplicate, skip
    INSERT OR IGNORE into accumulation (full email as JSON, attachments base64)
    if category == SURFACE and type in {WAREHOUSE_INBOUND, INBOUND_NOTIFICATION}:
        bundle = every accumulated email for this key
        mark released ("true final event received")
        emit DeliveryEvent
```

**Multi-PO:** one email referencing several POs can release several `DeliveryEvent`s. A failure on
one PO is logged and skipped; the others still process.

**Stale-hold sweep:** runs once per orchestrator pass after all new mail. Releases any unreleased
key whose oldest notice is older than 48h **and** which contains at least one HOLD email — this
handles direct-to-property confirmations that never get a warehouse leg. Keys with no HOLD-worthy
content are left waiting.

### 8.5 Stage 3 — Extract ✅

**Source fan-out:** one `ExtractionSource` per body and per attachment, **independently**. One
email with three attachments yields four sources.

**Adapter cascade — first `can_handle()` wins:**

| Order | Adapter | Handles | Confidence cap |
|---|---|---|---|
| 1 | `HtmlAdapter` | HTML bodies with tables | 1.0 |
| 2 | `PdfAdapter` | PDF **with** a text layer | 1.0 |
| 3 | `DocxAdapter` | `.docx` (+ embedded-image OCR fallback) | 1.0 |
| 4 | `OcrAdapter` | images, **and** PDFs with *no* text layer | 0.85 |
| 5 | `ExcelAdapter` | `.xlsx` trackers | 1.0 |
| 6 | `FreetextAdapter` | text bodies only — deliberately does **not** claim attachments | varies |

An adapter that raises is logged and treated as "found nothing" — never aborts the delivery. A
source no adapter recognises is logged explicitly rather than silently staged as an empty record.

📌 `DocxAdapter` was **not** in the original design — added after real dummy test documents showed
`.docx` is a plausible vendor attachment format.

**Shared primitives** (`stage3_extract/base.py`) — used by every document-shaped adapter so
behaviour is identical regardless of format: `map_headers()` / `match_header()` (fuzzy header
matching at RapidFuzz ratio 85), `build_record_from_row()`, `regex_extract_fields()`,
`split_sub_spec()` (`STE-402-LT-B` → parent `STE-402-LT`, suffix `B`), `apply_confidence_floor()`
(no spec + no quantity + no description → confidence forced to 0.0), `strip_print_chrome()`.

📌 **The print-chrome stripper is an empirical fix.** Every real dummy POD carried a browser print
header (a timestamp line and a `file:///` path line), and those were polluting extraction: a date's
`M/D` and a page number's `N/M` were being misread as quantity fractions.

**Regex hardening** (recorded in `config/settings.py`):

| Pattern | Fix | Reason |
|---|---|---|
| `SPEC_TOKEN_REGEX` | trailing `[A-Za-z]?` | `GR-350a-WTF` fuses a letter onto the numeric segment |
| `QTY_TOKEN_REGEX` | `(?<!\d/)` and `(?!\s*/\s*\d)` guards | `7/18/26` read as quantity `7/18`; a date has three slash-separated numbers, a real quantity fraction only two |

**Cross-source reconciliation** — groups by `(po_number, parent_spec_code)`:
- Quantities agree (or one missing) → keep the highest-confidence record, log the rest as discarded.
- Quantities **disagree** → keep **all** records, tag `extraction_source` with `+quantity_conflict`.
  The system never silently picks one.

**AI fallback — not implemented.** `ai_fallback.propose_fields()` raises `NotImplementedError`;
`ENABLE_AI_FALLBACK` is `False`; the `anthropic` SDK is not in `requirements.txt`. This is the
*only* function permitted to call an LLM, and even then it proposes field values only — the match
stays deterministic, so it structurally cannot invent a receiver.

### 8.6 Stages 4–7 — designed, not built 🚧

Dataclasses exist in `pipeline/models.py`; implementations do not.

**Stage 4 — Reconcile & match.** Score on three signals:

| Signals | Confidence | Intended outcome |
|---|---|---|
| 3 (PO + spec + description) | high | Receiver created automatically |
| 2 | medium — "maybe" | Created with lower confidence, or flagged |
| ≤1 / no PO | low / none | Routed to a person from the start |

PO and spec are **exact** comparisons — the spec resolves the exact line, so a fuzzy spec match
would be worse than none. Only the description is fuzzy (RapidFuzz `token_sort_ratio` ≥ 80).

**Stage 5 — Verify.** POD required for every material receiver; carrier tracking date used as the
receipt date when available, otherwise an email confirmation from the property; receivers required
for Net-30, CBD and ADR (CBD/ADR attached within 30 days); soft costs (freight, tax, overages)
exempt via `MATERIAL_COST_CODE_PREFIX`.

**Stage 6 — Build & log.** Feed the verified source table to Premier's existing engine: create the
receiver, generate the per-line key, attach the POD, and **link the pay request**.

**Stage 7 — Route & audit.** Validated PODs auto-approve; everything else routes to FAA or the
purchasing agent who created the original document. Every unmatched item lands in an exception
queue and an error log.

### 8.7 Deployment architecture

❓ **None exists.** No Dockerfile, no `.github/`, no `azure-pipelines.yml`, no IaC, no Key Vault
client, no Azure Function or Container App scaffold.

Target per the SoW (to be built):

```
Azure Container App / Function  (schedule or new-mail webhook)
        ├── Azure Key Vault      Graph + Spitfire credentials
        ├── Azure SQL Database   PO-line cache, accumulation state, audit trail
        ├── Azure Blob (opt.)    POD staging
        └── Azure DevOps         source control + CI/CD (code is Premier's)
```

Blocking questions: where the DB lives (data residency / network path to self-hosted Spitfire), and
whether the service runs in Azure reaching in or inside Premier's network.

### 8.8 Design principles observed in the code

Worth preserving — consistently applied, and the reason the codebase reads as well as it does:

1. **Every decision traceable** — `matched_rule` + `reason` on triage; `extraction_source` +
   `raw_snippet` on every extracted record.
2. **Never guess** — disagreeing sources are flagged, not resolved; missing evidence forces
   confidence to zero.
3. **Never lose a message** — failures route to `Errors`; `mark_failed` appends rather than
   overwrites.
4. **One bad input never aborts a run** — per-email, per-source, per-PO try/except boundaries.
5. **Injectable dependencies** — `conn` and `ocr_client` are parameters everywhere, so tests never
   touch the real database or a real service.
6. **Configuration in exactly one place** — `config/settings.py`.
7. **Deterministic core, constrained AI.**

---

## 9. Features & modules

| # | Module | Status | Remaining | Risk |
|---|---|---|---|---|
| 1 | Config & models | ✅ **100%** | Real vendor/carrier domains (placeholders today) | Low |
| 2 | Graph mailbox connector | ✅ **95%** | Mailbox address; App Access Policy; no pagination; `Mail.Send` not granted | **Med** |
| 3 | Stage 1 Triage | ✅ **100%** | Tune against real Premier mail | Low |
| 4 | Stage 2 Accumulate | ✅ **100%** | Validate 48h + 20-min poll vs real volume | Low |
| 5 | Stage 3 Extract | ✅ **90%** | Azure DocInt + Claude fallback both `NotImplementedError` | **Med** |
| 6 | Ingest Orchestrator | ✅ **100%** | Schedule / host it | Low |
| 7 | Stage 4 Match | 🚧 **0%** | Everything — logic prototyped in `api/services/reconcile.py` | **HIGH** |
| 8 | Stage 5 Verify | 🚧 **0%** | POD rules, CBD/ADR window, soft-cost exemption | **HIGH** |
| 9 | Stage 6 Build | 🚧 **0%** | Source table → Spitfire, line key, POD, **pay-request link** | **HIGH** |
| 10 | Stage 7 Route | 🚧 **0%** | Routing + error log | **HIGH** |
| 11 | Spitfire connector | 🚧 **0%** | Everything | **CRITICAL** |
| 12 | Match Orchestrator | 🚧 **0%** | Daily SQL-agent equivalent | High |
| 13 | Dashboard API | ✅ **100%** | No auth; demo data only | Med |
| 14 | Dashboard UI | ✅ **~95%** | No auth; hardcoded operator | Med |

**8 complete · 0 in progress · 6 blocked** 🚧

---

## 10. API reference

**Base URL (dev):** `http://127.0.0.1:8000` · **Docs:** `/docs`, `/redoc`, `/openapi.json`

> ⚠️ **No authentication on any endpoint**, including a destructive reset. Demo layer over
> synthetic data. Stages 4–7 are simulated — `request-info`, `template/send` and `inbox/organize`
> are mocks that record what *would* have happened.

**Operator attribution:** `body.operator` → `X-Operator` header → `config.DEFAULT_OPERATOR`.
Nothing in that chain is verified. The React client sends a hardcoded
`X-Operator: ashford@consciouscreations.ai`.

**Errors:** FastAPI shape — the human-readable reason is in `detail`, surfaced directly to the
reviewer. `400` = decision cannot be applied · `404` = not found · `422` = validation.

**Shared vocabularies** (identical to `pipeline.models`, so UI badges can never drift):
`confidence` = high/medium/low/none · `review_status` = auto_approved/pending_review/approved/
cancelled · `status` = pending/matched/failed/routed · `route_target` = auto_approved/
purchasing_agent/fixed_asset_accounting/exception_queue/not_applicable.

### Endpoint index

| # | Method | Path | Writes | Auth |
|---|---|---|---|---|
| 1 | GET | `/api/health` | — | ❌ |
| 2 | POST | `/api/admin/seed?reset=` | ✔ | ❌ |
| 3 | POST | `/api/admin/reset` | ✔ **destructive** | ❌ |
| 4 | GET | `/api/dashboard/summary` | — | ❌ |
| 5 | GET | `/api/reconciliation/exceptions` | — | ❌ |
| 6 | GET | `/api/reconciliation` | — | ❌ |
| 7 | GET | `/api/reconciliation/{match_id}` | — | ❌ |
| 8 | POST | `/api/reconciliation/{match_id}/approve` | ✔ | ❌ |
| 9 | POST | `/api/reconciliation/{match_id}/cancel` | ✔ | ❌ |
| 10 | POST | `/api/reconciliation/{match_id}/request-info` | ✔ | ❌ |
| 11 | GET | `/api/extracted-records` | — | ❌ |
| 12 | GET | `/api/extracted-records/{record_id}` | — | ❌ |
| 13 | GET | `/api/delivery-report` | — | ❌ |
| 14 | GET | `/api/inbox/preview` | — | ❌ |
| 15 | POST | `/api/inbox/organize` | ✔ | ❌ |
| 16 | GET | `/api/vendor/template` | — | ❌ |
| 17 | PUT | `/api/vendor/template` | ✔ | ❌ |
| 18 | POST | `/api/vendor/template/schedule` | ✔ | ❌ |
| 19 | POST | `/api/vendor/template/send` | ✔ | ❌ |
| 20 | GET | `/api/vendor/sends` | — | ❌ |

📊 **20 total · 20 implemented · 0 pending · 0 duplicate · 0 deprecated · 0 unused.**

### Key endpoints in detail

**`GET /api/dashboard/summary`** — totals, an honest funnel (Mail ingested → Receiving events →
Records extracted → Reconciled → Settled → Receipts staged), and four count-by breakdowns. Empty
categories are filled with zeros so chart axes do not jump around. *The gap between `reconciled`
and `settled` **is** the exception queue.*

**`GET /api/reconciliation/{match_id}`** — everything a reviewer needs to decide, in **one**
payload: the record, why it was flagged, up to 5 ranked candidate PO lines **with the per-signal
breakdown** so the reviewer sees *why* each ranked where it did, the decision history, the staged
receipt, and which fields are editable.
📌 If nothing scored at all (usually an unknown PO), the endpoint returns up to 10 *open* lines
scored anyway, so the item can still be resolved by hand rather than being a dead end.

**`POST /api/reconciliation/{match_id}/approve`** — approving is **not** a status flip. The
reviewer must *resolve* the item: pick the line the automation could not, and fill any required
field the vendor left out. Only then is a receipt staged. Approving fixes the data rather than
rubber-stamping a gap.

```json
{ "po_line_id": 4,
  "filled_fields": { "spec_code": "STE-402-LT", "pod_stated_date": "2026-07-18" },
  "note": "Confirmed against carrier POD" }
```

**Idempotent** — re-approving returns the existing outcome with `already_applied: true`; it does
not stage a second receipt or double-count quantity. Side effects: stage receipt → add received
qty to the PO line → `mark_matched()` → update `match_results` → append immutable
`review_decisions` row.

400 conditions include: *"No PO line is selected. Pick the correct line before approving — the
automation could not resolve one."* and *"Still missing {fields}. Fill these in before approving."*
Required fields: `spec_code`, `quantity_received`, `pod_stated_date` (plus `po_number`).

**`POST /api/reconciliation/{match_id}/cancel`** — `reason` is **mandatory** (`min_length=1`). A
cancelled receipt with no explanation is exactly the sort of gap this system exists to remove.
`mark_failed()` **appends** the reason to the record's `comments` rather than overwriting, so the
"why" travels with the record and not only in the audit table.

**`GET /api/delivery-report`** — three buckets by what the mail actually said happened:
`delivered_received` (delivered, received) · `in_transit` (out_for_delivery, shipped) ·
`cancelled` (cancelled **+ anything unrecognised**). 📌 Unrecognised keywords fall into the
exception bucket rather than being dropped, so the report always accounts for every mail.

**`GET /api/inbox/preview`** — the point is what is **left**: after filing, the inbox holds only
order confirmations, cancellations and anything unclear. Proposed folders come from the pipeline's
own map in `config.settings`, so this screen always agrees with what the real orchestrator would do.

### Missing endpoints (needed for production)

`POST /api/pipeline/run` · `GET /api/pipeline/status` · real Spitfire read/write · POD file fetch ·
**auth/login + session** · user management · audit export · `GET /metrics` · `GET /ready`.

### Regenerating frontend types

```
cd frontend && npm run gen:api     # requires the API to be running
```

---

## 11. Database

Two SQLite files, deliberately isolated so seeding or resetting the demo can never touch the
pipeline's real state. Both gitignored. Schema created idempotently with
`CREATE TABLE IF NOT EXISTS`.

| File | Owner | Purpose |
|---|---|---|
| `state/pipeline_state.sqlite3` | `pipeline/state_db.py` | Real pipeline state |
| `state/demo_dashboard.sqlite3` | `api/db.py` | Pipeline tables **+** 7 demo tables |

### Pipeline tables

**`seen_message_ids`** — `email_id` PK, `seen_at`. Graph-level idempotency.

**`accumulation`** — PK `(po_number, shipment_number, email_id)`, plus `notification_type`,
`category`, `received_at`, `payload_json`. Written with `INSERT OR IGNORE`.
⚠️ **`payload_json` holds base64 attachment content inline.** A 10 MB scanned POD becomes ~13 MB of
text, and `_bundle_for_key()` deserializes **every** email for a key on each read.

**`released_events`** — PK `(po_number, shipment_number)`, plus `released_at`, `release_reason`.
⚠️ SQLite treats NULL as distinct inside a PRIMARY KEY, so multiple NULL-shipment rows for one PO
**can** be inserted. `_is_released()` compensates by branching on `IS NULL`, so behaviour is
correct — but the constraint is not enforcing what it appears to.

**`extracted_records`** — **the Stage 3 → Stage 4 hand-off**, the most important table in the
system. `id` PK AUTOINCREMENT, `source_email_id`, `po_number`, `shipment_number`, `spec_code`,
`parent_spec_code`, `sub_spec_suffix`, `item_description`, `vendor_name`, `carrier_name`,
`tracking_number`, `quantity_received`, `unit_of_measure`, `pod_stated_date`, `email_date`,
`delivery_location`, `comments`, `extraction_source`, `extraction_confidence`, `raw_snippet`,
`status`, `created_at`, `updated_at`.

```
pending ──mark_matched()──▶ matched
   └─────mark_failed()────▶ failed   (reason appended to comments)
routed  — defined in the model, never written by any code path
```

### Demo tables

| Table | Rows | Purpose |
|---|---:|---|
| `po_lines` | 12 | Candidate set; **`line_key` UNIQUE** = Spitfire's GUID / ReceiptDocumentKey |
| `demo_emails` | 18 | Synthetic emails + the triage verdict they would have received |
| `match_results` | 13 | Reconciliation verdict; `extracted_record_id` UNIQUE |
| `review_decisions` | 0 | Human audit trail — **never overwritten** |
| `staged_receipts` | 5 | Mock stage 6 output; `item_number` = the line's GUID |
| `vendor_template` | 1 | Single row, `CHECK (id = 1)` |
| `vendor_send_log` | 0 | Simulated sends |

Seeded distribution: high/auto_approved 5 · high/pending 3 · medium/pending 2 · low/pending 2 ·
none/pending 1 → **exception queue = 8**.

📌 An **auto-approval writes no `review_decisions` row**, because no human decided anything. That
is precisely what keeps `auto_approved` distinct from `approved` on the dashboard.

### Relationships — logical only, none enforced

```
demo_emails.email_id ──(1:N)──▶ extracted_records.source_email_id
extracted_records.id ──(1:1)──▶ match_results.extracted_record_id   [UNIQUE]
po_lines.id          ──(1:N)──▶ match_results.po_line_id
match_results.id     ──(1:N)──▶ review_decisions.match_result_id
extracted_records.id ──(1:N)──▶ staged_receipts.extracted_record_id
accumulation(po,shipment) ────▶ released_events(po,shipment)
```

🚩 **No foreign keys anywhere**; `PRAGMA foreign_keys` never enabled. `seed.py` acknowledges it:
*"Child rows first — plain deletes, no FK cascade to rely on."*

### Indexes

🚩 **None beyond primary keys and two UNIQUE constraints.** These are all full scans:
`WHERE po_number = ? AND shipment_number = ?` · `WHERE status = 'pending'` ·
`WHERE flagged = 1 AND review_status = 'pending_review'` · `GROUP BY po_number, shipment_number`.

💡 Recommended:

```sql
CREATE INDEX idx_accumulation_key  ON accumulation (po_number, shipment_number);
CREATE INDEX idx_extracted_status  ON extracted_records (status);
CREATE INDEX idx_extracted_po      ON extracted_records (po_number);
CREATE INDEX idx_match_flagged     ON match_results (flagged, review_status);
CREATE INDEX idx_po_lines_spec     ON po_lines (spec_code);      -- critical for Stage 4
CREATE INDEX idx_po_lines_po       ON po_lines (po_number, line_status);
CREATE INDEX idx_decisions_match   ON review_decisions (match_result_id);
```

`idx_po_lines_spec` matters most: Stage 4 currently ranks candidates with an O(n) fuzzy pass over
**all** PO lines. A spec-code exact-match pre-filter backed by this index reduces that to
effectively O(1), because a spec is used only once per PO and therefore resolves to a single line.

### Migrations

🚩 **No migration framework.** `CREATE TABLE IF NOT EXISTS` makes first-run trivial but provides
**no path to alter a column or backfill data** in an existing environment. The SoW commits to
moving state into shared SQL Server — that migration has no plan. 💡 Adopt Alembic before any
deployed environment exists.

### Connection management

Pipeline: `state_db.get_connection(db_path)` — `Path` or `":memory:"`. Callers own the connection.
API: `deps.get_conn()` yields **one connection per request**, closed on the way out — deliberately
not a singleton, because Uvicorn runs sync endpoints on a threadpool and a `sqlite3` connection is
not thread-safe.

### Data integrity findings

| # | Finding | Severity |
|---|---|---|
| D1 | No foreign keys anywhere | 🟠 High |
| D2 | No indexes beyond PKs | 🟠 High |
| D3 | No migration framework | 🟠 High |
| D4 | Base64 attachment blobs inline in `payload_json` | 🟠 High |
| D5 | `released_events` PK does not constrain NULL shipments as intended | 🟡 Medium |
| D6 | `status = 'routed'` defined but never written | 🟢 Low |
| D7 | Business data unencrypted at rest | 🟢 Low → 🟠 in production |
| D8 | No retention/purge policy for `accumulation` payloads | 🟡 Medium |

✅ **Every SQL statement is parameterized with `?` placeholders — zero string-formatted SQL found.**

---

## 12. Security review

**Report only — nothing was fixed, no credential value is shown. Security score: 32 / 100.**

| Severity | Count |
|---|---:|
| 🔴 Critical | 3 |
| 🟠 High | 3 |
| 🟡 Medium | 3 |
| 🟢 Low | 2 |

### 🔴 S1 — Live Graph client secret in plaintext on disk
`.env` holds `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET` — all `[REDACTED]` —
issued 2026-07-20, one-year expiry.
**Mitigated:** `.env` is gitignored ✅ and verified **never committed** ✅; the file carries its own
"never commit" warning; all values read via `os.getenv`.
**Residual:** no encryption at rest, no Key Vault, no rotation procedure, single-machine custody.
**Fix:** provision Key Vault, move all three in, delete the local `.env`, rotate with Joe. The code
already anticipates this (`settings.py`: *"store in Key Vault once real infra exists — never
hardcode"*).

### 🔴 S2 — Zero authentication on all 20 endpoints and the UI
Operator identity resolves `body.operator` → `X-Operator` header → default, with **nothing
verified**. The React client sends a hardcoded constant. Any caller who can reach the port can
approve a receipt (staging a receipt and moving quantity onto a PO line), cancel an item, or
**attribute either action to any identity they choose** — the `review_decisions` audit trail
records whatever string was supplied.
Acknowledged in code as a deliberate demo-phase decision. Correct for synthetic data; disqualifying
for production.
**Fix:** Entra ID / OAuth2; derive the operator from the verified token; **remove** the
`X-Operator` path; add role-based authorization. **Must land before any real Premier data.**

### 🔴 S3 — Unauthenticated destructive endpoint
`POST /api/admin/reset` deletes from eight tables with no auth, no confirmation, no rate limit.
**Mitigating:** it can only reach `demo_dashboard.sqlite3`, never `pipeline_state.sqlite3`.
**Fix:** admin role + confirmation token; disable outside demo environments.

### 🟠 S4 — Graph app scope not restricted to one mailbox
`Mail.ReadWrite` is granted tenant-wide; the **Application Access Policy** restricting the app to
the single receiving mailbox **is not in place**. Until Joe applies it, these credentials can read
and move mail in *every* mailbox in Premier's tenant. Flagged honestly in the connector's own
docstring.
**Fix:** Joe applies `New-ApplicationAccessPolicy`. **Do not point this connector at a real Premier
mailbox until confirmed.**

### 🟠 S5 — Attachments deserialized and parsed with no validation
No size cap, no MIME verification (content-type is taken from the sender's claim, never magic
bytes), no malware scan. Bytes flow into **pdfplumber, openpyxl, python-docx and PIL** — all
parsers with CVE history — and into Tesseract. This is fully untrusted input from ~3,000 external
parties.
**Fix:** size cap before decode; verify magic bytes; MIME allowlist; resource-limited parsing;
pin and monitor parser versions.

### 🟠 S6 — No rate limiting on any endpoint
Combined with S2 and S3, an unauthenticated caller can loop reset or approve without restriction.

### 🟡 S7 — Graph `$filter` built by string interpolation
`f"displayName eq '{folder_name}'"`. Values are internal constants today so **not exploitable** —
but it is an injection-shaped construction. **Fix:** escape quotes or validate against the known set.

### 🟡 S8 — Permissive CORS
`allow_methods=["*"], allow_headers=["*"], allow_credentials=True`. Origins are restricted to
localhost:5173 today so the risk is contained, but the wildcard + credentials combination is wrong
to carry to production and `CORS_ORIGINS` is environment-overridable.

### 🟡 S9 — No retention or purge policy for stored email content
`accumulation.payload_json` retains full bodies and attachment content indefinitely; nothing prunes
released keys.

### 🟢 S10 — Sensitive business data unencrypted at rest
Full email bodies, PO numbers, vendor names, pricing context and PODs in plain SQLite files.

### 🟢 S11 — Unstructured logging with no redaction
All logging is `print()`; eight separate `_log()` functions; no levels, no correlation ids, no
redaction, no rotation or shipping.

### ✅ What is done well

| Control | Evidence |
|---|---|
| SQL injection resistance | Every statement parameterized; **zero** string-formatted SQL found |
| Secrets never hardcoded | All via `os.getenv`; Azure key placeholder carries an explicit Key Vault instruction |
| `.gitignore` hygiene | Covers `.env`, `state/*.sqlite3`, `*.log`; verified no secret ever committed |
| XSS resistance | React auto-escapes; **no** `dangerouslySetInnerHTML` anywhere |
| Database isolation | The demo DB physically cannot reach `pipeline_state.sqlite3` |
| Immutable audit trail | `review_decisions` append-only; `mark_failed` appends to comments |
| Fail-safe error handling | Failed emails route to an `Errors` folder — never silently dropped |
| Constrained AI | LLM proposes field values only; match is always deterministic. Currently disabled by flag *and* by an uninstalled SDK |
| Honest self-documentation | Known gaps documented in the code rather than hidden |

### Remediation priority

1. **S1** Key Vault — before production
2. **S2** Authentication — **before any real Premier data**
3. **S4** Application Access Policy — **before pointing at a real mailbox**
4. **S3** Protect `/admin/reset` — before any shared environment
5. **S5** Attachment validation — before processing real vendor mail
6. S11 logging · 7. S6 rate limiting · 8. S8 CORS · 9. S9 retention · 10. S7 escaping ·
11. S10 encryption at rest

💡 **The three hard gates are S2, S4 and S5.**

**Not assessed:** Spitfire auth (stub), Azure infra (nothing provisioned), network/TLS (no
deployment), dependency CVE scan (no SCA tooling, deps unpinned), penetration testing.

---

## 13. Performance review

| Finding | Impact |
|---|---|
| **`GraphMailbox.fetch_new()` has no pagination** — hard `$top=50`, no `@odata.nextLink`. A backlog >50 is silently truncated every poll; at a 20-min interval that caps throughput at 150/hour | 🔴 **Correctness + throughput** |
| **N+1 on attachments** — one extra Graph call per email with attachments; 50 emails → up to 51 requests per poll | 🟠 |
| **Token acquired per call** — `_headers()` calls `_access_token()` on every request. MSAL caches in-memory so it is cheap, but it is unnecessary per-message work | 🟡 |
| **Base64 attachment blobs in SQLite** — full serialize on write, full deserialize of *every* email in a key on each `_bundle_for_key()` | 🟠 |
| **No indexes** — every accumulation/extracted/match query is a scan | 🟠 at scale |
| **`_caches()` loads entire tables per request** — extracted records, PO lines and emails on every reconciliation call | 🟠 |
| **No pagination on any list endpoint** | 🟠 |
| **`rank_candidates` is O(n) fuzzy over all PO lines** — a spec-code exact-match pre-filter would cut this to effectively O(1) | 🟠 for Stage 4 |
| **OCR retry sleeps synchronously** — `time.sleep(2)` blocks the pipeline | 🟡 |
| Test suite 50s for 93 tests — dominated by real PDF/OCR fixture generation | ✅ acceptable |
| Frontend uses TanStack Query with precise key invalidation; builds clean. Bundle size not measured | ✅ |

💡 Priority: pagination fix (correctness) → indexes → attachment storage → endpoint pagination →
SQL Server migration.

---

## 14. Code quality & technical debt

**This is genuinely well-written code — the strongest artifact in the project. Score: 88/100.**

### ✅ Strengths

- **Docstrings explain *why*, and cite the spec.** `stage1_triage.triage()` documents that it
  deliberately reorders rule 5 before rule 4 versus the design doc's prose, explains why, and
  *keeps the doc's original rule numbering in `matched_rule`* for traceability. Exceptional
  discipline.
- **Comments record empirical findings**, not restatements — the `7/18/26` date-as-quantity bug and
  the `GR-350a-WTF` spec shape are both preserved with their reasoning.
- **Single source of truth for configuration** — 103 lines, nothing hardcoded elsewhere.
- **Correct abstractions** — `ExtractionAdapter`, `Mailbox`, `DocumentIntelligenceClient` ABCs.
  Adding a warehouse format means adding one adapter, exactly as the SoW promises.
- **Shared helpers pushed to `base.py`** so Html/Pdf/Excel/Ocr adapters behave identically;
  `records_from_ocr_result` is shared between `OcrAdapter` and `DocxAdapter`'s embedded-image path.
- **Defensive by design, honestly** — one bad email never aborts a run; a folder-move failure is
  logged not raised, with a comment explaining the email is already durably captured; OCR-
  unavailable and OCR-found-nothing are deliberately kept as *different* cases.
- **Never guesses** — quantity conflicts flagged not resolved; `mark_failed` appends;
  `apply_confidence_floor` zeroes confidence with no evidence.
- **Testable by construction** — `conn` and `ocr_client` injectable everywhere.
- Consistent naming, type hints throughout, dataclasses over dicts, no dead code, no duplication.

### 🚩 Weaknesses

- `print()` instead of `logging` — eight separate `_log()` definitions.
- No linter/formatter config (no ruff/black/flake8/mypy).
- Unpinned dependencies.
- **README is materially wrong.** It says *"Scaffolding only. `config/settings.py` and
  `pipeline/models.py` are implemented; everything else is a stub"* — false since ~16 July. It
  points at `../BuildPlan/` and `../ourDocs/` which **do not exist on this machine**, yet 20+
  source files reference those paths in docstrings. A new developer cannot follow a single one.

### Technical debt register

| # | Item | Cost | Interest |
|---|---|---|---|
| TD1 | 5,303 LOC uncommitted | 1h | Compounding — total loss risk |
| TD2 | README false + dangling `BuildPlan/` refs | 2h | Onboarding blocked |
| TD3 | No auth layer | 3–5d | Blocks production |
| TD4 | No CI/CD, no Docker | 3–5d | SoW commitment unmet |
| TD5 | Unpinned deps | 1h | Non-reproducible builds |
| TD6 | `print` → `logging` | 4h | No prod observability |
| TD7 | No DB indexes/FKs/migrations | 2d | Scale + integrity |
| TD8 | Graph pagination missing | 2h | **Silent data loss** |
| TD9 | Placeholder vendor/carrier domains | 1h + client input | Triage misfires on real mail |
| TD10 | Base64 blobs in SQLite | 1–2d | Scale |
| TD11 | No pagination on list endpoints | 1d | Scale |
| TD12 | Model config ≠ approved model | 5 min | Cost / approval scope |

---

## 15. Testing & DevOps

### Testing ✅ — the strongest area (72/100)

```
90 passed, 3 skipped, 0 failed — 93 collected — 50.35s
```

| File | Tests | Covers |
|---|---:|---|
| `test_extract.py` | 23 | All 6 adapters; **generates real in-memory PDFs via reportlab**, real OCR via Tesseract |
| `test_triage.py` | 13 | All 7 rules + ordering |
| `test_reconciliation_api.py` | 12 | Full HTTP layer via TestClient |
| `test_ingest_orchestrator.py` | 9 | Stages 1→2→3 wiring, failure isolation |
| `test_decisions.py` | 9 | Approve/cancel/idempotency |
| `test_reconcile_service.py` | 8 | Signal scoring, thresholds |
| `test_seed.py` | 7 | Determinism |
| `test_accumulate.py` | 6 | Release-once, multi-PO, stale sweep |
| `test_models.py` | 4 | Dataclasses |
| `test_mailbox_folder_routing.py` | 2 | Folder moves |
| `test_match/verify/build/route/end_to_end` | **0** | 🚧 5 stub files |

Quality is high — tests exercise real document bytes, not mocks of parsers. The 3 skips are
Tesseract-dependent and degrade gracefully.

🚩 **No coverage measurement** — `pytest-cov` not installed; coverage is unknown.
🚩 **Zero frontend tests** — no vitest/jest/RTL/Playwright; `package.json` has no `test` script.
🚩 **No end-to-end test** (stub).

### DevOps ❌ — the weakest area (12/100)

| Item | Status |
|---|---|
| Version control | ✅ Git + GitHub (`Conscious-Creations-Ink-Inc/Premier_Automation`), `main` synced 0/0 |
| **Azure DevOps (SoW requirement)** | ❌ **Not used** — code is on GitHub, not Premier's DevOps |
| CI | ❌ None — no `.github/`, no `azure-pipelines.yml` |
| CD | ❌ None |
| Docker | ❌ No Dockerfile |
| IaC | ❌ None |
| Monitoring / alerting | ❌ None |
| Structured logging | ❌ `print()` only |
| Backups | ❌ None |
| Rollback | ❌ None |
| Disaster recovery | ❌ None |
| Secrets management | ❌ Local `.env` |
| Environment separation | ❌ Single local env |

---

## 16. Completion analysis

### 📊 CALCULATED against the SoW's own effort model (160 hrs, two-person option)

| SoW work package | Hours | Maps to | Credit |
|---|---:|---|---:|
| Stage 1 — Connect & ingest | 60 | Repo Stages 1+2, Graph connector, state DB | **55 / 60** |
| Stage 2 — Extract, match & build | 80 | Extract ✅ (27) · Reconcile & match ❌ (13) · Build & route ❌ (40) | **27 / 80** |
| Stage 3 — Test, harden & go-live | 20 | Not started — needs a test environment | **0 / 20** |
| **Total** | **160** | | **📊 82 / 160 = 51%** |

**Cross-checks:** by stage 3/7 = 43% · by module 8/14 = 57% · by LOC ~48%.
📌 **Consolidated: 50% ± 5%.**

Note: the interim dashboard (~5,300 LOC) is **additional scope not in the SoW's 160 hours** — real
delivered value, but it does not advance the Phase-1 percentage.

---

## 17. Roadmap & sprint plan

> **Assumption stated explicitly:** Weeks 3–6 assume Spitfire access is granted by end of Week 2.
> If it is not, W3+ cannot start. **W1–W2 are unblocked today and should proceed regardless.**

### Week 1 — Protect what exists (NO BLOCKER) — *do this now*

| Task | Priority | Est |
|---|---|---|
| **Commit + push `api/`, `frontend/`, `tests/api/`, `docs/`, `run_api.py`** | 🔴 P0 | 1h |
| Mirror repo into Premier's Azure DevOps (SoW commitment) | 🔴 P0 | 2h |
| Fix README: real status, remove dangling `BuildPlan/` refs | 🔴 P0 | 2h |
| Pin all Python dependencies | 🟠 P1 | 1h |
| Add `pytest-cov`, establish a coverage baseline | 🟠 P1 | 2h |
| Chase Joe: mailbox address, App Access Policy, `Mail.Send`, Spitfire Swagger + service account + test env | 🔴 P0 | ongoing |
| Confirm approved Claude model vs `settings.ANTHROPIC_MODEL` | 🟡 P2 | 15m |
| Agree the Phase-1 KPI % with Nikunj + Joe | 🟠 P1 | — |

### Week 2 — Harden Stages 1–3 (NO BLOCKER)

| Task | Priority | Est |
|---|---|---|
| **Fix Graph pagination** (`@odata.nextLink`) — silent data loss | 🔴 P0 | 3h |
| Provision Azure DocInt + implement `RealDocumentIntelligenceClient` | 🟠 P1 | 1d |
| Azure Key Vault + move the Graph secret off disk | 🟠 P1 | 1d |
| `print()` → structured `logging` with correlation ids | 🟠 P1 | 4h |
| Attachment size cap + MIME validation | 🟠 P1 | 3h |
| DB indexes on hot query paths | 🟡 P2 | 2h |
| CI: pytest + `tsc --noEmit` on push | 🟠 P1 | 4h |
| Replace placeholder vendor/carrier domains with Premier's real lists | 🟠 P1 | 1h |

### Week 3 — Spitfire connector 🚧 *gated*
Swagger review with Joe → `connectors/spitfire.py`: auth, PO-line read, spec→GUID cache-sync query,
receiver write, POD attach, **pay-request link**. Retry/backoff. Integration tests against the test
environment. **Est 3–4 d.**

### Week 4 — Stage 4 Match 🚧 *gated*
**Port `api/services/reconcile.py` → `pipeline/stage4_match.py`** (the biggest accelerator
available). Add spec→GUID exact resolution, spec-code pre-filter for O(1) candidate narrowing,
`MatchResult` persistence, `match_orchestrator.py`. Back-test against the historical Cameo dataset
if Joe can share it. Fill `tests/test_match.py`. **Est 3 d.**

### Week 5 — Stages 5 + 6 🚧 *gated*
Stage 5: POD-required rule, carrier-tracking-date-else-email-confirmation resolution, CBD/ADR
30-day window, soft-cost exemption. Stage 6: `StagedPOReceipt` / `StagedPODDocument` → engine,
line-key generation, POD attach, pay-request link. Fill `test_verify.py`, `test_build.py`.
**Est 4 d.**

### Week 6 — Stage 7 + orchestration 🚧 *gated*
Routing (auto-approve / purchasing agent / FAA / exception queue), error log, stale-hold sweep,
`run_pipeline.py`, SQL Agent daily schedule. Fill `test_route.py`, **`test_end_to_end.py`**.
**Est 3 d.**

### Week 7 — Production hardening
Auth on API + UI (Entra ID / OAuth), remove hardcoded operator, protect `/admin/reset`, tighten
CORS, rate limiting, Dockerfile, Azure Container App/Function deploy, App Insights, alerts, backup
+ rollback runbook. **Est 5 d.**

### Week 8 — Validate & go-live
End-to-end on the real sample set in the test environment, threshold tuning with expediting + FAA,
security review, UAT with Joe, sign-off, scheduled live agent, hypercare.

### Sprint plan (2-week sprints)

| Sprint | Goal | Gate |
|---|---|---|
| **S1** (W1–2) | Repo safe, docs true, Stages 1–3 production-grade, CI live | ✅ Unblocked |
| **S2** (W3–4) | Spitfire connector + Stage 4 matching | 🚧 Spitfire access |
| **S3** (W5–6) | Stages 5–7 + full end-to-end | 🚧 Spitfire access |
| **S4** (W7–8) | Security, deploy, UAT, go-live | Sign-off Nikunj + Joe |

---

## 18. Costs & budget

### Development cost

❓ **INFORMATION NOT AVAILABLE.** No rate card, invoices or timesheets exist in the repo or the
documents. The SoW quotes **effort only** — 160 hrs (two-person, ~3.5 weeks) or 190 hrs
(four-person, ~2.5 weeks) — with **no monetary figures**.

📊 **What can be calculated in effort terms:**
- Consumed: ~82 hrs of the 160-hr Phase-1 scope
- Remaining Phase-1 scope: ~78 hrs
- 📌 Work identified in this review but *outside* the SoW's 160 hrs: **~120–160 hrs** (auth ~40,
  CI/CD + Docker + Azure deploy ~40, monitoring/logging ~16, DB hardening/migrations ~16, frontend
  tests ~16, security remediation ~24). The SoW's Stage-3 "Test, harden & go-live" allocates 20 hrs;
  that is not enough to cover production auth + DevOps from a standing start.
- 📌 Realistic remaining to production: **~200–240 hrs**, ≈ 5–6 calendar weeks at the SoW's
  two-person cadence, **plus** the Spitfire access wait.

### Current actual operating cost — ✅ CONFIRMED **$0.00 / month**

| Provider | Purpose | Usage today | Cost |
|---|---|---|---|
| Microsoft Graph | Read mailbox, download PODs, move mail | ✅ **0 calls** — `GRAPH_MAILBOX_ADDRESS` blank, orchestrator never scheduled | **$0** (no per-call charge; covered by M365) |
| Azure AI Document Intelligence | Production OCR | ✅ **0** — endpoint `None`, client raises `NotImplementedError` | **$0** |
| Anthropic Claude | Free-text fallback | ✅ **0** — `ENABLE_AI_FALLBACK=False`, SDK not installed, function raises | **$0** |
| Spitfire | PO read / receiver write | ✅ **0** — stub | **$0** (Premier-owned) |
| Azure Key Vault / SQL / Compute | — | ✅ **0** — nothing provisioned | **$0** |

### 🚩 Model configuration finding

`config/settings.py:57` sets `ANTHROPIC_MODEL = "claude-sonnet-5"`.

1. ✅ The ID is **valid and current** — Claude Sonnet 5, 1M context, **$3.00 / $15.00 per MTok**
   (introductory **$2.00 / $10.00** through 2026-08-31).
2. 🚩 The cloud-AI approval on record names **Claude Haiku 4.5** — **$1.00 / $5.00 per MTok**,
   200K context. The code specifies a model **3× more expensive on both input and output** than the
   one approved. Since `ENABLE_AI_FALLBACK=False`, nothing has been spent — but this is an
   approval-scope mismatch to reconcile before the flag is ever flipped. Haiku 4.5 is well suited to
   the actual job (constrained field extraction from short free text), so the approved model is
   likely also the correct one.

### 📌 ESTIMATED forward-looking cost

Source: Premier's own costing document. Figures it flags as "verify" are marked.

| Service | Unit price | Notes |
|---|---|---|
| Azure DocInt — Read/OCR | ~$1.50 / 1,000 pages | drops at volume |
| Azure DocInt — Layout | ~$10 / 1,000 pages | tables |
| Azure DocInt — Prebuilt Invoice | "higher (verify)" | ❓ |
| Azure SQL Basic | ~$5 / month | tier-dependent |
| Azure Key Vault | ~$0.03 / 10k ops | negligible |
| Container Apps / Functions | "a few $/month at daily-batch volume (verify)" | ❓ |
| Blob storage (POD staging) | negligible | |
| Graph / Spitfire / DevOps / OSS | **$0** | covered by existing licences |

| | Actual | Projected monthly |
|---|---|---|
| **TOTAL** | ✅ **$0.00** | 📌 **~$10–15 fixed + variable OCR** ❓ |

❓ **Monthly OCR page volume cannot be calculated.** It depends on Premier's real delivery-email
volume and the deterministic-vs-OCR split, and **no real mail has been processed**. Any monthly
figure now would be fabricated.

💡 The costing document's own control is already implemented in code: `OcrAdapter.can_handle()`
calls `_has_text_layer()` and only routes to OCR when pdfplumber finds nothing, so free
deterministic parsing handles clean documents. 💡 Run a **two-week metered pilot** against the real
mailbox before committing to a run-rate.

### Open-source licensing ✅

All runtime dependencies are MIT/BSD/Apache-2.0/PSF. The costing document flags two to avoid unless
licensed: **PyMuPDF** (AGPL-3.0) and **Surya** (paid model weights). ✅ Neither is used — the project
correctly standardised on pdfplumber.

---

## 19. Risk register — top 20

| # | Risk | Sev | Prob | Mitigation |
|---|---|---|---|---|
| R1 | **5,303 LOC uncommitted, one machine, no backup** | 🔴 Critical | High | Commit + push today |
| R2 | **Spitfire access not granted — blocks 4 of 7 stages** 🚧 | 🔴 Critical | Occurring | Escalate Nikunj→Joe with a dated deadline |
| R3 | Bus factor 1 (Rahul is sole committer) | 🔴 Critical | High | Onboard Anuransh; pair on Stage 4 |
| R4 | Live Graph secret plaintext on disk | 🔴 Critical | Med | Key Vault (W2) |
| R5 | Zero API/UI authentication | 🔴 Critical | Certain | Auth sprint before any real data |
| R6 | Graph App Access Policy absent → tenant-wide mailbox access | 🟠 High | Med | Joe applies policy before real mailbox |
| R7 | Graph pagination missing → **silent email loss >50/poll** | 🟠 High | High | Fix W2 |
| R8 | No CI/CD, no Docker, no deploy target | 🟠 High | Certain | W2 + W7 |
| R9 | Code on GitHub, not Premier's Azure DevOps (SoW breach) | 🟠 High | Certain | Mirror W1 |
| R10 | No end-to-end test — stages never proven together | 🟠 High | Certain | W6 |
| R11 | Triage rules tuned on synthetic data only | 🟠 High | High | Real sample set from Joe |
| R12 | Placeholder vendor/carrier domains | 🟠 High | Certain | Get real lists |
| R13 | No Phase-1 KPI agreed → open-ended acceptance | 🟠 High | Certain | Agree % with Nikunj + Joe |
| R14 | Azure DocInt volume/cost unknown | 🟠 High | High | Metered pilot |
| R15 | README materially false; `BuildPlan/` refs dangle | 🟠 High | Certain | Fix W1 |
| R16 | No coverage metric | 🟡 Med | Certain | pytest-cov W1 |
| R17 | Zero frontend tests | 🟡 Med | Certain | Vitest + Playwright |
| R18 | No DB indexes/FKs/migrations; base64 blobs | 🟡 Med | High | W2 + W7 |
| R19 | Unpinned dependencies | 🟡 Med | Med | Pin W1 |
| R20 | Model config (Sonnet 5) ≠ approved model (Haiku 4.5) | 🟡 Med | Low | Confirm + align |

---

## 20. Production readiness

| Gate | Verdict |
|---|---|
| Functional completeness | ❌ 4 of 7 stages missing |
| Authentication / authorization | ❌ None |
| Secrets management | ❌ Plaintext `.env` |
| Deployment | ❌ Nothing exists |
| CI/CD | ❌ None |
| Monitoring / alerting | ❌ None |
| Backup / DR / rollback | ❌ None |
| Test coverage | ⚠️ Strong where built; unmeasured; no end-to-end test |
| Data integrity | ⚠️ No foreign keys, no migrations |
| Performance at scale | ⚠️ Unvalidated; known bottlenecks |
| Code quality | ✅ High |
| Documentation | ⚠️ Excellent in-code; README materially false |

### ❌ **NOT PRODUCTION READY — 22 / 100.**
📌 Earliest realistic go-live: **6–8 weeks after Spitfire access is granted.**

---

## 21. Health scores

| Dimension | Score | Basis |
|---|---:|---|
| Architecture | **82** | Clean layering, correct abstractions; deployment architecture absent |
| Code Quality | **88** | Best-in-class docstrings/comments; `print` logging, no linter |
| Security | **32** | No auth, plaintext secret, unscoped Graph app, unvalidated attachments |
| Performance | **58** | Sound patterns; pagination bug, no indexes, no endpoint pagination |
| Scalability | **48** | SQLite + base64 + full-table loads will not hold at Premier's volume |
| Maintainability | **80** | Very readable and testable; bus factor 1, false README |
| Testing | **72** | 90 passing with real document fixtures; no coverage metric, no E2E, no frontend tests |
| Documentation | **35** | Superb in-code; README wrong, `BuildPlan/` unreachable, no API/deploy docs |
| DevOps | **12** | Git only. No CI, CD, Docker, IaC, monitoring, backups |
| Production Readiness | **22** | See §20 |

### 📊 OVERALL PROJECT HEALTH: **68 / 100**
### 📊 OVERALL PHASE-1 COMPLETION: **51%**

**Reading:** the engineering is well above average (architecture 82, code 88, testing 72). The score
is pulled down entirely by what surrounds the code — DevOps 12, readiness 22, security 32. That is
the signature of strong solo development without infrastructure, which is exactly what the evidence
shows.

### Top 20 recommended actions

**Immediate (today)**
1. 🔴 Commit and push the 5,303 uncommitted lines.
2. 🔴 Mirror the repo to Premier's Azure DevOps.
3. 🔴 Escalate Spitfire access with a dated deadline; state the cost of delay in writing.
4. 🔴 Rewrite the README to reflect reality; remove or relocate the `BuildPlan/` references.
5. 🟠 Agree the Phase-1 KPI percentage with Nikunj and Joe.

**Week 1–2 (unblocked)**
6. 🔴 Fix Graph pagination — a silent-data-loss defect, not a nice-to-have.
7. 🔴 Move the Graph secret into Azure Key Vault.
8. 🟠 Stand up CI (pytest + `tsc --noEmit`) on every push.
9. 🟠 Pin all Python dependencies.
10. 🟠 Add `pytest-cov`; publish a coverage baseline and a floor.
11. 🟠 Replace `print()` with structured logging + correlation ids.
12. 🟠 Cap and validate attachments before parsing.
13. 🟠 Get Premier's real vendor/carrier domain lists.
14. 🟠 Provision Azure DocInt and implement `RealDocumentIntelligenceClient`.
15. 🟡 Add DB indexes on the hot query paths.

**Week 3+ (needs access)**
16. 🔴 Build `connectors/spitfire.py`, including the pay-request link.
17. 🔴 **Port `api/services/reconcile.py` into `stage4_match.py`** rather than rewriting.
18. 🔴 Write the end-to-end test.
19. 🔴 Add authentication before any real Premier data touches the API or UI.
20. 🟠 Back-test matching against the historical Cameo dataset before go-live.

---

## 22. Runbook

### Prerequisites

Python 3.12 · Node.js 20+ · Tesseract OCR (**optional** — without it, 3 tests skip gracefully) ·
Git. Tesseract path is set at `config/settings.py` → `TESSERACT_CMD_PATH`.

### First-time setup

```powershell
python -m venv .venv
.venv\Scripts\activate            # POSIX: source .venv/bin/activate
pip install -r requirements.txt

cd frontend && npm install && cd ..

Copy-Item .env.example .env       # then fill in the Graph values
```

⚠️ Dependencies are unpinned — a fresh install today may not match one from last week.

| Variable | Source | Status |
|---|---|---|
| `GRAPH_TENANT_ID` | Joe, 2026-07-20 | ✅ |
| `GRAPH_CLIENT_ID` | Joe, 2026-07-20 | ✅ |
| `GRAPH_CLIENT_SECRET` | Joe, 2026-07-20 | ✅ expires 2027-07-20 |
| `GRAPH_MAILBOX_ADDRESS` | Premier | 🚧 **Not provided — leave blank** |

Optional: `API_HOST`, `API_PORT`, `CORS_ORIGINS`, `DEFAULT_OPERATOR`.

### Running

```powershell
.venv\Scripts\python.exe -m pytest -q     # expect: 90 passed, 3 skipped (~50s)
python run_api.py                          # API on :8000, docs at /docs
cd frontend; npm run dev                   # SPA on :5173, proxies /api
python -m api.demo.seed --reset            # deterministic reseed
```

Run the API **before** the frontend. On first boot the app seeds the demo database if empty;
existing data is left alone.

### Exercising the pipeline

```powershell
python run_pipeline.py    # ⚠️ STUB — does nothing
```

Drive Stages 1→3 directly instead:

```python
from config import settings
from connectors.mailbox import LocalFolderMailbox
from pipeline.ingest_orchestrator import process_new_mail

mailbox = LocalFolderMailbox(settings.SAMPLE_EMAILS_DIR)
print(f"staged {process_new_mail(mailbox)} records")
```

`LocalFolderMailbox` reads `sample_data/emails/*.json` and simulates folder moves by moving files
into subdirectories — the whole pipeline runs with no live mailbox access.

⚠️ **Do not point `GraphMailbox` at a real Premier mailbox** until the Application Access Policy is
confirmed (§12 S4).

### Key configuration

| Setting | Current | Note |
|---|---|---|
| `WAREHOUSE_SENDER_DOMAINS` | 3 placeholders | 🚧 Swap for Premier's real list |
| `FREIGHT_SENDER_DOMAINS` | 5 placeholders | 🚧 Swap for Premier's real list |
| `VENDOR_CONFIRMATION_DOMAINS` | 2 placeholders | 🚧 Swap for Premier's real list |
| `HOLD_GRACE_PERIOD_HOURS` | 48 | Validate against real volume |
| `HEADER_MATCH_THRESHOLD` | 85 | RapidFuzz ratio for column headers |
| `DESC_MATCH_THRESHOLD` | 80 | RapidFuzz `token_sort_ratio` for descriptions |
| `ENABLE_AI_FALLBACK` | `False` | 🚧 Keep false — the SDK is not installed |
| `ANTHROPIC_MODEL` | `claude-sonnet-5` | ❓ Approval on record names Claude Haiku 4.5 |
| `AZURE_DOC_INTELLIGENCE_ENDPOINT` | `None` | 🚧 Pending provisioning |
| `INGEST_ORCHESTRATOR_INTERVAL_MINUTES` | 20 | Not scheduled anywhere yet |

### Troubleshooting

| Symptom | Explanation |
|---|---|
| `GraphMailbox is missing required config: mailbox_address` | Expected — not yet provided. Use `LocalFolderMailbox`. |
| `Graph API auth failed: …` | Check `.env`, admin consent, secret expiry. Probe scripts in `d:/Premier/graph_probe/` diagnose independently. |
| Only 50 emails processed per run | 🚩 **Known defect (R7)**, not configuration. No `@odata.nextLink` pagination. |
| `RealDocumentIntelligenceClient requires a provisioned Azure … endpoint + key` | Expected. Use the Mock or Tesseract client. |
| `Real Claude fallback not yet wired up …` | Expected. Flag off, SDK absent. Tests monkeypatch `propose_fields`. |
| Three tests skip | Tesseract missing or path wrong. Harmless by design. |
| `no adapter recognized source for …` | Attachment type outside the six adapters — logged deliberately, not staged as an empty record. If it is a genuine Premier format, add an adapter. |
| `quantity conflict across sources … routing all, not guessing` | Working as designed — two sources disagreed, so all records are kept and flagged for a human. |
| Frontend shows no data | Is the API on :8000? `GET /api/health` → `seeded: true`? If not, `POST /api/admin/seed`. |
| `sqlite3.ProgrammingError: … thread` | A connection was shared across threads. Use `deps.get_conn()`. |

### Backup and recovery

| Asset | Backup | Recovery |
|---|---|---|
| Source code | GitHub `main` — but **5,303 lines uncommitted** | ⚠️ Partial |
| `pipeline_state.sqlite3` | None | ❌ |
| `demo_dashboard.sqlite3` | None — but fully regenerable | ✅ Reseed |
| `.env` secrets | None | ❌ Re-request from Joe |
| Mailbox | M365 native retention | ✅ Premier's |

🔴 **Immediate action: commit and push.** This is R1 and the single highest-value action available.

### Health checks

| Check | Command | Healthy |
|---|---|---|
| Tests | `python -m pytest -q` | `90 passed, 3 skipped` |
| Build | `cd frontend && npm run build` | exit 0 |
| API | `GET /api/health` | `{"status":"ok","seeded":true,…}` |
| Git clean | `git status --porcelain` | **empty** (currently is not) |
| Remote sync | `git rev-list --left-right --count origin/main...main` | `0  0` |

### Contacts and escalation

| Topic | Contact |
|---|---|
| Code, architecture, build | Rahul (Tech Lead) |
| Delivery, priorities, client | Nikunj Agarwal (Owner) |
| Spitfire access, SQL, engine, mailbox, Graph policy | Joe Higginbotham (Premier) |
| POD acceptance criteria, approval rules | Fixed-Asset Accounting — ❓ owner not confirmed |
| Cloud-AI / data residency decision | Corina + Joe (per SoW §3) |

**Release approval:** Nikunj + Joe, jointly.

---

## 23. Open questions

| Question | Owner | Impact |
|---|---|---|
| ❓ What is the agreed Phase-1 KPI (% auto-created)? Not stated anywhere in the SoW. | Nikunj + Joe | Acceptance criteria are open-ended |
| ❓ Which Claude model is approved? Code says `claude-sonnet-5`; approval on record is Haiku 4.5 (3× cheaper). | Nikunj + Joe/Corina | Cost + approval scope |
| ❓ Where will the production database live (data residency / network path to self-hosted Spitfire)? | Premier IT | Blocks infra design |
| ❓ Does the service run in Azure reaching in, or inside Premier's network? | Premier IT | Blocks deployment design |
| ❓ Does the receiver engine write via REST or direct to DB, and can it be reused as-is? | Joe | Blocks Stage 6 |
| ❓ Are spec codes genuinely unique per PO line? | Joe | **The entire match key depends on this** |
| ❓ Can the historical Cameo dataset be shared for back-testing? | Joe | Confidence before go-live |
| ❓ Committed Phase-1 go-live date | Nikunj + Joe | None exists; planning is week-to-week |

---

## 24. Future roadmap

**Phase 1 (current)** — clean-majority automation, seven-stage pipeline, exception routing to FAA.

**Phase 2 (SoW)** — confidence scoring, an exception dashboard *(largely pre-built already — the
`api/` + `frontend/` work is Phase-2 scope delivered early)*, delivery-date reminders that close the
loop by email (matched back by the line GUID in the metadata), weekly reconciliation, and a
click-to-confirm flow for direct-to-property orders.

**Phase 3 (SoW)** — Oracle integration as Spitfire begins writing to Oracle, broader vendor and
channel coverage, automated expediting follow-ups, and carrier tracking APIs (FedEx / UPS / DHL —
environment variables already reserved in the costing document).

---

## Appendix — reference documents

**Inside the repo:** `docs/PREMIER_AUTOMATION.md` (this file), `docs/workflow.html`,
`docs/workflow.png`.

**Outside the repo:**
- `d:/Premier/Documents/` — SoW, process guide, API costing, weekly plans, complete guide,
  discovery notes, workflow diagram
- `d:/Premier/graph_probe/` — Graph access probe scripts and reports
- `d:/Premier/dummy Testing data/` — real-shaped test documents (`docx/`, `pdf/`, `raw_images/`)

⚠️ The repository README references `../BuildPlan/` and `../ourDocs/`. **Neither exists on this
machine**, yet more than twenty source files cite them in docstrings. Correcting the README is a
pending action — see §17 Week 1. It has **not** been done as part of this documentation work.
