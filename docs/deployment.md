# Deployment

`ppm-receiver-automation` · Risk tier Critical

Ashford GitHub Standards v1.5 §10.

---

## 1. Current status — read this first

> **There is no production deployment.** Production status is **Development**. The system runs
> today on a developer host against Premier's live mailbox and Spitfire's training instance.
> Everything in §3 onward is the **agreed target**, not a description of something that exists.

Deploying this to production is blocked on the open items in §7. Do not treat this document as a
runbook for a deployment that is ready to happen.

## 2. Authority — who may do what

| Action | Who | Standards |
|---|---|---|
| Approve the code change | Two approvers, ≥1 qualified `ppm-developers` Company Developer, not the author | §7.2 |
| Approve the production deployment | A member of **`ppm-production-approvers`** | §10 |
| Run the deployment | The approved GitHub Actions pipeline | §10 |
| **Conscious Creations** | **Cannot approve either.** We build, test, and prepare | §3.1 |

**The production approver must not be the person who wrote the change** where feasible —
segregation of duties. **Nobody deploys to production from a workstation.** This is explicit in
§10 and is not negotiable for a Critical repository.

## 3. Target deployment model

Critical production solutions must use a GitHub-controlled deployment pipeline unless formally
excepted (§10). The agreed model:

| Environment | Trigger | Approval | Data |
|---|---|---|---|
| **Development** | Developer-triggered | None | Synthetic corpus |
| **Test** | On merge to `main` | Technical owner | Representative test data |
| **Production** | Manual, from a GitHub Actions workflow | `ppm-production-approvers`, via a GitHub environment protection rule | Live |

**Authentication to Azure: prefer OpenID Connect** over long-lived secrets (§10). A separate
least-privilege production identity, distinct from any development identity.

**Deployment logging is required.** Who deployed, what commit, when, and the outcome.

## 4. What must exist before a production deployment

| Requirement | Standards | Status |
|---|---|---|
| Production Azure resource group provisioned | §10 | **Outstanding** |
| Least-privilege deployment identity (OIDC) | §10 | **Outstanding** |
| Secrets in Azure Key Vault, not in the repository or a workflow file | §9 | **Outstanding** |
| GitHub environment with `ppm-production-approvers` protection | §10 | **Outstanding** |
| DNS / subdomain for the review interface | — | **Outstanding** |
| **Authentication on the review interface** | §9 | **Outstanding — blocker** |
| Failure alerting to the named support owner | §10 | **Outstanding — blocker** |
| Backup and restore defined for the state databases | §10 | **Outstanding — blocker** |
| Named production support owner | §5.2 | **Outstanding — blocker** |
| Rollback documented and validated | §10 | [`rollback.md`](rollback.md) — written, validation outstanding |
| Non-interactive Spitfire authentication | — | **Outstanding — blocker** |
| Stage 6 write method decided | — | **Outstanding — blocker** |

## 5. Configuration and secrets

All configuration is read from environment variables. **No credential is ever a literal in
source.** Every required variable is named and explained in [`.env.example`](../.env.example) with
placeholder values only.

For production, values come from **Azure Key Vault**, injected at deploy time. `.env` is a local
development mechanism and has no place in a deployed environment.

**Separate identities per environment** (§9). The development mailbox token, Spitfire account and
Azure AI key must not be the same as production's.

**Before first deployment, rotate every credential** that has existed in development. Credentials
that have been handled on developer workstations do not belong in production.

## 6. Deployment steps — target

Once §4 is satisfied:

1. Change merges to `main` after two approvals and any required Platform Admin review.
2. Test deployment runs automatically. Technical owner verifies.
3. A production deployment is requested from the GitHub Actions workflow.
4. GitHub environment protection pauses for **`ppm-production-approvers`** approval.
5. An approver — not the author — approves.
6. The workflow authenticates via OIDC and deploys with the least-privilege production identity.
7. **Verify before releasing the kill switch:** confirm the schedule state, run one manual batch,
   check the post ledger and Needs Attention queue.
8. Deployment logged automatically.

**If step 7 looks wrong, engage the kill switch** ([`rollback.md`](rollback.md) §1) and stop. Do
not "let it run and see".

## 7. Open items blocking production

| Item | Owner |
|---|---|
| Production Azure resource group, Contributor role, dashboard subdomain DNS | Premier / Ashford IT |
| Whether Premier's tenant migration changes the deployment target — launching on the existing Azure account was agreed as the interim | Ayotunde Gibbs |
| Named production support owner and business owner | Premier |
| Non-interactive Spitfire authentication | Joe Higginbotham |
| Stage 6 write method — API vs direct table | Joe Higginbotham |
| Sandbox environment for exercising the write path | Stan York (Spitfire vendor) |
| Cloud AI data-residency sign-off | Corina Heizer |
| Authentication on the review interface | Conscious Creations |
| Failure alerting; backup and restore definition | Conscious Creations + Premier |

## 8. What must never happen

- No deployment to production from a workstation.
- No deployment approved by the person who wrote the change, where segregation is feasible.
- No production secret in the repository, in a workflow file, or in a pull-request comment.
- No migration, schema change or backfill run automatically as part of a deployment. Migrations
  are written, reviewed, and run by a human against a backed-up database.
- No bypassing branch protection, required reviews, tests, or security scans to get a release out.
