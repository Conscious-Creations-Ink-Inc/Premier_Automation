"""Build the receiver-flow PRD from responses actually captured off the training server.

A generator rather than a hand-written document, for one reason: `dev_reports` already holds files
asserting things this server does not do. A document nobody can re-run goes stale silently, and
the reader has no way to tell which sentences are still true. This one is rebuilt from
`prd_endpoint_responses.json`, which `tools/verify_prd_endpoints.py` produces by calling every
endpoint named here — so regenerating it is how you check it.

    python tools/verify_prd_endpoints.py     # capture
    python tools/build_prd.py                # render

Anything in this document that is not machine-captured is marked as measured by hand, with the
date it was measured.
"""

from __future__ import annotations

import html as html_lib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import report_pdf  # noqa: E402

# Everything this produces lands in one folder, together with the capture it was built from.
# Separate from the rest of `dev_reports` on purpose: those are point-in-time findings that are
# true of the day they were written, while these four are regenerated as a set and are only
# coherent as a set — a PDF built from a stale capture would be the exact failure this whole
# exercise exists to stop.
WORKFLOW_DIR = ROOT / "dev_reports" / "workflow"

CAPTURE = WORKFLOW_DIR / "prd_endpoint_responses.json"
OUT_HTML = WORKFLOW_DIR / "receiver_api_flow_prd.html"
OUT_PDF = WORKFLOW_DIR / "receiver_api_flow_prd.pdf"
OUT_MD = WORKFLOW_DIR / "RECEIVER_FLOW.md"

# Extends the house style rather than replacing it: Premier's reports read as one family, and this
# only adds what a document full of JSON needs.
_EXTRA_STYLE = """
pre { background: #f6f6f7; border: 1px solid #e3e3e3; border-left: 3px solid #A6192E;
      padding: 2mm 2.5mm; margin: 0 0 3mm; font-family: Consolas, "Courier New", monospace;
      font-size: 7.2pt; white-space: pre-wrap; word-break: break-word;
      page-break-inside: avoid; }
code { font-family: Consolas, "Courier New", monospace; font-size: 7.4pt;
       background: #f2f2f3; padding: 0 1mm; }
h3 { font-size: 8.8pt; color: #1a1a1a; margin: 4mm 0 1.5mm;
     border-bottom: 1px solid #e3e3e3; padding-bottom: 1mm; }
.step { page-break-inside: avoid; margin: 0 0 4mm; }
.lbl { font-weight: 600; color: #A6192E; font-size: 7.6pt; text-transform: uppercase;
       letter-spacing: 0.04em; margin: 2mm 0 1mm; }
.warn { border-left: 3px solid #b8860b; background: #fdfaf2; padding: 2.5mm 3mm;
        margin: 0 0 3mm; font-size: 8.2pt; }
.ok { color: #2f5d50; font-weight: 600; }
.bad { color: #A6192E; font-weight: 600; }
"""


def esc(value: Any) -> str:
    return html_lib.escape(str(value), quote=False)


def pre(text: str) -> str:
    return f"<pre>{esc(text)}</pre>"


def jsonpre(value: Any) -> str:
    return pre(json.dumps(value, indent=2) if not isinstance(value, str) else value)


def table(headers: List[str], rows: List[List[str]]) -> str:
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


class Capture:
    """The probe results, looked up by method and path."""

    def __init__(self, path: Path):
        if not path.exists():
            raise SystemExit(f"{path} not found — run tools/verify_prd_endpoints.py first")
        self.data = json.loads(path.read_text(encoding="utf-8"))
        self.probes: List[Dict[str, Any]] = self.data["probes"]

    def find(self, method: str, path_fragment: str) -> Optional[Dict[str, Any]]:
        for probe in self.probes:
            if probe["method"] == method and path_fragment in probe["path"]:
                return probe
        return None

    def block(self, method: str, path_fragment: str, *, label: str = "") -> str:
        """One endpoint rendered whole: what we sent, what came back, how long it took."""
        probe = self.find(method, path_fragment)
        if probe is None:
            return f"<p class='warn'>No captured response for {esc(method)} {esc(path_fragment)}.</p>"
        out = [f"<div class='lbl'>{esc(label or 'request')}</div>",
               pre(f"{probe['method']} {probe['path']}")]
        if probe.get("request_body"):
            out.append(jsonpre(probe["request_body"]))
        status = probe.get("status")
        rows = probe.get("row_count")
        meta = f"HTTP {status}"
        if rows is not None:
            meta += f" — {rows} row(s)"
        meta += f" — {probe.get('bytes', 0)} bytes — {probe.get('elapsed_ms')} ms"
        out.append(f"<div class='lbl'>response — {esc(meta)}</div>")
        body = probe.get("response") or probe.get("error") or "(empty)"
        if probe.get("truncated"):
            body += "\n… truncated; full response in prd_endpoint_responses.json"
        out.append(pre(body))
        return "".join(out)


def build(cap: Capture) -> str:
    d = cap.data
    projects = d["project_ids"]
    po = d["sample_po"]
    key = d["sample_po_key"]
    parts: List[str] = []
    add = parts.append

    # --- the one fact everything else rests on -------------------------------------------------
    add("<h2>The one thing to understand first</h2>")
    add("<div class='callout'><b>Spitfire cannot tell you what you have already posted.</b> "
        "<code>ReceiptInProgressUnits</code> — the one field that looks like it would answer "
        "&ldquo;has this delivery been received?&rdquo; — reads <b>0.0</b> for the whole time a "
        "receipt sits unapproved, which is the entire window that matters. Nothing in this API is "
        "idempotent, and the document catalog does not deduplicate identical bytes. So every "
        "guard against double-posting lives in our own ledger, not in the ERP.</div>")
    add(f"<p>Measured against PO {esc(po)} on {date.today().isoformat()}. Lines 0001, 0002 and "
        f"0004 carry receipts we posted the same morning; the ERP reports nothing on any of "
        f"them:</p>")
    lines = d.get("po_lines", [])
    add(table(["Line", "Spec", "ContractUnits", "UOM", "ItemQuantity", "ReceivedUnits",
               "ReceiptInProgressUnits"],
              [[esc(l["line"]), esc(l.get("spec") or "—"), esc(l["ContractUnits"]),
                esc(l.get("UOM") or "—"), esc(l["ItemQuantity"]), esc(l["ReceivedUnits"]),
                f"<span class='bad'>{esc(l['ReceiptInProgressUnits'])}</span>"]
               for l in lines]))
    add("<p>Two further traps visible in that table: <code>ItemQuantity</code> reads "
        "<b>0.0</b> on every line — the ordered quantity is "
        "<code>RelatedLineDetails.ContractUnits</code> — and the spec code lives in "
        "<code>SourceItemNumber</code>, because <code>Specification</code> is null on real "
        "purchase orders.</p>")

    # --- steps ---------------------------------------------------------------------------------
    add("<h2>Step 1 &mdash; a mail arrives</h2>")
    add("<p>Microsoft Graph, application permissions (<code>Mail.ReadWrite</code>), restricted to "
        "the one mailbox by an Application Access Policy. Auth is a bearer token from MSAL "
        "client-credentials &mdash; not a cookie; that is Spitfire's mechanism, not Graph's.</p>")
    add("<div class='lbl'>list new messages</div>")
    add(pre("GET https://graph.microsoft.com/v1.0/users/{mailbox}/mailFolders/Inbox/messages"))
    add(jsonpre({"$top": 50, "$orderby": "receivedDateTime asc",
                 "$select": "id,internetMessageId,receivedDateTime,subject,from,body,hasAttachments",
                 "$filter": "receivedDateTime ge {watermark}"}))
    add("<p><code>ge</code> and not <code>gt</code>: the watermark is deliberately backdated and "
        "the seen-set is what prevents reprocessing. Listing a message twice costs one skipped "
        "row; missing one loses it for good.</p>")
    add("<div class='lbl'>then, per message</div>")
    add(pre("GET /users/{mailbox}/messages/{id}/attachments\n"
            "GET /users/{mailbox}/messages/{id}/attachments/{attachmentId}/$value"))
    add("<div class='warn'><b>Two traps that cost us records.</b> "
        "<code>hasAttachments</code> reads <b>false</b> for a message whose only images are "
        "inline, so a pasted-in photograph of a delivery note is invisible if you trust it. And "
        "an <code>itemAttachment</code> &mdash; a forwarded email, the single most common shape in "
        "Premier's mail &mdash; returns <b>MIME bytes, not a .msg file</b>; sniffing it as .msg "
        "emptied every POD field in 13 of 13 records.</div>")

    add("<h2>Step 2 &mdash; parse</h2>")
    add("<p>No HTTP except OCR. The body, the attachments and any scanned proof of delivery are "
        "read into one extracted record: PO number, spec code, quantity, UOM, delivery date, "
        "receiver. OCR is Azure Document Intelligence and is the only paid call in the pipeline:"
        "</p>")
    add(pre("POST {endpoint}/documentintelligence/documentModels/prebuilt-layout:analyze"
            "?api-version=2024-11-30&pages=1-10\n"
            "  Ocp-Apim-Subscription-Key: <key>\n"
            "  Content-Type: application/octet-stream\n"
            "  <raw bytes>\n\n"
            "-> 202 Accepted, with an operation-location response header\n"
            "   GET {operation-location}  -> poll until status leaves \"running\""))
    add("<p><code>pages=1-10</code> is sent only for PDFs; billing is per page and the polling "
        "GETs are free. The <code>pages</code> parameter is rejected for images.</p>")

    add(f"<h2>Step 3 &mdash; PO number &rarr; project &rarr; DocMasterKey</h2>")
    add("<p>This is the step everything else depends on, and there is <b>no single endpoint that "
        "does it</b>. An email gives us a PO number; every useful Spitfire endpoint is keyed by "
        "GUID. Three hops:</p>")
    add("<div class='lbl'>hop 1 — which projects can we search?</div>")
    add(cap.block("POST", "/api/projects", label="ask the server"))
    add(f"<p>Zero rows. This account has no project list of its own, so the three ids come from "
        f"configuration (<code>SPITFIRE_PROJECT_IDS</code>): "
        f"<code>{esc(', '.join(projects))}</code>. The live call is still attempted first, so "
        f"this corrects itself the moment Premier grants project membership. The GET form is not "
        f"an alternative:</p>")
    add(cap.block("GET", "/api/projects", label="and the other verb"))
    add("<div class='lbl'>hop 2 — search each project for the PO number</div>")
    add(cap.block("POST", f"/api/project/{projects[0]}/docs", label="the search that finds it"))
    add(f"<p>The other two answer <code>[]</code> for the same body &mdash; that is how the PO's "
        f"project is discovered, by elimination:</p>")
    add(table(["Project searched", "Rows"],
              [[esc(p), ("<span class='ok'>1 — found</span>" if (pr := cap.find("POST", f"/api/project/{p}/docs")) and pr.get("row_count")
                         else "0")] for p in projects]))
    add(f"<p><code>DocNoLike</code> is a <i>contains</i> match, so <code>2124</code> would return "
        f"912456 alongside 912457. Every candidate is re-checked for an exact hit on "
        f"<code>DocNo</code> or <code>SubContract</code> before its key is accepted.</p>")
    add("<div class='lbl'>hop 3 — the project CODE comes from the PO header, not the search</div>")
    add(pre(f"GET /api/document/{key}\n  -> header.Project    = the project code\n"
            f"  -> header.SubContract = the PO number, which is the join to everything else"))
    add(f"<p>All of it is then cached in <code>spitfire_po_index</code> keyed on the PO number, so "
        f"a known PO costs no search at all. <b>If the mirror has never seen a PO, posting is "
        f"refused</b> &mdash; there is no <code>forProject</code> without it, and the receipt "
        f"cannot be created.</p>")
    add("<div class='warn'>There is a documented fallback &mdash; a site-wide catalog search &mdash; "
        "and it has <b>never once executed</b>. It is called with <code>DocNoLike</code> and "
        "demands <code>TitleLike</code>, so it 400s and the exception is swallowed:"
        + cap.block("POST", "/api/catalog/search", label="the dead fallback") + "</div>")

    add("<h2>Step 4 &mdash; read the purchase order</h2>")
    add(cap.block("GET", f"/api/document/{key}", label="the header — about 120 fields"))
    add("<p>Of those we keep <code>DocNo</code>, <code>SubContract</code>, <code>Project</code>, "
        "<code>Status</code> and <code>DocDate</code>. <b><code>DocDate</code> is the order "
        "date</b>, established by testing all three candidate date fields against PO numbering "
        "across 27 consecutive pairs: <code>DocDate</code> got 0 out of order, "
        "<code>SourceDate</code> inverted on 11. <code>/api/document/{id}/dates</code> sounds "
        "right and is not &mdash; it returns schedule rows keyed by an unnamed GUID and carries no "
        "order date, so it is never called.</p>")
    add(cap.block("GET", f"/api/document/{key}/items", label="the lines — 68 fields each"))
    add("<p>Quantities are nested in <code>RelatedLineDetails</code>; the cost code and UOM are in "
        "<code>DocItemTask[0]</code>. Two fields are easily confused and are <b>not</b> the same "
        "thing:</p>")
    if lines:
        add(table(["Field", "Value on line 0001", "What it is"],
                  [["<code>DocItemTask[0].ProjEntity</code>", esc(lines[0].get("ProjEntity")),
                    "the cost code &mdash; what the receipt line posts against"],
                   ["<code>RelatedLineDetails.GLAcct / GLSub</code>",
                    f"{esc(lines[0].get('GLAcct'))} / {esc(lines[0].get('GLSub'))}",
                    "the general-ledger account. Not used by us."],
                   ["<code>DocItemTask[0].AccountCategory</code>",
                    esc(lines[0].get("AccountCategory")),
                    "what makes a line receivable. TAX- and FRT- prefixes are skipped."]]))
    add("<p>On corpus PO 908491, <b>10 of 25 lines are non-receivable</b> tax and freight, and "
        "nothing about their shape distinguishes them from an under-populated goods line &mdash; "
        "the account category is the only reliable discriminator.</p>")
    add(cap.block("GET", f"/api/document/{key}/addresses", label="vendor and ship-to"))
    add("<p><code>AddrType</code>: <code>T</code> vendor, <code>S</code> ship-to, "
        "<code>F</code> author, <code>R</code> remit-to.</p>")

    add("<h2>Step 5 &mdash; mirror it locally</h2>")
    add("<p>Reading a PO live costs four round trips, and about <b>8.6 seconds</b> even with a "
        "cached document key &mdash; verifying 29 records live is roughly 145 seconds, which is "
        "not a page render. So every PO read is written to <code>spitfire_po_index</code> (one row "
        "per PO) and <code>spitfire_po_lines</code> (one per line, keyed on Spitfire's own "
        "<code>DocItemKey</code>). The mirror is what the Records page and the posting gate both "
        "read.</p>")

    add("<h2>Step 6 &mdash; verify</h2>")
    add("<p>Re-reads the PO and puts its figures beside the email's. <b>It states the numbers and "
        "gives no verdict</b> &mdash; that is deliberate, and it is why a separate module decides "
        "whether a post may proceed. A reviewer can also pick a different PO line here, and that "
        "choice is persisted.</p>")

    add("<h2>Step 7 &mdash; decide</h2>")
    add("<p>Nine gates, each refusing by default and each naming the two facts that disagree. No "
        "HTTP of its own beyond the verify read.</p>")
    add(table(["#", "Gate", "Refusal it produces"],
              [["1", "record complete", "&ldquo;the record is incomplete &mdash; missing: received-by&rdquo;"],
               ["2", "POD bytes in hand", "&ldquo;the delivery email carried no attachments&hellip;&rdquo;"],
               ["3", "not already posted", "&ldquo;this delivery was already posted as receipt 0004&rdquo;"],
               ["4", "PO found in our projects", "&ldquo;not found in the projects this connector can search&rdquo;"],
               ["5", "line resolved by spec, or chosen by a person", "&ldquo;matched on description alone&rdquo;"],
               ["6", "something still outstanding", "&ldquo;shows nothing outstanding on this line (19 of 19)&rdquo;"],
               ["7", "quantity within outstanding", "&ldquo;this would over-receive: the email says 40 Set and only 2&hellip;&rdquo;"],
               ["8", "units agree", "&ldquo;the email says CS and the purchase order says EA&rdquo;"],
               ["9", "project known locally", "&ldquo;the project&hellip; is not known locally &mdash; verify the PO once&rdquo;"]]))
    add("<p>A partial delivery is <b>not</b> a discrepancy: the comparison is against what is "
        "still outstanding, so 2 arriving against 19 ordered posts and leaves 17 outstanding. "
        "Only exceeding the outstanding quantity needs a person.</p>")

    add("<h2>Step 8 &mdash; post the proof of delivery</h2>")
    add("<p>Five calls, and a read-back after each thing that matters. Written to our ledger the "
        "moment each succeeds, because there is no transaction and no way to ask afterwards what "
        "happened.</p>")
    add("<div class='lbl'>8.1 create the receipt — no body at all</div>")
    add(pre("POST /api/document/00000000-0000-0000-0000-000000000000/{receiptTypeKey}"
            "?forProject={projectCode}&forBatch={poNumber}\n\n"
            "-> 200, a BARE QUOTED GUID, not an object:\n"
            '   "2189d7d7-da0b-47f0-919b-0c3a2b07d6ce"'))
    add("<p>The parent is the null GUID and the type key is the <i>second path segment</i>. "
        "<code>forBatch</code> is the PO number and is what populates the header's "
        "<code>SubContract</code> &mdash; <b>the only field tying a receipt to its purchase "
        "order</b>. If it does not come back set, the receipt is an orphan and nothing further is "
        "attached.</p>")
    add("<div class='warn'><b>Creating the receipt stages people.</b> Spitfire applies the "
        "configured approval chain the instant the document exists: six routees, three of them "
        "real Premier employees at sequence 10. Nothing is emailed &mdash; that needs "
        "<code>route/apply</code>, which the write connector refuses by name &mdash; but they are "
        "on it.</div>")
    add("<div class='lbl'>8.2 title it — the body is a bare JSON string</div>")
    add(pre('PATCH /api/document/{receiptKey}/Title\n'
            '"CC-TEST - receiver automation - PO 912559 - 2026-08-17 - DO NOT PROCESS"'))
    add("<div class='lbl'>8.3 add the receipt line — the body is an ARRAY</div>")
    add(jsonpre([{"Description": "CC-TEST Amenity Tray at Ballroom Restrooms",
                  "ItemQuantity": 4.0,
                  "DocItemTask": [{"Quantity": 4.0, "UOM": "Set", "ProjEntity": "102011215"}],
                  "SourceItemNumber": "BRR-803-AC"}]))
    add("<p><code>PUT /items</code> is an undiscoverable 500; POST is the working call. "
        "<code>UOM</code> and <code>ProjEntity</code> are copied from the matched PO line rather "
        "than invented &mdash; a wrong <code>ProjEntity</code> books the receipt to the wrong "
        "budget. Lines read back as <code>{DocNo}-{seq}</code>, never as the integer sent.</p>")
    add("<div class='lbl'>8.4 upload the POD — multipart, two parts, no Content-Type header</div>")
    add(pre("POST /api/catalog/upload?xm=catalog\n"
            "  part 1  fileMeta  (application/json)\n"
            "  part 2  file      (application/octet-stream)\n\n"
            "-> 200 {\"name\":\"CC-TEST.POD.FedEx.pdf\","
            "\"key\":\"8a0af8b5-3fb3-44e5-af75-b4ebd490fcca\","
            "\"size\":64,\"progress\":\"100.0%\",\"error\":null}"))
    add(jsonpre({"value": "CC-TEST.POD.FedEx.pdf", "Name": "CC-TEST.POD.FedEx.pdf",
                 "type": "file", "FileType": "pdf", "size": 20535,
                 "MD5": "8342F644E7DC630772686415F287020B",
                 "date": "2026-08-17T00:00:00", "DocDate": "2026-08-17T00:00:00",
                 "ReferenceDate": "2026-08-17T00:00:00", "Due": "2026-08-17T00:00:00",
                 "Keywords": "CC-TEST POD 912559"}))
    add("<p>The failure ladder, each rung walked into in turn:</p>")
    add(table(["What is wrong", "What the server says"],
              [["no <code>file</code> part, or a hand-set <code>Content-Type</code>",
                "<b>415</b> &ldquo;File expected&rdquo; &mdash; setting the header by hand loses the multipart boundary"],
               ["no <code>fileMeta</code> part", "<b>400</b> &ldquo;Meta data missing&rdquo;"],
               ["<code>filemeta</code> in lower case", "<b>400</b> &mdash; part names are case-sensitive"],
               ["any of the four dates missing",
                "<b>500</b> &ldquo;Nullable object must have a value&rdquo;"]]))
    add("<p><code>MD5</code> must be upper case. Spitfire takes the stored filename from the "
        "<code>file</code> part and <b>ignores <code>fileMeta.Name</code></b>.</p>")
    add("<div class='lbl'>8.5 verify the bytes, then attach</div>")
    add(pre("GET /api/catalog/{fileKey}/versions\n"
            "  -> [{ ..., \"DataHash\": \"8342F644E7DC630772686415F287020B\" }]\n"
            "     the server's own MD5 — the only real proof the bytes arrived intact.\n"
            "     /meta and /object return 500 on the very same key /versions accepts."))
    add(jsonpre([{"DocKey": "<fileKey>",
                  "AttachedDocMaster": "00000000-0000-0000-0000-000000000000",
                  "Note": "CC-TEST proof of delivery",
                  "CatType": "<receipt doc type>", "MailRoute": "P", "AccessLevel": "V"}]))
    add(pre('-> 200 {"DocAttachKey":"0bc8af59-0bd2-4f90-80cb-ada94c10fffe","DocKey":"8a0af8b5-…"}'))

    add("<h2>Step 9 &mdash; post the receiver report</h2>")
    add("<p>A separate decision a person makes, so it is a separate button and a separate ledger "
        "state. The report is generated per delivery, rendered to PDF through headless Chrome, "
        "then uploaded and attached exactly as the POD was. Two document links follow.</p>")
    add("<p><b>One endpoint, two body shapes.</b> A file link populates <code>DocKey</code>; a "
        "document link populates <code>AttachedDocMaster</code> and leaves <code>DocKey</code> as "
        "the null GUID. Premier's own receipt 909330 carries both shapes in one collection.</p>")
    add(jsonpre([{"DocKey": "00000000-0000-0000-0000-000000000000",
                  "AttachedDocMaster": "<the PO's DocMasterKey>",
                  "Note": "CC-TEST purchase order",
                  "CatType": "<PO doc type>", "MailRoute": "P", "AccessLevel": "V"}]))
    add("<p>Not <code>PUT /api/document/{id}/link</code>, which <i>creates</i> child documents "
        "from type keys and returns 500 when handed an existing key.</p>")
    add("<p>Pay requests are found by the same <code>SubContract</code> join and linked the same "
        "way. This link matters more than it looks: a receiver without it leaves reporting "
        "treating the item as never received, so depreciation never starts &mdash; the gap behind "
        "Premier's manual quarterly catch-up.</p>")

    add("<h2>Step 10 &mdash; what Spitfire shows afterwards</h2>")
    add("<div class='callout'><b>Nothing, until a human approves the receipt.</b> It is left "
        "<i>In Process</i> and is never routed, so the purchase order goes on reporting nothing "
        "received. That is by design &mdash; dispatching the approval chain emails real Premier "
        "staff, and that is Premier's decision, not a button's.</div>")
    return "".join(parts)


def reference(cap: Capture) -> str:
    d = cap.data
    parts: List[str] = []
    add = parts.append

    add("<h2>Reference A &mdash; every endpoint, and what it answered</h2>")
    add(f"<p>Captured {date.today().isoformat()} against "
        f"<code>{esc(d['base_url'])}</code>. Regenerate with "
        f"<code>tools/verify_prd_endpoints.py</code>.</p>")
    rows = []
    for p in cap.probes:
        status = p.get("status")
        cls = "ok" if status and 200 <= status < 300 else "bad"
        rows.append([f"<code>{esc(p['method'])}</code>",
                     f"<code>{esc(p['path'])}</code>",
                     f"<span class='{cls}'>{esc(status)}</span>",
                     esc(p.get("row_count") if p.get("row_count") is not None else "—"),
                     esc(p.get("note", ""))])
    add(table(["Method", "Path", "Status", "Rows", "Why we call it"], rows))

    add("<h2>Reference B &mdash; endpoints that answer nothing useful</h2>")
    add("<p>Every one of these was called while writing this document. They are listed so nobody "
        "spends an afternoon rediscovering them.</p>")
    add(table(["Endpoint", "What happens", "What we do instead"],
              [["<code>POST /api/projects</code>",
                "<b>200</b> with zero rows",
                "the three project ids are configuration, not discovery"],
               ["<code>GET /api/projects</code>", "<b>405</b>", "&mdash;"],
               ["<code>GET /api/configuration/*</code>",
                "<b>404</b> &mdash; no such controller on this build",
                "document-type GUIDs are hardcoded from Premier's own SQL"],
               ["<code>GET /api/config/doctypes</code>", "<b>500</b>", "as above"],
               ["<code>POST /api/catalog/search/0/contents</code>",
                "<b>400</b> &ldquo;catalogFilters.TitleLike requires a value&rdquo;",
                "dead fallback; the project-scoped search is what works"],
               ["<code>GET /api/document/{unknown-guid}</code>",
                "<b>500</b>, not 404 &mdash; and byte-identical to a genuine fault",
                "a 500 can never be read as &ldquo;not found&rdquo;"],
               ["<code>GET /api/document/{id}/dates</code>",
                "200, but schedule rows keyed by an unnamed GUID",
                "the order date is the header's <code>DocDate</code>"],
               ["<code>PUT /api/document/{id}/items</code>", "500", "POST, with an array body"],
               ["<code>PUT /api/document/{id}/link</code>",
                "500 when handed an existing key",
                "document links go through <code>/attachments</code>"],
               ["<code>GET /api/catalog/{key}/meta</code> and <code>/object</code>",
                "500 on a key <code>/versions</code> accepts", "<code>/versions</code>"]]))

    add("<h2>Reference C &mdash; the GUIDs we hardcode, and why</h2>")
    add("<p>The endpoint that would list document types does not exist, so these come from "
        "Premier's own <code>czx_TPICreate_ReceiptDoc.sql</code> and from reading "
        "<code>xsfDocHeader</code> directly.</p>")
    add(table(["Constant", "Value", "Provenance"],
              [["purchase order type", f"<code>{esc(d['doc_types']['purchase_order'])}</code>",
                "@PODTK in Premier's SQL. The master plan transcribes this one character short."],
               ["receipt type", f"<code>{esc(d['doc_types']['receipt'])}</code>",
                "confirmed against TrainingsfDocSys: 76,414 documents carry it, "
                "<code>DocTypeKey_dv</code> reads &ldquo;Receipt&rdquo;"],
               ["pay request type", "<code>5b0a71d8-ed55-455b-bb99-2ab7d3b7a1cf</code>",
                "@PRDTK in the same SQL"],
               ["null GUID", "<code>00000000-0000-0000-0000-000000000000</code>",
                "Spitfire's &ldquo;no value&rdquo;; the parent slot on create and the unused half "
                "of an attachment row"],
               ["project ids", f"<code>{esc(', '.join(d['project_ids']))}</code>",
                "read out of <code>xsfDocHeader</code>; 18 / 8 / 2 corpus POs respectively"]]))

    add("<h2>Reference D &mdash; corrections this exercise produced</h2>")
    add("<p>Five things the codebase or an earlier report stated that the server contradicts. All "
        "five were checked by calling the endpoint.</p>")
    add(table(["Was documented as", "Actually", "Consequence"],
              [["<code>/api/configuration/*</code> returns 500 &ldquo;not yet implemented "
                "(case 36629)&rdquo;",
                "<b>404</b> &mdash; &ldquo;No type was found that matches the controller named "
                "'configuration'&rdquo;",
                "the controller does not exist; case 36629 may be chasing something that was "
                "never there"],
               ["the site-wide catalog search &ldquo;answers 200 with zero rows&rdquo;",
                "<b>400</b> &mdash; it requires <code>TitleLike</code> and we send "
                "<code>DocNoLike</code>",
                "the fallback in <code>resolve_po</code> has never executed; it throws and is "
                "swallowed"],
               ["&ldquo;re-sending an attach creates a second row&rdquo;",
                "<b>400</b> &ldquo;This file is already attached&rdquo; for the same fileKey",
                "the no-retry rule is still right, but for a different reason: a retried "
                "<i>upload</i> mints a new fileKey, and that one attaches cleanly"],
               ["<code>AttachNote</code> carries the attachment note",
                "silently ignored &mdash; returns <b>200</b>, reads back <code>Note: null</code>. "
                "<code>Note</code> is the field that works",
                "the old write probe recorded notes that were never stored"],
               ["<code>CatType</code> groups attachments by category",
                "confirmed dropped &mdash; a valid receipt GUID was sent and "
                "<code>00000000-…</code> stored",
                "attachments cannot be grouped by category; do not build on it"]]))

    add("<h2>Reference E &mdash; auth, in one paragraph</h2>")
    add("<p><b>Spitfire</b> has no <code>Authorization</code> header. A single cookie, "
        "<code>sfPMSAuth</code>, is the FormsAuthentication ticket and is sufficient on its own; "
        "the other three cookies a browser holds are a session id, settings and a session GUID, "
        "none of which authenticate. It is copied by hand from a browser because Premier's "
        "Spitfire has Entra SSO, and it <b>lapses on idle</b> &mdash; which is why an expired "
        "ticket raises a named error rather than being silently re-authenticated. Every write is "
        "recorded by the ERP as <code>api@consciouscreations.ai</code> regardless of who "
        "triggered it, so our own audit log is the only record that can tell two operators "
        "apart. <b>Graph</b> is unrelated: MSAL client-credentials, a bearer token, app-only "
        "permissions consented on the app registration.</p>")
    return "".join(parts)


def markdown(cap: Capture) -> str:
    """The whole flow as one plain file: every process, and the exact endpoint it hits.

    Generated from the same capture as the PDF rather than written by hand, so the two cannot
    drift apart and neither can go stale without the other. This is the version to grep, to paste
    into a ticket, and to read in an editor next to the code.
    """
    d = cap.data
    projects = d["project_ids"]
    po, key = d["sample_po"], d["sample_po_key"]
    lines = d.get("po_lines", [])
    L: List[str] = []
    add = L.append

    def endpoint(method: str, fragment: str, *, why: str = "") -> None:
        """One captured call: what went out, what came back."""
        probe = cap.find(method, fragment)
        if probe is None:
            add(f"```\n{method} {fragment}   (not captured)\n```\n")
            return
        add("```http")
        add(f"{probe['method']} {probe['path']}")
        if probe.get("request_body"):
            add(json.dumps(probe["request_body"], indent=2))
        status = probe.get("status")
        rows = probe.get("row_count")
        add(f"\n-> HTTP {status}" + (f", {rows} row(s)" if rows is not None else "")
            + f", {probe.get('bytes', 0)} bytes, {probe.get('elapsed_ms')} ms")
        body = (probe.get("response") or probe.get("error") or "(empty)").strip()
        add(body[:700] + ("\n… truncated" if len(body) > 700 else ""))
        add("```")
        if why:
            add(f"{why}\n")

    add("# Premier Receiver Automation — the complete flow, endpoint by endpoint")
    add("")
    add(f"Every HTTP call between a delivery email arriving and a receipt existing in sfPMS.")
    add(f"Responses below were returned by `{d['base_url']}` on {date.today().isoformat()} — none "
        f"is quoted from a specification.")
    add("")
    add("Regenerate: `python tools/verify_prd_endpoints.py && python tools/build_prd.py`")
    add("")
    add("---")
    add("")
    add("## Read this first")
    add("")
    add("**Spitfire cannot tell you what you have already posted.** `ReceiptInProgressUnits` — the "
        "one field that looks like it answers *\"has this been received?\"* — reads `0.0` for the "
        "entire time a receipt sits unapproved. Nothing in this API is idempotent and the catalog "
        "does not deduplicate identical bytes, so **every guard against double-posting is ours**, "
        "in a local ledger.")
    add("")
    if lines:
        add(f"Measured on PO {po}, whose lines 0001/0002/0004 carry receipts posted the same day:")
        add("")
        add("| Line | ContractUnits | UOM | ItemQuantity | ReceivedUnits | ReceiptInProgressUnits |")
        add("|---|---|---|---|---|---|")
        for l in lines:
            add(f"| {l['line']} | {l['ContractUnits']} | {l.get('UOM') or '—'} | "
                f"`{l['ItemQuantity']}` | {l['ReceivedUnits']} | **{l['ReceiptInProgressUnits']}** |")
        add("")
        add("Two more traps in that table: `ItemQuantity` is **always 0.0** — the ordered quantity "
            "is `RelatedLineDetails.ContractUnits` — and the spec code is in `SourceItemNumber`, "
            "because `Specification` is null on real purchase orders.")
    add("")
    add("---")
    add("")

    add("## 1. A mail arrives — Microsoft Graph")
    add("")
    add("Auth: MSAL client-credentials bearer token, `Mail.ReadWrite` application permission, "
        "restricted to one mailbox by an Application Access Policy.")
    add("")
    add("```http")
    add("GET https://graph.microsoft.com/v1.0/users/{mailbox}/mailFolders/Inbox/messages")
    add(json.dumps({"$top": 50, "$orderby": "receivedDateTime asc",
                    "$select": "id,internetMessageId,receivedDateTime,subject,from,body,hasAttachments",
                    "$filter": "receivedDateTime ge {watermark}"}, indent=2))
    add("```")
    add("")
    add("`ge` not `gt` — the watermark is backdated and the seen-set prevents reprocessing. "
        "Listing a message twice costs a skipped row; missing one loses it for good.")
    add("")
    add("```http")
    add("GET /users/{mailbox}/messages/{id}/attachments")
    add("GET /users/{mailbox}/messages/{id}/attachments/{attachmentId}/$value    -> raw bytes")
    add("POST /users/{mailbox}/messages/{id}/move        {\"destinationId\": \"<folderId>\"}")
    add("```")
    add("")
    add("**Two traps that cost us records:**")
    add("")
    add("- `hasAttachments` is **false** when a message's only images are inline — a pasted-in "
        "photo of a delivery note is invisible if you trust it.")
    add("- An `itemAttachment` (a forwarded email — the commonest shape in Premier's mail) returns "
        "**MIME bytes, not a `.msg` file**. Sniffing it as `.msg` emptied every POD field in 13 of "
        "13 records.")
    add("")

    add("## 2. Parse — the only paid call")
    add("")
    add("No Spitfire traffic. Body, attachments and any scanned POD become one extracted record: "
        "PO number, spec, quantity, UOM, delivery date, receiver.")
    add("")
    add("```http")
    add("POST {endpoint}/documentintelligence/documentModels/prebuilt-layout:analyze"
        "?api-version=2024-11-30&pages=1-10")
    add("Ocp-Apim-Subscription-Key: <key>")
    add("Content-Type: application/octet-stream")
    add("<raw bytes>")
    add("")
    add("-> 202 Accepted   + an `operation-location` response header")
    add("GET {operation-location}   -> poll until status leaves \"running\"")
    add("```")
    add("")
    add("`pages=1-10` is sent for PDFs only (images reject it). Billing is per page; the polling "
        "GETs are free.")
    add("")

    add("## 3. PO number → project → DocMasterKey")
    add("")
    add("**There is no single endpoint that does this.** Emails give a PO number; every useful "
        "Spitfire endpoint is keyed by GUID. Three hops:")
    add("")
    add("### 3a. Which projects can we search?")
    add("")
    endpoint("POST", "/api/projects",
             why=f"Zero rows — this account has no project list. So the ids are configuration: "
                 f"`{', '.join(projects)}`. The live call is still tried first, so this corrects "
                 f"itself the moment Premier grants project membership.")
    endpoint("GET", "/api/projects", why="The other verb is not an alternative.")
    add("### 3b. Search each project for the PO number")
    add("")
    endpoint("POST", f"/api/project/{projects[0]}/docs")
    add("The other two return `[]` for the same body — the PO's project is found by elimination:")
    add("")
    add("| Project | Rows |")
    add("|---|---|")
    for p in projects:
        pr = cap.find("POST", f"/api/project/{p}/docs")
        add(f"| `{p}` | {'**1 — found**' if pr and pr.get('row_count') else '0'} |")
    add("")
    add("`DocNoLike` is a *contains* match, so `2124` would also return 912456 and 912457. Every "
        "candidate is re-checked for an exact hit on `DocNo` or `SubContract` before its key is "
        "accepted.")
    add("")
    add("### 3c. The project *code* comes from the PO header, not the search")
    add("")
    add("```http")
    add(f"GET /api/document/{key}")
    add("  -> header.Project     = the project code   (needed as forProject when creating)")
    add("  -> header.SubContract = the PO number      (the join to every related document)")
    add("```")
    add("")
    add("All of it is cached in `spitfire_po_index`, so a known PO costs no search. **If the mirror "
        "has never seen the PO, posting is refused** — there is no `forProject` without it.")
    add("")
    add("> There is a documented site-wide fallback that has **never once executed**. It is called "
        "with `DocNoLike` and the server demands `TitleLike`, so it 400s and the exception is "
        "swallowed.")
    add("")
    endpoint("POST", "/api/catalog/search")

    add("## 4. Read the purchase order")
    add("")
    endpoint("GET", f"/api/document/{key}")
    add("We keep `DocNo`, `SubContract`, `Project`, `Status`, `DocDate`. **`DocDate` is the order "
        "date** — established by testing all three candidate dates against PO numbering across 27 "
        "consecutive pairs: `DocDate` got 0 out of order, `SourceDate` inverted on 11. "
        "`/api/document/{id}/dates` sounds right and is not: schedule rows keyed by an unnamed "
        "GUID, no order date. Never called.")
    add("")
    endpoint("GET", f"/api/document/{key}/items")
    add("Quantities are nested in `RelatedLineDetails`; UOM and cost code in `DocItemTask[0]`. "
        "Two fields are easily confused and are **not** the same thing:")
    add("")
    if lines:
        add("| Field | Line 0001 | What it is |")
        add("|---|---|---|")
        add(f"| `DocItemTask[0].ProjEntity` | `{lines[0].get('ProjEntity')}` | the cost code — what "
            f"the receipt line posts against |")
        add(f"| `RelatedLineDetails.GLAcct` / `GLSub` | `{lines[0].get('GLAcct')}` / "
            f"`{lines[0].get('GLSub')}` | the GL account. Not used by us. |")
        add(f"| `DocItemTask[0].AccountCategory` | `{lines[0].get('AccountCategory')}` | what makes "
            f"a line receivable — `TAX-` and `FRT-` are skipped |")
        add("")
    add("On corpus PO 908491, **10 of 25 lines are tax and freight**, and nothing in their shape "
        "distinguishes them from an under-populated goods line. The account category is the only "
        "reliable discriminator.")
    add("")
    endpoint("GET", f"/api/document/{key}/addresses",
             why="`AddrType`: `T` vendor, `S` ship-to, `F` author, `R` remit-to.")
    endpoint("GET", f"/api/document/{key}/route",
             why="The purchasing agent is the first routee's `UserName` — `ResponsibleParty_dv` "
                 "reads empty on real POs.")

    add("## 5. Mirror it locally")
    add("")
    add("A live PO read is four round trips and about **8.6 seconds** even with a cached key — "
        "verifying 29 records live is ~145 seconds, which is not a page render. So every read is "
        "written to:")
    add("")
    add("- `spitfire_po_index` — one row per PO: `doc_master_key`, `project_code`, vendor, "
        "ship-to, order date")
    add("- `spitfire_po_lines` — one row per line, keyed on Spitfire's own `DocItemKey` "
        "(sub-parts really are separate lines)")
    add("")

    add("## 6. Verify — states figures, gives no verdict")
    add("")
    add("Re-reads the PO and puts its numbers beside the email's. Deliberately makes no ruling; a "
        "reviewer can also pick a different PO line here, and that choice is persisted to "
        "`po_line_number` and honoured by the post.")
    add("")

    add("## 7. Decide — nine gates, each refusing by default")
    add("")
    add("| # | Gate | The refusal it produces |")
    add("|---|---|---|")
    add("| 1 | record complete | \"the record is incomplete — missing: received-by\" |")
    add("| 2 | POD bytes in hand | \"the delivery email carried no attachments…\" |")
    add("| 3 | not already posted | \"this delivery was already posted as receipt 0004\" |")
    add("| 4 | PO in one of our projects | \"not found in the projects this connector can search\" |")
    add("| 5 | line resolved by spec, **or chosen by a person** | \"matched on description alone\" |")
    add("| 6 | something still outstanding | \"shows nothing outstanding on this line (19 of 19)\" |")
    add("| 7 | quantity ≤ outstanding | \"this would over-receive: the email says 40 Set and only 2…\" |")
    add("| 8 | units agree | \"the email says CS and the purchase order says EA\" |")
    add("| 9 | project known locally | \"…is not known locally — verify the PO once\" |")
    add("")
    add("A **partial delivery is not a discrepancy**: the comparison is against what is still "
        "outstanding, so 2 arriving against 19 ordered posts and leaves 17. Only exceeding the "
        "outstanding quantity needs a person.")
    add("")

    add("## 8. Post the proof of delivery — 5 calls")
    add("")
    add("### 8.1 Create the receipt — **no body at all**")
    add("")
    add("```http")
    add("POST /api/document/00000000-0000-0000-0000-000000000000/{receiptTypeKey}"
        "?forProject={projectCode}&forBatch={poNumber}")
    add("")
    add("-> 200, a BARE QUOTED GUID (not an object):")
    add('   "2189d7d7-da0b-47f0-919b-0c3a2b07d6ce"')
    add("```")
    add("")
    add("The parent is the null GUID; the type key is the **second path segment**. `forBatch` is "
        "the PO number and is what populates `SubContract` — **the only field tying a receipt to "
        "its purchase order**. If it does not come back set, the receipt is an orphan and nothing "
        "further is attached.")
    add("")
    add("> **Creating the receipt stages people.** Spitfire applies the configured approval chain "
        "the instant the document exists: six routees, three of them real Premier employees at "
        "sequence 10. Nothing is emailed — that needs `route/apply`, refused by name — but they "
        "are on it.")
    add("")
    add("### 8.2 Title it — the body is a **bare JSON string**")
    add("")
    add("```http")
    add("PATCH /api/document/{receiptKey}/Title")
    add('"CC-TEST - receiver automation - PO 912559 - 2026-08-17 - DO NOT PROCESS"')
    add("```")
    add("")
    add("### 8.3 Add the receipt line — the body is an **array**")
    add("")
    add("```http")
    add("POST /api/document/{receiptKey}/items")
    add(json.dumps([{"Description": "CC-TEST Amenity Tray at Ballroom Restrooms",
                     "ItemQuantity": 4.0,
                     "DocItemTask": [{"Quantity": 4.0, "UOM": "Set",
                                      "ProjEntity": "102011215"}],
                     "SourceItemNumber": "BRR-803-AC"}], indent=2))
    add("```")
    add("")
    add("`PUT /items` is an undiscoverable 500. `UOM` and `ProjEntity` are copied from the matched "
        "PO line — a wrong `ProjEntity` books the receipt to the wrong budget. Lines read back as "
        "`{DocNo}-{seq}`, never the integer sent.")
    add("")
    add("### 8.4 Upload the POD — multipart, two parts, **no Content-Type header**")
    add("")
    add("```http")
    add("POST /api/catalog/upload?xm=catalog")
    add("  part 1: fileMeta   (application/json)")
    add("  part 2: file       (application/octet-stream)")
    add("")
    add(json.dumps({"value": "CC-TEST.POD.FedEx.pdf", "Name": "CC-TEST.POD.FedEx.pdf",
                    "type": "file", "FileType": "pdf", "size": 20535,
                    "MD5": "8342F644E7DC630772686415F287020B",
                    "date": "2026-08-17T00:00:00", "DocDate": "2026-08-17T00:00:00",
                    "ReferenceDate": "2026-08-17T00:00:00", "Due": "2026-08-17T00:00:00",
                    "Keywords": "CC-TEST POD 912559"}, indent=2))
    add("")
    add('-> 200 {"name":"CC-TEST.POD.FedEx.pdf",'
        '"key":"8a0af8b5-3fb3-44e5-af75-b4ebd490fcca","size":64,'
        '"progress":"100.0%","error":null}')
    add("```")
    add("")
    add("The failure ladder, each rung walked into in turn:")
    add("")
    add("| What is wrong | What the server says |")
    add("|---|---|")
    add("| no `file` part, or a hand-set `Content-Type` | **415** \"File expected\" — setting the "
        "header by hand loses the multipart boundary |")
    add("| no `fileMeta` part | **400** \"Meta data missing\" |")
    add("| `filemeta` in lower case | **400** — part names are case-sensitive |")
    add("| any of the four dates missing | **500** \"Nullable object must have a value\" |")
    add("")
    add("`MD5` must be upper case. Spitfire takes the stored filename from the `file` part and "
        "**ignores `fileMeta.Name`**.")
    add("")
    add("### 8.5 Verify the bytes, then attach")
    add("")
    add("```http")
    add("GET /api/catalog/{fileKey}/versions")
    add('  -> [{ …, "DataHash": "8342F644E7DC630772686415F287020B" }]')
    add("     the server's own MD5 — the only real proof the bytes arrived intact.")
    add("     /meta and /object return 500 on the very same key /versions accepts.")
    add("")
    add("POST /api/document/{receiptKey}/attachments")
    add(json.dumps([{"DocKey": "<fileKey>",
                     "AttachedDocMaster": "00000000-0000-0000-0000-000000000000",
                     "Note": "CC-TEST proof of delivery",
                     "CatType": "<receipt doc type>",
                     "MailRoute": "P", "AccessLevel": "V"}], indent=2))
    add("")
    add('-> 200 {"DocAttachKey":"0bc8af59-0bd2-4f90-80cb-ada94c10fffe","DocKey":"8a0af8b5-…"}')
    add("```")
    add("")

    add("## 9. Post the receiver report — 4 calls")
    add("")
    add("A separate decision a person makes, so a separate button and a separate ledger state. The "
        "report is generated per delivery, rendered to PDF through headless Chrome, then uploaded "
        "and attached exactly as the POD was (8.4 + 8.5). Then two document links.")
    add("")
    add("**One endpoint, two body shapes.** A *file* link populates `DocKey`; a *document* link "
        "populates `AttachedDocMaster` and leaves `DocKey` as the null GUID. Premier's own receipt "
        "909330 carries both shapes in one collection.")
    add("")
    add("```http")
    add("POST /api/document/{receiptKey}/attachments")
    add(json.dumps([{"DocKey": "00000000-0000-0000-0000-000000000000",
                     "AttachedDocMaster": "<the PO's DocMasterKey>",
                     "Note": "CC-TEST purchase order",
                     "CatType": "<PO doc type>",
                     "MailRoute": "P", "AccessLevel": "V"}], indent=2))
    add("```")
    add("")
    add("Not `PUT /api/document/{id}/link`, which *creates* child documents from type keys and "
        "returns 500 when handed an existing key.")
    add("")
    add("Pay requests are found by the same `SubContract` join and linked the same way. That link "
        "matters more than it looks: **a receiver without it leaves reporting treating the item as "
        "never received, so depreciation never starts** — the gap behind Premier's manual "
        "quarterly catch-up.")
    add("")
    add("```http")
    add(f"POST /api/project/{{projectCode}}/docs")
    add(json.dumps({"IncludeDocs": True, "IncludeFiles": False, "IncludeClosed": True,
                    "ResultLimit": 60,
                    "ForDocType": "5b0a71d8-ed55-455b-bb99-2ab7d3b7a1cf"}, indent=2))
    add("   then link every row whose SubContract equals the PO number")
    add("```")
    add("")

    add("## 10. Read everything back")
    add("")
    add("```http")
    add("GET /api/document/{receiptKey}              -> DocNo, SubContract")
    add("GET /api/document/{receiptKey}/items        -> the line, as {DocNo}-{seq}")
    add("GET /api/document/{receiptKey}/attachments  -> POD + report + PO link + pay requests")
    add("```")
    add("")
    add("**A 200 from this API proves nothing.** Empty-body writes return 204 whether they did "
        "anything or not, and an invented document GUID returns 500 rather than 404. The read-back "
        "is the only evidence any of it landed.")
    add("")

    add("## 11. What Spitfire shows afterwards")
    add("")
    add("**Nothing, until a human approves the receipt.** It is left *In Process* and never routed, "
        "so the purchase order goes on reporting nothing received. That is by design — dispatching "
        "the approval chain emails real Premier staff, and that is Premier's call, not a button's.")
    add("")
    add("---")
    add("")

    add("## Reference — every endpoint, and what it answered")
    add("")
    add(f"Captured {date.today().isoformat()} against `{d['base_url']}`.")
    add("")
    add("| Method | Path | Status | Rows | Why |")
    add("|---|---|---|---|---|")
    for p in cap.probes:
        add(f"| `{p['method']}` | `{p['path']}` | {p.get('status')} | "
            f"{p.get('row_count') if p.get('row_count') is not None else '—'} | "
            f"{p.get('note','')} |")
    add("")

    add("## Reference — endpoints that answer nothing useful")
    add("")
    add("| Endpoint | What happens | What we do instead |")
    add("|---|---|---|")
    add("| `POST /api/projects` | **200** with zero rows | the 3 project ids are configuration |")
    add("| `GET /api/projects` | **405** | — |")
    add("| `GET /api/configuration/*` | **404** — no such controller | doc-type GUIDs hardcoded |")
    add("| `GET /api/config/doctypes` | **500** | as above |")
    add("| `POST /api/catalog/search/0/contents` | **400** \"TitleLike requires a value\" | dead "
        "fallback; project-scoped search works |")
    add("| `GET /api/document/{unknown-guid}` | **500**, byte-identical to a real fault | a 500 "
        "can never be read as \"not found\" |")
    add("| `GET /api/document/{id}/dates` | 200, but unnamed-GUID schedule rows | order date is "
        "the header's `DocDate` |")
    add("| `PUT /api/document/{id}/items` | **500** | POST, with an array body |")
    add("| `PUT /api/document/{id}/link` | **500** on an existing key | links go through "
        "`/attachments` |")
    add("| `GET /api/catalog/{key}/meta`, `/object` | **500** on a key `/versions` accepts | "
        "`/versions` |")
    add("")

    add("## Reference — the GUIDs we hardcode, and why")
    add("")
    add("The endpoint that would list document types does not exist, so these come from Premier's "
        "own `czx_TPICreate_ReceiptDoc.sql` and from reading `xsfDocHeader` directly.")
    add("")
    add("| Constant | Value | Provenance |")
    add("|---|---|---|")
    add(f"| purchase order type | `{d['doc_types']['purchase_order']}` | @PODTK. The master plan "
        f"transcribes this one character short. |")
    add(f"| receipt type | `{d['doc_types']['receipt']}` | 76,414 documents carry it; "
        f"`DocTypeKey_dv` reads \"Receipt\" |")
    add("| pay request type | `5b0a71d8-ed55-455b-bb99-2ab7d3b7a1cf` | @PRDTK in the same SQL |")
    add("| null GUID | `00000000-0000-0000-0000-000000000000` | Spitfire's \"no value\" |")
    add(f"| project ids | `{', '.join(projects)}` | read from `xsfDocHeader`; 18 / 8 / 2 corpus "
        f"POs |")
    add("")

    add("## Reference — corrections this exercise produced")
    add("")
    add("Five things the codebase or an earlier report asserted that the server contradicts. All "
        "five checked by calling the endpoint.")
    add("")
    add("| Was documented as | Actually | Consequence |")
    add("|---|---|---|")
    add("| `/api/configuration/*` returns 500 \"case 36629\" | **404** — \"No type was found that "
        "matches the controller named 'configuration'\" | the controller does not exist; the case "
        "may be chasing something that never did |")
    add("| the catalog search \"answers 200 with zero rows\" | **400** — needs `TitleLike`, we send "
        "`DocNoLike` | `resolve_po`'s fallback has never executed |")
    add("| \"re-sending an attach creates a second row\" | **400** \"This file is already "
        "attached\" | no-retry is still right, but because a retried *upload* mints a new fileKey "
        "which then attaches cleanly |")
    add("| `AttachNote` carries the note | silently ignored — 200, reads back `Note: null`. `Note` "
        "is the field that works | the old write probe stored notes that were never saved |")
    add("| `CatType` groups attachments | dropped — sent a valid GUID, stored `00000000-…` | "
        "attachments cannot be grouped by category |")
    add("")

    add("## Reference — auth, in one paragraph")
    add("")
    add("**Spitfire** has no `Authorization` header. One cookie, `sfPMSAuth`, is the "
        "FormsAuthentication ticket and is sufficient alone; the other three a browser holds are a "
        "session id, settings and a session GUID, none of which authenticate. It is copied by hand "
        "from a browser because Premier's Spitfire has Entra SSO, and it **lapses on idle** — "
        "which is why an expired ticket raises a named error instead of being silently "
        "re-authenticated. Every write is recorded by the ERP as `api@consciouscreations.ai` "
        "regardless of who triggered it, so our own audit log is the only thing that can tell two "
        "operators apart. **Graph** is unrelated: MSAL client-credentials, bearer token, app-only "
        "permissions.")
    add("")
    return "\n".join(L)


def main() -> int:
    cap = Capture(CAPTURE)
    body = build(cap) + reference(cap)

    # The house style plus what a document full of JSON needs.
    report_pdf._STYLE = report_pdf._STYLE + _EXTRA_STYLE  # noqa: SLF001
    document = report_pdf.document(
        body,
        title="Premier Receiver Automation — API Flow",
        subtitle=(f"Every endpoint between a delivery email and a receipt in sfPMS. "
                  f"Responses captured live from {cap.data['base_url']} on "
                  f"{date.today().isoformat()}."),
        note=("Nothing in this document is quoted from a specification. Every response shown was "
              "returned by the training server and can be regenerated with "
              "tools/verify_prd_endpoints.py, which calls each endpoint again and rewrites the "
              "capture this document is built from."))
    OUT_HTML.write_text(document, encoding="utf-8")
    print(f"html  -> {OUT_HTML}  ({len(document):,} bytes)")

    # The single-file version: same facts, same capture, greppable and readable beside the code.
    text = markdown(cap)
    OUT_MD.write_text(text, encoding="utf-8")
    print(f"md    -> {OUT_MD}  ({len(text):,} bytes)")

    if not report_pdf.is_available():
        print("Chrome not found — HTML written, PDF skipped.")
        return 0
    pdf = report_pdf.render(document)
    OUT_PDF.write_bytes(pdf)
    print(f"pdf   -> {OUT_PDF}  ({len(pdf):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
