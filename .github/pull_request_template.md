<!--
  ppm-receiver-automation · Risk tier: CRITICAL · Data classification: Confidential

  Every section below is required. A section left blank, or filled with "n/a" where
  it does not genuinely apply, will be sent back.

  Reminder (Standards §7.2): Critical tier needs TWO human approvals, at least one
  from a technically qualified Company Developer in @ashford/ppm-developers. The
  author cannot be one of them, and External Partner Developers cannot satisfy a
  required approval.
-->

## 1. What changed

<!-- Plain language. What does this do that it did not do before? -->


## 2. Why

<!-- The reason, not the diff. Link the ticket, decision, call or incident. -->

**Refs:**


## 3. Who wrote it

<!-- Standards §1 requires an individual audit trail. If the pusher is not the
     author, name the real author here and add a Co-authored-by trailer on the
     commit. Do not leave this to the push identity. -->

| Author | Email |
|---|---|
|  |  |


## 4. Risk and scope

- [ ] This change is **Standard** risk within a Critical repository
- [ ] This change is **high risk** — explain below

**Does this change affect any of the following?** (tick all that apply — each one
triggers **Platform Admin review** under §8)

- [ ] Authentication or authorisation
- [ ] Sensitive-data handling
- [ ] Infrastructure or deployment configuration
- [ ] `.github/workflows/`
- [ ] Dependencies (added, removed, or version-changed)
- [ ] External integrations (Microsoft Graph, Azure AI, Spitfire, Oracle)
- [ ] **Financial logic** — line matching, quantity arithmetic, or receipt posting
- [ ] None of the above

**Platform Admin review required?**  ☐ Yes  ☐ No

### Systems touched

- [ ] Microsoft Graph / receiving mailbox
- [ ] Azure AI (OCR / Document Intelligence)
- [ ] Spitfire sfPMS — **read**
- [ ] Spitfire sfPMS — **write** *(if ticked, confirm the allowlist question below)*
- [ ] Spitfire SQL Server — **read only**
- [ ] Oracle ERP *(read-only reconciliation only — nothing writes to Oracle)*
- [ ] Review interface only
- [ ] None

### Control questions — answer honestly, they are the point of this section

- [ ] This change does **not** widen the Spitfire write allowlist, reorder its deny
      check, or otherwise weaken it
- [ ] This change issues **only `SELECT`** against Spitfire SQL Server
- [ ] This change **does not write to Oracle**
- [ ] This change does **not** remove or weaken validation, logging, error handling,
      or a security control
- [ ] No database migration, schema change or backfill runs automatically as part of
      this change


## 5. Testing

<!-- Standards §7.3: do NOT claim testing that was not performed. Paste the real
     output. Critical tier requires automated tests where feasible, plus negative
     and error scenarios, and retained reproducible evidence. -->

**What I ran:**

```
```

**Result:**

```
```

- [ ] Automated tests added or updated for the business logic changed
- [ ] Negative / error scenarios covered
- [ ] Security-sensitive paths covered
- [ ] Rollback validated (or: not applicable, explained below)
- [ ] Evidence retained in `docs/testing-evidence/`

**If any box above is unticked, say why here:**


## 6. Data and secrets

- [ ] No credentials, keys, tokens, connection strings or `.env` content in this diff
- [ ] No production data — no real PO numbers, property names, vendor names, employee
      names, addresses, tracking numbers or dollar amounts, in code, tests, fixtures,
      logs, docs or comments
- [ ] All new configuration is read from environment variables and documented in
      `.env.example` with a placeholder, not a real value
- [ ] Nothing under `state/` is included in this diff

**New dependencies** (§7.3 — leave empty if none):

| Package | Version | Purpose | Licence | Security scan result |
|---|---|---|---|---|
|  |  |  |  |  |


## 7. AI assistance disclosure

<!-- Standards §8: material AI assistance must be disclosed. The human directing the
     tool remains accountable. AI cannot be the accountable developer, security
     reviewer, or approver. -->

- [ ] **No** AI assistance
- [ ] **AI-assisted** — tool and extent described below

**Tool:**
**What it did:**

- [ ] I reviewed every changed file and the complete diff myself
- [ ] For AI-generated changes touching authentication, authorisation, financial
      logic, deployment workflows, sensitive-data handling or privileged
      integrations: a qualified human assessed the change without relying on AI


## 8. Deployment plan

<!-- Standards §10. Production approval is a SEPARATE authority from this PR:
     a member of @ashford/ppm-production-approvers, and not the author. -->

**Target environment:**  ☐ Development  ☐ Test  ☐ Production

**How it deploys:**

**Configuration or secrets that must be in place first:**

**Migrations required?**  ☐ No  ☐ Yes — named below, to be run by a human against a
backed-up database. Never automatically.


## 9. Rollback plan

<!-- Must be specific and executable. "Revert the commit" is only sufficient if it
     is genuinely sufficient — say what happens to data already written. -->

**To undo this change:**

**Data written by this change, and what happens to it on rollback:**

- [ ] Rollback has been tested, or is documented in `docs/rollback.md`


## 10. Reviewer checklist

- [ ] Two approvals obtained, at least one from a qualified `@ashford/ppm-developers`
      member who is not the author
- [ ] Platform Admin review obtained where §4 above requires it
- [ ] All CI checks green — lint, type check, tests, secret scan, dependency scan
- [ ] All review comments resolved
- [ ] Branch will be deleted after merge
