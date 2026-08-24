# Premier Receiver Automation — the complete flow, endpoint by endpoint

Every HTTP call between a delivery email arriving and a receipt existing in sfPMS.
Responses below were returned by `https://training.remingtonhotels.com/Training` on 2026-08-18 — none is quoted from a specification.

Regenerate: `python tools/verify_prd_endpoints.py && python tools/build_prd.py`

---

## Read this first

**Spitfire cannot tell you what you have already posted.** `ReceiptInProgressUnits` — the one field that looks like it answers *"has this been received?"* — reads `0.0` for the entire time a receipt sits unapproved. Nothing in this API is idempotent and the catalog does not deduplicate identical bytes, so **every guard against double-posting is ours**, in a local ledger.

Measured on PO 212559, whose lines 0001/0002/0004 carry receipts posted the same day:

| Line | ContractUnits | UOM | ItemQuantity | ReceivedUnits | ReceiptInProgressUnits |
|---|---|---|---|---|---|
| 0001 | 4.0 | Set | `0.0` | 0.0 | **0.0** |
| 0002 | 4.0 | Set | `0.0` | 0.0 | **0.0** |
| 0003 | 0.0 | — | `0.0` | 0.0 | **0.0** |
| 0004 | 2.0 | Set | `0.0` | 0.0 | **0.0** |
| 0005 | 2.0 | Set | `0.0` | 0.0 | **0.0** |
| Freight | 0.0 | — | `0.0` | 0.0 | **0.0** |

Two more traps in that table: `ItemQuantity` is **always 0.0** — the ordered quantity is `RelatedLineDetails.ContractUnits` — and the spec code is in `SourceItemNumber`, because `Specification` is null on real purchase orders.

---

## 1. A mail arrives — Microsoft Graph

Auth: MSAL client-credentials bearer token, `Mail.ReadWrite` application permission, restricted to one mailbox by an Application Access Policy.

```http
GET https://graph.microsoft.com/v1.0/users/{mailbox}/mailFolders/Inbox/messages
{
  "$top": 50,
  "$orderby": "receivedDateTime asc",
  "$select": "id,internetMessageId,receivedDateTime,subject,from,body,hasAttachments",
  "$filter": "receivedDateTime ge {watermark}"
}
```

`ge` not `gt` — the watermark is backdated and the seen-set prevents reprocessing. Listing a message twice costs a skipped row; missing one loses it for good.

```http
GET /users/{mailbox}/messages/{id}/attachments
GET /users/{mailbox}/messages/{id}/attachments/{attachmentId}/$value    -> raw bytes
POST /users/{mailbox}/messages/{id}/move        {"destinationId": "<folderId>"}
```

**Two traps that cost us records:**

- `hasAttachments` is **false** when a message's only images are inline — a pasted-in photo of a delivery note is invisible if you trust it.
- An `itemAttachment` (a forwarded email — the commonest shape in Premier's mail) returns **MIME bytes, not a `.msg` file**. Sniffing it as `.msg` emptied every POD field in 13 of 13 records.

## 2. Parse — the only paid call

No Spitfire traffic. Body, attachments and any scanned POD become one extracted record: PO number, spec, quantity, UOM, delivery date, receiver.

```http
POST {endpoint}/documentintelligence/documentModels/prebuilt-layout:analyze?api-version=2024-11-30&pages=1-10
Ocp-Apim-Subscription-Key: <key>
Content-Type: application/octet-stream
<raw bytes>

-> 202 Accepted   + an `operation-location` response header
GET {operation-location}   -> poll until status leaves "running"
```

`pages=1-10` is sent for PDFs only (images reject it). Billing is per page; the polling GETs are free.

## 3. PO number → project → DocMasterKey

**There is no single endpoint that does this.** Emails give a PO number; every useful Spitfire endpoint is keyed by GUID. Three hops:

### 3a. Which projects can we search?

```http
POST /api/projects
{
  "IncludeHidden": true,
  "IncludeClosed": true
}

-> HTTP 200, 0 row(s), 2 bytes, 407 ms
[]
```
Zero rows — this account has no project list. So the ids are configuration: `MRC024PB100003, MRC024PB100002, MRC026PB100002`. The live call is still tried first, so this corrects itself the moment Premier grants project membership.

```http
GET /api/projects

-> HTTP 405, 72 bytes, 250 ms
{"Message":"The requested resource does not support http method 'GET'."}
```
The other verb is not an alternative.

### 3b. Search each project for the PO number

```http
POST /api/project/MRC024PB100003/docs
{
  "DocNoLike": "212559",
  "IncludeDocs": true,
  "IncludeFiles": false,
  "IncludeClosed": true,
  "ResultLimit": 25,
  "ForDocType": "ff1975fd-76de-486c-888b-54e8fcd880e0"
}

-> HTTP 200, 1 row(s), 2012 bytes, 577 ms
[{"DocMasterKey":"1023ab34-f62c-4a2f-9d73-4c73446d3473","DocTypeKey":"ff1975fd-76de-486c-888b-54e8fcd880e0","DocReference":"00000000-0000-0000-0000-000000000000","DocDate":"2025-12-29T00:00:00","DocNo":"212559","DocBatchNo":"?","SourceDocNo":"","Title":"PO 212559 Accessories","Priority":5,"Confidential":false,"FromUser":"99cea406-bcbf-4542-8ca6-046ae7c4b1f8","SortFrom":"Pabon","ResponsibleParty_dv":"Alejandra Pabon","ResponsibleParty":"99cea406-bcbf-4542-8ca6-046ae7c4b1f8","Company":"6f93577f-1525-496d-8dda-84930fedd475","Company_dv":"Pigeon & Poodle","ToUser":"Pigeon & Poodle","SortTo":"Pigeon & Poodle","Author":"Alejandra Pabon","Due":null,"Signoff":"2026-01-26T16:56:52.78","Closed":null,"
… truncated
```
The other two return `[]` for the same body — the PO's project is found by elimination:

| Project | Rows |
|---|---|
| `MRC024PB100003` | **1 — found** |
| `MRC024PB100002` | 0 |
| `MRC026PB100002` | 0 |

`DocNoLike` is a *contains* match, so `2124` would also return 212456 and 212457. Every candidate is re-checked for an exact hit on `DocNo` or `SubContract` before its key is accepted.

### 3c. The project *code* comes from the PO header, not the search

```http
GET /api/document/1023ab34-f62c-4a2f-9d73-4c73446d3473
  -> header.Project     = the project code   (needed as forProject when creating)
  -> header.SubContract = the PO number      (the join to every related document)
```

All of it is cached in `spitfire_po_index`, so a known PO costs no search. **If the mirror has never seen the PO, posting is refused** — there is no `forProject` without it.

> There is a documented site-wide fallback that has **never once executed**. It is called with `DocNoLike` and the server demands `TitleLike`, so it 400s and the exception is swallowed.

```http
POST /api/catalog/search/0/contents
{
  "DocNoLike": "212559",
  "IncludeDocs": true,
  "IncludeFiles": false,
  "IncludeClosed": true,
  "ResultLimit": 25,
  "ForDocType": "ff1975fd-76de-486c-888b-54e8fcd880e0"
}

-> HTTP 400, 75 bytes, 280 ms
{"ThisStatus":400,"ThisReason":"catalogFilters.TitleLike requires a value"}
```
## 4. Read the purchase order

```http
GET /api/document/1023ab34-f62c-4a2f-9d73-4c73446d3473

-> HTTP 200, 3955 bytes, 422 ms
{"DocMasterKey":"1023ab34-f62c-4a2f-9d73-4c73446d3473","DocTypeKey":"ff1975fd-76de-486c-888b-54e8fcd880e0","DocTypeKey_dv":"PO/Contracts","DocReference":"00000000-0000-0000-0000-000000000000","DocDate":"2025-12-29T00:00:00","DocNo":"212559","SourceDocNo":null,"ExternalDocNo":null,"DocBatchNo":null,"Title":"PO 212559 Accessories","Source":"32","Priority":5,"NumToSend":0,"NumToForward":0,"Probability":0,"AutoTitled":false,"Confidential":false,"DocEdit":true,"Final":false,"DocFlag":false,"Due":"0001-01-01T00:00:00","Closed":"0001-01-01T00:00:00","Signoff":"2026-01-26T16:56:52.78","SourceDate":"2026-02-05T00:00:00","LinkedDocKey":"00000000-0000-0000-0000-000000000000","UniReferenceKey":"00000000
… truncated
```
We keep `DocNo`, `SubContract`, `Project`, `Status`, `DocDate`. **`DocDate` is the order date** — established by testing all three candidate dates against PO numbering across 27 consecutive pairs: `DocDate` got 0 out of order, `SourceDate` inverted on 11. `/api/document/{id}/dates` sounds right and is not: schedule rows keyed by an unnamed GUID, no order date. Never called.

```http
GET /api/document/1023ab34-f62c-4a2f-9d73-4c73446d3473/items

-> HTTP 200, 6 row(s), 49420 bytes, 1812 ms
[{"DocItemKey":"6b3e5233-436e-42bd-9030-1f85f12f27d5","TaskCount":1,"CommentCount":0,"LinkCount":0,"ItemRevisionMap":{"DocRevItemKey":"6b3e5233-436e-42bd-9030-1f85f12f27d5","DocItemKey":"6b3e5233-436e-42bd-9030-1f85f12f27d5","ContainerKey":"00000000-0000-0000-0000-000000000000","FromUser":"99cea406-bcbf-4542-8ca6-046ae7c4b1f8","ItemNumber":"0001","ItemSeq":20,"Created":"2026-01-05T08:52:50.31","ETag":"da40b18d1d6cff19aa16619c0de258f7"},"DocItemTask":[{"ItemTaskKey":"a124b3ba-524d-4604-a2fe-1ccfd882e33f","LinkedLineKey":"00000000-0000-0000-0000-000000000000","LinkedRFQKey":"00000000-0000-0000-0000-000000000000","LinkedCCCKey":"00000000-0000-0000-0000-000000000000","ProjEntity":"102011215","Pr
… truncated
```
Quantities are nested in `RelatedLineDetails`; UOM and cost code in `DocItemTask[0]`. Two fields are easily confused and are **not** the same thing:

| Field | Line 0001 | What it is |
|---|---|---|
| `DocItemTask[0].ProjEntity` | `102011215` | the cost code — what the receipt line posts against |
| `RelatedLineDetails.GLAcct` / `GLSub` | `900001.FDP` / `MRC` | the GL account. Not used by us. |
| `DocItemTask[0].AccountCategory` | `MAT-FDP` | what makes a line receivable — `TAX-` and `FRT-` are skipped |

On corpus PO 208491, **10 of 25 lines are tax and freight**, and nothing in their shape distinguishes them from an under-populated goods line. The account category is the only reliable discriminator.

```http
GET /api/document/1023ab34-f62c-4a2f-9d73-4c73446d3473/addresses

-> HTTP 200, 2 row(s), 5389 bytes, 281 ms
[{"DocAddrKey":"e49a5b2e-8868-4b56-ab13-d21a84e1fa18","AddrType":"F","SourceType":"U","UseSource":false,"UserKey":"99cea406-bcbf-4542-8ca6-046ae7c4b1f8","Person":"Alejandra Pabon","Company":"","Addr1":"14185 Dallas Parkway","Addr2":"Suite 1150","City":"Dallas","State":"TX","Zip":"75254","Phone":null,"Fax":"","Email":"alejandrapabon@premierpm.com","ContactProject":"MRC024PB100003","RoleName":"","Title":"","ETag":"9f8889e21e8b15d00b2bbeda15a8c464","MenuCommands":[{"MenuID":"","CommandName":"OpenMap","CommandArgument":null,"Enabled":true,"HasPermits":0,"IconImageUrl":"fa-regular fa-map","ItemText":"Show Map","InfoText":null,"DefaultValue":null,"HRef":null,"HrefTarget":null,"UCModule":"DOC","UCF
… truncated
```
`AddrType`: `T` vendor, `S` ship-to, `F` author, `R` remit-to.

```http
GET /api/document/1023ab34-f62c-4a2f-9d73-4c73446d3473/route

-> HTTP 200, 5 row(s), 37332 bytes, 1327 ms
[{"RouteID":"bbb2effc-c463-4a0e-9f3f-05b1c85b6a7a","UserKey":"99cea406-bcbf-4542-8ca6-046ae7c4b1f8","UserKey_Inactive":false,"Stage":1,"Sequence":1,"GroupNo":0,"FromUser":"99cea406-bcbf-4542-8ca6-046ae7c4b1f8","RecipientRole":"Junior Procurement Agent (Purchasing Agent)","Status":"P","StatusChoices":[{"key":null,"label":" Responded","value":"A"},{"key":null,"label":"Held","value":"H"},{"key":null,"label":"Pending","value":"P"}],"RouteVia":"W","RouteViaChoices":[{"key":null,"label":"E-Mail","value":"E"},{"key":null,"label":"E-Sign","value":"G"},{"key":null,"label":"Web","value":"W"},{"key":null,"label":"Hard Copy","value":"H"}],"UserDocEdit":true,"SendAlerts":true,"ReplyTo":true,"EmailFrom":n
… truncated
```
The purchasing agent is the first routee's `UserName` — `ResponsibleParty_dv` reads empty on real POs.

## 5. Mirror it locally

A live PO read is four round trips and about **8.6 seconds** even with a cached key — verifying 29 records live is ~145 seconds, which is not a page render. So every read is written to:

- `spitfire_po_index` — one row per PO: `doc_master_key`, `project_code`, vendor, ship-to, order date
- `spitfire_po_lines` — one row per line, keyed on Spitfire's own `DocItemKey` (sub-parts really are separate lines)

## 6. Verify — states figures, gives no verdict

Re-reads the PO and puts its numbers beside the email's. Deliberately makes no ruling; a reviewer can also pick a different PO line here, and that choice is persisted to `po_line_number` and honoured by the post.

## 7. Decide — nine gates, each refusing by default

| # | Gate | The refusal it produces |
|---|---|---|
| 1 | record complete | "the record is incomplete — missing: received-by" |
| 2 | POD bytes in hand | "the delivery email carried no attachments…" |
| 3 | not already posted | "this delivery was already posted as receipt 0004" |
| 4 | PO in one of our projects | "not found in the projects this connector can search" |
| 5 | line resolved by spec, **or chosen by a person** | "matched on description alone" |
| 6 | something still outstanding | "shows nothing outstanding on this line (19 of 19)" |
| 7 | quantity ≤ outstanding | "this would over-receive: the email says 40 Set and only 2…" |
| 8 | units agree | "the email says CS and the purchase order says EA" |
| 9 | project known locally | "…is not known locally — verify the PO once" |

A **partial delivery is not a discrepancy**: the comparison is against what is still outstanding, so 2 arriving against 19 ordered posts and leaves 17. Only exceeding the outstanding quantity needs a person.

## 8. Post the proof of delivery — 5 calls

### 8.1 Create the receipt — **no body at all**

```http
POST /api/document/00000000-0000-0000-0000-000000000000/{receiptTypeKey}?forProject={projectCode}&forBatch={poNumber}

-> 200, a BARE QUOTED GUID (not an object):
   "2189d7d7-da0b-47f0-919b-0c3a2b07d6ce"
```

The parent is the null GUID; the type key is the **second path segment**. `forBatch` is the PO number and is what populates `SubContract` — **the only field tying a receipt to its purchase order**. If it does not come back set, the receipt is an orphan and nothing further is attached.

> **Creating the receipt stages people.** Spitfire applies the configured approval chain the instant the document exists: six routees, three of them real Premier employees at sequence 10. Nothing is emailed — that needs `route/apply`, refused by name — but they are on it.

### 8.2 Title it — the body is a **bare JSON string**

```http
PATCH /api/document/{receiptKey}/Title
"CC-TEST - receiver automation - PO 212559 - 2026-08-17 - DO NOT PROCESS"
```

### 8.3 Add the receipt line — the body is an **array**

```http
POST /api/document/{receiptKey}/items
[
  {
    "Description": "CC-TEST Amenity Tray at Ballroom Restrooms",
    "ItemQuantity": 4.0,
    "DocItemTask": [
      {
        "Quantity": 4.0,
        "UOM": "Set",
        "ProjEntity": "102011215"
      }
    ],
    "SourceItemNumber": "BRR-803-AC"
  }
]
```

`PUT /items` is an undiscoverable 500. `UOM` and `ProjEntity` are copied from the matched PO line — a wrong `ProjEntity` books the receipt to the wrong budget. Lines read back as `{DocNo}-{seq}`, never the integer sent.

### 8.4 Upload the POD — multipart, two parts, **no Content-Type header**

```http
POST /api/catalog/upload?xm=catalog
  part 1: fileMeta   (application/json)
  part 2: file       (application/octet-stream)

{
  "value": "CC-TEST.POD.FedEx.pdf",
  "Name": "CC-TEST.POD.FedEx.pdf",
  "type": "file",
  "FileType": "pdf",
  "size": 20535,
  "MD5": "8342F644E7DC630772686415F287020B",
  "date": "2026-08-17T00:00:00",
  "DocDate": "2026-08-17T00:00:00",
  "ReferenceDate": "2026-08-17T00:00:00",
  "Due": "2026-08-17T00:00:00",
  "Keywords": "CC-TEST POD 212559"
}

-> 200 {"name":"CC-TEST.POD.FedEx.pdf","key":"8a0af8b5-3fb3-44e5-af75-b4ebd490fcca","size":64,"progress":"100.0%","error":null}
```

The failure ladder, each rung walked into in turn:

| What is wrong | What the server says |
|---|---|
| no `file` part, or a hand-set `Content-Type` | **415** "File expected" — setting the header by hand loses the multipart boundary |
| no `fileMeta` part | **400** "Meta data missing" |
| `filemeta` in lower case | **400** — part names are case-sensitive |
| any of the four dates missing | **500** "Nullable object must have a value" |

`MD5` must be upper case. Spitfire takes the stored filename from the `file` part and **ignores `fileMeta.Name`**.

### 8.5 Verify the bytes, then attach

```http
GET /api/catalog/{fileKey}/versions
  -> [{ …, "DataHash": "8342F644E7DC630772686415F287020B" }]
     the server's own MD5 — the only real proof the bytes arrived intact.
     /meta and /object return 500 on the very same key /versions accepts.

POST /api/document/{receiptKey}/attachments
[
  {
    "DocKey": "<fileKey>",
    "AttachedDocMaster": "00000000-0000-0000-0000-000000000000",
    "Note": "CC-TEST proof of delivery",
    "CatType": "<receipt doc type>",
    "MailRoute": "P",
    "AccessLevel": "V"
  }
]

-> 200 {"DocAttachKey":"0bc8af59-0bd2-4f90-80cb-ada94c10fffe","DocKey":"8a0af8b5-…"}
```

## 9. Post the receiver report — 4 calls

A separate decision a person makes, so a separate button and a separate ledger state. The report is generated per delivery, rendered to PDF through headless Chrome, then uploaded and attached exactly as the POD was (8.4 + 8.5). Then two document links.

**One endpoint, two body shapes.** A *file* link populates `DocKey`; a *document* link populates `AttachedDocMaster` and leaves `DocKey` as the null GUID. Premier's own receipt 209330 carries both shapes in one collection.

```http
POST /api/document/{receiptKey}/attachments
[
  {
    "DocKey": "00000000-0000-0000-0000-000000000000",
    "AttachedDocMaster": "<the PO's DocMasterKey>",
    "Note": "CC-TEST purchase order",
    "CatType": "<PO doc type>",
    "MailRoute": "P",
    "AccessLevel": "V"
  }
]
```

Not `PUT /api/document/{id}/link`, which *creates* child documents from type keys and returns 500 when handed an existing key.

Pay requests are found by the same `SubContract` join and linked the same way. That link matters more than it looks: **a receiver without it leaves reporting treating the item as never received, so depreciation never starts** — the gap behind Premier's manual quarterly catch-up.

```http
POST /api/project/{projectCode}/docs
{
  "IncludeDocs": true,
  "IncludeFiles": false,
  "IncludeClosed": true,
  "ResultLimit": 60,
  "ForDocType": "5b0a71d8-ed55-455b-bb99-2ab7d3b7a1cf"
}
   then link every row whose SubContract equals the PO number
```

## 10. Read everything back

```http
GET /api/document/{receiptKey}              -> DocNo, SubContract
GET /api/document/{receiptKey}/items        -> the line, as {DocNo}-{seq}
GET /api/document/{receiptKey}/attachments  -> POD + report + PO link + pay requests
```

**A 200 from this API proves nothing.** Empty-body writes return 204 whether they did anything or not, and an invented document GUID returns 500 rather than 404. The read-back is the only evidence any of it landed.

## 11. What Spitfire shows afterwards

**Nothing, until a human approves the receipt.** It is left *In Process* and never routed, so the purchase order goes on reporting nothing received. That is by design — dispatching the approval chain emails real Premier staff, and that is Premier's call, not a button's.

---

## Reference — every endpoint, and what it answered

Captured 2026-08-18 against `https://training.remingtonhotels.com/Training`.

| Method | Path | Status | Rows | Why |
|---|---|---|---|---|
| `GET` | `/api/system/version` | 200 | — | anonymous; proves reachability |
| `GET` | `/api/account/session` | 200 | — | session probe |
| `GET` | `/api/session/who` | 200 | — | whose ticket this is |
| `GET` | `/api/projects` | 405 | — | cannot enumerate projects; documented 405 |
| `POST` | `/api/projects` | 200 | 0 | the POST form; why the three project ids are configuration, not discovery |
| `POST` | `/api/project/MRC024PB100003/docs` | 200 | 1 | search MRC024PB100003 for PO 212559 |
| `POST` | `/api/project/MRC024PB100002/docs` | 200 | 0 | search MRC024PB100002 for PO 212559 |
| `POST` | `/api/project/MRC026PB100002/docs` | 200 | 0 | search MRC026PB100002 for PO 212559 |
| `POST` | `/api/catalog/search/0/contents` | 400 | — | resolve_po's fallback — never actually executes |
| `GET` | `/api/document/1023ab34-f62c-4a2f-9d73-4c73446d3473` | 200 | — | PO header |
| `GET` | `/api/document/1023ab34-f62c-4a2f-9d73-4c73446d3473/items` | 200 | 6 | PO lines |
| `GET` | `/api/document/1023ab34-f62c-4a2f-9d73-4c73446d3473/addresses` | 200 | 2 | vendor and ship-to |
| `GET` | `/api/document/1023ab34-f62c-4a2f-9d73-4c73446d3473/route` | 200 | 5 | purchasing agent |
| `GET` | `/api/document/1023ab34-f62c-4a2f-9d73-4c73446d3473/attachments` | 200 | 12 | what hangs off the PO |
| `GET` | `/api/configuration/doctypes` | 404 | — | would list document type GUIDs |
| `GET` | `/api/configuration/documenttypes` | 404 | — | would list document type GUIDs |
| `GET` | `/api/configuration/doctype` | 404 | — | would list document type GUIDs |
| `GET` | `/api/config/doctypes` | 500 | — | would list document type GUIDs |
| `GET` | `/api/document/1023ab34-f62c-4a2f-9d73-4c73446d3473/dates` | 200 | 1 | allowlisted but deliberately never called |
| `GET` | `/api/document/00000000-0000-0000-0000-000000000001` | 500 | — | an invented document GUID |
| `GET` | `/api/document/1023ab34-f62c-4a2f-9d73-4c73446d3473/items` | 200 | 6 | re-read to measure ReceiptInProgressUnits against known receipts |

## Reference — endpoints that answer nothing useful

| Endpoint | What happens | What we do instead |
|---|---|---|
| `POST /api/projects` | **200** with zero rows | the 3 project ids are configuration |
| `GET /api/projects` | **405** | — |
| `GET /api/configuration/*` | **404** — no such controller | doc-type GUIDs hardcoded |
| `GET /api/config/doctypes` | **500** | as above |
| `POST /api/catalog/search/0/contents` | **400** "TitleLike requires a value" | dead fallback; project-scoped search works |
| `GET /api/document/{unknown-guid}` | **500**, byte-identical to a real fault | a 500 can never be read as "not found" |
| `GET /api/document/{id}/dates` | 200, but unnamed-GUID schedule rows | order date is the header's `DocDate` |
| `PUT /api/document/{id}/items` | **500** | POST, with an array body |
| `PUT /api/document/{id}/link` | **500** on an existing key | links go through `/attachments` |
| `GET /api/catalog/{key}/meta`, `/object` | **500** on a key `/versions` accepts | `/versions` |

## Reference — the GUIDs we hardcode, and why

The endpoint that would list document types does not exist, so these come from Premier's own `czx_TPICreate_ReceiptDoc.sql` and from reading `xsfDocHeader` directly.

| Constant | Value | Provenance |
|---|---|---|
| purchase order type | `ff1975fd-76de-486c-888b-54e8fcd880e0` | @PODTK. The master plan transcribes this one character short. |
| receipt type | `0c9a537a-3c41-4d16-ab9f-130ef69ea6c8` | 76,414 documents carry it; `DocTypeKey_dv` reads "Receipt" |
| pay request type | `5b0a71d8-ed55-455b-bb99-2ab7d3b7a1cf` | @PRDTK in the same SQL |
| null GUID | `00000000-0000-0000-0000-000000000000` | Spitfire's "no value" |
| project ids | `MRC024PB100003, MRC024PB100002, MRC026PB100002` | read from `xsfDocHeader`; 18 / 8 / 2 corpus POs |

## Reference — corrections this exercise produced

Five things the codebase or an earlier report asserted that the server contradicts. All five checked by calling the endpoint.

| Was documented as | Actually | Consequence |
|---|---|---|
| `/api/configuration/*` returns 500 "case 36629" | **404** — "No type was found that matches the controller named 'configuration'" | the controller does not exist; the case may be chasing something that never did |
| the catalog search "answers 200 with zero rows" | **400** — needs `TitleLike`, we send `DocNoLike` | `resolve_po`'s fallback has never executed |
| "re-sending an attach creates a second row" | **400** "This file is already attached" | no-retry is still right, but because a retried *upload* mints a new fileKey which then attaches cleanly |
| `AttachNote` carries the note | silently ignored — 200, reads back `Note: null`. `Note` is the field that works | the old write probe stored notes that were never saved |
| `CatType` groups attachments | dropped — sent a valid GUID, stored `00000000-…` | attachments cannot be grouped by category |

## Reference — auth, in one paragraph

**Spitfire** has no `Authorization` header. One cookie, `sfPMSAuth`, is the FormsAuthentication ticket and is sufficient alone; the other three a browser holds are a session id, settings and a session GUID, none of which authenticate. It is copied by hand from a browser because Premier's Spitfire has Entra SSO, and it **lapses on idle** — which is why an expired ticket raises a named error instead of being silently re-authenticated. Every write is recorded by the ERP as `api@consciouscreations.ai` regardless of who triggered it, so our own audit log is the only thing that can tell two operators apart. **Graph** is unrelated: MSAL client-credentials, bearer token, app-only permissions.
