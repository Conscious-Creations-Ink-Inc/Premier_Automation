# Testing evidence

Ashford GitHub Standards v1.5 §7.3: *Critical repositories must use automated tests where
technically feasible and **retain reproducible evidence** for material business logic and
production-impacting changes.*

This directory holds that evidence. An auditor should be able to open it and see what was tested,
when, against what, and what the result was.

---

## What belongs here

Evidence is required for any change touching:

- **Line matching** — which PO line a delivered item is assigned to
- **Quantity logic** — open quantity, partial receipts, over-receipt prevention
- **Receipt posting** — anything that creates or attaches to a Spitfire document
- **The Spitfire write allowlist** — the deny check and the allowlist
- **Authentication and authorisation**
- **Rollback procedures**

Those first three are where a bug becomes a **financial misstatement**. Coverage target is 100%
on matching and quantity logic; 80% overall.

## What does not belong here

- Screenshots as a substitute for a test result
- Output from a run nobody can reproduce
- **Anything containing production data.** Evidence files are committed, so the same rules apply:
  no real PO numbers, property names, vendor names, staff names, addresses or dollar amounts. See
  [`../data-handling.md`](../data-handling.md).

## Naming

```
YYYY-MM-DD_<pr-number>_<short-description>.md
```

For example `2026-09-15_042_open-quantity-partial-receipt.md`.

## What each evidence file must contain

```markdown
# <What was tested>

**Date:**
**PR:**
**Commit:**
**Author:**
**Environment:** (local / test — never production)

## What changed and why it needs evidence

## Test command

<the exact command, so someone else can re-run it>

## Result

<the real, unedited output — pass counts, failures, coverage>

## Negative and error scenarios covered

## Rollback validation
<or: not applicable, and why>

## Conclusion
```

## The rule that matters

> **Do not claim testing was completed unless it was actually performed** (§7.3).

Paste the real output. If a test was skipped, say which and why. If coverage fell short of the
target, say so and say what is not covered. An honest gap is a finding; a fabricated pass is a
standards violation, and on a repository feeding a SOX-controlled system of record it is the kind
of finding that ends an engagement.

## Rollback validation

Before any release changing posting, matching or quantity logic, exercise and record:

| Test | Why |
|---|---|
| Kill switch engages and survives a process restart | It is the emergency control |
| Reverting the change restores prior behaviour | The primary rollback path |
| The duplicate guard refuses a second post for the same `(PO, delivery)` | Spitfire has no idempotency — this guard is the only protection |
| A `CLAIMED` ledger row is recoverable by reading the receipt back | The crash-mid-chain case |

See [`../rollback.md`](../rollback.md).
