"""Read, restore and dispatch the approval route on an existing CC-TEST receipt.

    python -m tools.spitfire_route_dispatch --doc <DocMasterKey>
    python -m tools.spitfire_route_dispatch --doc <DocMasterKey> --restore-routees --i-understand-this-writes
    python -m tools.spitfire_route_dispatch --doc <DocMasterKey> --dispatch --i-understand-this-writes
    python -m tools.spitfire_route_dispatch --doc <DocMasterKey> --set-status pod-confirmed --i-understand-this-writes

**This module can never issue a DELETE.** `NoDeleteHarness` raises on the verb itself, before a
socket is opened, so no code path here — present or future — can remove a routee, an attachment or
a document. That is a hard guard, not a convention: an earlier version of this script reached
dispatch-without-notifying-Premier by *stripping* the staged approvers, which removed three real
people from a receipt's route and destroyed their `RouteID`s. Removing people is not an acceptable
way to achieve quiet testing.

The consequence is deliberate and worth stating: **there is no way to dispatch to only ourselves.**
`route/apply` sends to whoever is staged. So either dispatch knowingly to the whole chain, or do not
dispatch — adjusting who is on a route belongs in the Spitfire UI, to a person.

What the route data says, measured across 4,602 mirrored route rows (counting Spitfire's
`0001-01-01T00:00:00` sentinel as absent, which is the trap — as a non-empty string it reads truthy
and inverts the result):

* **`Reached` is the gate for `Responded`, not `Alerted`.** All 191 rows offering `C,D,P,G` have
  neither timestamp. `A,H,P` — the set containing `Responded` — appears on 19 rows that are reached
  but not alerted. `Alerted` only adds `R` Restarted and `B` Sent Back (106 rows).
* So **only the stop the route has actually reached can be responded to.** Downstream stops showing
  no approve option is correct, and dispatching does not change it.

**Setting the document's `Status` is an operator action behind `--set-status`, never an automatic
one.** `PATCH /api/document/{id}/Status` with `"A"` marks a receipt POD Confirmed with no review and
no Fixed-Asset Accounting sign-off — measured on PO 212456, where a receipt that had never been
routed came back `Status_dv "POD Confirmed"`. It exists here because a `CC-TEST … DO NOT PROCESS`
receipt cannot be closed out any other way: our own stops sign off, and then the header sits at
`In Process` forever waiting on sequence 10, which is three real Premier employees nobody intends
to bother with a test document.

So it is one typed flag, on one document, on Training, on a `CC-TEST` artefact, and the step records
**which route stops it bypassed** — that cost belongs in the report rather than in somebody's memory.
`A` means two different things either side of this call: on `DocMasterDetail.Status` it is POD
Confirmed, on a `DocRoute` row it is Responded. `tools/spitfire_e2e_test.py` still never touches the
header, and nothing in `pipeline/` calls this.

Guard rails: the base URL must contain `training`; the target's Title must start with `CC-TEST`;
`--i-understand-this-writes` must be typed in full or nothing is sent; and no DELETE, ever.
"""

import argparse
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from config import settings
from connectors import spitfire_auth
from tools.spitfire_e2e_test import REPORTS_DIR, Harness, Run, TEST_MARKER, preflight

RESPONDED = "A"

# `A` on the *document header* is a different field with a different meaning from `A` on a route
# row: `DocMasterDetail.Status` A = "POD Confirmed", `DocRoute.Status` A = "Responded". Named
# separately so a reader of `set_status` is never left guessing which one is in play.
POD_CONFIRMED = "A"

# The approval chain Spitfire stages on a Receipt at creation, captured from CC-TEST receipt
# 1c0a70f5-6f1e-4937-a378-9f74915eab52, which still carries it intact. Used to rebuild a route that
# an earlier version of this script stripped. Stage 1 / sequence 10 / Web, status G "Pending Any" —
# any one of the three clears the step.
PREMIER_ROUTEES = (
    {"UserKey": "4a533d19-42d1-4ea5-a389-e3a9f93a699b", "name": "Cassie Breaux"},
    {"UserKey": "33404380-08a6-45a6-aa82-9c0087cf911a", "name": "Kendyl Shrogin"},
    {"UserKey": "a2f136b9-b77c-4a23-bd3c-cadfd9f0bd7b", "name": "Tina Tran"},
)
ROUTEE_STAGE, ROUTEE_SEQUENCE, ROUTEE_VIA, ROUTEE_STATUS = 1, 10, "W", "G"


class RouteDeleteRefused(RuntimeError):
    """Raised if anything asks this module to issue a DELETE. A programming error, never caught."""


def _is_session_release(path: str) -> bool:
    """`/api/document/{guid}/session` exactly — the commit-and-release call, and nothing else.

    Deliberately strict: four segments, a real GUID in the third, literal `session` in the fourth.
    `/session/changes`, `/session/end`, `/route` and every other path fail it.
    """
    segments = tuple(path.split("?")[0].strip("/").split("/"))
    if len(segments) != 4:
        return False
    a, b, key, tail = segments
    return (a.lower() == "api" and b.lower() == "document" and tail.lower() == "session"
            and len(key) == 36 and key.count("-") == 4
            and all(c in "0123456789abcdefABCDEF-" for c in key))


class NoDeleteHarness(Harness):
    """A harness that cannot delete anything, with one audited exception.

    Checked on the verb before the request is built, so it holds for any path and any future call
    site. `connectors/spitfire_write.py` has its own separate refusal; this one exists because that
    connector is not in this code path.

    **The exception is releasing a document edit session**, `DELETE /api/document/{guid}/session`.
    It removes nothing: it drops an edit lock and *commits* the staged changes — it is sfPMS's
    equivalent of pressing Save, and `POST /session/end` is not a substitute (measured: it returns
    200 and the values read back unchanged). Without it there is no way to write a route response at
    all, since `PUT /api/document/{id}/route` answers **501** on this build. Leaving sessions open
    would also strand edit locks on Premier's documents that nobody can see to clear.

    The carve-out is matched by exact shape, not by substring, so it cannot widen to
    `/session/changes` or to the `/route` delete that caused the earlier incident.
    """

    def call(self, method: str, path: str, **kwargs) -> requests.Response:
        if method.upper() == "DELETE" and not _is_session_release(path):
            raise RouteDeleteRefused(
                f"refused to DELETE {path} - this module deletes nothing from Spitfire except "
                "releasing a document edit session")
        return super().call(method, path, **kwargs)


def _choices(row: Dict[str, Any]) -> str:
    return ",".join(str(c.get("value")) for c in (row.get("StatusChoices") or []))


def _real_date(value: Any) -> bool:
    """Spitfire writes an unset date as `0001-01-01T00:00:00`, not as null. That string is truthy,
    so a plain `bool()` reports every row as reached and silently inverts any conclusion."""
    return bool(value) and not str(value).startswith("0001-01-01")


def _can_respond(row: Dict[str, Any]) -> bool:
    return RESPONDED in _choices(row).split(",")


def _acted_at(h: Harness, route_id: Optional[str]) -> Optional[str]:
    """When this route stop was acted on, read from the per-field audit trail.

    The obvious source — `Acted` on the `GET /route` payload — is **wrong**: it returns null even
    when the column holds a value. The history endpoint is the only honest one. A field it does not
    track answers `200 []`, so an empty result means "never written", not "wrong endpoint".
    """
    if not route_id:
        return None
    rows = h.json_or_none(h.get(f"/api/history/DocRoute/Acted/{route_id}")) or []
    stamps = [str(r.get("NewValue") or "").strip("[]") for r in rows
              if str(r.get("ColName", "")).lower().endswith(".acted") and r.get("NewValue")]
    return stamps[0] if stamps else None


def _route_rows(h: Harness, doc_key: str) -> List[Dict[str, Any]]:
    return h.json_or_none(h.get(f"/api/document/{doc_key}/route")) or []


def _route_table(rows: List[Dict[str, Any]]) -> List[str]:
    out = ["| Seq | To | Status | Choices | Reached | Can respond |",
           "|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda x: (x.get("Stage") or 0, x.get("Sequence") or 0)):
        out.append("| {} | {} | {} | `{}` | {} | {} |".format(
            r.get("Sequence"), r.get("UserName"), r.get("Status"), _choices(r) or "-",
            r.get("Reached") if _real_date(r.get("Reached")) else "-",
            "yes" if _can_respond(r) else "no"))
    return out


def read_header(h: Harness, run: Run, doc_key: str) -> Optional[Dict[str, Any]]:
    """The document, and the CC-TEST assertion that stops this touching a real Premier receipt."""
    s = run.step("1", "read the document and check the CC-TEST marker")
    doc = h.json_or_none(h.get(f"/api/document/{doc_key}"))
    if not isinstance(doc, dict) or not doc.get("DocMasterKey"):
        s.ok = False
        s.detail = "no document returned - note a bad GUID answers 500 here, not 404"
        return None
    title = str(doc.get("Title") or "")
    if not title.strip().startswith(TEST_MARKER):
        s.ok = False
        s.detail = f"REFUSED - title is not a {TEST_MARKER} artefact: {title[:60]!r}"
        return None
    s.ok = True
    s.detail = (f"DocNo {doc.get('DocNo')}, SubContract {doc.get('SubContract')}, "
                f"Status {doc.get('Status')}/{str(doc.get('Status_dv') or '').strip()}")
    return doc


def read_route(h: Harness, run: Run, doc_key: str, number: str,
               name: str) -> List[Dict[str, Any]]:
    s = run.step(number, name)
    rows = _route_rows(h, doc_key)
    reached = [x for x in rows if _real_date(x.get("Reached"))]
    actionable = [x for x in reached if _can_respond(x)]
    s.ok = bool(rows)
    s.detail = "{} routee(s); {} reached; {} can be set Responded (seq {})".format(
        len(rows), len(reached), len(actionable),
        ", ".join(str(x.get("Sequence")) for x in actionable) or "none")
    return rows


def restore_routees(h: Harness, run: Run, doc_key: str,
                    existing: List[Dict[str, Any]]) -> None:
    """Put the staged Premier approvers back onto a route they were stripped from.

    `POST /api/document/{id}/route` inserts one routee and requires `Sequence` — omitting it returns
    *"You must specify Sequence"*. The new rows get fresh `RouteID`s; the originals cannot be
    recovered, which is exactly why the delete should never have happened.

    Skips anyone already present, so re-running cannot duplicate a routee.
    """
    have = {str(x.get("UserKey") or "").lower() for x in existing}
    for i, routee in enumerate(PREMIER_ROUTEES, start=1):
        s = run.step(f"3.{i}", f"restore {routee['name']} at sequence {ROUTEE_SEQUENCE}")
        if routee["UserKey"].lower() in have:
            s.ok = None
            s.detail = "already on the route - skipped"
            continue
        body = {"UserKey": routee["UserKey"], "Stage": ROUTEE_STAGE,
                "Sequence": ROUTEE_SEQUENCE, "RouteVia": ROUTEE_VIA, "Status": ROUTEE_STATUS}
        r = h.write_call(s, "POST", f"/api/document/{doc_key}/route", body)
        if r is None:
            continue
        s.ok = r.ok
        s.detail = f"HTTP {r.status_code}" + ("" if r.ok else f" {r.text[:120]}")


def respond(h: Harness, run: Run, doc_key: str, rows: List[Dict[str, Any]],
            seq: Optional[int] = None, force: bool = False) -> None:
    """Sign off **our own** route step — the thing that has never happened on any receipt we posted.

    Every receipt this system has created sits at sequence 1 waiting on us; until that step is
    responded to, the route never advances and Premier's reviewers at sequence 10 never see it.
    This is the API equivalent of the thumbs-up on that row in sfPMS.

    Scope, deliberately narrow:

    * **Only a row that is ours and `Reached`.** Chosen by `UserKey` and timestamp, never by a
      hard-coded sequence — our account sits at 1, 5 and 15 and which one is live moves over time.
    * **Only where Spitfire says we may**, i.e. the row's `MenuCommands` carries
      `CanEditRouteResponseCode` enabled. If the server does not offer the capability we do not
      invent it.
    * **`Status` only.** Measured on Premier's own completed receipt 0004
      (`4d703b03-497d-4220-96fa-a953715af8f9`), every acted row reads `ResponseCode = None`; only
      `Status` moves to `A`. The `ResponseCode "A"` seen elsewhere was on a *purchase order* route,
      which is a different document type with different conventions.

    Signing our step asserts "the POD is attached and correct". It is **not** approving the receipt —
    sequence 10 is Premier's decision and nothing here touches it.
    """
    s = run.step("3", "sign off our route step (Status -> A)")
    mine = [r for r in rows
            if str(r.get("UserKey") or "").lower() == str(h.user_key).lower()]
    if seq is not None:
        # An explicitly named row. Still ours, still never someone else's.
        ours = [r for r in mine if r.get("Sequence") == seq]
        if not ours:
            s.ok = False
            s.detail = f"REFUSED - seq {seq} is not one of our rows on this document"
            return
    else:
        # `Acted` on this payload is always null - `/route` under-reports it - so it cannot be used
        # to tell a signed row from an unsigned one. `Status` is reported correctly, so that is the
        # test. Without this a second run re-signs the row the first run already signed.
        ours = [r for r in mine
                if _real_date(r.get("Reached")) and r.get("Status") != RESPONDED]
        if not ours:
            s.ok = False
            s.detail = "no row of ours is reached and unacted - nothing to sign"
            return
    row = ours[0]
    allowed = any(m.get("CommandName") == "CanEditRouteResponseCode" and m.get("Enabled")
                  for m in (row.get("MenuCommands") or []))
    if not allowed:
        s.ok = False
        s.detail = f"REFUSED - seq {row.get('Sequence')} does not offer CanEditRouteResponseCode"
        return
    if not _can_respond(row):
        # Spitfire is saying this row cannot move to `A` right now - typically because the route has
        # not reached it. Writing anyway produces a row the UI would never have produced, so it is
        # possible only behind an explicit acknowledgement and is recorded as such in the report.
        if not force:
            s.ok = False
            s.detail = (f"REFUSED - seq {row.get('Sequence')} choices are `{_choices(row)}`, "
                        f"which does not include {RESPONDED}")
            return
        s.name += "  [FORCED - Spitfire did not offer this]"

    # `PUT /api/document/{id}/route` answers 501 on this build - measured 2026-09-11, body
    # `{"ThisStatus":501,"ThisReason":null}`. The edit session is the only route, which is also what
    # sfPMS's own UI does: the thumbs-up stages the change and Save commits it. Same four calls as
    # `connectors/spitfire_write.set_line_quantity`, and the field name is the one sfPMS shows in its
    # own status bar when you open that dropdown: `DocRoute.Status`, keyed by the row's `RouteID`.
    if not h.write:
        s.ok = None
        s.detail = (f"dry run - would set DocRoute.Status=A on seq {row.get('Sequence')} "
                    f"(RouteID {str(row.get('RouteID'))[:8]}) through an edit session")
        return

    h.call("DELETE", f"/api/document/{doc_key}/session")        # drop any inherited session
    opened = h.json_or_none(h.get(f"/api/document/{doc_key}/session?freshenData=true"))
    if not isinstance(opened, str) or len(opened) != 36:
        s.ok = False
        s.detail = f"could not open an edit session: {str(opened)[:120]!r}"
        return

    change = [{"DataMember": "DocRoute", "DataField": "Status",
               "InstanceKey": row.get("RouteID"), "Data": RESPONDED, "IsURIEncoded": False}]
    patched = h.call("PATCH", f"/api/document/{doc_key}/session/changes", json=change)
    # The release commits, so it runs even if the PATCH complained - an abandoned session would
    # strand an edit lock on a Premier document that nobody can see to clear.
    released = h.call("DELETE", f"/api/document/{doc_key}/session?sessionID={opened}")
    s.ok = patched.ok and released.ok
    # The 200 body is a table -> change-counter map. It is the only immediate signal of whether
    # sfPMS actually counted the change, and `/route` is too unreliable to substitute for it:
    # measured on PO 212560, the audit trail recorded Status [P]->[A] while the route payload went
    # on reporting `P`. Always record it.
    s.detail = ("seq {} RouteID {} -> PATCH {} {} / release {}".format(
        row.get("Sequence"), str(row.get("RouteID"))[:8], patched.status_code,
        (patched.text or "")[:150].replace("\n", " "), released.status_code))


def verify_signed(h: Harness, run: Run, doc_key: str, doc_before: Dict[str, Any]) -> None:
    """Did it actually commit, and did the route move?

    **A 2xx from the PUT proves nothing** — empty-body PUTs on `/route` return 204 whether they did
    anything or not, and sfPMS's own UI stages this change in an edit session that only commits on
    Save, so an uncommitted write reads back completely unchanged. `Acted` becoming non-null is the
    only evidence that the response landed.
    """
    rows = _route_rows(h, doc_key)
    ours = [r for r in rows if str(r.get("UserKey") or "").lower() == str(h.user_key).lower()]

    # `Acted` must come from the audit trail, NOT from the route payload. `GET /route` returns
    # `Acted: null` even when the column holds a value - measured on PO 206993 seq 1, where
    # `/route` said null while `/api/history/DocRoute/Acted/{RouteID}` showed the timestamp written
    # in the same transaction as the status. Trusting the payload produced two consecutive false
    # negatives ("NOT COMMITTED", then "workflow not fired") on writes that had fully succeeded.
    # `Status` on the payload *is* reported correctly, so only `Acted` needs the extra call.
    s = run.step("4", "read back: did the response land?")
    responded = [r for r in ours if r.get("Status") == RESPONDED]
    if not responded:
        s.ok = False
        s.detail = ("NOT COMMITTED - no row of ours reads Status={}. Note an uncommitted edit "
                    "session reads back completely unchanged".format(RESPONDED))
    else:
        row = responded[-1]
        stamped = _acted_at(h, row.get("RouteID"))
        s.ok = bool(stamped)
        s.detail = ("seq {} Status={} Acted={} (from the audit trail; /route under-reports it)"
                    .format(row.get("Sequence"), row.get("Status"), stamped)
                    if stamped else
                    "seq {} reads Status={} but the audit trail shows no Acted - the value was "
                    "written without being treated as a response".format(
                        row.get("Sequence"), row.get("Status")))

    adv = run.step("5", "did the route advance to the next stop?")
    live = [r for r in rows if _real_date(r.get("Reached")) and not r.get("Acted")]
    adv.ok = None
    adv.detail = ("reached: " + ", ".join(
        f"seq {r.get('Sequence')} {r.get('UserName')} (`{_choices(r)}`)" for r in live)
        if live else "no unacted stop is reached - route did not advance on its own")

    hdr = run.step("6", "read back: the document Status was not touched")
    after = h.json_or_none(h.get(f"/api/document/{doc_key}")) or {}
    hdr.ok = doc_before.get("Status") == after.get("Status")
    hdr.detail = "Status {} -> {} ({})".format(
        doc_before.get("Status"), after.get("Status"),
        str(after.get("Status_dv") or "").strip())


def perform(h: Harness, run: Run, doc_key: str) -> None:
    """`PUT /route/perform` — ask the ATC service to actually carry out the route's actions.

    Setting `DocRoute.Status` to `A` through the edit session writes the field but does **not**
    complete the response: measured on PO 206993, the row read `Status A` with `Acted` still null
    and the next stop never reached. sfPMS evidently treats the value and the workflow as separate,
    and this is the call that runs the workflow — *"asks the ATC service to perform any route
    actions (emails or workflows etc) for the current document route"*.

    **It reaches people.** `connectors/spitfire_write.py` refuses it by name for that reason, and so
    does `tools/spitfire_write_probe.py` ("dispatches the approval chain — EMAILS REAL PREMIER
    STAFF"). It is available here only behind its own flag, on a `CC-TEST` document, on Training.
    Whoever is staged on the route is who gets notified — there is no selective form, and the fix
    for "the wrong people are on it" is to correct the route in sfPMS, never to delete them.
    """
    s = run.step("5", "perform route actions (route/perform) - NOTIFIES WHOEVER IS STAGED")
    r = h.write_call(s, "PUT", f"/api/document/{doc_key}/route/perform")
    if r is None:
        return
    s.ok = r.ok
    s.detail = f"HTTP {r.status_code}" + (f" {r.text[:150]}" if r.text.strip() else " (no body)")


def dispatch(h: Harness, run: Run, doc_key: str) -> None:
    """`route/apply` with `NoChange` — dispatch the route exactly as it stands.

    **This notifies everyone currently staged.** There is no selective form; the route is the list.
    `NoChange` is the only safe effect here, since anything that re-applies a configured rule would
    rewrite the very list the caller just reviewed.
    """
    s = run.step("4", "dispatch the route (route/apply) - NOTIFIES EVERYONE STAGED")
    r = h.write_call(s, "POST", f"/api/document/{doc_key}/route/apply?effectList=NoChange")
    if r is None:
        return
    s.ok = r.ok
    s.detail = f"HTTP {r.status_code}" + ("" if r.ok else f" {r.text[:150]}")


def set_status(h: Harness, run: Run, doc_key: str, doc_before: Dict[str, Any],
               rows: List[Dict[str, Any]]) -> None:
    """`PATCH /api/document/{id}/Status` with `"A"` — mark the receipt **POD Confirmed**.

    This does not complete the route; it steps over it. Sequence 10 stays exactly as it was, and
    Premier's reviewers are neither notified nor recorded as having approved anything. That is the
    point on a `CC-TEST` document and would be indefensible on a real one, which is why the guards
    that got us here — Training, `CC-TEST` title, one `--doc`, the acknowledgement flag — are not
    optional and why the bypassed stops are listed in the report.

    **A 200 proves nothing on this API**, so the value is read back from the document itself.
    """
    s = run.step("8", "set the document Status -> POD Confirmed  [BYPASSES THE ROUTE]")
    before = str(doc_before.get("Status") or "")
    if before == POD_CONFIRMED:
        s.ok = None
        s.detail = "already {} ({}) - nothing to do".format(
            before, str(doc_before.get("Status_dv") or "").strip())
        return

    # Whoever has not acted is who this write is stepping over. Reported by sequence and name.
    pending = [r for r in sorted(rows, key=lambda x: (x.get("Stage") or 0, x.get("Sequence") or 0))
               if r.get("Status") != RESPONDED]
    bypassed = ", ".join(f"seq {r.get('Sequence')} {r.get('UserName')}" for r in pending) or "none"

    r = h.write_call(s, "PATCH", f"/api/document/{doc_key}/Status", POD_CONFIRMED)
    if r is None:
        s.detail += f" (would bypass: {bypassed})"
        return
    s.ok = r.ok
    s.detail = "HTTP {}{} - bypassed: {}".format(
        r.status_code, "" if r.ok else f" {r.text[:150]}", bypassed)

    # The read-back is the only evidence. `Status_dv` is the display value sfPMS shows in its own
    # dropdown, so it is what a person looking at the page will see.
    v = run.step("9", "read back: did the Status actually change?")
    after = h.json_or_none(h.get(f"/api/document/{doc_key}")) or {}
    landed = str(after.get("Status") or "")
    v.ok = landed == POD_CONFIRMED
    v.detail = "Status {} -> {} ({})".format(
        before or "(none)", landed or "(none)", str(after.get("Status_dv") or "").strip() or "-")


def write_report(run: Run, doc_key: str, write: bool,
                 before: List[Dict[str, Any]], after: List[Dict[str, Any]]) -> str:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = REPORTS_DIR / f"Spitfire_Route_Dispatch_{stamp}.md"
    lines = [
        f"# Spitfire route - {stamp}", "",
        f"- Instance: `{settings.SPITFIRE_BASE_URL}`",
        f"- Mode: **{'WRITE' if write else 'read-only'}**",
        f"- Document: `{doc_key}`", "",
        "This tool never issues a DELETE. `Responded` belongs to the reviewer, and only the stop",
        "the route has *reached* can be set to it. The document `Status` moves only when a person",
        "types `--set-status`; that write bypasses the route, and step 8 names who it stepped over.",
        "", "## Steps", "", "| # | Step | Result | Detail |", "|---|---|---|---|",
    ]
    lines += [f"| {s.number} | {s.name} | **{s.mark}** | {s.detail} |" for s in run.steps]
    if before:
        lines += ["", "## Route before", ""] + _route_table(before)
    if after:
        lines += ["", "## Route after", ""] + _route_table(after)
    lines += ["", "## Requests issued", "", "```"] + run.requests_made + ["```", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    # `--doc` is deliberately a single key with no batch, glob or "every stuck receipt" form. One
    # purchase order per invocation, typed by a person who has read the previous result. The tool
    # that preceded this one did something broad off a narrowly-approved instruction and removed
    # three real people from a route; the cost of that is why this stays one at a time.
    p.add_argument("--doc", required=True, help="DocMasterKey of ONE CC-TEST receipt")
    p.add_argument("--respond", choices=["approved"],
                   help="sign off OUR OWN reached route step (Status -> A)")
    p.add_argument("--seq", type=int,
                   help="target this sequence instead of the reached one (must still be our row)")
    p.add_argument("--force-unoffered", action="store_true",
                   help="write Status=A even where Spitfire does not offer it - produces a row the "
                        "UI would not produce; use only to probe, never as normal operation")
    p.add_argument("--perform", action="store_true",
                   help="run the route's actions - NOTIFIES WHOEVER IS STAGED ON IT")
    p.add_argument("--restore-routees", action="store_true",
                   help="re-add the staged Premier approvers at sequence 10")
    p.add_argument("--dispatch", action="store_true",
                   help="send the route - NOTIFIES EVERYONE ON IT")
    # Spelled out as a value rather than a bare switch: the one thing this sets is the one thing
    # the SoW reserves for a human, so the operator types what it is going to become.
    p.add_argument("--set-status", choices=["pod-confirmed"],
                   help="set the document header Status to A / POD Confirmed - BYPASSES THE ROUTE "
                        "and its Fixed-Asset Accounting sign-off; operator use on CC-TEST only")
    p.add_argument("--i-understand-this-writes", action="store_true",
                   help="required for --respond, --restore-routees, --dispatch or --set-status")
    args = p.parse_args(argv)

    cookie = spitfire_auth.auth_ticket_value(settings.SPITFIRE_BASE_URL)
    if not cookie:
        print("No Spitfire credentials: set SPITFIRE_UID and SPITFIRE_PW in .env.", file=sys.stderr)
        return 2

    wants_write = (bool(args.respond) or args.perform or args.restore_routees or args.dispatch
                   or bool(args.set_status))
    write = bool(args.i_understand_this_writes) and wants_write
    run = Run()
    h = NoDeleteHarness(settings.SPITFIRE_BASE_URL, cookie, write, run)

    print("mode: {}\n".format(
        "WRITE" if write else ("DRY RUN - pass --i-understand-this-writes to act"
                               if wants_write else "READ-ONLY")))

    before: List[Dict[str, Any]] = []
    after: List[Dict[str, Any]] = []
    if preflight(h, run):
        doc = read_header(h, run, args.doc)
        if doc:
            before = read_route(h, run, args.doc, "2", "read the route as it stands")
            if args.respond:
                respond(h, run, args.doc, before, seq=args.seq, force=args.force_unoffered)
            if args.perform:
                perform(h, run, args.doc)
            if (args.respond or args.perform) and write:
                verify_signed(h, run, args.doc, doc)
            if args.restore_routees:
                restore_routees(h, run, args.doc, before)
            if args.dispatch:
                dispatch(h, run, args.doc)
            if args.set_status:
                # Last, deliberately: the route is read and signed before the header is moved, so
                # step 8 reports the stops as they stand at the moment of the bypass.
                set_status(h, run, args.doc, doc, _route_rows(h, args.doc) if write else before)
            if write:
                after = read_route(h, run, args.doc, "7", "read the route back")

    print(f"{'#':<6}{'STEP':<50}{'RESULT':<8}DETAIL")
    print("-" * 120)
    for s in run.steps:
        print(f"{s.number:<6}{s.name[:49]:<50}{s.mark:<8}{s.detail[:58]}")
    print(f"\nrequests issued: {len(run.requests_made)}")
    print(f"report: {write_report(run, args.doc, write, before, after)}")
    return 1 if run.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
