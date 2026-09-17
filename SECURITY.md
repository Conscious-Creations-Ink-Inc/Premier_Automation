# Security Policy — `ppm-receiver-automation`

**Risk tier: Critical. Data classification: Confidential.**
Governed by Ashford GitHub Repository and Developer Standards v1.5, §9.

This repository reads a live mailbox, holds credentials to a production ERP, and creates
documents that affect financial reporting. Treat every security question here as urgent.

---

## 1. Reporting a security issue

**Do not open a public issue, and do not discuss it in a pull request comment.**

Submit a ticket to **`AshfordIT@Ashfordinc.com`**, copying:

- **Ayotunde Gibbs** — GitHub Platform Administrator
- **Henry Noel** — GitHub Platform Administrator

Also notify **Joe Higginbotham** (Premier technical owner) if the issue affects Spitfire, the
receiving mailbox, or purchase-order data.

Include: what you found, where, when you found it, what systems or data are affected, and
whether it is still live. If you are unsure whether something counts as a security issue, report
it. There is no penalty for a false alarm and a real one for silence.

## 2. Credential exposure — the response procedure

This applies to any password, API key, token, certificate, private key, connection string, or
`.env` content that reaches this repository, a pull request, a log, an issue, a chat message, a
screenshot, or an AI prompt.

### Step 1 — Report immediately. Do not wait to investigate.

Ticket to `AshfordIT@Ashfordinc.com`, cc Ayotunde Gibbs and Henry Noel. Say plainly what was
exposed, where, and for how long.

### Step 2 — Rotate or revoke the credential.

| Credential | Where to rotate |
|---|---|
| `GRAPH_CLIENT_SECRET` | Azure portal → App registrations → the app → Certificates & secrets |
| `AZURE_VISION_KEY` | Azure portal → the Azure AI resource → Keys and Endpoint (rotate the key in use; the reserve key covers the gap) |
| `SPITFIRE_SESSION_COOKIE` | Not rotatable — invalidate the session and recapture. Notify Joe Higginbotham. |
| Spitfire service account password | Premier — Joe Higginbotham |

### Step 3 — Understand that deleting the file is **not** sufficient.

> **Git history retains the value.** Removing the secret in a later commit leaves it fully
> readable in the commit that introduced it, in every clone, fork, and CI cache. The credential
> must be treated as compromised and rotated **regardless** of whether the file was deleted,
> the commit amended, or the branch force-pushed.

Purging history is a separate remediation, requires Platform Admin involvement, and **never
replaces rotation**.

### Step 4 — Record it.

Note what was exposed, when, when it was rotated, and by whom. Critical-tier repositories are
audited.

## 3. What must never be committed

Passwords · API keys · tokens · certificates · private keys · connection strings · `.env` files ·
production datasets · sensitive personal or financial data.

Also prohibited as production data under §12, and equally serious here:

- Real purchase-order, receipt, or shipment numbers
- Real property, hotel, or project names
- Real vendor names, domains, or contact addresses
- Real employee or third-party individual names
- Real postal addresses, tracking numbers, or dollar amounts
- Real Spitfire hostnames, project codes, tenant IDs, or subscription IDs

Use synthetic data and `.test` domains (RFC 6761 — reserved, and can never resolve).

### Files that are local-only and must never be tracked

| File / path | Contents |
|---|---|
| `.env` | Live credentials. The only file the application reads. |
| `ACCESS.local.md` | Plaintext credential ledger, for human lookup. |
| `state/` | Real client attachments, proof-of-delivery documents, and pipeline databases. |
| `state/spitfire_cassettes/` | Recorded Spitfire responses containing real vendor names, named Premier employees, and cost codes. |

All are covered by `.gitignore`. **Never `git add -f` any of them.**

## 4. Secret management

- Use approved secret-management tooling, including **Azure Key Vault** where applicable.
- Configuration reaches the application through environment variables only, loaded via
  `config.py`. No credential is ever a literal in source.
- Prefer short-lived authentication — **Azure OpenID Connect** — over long-lived secrets for
  deployment.
- Separate identities and permissions for development, test and production.
- Least privilege everywhere: the Spitfire service account needs **read** on purchase orders and
  their lines, and nothing else.

## 5. Security controls in this repository

These are controls, not conveniences. Weakening one requires Platform Admin review.

- **Spitfire write allowlist** (`write_client.py`) — a deny check runs on the raw path *before*
  the allowlist is consulted, so an error in the allowlist cannot authorise a denied route.
  Covered by dedicated tests. Do not widen it, reorder it, or add a path to make a test pass.
- **SQL read-only discipline** — `SELECT` only against Spitfire. No DML or DDL against `xsf*`
  tables in any path, tests included.
- **No Oracle writes** — read-only reconciliation only.
- **Kill switch** — one switch stops all automated processing and stays stopped.
- **No autonomous posting** — nothing reaches Spitfire without a human approving it.

## 6. Required scanning (Critical tier, §9)

- Dependency scanning — required
- Secret scanning **with push protection** — required
- Static code / security analysis — required where technically applicable

Findings above the approved threshold must be resolved or formally accepted before production.

## 7. AI tooling

- Never provide production credentials, restricted data, or unrelated company documents to an AI
  tool.
- Never paste `.env` or `ACCESS.local.md` contents into a prompt.
- AI-generated code is untrusted until reviewed and tested by a human.
- An AI tool must never merge, approve, or deploy a change.

See `CLAUDE.md` for the enforced restrictions.

## 8. Questions

Ayotunde Gibbs or Henry Noel for standards questions. `AshfordIT@Ashfordinc.com` (cc both) for
access requests, security concerns, or accidental credential exposure.
