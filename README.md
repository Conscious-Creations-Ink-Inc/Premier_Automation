# ppm-receiver-automation

Automates Premier Project Management's receiving process. It reads vendor delivery-confirmation
emails and proof-of-delivery attachments from a monitored mailbox, extracts purchase-order and
line-item data, matches it against open PO lines in **Spitfire sfPMS**, and prepares receiver
documents for a person to approve.

This replaces a manual procurement and fixed-asset receiving process running across
approximately 66 properties. Approved receivers flow downstream into **Oracle ERP** for asset
capitalisation.

> **The system never posts autonomously.** Anything it cannot resolve — a missing spec code, a
> quantity that does not reconcile, unreadable paperwork — is routed to *Needs Attention* with a
> written reason. A person decides. Nothing reaches Spitfire without human approval.

---

## Repository classification

| Field | Value |
|---|---|
| Organisation | `ashford` |
| Repository | `ppm-receiver-automation` |
| Visibility | Private |
| Company scope | Premier (PPM) |
| Department | Procurement / Fixed Asset Accounting |
| **Risk tier** | **Critical** |
| **Data classification** | **Confidential** |
| Production status | Development |

**Why Critical** (Standards v1.5 §5.3 — it meets five criteria): it creates and posts documents
that materially affect financial reporting and fixed-asset capitalisation; it has write access to
a critical enterprise platform; it feeds a SOX-controlled system of record; its failure would
interrupt a live business process across ~66 properties; and it uses a privileged service
principal.

## Ownership

| Role | Owner | Responsibility |
|---|---|---|
| Business owner | **To be named by Premier** *(Corina Heizer or Adam Okolicany)* | Accountable for the business process and outcome |
| Technical owner | **Joe Higginbotham** (Premier) | Understands and maintains the solution; named PR approver for Premier |
| Production support owner | **To be named by Premier** | Ongoing operation and incident response |
| Delivery partner | Conscious Creations (`partner-conscious-creations`) | Builds and tests; **cannot approve PRs or production** |
| Platform Admins | Ayotunde Gibbs, Henry Noel (Ashford IT) | GitHub configuration, security review, Critical-repo governance |

The two "to be named" rows are open items tracked in [`docs/support-runbook.md`](docs/support-runbook.md).
They must be filled before production sign-off.

## Systems and data

| System | Access | Notes |
|---|---|---|
| Microsoft Graph | **Read/write** on one mailbox | `Mail.ReadWrite` — write is needed only to move processed messages into a filed folder |
| Azure AI (Vision / Document Intelligence) | Read | OCR for photographed and scanned proof of delivery. Metered — see the spend cap in `.env.example` |
| Spitfire sfPMS REST API | **Read**, plus a narrow allowlisted write surface | Write is deny-checked before allowlisting; see [`docs/spitfire-integration.md`](docs/spitfire-integration.md) |
| Spitfire SQL Server | **`SELECT` only** | No `INSERT`, `UPDATE`, `DELETE` or DDL in any code path, tests included |
| Oracle ERP | **Read only** | Reconciliation. **Nothing in this repository writes to Oracle.** |

**Data touched:** vendor delivery documents and proof of delivery; purchase-order headers, lines,
quantities and cost codes; supplier and property identifiers; names of individuals who signed for
goods. Classified **Confidential**. Full treatment in [`docs/data-handling.md`](docs/data-handling.md).

**Test data is synthetic.** Fixtures mirror the *shape* of real messages — subject grammars,
table layouts, forwarding wrappers — with every identifier substituted. Real PO numbers, property
names, vendor names and staff names are prohibited anywhere in this repository.

## How it works

Seven stages. An email enters at 1 and either becomes an approved receiver at 6, or is routed to
a person at 7.

| Stage | Does | State |
|---|---|---|
| 1 · Ingest & triage | Read the mailbox; classify each message and record why | Implemented, tested |
| 2 · Accumulate | Group multi-part shipments; sweep stale holds | Implemented, tested |
| 3 · Extract | Read bodies and attachments — HTML, PDF, DOCX, XLSX, plain text, OCR for images | Implemented, tested |
| 4 · Match | Resolve the PO and match each item to a line by `SourceItemNumber` | In progress |
| 5 · Verify | Check quantities against what is still open on the line | In progress |
| 6 · Build | Create the receipt, attach the POD and report | **Blocked** — see below |
| 7 · Route | Send anything unresolved to a reviewer, with the reason | In progress |

**Stage 6 is blocked on one decision:** whether receivers are created through Spitfire's REST API
or by writing to its tables directly. Direct table inserts bypass Spitfire's own workflow
triggers, which is the concrete argument for the API route. The decision sits with Premier.
Detail in [`docs/spitfire-integration.md`](docs/spitfire-integration.md).

**Matching rules that must not drift:**

```
Line matching key   SourceItemNumber (spec code)
Open quantity       RelatedLineDetails.ContractUnits - ReceivedUnits
```

Open quantity subtracts prior receipts. Reading `ContractUnits` alone over-receives any line with
a partial receipt against it.

## The interface

One interface, server-rendered, no build step:

```
python run_api.py     # then http://127.0.0.1:8000/ui
```

Seven pages behind a sidebar: delivery status per PO, extracted records, all mail with its triage
verdict, a read-only view of the live inbox, *Needs a human*, the automation schedule and run
history, and the receiver report. A kill switch is pinned to the sidebar on every page.

**A record is complete when it carries the five facts only a delivery notification can supply** —
PO number, spec code, description, quantity, delivery date. Vendor, unit, PO line number and
signatory are derived from sources that already hold the authoritative value, so nobody is asked
to retype them.

**Two databases, never joined.** A development corpus and live mail are kept in separate stores
and surfaced on separate pages. The receiver report is evidence, and evidence that mixes test
data with real receiving mail is worth nothing.

### One rule for the UI

All markup is built in a single module, which is what makes the surface XSS-safe by construction:
never build markup with an f-string outside that module, and never construct raw HTML from
anything a user or a mail server supplied. Mail subjects are attacker-influenced by definition.

## Running it locally

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt -r requirements-dev.txt
copy .env.example .env            # then fill in — see .env.example for each value
```

Never commit `.env`. It is gitignored and is the only file the code reads for credentials.

```bash
pytest                            # full suite, ~21 minutes
python run_api.py                 # the interface
python -m tools.ingest_mailbox    # read the live mailbox (read-only)
```

Nothing runs unattended unless someone turns it on. The schedule is off until enabled in the UI,
and the kill switch stops everything — schedule and manual runs — until Resume is pressed. A run
in progress finishes the email it is on, then halts.

## Testing

`pytest`, with unit tests mirroring the source package. Integration tests are marked and skip
cleanly when credentials are absent; they never run against a live Spitfire instance in CI.

Critical tier requires automated tests with retained, reproducible evidence, covering negative
and error scenarios, security-sensitive paths, and rollback validation. Coverage target is 80%
overall and **100% on line matching and quantity logic** — those two are where a bug becomes a
financial misstatement. Evidence is retained in [`docs/testing-evidence/`](docs/testing-evidence/).

## Deployment, support and rollback

| Question | Document |
|---|---|
| How it deploys, to where, by whom | [`docs/deployment.md`](docs/deployment.md) |
| How to undo a bad release | [`docs/rollback.md`](docs/rollback.md) |
| It broke at 2am — what now | [`docs/support-runbook.md`](docs/support-runbook.md) |
| What data it touches, and where that data lives | [`docs/data-handling.md`](docs/data-handling.md) |
| Architecture, data flow, trust boundaries | [`docs/architecture.md`](docs/architecture.md) |
| Spitfire endpoints, auth, constants | [`docs/spitfire-integration.md`](docs/spitfire-integration.md) |

Production deployment is a **separate authority** from code approval: a member of
`ppm-production-approvers`, who must not be the person who wrote the change. Nobody deploys from
a workstation.

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) before your first change — branching, commit format,
the approval matrix, and the hard rules. Read [`SECURITY.md`](SECURITY.md) for the
credential-exposure procedure. If you are using Claude Code, [`CLAUDE.md`](CLAUDE.md) governs the
session.

Critical tier requires **two human approvals**, at least one from a technically qualified Company
Developer in `ppm-developers`. Conscious Creations is an external partner and **cannot satisfy a
required approval** — we contribute and review, Premier approves.

## Support

Standards questions: Ayotunde Gibbs or Henry Noel.
Access requests, security concerns, or accidental credential exposure: raise a ticket to
`AshfordIT@Ashfordinc.com`, copying both.
Business and functional questions: Joe Higginbotham (Premier).
