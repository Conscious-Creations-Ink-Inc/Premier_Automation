# Data handling

`ppm-receiver-automation` · **Data classification: Confidential**

Ashford GitHub Standards v1.5 §9 and §12.

---

## 1. What data this system touches

| Data | Source | Classification | Why it is needed |
|---|---|---|---|
| Vendor delivery emails — subjects, bodies, headers | Monitored mailbox, via Microsoft Graph | Confidential | The delivery facts exist only here |
| Attachments — proof of delivery, bills of lading, warehouse receipts, packing lists, photographs | Same | Confidential | Evidence for the receipt; often the only source of quantity and date |
| Purchase-order headers and lines — numbers, spec codes, descriptions, quantities, cost codes, unit values | Spitfire sfPMS | Confidential | The set a delivery is matched against |
| Vendor and supplier identities | Both | Confidential | Matching, and the vendor chase flow |
| Property and project identifiers | Both | Confidential | Routing the receipt to the right project |
| Names of individuals who signed for goods | Proof-of-delivery documents | Confidential — **personal data** | Recorded on the receipt as evidence |
| Premier staff names and addresses | Email signatures and forwarding wrappers | Confidential — **personal data** | Incidental; captured because it is in the mail body |
| Receipt documents created | Produced by this system | Confidential — **financially material** | The output |

**Personal data is present.** Signatories, expeditors and warehouse staff appear in delivery
paperwork. It is incidental to the purpose but it is real, and it is why message bodies and
attachment contents are never logged.

## 2. Where the data lives

| Location | Contents | Controls |
|---|---|---|
| Microsoft 365 mailbox | Source mail | Premier's tenant. We hold a scoped app-only token, `Mail.ReadWrite`, read plus move only |
| Azure AI service | Attachment bytes, transiently, for OCR | Runs in the approved Azure tenant. **Residency sign-off is an open item** — see §6 |
| Local SQLite state | Mail bodies, attachment blobs, extracted records, the post ledger | On the application host. Several gigabytes of client documents. **Gitignored, never committed** |
| Spitfire sfPMS | Purchase orders, receipts | Premier's system of record |
| This repository | **Source code and synthetic fixtures only** | No production data, ever |

## 3. What must never enter this repository

Prohibited under §9 and §12, and treated as equally serious here:

**Credentials** — passwords, API keys, tokens, certificates, private keys, connection strings,
`.env` contents.

**Production data**, which includes all of:

- Real purchase-order, receipt, notice, shipment or tracking numbers
- Real property, hotel, or project names and project codes
- Real vendor names, domains, or contact addresses
- Real employee or third-party individual names
- Real postal addresses
- Real dollar amounts
- Real hostnames, tenant IDs, or subscription IDs
- Any real email body, attachment, or document

This applies to source, tests, fixtures, logs, docstrings, comments, documentation, commit
messages, pull-request text, and AI prompts.

### Local-only paths

| Path | Why |
|---|---|
| `.env` | Live credentials. The only file the code reads for them |
| `ACCESS.local.md` | Plaintext credential ledger for human lookup |
| `state/` | Real client attachments, recovered mail, pipeline databases |
| `state/spitfire_cassettes/` | Recorded Spitfire responses — real vendor names, named staff, cost codes |
| `dev_reports/` | Internal working documents |

All are covered by `.gitignore`. **Never `git add -f` any of them.**

## 4. Test data

Fixtures are **synthetic** and committed.

The rule is: **substitute the identifiers, preserve the grammar.** Fixtures mirror the shape of
real messages — subject grammars, table layouts, the forwarding wrappers expeditors add, the item
prefixes — because fixtures that invent their own message shapes produce a green suite that
proves nothing about production behaviour. That failure has already happened once on this project
and was logged as a go-live blocker.

So: keep the structure exactly, and replace every identifier.

- Use `.test` domains (RFC 6761 — reserved, and can never resolve to a real host).
- Keep number *lengths* identical, so digit-count-sensitive parsing exercises the same paths.
- Never derive a fixture from a real document without anonymising it first.

## 5. Data minimisation in the code

- **Logs never carry credentials, full email bodies, or attachment contents.** Log identifiers
  and verdicts, not payloads.
- **OCR runs once, at ingest.** The verdict is stored on the attachment ledger and everything
  downstream reads those columns rather than re-reading the file. Fewer reads, less exposure,
  less spend.
- **Least privilege.** The mailbox token is scoped to one mailbox. The Spitfire service account
  needs read on purchase orders and their lines and nothing else.
- **Nothing is sent to an LLM.** Extraction is deterministic. The disabled fallback stays
  disabled until Premier confirms in writing that confidential content may be sent — see §6.

## 6. Open items

| Item | Owner | Status |
|---|---|---|
| **Cloud AI data-residency sign-off** — confirmation that attachment content may be processed by the Azure AI service, and where that processing happens | Corina Heizer (Premier) | Outstanding |
| **Checklist #1b** — whether confidential content may be sent to an LLM at all. `ENABLE_AI_FALLBACK` stays `False` until answered in writing | Premier | Outstanding |
| **Exchange Application Access Policy** — confirmation the app registration is scoped to the single receiving mailbox rather than tenant-wide | Joe Higginbotham | Outstanding — verify with `Get-ApplicationAccessPolicy` |
| **Retention** — how long mail bodies and attachment blobs are kept in local state | Premier | Undefined |
| **Backup and restore** — schedule, retention, and who owns restoring | Premier | Undefined; blocks `rollback.md` §5 |

Until the Exchange access policy is confirmed, treat the mailbox permission as **broader than
intended** and handle accordingly.

## 7. If data is exposed

Treat a production-data leak the same way as a credential leak: report first, remediate second,
and understand that **deleting the file does not remove it from Git history**.

Report to `AshfordIT@Ashfordinc.com`, copying Ayotunde Gibbs and Henry Noel. Notify Joe
Higginbotham if Premier's operational data is involved. Full procedure in `SECURITY.md`.
