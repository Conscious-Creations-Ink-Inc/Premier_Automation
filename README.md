# Premier Receiver Automation

Email-parsing orchestrator for Premier Project Management's receiver process. It ingests
receiving emails (warehouse inbound notifications, delivery-to-property confirmations,
PODs/BOLs), triages them, extracts PO / spec / quantity data from bodies and attachments,
and stages records for reconciliation. One user interface sits on top of it — see
[The interface](#the-interface) below.

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
| Dashboard API (`api/`) | Working demo on seeded synthetic data |
| The UI (`api/ui/`) | Seven pages behind one sidebar. **No authentication** — see the handbook's S2 |
| Scheduler (`operations/`) | Working: interval schedule, run history, kill switch. **Off until enabled on /ui/automation** |
| Production entry point | **None yet** — `run_pipeline.py` is a stub; the pipeline is otherwise invoked from the UI or from tests |

## The interface

**There is one.** `python run_api.py`, then <http://127.0.0.1:8000/ui>. Server-rendered, no build
step, one inline script. Seven pages behind a sidebar:

| Group | Page | Reads | What it answers |
|---|---|---|---|
| Deliveries | Delivery status | corpus | Where each purchase order stands, and the mail that says so |
| | Records | corpus | What was extracted and is ready to go further, plus the receiver report sheet |
| Mail | Mails | corpus | Every email through Stage 1 and what triage decided about it |
| | Inbox | **live mailbox** | Premier's receiving inbox, read and never written |
| Needs action | Needs a human | corpus | What the pipeline could not finish on its own |
| Operations | Automation | operations DB | Run now, the schedule, run history |
| | Receiver report | **live** | The report Premier compares against Spitfire's Receipt Log, and its Excel export |

The kill switch is pinned to the bottom of the sidebar on every page.

There were three UIs until 11 Aug 2026 — these pages, an operations console on :8500, and a React
dashboard on :5173. The console's four screens moved here (its logic still lives in `operations/`,
without the app or the renderer it used to carry); the React dashboard was deleted and is recoverable
from git history at `d14f407`. **`console/app.py` and `console/view.py` were never committed and are
gone.**

### The one line that must not be crossed

Two databases, never joined:

* **corpus** — `state/sample_state.sqlite3`, the 14 `.msg` files used to develop and test against.
* **live** — `state/pipeline_state.sqlite3`, mail read from `receiver@premierpm.com`.

Records and Receiver report are the same builder over the two different stores, which is exactly why
they are separate pages. The report is evidence; evidence that mixes test data with Premier's real
receiving mail is worth nothing.

`run_api.py` also still serves `/api/*` — a demo surface on synthetic data
(`state/demo_dashboard.sqlite3`) that nothing in the UI reads. Same process, nothing else shared.

## Layout

```
config/       settings (paths, sender domains, regexes, thresholds)
connectors/   mailbox.py (Graph API + local-folder), spitfire.py (read-only)
pipeline/     stages 1–3, ingest_orchestrator, models, state DB
api/          FastAPI: routers/ + services/ (mock stages 4–7) + stores/ + demo seed
api/ui/       THE UI — html.py is the whole design system, routes.py every page
operations/   running the automation: schedule, kill switch, live inbox read, run history
sample_data/  email fixtures for local runs
state/        runtime SQLite databases (gitignored)
tests/        pipeline tests + tests/api/
docs/         handbook, analysis findings, assets/
```

`api/ui/html.py` is the only place markup is built. Its docstring states the two rules that make the
whole surface XSS-safe by construction — never build markup with an f-string outside that file, and
never construct `Raw` from anything a user or a mail server supplied. Mail subjects are
attacker-influenced by definition, and the corpus already contains one carrying a bare `&`.

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
python run_api.py               # the UI on http://127.0.0.1:8000/ui
python -m tools.ingest_corpus --reset       # re-run the .msg corpus (the corpus pages)
python -m tools.ingest_mailbox              # read the live mailbox (read-only)
python -m api.demo.seed --reset             # reseed the /api/* demo database
```

Nothing runs unattended unless someone turns it on: the schedule is off until enabled on
`/ui/automation`, and the kill switch in the sidebar stops everything — schedule and Run now — until
Resume is pressed. A run in progress finishes the email it is on and then halts.
