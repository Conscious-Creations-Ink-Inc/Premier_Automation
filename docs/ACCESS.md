# Access inventory

Every credential, host and identifier this project depends on: what it is, which environment
variable carries it, who grants it, how to renew it, and what stops working without it.

**No values live in this file.** They live in `.env`, which is gitignored and is the only file the
code reads. `ACCESS.local.md` (also gitignored) holds them in one place for a person to look up.

Status: **working** = exercised against the live service; **partial** = works with a caveat named
in the row; **not provisioned** = asked for, not yet granted.

---

## 1. Microsoft Graph — the receiver mailbox

| | |
|---|---|
| what | App-only token for `receiver@example-pm.test`; the source of every email the pipeline reads |
| env | `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET`, `GRAPH_MAILBOX_ADDRESS` |
| granted by | Conscious Creations Azure tenant, app registration "Conscious Creations" |
| scope | `Mail.ReadWrite` — verified live 2026-07-28 (list folders, read bodies, list attachments, move) |
| renew | Azure portal → App registrations → Conscious Creations → Certificates & secrets |
| without it | no ingest at all; every page is empty |
| status | **working** |

`Mail.ReadWrite` rather than `Mail.Read` because the pipeline moves each message it processes.
That move is also why `email_id` cannot be the dedupe key — Graph's folder-scoped `id` changes on
move, so `internetMessageId` is carried alongside it (see `pipeline/models.py::RawEmail`).

An exhaustive probe on 2026-07-14 showed **zero** permissions on this app; the grant landed between
then and 28 July. If reads start failing with 403, check the grant before the code.

## 2. Azure AI — OCR for photographed PODs

| | |
|---|---|
| what | Document Intelligence / Vision, for delivery notes that arrive as photographs or scans |
| env | `AZURE_VISION_ENDPOINT`, `AZURE_VISION_KEY`, optionally `AZURE_DOC_INTELLIGENCE_*` |
| granted by | Conscious Creations Azure subscription |
| renew | Azure portal → the AI resource → Keys and Endpoint |
| without it | `PREMIER_OCR_CLIENT=auto` falls back to local Tesseract, then to the mock, where image PODs route to a person with a stated reason rather than silently yielding nothing |
| status | **working** |

Paid per call. OCR runs **once, at ingest**, and the verdict is stored on `attachment_ledger`
(`is_pod`, `pod_po_numbers`, `pod_delivery_date`, `pod_signed_by`). Everything downstream reads
those columns rather than re-reading the file — `record_completion._pod_facts` documents why.

## 3. Spitfire sfPMS — the ERP

| | |
|---|---|
| what | Training instance, sfPMS 2023.0.9692.36214 |
| env | `SPITFIRE_BASE_URL` |
| host | `https://spitfire-host.test/instance` |
| status | **working, office IP only** |

`GET /api/system/version` and `/api/system/branding` answer anonymously from outside Premier's
network, so *reachability* is not the constraint — **authenticated** access is. See §7.

Only `v23` is served; v1 and v18–v25 all return 500. The Swagger has no version dropdown and no
`securitySchemes`, and declares **no business enums at all** — doc-type keys, statuses, UOMs and
date-type names exist only in live responses, which is what §7 exists to capture.

### 3a. Account login — how we authenticate (since 2026-09-15)

| | |
|---|---|
| env | `SPITFIRE_UID`, `SPITFIRE_PW` (+ `SPITFIRE_BASE_URL`) — one pair per environment |
| what | `POST /api/Account` with `SiteLogin {UID, PW, IsHashed:false, tzOffset}`; Spitfire answers with the `sfPMSAuth` ticket cookie, and every later call carries it |
| code | `connectors/spitfire_auth.py` — one ticket shared process-wide; read client logs in and renews on lapse; write client takes the ticket and renews only in `whoami()` before a posting chain, never mid-chain |
| renew | automatic. A changed password: edit `.env` — it is re-read on every login; restart to apply immediately |
| production | same code; only `SPITFIRE_BASE_URL` / `SPITFIRE_UID` / `SPITFIRE_PW` differ (from `.env`, or injected env vars where there is no `.env`) |
| status | **working on Training** — 2026-09-15, login OK, identity `Conscious Creations <api@consciouscreations.ai>` |

When UID/PW are set they win over any `SPITFIRE_SESSION_COOKIE`. The login is never recorded to or
replayed from a cassette (its body carries the password, and a replay would issue no ticket).

### 3b. Session cookie — development fallback

| | |
|---|---|
| env | `SPITFIRE_SESSION_COOKIE` — used only when `SPITFIRE_UID`/`SPITFIRE_PW` are blank |
| what | The value of the browser's `sfPMSAuth` cookie. Not `sfSession`, which is only a session id |
| lifetime | **Short.** Lapses on idle and cannot be renewed by the code |
| status | **dev only** — reads and writes are attributed to whoever's browser it came from |

`IsHashed` must stay false for a plaintext password. `SPITFIRE_TZ_OFFSET` is sent on login and
Spitfire stamps server-side dates against it — a wrong value shifts received dates by hours, which
matters because Stage 5 compares them to POD dates. `-5` is US Eastern standard, `-4` during DST.

**Ask for Read only.** Measured 2026-08-13: `POST /api/account/allows` with `{}` returns **31** =
Read + Insert + Update + Delete + Blanket. Read-only is currently enforced client-side by
`connectors/spitfire.py::_ALLOWED`, not by Spitfire.

### 3c. Identifiers

| env | what | source |
|---|---|---|
| `SPITFIRE_PROJECT_IDS` | `PRJ001PB100003`, `PRJ001PB100002`, `PRJ002PB100002` — where the corpus POs live | read from `TrainingsfDocSys.dbo.xsfDocHeader`; needed because this account cannot enumerate projects (`POST /api/projects` returns zero rows, `GET` is 405) |
| `SPITFIRE_PO_DOC_TYPE_KEY` | PO document type, `ff1975fd-…` | `dev_reports/Stored_Procedures/czx_TPICreate_ReceiptDoc.sql` (`@PODTK`) |
| `SPITFIRE_RECEIPT_DOC_TYPE_KEY` | Receipt document type, `0c9a537a-…` | the same `.sql` (`@RDTK`), confirmed 10 Aug against 76,414 documents whose `DocTypeKey_dv` reads "Receipt" |

Take both GUIDs from the `.sql`, **never** from `PREMIER_AUTOMATION_MASTER_PLAN.md` §3.5, which
transcribes `@PODTK` one character short. They cannot be confirmed from the API: every
`/api/configuration/*` endpoint returns `500 … not yet implemented (case 36629)`, read and write
alike, and faults before the auth check.

### 3d. Pre-shared key (PSK) — a third scheme, discovered 2026-08-22

| | |
|---|---|
| env | `SPITFIRE_API_CLIENT_ID`, `SPITFIRE_API_CLIENT_KEY` |
| what | Two request **headers**, `APIClientID` and `APIClientKey`. `APIClientID` is an sfPMS *user key GUID*; `APIClientKey` is that user's Spitfire federated key (`UserFederatedKey` for provider `Spitfire`) |
| requires | `UserPSKAuthOK` enabled on that user |
| granted by | Premier / Spitfire — Ref **Case 36867** |
| renew | The key can be rotated without changing the id |
| status | **supplied but not working on Training — see below** |

This is not the session cookie (§3a) and not the `POST /api/Account` service login (§3b). It
arrived with the vendor's `sfPMS-PO-Receipt` Postman collection, which authenticates every one
of its 15 requests this way. **It matters more than the other two: a PSK does not lapse on
idle**, so it is the thing that would close the gap §3a calls "the single largest gap between
the mechanics work and the automation runs".

The pair issued for `REST Automation` returns `401 {"ThisReason":"Invalid"}` on every
authenticated endpoint of the training host, and `GET /api/account/session` returns `false`
under it. Tested as-supplied, lowercased, dash-stripped, brace-wrapped and with id and key
swapped — all identical, so it is not a formatting problem. Note the reason differs from the
no-credential case (`"Not authenticated"`), which proves the scheme is switched on and is
actively rejecting these values rather than ignoring them.

**RESOLVED 2026-08-22 -- the user exists on Training but has no federated identity linked.**
Found with GET endpoints only, no admin console needed. `GET /api/contact/{userKey}` accepts the
`apiClientId` directly, because that value *is* a user key:

```http
GET /api/contact/3DA6B772-2AF4-49FE-8D72-5DA8C8939EA3
-> 200
{"UserKey":"3da6b772-2af4-49fe-8d72-5da8c8939ea3",
 "UserName":"Receiving Automation",
 "UserLogin":"ReceivingAutomation@example.com",
 "FederatedIdentityInfo":"No linked accounts"}
```

`FederatedIdentityInfo` is a human-readable summary of that contact's linked identities, capped
at 50 chars. Sampled across the routees of PO 907030 it reads `"ID;  last used Feb 02 "` or
`"Profile Picture and  2 linked identities;  last used Aug 27, 2025 "` for people who actually
log in, and `"No linked accounts"` for the `Spitfire` system account and for vendor contacts who
never do. `Receiving Automation` reads **"No linked accounts"**.

So of the three candidates, it is the third: the account is on the right host, but **no
`UserFederatedKey` for provider `Spitfire` has ever been issued for it**, which is why the
supplied `apiClientKey` matches nothing and every call 401s. `UserPSKAuthOK` is moot until a key
exists. Note the login is `ReceivingAutomation@example.com` -- an `example.com` address, so the
account looks provisioned from a template and never finished.

**The key itself can never be read back.** `PSK`, `APIClient` and `ClientKey` appear **zero**
times across all 300 paths and every schema of the v23 OpenAPI document, and the account record
from `GET /api/Account` carries no key field. `FederatedIdentityInfo` reports only *that* an
identity exists and when it was last used. The scheme is header-level and evaluated before
routing -- which is also why a write-shaped POST returns the same `401 Invalid` as a GET.

**Open ask (Spitfire Case 36867):** issue a `UserFederatedKey` for provider `Spitfire` against
user `3da6b772-2af4-49fe-8d72-5da8c8939ea3` (`Receiving Automation`) on the Training site, enable
`UserPSKAuthOK` on it, and send the key. Confirm too whether that account is meant to be the
identity automation writes are attributed to -- it currently has an `example.com` login.

Evidence: `dev_reports/Postman_Full_Sweep_2026-08-22/REPORT.md` calls 1-6, and `PSK_DIAGNOSIS.md`
in the same directory.

## 4. Azure DevOps

| | |
|---|---|
| what | Premier's repo / boards |
| granted by | Premier |
| status | **validated 2026-07-28, reported — not independently re-tested in-session** |

## 5. Spitfire VM

| | |
|---|---|
| what | Gateway to the Spitfire environment |
| granted by | Premier |
| status | **validated 2026-07-28, reported — not independently re-tested in-session** |

---

## 6. What is *not* an access problem

Recorded here because each was mistaken for one:

- **Reports are not in the REST API.** They are SSRS behind `sfReportViewer.aspx`. Every export
  endpoint failing was the wrong question, not a permission gap. `GET /api/session/reports/32`
  lists 40 named reports including "Receipt Log" (arg 65). Pulling one is a WebForms postback and
  needs `ASP.NET_SessionId` **as well as** `sfPMSAuth` — with the auth ticket alone the fetch
  returns `200` and zero bytes, which looks exactly like a permission failure and is not.
- **`500` does not mean `404`.** An invented document GUID returns 500 with a body identical to a
  genuine server fault. Never read it as "not found".
- **The `/api/config*` 500s are declared stubs**, not faults:
  `{"ThisStatus":500,"ThisReason":"…not yet implemented (case 36629)"}`. 18 endpoints share that
  case, and no credential changes it.

---

## 7. The office-IP constraint, and the way around it

Authenticated Spitfire access works only from Premier's office network, and §3a lapses on idle. So
`connectors/spitfire_cassette.py` records responses while connected and replays them when not. It
mounts as a `requests` transport adapter **underneath** both clients, so the allowlist, the retry
rules, the audit log and the 401 handling all run unchanged around a response that came from disk.

| `SPITFIRE_CASSETTE_MODE` | behaviour |
|---|---|
| `off` *(default)* | nothing is mounted; the code behaves exactly as it does today |
| `record` | call live, save every response, return the live one |
| `replay` | never open a socket. A call nothing recorded raises, naming it. **Writes refuse** |
| `auto` | replay on a hit, else live-and-record — warms the store as you work at the office |

**While you are on the office IP:**

```
python -m tools.spitfire_record_cassettes            # all 28 POs, ~115 calls
python -m tools.spitfire_record_cassettes --verify   # later: has anything drifted?
python -m tools.spitfire_record_cassettes --promote  # scrubbed subset -> tests/fixtures/spitfire/
```

**Anywhere else:** set `SPITFIRE_CASSETTE_MODE=replay`. Verify, the PO mirror and `verify_pod` all
work. Posting does not, and the Post button is not drawn — a control whose only possible outcome is
a refusal teaches people to ignore refusals.

Writes are never replayed, by design: `create_receipt` would return the same DocMasterKey every
time, so two deliveries would become one receipt and the ledger would record a key that looks real.

Store: `state/spitfire_cassettes/`, gitignored — the bodies carry real vendor names, named Premier
employees staged on approval routes, and cost codes.

**Known limitation.** `RequestRecord` / `WriteRecord` carry no `replayed` flag yet, so
`post_ledger.audit_json` cannot distinguish a replayed call from a live one. Because writes refuse
offline, no posted receipt can be built from replayed data — and a replayed response does carry an
`X-Spitfire-Cassette: replay` header if you are reading one by hand.

---

## 8. Open asks with Premier

| # | ask | blocks |
|---|---|---|
| 1 | ~~Provision an automation account~~ done (§3a). Still open: scope it below AdminLevel 31, and confirm the password does not expire | least privilege |
| 2 | Who approves an automated receiver? Creating one auto-stages three real employees at sequence 10 | dispatching a receipt |
| 3 | Does production have the same office-IP restriction as training? | deployment |
| 4 | Does `cost/committed` populate in production? `GET /api/xts/state` says *"ERP peer not configured"*, and `po_date` reads `1900-01-01` on all 1,726 training rows | committed-cost reporting |

On #2: `route/apply` would email Cassie Breaux, Kendyl Shrogin and Tina Tran. It is refused by name
in `connectors/spitfire_write.py::_DENIED_SUBSTRINGS`. Nothing has ever been dispatched.
