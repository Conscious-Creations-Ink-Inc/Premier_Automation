# Support runbook

`ppm-receiver-automation` · Risk tier Critical

For whoever is on call. Written to be usable by someone who did not build this.

---

## 1. Support ownership

| Role | Owner | Reach for |
|---|---|---|
| Production support owner | **TO BE NAMED BY PREMIER** — open item, required before production | First responder |
| Technical owner | Joe Higginbotham (Premier) | Spitfire, purchase orders, anything touching a posted receipt |
| Business owner | **TO BE NAMED BY PREMIER** *(Corina Heizer or Adam Okolicany)* | "Should we stop processing?" |
| Delivery partner | Conscious Creations | Code defects, pipeline behaviour |
| Platform Admins | Ayotunde Gibbs, Henry Noel | Repository, access, credential exposure |
| Production approvers | `ppm-production-approvers` | Authorising a fix into production |

> **Two ownership rows are unfilled.** Until Premier names a production support owner and a
> business owner, there is no defined first responder. This is a go-live blocker, not a
> formality — raise it rather than working around it.

## 2. Escalation

| Severity | Means | Do |
|---|---|---|
| **S1** | A receipt was posted that should not have been, or duplicates are being created | **Kill switch immediately** (`rollback.md` §1). Then Joe Higginbotham, then the business owner. |
| **S2** | Processing stopped entirely — no mail being read | Kill switch, diagnose with §4, notify the technical owner within the business day |
| **S3** | Some mail misrouted or unprocessed; nothing wrong posted | Diagnose with §4. No emergency stop needed. |
| **S4** | Cosmetic, or a single record needing manual handling | Handle through the review interface |
| **Security** | Credential exposed, suspected compromise, unexpected access | `AshfordIT@Ashfordinc.com`, cc Ayotunde Gibbs and Henry Noel — **same day**. See `SECURITY.md`. |

**When in doubt, engage the kill switch.** It is fully reversible, it survives a restart, and a
run in progress finishes its current email cleanly rather than stopping mid-write. Stopping
wrongly costs a delay. Not stopping can cost a financial misstatement.

## 3. Failure notification

> **Open item:** automated alerting is **not yet configured**. Today, failures surface only on the
> Automation page's run history — which means they are found by someone looking. Before
> production, alerting on the conditions in §4 must be routed to the named production support
> owner. Tracked as a go-live blocker.

Until then: check the Automation page's run history daily. A run that reports refused pages,
repeated errors, or a growing Needs Attention queue is the early signal.

---

## 4. Failure modes — what you will actually see

### 4.1 No mail is being read at all

| Check | Likely cause | Fix |
|---|---|---|
| Is the kill switch engaged? | Someone stopped it and did not resume | Understand why it was pulled **before** resuming |
| Is the schedule enabled? | It is **off by default** — nothing runs unattended unless turned on | Enable on the Automation page |
| Graph calls returning 403 | The app-registration permission grant was removed or lapsed | Check the grant **before** suspecting the code. Escalate to Premier / Ashford IT |
| Graph calls returning 401 | `GRAPH_CLIENT_SECRET` expired or was rotated | Update from the secret store. Secrets expire ~1 year from issue |
| Process not running | Host restarted | Restart. Confirm the kill switch state on boot |

### 4.2 Mail is read, but attachments yield nothing

| Symptom | Cause | Fix |
|---|---|---|
| Attachments filed as "service unavailable", count climbing | **The OCR page cap for the run was hit.** This is the single most common operational failure — it has previously left over a hundred attachments unprocessed while the OCR service was answering normally | Raise `PREMIER_OCR_PAGES_PER_RUN`. Then reprocess the affected mail (`rollback.md` §4) |
| `InvalidContentLength: the input image is too large` | Request exceeded the tier's cap (4 MB on the free tier) | Raise `PREMIER_OCR_MAX_UPLOAD_BYTES` on a paid tier, or accept scaling |
| Quota errors despite low volume | Calls firing back to back; each analysis costs a POST plus several poll GETs against the same allowance | Raise `PREMIER_OCR_MIN_CALL_GAP` (3s ≈ 20/min, the free tier's limit) |
| Images route to a person with a stated reason | OCR credentials absent — the mock client is in use | Set `AZURE_VISION_ENDPOINT` / `AZURE_VISION_KEY`. **This is correct behaviour**, not a bug: it never silently yields nothing |

### 4.3 Purchase orders will not resolve

| Symptom | Cause | Fix |
|---|---|---|
| "Not found in the projects this connector searches" | **The PO is outside `SPITFIRE_PROJECT_IDS`.** The service account cannot enumerate projects, so the list is fixed in configuration | Add the project ID. If Premier has not opened that project, escalate — this is a known boundary, not a defect |
| All Spitfire reads failing | The session ticket lapsed on idle | Recapture it, or escalate for the non-interactive auth path (open item) |
| Reads failing from a particular location | Spitfire answers only from an allowed network | Work from an allowed network, or use the cassette replay mode |
| Reads succeed but values look stale | Serving from the local mirror because live access is unavailable | Expected fallback. Confirm before treating mirror data as authoritative |

### 4.4 Records stuck in Needs Attention

This is **the system working**, not failing. It refuses to guess. Common reasons:

- Spec code missing or mangled on the vendor's paperwork
- Quantity does not reconcile against the open quantity on the line
- Attachment unreadable, encrypted or corrupt
- No proof of delivery present — **posting is refused outright**; the only way past is a named
  person accepting the risk on the waiver page, which is stored on the record and carried into
  the ledger

Handle through the review interface. A growing queue with no obvious cause means an upstream
change — a vendor altered their email format. That is a code change, not an operational fix.

### 4.5 Duplicate receipts, or a receipt that should not exist

**S1. Kill switch, then `rollback.md` §3.** Do not retry a post because Spitfire appears to show
nothing — Spitfire's quantities do not roll up until approval, and the local post ledger is the
only reliable record that a receipt exists.

### 4.6 The interface will not start

| Symptom | Cause |
|---|---|
| Exits immediately | Another instance is already running. Only one may run at a time; a second exits rather than binding a port beside the first |
| Restart loop | Something is writing under the state directory in a way the reloader is watching |
| Unreachable | The interface has **no authentication** — it must sit behind network controls. Check those before assuming the process is down |

---

## 5. Health checks

| Check | Where | Healthy |
|---|---|---|
| Last run completed | Automation page, run history | Within the configured interval |
| Kill switch | Sidebar, every page | Not engaged |
| Schedule | Automation page | Enabled |
| Needs Attention queue | Needs a human | Stable, not growing run over run |
| OCR refusals | Run history | Zero pages refused |
| Post ledger | Receiver report | No rows stuck at `CLAIMED` |

**A row stuck at `CLAIMED`** means a post chain started and did not finish — a receipt may exist
in Spitfire without the chain completing. Read the receipt back before doing anything else.

## 6. Routine operations

| Task | Frequency | Notes |
|---|---|---|
| Review run history | Daily | Until alerting exists (§3), this is the only failure signal |
| Clear the Needs Attention queue | Daily | Business task, not technical |
| Rotate `GRAPH_CLIENT_SECRET` | Before expiry, ~annually | Azure portal → App registrations → Certificates & secrets |
| Rotate `AZURE_VISION_KEY` | Per policy | Two keys exist; rotate the one in use while the reserve covers the gap |
| Recapture the Spitfire session ticket | On lapse | Interim, until the non-interactive path is provisioned |
| Verify backups restore | **Undefined — open item** | See below |

## 7. Open items blocking production support

| Item | Owner | Why it blocks |
|---|---|---|
| Name the production support owner | Premier | No defined first responder |
| Name the business owner | Premier | No one to authorise stopping the business process |
| Configure failure alerting | Conscious Creations + Premier | Failures are currently found by looking |
| Define backup schedule, retention and restore ownership for the state databases | Premier | `rollback.md` §5 cannot be executed without backups |
| Non-interactive Spitfire authentication | Joe Higginbotham | Unattended operation is impossible while auth depends on a hand-captured ticket that lapses |
| Authentication on the review interface | Conscious Creations | Must sit behind network controls until then |
| Stage 6 write method decision | Joe Higginbotham | Posting cannot go live |

## 8. Contacts

| Need | Contact |
|---|---|
| Spitfire, POs, posted receipts | Joe Higginbotham (Premier) |
| Repository, access, GitHub | Ayotunde Gibbs, Henry Noel |
| Credential exposure or security | `AshfordIT@Ashfordinc.com`, cc both Platform Admins — **same day** |
| Production deployment authority | `ppm-production-approvers` |
| Code defects | Conscious Creations |
