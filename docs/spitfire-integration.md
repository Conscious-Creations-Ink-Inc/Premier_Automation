# Spitfire sfPMS integration

Endpoints, authentication, constants, and the controls that keep this integration inside its
approved surface.

---

## 1. Posture

**Reads are the production path today. Writes are gated and Stage 6 is blocked.**

| | |
|---|---|
| Read | Purchase-order headers, items, addresses, approval route |
| Write | A narrow allowlisted surface — receipts and their attachments only |
| SQL Server | **`SELECT` only.** No `INSERT`, `UPDATE`, `DELETE` or DDL in any code path, tests included |

## 2. Authentication

Three mechanisms, in order of preference:

**1. Service account (`UID`/`PW`) — the intended production path.** Not yet provisioned. When it
is, the account needs **read on purchase orders and their lines and nothing else** — not the
ability to create documents, attach files, or post comments.

**2. Session cookie (`sfPMSAuth`) — the interim path in use today.** The `sfPMSAuth` cookie alone
is the FormsAuthentication ticket and is sufficient — proven across live PO reads with nothing
else set. `ASP.NET_SessionId`, `sfSession` and `sfSettings` do **not** authenticate; supplying
`sfSession` instead produces a client that fails every call.

It is captured by hand from a browser session and **lapses on idle**. The connector chooses cookie
mode whenever `SPITFIRE_SESSION_COOKIE` is set, and deliberately will **not** silently fall back
to a password login when the ticket dies — a silent fallback would hide the fact that unattended
operation is not actually possible yet.

**3. Pre-shared key (`apiClientId` / `apiClientKey`) — for the write collection.** Not yet issued.
Pending the Spitfire vendor.

> **Open item — the blocker for unattended operation.** It is unconfirmed whether Spitfire exposes
> a programmatic login endpoint at all. Until it does, nothing here can run unattended: the ticket
> expires and a person must recapture it. Owner: Joe Higginbotham.

**Network restriction:** Spitfire answers only from an allowed network. `GET /api/system/version`
and `/api/system/branding` respond anonymously from outside, so *reachability* is not the
constraint — **authenticated** access is. Only one API version is served; others return 500.

## 3. Read endpoints

| Purpose | Call |
|---|---|
| Liveness | `GET /api/account/session` — answers false rather than 401 |
| Whose session | `GET /api/session/who` |
| Find a PO | `POST /api/project/{projectID}/docs` with `{DocNoLike, ForDocType}` → `DocMasterKey` |
| Fallback search | `POST /api/catalog/search/{scope}/contents` — rarely hits |
| PO header | `GET /api/document/{key}` — `DocNo`, `Status_dv`, `DocDate` |
| **PO items** | `GET /api/document/{key}/items` — **the quantities** |
| Addresses | `GET /api/document/{key}/addresses` — vendor (`AddrType T`), ship-to (`S`) |
| Route | `GET /api/document/{key}/route` — purchasing agent |

`GET /api/document/{key}/dates` is deliberately **never called** — it carries no order date.

### Quantities — get this right

```
open quantity = RelatedLineDetails.ContractUnits − RelatedLineDetails.ReceivedUnits
```

- Quantities come from each item's `RelatedLineDetails`: `ContractUnits` (ordered),
  `ReceivedUnits`, `ReceiptInProgressUnits` (in transit).
- **Not from `ItemQuantity`**, which reads `0.0` on lines that genuinely order stock.
- **Subtract prior receipts.** Reading `ContractUnits` alone over-receives any line that already
  carries a partial receipt.
- Tax and freight lines (`AccountCategory` `TAX-` / `FRT-`) are dropped, not received against.

### Line matching

The match key is **`SourceItemNumber`** — the spec code. It decides which PO line a delivered item
belongs to. Exact match first; where a vendor omits or mangles the spec code, a fuzzy fallback
runs against the line description with a high threshold, and anything below threshold routes to
Needs Attention rather than being guessed.

## 4. Domain constants — never alter, never fetch at runtime

```
Receipt DocTypeKey  = 0C9A537A-3C41-4D16-AB9F-130EF69EA6C8
ForDocType GUID     = ff1975fd-76de-486c-888b-54e8fcd880e0
```

`ForDocType` is a hardcoded Spitfire **stock constant, identical on every installation**. It is
never fetched at runtime and must not be "improved" into a lookup.

The PO document-type key narrows document search; the receipt key identifies existing receipts, so
an already-received PO can be distinguished from a new delivery.

### Attachment chain

```
xsfFileAttach → xsfFile → xsfFileVersionInfo → xsfFileVersion (blob)
```

### "Needs Attention" flow

```
DocHeader.Status = N
DocHeader.Type   = N
exception note  → DocRevision.csString050
originating email attached to the flagged receipt
```

## 5. Project discovery — a real boundary

`SPITFIRE_PROJECT_IDS` is a fixed list, and that is a limitation, not a design choice. The service
account **cannot enumerate projects**: `POST /api/projects` answers 200 with zero rows, and `GET`
returns 405.

**Consequence:** a PO in a project outside that list will not resolve. The interface says so
plainly — "not found in the projects this connector searches" — rather than claiming the PO does
not exist. Project IDs are added to configuration as Premier opens projects up.

> **Open item.** Premier either widens the service account so projects can be enumerated, or
> accepts a maintained list. Owner: Joe Higginbotham.

## 6. The write surface — a security control

`spitfire/write_client.py` is deliberately a separate module from the read connector.

**How the guard works, and why the order matters:**

1. A **deny check runs first**, on the raw lowercased path, independent of the allowlist.
2. Only then is the request matched against `_ALLOWED_WRITES` and `_ALLOWED_READBACKS`.

Because the deny check runs *before* the allowlist and matches the raw path, a future mistake in
the allowlist **cannot** accidentally authorise a denied route. Requests are rejected before a
socket opens.

> **This is a security control, not a convenience.** Do not widen `_ALLOWED_WRITES`, do not
> reorder the deny check, and never add a path to make a test pass. Changes require Platform Admin
> review under §8 (external integration). Covered by dedicated tests — keep them.

### Duplicate posting

Spitfire provides **no idempotency**, so the local post ledger is the only duplicate guard.
Measured against the training instance:

- The catalog does not deduplicate — the same file uploaded twice produced two file keys and two
  catalog entries.
- `POST /api/document/{id}/attachments` has no idempotency — the identical body twice gave two
  rows, success both times.
- `ReceiptInProgressUnits` — the field whose schema calls it *"units tentatively received not yet
  approved"* — reads **`0.0`** on a PO that already carries an unapproved receipt.

Quantities roll up only on approval, and receipts created here are never auto-routed, so the
window in which Spitfire looks untouched lasts as long as a human takes. **A guard reading
Spitfire's own numbers would post a second receipt every time.**

The claim is therefore written to the ledger **before** the first call, not after the last. A
crash mid-chain leaves a recoverable claimed row; the opposite order would lose the record of a
receipt that exists.

## 7. Stage 6 — the open architecture decision

**Blocked pending Joe Higginbotham.** Two options:

**A — REST API (recommended).** Creates receipts through Spitfire's own endpoints.

**B — Direct table insert.** Writes to the underlying tables.

> **The concrete argument for the API route:** direct table inserts **bypass Spitfire's workflow
> triggers** — `xsfDocSession`, `xsfDocWorkflow`, `xsfDocWriteTx`. A receipt created that way is
> not a receipt Spitfire's own workflow has seen. On a SOX-controlled system of record, that is a
> control gap, not an implementation detail.

Also outstanding: the sandbox base URL and the PSK write-collection credentials, pending Stan York
at the Spitfire vendor. Without a sandbox there is nowhere safe to exercise the write path.

## 8. Working away from the allowed network

A cassette layer records Spitfire responses while connected and replays them when not.

| Mode | Behaviour |
|---|---|
| `off` | Nothing mounted; behaves exactly as in production. **The default.** |
| `record` | Call live and save every response |
| `replay` | Never opens a socket. Reads, the PO mirror and read-backs work; **posting is refused**, because a replayed create would return one document key for every delivery |
| `auto` | Replay on a hit, otherwise live-and-record |

**Recorded cassettes contain real vendor names, named staff on approval routes, and cost codes.**
The cassette directory is gitignored and must stay that way. A scrubbed subset is what belongs in
test fixtures.

## 9. Open items

| Item | Owner |
|---|---|
| Stage 6 write method — API vs direct table | Joe Higginbotham |
| Sandbox base URL and PSK write credentials | Stan York (Spitfire vendor) |
| Non-interactive authentication path | Joe Higginbotham |
| Service account provisioning (`UID`/`PW`, read-only) | Premier |
| Project enumeration, or an agreed maintained list | Joe Higginbotham |
