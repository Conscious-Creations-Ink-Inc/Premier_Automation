"""Probe the sfPMS write surface to learn what posting a POD, a receipt or a report requires.

The Swagger declares 232 write operations and documents almost nothing about what a body must
contain — no required-field lists that survive `allOf`/`oneOf`, no enums for any business field.
The only way to learn a requirement is to send a request and read the rejection. That is what this
does: it walks the write surface, records the exact body sent and the exact response, and builds
the validation ladder for each endpoint.

TRAINING INSTANCE ONLY. Never point this at production.

Blast radius is controlled three ways:

1. Every document-scoped write targets a receipt **this script creates**, titled with a `CC-TEST`
   marker. Nothing is written to a real Premier PO — the PO is only ever read.
2. `_DENY` refuses 15 operations that reach people, external parties or site-wide state. Two matter
   most: `route/apply` dispatches the approval chain and emails real Premier staff, and
   `supportcase` files a case with Spitfire Management. `PATCH /{id}/Status` is refused separately
   because setting it to "A" marks a receipt POD-Confirmed without any approval, bypassing the FAA
   gate that Premier's own receipts route through.
3. DELETE is allowed only against keys this run created, tracked in `self.mine`.

    python tools/spitfire_write_probe.py --i-understand-this-writes [--po 207030]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

ROOT = Path(__file__).resolve().parents[1]
MARKER = "CC-TEST"

# Operations refused outright. Substring match on the path, plus an explicit method where the
# same path is harmless for other verbs.
_DENY: Dict[str, str] = {
    "route/apply": "dispatches the approval chain — EMAILS REAL PREMIER STAFF",
    "route/perform": "asks the ATC service to perform route actions (emails, workflows)",
    "supportcase": "files a support case with Spitfire Management — EXTERNAL VENDOR",
    "system/configuration": "reloads site-wide configuration (3 real sessions active)",
    "users/idle": "prunes user sessions — would kick real users off",
    "/send": "sends email",
    "featuretracking": "changes account preferences",
    "/logout": "destroys the session ticket this run depends on",
    "oauthdata": "stores partner credentials in xtsConfig",
    "system/dev": "toggles server dev mode",
    "system/dri": "suppresses declarative referential integrity",
    "documents/trash": "recovers deleted documents",
    "xts/inbound": "injects an ERP peer push payload",
    "system/hms": "peer handshake",
    "obfuscate": "writes nothing observable; no requirement to learn",
    # Config writes whose GET is implemented: a blank insert would land in real site config.
    "csi-maintenance": "GET is implemented, so a write would insert a blank CSI code",
}


def deny_reason(method: str, path: str) -> Optional[str]:
    low = path.lower()
    for frag, why in _DENY.items():
        if frag in low:
            return why
    # Status is the approval-gate bypass. Premier's receipts must reach POD Confirmed via a route.
    if method == "PATCH" and re.search(r"/api/document/[^/]+/Status$", path, re.I):
        return "PATCH Status bypasses the FAA approval gate — forbidden by design"
    return None


def load_env() -> Dict[str, str]:
    txt = (ROOT / ".env").read_text(encoding="utf-8")
    return {k: v.strip() for k, v in re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", txt, re.M)}


class WriteProbe:
    def __init__(self, env: Dict[str, str], out: Path, timeout: float):
        self.base = env["SPITFIRE_BASE_URL"].strip().rstrip("/")
        self.timeout = timeout
        self.out = out
        (out / "bodies").mkdir(parents=True, exist_ok=True)
        self.s = requests.Session()
        self.s.cookies.set("sfPMSAuth", env["SPITFIRE_SESSION_COOKIE"].strip(),
                           domain="training.remingtonhotels.com", path="/")
        self.receipt_type = env["SPITFIRE_RECEIPT_DOC_TYPE_KEY"].strip()
        self.po_type = env["SPITFIRE_PO_DOC_TYPE_KEY"].strip()
        self.records: List[Dict[str, Any]] = []
        self.denied: List[Tuple[str, str, str]] = []
        self.mine: set = set()          # keys this run created — the only DELETE targets
        self.receipt_key: Optional[str] = None
        self.file_key: Optional[str] = None

    # ---------------------------------------------------------------- plumbing
    def call(self, method: str, path: str, *, body: Any = None, files: Any = None,
             note: str = "", phase: str = "") -> Dict[str, Any]:
        why = deny_reason(method, path)
        if why:
            self.denied.append((method, path, why))
            return {"status": None, "denied": why}

        url = self.base + path
        rec: Dict[str, Any] = {"phase": phase, "method": method, "path": path,
                               "sent": body if files is None else "<multipart>", "note": note}
        t0 = time.time()
        try:
            r = self.s.request(method, url, json=body if files is None else None,
                               files=files, timeout=self.timeout, allow_redirects=False)
        except requests.RequestException as exc:
            rec.update(status=None, error=f"{type(exc).__name__}: {exc}"[:200])
            self.records.append(rec)
            return rec

        rec["status"] = r.status_code
        rec["ms"] = int((time.time() - t0) * 1000)
        try:
            parsed = r.json()
        except ValueError:
            parsed = r.text
        rec["response"] = parsed
        # sfPMS puts its own faults in ThisReason; the ASP.NET layer uses Message.
        if isinstance(parsed, dict):
            rec["reason"] = parsed.get("ThisReason") or parsed.get("Message")
        rec["preview"] = (json.dumps(parsed, default=str) if not isinstance(parsed, str)
                          else parsed)[:400]
        self.records.append(rec)
        return rec

    def log(self, rec: Dict[str, Any], label: str) -> None:
        if rec.get("denied"):
            print(f"  DENY  {label}\n          {rec['denied']}", flush=True)
            return
        reason = rec.get("reason") or ""
        print(f"  {str(rec.get('status')):>4}  {label}"
              + (f"\n          {str(reason)[:150]}" if reason else ""), flush=True)

    # ------------------------------------------------------- phase 1: the chain
    def build_receipt(self, po_number: str, po_key: str, project: str) -> None:
        """The POD/receipt chain, end to end, on artifacts this run owns."""
        print(f"\n{'='*94}\nPHASE 1 — the POD/receipt chain (PO {po_number}, project {project})\n{'='*94}")

        # 1. upload a POD
        payload = f"{MARKER} write-probe POD {time.strftime('%Y-%m-%d %H:%M:%S')}\n".encode()
        import hashlib
        meta = {
            "value": f"{MARKER}.WriteProbe.txt", "Name": f"{MARKER}.WriteProbe.txt",
            "type": "file", "FileType": "txt", "size": len(payload),
            "MD5": hashlib.md5(payload).hexdigest().upper(),
            "date": "2026-08-13T00:00:00", "DocDate": "2026-08-13T00:00:00",
            "ReferenceDate": "2026-08-13T00:00:00", "Due": "2026-08-13T00:00:00",
        }
        r = self.call("POST", "/api/catalog/upload?xm=catalog", phase="chain",
                      files={"fileMeta": (None, json.dumps(meta), "application/json"),
                             "file": (meta["Name"], payload, "text/plain")},
                      note="upload the POD")
        self.log(r, "POST /api/catalog/upload?xm=catalog")
        if r.get("status") == 200 and isinstance(r.get("response"), dict):
            self.file_key = r["response"].get("key")
            self.mine.add(self.file_key)
            print(f"          fileKey={self.file_key}")

        # 2. create the receipt as a child of the PO
        r = self.call("POST", f"/api/document/00000000-0000-0000-0000-000000000000/"
                              f"{self.receipt_type}?forProject={project}&forBatch={po_number}",
                      phase="chain", note="create the receipt document")
        self.log(r, "POST /api/document/{nullGuid}/{receiptType}?forProject&forBatch")
        if r.get("status") == 200 and isinstance(r.get("response"), str):
            self.receipt_key = r["response"].strip('"')
            self.mine.add(self.receipt_key)
            print(f"          receiptKey={self.receipt_key}")
        if not self.receipt_key:
            print("  !! no receipt created — document-scoped probing will be skipped")
            return

        # 3. mark it immediately so a human can identify it
        r = self.call("PATCH", f"/api/document/{self.receipt_key}/Title", phase="chain",
                      body=f"{MARKER} - write probe 2026-08-13 - DO NOT APPROVE",
                      note="mark the document")
        self.log(r, "PATCH /api/document/{id}/Title")

        # 4. read back what creation produced (a route is applied automatically)
        for p, lbl in ((f"/api/document/{self.receipt_key}", "header"),
                       (f"/api/document/{self.receipt_key}/route", "auto-applied route")):
            resp = self.s.get(self.base + p, timeout=self.timeout)
            print(f"  READ  GET {p.split('/api')[1]} -> {resp.status_code} ({lbl})")
            if p.endswith("/route") and resp.status_code == 200:
                try:
                    for row in resp.json():
                        print(f"          routee seq={row.get('Sequence')} "
                              f"{row.get('UserName')!r} action={row.get('RouteAction')}")
                except ValueError:
                    pass

        # 5. add a receipt line — the shape the E2E test proved
        line = [{"DocItemNumber": "0001", "ItemQuantity": 1.0,
                 "Description": f"{MARKER} write probe line"}]
        r = self.call("POST", f"/api/document/{self.receipt_key}/items", phase="chain",
                      body=line, note="add a receipt line")
        self.log(r, "POST /api/document/{id}/items")

        # 6. attach the POD as a file link
        if self.file_key:
            att = [{"DocKey": self.file_key,
                    "AttachedDocMaster": "00000000-0000-0000-0000-000000000000",
                    "AttachNote": f"{MARKER} POD"}]
            r = self.call("POST", f"/api/document/{self.receipt_key}/attachments", phase="chain",
                          body=att, note="attach the POD (file link)")
            self.log(r, "POST /api/document/{id}/attachments   [file link]")

        # 7. link the PO as a document link — the other shape of the same collection
        docl = [{"DocKey": "00000000-0000-0000-0000-000000000000",
                 "AttachedDocMaster": po_key, "AttachNote": f"{MARKER} parent PO"}]
        r = self.call("POST", f"/api/document/{self.receipt_key}/attachments", phase="chain",
                      body=docl, note="link the PO (doc link)")
        self.log(r, "POST /api/document/{id}/attachments   [doc link]")

    # ------------------------------- phase 2: requirement probe, our doc only
    def probe_document_writes(self, spec: dict) -> None:
        if not self.receipt_key:
            return
        print(f"\n{'='*94}\nPHASE 2 — requirement probe on OUR receipt {self.receipt_key}\n"
              f"{'='*94}\nEmpty/minimal bodies. The rejection names the requirement.\n")
        W = ("POST", "PUT", "PATCH")
        for path, item in sorted(spec["paths"].items()):
            if not re.match(r"^/api/[Dd]ocument/\{id\}", path):
                continue
            for m, op in item.items():
                if m.upper() not in W:
                    continue
                filled, skip = self._fill(path)
                if skip:
                    continue
                body = [] if "items" in path or "attachments" in path else {}
                r = self.call(m.upper(), filled, body=body, phase="probe",
                              note=(op.get("summary") or "")[:110])
                self.log(r, f"{m.upper():<6}{path}")

    def _fill(self, path: str) -> Tuple[str, bool]:
        vals = {"id": self.receipt_key, "fieldName": "Title", "itemKey": "0",
                "key": "0", "fileKey": self.file_key or "0", "disposition": "0",
                "itemType": "0", "requestedMode": "0", "sessionID": "0",
                "attachmentKey": "0", "newItems": "1", "routeKey": "0",
                "scriptName": "none", "typeKey": self.receipt_type, "revKey": "0",
                "routeId": "0", "targetDMK": self.receipt_key or "0", "location": "0"}
        out = path
        for name in re.findall(r"\{([^}]+)\}", path):
            v = vals.get(name) or vals.get(name[0].lower() + name[1:])
            if v is None:
                return path, True
            out = out.replace("{" + name + "}", str(v))
        return out, False

    # --------------------------------------- phase 3: reports and export tasks
    def probe_reports(self) -> None:
        print(f"\n{'='*94}\nPHASE 3 — report / export / background-task endpoints\n{'='*94}")
        rk = self.receipt_key or "00000000-0000-0000-0000-000000000000"
        cases = [
            ("POST", f"/api/document/{rk}/attachments/assembled/0", {},
             "build a PDF of the assembled attachments — the receipt+POD output file"),
            ("POST", "/api/catalog/docs/pdf", {"DocKeys": [rk]},
             "build a PDF across a batch of documents"),
            ("POST", "/api/catalog/download/zip", {"DocKeys": [rk]}, "zip a document set"),
            ("POST", "/api/catalog/download/files", [], "zip a file set"),
            ("POST", "/api/excel/export/bfa", {}, "Excel export"),
            ("POST", "/api/excel/export/cobra", {}, "Cobra export"),
            ("POST", "/api/projects/executive", {}, "executive summary task"),
            ("POST", "/api/excel/import/contacts", {}, "contact import"),
        ]
        for m, p, b, note in cases:
            r = self.call(m, p, body=b, phase="report", note=note)
            self.log(r, f"{m:<6}{p}")
            # A task key means the work is queued; poll it so we learn the task contract.
            if r.get("status") in (200, 202) and isinstance(r.get("response"), str) \
                    and re.fullmatch(r"[0-9a-f-]{36}", r["response"].strip('"')):
                tid = r["response"].strip('"')
                time.sleep(1.5)
                st = self.s.get(f"{self.base}/api/session/task/{tid}/state", timeout=self.timeout)
                print(f"          task {tid} state -> {st.status_code} {st.text[:180]}")

    # ------------------------------------------- phase 4: config stubs, safely
    def probe_config(self, spec: dict) -> None:
        print(f"\n{'='*94}\nPHASE 4 — config writes (all have stubbed GETs, so nothing can land)\n{'='*94}")
        for path, item in sorted(spec["paths"].items()):
            if not path.startswith(("/api/configuration", "/api/config/")):
                continue
            for m, op in item.items():
                if m.upper() not in ("POST", "PATCH"):
                    continue
                if "{" in path:
                    continue
                r = self.call(m.upper(), path, body=[] if m.upper() == "POST" else {},
                              phase="config", note=(op.get("summary") or "")[:100])
                self.log(r, f"{m.upper():<6}{path}")

    # ------------------------------------------------------------------- misc
    def probe_other(self) -> None:
        print(f"\n{'='*94}\nPHASE 5 — other write surfaces (contact, alerts, comments, xts, session)\n{'='*94}")
        cases = [
            ("POST", "/api/contact", {}, "create a contact"),
            ("POST", "/api/Alerts", {}, "create an alert"),
            ("POST", "/api/comments/00000000-0000-0000-0000-000000000000", {}, "comment on a topic"),
            ("POST", "/api/session/log", {}, "log posted data"),
            ("POST", "/api/session/exchangetoken", {}, "mint an exchange token"),
            ("POST", "/api/session/permits", [], "batch permission demand"),
            ("POST", "/api/account/allows", {}, "authorization flags for a demand"),
            ("POST", "/api/account/downloadsession", {}, "mint a download-ticket cookie"),
            ("PUT", "/api/xts/queue/0/0", {}, "enqueue a peer task"),
            ("PUT", "/api/xts/map", {}, "update a key map"),
            ("POST", "/api/arr/open", {}, "open a route-response link"),
        ]
        for m, p, b, note in cases:
            r = self.call(m, p, body=b, phase="other", note=note)
            self.log(r, f"{m:<6}{p}")

    def save(self) -> None:
        (self.out / "write_probe.json").write_text(json.dumps(
            {"base": self.base, "receipt_key": self.receipt_key, "file_key": self.file_key,
             "created": sorted(self.mine), "records": self.records,
             "denied": [{"method": m, "path": p, "why": w} for m, p, w in self.denied]},
            indent=1, default=str), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--i-understand-this-writes", action="store_true", required=True,
                    help="required acknowledgement; this creates documents on training")
    ap.add_argument("--po", default="207030")
    ap.add_argument("--po-key", default="e2448751-9201-4cb9-8cf5-1000f3c303c7")
    ap.add_argument("--project", default="MRC024PB100003")
    ap.add_argument("--timeout", type=float, default=45.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    env = load_env()
    out = Path(args.out) if args.out else ROOT.parent / "dev_reports" / "spitfire_write_probe"
    out.mkdir(parents=True, exist_ok=True)
    spec = requests.get(f"{env['SPITFIRE_BASE_URL'].strip().rstrip('/')}"
                        f"/swagger/v23/swagger.json", timeout=60).json()

    p = WriteProbe(env, out, args.timeout)
    who = p.s.get(f"{p.base}/api/session/who", timeout=args.timeout)
    if who.status_code != 200:
        print(f"session/who -> {who.status_code}; recapture sfPMSAuth first")
        return 1
    print(f"authenticated as {who.json().get('FullName')} on {p.base}")

    p.build_receipt(args.po, args.po_key, args.project)
    p.probe_document_writes(spec)
    p.probe_reports()
    p.probe_config(spec)
    p.probe_other()
    p.save()

    import collections
    mix = collections.Counter(r.get("status") for r in p.records)
    print(f"\n{'='*94}\n{len(p.records)} write calls issued · {len(p.denied)} refused")
    print("status mix:", dict(sorted(mix.items(), key=lambda x: str(x[0]))))
    print(f"created: {sorted(p.mine)}")
    print(f"detail -> {out / 'write_probe.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
