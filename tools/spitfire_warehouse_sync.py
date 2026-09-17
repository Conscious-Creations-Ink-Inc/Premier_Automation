"""Sweep every readable Spitfire endpoint into the local warehouse.

`tools/pull_spitfire_po.py` mirrors the handful of purchase orders the pipeline has seen, keeping
the 32 fields Stage 4 matches on. This sweeps *every* purchase order in every configured project
and keeps everything each endpoint returns, in `state/spitfire_mirror.sqlite3`. See
`pipeline/spitfire_warehouse.py` for the schema and why it is a separate file.

    python -m tools.spitfire_warehouse_sync --preflight      # auth + counts, fetches nothing
    python -m tools.spitfire_warehouse_sync --discover       # enumerate documents, read none
    python -m tools.spitfire_warehouse_sync                  # the full sweep
    python -m tools.spitfire_warehouse_sync --resume         # continue an interrupted one
    python -m tools.spitfire_warehouse_sync --po 206725      # one purchase order
    python -m tools.spitfire_warehouse_sync --from-state     # only POs the pipeline has seen
    python -m tools.spitfire_warehouse_sync --status         # what is in the warehouse
    python -m tools.spitfire_warehouse_sync --discover-mode number    # every PO, no project list
    python -m tools.spitfire_warehouse_sync --refresh-mirror --discover  # fill the app's mirror

**The project list is the ceiling, and it cannot be raised from the API.** Project-scoped discovery
sees only `SPITFIRE_PROJECT_IDS`; `GET /api/projects` is 405 and `POST /api/projects` answers
`200 []` for every filter shape, so there is no way to ask what else exists. That is not a
permission limit — any project code we can name returns its documents in full. `--discover-mode
number` sidesteps it by walking the PO number space through `POST /api/viewable/DocMasterAlt`,
which needs no project at all, and the project codes then arrive on the headers.

**Reads only.** Every request goes through `connectors.spitfire.SpitfireReadClient`, which has no
write methods and whose allowlist refuses anything else before a socket opens. That matters more
here than anywhere else in the codebase: the credential itself is not read-only — sfPMS reports
AdminLevel 31, Read+Insert+Update+Delete+Blanket — so the allowlist is the whole guarantee, and
`sf_call_log` records every request issued so the claim is checkable rather than asserted.

**It will be interrupted.** `SPITFIRE_SESSION_COOKIE` is a hand-copied browser ticket that lapses
on idle, and a full sweep is a few thousand requests over hours. Every unit of work is checkpointed
in `sf_sweep_progress` before the next one starts, so `--resume` picks up where the cookie died
rather than starting over.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests                                                    # noqa: E402

from config import settings                                        # noqa: E402
from connectors.spitfire import SpitfireReadClient                 # noqa: E402
from pipeline import spitfire_warehouse as wh                      # noqa: E402
from pipeline import spitfire_mirror, state_db                     # noqa: E402

REPORTS_DIR = ROOT.parent / "dev_reports"

# The thirteen dialogs, all of which answer 200 and all of which return the same read-only menu
# shape. They describe what a document *could* do, which is how the write phase was researched
# without writing; none of them changes anything.
DIALOGS = ("abstract", "address", "attachable", "attributes", "compliance", "copyable", "dates",
           "exclusive", "inclusions", "items", "link", "routable", "templates")

# Code lists to ask for by name. `/api/choices/{set}/{docType}` 404s on a set that does not exist,
# which makes it an existence probe — so a miss here is information, not a failure. The graph is
# also walked: any `NextSet` a returned row names is queued too.
CHOICE_SEEDS = ("UOM", "CostType", "AccountCategory", "Status", "ItemStatus", "ItemType",
                "Subtype", "ContractType", "PayControl", "RetentionMethod", "LaborClass",
                "MarkupControl", "Priority", "AddrType", "RouteVia", "ResponseCode")

SUGGESTION_SEEDS = (("Vendor", "0"), ("Contact", "0"), ("Project", "0"), ("User", "0"))

UICFG_PARTS = ("DocHeader", "DocItems")


class SessionLapsed(RuntimeError):
    """A 401 mid-sweep. Only a human can fix it, so the run stops rather than thrashing."""


class Call(NamedTuple):
    """One request and what came back, before anything has been written down."""
    path: str
    payload: Any
    status: Optional[int]
    body: bytes
    content_type: str
    elapsed_ms: int
    error: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def say(message: str) -> None:
    print(message, flush=True)


class Sweeper:
    """One run. Owns the connection, the client, and the counters the report is built from."""

    def __init__(self, conn, client: SpitfireReadClient, sweep_id: int, args) -> None:
        self.conn = conn
        self.client = client
        self.sweep_id = sweep_id
        self.args = args
        self.calls = 0
        self.failures: List[str] = []
        self.skipped: List[str] = []

    # --- one call ----------------------------------------------------------

    @staticmethod
    def call(client: SpitfireReadClient, path: str, payload: Any = None) -> Call:
        """One allowlisted read, as a plain record. Touches no database and no shared state.

        Deliberately a staticmethod taking its own client: this is the half that runs on worker
        threads, and both a sqlite connection and a `requests.Session` are single-thread objects.
        `record` below is the other half, and it only ever runs on the calling thread — the same
        split `po_verify.verify_rows` makes for the same reason.
        """
        started = time.monotonic()
        try:
            response = client.read(path, payload)
        except (requests.RequestException, RuntimeError) as exc:
            return Call(path, payload, None, b"", "",
                        int((time.monotonic() - started) * 1000), f"{type(exc).__name__}: {exc}")
        return Call(path, payload, response.status_code, response.content or b"",
                    response.headers.get("Content-Type", ""),
                    int((time.monotonic() - started) * 1000), "")

    def record(self, call: Call) -> Any:
        """Archive and log one call, then return its parsed JSON or None.

        A non-2xx is recorded and returns None rather than raising, because on this API a status
        is rarely a verdict — an unknown code-set name 404s by design, `/status` 400s, and an
        invented document GUID returns 500 with a body identical to a real fault. The one status
        that does mean stop is 401: the ticket is dead and every later call would fail the same
        way.
        """
        verb = "GET" if call.payload is None else "POST"
        digest = ""
        if call.status is not None:
            digest = wh.archive(self.conn, verb, call.path, call.status, call.content_type,
                                call.body, _now())
        self.calls += 1
        wh.log_call(self.conn, self.sweep_id, _now(), verb, call.path, call.payload, call.status,
                    len(call.body), call.elapsed_ms, digest, call.error)

        if call.status == 401:
            raise SessionLapsed(f"401 on {call.path} — the Spitfire session has lapsed")
        if call.error:
            self.failures.append(f"{call.path}: {call.error}")
            return None
        if call.status is None or not (200 <= call.status < 300):
            self.skipped.append(f"{call.path}: HTTP {call.status}")
            return None
        try:
            return json.loads(call.body.decode("utf-8-sig") or "null")
        except (UnicodeDecodeError, ValueError):
            self.skipped.append(f"{call.path}: body is not JSON")
            return None

    def fetch(self, path: str, payload: Any = None) -> Any:
        """Call and record in one step. For the single-threaded passes: reference, discovery."""
        result = self.record(self.call(self.client, path, payload))
        if self.args.sleep:
            time.sleep(self.args.sleep)
        return result

    def _stamp(self, **extra) -> Dict[str, Any]:
        return dict(sweep_id=self.sweep_id, fetched_at=_now(), **extra)

    @staticmethod
    def _rows(payload: Any) -> List[dict]:
        """Endpoints return a bare array, an object, or an object wrapping `Rows`. Normalise."""
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        if isinstance(payload, dict):
            inner = payload.get("Rows")
            if isinstance(inner, list):
                return [r for r in inner if isinstance(r, dict)]
            return [payload]
        return []

    # --- reference data ----------------------------------------------------

    def sweep_reference(self) -> None:
        """Site-wide lookups: the field dictionary, the code lists, the report names.

        Cheap, and they are the vocabulary every other table's codes are written in — a UOM of
        `LT` is unreadable without them.
        """
        say("reference data")
        for part in UICFG_PARTS:
            payload = self.fetch(f"/api/uicfg/live/{part}")
            items = (payload or {}).get("UIItems") if isinstance(payload, dict) else None
            n = wh.save_rows(self.conn, "sf_uicfg_field", self._rows(items),
                             self._stamp(part_name=part))
            say(f"  + uicfg {part:10} {n} fields")

        for report_set in ("32", "0"):
            payload = self.fetch(f"/api/session/reports/{report_set}")
            n = wh.save_rows(self.conn, "sf_report", self._rows(payload),
                             self._stamp(report_set=report_set))
            say(f"  + reports/{report_set:3} {n} named reports")

        doc_type = settings.SPITFIRE_PO_DOC_TYPE_KEY or "1"
        queue = list(CHOICE_SEEDS)
        seen: set = set()
        found = 0
        while queue:
            set_name = queue.pop(0)
            if set_name in seen:
                continue
            seen.add(set_name)
            rows = self._rows(self.fetch(f"/api/choices/{set_name}/{doc_type}"))
            if not rows:
                continue
            found += wh.save_rows(self.conn, "sf_choice", rows,
                                  self._stamp(set_name=set_name, for_doc_type=doc_type))
            # Follow the graph: a code list can name its successor, and those are the sets we
            # would never have guessed.
            for row in rows:
                nxt = str(row.get("NextSet") or "").strip()
                if nxt and nxt not in seen and len(seen) < 200:
                    queue.append(nxt)
        say(f"  + choices    {found} codes across {len(seen)} set names probed")

        for lookup, context in SUGGESTION_SEEDS:
            rows = self._rows(self.fetch(f"/api/suggestions/{lookup}/{context}"))
            wh.save_rows(self.conn, "sf_suggestion", rows,
                         self._stamp(lookup_name=lookup, data_context=context))
        self.conn.commit()

    def sweep_project(self, project: str) -> None:
        rows = self._rows(self.fetch(f"/api/project/{project}/TypeSummary"))
        wh.save_rows(self.conn, "sf_doc_type_summary", rows, self._stamp(project_code=project))
        if self.args.with_cost:
            for suffix, table in (("committed", "sf_project_cost_committed"),
                                  ("transactions", "sf_project_cost_transaction")):
                cost = self._rows(self.fetch(f"/api/project/{project}/cost/{suffix}"))
                n = wh.save_rows(self.conn, table, cost, self._stamp(project_code=project))
                say(f"  + {project} cost/{suffix:12} {n} rows")
        self.conn.commit()

    def sweep_reference_projects(self, projects: Sequence[str]) -> None:
        """TypeSummary (and optionally cost) for the known projects.

        `discover()` gets this as a side effect of looping projects; the number walk does not loop
        projects at all, so it is called explicitly. `sf_doc_type_summary` is what step "sweep every
        document type" reads its type keys from, and it is the independent cross-check on how many
        documents a project should have yielded.
        """
        for project in projects:
            self.sweep_project(project)

    # --- discovery ---------------------------------------------------------

    def doc_type_targets(self, spec: str,
                         projects: Sequence[str]) -> List[Tuple[str, str]]:
        """[(DocTypeKey, name)] to enumerate, from `--doc-types`.

        `all` reads the types from `sf_doc_type_summary`, which `sweep_project` has just filled
        from `GET /api/project/{id}/TypeSummary` — the only place the doc-type list is obtainable.
        `GET /api/configuration/doc-types` cannot settle it: it is a declared stub returning
        `500 … not yet implemented (case 36629)`, and auth changes nothing.

        Types with no documents are dropped, which takes the 60 configured types down to about 25
        and saves ten search calls each for nothing.
        """
        if spec == "po":
            return [(settings.SPITFIRE_PO_DOC_TYPE_KEY, "PO/Contracts")]
        if spec != "all":
            return [(key.strip(), "") for key in spec.split(",") if key.strip()]
        if not projects:
            return []
        marks = ",".join("?" * len(projects))
        rows = self.conn.execute(
            f"SELECT DocTypeKey, MAX(DocType), "
            f"       SUM(COALESCE(cnt_open, 0) + COALESCE(cnt_closed, 0)) AS n "
            f"  FROM sf_doc_type_summary "
            f" WHERE project_code IN ({marks}) AND COALESCE(DocTypeKey, '') != '' "
            f" GROUP BY DocTypeKey HAVING n > 0 ORDER BY n DESC", tuple(projects)).fetchall()
        return [(str(r[0]), str(r[1] or "")) for r in rows]

    def discover(self, projects: Sequence[str],
                 doc_types: str = "po") -> Dict[str, Tuple[str, str]]:
        """Enumerate every document of the wanted types in each project.

        Returns {DocMasterKey: (document number, project)}.

        `DocNoLike` is a *contains* filter and an empty filter body returns nothing at all, so
        there is no "list everything" call. Asking once per digit and unioning the results is
        exhaustive, because every document number contains at least one digit — measured, a
        single `DocNoLike:"2"` finds 609 of the 618 that the union finds.

        Bounded by `SPITFIRE_PROJECT_IDS`, which cannot be grown from the API — see
        `discover_by_number` for the way past that.
        """
        # TypeSummary first: `--doc-types all` reads the type list out of what it stores.
        self.sweep_reference_projects(projects)
        types = self.doc_type_targets(doc_types, projects)
        if not types:
            say("  no document types to enumerate")
            return {}
        if doc_types != "po":
            say(f"  {len(types)} document types: "
                + ", ".join(name or key[:8] for key, name in types[:8])
                + (" ..." if len(types) > 8 else ""))

        found: Dict[str, Tuple[str, str]] = {}
        for project in projects:
            before = len(found)
            for doc_type, name in types:
                for digit in "0123456789":
                    payload = self.fetch(f"/api/project/{project}/docs", {
                        "DocNoLike": digit,
                        "ForDocType": doc_type,
                        "IncludeDocs": True,
                        "IncludeFiles": False,
                        "IncludeClosed": True,
                        "ResultLimit": 5000,
                    })
                    rows = self._rows(payload)
                    wh.save_rows(self.conn, "sf_document_search", rows,
                                 self._stamp(project_code=project))
                    for row in rows:
                        key = str(row.get("DocMasterKey") or "").strip()
                        if key:
                            number = str(row.get("DocNo")
                                         or row.get("SubContract") or "").strip()
                            found[key] = (number, project)
                    self.conn.commit()
            say(f"  + {project:16} {len(found) - before} documents")
        return found

    def discover_by_number(self, low: int, high: int,
                           plan: bool = True) -> Dict[str, Tuple[str, str]]:
        """Enumerate purchase orders by walking the PO *number* space. No project id needed.

        `discover()` above can only see inside `SPITFIRE_PROJECT_IDS`, and that list cannot be
        grown from the API: `GET /api/projects` is 405, `POST /api/projects` answers `200 []` for
        every filter shape, `suggestions/Project` 500s and `choices/Project` 404s. Measured
        2026-09-01, that is not a permission limit — any project code we can *name* returns its
        documents in full — so the missing thing is the list of codes, not access to them.

        `POST /api/viewable/DocMasterAlt` needs no project at all and answers `""` for no match,
        which is a clean negative on an API where 500 does not mean not-found. The number space is
        dense and bounded: 6/6 consecutive numbers present at every band from 120000 to 213000,
        0/6 at 214000 and above. So walking it is exhaustive where the project search is not, and
        every project code we lack arrives for free on the headers this turns up.

        Probes are checkpointed individually, not just the documents they find: a resumed run must
        not re-probe tens of thousands of numbers to rediscover what it already asked.
        """
        # `plan=False` on a resume: work only through what the interrupted run already queued.
        # Re-planning from `--po-range` would silently widen or narrow the original walk depending
        # on what the resuming command line happened to say, and the range is not recorded.
        if plan:
            wh.plan_units(self.conn, self.sweep_id, "number",
                          [str(n) for n in range(low, high)], _now())
        todo = wh.pending(self.conn, self.sweep_id, "number")
        scope = f"{low}-{high}" if plan else "resumed"
        say(f"number-space discovery: {len(todo)} numbers left to probe ({scope})")

        found: Dict[str, Tuple[str, str]] = {}
        batch_size = max(1, self.args.workers)
        for start in range(0, len(todo), batch_size):
            batch = todo[start:start + batch_size]
            for number, key, error in self._probe_batch(batch):
                if error is not None:
                    wh.mark_unit(self.conn, self.sweep_id, "number", number, "FAILED", _now(),
                                 str(error))
                    self.failures.append(f"probe {number}: {error}")
                    continue
                if key:
                    found[key] = (number, "")
                wh.mark_unit(self.conn, self.sweep_id, "number", number, "DONE", _now())
            self.conn.commit()
            done = start + len(batch)
            if done % 500 < batch_size:
                say(f"  · {done}/{len(todo)} probed · {len(found)} purchase orders")
        say(f"  + {len(found)} purchase orders across {len(todo)} numbers")
        return found

    @staticmethod
    def _probe_payload(number: str) -> dict:
        """The `DocMasterAlt` body for one PO number. No project id is involved."""
        return {
            "RequestID": f"probe-{number}",
            "DVName": "DocMasterAlt",
            "MatchingValue": str(number).strip(),
            "DependsOn": [settings.SPITFIRE_PO_DOC_TYPE_KEY, "empty", "empty"],
        }

    def _probe_batch(self, numbers: Sequence[str]):
        """One number per thread. -> [(number, DocMasterKey or None, error)].

        Deliberately routed through `Sweeper.call` / `record` rather than
        `client.resolve_po_alt`, so every probe is archived and logged in `sf_call_log` like any
        other read. A number walk is tens of thousands of requests; leaving them out of the log
        would gut the "we only ever read, and here is the list" guarantee this module rests on —
        the log would show a few hundred document reads and no account of how they were found.

        The response is a bare quoted GUID, or `""` for no match — a clean negative, unlike the
        500-is-not-404 trap elsewhere on this API. Anything else (a fault, a lapsed session) is
        left to `record`, which raises `SessionLapsed` on 401 and returns None otherwise.
        """
        calls = []
        if len(numbers) == 1:
            number = numbers[0]
            calls = [(number, self.call(self.client, "/api/viewable/DocMasterAlt",
                                        self._probe_payload(number)))]
        else:
            with ThreadPoolExecutor(max_workers=len(numbers)) as pool:
                futures = [(n, pool.submit(self.call, SpitfireReadClient(
                    timeout=self.client.timeout), "/api/viewable/DocMasterAlt",
                    self._probe_payload(n))) for n in numbers]
                for number, future in futures:
                    try:
                        calls.append((number, future.result()))
                    except Exception as exc:                  # noqa: BLE001
                        calls.append((number, exc))

        out = []
        for number, call in calls:
            if isinstance(call, Exception):
                out.append((number, None, call))
                continue
            parsed = self.record(call)          # raises SessionLapsed on 401
            # A miss is `200 ""`, so anything that is not a 2xx is an anomaly, not a negative.
            # Reporting it as an error keeps the number FAILED rather than DONE, and a FAILED unit
            # is still pending — so `--resume` asks again instead of skipping that PO forever.
            if call.error or call.status is None or not (200 <= call.status < 300):
                out.append((number, None, call.error or f"HTTP {call.status}"))
                continue
            key = str(parsed or "").strip() if isinstance(parsed, str) else ""
            out.append((number, key or None, None))
            if self.args.sleep:
                time.sleep(self.args.sleep)
        return out

    def resolve_extra(self, po_numbers: Sequence[str]) -> Dict[str, Tuple[str, str]]:
        """POs named on the command line, or seen by the pipeline but absent from the projects.

        `resolve_po_alt` is what makes this work at all: purchase orders genuinely live outside
        `SPITFIRE_PROJECT_IDS`, and the project-scoped search structurally cannot see them.
        """
        out: Dict[str, Tuple[str, str]] = {}
        for number in po_numbers:
            key = None
            try:
                key = self.client.resolve_po_alt(number) or self.client.resolve_po(number)
            except requests.RequestException as exc:
                self.failures.append(f"resolve {number}: {exc}")
            if key:
                out[key] = (number, "")
            else:
                self.skipped.append(f"PO {number}: not found")
        return out

    # --- one document ------------------------------------------------------

    def labels_for(self, key: str) -> List[Tuple[str, str]]:
        """(label, path) for every endpoint one document is read from."""
        out = [("header", f"/api/document/{key}")]
        out += [(s, f"/api/document/{key}/{s}") for s in
                ("items", "addresses", "route", "dates", "attachments", "comments")]
        if not self.args.no_dialogs:
            out += [(f"dialog:{d}", f"/api/document/{key}/dialog/{d}") for d in DIALOGS]
        return out

    def call_document(self, key: str, client: SpitfireReadClient) -> List[Tuple[str, Call]]:
        """Every endpoint for one document, called. Runs on a worker thread, writes nothing."""
        calls = []
        for label, path in self.labels_for(key):
            calls.append((label, self.call(client, path)))
            if self.args.sleep:
                time.sleep(self.args.sleep)
        return calls

    def record_document(self, calls: Sequence[Tuple[str, Call]]) -> Dict[str, Any]:
        """Archive and log a document's calls, on the calling thread. -> {label: parsed body}."""
        data: Dict[str, Any] = {"dialogs": {}}
        for label, call in calls:
            parsed = self.record(call)
            if label.startswith("dialog:"):
                data["dialogs"][label.split(":", 1)[1]] = parsed
            else:
                data[label] = parsed
        return data

    def read_document(self, key: str) -> Dict[str, Any]:
        """Call and record one document, single threaded. Used by `--workers 1` and the tests."""
        return self.record_document(self.call_document(key, self.client))

    def store_document(self, key: str, po_number: str, project: str,
                       data: Dict[str, Any]) -> Tuple[int, str]:
        header = data.get("header")
        if isinstance(header, dict):
            po_number = po_number or str(header.get("DocNo")
                                         or header.get("SubContract") or "").strip()
            project = project or str(header.get("Project") or "").strip()
            # The document's own spelling of its key wins. `resolve_po_alt` hands back an
            # upper-case GUID and the header a lower-case one; the columns collate NOCASE so a
            # join still works either way, but there is no reason to store both spellings.
            key = str(header.get("DocMasterKey") or key).strip() or key
        base = self._stamp(doc_master_key=key, po_number=po_number)

        rows = 0
        rows += wh.save_rows(self.conn, "sf_document", self._rows(header),
                             self._stamp(po_number=po_number, project_code=project))

        items = self._rows(data.get("items"))
        rows += wh.save_rows(self.conn, "sf_document_item", items, base)
        for item in items:
            item_key = str(item.get("DocItemKey") or "").strip()
            if not item_key:
                continue
            child = dict(base, doc_item_key=item_key)
            rows += wh.save_rows(self.conn, "sf_item_task",
                                 self._rows(item.get("DocItemTask")), child)
            # RelatedLineDetails comes back as a bare object on most lines and as a one-element
            # list on others; both mean the same thing.
            rows += wh.save_rows(self.conn, "sf_item_related",
                                 self._rows(item.get("RelatedLineDetails")), child)
            rows += wh.save_rows(self.conn, "sf_item_revision_map",
                                 self._rows(item.get("ItemRevisionMap")), child)

        for suffix, table in (("addresses", "sf_document_address"),
                              ("route", "sf_document_route"),
                              ("dates", "sf_document_date"),
                              ("attachments", "sf_document_attachment"),
                              ("comments", "sf_document_comment")):
            rows += wh.save_rows(self.conn, table, self._rows(data.get(suffix)), base)

        for name, payload in (data.get("dialogs") or {}).items():
            rows += wh.save_rows(self.conn, "sf_document_dialog", self._rows(payload),
                                 dict(base, dialog=name))
        return rows, po_number

    def sweep_documents(self, targets: Dict[str, Tuple[str, str]], refs: Sequence[str]) -> None:
        """Fetch concurrently in batches, write serially, checkpoint after every document."""
        total = len(refs)
        done = 0
        failed = 0
        batch_size = max(1, self.args.workers)
        for start in range(0, total, batch_size):
            batch = list(refs[start:start + batch_size])
            for key, calls, error in self._call_batch(batch):
                number, project = targets.get(key, ("", ""))
                if error is not None:
                    wh.mark_unit(self.conn, self.sweep_id, "document", key, "FAILED", _now(),
                                 str(error))
                    self.failures.append(f"document {key}: {error}")
                    self.conn.commit()
                    failed += 1
                    say(f"  ! [{done + failed:4}/{total}] {number or key[:8]:10} {error}")
                    continue
                rows, number = self.store_document(
                    key, number, project, self.record_document(calls))
                wh.mark_unit(self.conn, self.sweep_id, "document", key, "DONE", _now())
                self.conn.commit()
                done += 1
                say(f"  + [{done + failed:4}/{total}] {number or key[:8]:10} {rows:5} rows")
        if failed:
            say(f"  {failed} documents failed; they stay PENDING for --resume")

    def _call_batch(self, batch: Sequence[str]):
        """One document per thread, each with its own client.

        A `requests.Session` is not safe to share across threads — this is why `po_verify` builds
        one client per task rather than one per worker, and constructing it is only a dict and a
        cookie jar. Nothing here touches the database; the returned calls are recorded by the
        caller, on the caller's thread.
        """
        if len(batch) == 1:
            key = batch[0]
            try:
                return [(key, self.call_document(key, self.client), None)]
            except Exception as exc:                          # noqa: BLE001
                return [(key, [], exc)]
        results = []
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            futures = [(key, pool.submit(self.call_document, key, SpitfireReadClient(
                timeout=self.client.timeout))) for key in batch]
            for key, future in futures:
                try:
                    results.append((key, future.result(), None))
                except Exception as exc:                      # noqa: BLE001
                    results.append((key, [], exc))
        return results

    # --- catalog -----------------------------------------------------------

    def sweep_catalog(self) -> None:
        """Metadata for every attached file the document sweep turned up.

        `DocKey` on an attachment row is a catalog file key on file links and the null GUID on
        document links, so the null is filtered rather than fetched.
        """
        keys = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT DocKey FROM sf_document_attachment "
            "WHERE DocKey IS NOT NULL AND DocKey != '' "
            "  AND DocKey != '00000000-0000-0000-0000-000000000000'").fetchall()]
        wh.plan_units(self.conn, self.sweep_id, "catalog", keys, _now())
        todo = wh.pending(self.conn, self.sweep_id, "catalog")
        say(f"catalog: {len(todo)} files ({len(keys)} attached, the rest already read)")
        for i, key in enumerate(todo, 1):
            stamp = self._stamp(file_key=key)
            try:
                wh.save_rows(self.conn, "sf_catalog_file",
                             self._rows(self.fetch(f"/api/catalog/{key}/meta")), stamp)
                wh.save_rows(self.conn, "sf_catalog_version",
                             self._rows(self.fetch(f"/api/catalog/{key}/versions")), stamp)
                wh.save_rows(self.conn, "sf_catalog_access",
                             self._rows(self.fetch(f"/api/catalog/{key}/AccessHistory")), stamp)
            except SessionLapsed:
                raise
            except Exception as exc:                          # noqa: BLE001
                wh.mark_unit(self.conn, self.sweep_id, "catalog", key, "FAILED", _now(), str(exc))
                self.failures.append(f"catalog {key[:8]}: {exc}")
            else:
                wh.mark_unit(self.conn, self.sweep_id, "catalog", key, "DONE", _now())
            self.conn.commit()
            if i % 50 == 0:
                say(f"  · {i}/{len(todo)}")

    # --- the legacy mirror -------------------------------------------------

    def refresh_mirror(self, source: str,
                       po_numbers: Optional[Sequence[str]] = None) -> int:
        """Project the warehouse into `spitfire_po_index` / `spitfire_po_lines`. **No network.**

        This used to issue four live requests per purchase order through `client.read_po`, which is
        why the mirror held 36 POs while the warehouse held 420: it refreshed whatever a run
        happened to name, one round trip at a time, and any lapse in the session lost the rest. The
        sweep has already written those same four payloads to disk, so re-fetching them is slower
        *and* less complete.

        `spitfire_mirror.refresh_from_warehouse` reassembles them and hands them to the same
        `connectors.spitfire.build_po_document` the live path uses, so the two cannot disagree about
        a quantity. `po_numbers` of None projects everything the warehouse holds.
        """
        state = state_db.get_connection(state_db.path_for(source))
        try:
            # PO-only by default, whatever `--doc-types` pulled in: a receipt's DocNo is `0002`
            # and would otherwise be mirrored as a purchase order of that number.
            pos, lines = spitfire_mirror.refresh_from_warehouse(
                state, self.conn, _now(), po_numbers)
            say(f"  + {lines} lines across {pos} purchase orders")
            return pos
        except Exception as exc:                              # noqa: BLE001
            self.failures.append(f"mirror: {exc}")
            say(f"  ! mirror projection failed: {exc}")
            return 0
        finally:
            state.close()


# --- inputs -------------------------------------------------------------------


def _po_range(text: str) -> Tuple[int, int]:
    """"195000-214000" -> (195000, 214000). Half-open, so the high bound is exclusive."""
    try:
        low, _, high = text.partition("-")
        low, high = int(low.strip()), int(high.strip())
    except ValueError:
        raise SystemExit(f"--po-range wants LOW-HIGH, got {text!r}")
    if high <= low:
        raise SystemExit(f"--po-range {text!r} is empty: high must exceed low")
    return low, high


def po_numbers_from_state(source: str) -> List[str]:
    """Every PO the pipeline has extracted or is accumulating against.

    Both tables, not just `extracted_records`: a delivery whose extraction failed still has an
    `accumulation` row, and those are the POs a person will have to finish by hand.
    """
    conn = state_db.get_connection(state_db.path_for(source))
    try:
        rows = conn.execute(
            "SELECT DISTINCT po_number FROM extracted_records "
            "  WHERE po_number IS NOT NULL AND po_number != '' "
            "UNION "
            "SELECT DISTINCT po_number FROM accumulation "
            "  WHERE po_number IS NOT NULL AND po_number != '' "
            "ORDER BY po_number").fetchall()
    finally:
        conn.close()
    return [str(r[0]).strip() for r in rows if str(r[0] or "").strip()]


def print_status(conn) -> None:
    say("warehouse contents")
    for table, count in sorted(wh.table_counts(conn).items()):
        if count:
            say(f"  {table:32} {count:>9,}")
    say("")
    say("recent sweeps")
    for row in conn.execute(
            "SELECT sweep_id, started_at, finished_at, status, docs_done, docs_failed, "
            "calls_ok, calls_failed FROM sf_sweep ORDER BY sweep_id DESC LIMIT 8").fetchall():
        say(f"  #{row[0]:<4} {row[1]}  {row[3]:<12} docs {row[4] or 0}/{(row[4] or 0) + (row[5] or 0)}"
            f"  calls {row[6] or 0} ok / {row[7] or 0} failed")


def write_report(conn, sweeper: Sweeper, status: str, started: str) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = REPORTS_DIR / f"Spitfire_Warehouse_Sweep_{stamp}.md"
    counts = wh.table_counts(conn)
    by_status = conn.execute(
        "SELECT COALESCE(status, 0), COUNT(*) FROM sf_call_log WHERE sweep_id = ? "
        "GROUP BY 1 ORDER BY 2 DESC", (sweeper.sweep_id,)).fetchall()
    endpoints = conn.execute(
        "SELECT method, COUNT(*), SUM(bytes) FROM sf_call_log WHERE sweep_id = ? "
        "GROUP BY method", (sweeper.sweep_id,)).fetchall()

    lines = [
        f"# Spitfire warehouse sweep #{sweeper.sweep_id}",
        "",
        f"- started `{started}` · finished `{_now()}` · **{status}**",
        f"- host `{settings.SPITFIRE_BASE_URL}`",
        f"- database `{settings.SPITFIRE_WAREHOUSE_DB_PATH}`",
        f"- {sweeper.calls} requests issued",
        "",
        "## Requests by status",
        "",
        "| HTTP | calls |", "| --- | --- |",
    ]
    lines += [f"| {code or 'network error'} | {n} |" for code, n in by_status]
    lines += ["", "| verb | calls | bytes |", "| --- | --- | --- |"]
    lines += [f"| {m} | {n} | {b or 0:,} |" for m, n, b in endpoints]
    lines += ["", "## Rows in the warehouse", "", "| table | rows |", "| --- | --- |"]
    lines += [f"| `{t}` | {c:,} |" for t, c in sorted(counts.items()) if c > 0]

    if sweeper.skipped:
        lines += ["", f"## Non-2xx responses ({len(sweeper.skipped)})", "",
                  "Expected on this API: an unknown code-set name 404s by design, and several",
                  "endpoints are declared stubs. Listed so they are visible, not because they",
                  "are all faults.", ""]
        lines += [f"- `{s}`" for s in sweeper.skipped[:200]]
    if sweeper.failures:
        lines += ["", f"## Failures ({len(sweeper.failures)})", ""]
        lines += [f"- `{f}`" for f in sweeper.failures[:200]]

    lines += ["", "## Read-only", "",
              "Every request above passed `connectors.spitfire.is_allowed` before a socket was",
              "opened, and `sf_call_log` in the warehouse holds the full list. No POST, PUT,",
              "PATCH or DELETE was issued other than the two read-shaped searches sfPMS requires",
              "(`/api/project/{id}/docs`, `/api/viewable/DocMasterAlt`).", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--po", action="append", default=[], metavar="NUMBER",
                        help="sweep this PO only; repeatable")
    parser.add_argument("--from-state", action="store_true",
                        help="sweep the POs the pipeline has seen, instead of every project")
    parser.add_argument("--source", choices=[state_db.STORE_MAILBOX, state_db.STORE_SAMPLE],
                        default=state_db.STORE_MAILBOX,
                        help="which state store --from-state and --refresh-mirror read")
    parser.add_argument("--projects", default="",
                        help="comma-separated project ids (default: SPITFIRE_PROJECT_IDS)")
    parser.add_argument("--discover-mode", choices=("project", "number"), default="project",
                        help="how to find POs: project-scoped search, or walk the PO number space "
                             "(the only route to POs outside SPITFIRE_PROJECT_IDS)")
    parser.add_argument("--doc-types", default="po", metavar="po|all|KEY,KEY",
                        help="which document types project discovery enumerates. 'all' takes them "
                             "from TypeSummary and skips types with no documents "
                             "(default: %(default)s)")
    parser.add_argument("--po-range", default="195000-214000", metavar="LOW-HIGH",
                        help="number range for --discover-mode number. The space is empty above "
                             "214000 and is long-closed history below ~195000 (default: %(default)s)")
    parser.add_argument("--preflight", action="store_true",
                        help="check auth and count what a sweep would fetch, then stop")
    parser.add_argument("--discover", action="store_true",
                        help="enumerate documents and reference data, read no document")
    parser.add_argument("--resume", action="store_true",
                        help="continue the most recent unfinished sweep")
    parser.add_argument("--status", action="store_true", help="print warehouse contents and stop")
    parser.add_argument("--limit", type=int, default=None, help="cap how many documents to read")
    parser.add_argument("--workers", type=int, default=4, help="concurrent document fetches")
    parser.add_argument("--sleep", type=float, default=0.0, help="seconds to pause between calls")
    parser.add_argument("--no-dialogs", action="store_true", help="skip the 13 dialog/* reads")
    parser.add_argument("--no-catalog", action="store_true", help="skip attachment file metadata")
    parser.add_argument("--with-cost", action="store_true",
                        help="also pull project cost/committed and cost/transactions")
    parser.add_argument("--refresh-mirror", action="store_true",
                        help="also refresh spitfire_po_index/po_lines in the state store")
    parser.add_argument("--db", default=None, help="warehouse path (default: settings)")
    parser.add_argument("--no-report", action="store_true", help="skip writing the report file")
    args = parser.parse_args(argv)

    conn = wh.get_connection(Path(args.db) if args.db else settings.SPITFIRE_WAREHOUSE_DB_PATH)
    if args.status:
        print_status(conn)
        return 0

    client = SpitfireReadClient()
    started = _now()

    # Preflight. `/api/system/version` answers anonymously, so a failure here is reachability;
    # `has_session` failing after it is the cookie, and those are different problems.
    try:
        version = client.server_version()
    except Exception as exc:                                  # noqa: BLE001
        say(f"cannot reach {settings.SPITFIRE_BASE_URL}: {exc}")
        return 2
    say(f"host      {settings.SPITFIRE_BASE_URL}  ·  sfPMS {version}")

    if client.login_mode:
        try:
            client.ensure_session()
        except Exception as exc:                              # noqa: BLE001
            say(f"login failed: {exc}")
            return 2
    if not client.has_session():
        say("no session. Set SPITFIRE_UID/SPITFIRE_PW in .env, or recapture SPITFIRE_SESSION_COOKIE")
        say("from the browser (F12 -> Application -> Cookies -> sfPMSAuth), and run again.")
        say("Refusing to sweep: a run started without a session records nothing but 401s.")
        return 2
    who = client.whoami() or {}
    say(f"session   {who.get('FullName') or '?'} <{who.get('EMail') or '?'}>")

    projects = [p.strip() for p in (args.projects or "").split(",") if p.strip()] \
        or list(settings.SPITFIRE_PROJECT_IDS)

    resumed_from = None
    if args.resume:
        resumed_from = wh.resumable_sweep(conn)
        if resumed_from is None:
            say("nothing to resume — no sweep is RUNNING or INTERRUPTED.")
            return 1
        say(f"resuming sweep #{resumed_from}")

    sweep_id = resumed_from or wh.start_sweep(
        conn, started, settings.SPITFIRE_BASE_URL, " ".join(sys.argv[1:]), version,
        str(who.get("UserKey") or ""), str(who.get("EMail") or ""))
    sweeper = Sweeper(conn, client, sweep_id, args)

    status = "DONE"
    try:
        if resumed_from is None:
            sweeper.sweep_reference()

            targets: Dict[str, Tuple[str, str]] = {}
            if args.po:
                targets.update(sweeper.resolve_extra(args.po))
            elif args.from_state:
                targets.update(sweeper.resolve_extra(po_numbers_from_state(args.source)))
            elif args.discover_mode == "number":
                say("discovery")
                sweeper.sweep_reference_projects(projects)
                targets.update(sweeper.discover_by_number(*_po_range(args.po_range)))
            else:
                say("discovery")
                targets.update(sweeper.discover(projects, args.doc_types))
                # POs the pipeline has seen that no configured project lists — they exist, they
                # are just somewhere else, and DocMasterAlt is how they are reachable at all.
                known = {n for n, _ in targets.values()}
                extra = [n for n in po_numbers_from_state(args.source) if n not in known]
                if extra:
                    say(f"  · {len(extra)} seen POs outside the configured projects")
                    targets.update(sweeper.resolve_extra(extra))

            wh.plan_units(conn, sweep_id, "document", targets, _now())
        else:
            targets = {}
            # A number walk is thousands of probes and will be cut off. `final_status` keeps such
            # a run INTERRUPTED so it can be found again, but a resume that only drained the
            # document queue would leave the rest of the number space permanently unasked — the
            # documents it would have found are not in the to-do list, so nothing else ever looks.
            if wh.pending(conn, sweep_id, "number"):
                say("discovery (resuming an unfinished number walk)")
                targets.update(sweeper.discover_by_number(0, 0, plan=False))
                wh.plan_units(conn, sweep_id, "document", targets, _now())

        refs = wh.pending(conn, sweep_id, "document")
        # A resumed run has the to-do list but not the {key: (po, project)} map; the header itself
        # carries both, so `store_document` fills them in.
        targets = targets or {}
        if args.limit:
            refs = refs[:args.limit]

        say(f"documents: {len(refs)} to read")
        if args.preflight:
            say("preflight only — nothing fetched.")
            wh.finish_sweep(conn, sweep_id, _now(), "INTERRUPTED", "preflight")
            return 0
        if not args.discover:
            sweeper.sweep_documents(targets, refs)
            if not args.no_catalog:
                sweeper.sweep_catalog()

        if args.refresh_mirror:
            # Only a targeted run narrows the projection. After a full sweep the useful unit is
            # "everything on disk", not the subset this run happened to touch — a resumed run has
            # an empty `targets` and would otherwise project almost nothing.
            numbers = ([n for n, _ in targets.values() if n]
                       if (args.po or args.from_state) else None)
            scope = f"{len(numbers)} POs" if numbers else "the whole warehouse"
            say(f"mirror: projecting {scope} into the {args.source} state store")
            say(f"  + {sweeper.refresh_mirror(args.source, numbers)} purchase orders")

    except SessionLapsed as exc:
        status = "INTERRUPTED"
        say("")
        say(f"!! {exc}")
        say(f"   Recapture sfPMSAuth and run:  python -m tools.spitfire_warehouse_sync --resume")
    except KeyboardInterrupt:
        status = "INTERRUPTED"
        say("")
        say("interrupted. Resume with:  python -m tools.spitfire_warehouse_sync --resume")

    status, note = wh.final_status(conn, sweep_id, status)
    if note:
        say(f"{note}. Resume with:")
        say("  python -m tools.spitfire_warehouse_sync --resume")
    wh.finish_sweep(conn, sweep_id, _now(), status, note)
    say("")
    print_status(conn)
    if not args.no_report:
        say("")
        say(f"report    {write_report(conn, sweeper, status, started)}")
    return 0 if status == "DONE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
