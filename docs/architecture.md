# Architecture

`ppm-receiver-automation` · Risk tier Critical · Data classification Confidential

---

## 1. What the system is

A pipeline that turns vendor delivery emails into receiver documents in Spitfire sfPMS, with a
human approving every posting. It is an **assistive** system, not an autonomous one: its output is
a proposal plus the evidence behind it.

## 2. Data flow

```
   ┌──────────────────┐
   │ Receiving mailbox│  Microsoft 365, one monitored mailbox
   └────────┬─────────┘
            │  Microsoft Graph, app-only token, Mail.ReadWrite
            ▼
   ┌──────────────────┐
   │ 1. Ingest        │  poll for new mail; fetch bodies + attachments
   │    & Triage      │  classify: delivery / status notice / noise
   └────────┬─────────┘  every message leaves with a category and a reason
            ▼
   ┌──────────────────┐
   │ 2. Accumulate    │  group multi-part shipments; hold, then sweep stale
   └────────┬─────────┘
            ▼
   ┌──────────────────┐        ┌───────────────────┐
   │ 3. Extract       │───────▶│ Azure AI (OCR)    │  photographed / scanned PODs
   │                  │◀───────│ metered, capped   │
   └────────┬─────────┘        └───────────────────┘
            │  PO numbers, spec codes, quantities, dates, signatory
            ▼
   ┌──────────────────┐        ┌───────────────────┐
   │ 4. Match         │───────▶│ Spitfire REST     │  READ: PO header, items,
   │                  │◀───────│ + local mirror    │  addresses, route
   └────────┬─────────┘        └───────────────────┘
            ▼
   ┌──────────────────┐
   │ 5. Verify        │  open qty = ContractUnits − ReceivedUnits
   └────────┬─────────┘  quantity, date and evidence checks
            │
            ├──── resolved ────▶ ┌──────────────────┐
            │                    │ Review interface │  a person approves
            │                    └────────┬─────────┘
            │                             ▼
            │                    ┌──────────────────┐
            │                    │ 6. Build/Post    │  BLOCKED — pending decision
            │                    └────────┬─────────┘
            │                             ▼
            │                    ┌──────────────────┐
            │                    │ Spitfire receipt │  WRITE, narrow allowlist
            │                    └────────┬─────────┘
            │                             ▼
            │                    ┌──────────────────┐
            │                    │ Oracle ERP       │  READ ONLY from our side
            │                    └──────────────────┘
            ▼
   ┌──────────────────┐
   │ 7. Needs         │  everything unresolved, with a written reason
   │    Attention     │
   └──────────────────┘
```

## 3. Trust boundaries

| Boundary | Direction | What crosses | Control |
|---|---|---|---|
| **Mailbox → pipeline** | in | Untrusted content. Mail subjects, bodies and attachments are attacker-influenced by definition. | Content sniffed from bytes, never filename. All markup built through one escaping module. Attachments never executed. |
| **Pipeline → Azure AI** | out | Attachment bytes for OCR | Metered and rate-limited. No credentials or unrelated data in the payload. |
| **Pipeline → Spitfire REST** | both | PO reads; receipt writes | **Path allowlist with a deny check that runs first, on the raw path.** See [`spitfire-integration.md`](spitfire-integration.md). |
| **Pipeline → Spitfire SQL** | in | PO and document reads | **`SELECT` only.** No DML or DDL in any code path. |
| **Pipeline → Oracle** | in | Reconciliation reads | **Nothing writes to Oracle.** |
| **Human → review UI** | both | Approvals, manual records, waivers | Every decision is attributed and stored. |

**The system's own posture:** it holds credentials to a production ERP and a live mailbox. It is
itself a high-value target. Credentials live only in environment variables sourced from an
approved secret store, never in source.

## 4. Local state

Runtime state is SQLite. All of it is gitignored — it holds real client documents.

| Store | Holds |
|---|---|
| Pipeline state | Live mail, attachment ledger, extracted records, post ledger |
| Development corpus | A separate store for development and test messages |
| Spitfire mirror | Cached PO headers and lines, so a lapsed session does not stop reads |
| Console | Run history, schedule settings, the kill switch |

**Two stores, never joined.** Development mail and live mail are kept apart and surfaced on
separate pages. The receiver report is evidence; evidence that mixes test data with real
receiving mail is worth nothing.

## 5. Design decisions that must not be reversed

**Attachments are read before the email is judged.** What an email *means* often lives only in
its attachment. Triage that decides on the body alone discards deliveries.

**Content type is sniffed from bytes, never from the filename.** Extensions lie, and one
attachment class arrives as MIME with a misleading name.

**The post ledger is the only duplicate guard.** Spitfire cannot tell us whether a delivery has
already been received:
- The catalog does not deduplicate — the same file uploaded twice yields two entries.
- The attachment endpoint has no idempotency — the identical body twice gives two rows.
- `ReceiptInProgressUnits` reads `0.0` on a PO that already carries an unapproved receipt.

Quantities appear to roll up only on approval, and receipts created here are deliberately never
auto-routed — so the window in which Spitfire looks untouched lasts as long as a human takes. A
guard reading Spitfire's own numbers would post a second receipt every time.

**The claim is written before the first call, not after the last.** A crash mid-chain leaves a
claimed row that is visible and recoverable. The opposite order would lose the record of a
receipt that exists, and the retry would create another.

**The kill switch is cooperative and durable.** It is a database row *and* an in-memory event.
Stopping lands between emails, at a point where the previous email is fully settled — never
mid-write, which would leave a state nobody can reason about. It survives a restart, because a
kill switch that forgets when the process bounces is not a kill switch.

**All markup is built in one module.** Never build markup with an f-string outside it; never
construct raw HTML from anything a user or mail server supplied.

## 6. Extraction is deterministic

There is **no LLM in the live path.** Extraction is format adapters plus OCR. A disabled fallback
call site exists behind `ENABLE_AI_FALLBACK`, default `False`, pending written confirmation from
Premier that confidential content may be sent to an LLM.

If it is ever enabled, these bind: versioned prompt templates rather than inline strings; every
call returns into a validated schema; parse failures route to Needs Attention and never fall
through to a default; prompt version, model name and token usage logged on every call;
temperature 0, because determinism is an audit requirement here.

## 7. Known architectural gaps

| Gap | Impact | Owner |
|---|---|---|
| No authentication on the review interface | Must be behind network controls until added | Conscious Creations |
| Structured logging with correlation IDs incomplete | Harder to trace one email end to end | Conscious Creations |
| Spitfire auth relies on a hand-captured session ticket | Lapses on idle; no unattended operation | Joe Higginbotham |
| PO discovery limited to a fixed project list | POs outside it do not resolve | Joe Higginbotham |
| Stage 6 write method undecided | Blocks go-live | Joe Higginbotham |
