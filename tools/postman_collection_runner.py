"""Run a Postman collection against sfPMS and record the exact request and response.

Answers "what do the endpoints in dev_reports/postman actually return", which neither the
collections nor the runbook can: the runbook's status codes were written against sfPMS
2023.0.9692.36214 and the host has since moved on, so every documented result is a claim
until it is re-fired.

Not a Postman clone. It resolves {{variables}}, walks the folders in order, and emulates only
the parts of each Tests script that later requests depend on (the captured docKey, poDmk,
receiptDmk, poItemNumbers and the generated quantity-patch body). Assertions are not
re-implemented -- the response body is recorded verbatim and can be judged afterwards.

WRITES ARE OFF BY DEFAULT. `_is_readable` mirrors tools/spitfire_capture_responses.py: GET
plus the read-shaped POSTs sfPMS uses for search. Anything else needs --allow-writes. Two
paths are denied by name and cannot be reached even then:

  /logout      both are GETs, and either destroys the hand-captured ticket this depends on
  route/apply  creating a receipt auto-stages three real Premier employees on its route;
               this is the call that would email them

    python tools/postman_collection_runner.py --phase reads
    python tools/postman_collection_runner.py --phase all --allow-writes
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
POSTMAN = ROOT.parent / "dev_reports" / "postman"

RECEIVER_COLLECTION = POSTMAN / "Spitfire_postman_collection" / "Spitfire_Receiver_GETs.postman_collection.json"
RECEIVER_ENV = POSTMAN / "Spitfire_postman_collection" / "Spitfire_Training.postman_environment.json"
RECEIPT_COLLECTION = POSTMAN / "sfPMS-PO-Receipt.postman_collection.json"
RECEIPT_ENV = POSTMAN / "Spitfire_postman_collection" / "sfPMS — PPM Sandbox.postman_environment.json"

# GETs that mutate, and the one call that notifies humans. Denied by name, always.
DENY_SUBSTRINGS = ("/logout", "route/apply")

# Search endpoints that read despite being POST. Mirrors connectors/spitfire.py::_ALLOWED,
# plus the data-view resolver the vendor collection opens with.
READ_SHAPED_POSTS = ("/api/projects", "/docs", "/api/viewable/")

CC_TEST_MARKER = "CC-TEST"


# --------------------------------------------------------------------------- environment


def load_env_file() -> Dict[str, str]:
    """Premier_Automation/.env -- the only place credentials are read from."""
    path = ROOT / ".env"
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    return {k: v.strip() for k, v in re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", text, re.M)}


def load_postman_env(path: Path) -> Dict[str, str]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {v["key"]: v.get("value", "") for v in doc.get("values", []) if v.get("enabled", True)}


# --------------------------------------------------------------------------- the guard


def _is_readable(method: str, path: str) -> Tuple[bool, str]:
    """The guard. Returns (allowed, reason-if-not)."""
    lowered = path.lower()
    for bad in DENY_SUBSTRINGS:
        if bad in lowered:
            return False, f"denied by name: {bad}"
    if method == "GET":
        return True, ""
    if method == "POST" and any(frag in lowered for frag in READ_SHAPED_POSTS):
        return True, ""
    return False, f"{method} mutates"


# --------------------------------------------------------------------------- collection


def resolve(text: Any, env: Dict[str, str]) -> Any:
    if not isinstance(text, str):
        return text
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(env.get(m.group(1), m.group(0))), text)


def flatten(items: List[dict], folder: str = "") -> List[Tuple[str, dict]]:
    out: List[Tuple[str, dict]] = []
    for it in items:
        if "item" in it:
            out.extend(flatten(it["item"], it["name"]))
        else:
            out.append((folder, it))
    return out


def build_qty_patch_body(items: List[dict], manifest_text: str) -> str:
    """Port of the collection's pre-request script on 'Set received quantities'.

    Receives every open line in full unless a manifest names specific lines, in the form
    `0005=24;0007=10`. A line the manifest does not name is not received at all.
    """
    manifest: Dict[str, float] = {}
    for pair in re.split(r"[;,]", manifest_text or ""):
        bits = pair.split("=")
        if len(bits) == 2 and bits[0].strip():
            manifest[bits[0].strip()] = float(bits[1])
    have_manifest = bool(manifest)

    changes = []
    for i in items:
        detail = i.get("RelatedLineDetails") or {}
        task = (i.get("DocItemTask") or [{}])[0]
        number = str((i.get("ItemRevisionMap") or {}).get("ItemNumber") or "").strip()
        open_units = float(detail.get("ContractUnits") or 0) - float(detail.get("ReceivedUnits") or 0)
        qty = manifest.get(number) if have_manifest else open_units
        if qty is None or not qty > 0:
            continue
        if qty > open_units:
            print(f"      WARNING manifest says {qty} for item {number} but {open_units} outstanding")
        changes.append(
            {
                "DataMember": "DocItemTask",
                "DataField": "Quantity",
                "InstanceKey": task.get("ItemTaskKey"),
                "Data": str(qty),
            }
        )
    return json.dumps(changes, indent=2)


# --------------------------------------------------------------------------- the runner


class Runner:
    def __init__(self, env: Dict[str, str], out: Path, allow_writes: bool) -> None:
        self.env = env
        self.out = out
        self.allow_writes = allow_writes
        self.session = requests.Session()
        self.session.trust_env = False
        self.records: List[dict] = []
        self.created: List[Tuple[str, str]] = []
        (out / "bodies").mkdir(parents=True, exist_ok=True)

    # -- auth ---------------------------------------------------------------

    def cookie_headers(self) -> Dict[str, str]:
        return {"Cookie": "sfPMSAuth=" + self.env["sfPMSAuth"]}

    def psk_headers(self) -> Dict[str, str]:
        return {
            "APIClientID": self.env.get("apiClientId", ""),
            "APIClientKey": self.env.get("apiClientKey", ""),
        }

    @staticmethod
    def redact(headers: Dict[str, str]) -> Dict[str, str]:
        safe = dict(headers)
        if "Cookie" in safe:
            safe["Cookie"] = "sfPMSAuth=<REDACTED %d chars>" % max(0, len(headers["Cookie"]) - 11)
        for k in ("APIClientKey", "APIClientID"):
            if safe.get(k):
                safe[k] = "<REDACTED>"
        return safe

    # -- one call -----------------------------------------------------------

    def call(
        self,
        name: str,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Optional[str] = None,
        folder: str = "",
        documented: str = "",
        auth_substituted: bool = False,
        note: str = "",
    ) -> Optional[dict]:
        n = len(self.records) + 1
        path = url.split("/Training", 1)[-1] if "/Training" in url else url

        allowed, reason = _is_readable(method, path)
        if not allowed and not (self.allow_writes and "denied by name" not in reason):
            self.records.append(
                dict(
                    n=n, folder=folder, name=name, method=method, url=url,
                    req_headers=self.redact(headers), req_body=body, status=None,
                    resp_headers={}, ms=0, bytes=0, skipped=reason, documented=documented,
                    auth_substituted=auth_substituted, note=note,
                )
            )
            print("[%02d] %-6s SKIP %s  (%s)" % (n, method, name, reason))
            return None

        t0 = time.time()
        err = None
        try:
            resp = self.session.request(
                method, url, headers=headers,
                data=body.encode("utf-8") if body else None,
                timeout=90, allow_redirects=False,
            )
            ms = int((time.time() - t0) * 1000)
            status, rhdrs, text = resp.status_code, dict(resp.headers), resp.text
        except Exception as exc:  # noqa: BLE001 - recording the failure is the point
            ms = int((time.time() - t0) * 1000)
            status, rhdrs, text, err = None, {}, "", f"{type(exc).__name__}: {exc}"

        slug = re.sub(r"[^A-Za-z0-9]+", "_", name)[:44].strip("_")
        body_file = self.out / "bodies" / ("%02d_%s.json" % (n, slug))
        body_file.write_text(text, encoding="utf-8")

        drift = ""
        if documented and status is not None and str(status) != documented:
            drift = "documented %s, got %s" % (documented, status)

        self.records.append(
            dict(
                n=n, folder=folder, name=name, method=method, url=url,
                req_headers=self.redact(headers), req_body=body, status=status,
                resp_headers=rhdrs, ms=ms, bytes=len(text), err=err,
                body_file=body_file.name, documented=documented, drift=drift,
                auth_substituted=auth_substituted, note=note, skipped="",
            )
        )
        flag = " <<DRIFT %s>>" % drift if drift else ""
        print("[%02d] %-6s %-4s %6dms %8dB  %s%s" % (n, method, status or "ERR", ms, len(text), name, flag))
        if err:
            print("      ERROR", err)
        try:
            return json.loads(text)
        except Exception:  # noqa: BLE001 - plenty of responses are not JSON
            return None


# --------------------------------------------------------------------------- report


def write_report(out: Path, records: List[dict], meta: Dict[str, str]) -> None:
    lines: List[str] = []
    add = lines.append
    add("# Every endpoint named in `dev_reports/postman` - exact request and response\n")
    for k, v in meta.items():
        add("- **%s**: %s" % (k, v))
    add("")

    fired = [r for r in records if not r.get("skipped")]
    ok = sum(1 for r in fired if r.get("status") == 200)
    drifted = [r for r in fired if r.get("drift")]
    add("**%d calls fired, %d returned 200. %d skipped. %d differ from what the runbook documents.**\n"
        % (len(fired), ok, len(records) - len(fired), len(drifted)))

    add("| # | Method | Path | Status | ms | Bytes | vs. runbook |")
    add("|---|---|---|---|---|---|---|")
    for r in records:
        path = r["url"].split("/Training", 1)[-1] if "/Training" in r["url"] else r["url"]
        status = r.get("skipped") or (r.get("status") or "ERR")
        add("| %d | %s | `%s` | %s | %s | %s | %s |" % (
            r["n"], r["method"], path, status, r.get("ms") or "", r.get("bytes") or "",
            r.get("drift") or ("-" if not r.get("documented") else "as documented")))
    add("")
    add("---\n")

    for r in records:
        add("## %02d - %s" % (r["n"], r["name"]))
        if r.get("note"):
            add("_%s_\n" % r["note"])
        if r.get("auth_substituted"):
            add("> Authenticated with the **session cookie**, not the `APIClientID`/`APIClientKey` "
                "pair this collection was written for. The endpoint is exercised; the collection's "
                "own auth path is not.\n")
        if r.get("skipped"):
            add("**Not fired** - %s. The request as it would have gone out:\n" % r["skipped"])
        add("```http")
        add("%s %s" % (r["method"], r["url"]))
        for k, v in r["req_headers"].items():
            add("%s: %s" % (k, v))
        if r.get("req_body"):
            add("")
            add(r["req_body"])
        add("```")
        if r.get("skipped"):
            add("")
            continue
        add("```http")
        add("HTTP/1.1 %s" % r["status"])
        for k in ("Content-Type", "Content-Length", "Location"):
            if k in r.get("resp_headers", {}):
                add("%s: %s" % (k, r["resp_headers"][k]))
        add("")
        raw = (out / "bodies" / r["body_file"]).read_text(encoding="utf-8")
        try:
            pretty = json.dumps(json.loads(raw), indent=2)
        except Exception:  # noqa: BLE001
            pretty = raw
        if len(pretty) > 1800:
            add(pretty[:1800].rstrip())
            add("... truncated - %d bytes total; full body: bodies/%s" % (r["bytes"], r["body_file"]))
        else:
            add(pretty or "(empty body)")
        add("```")
        add("_%d bytes, %d ms_\n" % (r["bytes"], r["ms"]))

    (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    (out / "bodies" / "_index.json").write_text(json.dumps(records, indent=2), encoding="utf-8")


__all__ = [
    "Runner", "load_env_file", "load_postman_env", "flatten", "resolve",
    "build_qty_patch_body", "write_report", "_is_readable",
    "RECEIVER_COLLECTION", "RECEIVER_ENV", "RECEIPT_COLLECTION", "RECEIPT_ENV",
    "CC_TEST_MARKER",
]


# --------------------------------------------------------------------------- phases

# Endpoints the runbook names only in prose, with the status it claims for each, so the
# report can flag drift on a build the runbook was never tested against.
PROSE_ENDPOINTS: List[Tuple[str, str, str, str]] = [
    ("prose - projects list is POST-only",       "/api/projects",                              "405", ""),
    ("prose - project docs is POST-only",        "/api/project/{{projectId}}/docs",            "405", ""),
    ("prose - catalog file (redirect)",          "/api/catalog/{{fileKey}}/file",              "302", "302 to sfImg.ashx"),
    ("prose - catalog access history",           "/api/catalog/{{fileKey}}/AccessHistory",     "200", "who opened it, when, how"),
    ("prose - catalog meta (broken)",            "/api/catalog/{{fileKey}}/meta",              "500", "same key /versions accepts"),
    ("prose - catalog object (broken)",          "/api/catalog/{{fileKey}}/object",            "500", "same key /versions accepts"),
    ("prose - catalog router (broken)",          "/api/catalog/{{fileKey}}/router",            "500", "same key /versions accepts"),
    ("prose - per-field audit trail",            "/api/history/DocMasterDetail/Status/{{docKey}}", "200", "ChangeWhen, PriorValue, NewValue, ByUser. DataMember from uicfg/live/DocHeader"),
    ("prose - report list, arg 0",               "/api/session/reports/0",                     "200", ""),
    ("prose - configuration doc-types (stub)",   "/api/configuration/doc-types",               "500", "case 36629, not yet implemented"),
    ("prose - document export (blocked)",        "/api/document/{{docKey}}/export",            "400", ""),
    ("prose - incremental changes, items",       "/api/document/{{docKey}}/changes/items",     "404", "no ETag diff-gram on this build"),
    ("prose - incremental changes, attachments", "/api/document/{{docKey}}/changes/attachments", "404", "no ETag diff-gram on this build"),
    ("prose - dialog abstract",                  "/api/Document/{{docKey}}/dialog/abstract",   "200", "carries a Receipt Log report link"),
    ("prose - dialog dates",                     "/api/Document/{{docKey}}/dialog/dates",      "200", "lists addable date types"),
]


def resolve_po(run: "Runner", base: str, po: str) -> str:
    """Turn a PO number into a DocMasterKey up front, via the vendor collection's resolver.

    Folder B of the receiver collection only searches the three projects in
    SPITFIRE_PROJECT_IDS, so for any PO outside them it finds nothing and `docKey` would keep
    whatever the environment file shipped with -- and the whole C group would then read a
    different document than the one asked for, silently. `viewable/DocMasterAlt` needs no
    project id and covers every PO, so it seeds `docKey` before anything else runs.
    """
    body = json.dumps({
        "RequestID": "1",
        "DVName": "DocMasterAlt",
        "MatchingValue": po,
        "DependsOn": [run.env.get("poTypeKey", ""), "empty", "empty"],
    })
    parsed = run.call(
        "resolve PO %s to a DocMasterKey" % po, "POST", base + "/api/viewable/DocMasterAlt",
        dict(run.cookie_headers(), **{"Content-Type": "application/json", "Accept": "application/json"}),
        body, folder="0 - resolve",
        note="Seeds docKey for every later call. Folder B cannot do this for a PO outside the "
             "three configured projects; this can.")
    key = parsed if isinstance(parsed, str) else ""
    if key:
        run.env["docKey"] = key
        run.env["poDmk"] = key
        print("      -> docKey = %s" % key)
    else:
        print("      -> PO %s does not resolve; docKey left unset" % po)
        run.env["docKey"] = ""
    return key


def phase_psk_probe(run: "Runner", base: str) -> None:
    """Five read-only variants. Records whether the supplied PSK pair resolves to a user."""
    cid = run.env.get("apiClientId", "")
    key = run.env.get("apiClientKey", "")
    if not cid or not key:
        print("  no PSK pair configured - skipping probe")
        return
    variants = [
        ("PSK as supplied", cid, key),
        ("PSK lowercased", cid.lower(), key.lower()),
        ("PSK without dashes", cid.replace("-", ""), key.replace("-", "")),
        ("PSK brace-wrapped", "{" + cid + "}", "{" + key + "}"),
        ("PSK id and key swapped", key, cid),
    ]
    for label, i, k in variants:
        run.call(label, "GET", base + "/api/session/who",
                 {"APIClientID": i, "APIClientKey": k, "Accept": "application/json"},
                 folder="0 - PSK probe",
                 note="Does the APIClientID/APIClientKey pair resolve to a user on this host?")
    run.call("PSK on the anonymous liveness probe", "GET", base + "/api/account/session",
             {"APIClientID": cid, "APIClientKey": key, "Accept": "application/json"},
             folder="0 - PSK probe",
             note="Returns a bare boolean. false means the pair produced no session.")


def phase_collection(run: "Runner", collection: Path, base: str, headers_fn,
                     folder_filter=None, auth_substituted: bool = False) -> None:
    doc = json.loads(collection.read_text(encoding="utf-8"))
    for var in doc.get("variable", []):
        run.env.setdefault(var["key"], var.get("value", ""))

    for folder, item in flatten(doc["item"]):
        req = item["request"]
        raw_url = req["url"]["raw"] if isinstance(req["url"], dict) else req["url"]
        if folder_filter and not folder_filter(req["method"], raw_url):
            continue
        url = resolve(raw_url, run.env)
        headers = dict(headers_fn())
        for h in req.get("header", []):
            if h.get("disabled"):
                continue
            # The collection's own auth headers are supplied by headers_fn, not copied.
            if h["key"] in ("Cookie", "APIClientID", "APIClientKey"):
                continue
            headers[h["key"]] = resolve(h["value"], run.env)

        body = None
        raw_body = (req.get("body") or {}).get("raw")
        if raw_body:
            body = resolve(raw_body, run.env)

        parsed = run.call(item["name"], req["method"], url, headers, body,
                          folder=folder, auth_substituted=auth_substituted)
        capture(run, item["name"], url, parsed)


def capture(run: "Runner", name: str, url: str, parsed: Any) -> None:
    """Emulate only the Tests-script captures that later requests depend on."""
    env = run.env
    if name.startswith("B1") and isinstance(parsed, list):
        po = env.get("poNumber")
        hit = next((r for r in parsed if r.get("DocNo") == po or r.get("SubContract") == po), None)
        if hit:
            env["docKey"] = hit["DocMasterKey"]
            env["foundInProject"] = url.split("/api/project/")[1].split("/")[0]
            print("      -> docKey = %s  (%s)" % (env["docKey"], env["foundInProject"]))
        else:
            print("      -> no exact match (%d rows) - keeping the resolved docKey" % len(parsed))

    elif name.startswith("Resolve PO number") and isinstance(parsed, str):
        env["poDmk"] = parsed
        print("      -> poDmk = %s" % parsed)

    elif name.startswith("PO header") and isinstance(parsed, dict):
        env["project"] = parsed.get("Project") or ""
        env["poNumber"] = parsed.get("SubContract") or env.get("poNumber", "")
        print("      -> project = %s, poNumber = %s" % (env["project"], env["poNumber"]))

    elif name.startswith("PO items") and isinstance(parsed, list):
        numbers = [str((i.get("ItemRevisionMap") or {}).get("ItemNumber") or "").strip() for i in parsed]
        env["poItemNumbers"] = json.dumps([n for n in numbers if n])
        print("      -> %d PO line number(s)" % len([n for n in numbers if n]))

    elif name.startswith("Create Receipt") and isinstance(parsed, str) and parsed:
        env["receiptDmk"] = parsed
        run.created.append(("receipt document", parsed))
        print("      -> receiptDmk = %s  [CREATED]" % parsed)

    elif name.startswith("Create Pay Request") and isinstance(parsed, str) and parsed:
        run.created.append(("pay request", parsed))
        print("      -> pay request %s  [CREATED]" % parsed)

    elif name.startswith("Receipt items") and isinstance(parsed, list):
        env["receiptQtyPatchBody"] = build_qty_patch_body(parsed, env.get("exampleManifest", ""))
        print("      -> prepared %d quantity change(s)" % len(json.loads(env["receiptQtyPatchBody"])))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", default="reads", choices=["reads", "all"])
    ap.add_argument("--allow-writes", action="store_true", help="permit the 10 mutating requests")
    ap.add_argument("--out", default=None)
    ap.add_argument("--po", default="907030", help="PO number to run against")
    ap.add_argument("--receipt-dmk", default="",
                    help="an existing receipt DocMasterKey, so the four receipt-reading requests "
                         "can be exercised without creating one")
    args = ap.parse_args()

    dotenv = load_env_file()
    env = load_postman_env(RECEIVER_ENV)
    env.update({k: v for k, v in load_postman_env(RECEIPT_ENV).items() if v})
    env["sfPMSAuth"] = dotenv.get("SPITFIRE_SESSION_COOKIE", "")
    env["apiClientId"] = dotenv.get("SPITFIRE_API_CLIENT_ID", "")
    env["apiClientKey"] = dotenv.get("SPITFIRE_API_CLIENT_KEY", "")
    base = dotenv.get("SPITFIRE_BASE_URL", "https://spitfire-host.test/instance")
    env["base"] = base
    env["baseUrl"] = base
    env["poNumber"] = args.po
    env["runVariants"] = "true"
    env["receiptDmk"] = args.receipt_dmk
    env["receiptTrackingNumber"] = CC_TEST_MARKER + "-" + time.strftime("%Y%m%d")

    if not env["sfPMSAuth"]:
        print("SPITFIRE_SESSION_COOKIE is empty in .env - nothing can authenticate", file=sys.stderr)
        return 2

    out = Path(args.out) if args.out else (
        ROOT.parent / "dev_reports" / ("Postman_Full_Sweep_" + time.strftime("%Y-%m-%d")))
    run = Runner(env, out, allow_writes=args.allow_writes)

    print("=== phase 0 - PSK probe ===")
    phase_psk_probe(run, base)

    print("")
    print("=== phase 0b - resolve the PO ===")
    resolve_po(run, base, args.po)

    print("")
    print("=== phase 1 - Spitfire_Receiver_GETs (19) ===")
    phase_collection(run, RECEIVER_COLLECTION, base, run.cookie_headers)

    print("")
    print("=== phase 2 - endpoints named only in the runbook prose (%d) ===" % len(PROSE_ENDPOINTS))
    for label, path, documented, note in PROSE_ENDPOINTS:
        run.call(label, "GET", base + resolve(path, env), run.cookie_headers(),
                 folder="Runbook prose", documented=documented, note=note)

    only_reads = lambda method, raw_url: _is_readable(method, raw_url)[0]  # noqa: E731
    print("")
    print("=== phase 3 - sfPMS-PO-Receipt%s ===" % ("" if args.phase == "all" else ", read-only subset"))
    phase_collection(run, RECEIPT_COLLECTION, base, run.cookie_headers,
                     folder_filter=None if args.phase == "all" else only_reads,
                     auth_substituted=True)

    version = ""
    try:
        version = requests.get(base + "/api/system/version", timeout=30).text.strip()
    except Exception:  # noqa: BLE001
        version = "(unavailable)"

    meta = {
        "host": base,
        "sfPMS version": version,
        "auth": "session cookie (sfPMSAuth) from Premier_Automation/.env",
        "PO under test": args.po,
        "writes": "ENABLED" if args.allow_writes else "blocked (default)",
        "denied by name": ", ".join(DENY_SUBSTRINGS),
    }
    write_report(out, run.records, meta)

    if run.created:
        lines = ["# Artefacts created on training by this run", "",
                 "Marked `%s`. `route/apply` was never called, so nobody was notified." % CC_TEST_MARKER,
                 "", "| what | key |", "|---|---|"]
        lines += ["| %s | `%s` |" % (what, key) for what, key in run.created]
        (out / "CREATED_ARTEFACTS.md").write_text("\n".join(lines), encoding="utf-8")

    fired = [r for r in run.records if not r.get("skipped")]
    ok = sum(1 for r in fired if r.get("status") == 200)
    print("")
    print("%d fired, %d x 200, %d skipped. Report: %s"
          % (len(fired), ok, len(run.records) - len(fired), out / "REPORT.md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
