# Premier Receiver Automation

Email-parsing orchestrator for Premier Project Management's receiver process. It ingests
receiving emails (warehouse inbound notifications, delivery-to-property confirmations,
PODs/BOLs), triages them, extracts PO / spec / quantity data from bodies and attachments,
and stages records for reconciliation — plus a FastAPI + React dashboard that demos the
reconciliation and review workflow.

Full technical handbook (architecture, stage specs, security review, tech-debt register):
[`docs/PREMIER_AUTOMATION.md`](docs/PREMIER_AUTOMATION.md).
Known defects from the latest code analysis: [`docs/CODE-ANALYSIS-FINDINGS.md`](docs/CODE-ANALYSIS-FINDINGS.md).

## Status

| Piece | State |
|---|---|
| Stage 1 — Ingest (Graph API / local-folder mailbox) | Implemented + tested |
| Stage 1 — Triage (7-rule chain) | Implemented + tested |
| Stage 2 — Accumulate (multi-part shipments, stale-hold sweep) | Implemented + tested |
| Stage 3 — Extract (HTML / PDF / DOCX / OCR / Excel / freetext adapters) | Implemented + tested |
| Stages 4–7 — Match / Verify / Build / Route | **Stubs only** (`pipeline/stage4_match.py` …) — real logic is currently *mocked* in `api/services/` against synthetic demo data |
| Dashboard API (`api/`) + frontend (`frontend/`) | Working demo on seeded synthetic data |
| Production entry point / scheduler | **None yet** — `run_pipeline.py` is a stub; the pipeline is only invoked from tests |

## Layout

```
config/       settings (paths, sender domains, regexes, thresholds)
connectors/   mailbox.py (Graph API + local-folder), spitfire.py (stub)
pipeline/     stages 1–3, ingest_orchestrator, models, state DB
api/          FastAPI dashboard: routers/, services/ (mock stages 4–7), stores/, demo seed
frontend/     React + Vite dashboard (7 pages)
sample_data/  email fixtures for local runs
state/        runtime SQLite databases (gitignored)
tests/        pipeline tests + tests/api/
docs/         handbook, analysis findings, assets/
```

## Setup

```
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt -r requirements-dev.txt
copy .env.example .env        # then fill in the GRAPH_* values
```

## Common commands

```
pytest                          # run all tests (from repo root)
python run_api.py               # start the dashboard API on :8000
python -m api.demo.seed --reset # reseed the demo database
cd frontend && npm install && npm run dev   # dashboard UI (proxies /api -> :8000)
```
