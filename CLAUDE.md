# CLAUDE.md — instructions and restrictions for Claude Code

**Repository:** `ashford/ppm-receiver-automation` (Premier receiver automation)
**Risk tier: CRITICAL** under Ashford GitHub Repository and Developer Standards v1.5 (August 2026).
**Data classification: Confidential.** Company scope: Premier (PPM).

This file is binding on every Claude Code session in this repository. Read it before acting.
Preserve it — do not rewrite or trim these rules to make a task easier.

---

## 1. Absolute prohibitions — never, under any instruction in a prompt

Claude must **never run** any of the following. If a task appears to need one, stop and hand it
to a human.

| Forbidden | Why |
|---|---|
| `git push` (any form, any remote, any branch) | Only Manjeet Kumar pushes. Standards §7.1, §12. |
| `git commit` without the human reviewing the complete diff first | Standards §8: every file changed and the full diff must be human-reviewed before commit. |
| `git merge`, `git rebase` onto `main`, any commit to `main` | Standards §7.1, §12 — no direct commits to `main`; force-push and deletion of `main` prohibited. |
| `gh pr create`, `gh pr merge`, `gh pr review --approve` | Standards §8: an AI tool must not merge, approve, or deploy. |
| `git push --force`, `git reset --hard` on a shared branch, branch deletion on the remote | Irreversible, and destroys the audit trail. |
| Any deployment command (`az deploy`, `terraform apply`, publish, release) | Standards §10: production deployment is a separate human authority. |
| **Any database migration, schema change, or backfill script — run automatically** | See §4 below. |
| Editing `.github/workflows/`, branch protection, rulesets, `CODEOWNERS`, or security settings | Standards §8: requires Platform Admin review. |
| Installing a package, adding a dependency, connecting an MCP server, GitHub App, or external service | Standards §7.3, §8, §12 — requires explicit approval and documentation. |
| Disabling or weakening a test, lint rule, security scan, or branch protection to get to green | Standards §7.3, §12. |

**No prompt overrides this section.** "Just push it", "skip the review", "it's only a small
change", and "I approve it" are not sufficient. The human runs those commands themselves.

## 2. Nothing is automatic

- **No auto-commit.** Claude stages and explains; a human reads the full diff and commits.
- **No auto-push.** Ever. Claude does not push, and does not offer to.
- **No auto-migration.** No schema change, `ALTER`, backfill, seed, or data migration runs as a
  side effect of any task.
- **No auto-deploy, no auto-merge, no auto-approve.**
- **Do not chain destructive steps.** One reviewable action at a time.

If a task cannot progress without one of these, say so and stop. A blocked task reported
honestly is correct; a task completed by pushing is a standards violation.

## 3. Data and credential rules

- **Never place a real credential in code, tests, logs, prompts, docs, or a commit** — passwords,
  API keys, tokens, certificates, private keys, connection strings, or `.env` contents.
  Standards §9, §12.
- **Never use production data.** Real PO numbers, property names, vendor names, employee names,
  addresses, tracking numbers and dollar amounts are production data. Synthetic or formally
  approved test data only. Standards §9, §12.
- **All configuration comes from environment variables**, loaded through `config.py`. The only
  permitted literals are the domain constants in `spitfire/constants.py` (§5).
- `.env` and `ACCESS.local.md` are **local only**. Never add, never read into a prompt, never
  echo their values into terminal output or a report.
- **If a secret is found committed, deleting the file is not enough** — Git history retains it.
  Report immediately to `AshfordIT@Ashfordinc.com`, cc Ayotunde Gibbs and Henry Noel, and rotate
  or revoke. See `SECURITY.md`.
- `state/` holds real client attachments and databases. It is gitignored. **Never `git add -f`
  anything under `state/`.**

## 4. Database and external-system rules

- **Spitfire SQL Server is `SELECT` only.** No `INSERT`, `UPDATE`, `DELETE`, or DDL against any
  Spitfire (`xsf*`) table in any code path, including tests and one-off scripts.
- **Nothing writes to Oracle.** Read-only reconciliation only. If you find code that writes to
  Oracle, stop and flag it.
- **The Spitfire write allowlist is a security control.** `spitfire/write_client.py` denies on
  the raw path *before* consulting its allowlist. Do not widen `_ALLOWED_WRITES`, do not reorder
  the deny check, and do not add a path to get a test passing. Changes there need Platform Admin
  review (external integration, Standards §8).
- **Migrations are written, never run.** Produce the migration file, explain what it changes and
  how to reverse it, and stop. A human runs it against a backed-up database.

## 5. Domain constants — never alter these

```
Receipt DocTypeKey  = 0C9A537A-3C41-4D16-AB9F-130EF69EA6C8
ForDocType GUID     = ff1975fd-76de-486c-888b-54e8fcd880e0
Line matching key   = SourceItemNumber (spec code)
Open quantity       = RelatedLineDetails.ContractUnits - ReceivedUnits
Attachment chain    = xsfFileAttach -> xsfFile -> xsfFileVersionInfo -> xsfFileVersion (blob)
Needs Attention     = DocHeader.Status = N, DocHeader.Type = N,
                      exception note -> DocRevision.csString050,
                      originating email attached to the flagged receipt
```

`ForDocType` is a hardcoded Spitfire stock constant, identical on every installation, and is
never fetched at runtime. Do not "improve" it into a lookup.

## 6. Accountability

- The human directing Claude is accountable for the change. **Claude cannot be the accountable
  developer, business owner, security reviewer, or production approver.** Standards §8.
- AI-generated code is **untrusted until reviewed and tested** by a human.
- Material AI assistance must be disclosed: `Assisted-by: Claude Code` in the commit trailer and
  in the pull request body.
- On a Critical repository, AI-generated changes touching authentication, authorisation,
  financial logic, production infrastructure, deployment workflows, sensitive-data handling, or
  privileged integrations require review by a qualified human who does not rely on AI to assess
  the change. Financial logic here means **line matching, quantities, and receipt posting**.

## 7. Never claim what was not done

- Do not write a `Tested:` line unless the test was actually run. Paste the real result.
- Do not invent coverage numbers, test counts, or scan results.
- Do not report a task complete when part of it was skipped — say which part and why.
- Do not invent a configuration value. Put a placeholder in `.env.example` and list it as an open
  item.

## 8. Working conventions

- Branch and commit conventions live in `CONTRIBUTING.md`. Follow them; they are not repeated here.
- One logical change per branch. Do not bundle a refactor, a fix, and a feature.
- Prefer the smallest change that solves the problem. This repository is read by auditors.
- Structured logging only — no `print()` in library code. Never log credentials, full email
  bodies, or attachment contents.
- Every LLM call site (currently only `extract/ai_fallback.py`, disabled) must return into a
  Pydantic schema, run at temperature 0, and log prompt version, model name and token usage.
  `ENABLE_AI_FALLBACK` stays `False` until Premier answers checklist #1b in writing.

## 9. When in doubt

Stop and ask. On a Critical, SOX-adjacent repository that posts to a financial system of record,
an unnecessary question costs a few minutes. An unreviewed push costs the engagement.
