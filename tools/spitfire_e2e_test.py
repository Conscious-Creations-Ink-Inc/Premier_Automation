"""Prove the whole Spitfire receiver chain against TRAINING, one step at a time.

    python -m tools.spitfire_e2e_test --dry-run          # reads only; shows what it would create
    python -m tools.spitfire_e2e_test --i-understand-this-writes

Every result we have for the write path came from snippets typed by hand into a shell. That is
not evidence anyone can re-check, and it is not something a second person could reproduce. This
script is the repeatable version: it runs steps 2-12 in order, asserts each one, and prints a
pass/fail table plus every request it issued.

**This module writes to Spitfire. `connectors/spitfire.py` still does not.** The read-only
connector keeps its allowlist and its zero write methods, so the guarantee made to Premier —
"the connector cannot write" — stays literally true. This harness holds its own session and is
the only thing in the repo that can create a document.

Four guard rails, none of them optional:

* The base URL must contain `training`. Production is refused outright, not warned about.
* `--i-understand-this-writes` must be typed in full, or the run is a dry run.
* Everything created is named `CC-TEST ...`; the script refuses to create a document whose title
  does not start with that marker, so an artefact can never end up looking like real work.
* Before the route is dispatched, the route is read back and must contain **exactly one** routee
  whose UserKey is ours. Anything else aborts the dispatch. This is what stops a test from
  emailing Premier's expediting team, and it is checked, not assumed.

Nothing here sets a document's Status. Spitfire will accept `PATCH /Status -> "A"` and mark a
receipt POD Confirmed with no approval and no Fixed-Asset Accounting sign-off; the SoW keeps FAA
as the final gate, so status belongs to the humans on the route. The capability is deliberately
absent from this script.
"""

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from config import settings
from connectors import spitfire_auth

REPORTS_DIR = settings.BASE_DIR.parent / "dev_reports"

PO_DOC_TYPE = "ff1975fd-76de-486c-888b-54e8fcd880e0"
RECEIPT_DOC_TYPE = "0c9a537a-3c41-4d16-ab9f-130ef69ea6c8"
PAY_REQUEST_DOC_TYPE = "5b0a71d8-ed55-455b-bb99-2ab7d3b7a1cf"
EMPTY_GUID = "00000000-0000-0000-0000-000000000000"

TEST_MARKER = "CC-TEST"
"""Every artefact carries this. A person opening one in the Spitfire UI with no context should
know inside a second that it is ours and not real."""

# PO 906577 line 0002 (LT-13) is ordered 11 / received 8, so three units are genuinely
# outstanding. A partially-received line is the honest test: receiving against a line that is
# already complete would exercise the over-receipt path and prove nothing about the normal one.
DEFAULT_PROJECT = "PRJ001PB100003"
DEFAULT_PO = "906577"

_HTML_TAG = re.compile(r"<[^>]+>")


def _strip(value: Any) -> str:
    return re.sub(r"\s+", " ", _HTML_TAG.sub(" ", str(value or ""))).strip()


@dataclass
class Step:
    number: str
    name: str
    ok: Optional[bool] = None     # None == skipped
    detail: str = ""

    @property
    def mark(self) -> str:
        return "SKIP" if self.ok is None else ("PASS" if self.ok else "FAIL")


@dataclass
class Run:
    steps: List[Step] = field(default_factory=list)
    requests_made: List[str] = field(default_factory=list)
    artefacts: Dict[str, str] = field(default_factory=dict)

    def step(self, number: str, name: str) -> Step:
        s = Step(number, name)
        self.steps.append(s)
        return s

    @property
    def failed(self) -> int:
        return sum(1 for s in self.steps if s.ok is False)


class Harness:
    def __init__(self, base_url: str, cookie: str, write: bool, run: Run):
        self.base_url = base_url.rstrip("/")
        self.write = write
        self.run = run
        self.session = requests.Session()
        self.session.cookies.set("sfPMSAuth", cookie,
                                 domain=requests.utils.urlparse(self.base_url).hostname, path="/")
        self.session.headers.update({"accept": "application/json"})
        self.user_key: Optional[str] = None

    def call(self, method: str, path: str, **kwargs) -> requests.Response:
        started = time.monotonic()
        r = self.session.request(method, self.base_url + path, timeout=120, **kwargs)
        self.run.requests_made.append(
            f"{method.upper()} {path} -> {r.status_code} ({int((time.monotonic()-started)*1000)} ms)")
        return r

    def get(self, path: str) -> requests.Response:
        return self.call("GET", path)

    def post(self, path: str, payload: Any = None, **kwargs) -> requests.Response:
        if payload is not None:
            kwargs["json"] = payload
        return self.call("POST", path, **kwargs)

    def json_or_none(self, r: requests.Response) -> Any:
        if not (r.ok and (r.text or "").strip()):
            return None
        try:
            return r.json()
        except ValueError:
            return None

    # --- guarded writes ---------------------------------------------------

    def write_call(self, step: Step, method: str, path: str, payload: Any = None, **kwargs):
        """Every write funnels through here so `--dry-run` is airtight rather than remembered
        at each call site."""
        if not self.write:
            step.detail = f"dry run - would {method.upper()} {path}"
            step.ok = None
            return None
        if payload is not None:
            kwargs["json"] = payload
        return self.call(method, path, **kwargs)


def preflight(h: Harness, run: Run) -> bool:
    s = run.step("0", "identity and instance check")
    if "training" not in h.base_url.lower():
        s.ok = False
        s.detail = f"REFUSED - base url is not a training instance: {h.base_url}"
        return False
    who = h.json_or_none(h.get("/api/session/who")) or {}
    if not who.get("UserKey"):
        s.ok = False
        s.detail = "no live session - the sfPMSAuth cookie is expired or rejected"
        return False
    h.user_key = who["UserKey"]
    s.ok = True
    s.detail = f"{who.get('FullName')} ({h.user_key}) on {h.base_url}"
    return True


def find_po(h: Harness, run: Run, project: str, po_number: str) -> Optional[dict]:
    s = run.step("3", f"find PO {po_number} in {project}")
    rows = h.json_or_none(h.post(f"/api/project/{project}/docs", {
        "ForDocType": PO_DOC_TYPE, "DocNoLike": po_number, "IncludeClosed": True,
    })) or []
    match = next((x for x in rows if str(x.get("DocNo") or "").strip() == po_number), None)
    if not match:
        s.ok = False
        s.detail = f"not found ({len(rows)} rows returned)"
        return None
    s.ok = True
    s.detail = f"DocMasterKey {match['DocMasterKey']}"
    return match


def pick_line(h: Harness, run: Run, po_key: str) -> Optional[dict]:
    """A receivable line with something still outstanding.

    Tax and freight are excluded on AccountCategory, not on description: the categories are the
    only reliable discriminator, and on some POs 40% of lines are one or the other.
    """
    s = run.step("4", "read PO lines and pick a receivable one")
    items = h.json_or_none(h.get(f"/api/document/{po_key}/items")) or []
    best = None
    for it in items:
        task = (it.get("DocItemTask") or [{}])[0]
        if str(task.get("AccountCategory") or "").upper().startswith(("TAX-", "FRT-")):
            continue
        rel = it.get("RelatedLineDetails") or {}
        outstanding = (float(rel.get("ContractUnits") or 0)
                       - float(rel.get("ReceivedUnits") or 0)
                       - float(rel.get("ReceiptInProgressUnits") or 0))
        if outstanding > 0:
            best = {"item": it, "task": task, "rel": rel, "outstanding": outstanding}
            break
    if not best:
        s.ok = False
        s.detail = f"no line with outstanding quantity among {len(items)}"
        return None
    it, rel = best["item"], best["rel"]
    s.ok = True
    s.detail = (f"line {it.get('DocItemNumber')} {it.get('SourceItemNumber')} - "
                f"ordered {rel.get('ContractUnits')}, received {rel.get('ReceivedUnits')}, "
                f"outstanding {best['outstanding']:g}")
    return best


def read_addresses(h: Harness, run: Run, po_key: str) -> dict:
    s = run.step("5", "read vendor and ship-to")
    rows = h.json_or_none(h.get(f"/api/document/{po_key}/addresses")) or []
    by_type = {str(a.get("AddrType") or "").upper(): a for a in rows if isinstance(a, dict)}
    vendor = by_type.get("T") or {}
    s.ok = bool(rows)
    s.detail = f"{len(rows)} address(es); vendor={_strip(vendor.get('Company')) or '(none)'}"
    return {"vendor": vendor, "ship_to": by_type.get("S") or {}}


def upload_pod(h: Harness, run: Run, stamp: str) -> Optional[str]:
    """`fileMeta` + `file`, and the date fields are mandatory.

    The failure ladder is worth keeping in mind when this breaks: no file gives 415; a missing
    `fileMeta` part gives 400 "Meta data missing"; a lowercase `filemeta` gives the same 400
    because the part name is case-sensitive; and `fileMeta` without dates gives
    500 "Nullable object must have a value" from a dereferenced nullable DateTime.
    """
    s = run.step("7", "upload POD to the catalog")
    name = f"ConsciousCreations.Testing.{stamp}.txt"
    body = (f"{TEST_MARKER} - receiver automation end-to-end check.\r\n"
            f"Generated {stamp}. Contains no business data. Safe to delete.\r\n").encode()
    meta = {
        "value": name, "Name": name, "type": "file", "FileType": "txt",
        "size": len(body), "MD5": hashlib.md5(body).hexdigest().upper(),
        "date": f"{stamp}T00:00:00", "DocDate": f"{stamp}T00:00:00",
        "ReferenceDate": f"{stamp}T00:00:00", "Due": f"{stamp}T00:00:00",
        "Keywords": f"{TEST_MARKER} automation",
    }
    r = h.write_call(s, "POST", "/api/catalog/upload?xm=catalog", files={
        "fileMeta": (None, json.dumps(meta), "application/json"),
        "file": (name, body, "text/plain"),
    })
    if r is None:
        return None
    payload = h.json_or_none(r) or {}
    key = payload.get("key")
    s.ok = bool(key)
    s.detail = f"{payload.get('progress')} fileKey={key}" if key else f"{r.status_code} {r.text[:120]}"
    if key:
        h.run.artefacts["catalog file"] = f"{key}  ({name})"
        # The server recomputes the hash; if it disagrees with ours the upload is not what we sent.
        versions = h.json_or_none(h.get(f"/api/catalog/{key}/versions")) or []
        server_hash = (versions[0] or {}).get("DataHash") if versions else None
        if server_hash and server_hash != meta["MD5"]:
            s.ok = False
            s.detail += f" - HASH MISMATCH server={server_hash} local={meta['MD5']}"
    return key


def create_receipt(h: Harness, run: Run, project: str, po_number: str, stamp: str) -> Optional[str]:
    s = run.step("8", "create the receipt document")
    path = (f"/api/document/{EMPTY_GUID}/{RECEIPT_DOC_TYPE}"
            f"?forProject={project}&forBatch={po_number}")
    r = h.write_call(s, "POST", path)
    if r is None:
        return None
    key = h.json_or_none(r)
    if not (isinstance(key, str) and len(key) == 36):
        s.ok = False
        s.detail = f"{r.status_code} {r.text[:150]}"
        return None
    h.run.artefacts["receipt document"] = key
    title = f"{TEST_MARKER} - receiver automation {stamp} - DO NOT PROCESS"
    if not title.startswith(TEST_MARKER):                      # belt and braces; see module docstring
        raise RuntimeError("refusing to title a document without the test marker")
    h.call("PATCH", f"/api/document/{key}/Title", json=title)
    header = h.json_or_none(h.get(f"/api/document/{key}")) or {}
    s.ok = str(header.get("SubContract") or "").strip() == po_number
    s.detail = (f"{key} DocNo={header.get('DocNo')} SubContract={header.get('SubContract')} "
                f"Status={header.get('Status_dv')}")
    if not s.ok:
        s.detail += "  <- forBatch did not set SubContract, so the PO link is missing"
    return key


def set_line_quantity(h: Harness, run: Run, receipt_key: str, picked: dict, qty: float) -> bool:
    """Write the quantity onto the line Spitfire already built against this PO line.

    Was `add_line`, which did `POST /items` and called the step a success if the document came back
    with any rows at all. Both halves of that were wrong, and together they are why this harness
    reported a working chain for weeks while nothing was ever counted:

    * `POST /items` **appends** a row. `SCDocItemKey`, `Subcontract`, `ProjEntity`,
      `AccountCategory`, `UOM` and the task quantity are all discarded on insert, leaving a line
      attached to no purchase order line at all.
    * Creating a receipt with `forBatch` already builds one item per PO line, correctly linked,
      with only the quantity blank. The rows this step used to count were those — they would have
      been there whether the POST had happened or not.

    See `connectors.spitfire_write.set_line_quantity`, and
    `dev_reports/2026-08-22-session-changes-verified.md` for the measurements.
    """
    s = run.step("9", "set the quantity on the receipt line")
    it = picked["item"]
    line_key = str((it.get("RelatedLineDetails") or {}).get("SCDocItemKey")
                   or it.get("DocItemKey") or "")

    rows = h.json_or_none(h.get(f"/api/document/{receipt_key}/items")) or []
    target = next((row for row in rows
                   if str((row.get("RelatedLineDetails") or {}).get("SCDocItemKey") or "").lower()
                   == line_key.lower()), None)
    if target is None:
        s.ok = False
        s.detail = f"the receipt carries no line against PO line {line_key[:8]}"
        return False

    task_key = str(((target.get("DocItemTask") or [{}])[0]).get("ItemTaskKey") or "")
    if not task_key:
        s.ok = False
        s.detail = f"line {target.get('DocItemNumber')} has no ItemTaskKey"
        return False

    # Release the session left open by creating and titling the document. A change staged into an
    # inherited session is silently discarded — every call still answers 200.
    opened = h.json_or_none(h.get(f"/api/document/{receipt_key}/session"))
    if isinstance(opened, str) and opened.strip():
        h.write_call(s, "DELETE", f"/api/document/{receipt_key}/session?sessionID={opened}")

    session = h.json_or_none(h.get(f"/api/document/{receipt_key}/session?freshenData=true"))
    if not (isinstance(session, str) and session.strip()):
        s.ok = False
        s.detail = f"no edit session: {str(session)[:80]!r}"
        return False

    change = [{"DataMember": "DocItemTask", "DataField": "Quantity",
               "InstanceKey": task_key, "Data": f"{qty:g}", "IsURIEncoded": False}]
    r = h.write_call(s, "PATCH", f"/api/document/{receipt_key}/session/changes", change)
    if r is None:
        return False
    # DELETE is what commits. POST /session/end answers 200 and does not.
    h.write_call(s, "DELETE", f"/api/document/{receipt_key}/session?sessionID={session}")

    # Read back only now, after the session is released — earlier returns the pre-change value.
    after = h.json_or_none(h.get(f"/api/document/{receipt_key}/items")) or []
    written = next((float(((row.get("DocItemTask") or [{}])[0]).get("Quantity") or 0.0)
                    for row in after
                    if row.get("DocItemNumber") == target.get("DocItemNumber")), 0.0)
    s.ok = abs(written - qty) < 0.001
    s.detail = (f"line {target.get('DocItemNumber')} "
                f"{target.get('SourceItemNumber')} qty={written:g} (wanted {qty:g})")
    return s.ok


def attach_pod(h: Harness, run: Run, receipt_key: str, file_key: str) -> bool:
    s = run.step("10", "attach the POD to the receipt")
    payload = [{"DocKey": file_key, "Note": f"{TEST_MARKER} automation check",
                "CatType": RECEIPT_DOC_TYPE, "MailRoute": "P", "AccessLevel": "V"}]
    r = h.write_call(s, "POST", f"/api/document/{receipt_key}/attachments", payload)
    if r is None:
        return False
    rows = h.json_or_none(h.get(f"/api/document/{receipt_key}/attachments")) or []
    s.ok = any(str(a.get("DocKey")).lower() == file_key.lower() for a in rows)
    s.detail = f"{len(rows)} attachment(s)" if s.ok else f"{r.status_code} {r.text[:120]}"
    return s.ok


def link_pay_request(h: Harness, run: Run, project: str, po_number: str, receipt_key: str) -> bool:
    """The pay-request link. Never exercised before this script existed.

    The SoW is explicit that a receiver without the pay-request link leaves reporting treating the
    item as never received, so depreciation never starts — the exact gap behind Premier's manual
    quarterly catch-up. `SubContract` is the shared field: commitment, pay request, CCO and receipt
    all carry the PO number in it, which is what makes the pay request findable at all.

    **Not `PUT /link`.** That endpoint *"creates and links child doc(s)"* — it makes new documents
    from type keys, so handing it an existing DocMasterKey returns 500. Premier's own receipts link
    documents through the *attachments* collection, which carries two different shapes:

        file link  ->  DocKey = <fileKey>,  AttachedDocMaster = null
        doc  link  ->  DocKey = null,       AttachedDocMaster = <DocMasterKey>

    Receipt 909330 has three doc links (its PO and two pay requests) and one file link (the POD),
    all in the same collection, distinguished only by which of those two fields is populated.
    """
    s = run.step("11", "find and link the pay request")
    rows = h.json_or_none(h.post(f"/api/project/{project}/docs", {
        "ForDocType": PAY_REQUEST_DOC_TYPE, "IncludeClosed": True,
    })) or []
    matches = [x for x in rows if str(x.get("SubContract") or "").strip() == po_number]
    if not matches:
        s.ok = None
        s.detail = f"no pay request on PO {po_number} ({len(rows)} on the project) - nothing to link"
        return False
    target = matches[0]["DocMasterKey"]
    r = h.write_call(s, "POST", f"/api/document/{receipt_key}/attachments", [{
        "AttachedDocMaster": target, "DocKey": EMPTY_GUID,
        "CatType": PAY_REQUEST_DOC_TYPE, "Note": f"{TEST_MARKER} pay request link",
    }])
    if r is None:
        return False
    linked = [a for a in (h.json_or_none(h.get(f"/api/document/{receipt_key}/attachments")) or [])
              if str(a.get("AttachedDocMaster") or "").lower() == target.lower()]
    s.ok = bool(linked)
    s.detail = (f"linked pay request {matches[0].get('DocNo')} ({target[:8]}…)"
                if linked else f"{r.status_code} {r.text[:150]}")
    return bool(linked)


def route_to_self_and_dispatch(h: Harness, run: Run, receipt_key: str) -> bool:
    """Dispatch the document, to ourselves and only ourselves.

    `route/apply` is the one call in the chain that reaches people rather than data — on a real
    Premier receipt the route names an expeditor, a contract administrator and an FAA manager. So
    the route is replaced with a single entry pointing at this account and **read back and
    verified** before anything is dispatched. If the read-back shows anything other than exactly
    one routee that is us — the clear silently failed, or Spitfire re-applied a configured default
    on save — the dispatch is abandoned. Treating that check as advisory would defeat the point of
    having it.
    """
    setup = run.step("12a", "remove every routee who is not us")

    # Creating a receipt makes Spitfire apply its *configured* approval chain, so the document
    # arrives with a route already on it — on PO 906577 that was six entries, three of them real
    # Premier staff at sequence 10. `DELETE /route` takes a **body**: the RouteIDs to remove.
    # Called without one it deletes nothing and returns 200, which is exactly how the first run of
    # this script ended up with a route it thought it had cleared.
    existing = h.json_or_none(h.get(f"/api/document/{receipt_key}/route")) or []
    strangers = [x for x in existing
                 if str(x.get("UserKey") or "").lower() != str(h.user_key).lower()]
    if strangers:
        h.write_call(setup, "DELETE", f"/api/document/{receipt_key}/route",
                     [x.get("RouteID") for x in strangers if x.get("RouteID")])

    routees = h.json_or_none(h.get(f"/api/document/{receipt_key}/route")) or []
    others = [x for x in routees
              if str(x.get("UserKey") or "").lower() != str(h.user_key).lower()]

    # The safety property is "nobody else is on this route", not "there is exactly one entry".
    # Spitfire legitimately puts us on it several times at different sequences, and demanding a
    # single entry would fail a route that is already perfectly safe.
    setup.ok = bool(routees) and not others
    setup.detail = f"{len(routees)} routee(s), {len(others)} not us: " + ", ".join(
        sorted({str(x.get("UserName")) for x in (others or routees)}))

    dispatch = run.step("12b", "dispatch the route (route/apply)")
    if not setup.ok:
        dispatch.ok = None
        dispatch.detail = ("ABORTED - route still contains people who are not us. Dispatching "
                           "would notify Premier staff. This is the guard doing its job.")
        return False

    # `effectList` is a required *query* parameter, not a body:
    #   EndRoute ; (APPEND | RESET) auto|key|name ; CCToLast ; NoChange
    # NoChange dispatches the route exactly as it stands, which is the only option safe here —
    # anything that re-applies a configured route would put the strangers straight back.
    r = h.write_call(dispatch, "POST",
                     f"/api/document/{receipt_key}/route/apply?effectList=NoChange")
    if r is None:
        return False
    after = h.json_or_none(h.get(f"/api/document/{receipt_key}/route")) or []
    marked = [x for x in after if x.get("Alerted") or x.get("Reached")]
    strangers_after = [x for x in after
                       if str(x.get("UserKey") or "").lower() != str(h.user_key).lower()]
    dispatch.ok = r.ok and not strangers_after
    dispatch.detail = (f"HTTP {r.status_code}; {len(marked)}/{len(after)} routee(s) alerted/reached"
                       + ("; STRANGERS REAPPEARED" if strangers_after else "")
                       ) if r.ok else f"{r.status_code} {r.text[:150]}"
    return bool(dispatch.ok)


def write_report(run: Run, stamp: str, args) -> str:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"Spitfire_E2E_Test_{stamp}.md"
    lines = [
        f"# Spitfire end-to-end write test - {stamp}", "",
        f"- Instance: `{settings.SPITFIRE_BASE_URL}`",
        f"- Mode: **{'WRITE' if args.i_understand_this_writes else 'dry run'}**",
        f"- PO: `{args.po}` in project `{args.project}`",
        "", "## Steps", "",
        "| # | Step | Result | Detail |", "|---|---|---|---|",
    ]
    for s in run.steps:
        lines.append(f"| {s.number} | {s.name} | **{s.mark}** | {s.detail} |")
    if run.artefacts:
        lines += ["", "## Artefacts created", "",
                  "All carry the `CC-TEST` marker. Left in place deliberately; remove when done.",
                  "", "| What | Key |", "|---|---|"]
        lines += [f"| {k} | `{v}` |" for k, v in run.artefacts.items()]
    lines += ["", "## Requests issued", "", "```"] + run.requests_made + ["```", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--project", default=DEFAULT_PROJECT)
    p.add_argument("--po", default=DEFAULT_PO)
    p.add_argument("--qty", type=float, default=1.0, help="quantity to receive (default 1)")
    p.add_argument("--dry-run", action="store_true", help="read everything, create nothing")
    p.add_argument("--i-understand-this-writes", action="store_true",
                   help="required to create anything in Spitfire")
    args = p.parse_args(argv)

    cookie = spitfire_auth.auth_ticket_value(settings.SPITFIRE_BASE_URL)
    if not cookie:
        print("No Spitfire credentials: set SPITFIRE_UID and SPITFIRE_PW in .env.", file=sys.stderr)
        return 2

    write = bool(args.i_understand_this_writes) and not args.dry_run
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    run = Run()
    h = Harness(settings.SPITFIRE_BASE_URL, cookie, write, run)

    print(f"mode: {'WRITE' if write else 'DRY RUN (nothing will be created)'}\n")

    if preflight(h, run):
        po = find_po(h, run, args.project, args.po)
        if po:
            po_key = po["DocMasterKey"]
            picked = pick_line(h, run, po_key)
            read_addresses(h, run, po_key)
            if picked:
                qty = min(args.qty, picked["outstanding"])
                file_key = upload_pod(h, run, stamp)
                receipt_key = create_receipt(h, run, args.project, args.po, stamp)
                if receipt_key:
                    set_line_quantity(h, run, receipt_key, picked, qty)
                    if file_key:
                        attach_pod(h, run, receipt_key, file_key)
                    link_pay_request(h, run, args.project, args.po, receipt_key)
                    route_to_self_and_dispatch(h, run, receipt_key)

    print(f"{'#':<5}{'STEP':<44}{'RESULT':<8}DETAIL")
    print("-" * 118)
    for s in run.steps:
        print(f"{s.number:<5}{s.name[:43]:<44}{s.mark:<8}{s.detail[:60]}")

    if run.artefacts:
        print("\nartefacts created (all marked CC-TEST, left in place):")
        for k, v in run.artefacts.items():
            print(f"  {k:<20} {v}")

    print(f"\nrequests issued: {len(run.requests_made)}")
    print(f"report: {write_report(run, datetime.now().strftime('%Y-%m-%d_%H%M%S'), args)}")
    return 1 if run.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
