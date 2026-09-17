"""Capture the raw response body of every readable sfPMS endpoint.

Answers "what does this endpoint actually return", which the Swagger cannot: the spec
declares no enums for any business field, so code lists, status vocabularies and date-type
names only exist in live responses.

READ ONLY, enforced twice. `_is_readable` rejects anything that is not a GET, except the two
read-shaped POSTs that Spitfire uses for search (`/api/projects`, `/api/project/{id}/docs`) —
the same pair `connectors/spitfire.py::_ALLOWED` permits. Both Logout endpoints are denied by
name: they are GETs, and calling one would destroy the hand-captured session ticket this script
depends on.

Auth is the `sfPMSAuth` cookie from .env. It lapses on idle; recapture with
F12 -> Application -> Cookies -> sfPMSAuth. Runs anonymously if it is absent or dead, which is
still useful — it records exactly which endpoints answer without a login.

    python tools/spitfire_capture_responses.py [--out DIR] [--limit N] [--timeout S]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parents[1]
SPEC_URL = "/swagger/v23/swagger.json"

# GETs that mutate. Both end the session; the ticket is hand-captured and not cheaply replaced.
DENY_SUBSTRINGS = ("/logout",)

# Search endpoints that read despite being POST. Mirrors connectors/spitfire.py::_ALLOWED.
READ_SHAPED_POSTS = {
    "/api/projects",
    "/api/project/{projectID}/docs",
}


def load_env() -> Dict[str, str]:
    path = ROOT / ".env"
    if not path.exists():
        return {}
    pairs = re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", path.read_text(encoding="utf-8"), re.M)
    return {k: v.strip() for k, v in pairs}


def _is_readable(method: str, path: str) -> Tuple[bool, str]:
    """The guard. Returns (allowed, reason-if-not)."""
    lowered = path.lower()
    for bad in DENY_SUBSTRINGS:
        if bad in lowered:
            return False, "denied: ends the session"
    if method == "GET":
        return True, ""
    if method == "POST" and path in READ_SHAPED_POSTS:
        return True, ""
    return False, f"not a read ({method})"


class Capturer:
    def __init__(self, base: str, cookie: str, timeout: float, out: Path):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.out = out
        self.bodies = out / "bodies"
        self.bodies.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/json"
        if cookie:
            host = re.sub(r"^https?://([^/]+).*$", r"\1", self.base)
            self.session.cookies.set("sfPMSAuth", cookie, domain=host, path="/")
        self.authenticated = False
        self.records: List[Dict[str, Any]] = []

    # -- auth ------------------------------------------------------------------
    def check_auth(self) -> bool:
        r = self.session.get(f"{self.base}/api/session/who", timeout=self.timeout)
        self.authenticated = r.status_code == 200
        print(f"  /api/session/who -> {r.status_code}", flush=True)
        if not self.authenticated:
            print("  cookie is dead or absent; continuing ANONYMOUSLY", flush=True)
        return self.authenticated

    # -- one call --------------------------------------------------------------
    def call(self, method: str, path: str, body: Any = None, note: str = "") -> Dict[str, Any]:
        ok, why = _is_readable(method, path if body is None else path)
        url = f"{self.base}{path}"
        rec: Dict[str, Any] = {"method": method, "path": path, "note": note}
        started = time.time()
        try:
            if method == "GET":
                r = self.session.get(url, timeout=self.timeout, allow_redirects=False)
            else:
                r = self.session.post(url, json=body, timeout=self.timeout, allow_redirects=False)
        except requests.RequestException as exc:
            rec.update(status=None, error=str(exc)[:300], ms=int((time.time() - started) * 1000))
            self.records.append(rec)
            return rec

        rec["status"] = r.status_code
        rec["ms"] = int((time.time() - started) * 1000)
        rec["content_type"] = r.headers.get("Content-Type", "")
        rec["bytes"] = len(r.content)
        text = r.text
        try:
            parsed = r.json()
            rec["json"] = True
            if isinstance(parsed, list):
                rec["shape"] = f"array[{len(parsed)}]"
                if parsed and isinstance(parsed[0], dict):
                    rec["keys"] = sorted(parsed[0].keys())
            elif isinstance(parsed, dict):
                rec["shape"] = "object"
                rec["keys"] = sorted(parsed.keys())
            else:
                rec["shape"] = type(parsed).__name__
        except ValueError:
            parsed = None
            rec["json"] = False
            rec["shape"] = "text"
        rec["preview"] = text[:600]

        slug = re.sub(r"[^A-Za-z0-9]+", "_", f"{method}{path}").strip("_")[:150]
        target = self.bodies / f"{slug}.json"
        payload = parsed if parsed is not None else text
        target.write_text(
            json.dumps({"request": {"method": method, "path": path, "body": body},
                        "status": r.status_code, "response": payload}, indent=1, default=str),
            encoding="utf-8",
        )
        rec["saved"] = target.name
        self.records.append(rec)
        return rec


def fill(path: str, values: Dict[str, str]) -> Optional[str]:
    """Substitute {param} placeholders. Returns None if any is unknown."""
    out = path
    for name in re.findall(r"\{([^}]+)\}", path):
        key = name.lower()
        if key not in values:
            return None
        out = out.replace("{" + name + "}", quote(str(values[key]), safe=""))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=None, help="output directory")
    ap.add_argument("--limit", type=int, default=0, help="stop after N calls (0 = all)")
    ap.add_argument("--timeout", type=float, default=45.0)
    args = ap.parse_args()

    env = load_env()
    base = env.get("SPITFIRE_BASE_URL", "https://spitfire-host.test/instance")
    cookie = env.get("SPITFIRE_SESSION_COOKIE", "")
    out = Path(args.out) if args.out else ROOT.parent / "dev_reports" / "spitfire_capture"
    out.mkdir(parents=True, exist_ok=True)

    print(f"base   : {base}")
    print(f"cookie : {'present (%d chars)' % len(cookie) if cookie else 'ABSENT'}")
    print(f"out    : {out}\n")

    cap = Capturer(base, cookie, args.timeout, out)
    print("auth check")
    cap.check_auth()

    print("\nfetching spec")
    spec = requests.get(f"{base}{SPEC_URL}", timeout=args.timeout).json()
    paths = spec.get("paths", {})
    print(f"  {len(paths)} paths")

    # Known path-parameter values. Project ids and doc type keys come from .env; document
    # keys are discovered below, because a DocMasterKey cannot be guessed.
    projects = [p for p in env.get("SPITFIRE_PROJECT_IDS", "").split(",") if p.strip()]
    values: Dict[str, str] = {}
    if projects:
        values["projectid"] = projects[0].strip()
        values["project"] = projects[0].strip()
    for k, v in (("fordoctype", env.get("SPITFIRE_PO_DOC_TYPE_KEY", "")),
                 ("typekey", env.get("SPITFIRE_RECEIPT_DOC_TYPE_KEY", "")),
                 ("folderdesignation", env.get("SPITFIRE_SEARCH_SCOPE", "0")),
                 ("count", "1"), ("enabled", "false"), ("mode", "0"),
                 ("setname", "UOM"), ("lookupname", "Vendor"), ("datacontext", "0"),
                 ("menuid", "0"), ("groupid", "0"), ("partname", "DocHeader"),
                 ("filetype", "pdf"), ("setid", "0"), ("ucmodule", "DOC"), ("ucname", "PO")):
        if v:
            values[k] = v

    # Discover a real document key so the ~40 /api/document/{id}/* readers can be exercised.
    if cap.authenticated and projects:
        print("\ndiscovering a document key")
        r = cap.call("POST", "/api/projects", body={}, note="discovery")
        r = cap.call("POST", f"/api/project/{projects[0].strip()}/docs",
                     body={"ForDocType": env.get("SPITFIRE_PO_DOC_TYPE_KEY", "")},
                     note="discovery")
        try:
            rows = json.loads((cap.bodies / r["saved"]).read_text(encoding="utf-8"))["response"]
            for row in rows if isinstance(rows, list) else []:
                key = row.get("DocMasterKey") or row.get("DocMaster")
                if key:
                    values["id"] = key
                    print(f"  using DocMasterKey {key}")
                    break
        except Exception as exc:  # discovery is best-effort
            print(f"  discovery failed: {exc}")

    # Sweep.
    print("\nsweeping")
    skipped: List[Tuple[str, str, str]] = []
    calls = 0
    for path, item in sorted(paths.items()):
        for method, op in item.items():
            m = method.upper()
            if m not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                continue
            ok, why = _is_readable(m, path)
            if not ok:
                skipped.append((m, path, why))
                continue
            filled = fill(path, values)
            if filled is None:
                unknown = [n for n in re.findall(r"\{([^}]+)\}", path) if n.lower() not in values]
                skipped.append((m, path, f"no value for {','.join(unknown)}"))
                continue
            body = {} if m == "POST" else None
            rec = cap.call(m, filled, body=body, note=op.get("summary", "")[:120])
            calls += 1
            # status is None when the request raised (timeout, reset) — keep sweeping.
            print(f"  {str(rec.get('status')):>4} {m:<5} {filled}", flush=True)
            if args.limit and calls >= args.limit:
                print("  limit reached")
                break
        if args.limit and calls >= args.limit:
            break

    # Index.
    (out / "index.json").write_text(
        json.dumps({"base": base, "authenticated": cap.authenticated,
                    "spec_version": spec.get("info", {}).get("version"),
                    "calls": cap.records,
                    "skipped": [{"method": m, "path": p, "why": w} for m, p, w in skipped]},
                   indent=1, default=str),
        encoding="utf-8",
    )

    by_status: Dict[Any, int] = {}
    for r in cap.records:
        by_status[r.get("status")] = by_status.get(r.get("status"), 0) + 1
    print(f"\n{calls} calls · authenticated={cap.authenticated}")
    print("status mix:", dict(sorted(by_status.items(), key=lambda x: str(x[0]))))
    print(f"skipped {len(skipped)} (writes + unfillable path params)")
    print(f"bodies -> {cap.bodies}")
    print(f"index  -> {out / 'index.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
