"""Record every Spitfire response the application actually needs, so development can continue
away from Premier's office IP.

Spitfire answers only from that network, and `SPITFIRE_SESSION_COOKIE` is a hand-captured browser
ticket that lapses on idle — so the connected window is short even when you are there. Run this
while you have one; then `SPITFIRE_CASSETTE_MODE=replay` gives you Verify, the PO mirror and every
read-back with no network at all.

**This is not `tools/spitfire_capture_responses.py`.** That one sweeps the Swagger: GET-only, one
call per endpoint *template* against a single sample GUID. It answers "what shape does `/items`
return"; it cannot answer "what does PO 906725 hold". This tool is driven by the call sites — the
six read operations `connectors/spitfire.py` exposes, for the purchase orders the pipeline has
actually seen.

    python -m tools.spitfire_record_cassettes                 # the full sweep
    python -m tools.spitfire_record_cassettes --po 906725     # one PO
    python -m tools.spitfire_record_cassettes --verify        # re-read live and diff vs the store
    python -m tools.spitfire_record_cassettes --promote       # scrubbed subset -> tests/fixtures
    python -m tools.spitfire_record_cassettes --status        # what is in the store

**Reads only.** Every call goes through `SpitfireReadClient`, which has no write methods and whose
allowlist refuses anything else before a socket opens. Nothing here can create a receipt.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings                                        # noqa: E402
from connectors import spitfire_cassette                           # noqa: E402
from connectors.spitfire import SpitfireReadClient                 # noqa: E402
from pipeline import state_db                                      # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "spitfire"

# The four POs promoted to `tests/fixtures/`, each because it exercises a shape that has already
# cost us a bug. Anything else stays in the local store: the bodies carry real vendor names and
# named Premier employees on approval routes, and this repo is pushed to GitHub.
FIXTURE_POS = {
    "906725": "six specs on six clean lines — the happy multi-line delivery",
    "908491": "base/shade collapsing onto a parent line; Authority line numbers that are not Spitfire's",
    "910635": "one spec (GR-350c-WTF) on two lines, settled only by unit of measure",
    "907514": "one spec (LOB-900-SI) on 29 lines — the case no spec can resolve",
}

# Fields rewritten before a body is committed. Names, not data: spec codes, quantities, line
# numbers, units and cost codes are what the tests are about and are kept exactly as recorded.
_PERSON_FIELDS = ("UserName", "UserName_dv", "Contact", "Email", "FromUser", "FromUser_dv",
                  "LastStatusBy", "LastStatusBy_dv", "Author", "Author_dv")
_COMPANY_FIELDS = ("Company", "Company_dv")


@dataclass
class Sweep:
    """What a run did, in the terms the summary prints."""
    recorded: int = 0
    failed: List[str] = field(default_factory=list)
    resolved: Dict[str, str] = field(default_factory=dict)
    unresolved: List[str] = field(default_factory=list)


def po_numbers(source: str) -> List[str]:
    """Every PO worth recording: the mirror's index, plus anything the pipeline has extracted.

    The union matters. `spitfire_po_index` holds what has been resolved before; `extracted_records`
    holds POs from mail that arrived since, and those are exactly the ones a developer will be
    looking at next — recording only the former guarantees the first offline session hits a miss.
    """
    conn = state_db.get_connection(state_db.path_for(source))
    try:
        rows = conn.execute(
            "SELECT DISTINCT po_number FROM spitfire_po_index "
            " WHERE TRIM(COALESCE(po_number,'')) <> '' "
            "UNION "
            "SELECT DISTINCT po_number FROM extracted_records "
            " WHERE TRIM(COALESCE(po_number,'')) <> '' "
            "ORDER BY po_number").fetchall()
    finally:
        conn.close()
    return [str(r[0]).strip() for r in rows]


def file_keys(source: str) -> List[str]:
    """Catalog keys already posted, so `verify_pod` can re-check a hash offline."""
    conn = state_db.get_connection(state_db.path_for(source))
    try:
        rows = conn.execute(
            "SELECT pod_file_key FROM spitfire_post WHERE TRIM(COALESCE(pod_file_key,'')) <> '' "
            "UNION "
            "SELECT report_file_key FROM spitfire_post "
            " WHERE TRIM(COALESCE(report_file_key,'')) <> ''").fetchall()
    finally:
        conn.close()
    return [str(r[0]).strip() for r in rows]


def sweep(pos: Sequence[str], source: str, quiet: bool = False) -> Sweep:
    """Issue every call the application makes, with the cassette adapter recording underneath.

    One client for the whole run rather than one per PO: the cookie is the expensive part and
    reusing the session is what keeps a 28-PO sweep to a couple of minutes.
    """
    result = Sweep()
    client = SpitfireReadClient()

    def say(message: str) -> None:
        if not quiet:
            print(message, flush=True)

    # Preflight, and not an optional one. `_ensure_session` asks `has_session()` before every PO
    # read, and that call is recorded like any other — so recording while the cookie has already
    # lapsed banks `false`, and every later offline session then dies on "the supplied sfPMSAuth
    # cookie has expired", which is a baffling thing to be told by a machine with no network. The
    # store would look full and be worthless. Better to refuse to write it.
    if client.login_mode and spitfire_cassette.mode() != spitfire_cassette.REPLAY:
        client.ensure_session()
    if not client.has_session():
        raise SystemExit(
            "Spitfire says this session is not live, so there is nothing worth recording. "
            "Set SPITFIRE_UID/SPITFIRE_PW in .env (or capture a fresh sfPMSAuth cookie as "
            "SPITFIRE_SESSION_COOKIE) and run this again.")

    say(f"recording into {settings.SPITFIRE_CASSETTE_DIR}")
    say(f"mode {spitfire_cassette.mode()}  ·  {len(pos)} purchase orders")

    # Session-level reads first. They are cheap, they are called on every run of the real app, and
    # a replay session that cannot answer `has_session` fails before it reaches anything useful.
    for label, call in (("account/session", client.has_session),
                        ("session/who", client.whoami),
                        ("system/version", client.server_version),
                        ("projects", client.list_project_ids)):
        try:
            call()
            result.recorded += 1
        except Exception as exc:                       # noqa: BLE001 — one dead endpoint is not the run
            result.failed.append(f"{label}: {exc}")
            say(f"  ! {label}: {exc}")

    for po_number in pos:
        try:
            key = client.resolve_po(po_number)
            if not key:
                result.unresolved.append(po_number)
                say(f"  - {po_number}: not found in {', '.join(settings.SPITFIRE_PROJECT_IDS)}")
                continue
            # `read_po` is the four calls the app makes — header, items, addresses, route — and
            # calling it rather than the endpoints directly is what keeps the recording honest:
            # whatever the connector asks for is what lands in the store.
            doc = client.read_po(key)
            result.resolved[po_number] = key
            result.recorded += 5
            say(f"  + {po_number}  {len(doc.lines)} lines  {doc.vendor_name[:34]}")
        except Exception as exc:                       # noqa: BLE001
            result.failed.append(f"PO {po_number}: {exc}")
            say(f"  ! {po_number}: {exc}")

    for key in file_keys(source):
        try:
            client._get_json(f"/api/catalog/{key}/versions")        # noqa: SLF001
            result.recorded += 1
        except Exception as exc:                       # noqa: BLE001
            result.failed.append(f"catalog {key[:8]}: {exc}")

    return result


def verify(pos: Sequence[str], source: str) -> int:
    """Read live and compare against what is stored. Returns the number that drifted.

    Staleness is the failure mode this whole feature invites: a quantity moves in Spitfire, the
    cassette does not, and a screen shows a confident wrong number. This is how you find out on
    purpose rather than by being surprised.
    """
    store = spitfire_cassette.CassetteStore()
    was = settings.SPITFIRE_CASSETTE_MODE
    settings.SPITFIRE_CASSETTE_MODE = "off"            # read live, do not overwrite
    try:
        client = SpitfireReadClient()
        drifted = 0
        for po_number in pos:
            key = client.resolve_po(po_number)
            if not key:
                continue
            for suffix in ("", "/items", "/addresses", "/route"):
                path = f"/api/document/{key}{suffix}"
                stored = store.lookup("GET", path)
                if stored is None:
                    print(f"  ? {po_number}{suffix or ' (header)'}: nothing recorded")
                    continue
                live = client._request("GET", path).content        # noqa: SLF001
                if live != stored.body:
                    drifted += 1
                    print(f"  ~ {po_number}{suffix or ' (header)'}: changed since "
                          f"{stored.recorded_at[:10]}")
        return drifted
    finally:
        settings.SPITFIRE_CASSETTE_MODE = was


def _scrub(value: object) -> object:
    """Replace personal and vendor identifiers, in place, at any depth.

    Only names, companies and addresses. Everything the matcher reads — `SourceItemNumber`,
    `ItemQuantity`, `DocItemNumber`, `UOM`, `ProjEntity` — is left exactly as recorded, because a
    fixture that has been tidied is a fixture that stops proving anything.
    """
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for name, inner in value.items():
        if name in _PERSON_FIELDS and isinstance(inner, str) and inner.strip():
            out[name] = "Redacted Person" if "@" not in inner else "person@example.invalid"
        elif name in _COMPANY_FIELDS and isinstance(inner, str) and inner.strip():
            out[name] = "Redacted Vendor"
        else:
            out[name] = _scrub(inner)
    return out


def promote() -> int:
    """Copy the four fixture POs' cassettes into `tests/fixtures/spitfire/`, scrubbed.

    Copied rather than moved: the local store stays complete, and the committed subset is a
    deliberately small thing that CI and a fresh clone can run the offline path against.
    """
    store = spitfire_cassette.CassetteStore()
    client_keys = _keys_for_fixture_pos(store)
    if not client_keys:
        print("no cassettes for the fixture POs — run the sweep first")
        return 0

    FIXTURES.mkdir(parents=True, exist_ok=True)
    copied = 0
    for path in client_keys:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        body = bytes.fromhex(envelope.get("body_hex", ""))
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            parsed = None
        if parsed is not None:
            envelope["body_hex"] = json.dumps(_scrub(parsed)).encode("utf-8").hex()
            envelope["scrubbed"] = True
        (FIXTURES / path.name).write_text(json.dumps(envelope, indent=1), encoding="utf-8")
        copied += 1

    (FIXTURES / "README.md").write_text(
        "# Recorded Spitfire responses (committed subset)\n\n"
        "Written by `tools/spitfire_record_cassettes.py --promote`. Personal and vendor names are\n"
        "scrubbed; spec codes, quantities, line numbers, units and cost codes are exactly as\n"
        "recorded, because those are what the tests are about.\n\n"
        "The full local store is `state/spitfire_cassettes/` and is gitignored.\n\n"
        + "".join(f"- **{po}** — {why}\n" for po, why in FIXTURE_POS.items()),
        encoding="utf-8")
    return copied


def _keys_for_fixture_pos(store: spitfire_cassette.CassetteStore) -> List[Path]:
    """Cassettes belonging to the fixture POs — the four document reads each, plus the searches
    that resolved them. Matched on the stored `path` and body rather than on a filename, which is
    a hash and says nothing."""
    if not store.root.exists():
        return []
    conn = state_db.get_connection(state_db.path_for(state_db.STORE_MAILBOX))
    try:
        wanted = {
            str(r[0]): str(r[1]) for r in conn.execute(
                f"SELECT po_number, doc_master_key FROM spitfire_po_index "
                f"WHERE po_number IN ({','.join('?' * len(FIXTURE_POS))})",
                tuple(FIXTURE_POS)).fetchall()}
    finally:
        conn.close()
    if not wanted:
        return []
    keys = set(wanted.values())
    out = []
    for path in sorted(store.root.glob("*.json")):
        envelope = json.loads(path.read_text(encoding="utf-8"))
        stored_path = str(envelope.get("path", ""))
        if any(k and k in stored_path for k in keys):
            out.append(path)
        elif stored_path in ("/api/projects", "/api/account/session", "/api/session/who"):
            out.append(path)                     # the session reads every replay run needs
    return out


def status() -> None:
    store = spitfire_cassette.CassetteStore()
    print(f"mode      {spitfire_cassette.mode()}")
    print(f"store     {store.root}")
    print(f"cassettes {store.count()}")
    if store.root.exists():
        size = sum(p.stat().st_size for p in store.root.glob("*.json"))
        print(f"size      {size / 1_048_576:.1f} MB")
    print(f"fixtures  {len(list(FIXTURES.glob('*.json'))) if FIXTURES.exists() else 0} "
          f"in {FIXTURES.relative_to(ROOT)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--po", action="append", help="record only this PO; repeatable")
    parser.add_argument("--source", default=state_db.STORE_MAILBOX,
                        choices=(state_db.STORE_MAILBOX, state_db.STORE_SAMPLE))
    parser.add_argument("--verify", action="store_true", help="read live and diff against the store")
    parser.add_argument("--promote", action="store_true",
                        help="copy the fixture POs into tests/fixtures/spitfire, scrubbed")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    if args.status:
        status()
        return 0
    if args.promote:
        print(f"promoted {promote()} cassettes into {FIXTURES.relative_to(ROOT)}")
        return 0

    pos = args.po or po_numbers(args.source)
    if not pos:
        print("no purchase orders to record — is the state store empty?")
        return 1

    if args.verify:
        settings.SPITFIRE_CASSETTE_MODE = "off"
        drifted = verify(pos, args.source)
        print(f"\n{drifted} recorded response(s) no longer match live"
              if drifted else "\nevery recorded response still matches live")
        return 1 if drifted else 0

    # `record`, not `auto`: a refresh must overwrite what is there, or a stale body would be
    # replayed for ever because a hit short-circuits the call that would replace it.
    settings.SPITFIRE_CASSETTE_MODE = "record"
    result = sweep(pos, args.source)

    print(f"\nrecorded {result.recorded} response(s) into {settings.SPITFIRE_CASSETTE_DIR}")
    if result.unresolved:
        print(f"not found in Spitfire: {', '.join(result.unresolved)}")
    if result.failed:
        print(f"{len(result.failed)} call(s) failed:")
        for line in result.failed[:20]:
            print(f"  {line}")
    print("\nnext: set SPITFIRE_CASSETTE_MODE=replay in .env and work with no network.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
