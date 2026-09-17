# Contributing — `ppm-receiver-automation`

**Risk tier: Critical. Data classification: Confidential.**
Governed by Ashford GitHub Repository and Developer Standards v1.5 (August 2026).

This repository creates documents that affect financial reporting and fixed-asset
capitalisation, and it feeds a SOX-controlled system of record. Assume every change you make
will be read by an auditor. These rules are hard requirements, not style preferences.

---

## 1. Before your first contribution

- Individual GitHub account, company email verified, **MFA enabled**, recovery codes vaulted.
  Shared accounts are prohibited.
- Work only from a company-managed or explicitly approved device.
- Use only approved tooling. Personal AI or GitHub subscriptions require written approval.
- Read `CLAUDE.md` and `SECURITY.md`.

## 2. Branching

- **Never work on `main`.** Never commit to it directly. Force-push and deletion of `main` are
  prohibited.
- Branch from the latest `main`.
- Name: `<type>/<short-kebab-description>` where type is `feature`, `fix`, `documentation`,
  `chore`, `refactor`, or `test`.
  - `feature/graph-mailbox-poller`, `fix/line-match-open-quantity`,
    `documentation/spitfire-integration-runbook`
- **One logical change per branch.** Do not bundle a refactor, a bug fix and a feature. A
  Critical-tier reviewer will reject it, and they will be right.
- Delete the branch after merge.

## 3. Commits

Conventional Commits, with mandatory trailers.

```
<type>(<scope>): <imperative summary, max 72 chars>

<body — what changed and why. Wrap at 100 chars. Explain the reasoning,
not the diff; the diff is already visible.>

Refs: <ticket or decision reference>
Tested: <what was actually run and what the result was>
Assisted-by: Claude Code
Co-authored-by: Full Name <name@consciouscreations.ai>
```

- **Type:** `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`, `build`, `ci`
- **Scope:** `mail`, `extract`, `match`, `spitfire`, `review`, `orchestrator`, `dashboard`,
  `config`, `docs`, `ci`, `deps`

**`Tested:` must be truthful.** Do not claim testing that was not performed. If nothing was run,
write `Tested: none — documentation only`. Fabricating a test result on a Critical repository is
a standards violation, not a shortcut.

**`Assisted-by: Claude Code`** is required on any commit where AI materially contributed, and
the assistance must also be disclosed in the pull request body.

**Authorship.** Manjeet Kumar is the only person who pushes, so GitHub will attribute pushes to
him. Every commit authored by someone else **must** carry a `Co-authored-by:` trailer naming the
real author, and should set the commit author properly (`git commit --author`) where practical.
Manjeet is the release gatekeeper, not the author of record.

**When to commit.** Often, in small coherent steps. Every commit must leave the branch passing
lint and unit tests — squash before pushing if it does not. Do not push until the change is
complete and internally reviewed.

## 4. Pull requests

1. Manjeet pushes the branch and opens the PR, completing the template in full.
2. CI must be green: lint, type check, unit tests, secret scan, dependency scan.
3. **Two approvals required** (Critical tier), at least one from a technically qualified Company
   Developer in `ppm-developers`. **We cannot supply either** — External Partner Developers may
   join the discussion but cannot satisfy a required approval. Joe Higginbotham is Premier's
   named approver.
4. **Platform Admin review** is additionally required for anything touching authentication,
   authorisation, sensitive data, infrastructure, `.github/workflows`, dependencies, or external
   integrations. On this repository that is frequent.
5. All comments resolved, all checks green.
6. Squash merge, preserving the full conventional-commit message as the squash body.
7. Production deployment is a **separate authority** — `ppm-production-approvers`, and not the
   person who wrote the change. Nobody deploys from a workstation.

Approval latency is a known project risk. Give Joe advance notice before a PR lands, and batch
related work into fewer, larger-context PRs rather than a stream of small ones.

---

## 5. Hard rules — non-negotiable

1. **No secrets in the tree.** No passwords, API keys, tokens, certificates, private keys,
   connection strings, `.env` files, production datasets, or personal/financial data. If you find
   one already committed, **deleting the file is not enough** — report it (see `SECURITY.md`) and
   rotate it, because Git history retains it.
2. **No production data anywhere** — not in tests, fixtures, logs, docstrings, prompts, or docs.
   Real PO numbers, property names, vendor names, employee names, addresses, tracking numbers
   and dollar amounts are all production data. Synthetic only. Use `.test` domains.
3. **All configuration comes from environment variables**, through `config.py` (Pydantic
   Settings). Zero hardcoded hosts, paths, credentials, tenant IDs or subscription IDs anywhere
   else. The domain constants in `spitfire/constants.py` are the only permitted literals.
4. **Never remove validation, logging, error handling, or a security control to make a test
   pass.** Fix the code or fix the test.
5. **Spitfire SQL Server is `SELECT` only.** No `INSERT`, `UPDATE`, `DELETE` or DDL against any
   `xsf*` table in any code path — tests and one-off scripts included.
6. **Nothing writes to Oracle.** Read-only reconciliation only. If you find code that writes to
   Oracle, stop and escalate.
7. **The Spitfire write allowlist is a security control.** `write_client.py` denies on the raw
   path *before* consulting its allowlist. Do not widen `_ALLOWED_WRITES`, do not reorder the
   deny check, and never add a path to make a test pass. Changes require Platform Admin review.
8. **Migrations are written, never auto-run.** Produce the file, document what it changes and how
   to reverse it, and stop. A human runs it against a backed-up database.
9. **Never commit anything under `state/`.** It holds real client attachments and databases. No
   `git add -f`.

## 6. Python standards

- Python 3.11+, matching `pyproject.toml`.
- **Ruff** for lint and format, line length 100. Fix every finding; no blanket `# noqa`.
- **Full type annotations** on every function signature, including return types. `mypy --strict`
  clean, or as close as the codebase allows with documented exceptions.
- **Pydantic v2 models at every external boundary** — Graph responses, OCR output, LLM output,
  Spitfire request and response bodies. Never pass raw dicts between modules.
- **Docstrings** on every public module, class and function. Google style. Say what it does, what
  it assumes, and what it raises.
- **No bare `except:`.** Catch specific exceptions, log with context, then re-raise or route to
  Needs Attention. Never swallow silently. Prefer a specific exception to `except Exception`.
- **Structured logging only.** JSON formatter, and every log line carries a correlation ID that
  traces one email through the whole pipeline. **Never log credentials, full email bodies, or
  attachment contents.**
- **No `print()` in library code.** CLI entry points under `cli/` may write to stdout — that is
  their interface.
- Functions do one thing. Over ~50 lines or more than 5 parameters, split it.
- No global mutable state. Inject dependencies; do not import them at call sites.
- **All I/O goes through a client class** — Graph, Azure AI, Spitfire, SQL — with explicit
  timeout, retry with exponential backoff, and circuit-breaking on repeated failure. No naked
  `requests.get`.
- **Idempotency is mandatory.** Reprocessing the same email must never create a duplicate
  receiver. Enforce with a deterministic idempotency key persisted before the write.

## 7. Extraction and AI

- Extraction is **deterministic today** — format adapters plus OCR. There is no LLM in the live
  path.
- `ENABLE_AI_FALLBACK` stays `False` until Premier answers checklist #1b in writing, confirming
  that confidential content may be sent to an LLM. **Do not enable it, and do not build around
  the assumption that it will be enabled.**
- If and when it is enabled, these bind: prompts live in versioned template files, never as
  inline strings; every call returns into a Pydantic schema; parse failures route to Needs
  Attention and never fall through to a default; log prompt version, model name and token usage
  on every call; temperature 0 — determinism is an audit requirement here, not a preference;
  never send credentials or restricted data into a prompt.

## 8. Tests

- `pytest`. Unit tests mirror the source package structure one-for-one.
- Integration tests are marked and **skip cleanly when credentials are absent**. They must never
  be required to run in CI against a live Spitfire instance.
- Critical-tier tests must include **negative and error scenarios, security-sensitive paths, and
  rollback validation**.
- Coverage target **80%** overall; **100% on line matching and quantity logic**. Those two are
  where a bug becomes a financial misstatement.
- Fixtures are synthetic and committed. **Preserve the real email grammar, substitute the
  identifiers** — fixtures that invent their own message shapes produce a green suite that proves
  nothing.
- The full suite takes roughly 21 minutes. Do not edit files while it runs; that produces false
  failures.

## 9. Dependencies

- **Do not add a dependency without explicit approval.** Document its purpose, licence, and
  security scan result in the PR.
- Pin versions. Dependency changes trigger Platform Admin review.

## 10. Repository hygiene

- No commented-out code. Git remembers it.
- No `test.py`, `scratch.py`, `final_v2.py`, `old/`, `backup/`, `temp/`, or stray notebooks.
- No `TODO` without an owner and a reference — `# TODO(rahul): handle multi-PO emails — Stage 5`.
- No unused imports, dead functions, or unreachable branches.
- Consistent naming: `snake_case` for Python, `PascalCase` for classes.
- Every directory that matters has a module docstring or short `README.md` explaining its role.
- `CHANGELOG.md` maintained from the first release, Keep a Changelog format.

## 11. AI-assisted development

- The human directing the tool is **accountable** for the change. AI cannot be the accountable
  developer, business owner, security reviewer, or production approver.
- AI-generated code is **untrusted until reviewed and tested**.
- Review every changed file and the complete diff before committing.
- Review commands before authorising them — especially package installation, network access,
  GitHub administration, and cloud operations.
- Never let an AI tool bypass branch rules, tests, security scans, reviews, or production
  approvals, and never let it merge, approve or deploy.
- Never give it production credentials or restricted data.
- Changes to authentication, authorisation, financial logic, deployment workflows,
  sensitive-data handling or privileged integrations need a qualified human reviewer who does
  not rely on AI to assess them. **Financial logic here means line matching, quantities, and
  receipt posting.**

See `CLAUDE.md` for the machine-readable version of these restrictions.
