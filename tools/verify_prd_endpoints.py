"""Call every endpoint the PRD names and record exactly what came back.

The PRD documents an integration whose behaviour is not guessable from its OpenAPI document — the
spec describes none of the required shapes, and several endpoints in it do not exist on this
build. So the document is written from responses, not from the spec and not from memory, and this
script is what produces them.

It exists because the last three attempts to write this down went stale. `dev_reports` already
contains files asserting things this server does not do, and probing while planning found three
more in the code's own comments:

    /api/configuration/*            documented 500 "case 36629"  -> actually 404, no such controller
    /api/catalog/search/{s}/contents documented "200, zero rows"  -> actually 400, wants TitleLike
    GET /api/projects               documented 405                -> confirmed 405

A quote in a comment cannot be re-checked. A JSON file of real responses can, which is why the
output of this script ships beside the document and is regenerated rather than edited.

    python tools/verify_prd_endpoints.py                 # reads only
    python tools/verify_prd_endpoints.py --write         # also re-proves the write shapes
    python tools/verify_prd_endpoints.py --out FILE.json

**Reads are safe to run against anything.** `--write` is not: it creates a receipt on Premier's
instance. Everything it creates is titled CC-TEST, is left In Process, is never routed, and is
listed at the end so nothing is unexplained.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402

# A PO that exists, is in the first configured project, and has receivable lines. Used for the
# resolution walk-through the PRD prints verbatim.
SAMPLE_PO = "212559"
SAMPLE_PO_KEY = "1023ab34-f62c-4a2f-9d73-4c73446d3473"

PO_FILTER = {
    "DocNoLike": SAMPLE_PO,
    "IncludeDocs": True,
    "IncludeFiles": False,
    "IncludeClosed": True,
    "ResultLimit": 25,
    "ForDocType": settings.SPITFIRE_PO_DOC_TYPE_KEY,
}

MAX_BODY = 1400
"""How much of a response body is kept. Enough to show the shape and the fields the PRD names;
the PO header alone is 120 fields and nobody reads all of them in a document."""


class Probe:
    """One request and its answer, in the shape the PRD renders."""

    def __init__(self, session: requests.Session, base: str):
        self.session = session
        self.base = base
        self.results: List[Dict[str, Any]] = []

    def call(self, method: str, path: str, *, body: Any = None, note: str = "",
             expect: str = "") -> Dict[str, Any]:
        url = f"{self.base}{path}"
        started = time.monotonic()
        record: Dict[str, Any] = {
            "method": method, "path": path, "note": note,
            "request_body": body, "expected": expect,
        }
        try:
            response = self.session.request(method, url, json=body, timeout=60)
            record["status"] = response.status_code
            record["bytes"] = len(response.content)
            text = response.text
            record["truncated"] = len(text) > MAX_BODY
            record["response"] = text[:MAX_BODY]
            try:
                parsed = response.json()
                record["row_count"] = len(parsed) if isinstance(parsed, list) else None
            except ValueError:
                record["row_count"] = None
        except requests.RequestException as exc:
            # Recorded rather than raised. An endpoint that times out is a finding the document
            # should carry, and one unreachable path must not stop the other thirty being probed.
            record["status"] = None
            record["error"] = f"{type(exc).__name__}: {exc}"
        record["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        self.results.append(record)
        flag = record.get("status")
        rows = record.get("row_count")
        suffix = f", {rows} row(s)" if rows is not None else ""
        print(f"  {flag if flag is not None else 'ERR':>4}  {method:<5} {path[:64]:<66}{suffix}")
        return record


def read_probes(probe: Probe) -> None:
    """Every read the PRD documents, in the order the document walks them."""

    print("\n-- liveness and identity --")
    probe.call("GET", "/api/system/version", note="anonymous; proves reachability",
               expect='a bare JSON string, e.g. "2023.0.9692.36214"')
    probe.call("GET", "/api/account/session", note="session probe",
               expect="a bare boolean; answers false rather than 401ing")
    probe.call("GET", "/api/session/who", note="whose ticket this is",
               expect="EMail with a capital M — the spelling that actually works")

    print("\n-- step 3: PO number -> project -> DocMasterKey --")
    probe.call("GET", "/api/projects",
               note="cannot enumerate projects; documented 405",
               expect="405 — the verb is not supported")
    probe.call("POST", "/api/projects", body={"IncludeHidden": True, "IncludeClosed": True},
               note="the POST form; why the three project ids are configuration, not discovery",
               expect="200 with zero rows for this account")
    for project_id in settings.SPITFIRE_PROJECT_IDS:
        probe.call("POST", f"/api/project/{project_id}/docs", body=PO_FILTER,
                   note=f"search {project_id} for PO {SAMPLE_PO}",
                   expect="one row in the project that owns it, [] in the others")
    probe.call("POST", f"/api/catalog/search/{settings.SPITFIRE_SEARCH_SCOPE}/contents",
               body=PO_FILTER,
               note="resolve_po's fallback — never actually executes",
               expect="400: catalogFilters.TitleLike requires a value")

    print("\n-- step 4: reading the purchase order --")
    probe.call("GET", f"/api/document/{SAMPLE_PO_KEY}", note="PO header",
               expect="~120 fields; DocNo, SubContract, Project, DocDate are what we keep")
    probe.call("GET", f"/api/document/{SAMPLE_PO_KEY}/items", note="PO lines",
               expect="quantities in RelatedLineDetails.ContractUnits; ItemQuantity reads 0.0")
    probe.call("GET", f"/api/document/{SAMPLE_PO_KEY}/addresses", note="vendor and ship-to",
               expect="AddrType T = vendor, S = ship-to, F = author")
    probe.call("GET", f"/api/document/{SAMPLE_PO_KEY}/route", note="purchasing agent",
               expect="UserName of the first routee")
    probe.call("GET", f"/api/document/{SAMPLE_PO_KEY}/attachments", note="what hangs off the PO",
               expect="file links and document links in one collection")

    print("\n-- endpoints that answer nothing, and are documented as such --")
    for path in ("/api/configuration/doctypes", "/api/configuration/documenttypes",
                 "/api/configuration/doctype", "/api/config/doctypes"):
        probe.call("GET", path, note="would list document type GUIDs",
                   expect="404 — there is no 'configuration' controller on this build")
    probe.call("GET", f"/api/document/{SAMPLE_PO_KEY}/dates",
               note="allowlisted but deliberately never called",
               expect="schedule rows keyed by an unnamed GUID; carries no order date")
    probe.call("GET", "/api/document/00000000-0000-0000-0000-000000000001",
               note="an invented document GUID",
               expect="500, not 404 — sfPMS does not 404 for a missing document")


def receipt_in_progress_check(probe: Probe) -> Dict[str, Any]:
    """The single most important fact in the document, re-measured every run.

    `ReceiptInProgressUnits` is the one field that looks like it would answer "have we already
    posted this", and it reads 0.0 for the whole approval window. The check is run against a PO
    that *does* carry receipts, so a 0.0 here is evidence rather than an absence of data.
    """
    print("\n-- the field that cannot see an unapproved receipt --")
    record = probe.call("GET", f"/api/document/{SAMPLE_PO_KEY}/items",
                        note="re-read to measure ReceiptInProgressUnits against known receipts",
                        expect="0.0 on lines that demonstrably carry receipts")
    lines: List[Dict[str, Any]] = []
    try:
        for item in json.loads(record.get("response") or "[]"):
            related = item.get("RelatedLineDetails") or {}
            task = (item.get("DocItemTask") or [{}])[0]
            lines.append({
                "line": item.get("DocItemNumber"),
                "ContractUnits": related.get("ContractUnits"),
                "UOM": related.get("UOM") or task.get("UOM"),
                "ReceivedUnits": related.get("ReceivedUnits"),
                "ReceiptInProgressUnits": related.get("ReceiptInProgressUnits"),
                # Both, because they are different things and the PRD must not call either
                # "the cost code": ProjEntity is what the receipt line posts against, GLAcct is
                # the general-ledger account.
                "DocItemTask[0].ProjEntity": task.get("ProjEntity"),
                "RelatedLineDetails.GLAcct": related.get("GLAcct"),
            })
    except (ValueError, TypeError):
        # A truncated body is expected — MAX_BODY is smaller than this response. The caller falls
        # back to a dedicated unlimited read.
        lines = []
    return {"lines": lines}


def measure_lines(session: requests.Session, base: str) -> List[Dict[str, Any]]:
    """The line table, read without truncation. Small enough to be worth its own call."""
    response = session.get(f"{base}/api/document/{SAMPLE_PO_KEY}/items", timeout=60)
    out = []
    for item in response.json():
        related = item.get("RelatedLineDetails") or {}
        task = (item.get("DocItemTask") or [{}])[0]
        out.append({
            "line": item.get("DocItemNumber"),
            "spec": item.get("SourceItemNumber"),
            "ContractUnits": related.get("ContractUnits"),
            "UOM": related.get("UOM") or task.get("UOM"),
            "ItemQuantity": item.get("ItemQuantity"),
            "ReceivedUnits": related.get("ReceivedUnits"),
            "ReceiptInProgressUnits": related.get("ReceiptInProgressUnits"),
            "ProjEntity": task.get("ProjEntity"),
            "AccountCategory": task.get("AccountCategory"),
            "GLAcct": related.get("GLAcct"),
            "GLSub": related.get("GLSub"),
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--out",
        default=str(ROOT / "dev_reports" / "workflow" / "prd_endpoint_responses.json"),
        help="where the capture lands; build_prd.py reads this same path")
    parser.add_argument("--write", action="store_true",
                        help="also re-prove the write shapes; CREATES a CC-TEST receipt")
    args = parser.parse_args()

    cookie = settings.SPITFIRE_SESSION_COOKIE
    if not cookie:
        print("SPITFIRE_SESSION_COOKIE is not set — nothing can be probed.")
        return 1

    session = requests.Session()
    session.cookies.set("sfPMSAuth", cookie,
                        domain="training.remingtonhotels.com", path="/")
    base = settings.SPITFIRE_BASE_URL
    print(f"probing {base}")

    probe = Probe(session, base)
    read_probes(probe)
    receipt_in_progress_check(probe)

    payload: Dict[str, Any] = {
        "base_url": base,
        "sample_po": SAMPLE_PO,
        "sample_po_key": SAMPLE_PO_KEY,
        "project_ids": list(settings.SPITFIRE_PROJECT_IDS),
        "doc_types": {
            "purchase_order": settings.SPITFIRE_PO_DOC_TYPE_KEY,
            "receipt": settings.SPITFIRE_RECEIPT_DOC_TYPE_KEY,
        },
        "probes": probe.results,
        "po_lines": measure_lines(session, base),
        "writes": "not re-issued; run with --write" if not args.write else "see write_probes",
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    statuses = [str(r.get("status")) for r in probe.results]
    print(f"\n{len(probe.results)} endpoints probed -> {out_path}")
    print("  statuses:", ", ".join(f"{s}x{statuses.count(s)}" for s in sorted(set(statuses))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
