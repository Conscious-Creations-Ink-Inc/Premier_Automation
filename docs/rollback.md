# Rollback

`ppm-receiver-automation` · **Read this before you need it.**

Auditors read this document. So does whoever is on call at 2am. Every procedure here is meant to
be executable by someone who did not write the code.

---

## 0. First, decide which kind of rollback you need

| Situation | Go to |
|---|---|
| The system is misbehaving and you need it to stop **now** | [§1 Emergency stop](#1-emergency-stop--do-this-first) |
| Bad release — the code is wrong, nothing has been posted | [§2 Code rollback](#2-code-rollback) |
| A receipt was posted to Spitfire that should not have been | [§3 Reversing a posted receipt](#3-reversing-a-posted-receipt) |
| Mail was processed and filed incorrectly | [§4 Reprocessing mail](#4-reprocessing-mail) |
| Local data is corrupt | [§5 State recovery](#5-state-recovery) |

---

## 1. Emergency stop — do this first

**If you are unsure whether damage is ongoing, stop the system before diagnosing.** Stopping is
cheap and fully reversible. Diagnosing while it keeps running is not.

1. Open the review interface. **The kill switch is pinned to the bottom of the sidebar on every
   page.** Press it.
2. Confirm it engaged — the sidebar shows the stopped state and the time it was pulled.

**What the kill switch does:**
- Stops the schedule and manual runs.
- **Survives a process restart.** It is a database row as well as an in-memory flag, so a bounce
  does not silently resume.
- A run in progress **finishes the email it is on**, then halts. This is deliberate: stopping
  mid-write would leave a half-settled email — ledgered attachments with no verdict, an
  accumulation row with no log entry — which is precisely the state nobody can reason about
  afterwards. Expect a stop to land within one email, not within one instruction.

**It stays stopped until someone presses Resume.** Do not resume until the cause is understood.

**If the interface is unreachable:** stop the process itself. The kill-switch row is already
persisted, so if it was engaged before the process died it will still be engaged on restart.

---

## 2. Code rollback

Nothing was posted; the deployed code is simply wrong.

1. **Engage the kill switch** (§1). Do this first even for a "safe" rollback.
2. Identify the last known-good merge commit on `main`.
3. Open a `fix/` branch and **revert** the offending merge. Do not force-push, do not rewrite
   `main` history — both are prohibited.
   ```bash
   git checkout -b fix/revert-<what>
   git revert -m 1 <merge-commit-sha>
   ```
4. The revert goes through the normal pull-request process: **two approvals**, Platform Admin
   review if it touches auth, sensitive data, infrastructure, workflows, dependencies or
   integrations.
5. Redeploy through the approved pipeline. **Not from a workstation.**
6. Release the kill switch only after verifying the fix on a small batch.

**Emergency exception:** if a Critical incident makes the normal PR path too slow, that is an
approved-emergency-process question for the Platform Admins, not a decision to make alone. Contact
`AshfordIT@Ashfordinc.com`, cc Ayotunde Gibbs and Henry Noel. Commits direct to `main` outside an
approved emergency process are prohibited.

---

## 3. Reversing a posted receipt

**This is the one that matters. Read the whole section before acting.**

### 3.1 Why you cannot simply "delete and retry"

Spitfire has **no idempotency** on the paths this system uses. All three of these were measured
against the training instance:

- The catalog does not deduplicate — the same file uploaded twice produced two different file
  keys and two separate catalog entries.
- The attachment endpoint has no idempotency — the identical body posted twice gave two rows,
  success both times.
- `RelatedLineDetails.ReceiptInProgressUnits` — the field whose own schema calls it *"units
  tentatively received not yet approved"* — reads **`0.0`** on a PO that already carries an
  unapproved receipt.

That last one is the trap. Quantities appear to roll up only on approval, and receipts created by
this system are deliberately never auto-routed. So the window in which Spitfire *looks* untouched
lasts as long as a human takes to approve — not seconds.

> **Consequence: retrying a post because "Spitfire shows nothing" will create a second receipt.**
> The local post ledger is the **only** thing that knows a receipt exists. Trust it over Spitfire.

### 3.2 Procedure

1. **Engage the kill switch** (§1). Non-negotiable — a scheduled run must not race you.
2. **Read the post ledger** for the affected delivery. Find the row and its state.
   - `CLAIMED` — the claim was written but the chain did not complete. **A receipt may still
     exist in Spitfire.** Verify by reading it back before doing anything else.
   - Completed — the receipt exists, and the ledger records its document key.
3. **Read the receipt back from Spitfire** using the recorded key. Confirm what actually exists
   before you try to reverse it. Do not assume.
4. **Do not delete the receipt from the database.** This system has no delete path against
   Spitfire, and creating one is not a rollback step — it is a schema-level change requiring
   Platform Admin review and Premier's approval.
5. **Void the receipt in Spitfire through Spitfire's own interface**, or ask Premier to. This is
   a Premier action on a Premier system of record. Contact the technical owner.
6. **Leave the post-ledger row in place** and annotate it. Deleting it removes the duplicate
   guard, and the next run will post again.
7. Record the incident: what posted, when, its document key, who voided it, when.

### 3.3 If a duplicate receipt was already created

1. Kill switch on.
2. Identify every affected `(PO, delivery)` pair from the post ledger.
3. Hand the list to Premier's technical owner. Voiding duplicates in a SOX-controlled system of
   record is Premier's action, not ours.
4. Before resuming, establish **why** the guard was bypassed. The usual cause is a ledger row that
   was deleted, or a post attempted with the ledger pointed at a different database file.

---

## 4. Reprocessing mail

Safe. Reprocessing is idempotent by design — dedupe is by content fingerprint per message,
delivery key per record, and evidence key per post, not by timestamp.

1. Kill switch on.
2. Re-run the affected mail through the pipeline.
3. Verify in the review interface that no duplicate records appeared.
4. Kill switch off.

**Overlap is safe.** Each ingest run deliberately re-reads a short window to cover clock skew and
mail that arrived mid-run.

**One caveat:** processed messages are *moved* into a filed folder, so the Graph message `id`
changes on move. `internetMessageId` is carried alongside it for exactly this reason. If you are
matching messages by hand, match on `internetMessageId`.

---

## 5. State recovery

Runtime state is SQLite, and none of it is in Git.

1. Kill switch on. **Stop the process** — SQLite files are written continuously by the arrivals
   watch.
2. Restore the affected database file from backup.
3. **Restore the post ledger and the pipeline state together, from the same point in time.** A
   post ledger older than the pipeline state loses the record of receipts that exist, and the next
   run will post them again. This is the most dangerous mistake in this document.
4. Restart, verify run history and the post ledger read correctly, then release the kill switch.

**If no backup exists:** do not resume automated posting. Reconcile against Spitfire by hand
first, and rebuild the post ledger from what Spitfire actually holds.

> **Open item:** the backup schedule, retention and restore ownership for these databases are not
> yet defined. This must be agreed with Premier before production. Tracked in
> [`support-runbook.md`](support-runbook.md).

---

## 6. Rollback validation

Critical tier requires rollback validation (§7.3). Before a release that changes posting, matching
or quantity logic, exercise and record:

| Test | Evidence |
|---|---|
| Kill switch engages, and stays engaged across a restart | `docs/testing-evidence/` |
| A revert of the change restores prior behaviour | PR body |
| The duplicate guard still refuses a second post for the same `(PO, delivery)` | Test result |
| A `CLAIMED` ledger row is recoverable by reading the receipt back | Test result |

---

## 7. Who to call

| Situation | Contact |
|---|---|
| Anything touching a posted receipt | Joe Higginbotham (Premier technical owner) |
| Production deployment or rollback authority | `ppm-production-approvers` |
| Credential exposure | `AshfordIT@Ashfordinc.com`, cc Ayotunde Gibbs and Henry Noel |
| Repository or access issues | Platform Admins |
