"""Read purchase orders out of Spitfire and mirror them locally. Writes nothing to Spitfire.

This is Milestone 1 of the integration: prove we can authenticate without a browser, turn a PO
number from an email into a Spitfire document, and pull its lines with the field traps handled.
Stage 4 then matches against the mirror instead of against the twelve hand-written rows in
`api/demo/catalog.py`.

    python -m tools.pull_spitfire_po --preflight            # reachability + login, reads no PO
    python -m tools.pull_spitfire_po --po 912456            # one PO
    python -m tools.pull_spitfire_po --from-state           # every PO the pipeline has seen
    python -m tools.pull_spitfire_po --from-state --source sample --limit 20

**Nothing here can write to Premier's ERP.** `connectors.spitfire.SpitfireReadClient` has no
write methods, and its allowlist rejects any request that is not a known read before a socket is
opened. Every run ends by dumping the exact list of requests it issued into the report, so the
claim is checkable by Premier rather than merely asserted.

`--preflight` needs no credentials for its first half: `GET /api/system/version` answers
anonymously, which is how we know the training host is reachable from outside Premier's network
at all. Run it before asking anyone to debug a login.
"""

import argparse
import sys
from datetime import datetime, timezone
from typing import List, Optional

import requests

from config import settings
from connectors.spitfire import SpitfireReadClient, SpitfireReadOnlyViolation, is_allowed
from pipeline import spitfire_mirror, state_db

REPORTS_DIR = settings.BASE_DIR.parent / "dev_reports"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def po_numbers_from_state(source: str, limit: Optional[int]) -> List[str]:
    """Every PO the pipeline has extracted or is accumulating against.

    Both tables are read, not just `extracted_records`: a delivery whose extraction failed still
    has an `accumulation` row, and those are precisely the POs a person will have to finish by
    hand — so they are the ones whose lines the review UI most needs mirrored.
    """
    conn = state_db.get_connection(state_db.path_for(source))
    try:
        rows = conn.execute(
            "SELECT DISTINCT po_number FROM extracted_records WHERE po_number IS NOT NULL "
            "  AND po_number != '' "
            "UNION "
            "SELECT DISTINCT po_number FROM accumulation WHERE po_number IS NOT NULL "
            "  AND po_number != '' "
            "ORDER BY po_number"
        ).fetchall()
    finally:
        conn.close()
    numbers = [str(r[0]).strip() for r in rows]
    return numbers[:limit] if limit else numbers


def preflight(client: SpitfireReadClient, with_login: bool) -> int:
    print(f"base url        : {client.base_url}")
    try:
        version = client.server_version()
    except (requests.RequestException, ValueError) as e:
        print(f"reachability    : FAILED ({type(e).__name__}: {e})")
        return 1
    # Plain ASCII in everything printed: the Windows console runs cp1252/cp437 by default and
    # renders an em dash as a replacement glyph, which makes a healthy preflight look broken.
    print(f"server version  : {version}   (anonymous - no credentials needed for this call)")

    if not with_login:
        return 0

    if client.cookie_mode:
        # A borrowed browser ticket. Whose it is matters for the audit trail, so it is printed
        # rather than left implicit — every read in this run is attributed to that person.
        if not client.has_session():
            print("login           : FAILED - the supplied SPITFIRE_SESSION_COOKIE is expired "
                  "or rejected.")
            print("                  Capture a fresh sfPMSAuth value from the browser.")
            return 1
        who = client.whoami()
        print(f"login           : OK via supplied sfPMSAuth cookie")
        print(f"identity        : {who.get('FullName') or '(unknown)'}  "
              f"UserKey={who.get('UserKey') or '?'}")
        print("                  NB borrowed session - reads are attributed to this account.")
        return 0

    if not client.uid or not client.pw:
        print("login           : SKIPPED - no SPITFIRE_UID/SPITFIRE_PW and no "
              "SPITFIRE_SESSION_COOKIE in .env.")
        print("                  Premier has not provisioned svc-receiver-automation yet.")
        print("                  For this phase that account needs READ permission only.")
        return 0
    try:
        client.authenticate()
    except (RuntimeError, requests.RequestException) as e:
        print(f"login           : FAILED ({e})")
        return 1
    print(f"login           : OK as {client.uid}")
    print(f"session active  : {client.has_session()}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--po", action="append", default=[], metavar="NUMBER",
                        help="a PO number to pull; repeatable")
    parser.add_argument("--from-state", action="store_true",
                        help="pull every PO number the pipeline has seen")
    parser.add_argument("--source", choices=[state_db.STORE_MAILBOX, state_db.STORE_SAMPLE],
                        default=state_db.STORE_SAMPLE,
                        help="which state store to read PO numbers from and mirror into "
                             "(default: sample — the .msg corpus, never Premier's live mail)")
    parser.add_argument("--limit", type=int, default=None, help="cap how many POs to pull")
    parser.add_argument("--preflight", action="store_true",
                        help="check reachability and login, then stop without reading any PO")
    parser.add_argument("--no-report", action="store_true", help="skip writing the report file")
    args = parser.parse_args(argv)

    try:
        client = SpitfireReadClient()
    except ValueError as e:
        print(f"cannot start: {e}", file=sys.stderr)
        return 2

    if args.preflight:
        return preflight(client, with_login=True)

    po_numbers = list(dict.fromkeys(args.po))
    if args.from_state:
        po_numbers = list(dict.fromkeys(po_numbers + po_numbers_from_state(args.source, args.limit)))
    if not po_numbers:
        parser.error("nothing to do — pass --po NUMBER, --from-state, or --preflight")
    if args.limit:
        po_numbers = po_numbers[:args.limit]

    if preflight(client, with_login=True) != 0:
        return 1
    if not client.cookie_mode and (not client.uid or not client.pw):
        print("\nno credentials, so no PO can be read. Nothing was written anywhere.")
        return 1

    conn = state_db.get_connection(state_db.path_for(args.source))
    results = []
    print(f"\npulling {len(po_numbers)} PO(s) into the {args.source} store\n")
    try:
        for po_number in po_numbers:
            try:
                key = client.resolve_po(po_number)
                if not key:
                    print(f"  {po_number:<10} NOT FOUND in Spitfire")
                    results.append((po_number, None, None, "not found in Spitfire"))
                    continue
                doc = client.read_po(key)
                stored = spitfire_mirror.save_po(conn, doc, _now())
                print(f"  {po_number:<10} {stored} line(s), {doc.tax_lines_skipped} tax line(s) "
                      f"skipped - {doc.project_code} {doc.doc_status_label}")
                results.append((po_number, key, doc, None))
            except SpitfireReadOnlyViolation:
                raise   # a bug in our own code, never something to log and continue past
            except (requests.RequestException, RuntimeError, ValueError) as e:
                # One unreadable PO must not lose the other forty. The reason is carried into the
                # report so an operator sees which POs are missing and why.
                print(f"  {po_number:<10} ERROR {type(e).__name__}: {e}")
                results.append((po_number, None, None, f"{type(e).__name__}: {e}"))
    finally:
        conn.close()

    if not args.no_report:
        path = write_report(client, results, args.source)
        print(f"\nreport: {path}")

    escaped = [r for r in client.audit_log if not is_allowed(r.method, r.path)]
    print(f"requests issued: {len(client.audit_log)}, all reads: {not escaped}")
    return 0 if not escaped else 1


def write_report(client: SpitfireReadClient, results, source: str):
    """A run report plus the audit trail, per the project's dev_reports convention."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = REPORTS_DIR / f"Spitfire_PO_Pull_{stamp}.md"

    found = [r for r in results if r[2] is not None]
    lines = [
        f"# Spitfire PO pull — {stamp}",
        "",
        f"- Instance: `{client.base_url}`",
        f"- User: `{client.uid}`",
        f"- State store: `{source}`",
        f"- POs requested: {len(results)}; resolved: {len(found)}; unresolved: {len(results) - len(found)}",
        "",
        "**Nothing was written to Spitfire.** Every request issued is listed at the end of this",
        "file; each one is a read on `connectors/spitfire.py::_ALLOWED`.",
        "",
        "## Purchase orders",
        "",
        "| PO | DocMasterKey | Project | Status | Vendor | Lines | Tax skipped | Note |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for po_number, key, doc, error in results:
        if doc is None:
            lines.append(f"| {po_number} | — | — | — | — | — | — | {error} |")
        else:
            lines.append(
                f"| {po_number} | `{key}` | {doc.project_code} | {doc.doc_status_label} | "
                f"{doc.vendor_name} | {len(doc.lines)} | {doc.tax_lines_skipped} | |"
            )

    for po_number, _key, doc, _error in results:
        if doc is None or not doc.lines:
            continue
        lines += ["", f"### PO {po_number} — {doc.project_name}", "",
                  "| Line | Spec | Description | UOM | Ordered | Received | In transit | "
                  "Outstanding | Cost code |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for line in doc.lines:
            # "In transit" is ReceiptInProgressUnits — a receipt raised but not yet approved.
            # A non-zero value here on a line we are about to receive against means someone (or a
            # previous run) has already raised one, and is the strongest duplicate signal we get.
            lines.append(
                f"| {line.line_number} | {line.spec_code} | {line.description} | "
                f"{line.unit_of_measure} | {line.qty_ordered:g} | {line.qty_received:g} | "
                f"{line.qty_in_transit:g} | {line.qty_outstanding:g} | {line.cost_code} |"
            )

    lines += ["", "## Request audit", "",
              "Evidence for Premier that this run only read. Every row is checked against the",
              "read-only allowlist; a `no` in the last column would be a defect in this tool.",
              "", "| # | Method | Path | Status | ms | Allowed read |", "|---|---|---|---|---|---|"]
    for i, record in enumerate(client.audit_log, start=1):
        allowed = "yes" if is_allowed(record.method, record.path) else "**no**"
        lines.append(f"| {i} | {record.method} | `{record.path}` | {record.status or '—'} | "
                     f"{record.elapsed_ms} | {allowed} |")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    raise SystemExit(main())
