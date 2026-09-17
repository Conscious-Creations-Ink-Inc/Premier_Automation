"""The warehouse: a column per API field, and a sweep that survives being interrupted.

Two properties are worth proving here and nothing else really is. The first is **fidelity** — a
response goes in and every scalar it carried comes back out of a column, because the whole point
of keeping 116 columns instead of 14 is that nothing is silently dropped. The second is
**resumability**: `SPITFIRE_SESSION_COOKIE` is a hand-copied ticket and a full sweep is a few
thousand requests, so a run that cannot be resumed is a run that never finishes.

The sweeper is driven through a fake client rather than the network. It is the *store* and the
*checkpointing* that are under test; `tests/test_spitfire_cassette.py` already covers the
transport, and a test that needed Premier's office IP would never run.
"""

import json

import pytest
import requests

from config import settings
from pipeline import spitfire_warehouse as wh
from tools import spitfire_warehouse_sync as sync

HEADER = {
    "DocMasterKey": "03cccdd7-32d0-40d9-8dac-ab14f36305c3",
    "DocNo": "206725",
    "Project": "MRC024PB100003",
    "Confidential": False,
    "CostImpact": 1234.56,
    "CurrentSeq": 10,
    "Notes": None,
}
ITEM = {
    "DocItemKey": "aaaaaaaa-0000-0000-0000-000000000001",
    "SourceItemNumber": "EXT-925-AC",
    "ItemQuantity": 0.0,
    "DocItemTask": [{"ItemTaskKey": "tttttttt-0000-0000-0000-000000000001", "Quantity": 2.0}],
    "RelatedLineDetails": {"ContractUnits": 2.0, "ReceivedUnits": 1.0,
                           "ReceiptInProgressUnits": 0.5},
    "ItemRevisionMap": {"DocRevItemKey": "rrrrrrrr-0000-0000-0000-000000000001"},
}


@pytest.fixture
def conn():
    return wh.get_connection(":memory:")


# --- schema -------------------------------------------------------------------


def test_every_declared_api_field_becomes_a_column(conn):
    """The 116-field header is 116 columns, not one JSON blob."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sf_document)")}
    declared = {name for name, _ in wh._api_columns("sf_document")}
    assert declared <= columns
    assert len(declared) > 100, "the header spec was measured at 116 fields"
    assert {"po_number", "project_code", "sweep_id", "fetched_at", "raw_sha256"} <= columns


def test_numeric_fields_keep_numeric_affinity(conn):
    """`WHERE ContractUnits > ReceivedUnits` has to work arithmetically, not lexically."""
    wh.save_rows(conn, "sf_item_related",
                 [{"ContractUnits": 10.0, "ReceivedUnits": 2.0}], {"doc_item_key": "I1"})
    value = conn.execute("SELECT ContractUnits FROM sf_item_related").fetchone()[0]
    assert isinstance(value, float)
    assert conn.execute(
        "SELECT COUNT(*) FROM sf_item_related WHERE ContractUnits > ReceivedUnits"
    ).fetchone()[0] == 1


def test_reserved_words_survive_as_column_names(conn):
    """sf_catalog_file really does have a column called `From`. Unquoted DDL fails on it —
    that is why every generated identifier is quoted rather than merely validated."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sf_catalog_file)")}
    assert "From" in columns
    wh.save_rows(conn, "sf_catalog_file", [{"From": "someone"}], {"file_key": "F1"})
    assert conn.execute('SELECT "From" FROM sf_catalog_file').fetchone()[0] == "someone"


def test_widen_adds_a_field_the_schema_never_declared(conn):
    """The server has changed build three times during this project. A new field must land."""
    added = wh._widen(conn, "sf_document_item", ["FieldFromAFutureBuild"])
    assert added == ["FieldFromAFutureBuild"]
    wh.save_rows(conn, "sf_document_item",
                 [{"DocItemKey": "K1", "FieldFromAFutureBuild": 7}], {"doc_master_key": "D1"})
    stored = conn.execute("SELECT FieldFromAFutureBuild FROM sf_document_item").fetchone()[0]
    # No declared affinity means BLOB affinity, so 7 is still an integer rather than "7".
    assert stored == 7 and isinstance(stored, int)


def test_a_hostile_field_name_is_dropped_not_escaped(conn):
    """`_widen` builds DDL from keys the server sent. Anything not an identifier does not get in."""
    assert wh._widen(conn, "sf_document_item", ['x"); DROP TABLE sf_document; --']) == []
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name = 'sf_document'").fetchone()[0] == 1


def test_guid_columns_join_across_casing(conn):
    """DocMasterAlt answers in upper case and the document header in lower. Same purchase order."""
    wh.save_rows(conn, "sf_document", [HEADER], {"po_number": "206725"})
    wh.save_rows(conn, "sf_document_item", [ITEM],
                 {"doc_master_key": HEADER["DocMasterKey"].upper(), "po_number": "206725"})
    assert conn.execute(
        "SELECT COUNT(*) FROM sf_document_item i "
        "JOIN sf_document d ON i.doc_master_key = d.DocMasterKey").fetchone()[0] == 1


# --- writing ------------------------------------------------------------------


def test_booleans_are_stored_so_that_equals_one_matches(conn):
    wh.save_rows(conn, "sf_document", [dict(HEADER, Confidential=True)], {})
    assert conn.execute(
        "SELECT COUNT(*) FROM sf_document WHERE Confidential = 1").fetchone()[0] == 1


def test_resaving_corrects_rather_than_duplicates(conn):
    wh.save_rows(conn, "sf_document", [HEADER], {"po_number": "206725"})
    wh.save_rows(conn, "sf_document", [dict(HEADER, DocNo="206725", CostImpact=99.0)],
                 {"po_number": "206725"})
    rows = conn.execute("SELECT COUNT(*), MAX(CostImpact) FROM sf_document").fetchone()
    assert rows == (1, 99.0)


def test_rows_with_no_key_of_their_own_deduplicate_on_content(conn):
    row = {"RevID": 1, "UserName": "someone", "AccessType": "View"}
    wh.save_rows(conn, "sf_catalog_access", [row], {"file_key": "F1"})
    wh.save_rows(conn, "sf_catalog_access", [row], {"file_key": "F1"})
    assert conn.execute("SELECT COUNT(*) FROM sf_catalog_access").fetchone()[0] == 1


def test_archive_deduplicates_identical_bodies(conn):
    """618 documents return a great many byte-identical `[]`. They are one row."""
    first = wh.archive(conn, "GET", "/api/document/a/comments", 200, "application/json", b"[]", "t1")
    second = wh.archive(conn, "GET", "/api/document/b/comments", 200, "application/json", b"[]", "t2")
    assert first == second
    count, seen = conn.execute(
        "SELECT COUNT(*), MAX(seen_count) FROM sf_raw_response").fetchone()
    assert (count, seen) == (1, 2)
    assert wh.read_archived(conn, first) == b"[]"


# --- the sweeper --------------------------------------------------------------


class FakeClient:
    """Answers the sweeper's reads from a dict of path -> body, and can be made to lapse."""

    def __init__(self, bodies, lapse_after=None):
        self.bodies = bodies
        self.lapse_after = lapse_after
        self.calls = []

    def read(self, path, payload=None):
        self.calls.append(path)
        made = requests.Response()
        if self.lapse_after is not None and len(self.calls) > self.lapse_after:
            made.status_code = 401
            made._content = b'{"ThisReason":"Not authenticated"}'
        else:
            body = self.bodies.get(path)
            made.status_code = 200 if body is not None else 404
            made._content = json.dumps(body).encode() if body is not None else b'{"Message":"no"}'
        made.headers["Content-Type"] = "application/json"
        return made


class Args:
    sleep = 0.0
    workers = 1
    no_dialogs = True
    no_catalog = True
    with_cost = False
    limit = None


def bodies_for(key):
    return {
        f"/api/document/{key}": HEADER,
        f"/api/document/{key}/items": [ITEM],
        f"/api/document/{key}/addresses": [{"DocAddrKey": "A1", "AddrType": "T"}],
        f"/api/document/{key}/route": [{"RouteID": 1, "UserName": "someone"}],
        f"/api/document/{key}/dates": [],
        f"/api/document/{key}/attachments": [{"DocAttachKey": "AT1", "FileName": "pod.pdf"}],
        f"/api/document/{key}/comments": [],
    }


def sweeper_for(conn, bodies, lapse_after=None):
    sweep_id = wh.start_sweep(conn, "now", "http://x", "test")
    return sync.Sweeper(conn, FakeClient(bodies, lapse_after), sweep_id, Args())


def test_one_document_lands_in_every_child_table(conn):
    key = HEADER["DocMasterKey"]
    sweeper = sweeper_for(conn, bodies_for(key))
    sweeper.store_document(key, "206725", "MRC024PB100003", sweeper.read_document(key))

    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in (
        "sf_document", "sf_document_item", "sf_item_task", "sf_item_related",
        "sf_item_revision_map", "sf_document_address", "sf_document_route",
        "sf_document_attachment")}
    assert counts == {"sf_document": 1, "sf_document_item": 1, "sf_item_task": 1,
                      "sf_item_related": 1, "sf_item_revision_map": 1,
                      "sf_document_address": 1, "sf_document_route": 1,
                      "sf_document_attachment": 1}


def test_related_line_details_survives_being_an_object_or_a_list(conn):
    """It arrives as a bare object on most lines and a one-element list on others."""
    key = HEADER["DocMasterKey"]
    as_list = dict(ITEM, DocItemKey="K2",
                   RelatedLineDetails=[ITEM["RelatedLineDetails"]])
    sweeper = sweeper_for(conn, dict(bodies_for(key), **{
        f"/api/document/{key}/items": [ITEM, as_list]}))
    sweeper.store_document(key, "206725", "", sweeper.read_document(key))
    assert conn.execute("SELECT COUNT(*) FROM sf_item_related").fetchone()[0] == 2
    assert conn.execute(
        "SELECT SUM(ContractUnits - ReceivedUnits - ReceiptInProgressUnits) "
        "FROM sf_item_related").fetchone()[0] == 1.0


def test_every_scalar_in_the_response_is_readable_from_a_column(conn):
    """Fidelity. The archived body is the reference; the columns must agree with it."""
    key = HEADER["DocMasterKey"]
    sweeper = sweeper_for(conn, bodies_for(key))
    sweeper.store_document(key, "206725", "", sweeper.read_document(key))

    digest = conn.execute(
        "SELECT body_sha256 FROM sf_call_log WHERE path = ? AND status = 200",
        (f"/api/document/{key}",)).fetchone()[0]
    archived = json.loads(wh.read_archived(conn, digest))

    conn.row_factory = __import__("sqlite3").Row
    stored = conn.execute("SELECT * FROM sf_document").fetchone()
    for field, value in archived.items():
        assert field in stored.keys(), f"{field} has no column"
        expected = (1 if value else 0) if isinstance(value, bool) else value
        assert stored[field] == expected, field


def test_a_404_on_one_endpoint_does_not_lose_the_document(conn):
    """An unknown code set 404s by design and several endpoints are declared stubs. Keep going."""
    key = HEADER["DocMasterKey"]
    bodies = bodies_for(key)
    del bodies[f"/api/document/{key}/attachments"]
    sweeper = sweeper_for(conn, bodies)
    sweeper.store_document(key, "206725", "", sweeper.read_document(key))
    assert conn.execute("SELECT COUNT(*) FROM sf_document").fetchone()[0] == 1
    assert any("attachments" in s for s in sweeper.skipped)


def test_a_401_stops_the_sweep_rather_than_thrashing(conn):
    key = HEADER["DocMasterKey"]
    sweeper = sweeper_for(conn, bodies_for(key), lapse_after=2)
    with pytest.raises(sync.SessionLapsed):
        sweeper.read_document(key)


# --- resuming -----------------------------------------------------------------


def test_a_finished_document_is_not_read_again(conn):
    sweep_id = wh.start_sweep(conn, "now", "http://x", "test")
    wh.plan_units(conn, sweep_id, "document", ["A", "B", "C"], "now")
    wh.mark_unit(conn, sweep_id, "document", "A", "DONE", "now")
    conn.commit()
    assert wh.pending(conn, sweep_id, "document") == ["B", "C"]


def test_a_failed_document_stays_on_the_list(conn):
    """A failure is not a completion — a resumed sweep has to try it again."""
    sweep_id = wh.start_sweep(conn, "now", "http://x", "test")
    wh.plan_units(conn, sweep_id, "document", ["A"], "now")
    wh.mark_unit(conn, sweep_id, "document", "A", "FAILED", "now", "boom")
    conn.commit()
    assert wh.pending(conn, sweep_id, "document") == ["A"]


def test_only_an_unfinished_sweep_is_resumable(conn):
    first = wh.start_sweep(conn, "now", "http://x", "test")
    wh.finish_sweep(conn, first, "now", "DONE")
    assert wh.resumable_sweep(conn) is None

    second = wh.start_sweep(conn, "now", "http://x", "test")
    wh.finish_sweep(conn, second, "now", "INTERRUPTED")
    assert wh.resumable_sweep(conn) == second


def test_a_partial_run_does_not_report_itself_finished(conn):
    """`--limit 8` on a 624-document sweep once marked the whole sweep DONE, which made the
    remaining 616 unresumable — the work was abandoned rather than picked up. A sweep with
    pending units is INTERRUPTED however cleanly it stopped."""
    sweep_id = wh.start_sweep(conn, "now", "http://x", "test")
    wh.plan_units(conn, sweep_id, "document", ["A", "B"], "now")
    wh.mark_unit(conn, sweep_id, "document", "A", "DONE", "now")
    conn.commit()

    status, note = wh.final_status(conn, sweep_id, "DONE")
    assert (status, note) == ("INTERRUPTED", "1 documents still unread")
    wh.finish_sweep(conn, sweep_id, "now", status, note)
    assert wh.resumable_sweep(conn) == sweep_id

    wh.mark_unit(conn, sweep_id, "document", "B", "DONE", "now")
    conn.commit()
    assert wh.final_status(conn, sweep_id, "DONE") == ("DONE", "")


def test_an_unfinished_number_walk_is_not_a_finished_sweep(conn):
    """`--discover-mode number` probes ~19,000 PO numbers and checkpoints each one.

    That walk is the run most likely to be cut off by a lapsed cookie, and guarding only the
    `document` scope would stamp it DONE with most of the number space never asked — the same
    silent abandonment as the `--limit` bug above, one layer earlier. The remaining numbers would
    then never be probed by any later run.
    """
    sweep_id = wh.start_sweep(conn, "now", "http://x", "test")
    wh.plan_units(conn, sweep_id, "number", ["195000", "195001"], "now")
    wh.mark_unit(conn, sweep_id, "number", "195000", "DONE", "now")
    conn.commit()

    status, note = wh.final_status(conn, sweep_id, "DONE")
    assert status == "INTERRUPTED"
    assert "unprobed" in note
    assert wh.resumable_sweep(conn) == sweep_id

    wh.mark_unit(conn, sweep_id, "number", "195001", "DONE", "now")
    conn.commit()
    assert wh.final_status(conn, sweep_id, "DONE") == ("DONE", "")


def test_every_outstanding_scope_is_named_at_once(conn):
    """One run can be short on numbers, documents and catalog files together; the note has to say
    so in one breath, or a reader fixes the first and is surprised by the second."""
    sweep_id = wh.start_sweep(conn, "now", "http://x", "test")
    for scope in ("number", "document", "catalog"):
        wh.plan_units(conn, sweep_id, scope, ["a"], "now")
    conn.commit()
    _, note = wh.final_status(conn, sweep_id, "DONE")
    assert "unprobed" in note and "unread" in note
    assert note.count(",") == 2


def test_replanning_keeps_what_is_already_done(conn):
    """Discovery runs again on a resume; it must not reset the to-do list."""
    sweep_id = wh.start_sweep(conn, "now", "http://x", "test")
    wh.plan_units(conn, sweep_id, "document", ["A", "B"], "now")
    wh.mark_unit(conn, sweep_id, "document", "A", "DONE", "now")
    conn.commit()
    wh.plan_units(conn, sweep_id, "document", ["A", "B", "C"], "now")
    assert wh.pending(conn, sweep_id, "document") == ["B", "C"]


# --- the read-only guarantee --------------------------------------------------


def test_the_sweep_only_calls_allowlisted_reads():
    """Every path the sweeper is capable of asking for passes `is_allowed` before a socket opens."""
    from connectors.spitfire import is_allowed

    key = "03cccdd7-32d0-40d9-8dac-ab14f36305c3"
    paths = [f"/api/document/{key}"]
    paths += [f"/api/document/{key}/{s}" for s in
              ("items", "addresses", "route", "dates", "attachments", "comments")]
    paths += [f"/api/document/{key}/dialog/{d}" for d in sync.DIALOGS]
    paths += [f"/api/catalog/{key}/{s}" for s in ("meta", "versions", "AccessHistory")]
    paths += [f"/api/uicfg/live/{p}" for p in sync.UICFG_PARTS]
    paths += [f"/api/choices/{s}/1" for s in sync.CHOICE_SEEDS]
    paths += [f"/api/suggestions/{n}/{c}" for n, c in sync.SUGGESTION_SEEDS]
    paths += ["/api/session/reports/32", "/api/project/P/TypeSummary",
              "/api/project/P/cost/committed", "/api/project/P/cost/transactions"]
    for path in paths:
        assert is_allowed("GET", path), path
    assert is_allowed("POST", "/api/project/P/docs")
    assert is_allowed("POST", "/api/viewable/DocMasterAlt")
    # and the sweep still cannot write
    assert not is_allowed("POST", f"/api/document/{key}/attachments")
    assert not is_allowed("PATCH", f"/api/document/{key}/Status")
    assert not is_allowed("DELETE", f"/api/document/{key}")


# --- number-space discovery ---------------------------------------------------
# The only way to reach purchase orders outside SPITFIRE_PROJECT_IDS: `POST /api/viewable/
# DocMasterAlt` takes a PO number and no project. What is pinned here is that the walk is
# checkpointed, audited and resumable, because it is tens of thousands of requests against a
# session that lapses.

ALT = "/api/viewable/DocMasterAlt"


def _probe_sweeper(conn, hits, workers=1):
    """A sweeper whose DocMasterAlt answers a key for `hits` and `""` for everything else."""
    class ProbeClient(FakeClient):
        def read(self, path, payload=None):
            self.calls.append(path)
            made = requests.Response()
            made.status_code = 200
            number = str((payload or {}).get("MatchingValue") or "")
            made._content = json.dumps(hits.get(number, "")).encode()
            made.headers["Content-Type"] = "application/json"
            return made

    sweep_id = wh.start_sweep(conn, "now", "http://x", "test")
    args = Args()
    args.workers = workers
    return sync.Sweeper(conn, ProbeClient({}), sweep_id, args)


def test_number_walk_finds_only_the_numbers_that_exist(conn):
    sweeper = _probe_sweeper(conn, {"195001": "KEY-1", "195003": "KEY-3"})
    found = sweeper.discover_by_number(195000, 195005)

    assert found == {"KEY-1": ("195001", ""), "KEY-3": ("195003", "")}
    # A miss is `200 ""`, an ordinary answer — not a failure to retry.
    assert sweeper.failures == []
    assert len(wh.pending(conn, sweeper.sweep_id, "number")) == 0


def test_every_probe_is_written_to_the_call_log(conn):
    """The read-only claim rests on `sf_call_log` listing every request actually issued.

    A walk of 19,000 numbers routed around the log would leave it showing a few hundred document
    reads and no account of how they were found.
    """
    sweeper = _probe_sweeper(conn, {"195001": "KEY-1"})
    sweeper.discover_by_number(195000, 195005)

    logged = conn.execute(
        "SELECT COUNT(*) FROM sf_call_log WHERE path = ? AND sweep_id = ?",
        (ALT, sweeper.sweep_id)).fetchone()[0]
    assert logged == 5
    assert conn.execute(
        "SELECT COUNT(DISTINCT method) FROM sf_call_log WHERE method NOT IN ('GET', 'POST')"
    ).fetchone()[0] == 0


def test_an_interrupted_walk_resumes_where_it_stopped_and_never_re_probes(conn):
    """`plan=False` is what a resume passes: work the queue that is already there.

    Re-planning from `--po-range` would silently widen or narrow the original walk depending on
    what the resuming command line said, and the range is not recorded anywhere.
    """
    sweeper = _probe_sweeper(conn, {"195004": "KEY-4"})
    wh.plan_units(conn, sweeper.sweep_id, "number",
                  [str(n) for n in range(195000, 195005)], "now")
    for number in ("195000", "195001"):
        wh.mark_unit(conn, sweeper.sweep_id, "number", number, "DONE", "now")
    conn.commit()

    found = sweeper.discover_by_number(0, 0, plan=False)

    assert found == {"KEY-4": ("195004", "")}
    # Only the three that were still pending, and nothing outside the original range.
    assert sweeper.client.calls == [ALT] * 3
    assert wh.pending(conn, sweeper.sweep_id, "number") == []


def test_a_probe_that_faults_stays_pending_rather_than_being_skipped(conn):
    """A miss is `200 ""`. Any other status is an anomaly, and marking it DONE would drop that
    purchase order silently and permanently — no later run would ever ask again."""
    class FaultingClient(FakeClient):
        def read(self, path, payload=None):
            self.calls.append(path)
            made = requests.Response()
            number = str((payload or {}).get("MatchingValue") or "")
            made.status_code = 500 if number == "195002" else 200
            made._content = b'{"ThisReason":"An error has occurred."}' if number == "195002" \
                else json.dumps("").encode()
            made.headers["Content-Type"] = "application/json"
            return made

    sweep_id = wh.start_sweep(conn, "now", "http://x", "test")
    sweeper = sync.Sweeper(conn, FaultingClient({}), sweep_id, Args())
    sweeper.discover_by_number(195000, 195004)

    assert wh.pending(conn, sweep_id, "number") == ["195002"]
    assert wh.final_status(conn, sweep_id, "DONE")[0] == "INTERRUPTED"


def test_doc_type_targets_defaults_to_purchase_orders_only(conn):
    sweeper = _probe_sweeper(conn, {})
    assert sweeper.doc_type_targets("po", ["P1"]) == [
        (settings.SPITFIRE_PO_DOC_TYPE_KEY, "PO/Contracts")]


def test_doc_type_targets_all_reads_typesummary_and_drops_empty_types(conn):
    """`--doc-types all` cannot come from the Swagger — `GET /api/configuration/doc-types` is a
    declared stub returning `500 … not yet implemented (case 36629)`. TypeSummary is the only
    source, and skipping the types holding no documents is what takes 60 down to about 14: each
    one it keeps costs ten search calls per project."""
    sweeper = _probe_sweeper(conn, {})
    wh.save_rows(conn, "sf_doc_type_summary", [
        {"DocTypeKey": "T-PO", "DocType": "PO/Contracts", "cnt_open": 278, "cnt_closed": 36},
        {"DocTypeKey": "T-RCPT", "DocType": "Receipt", "cnt_open": 23, "cnt_closed": 145},
        {"DocTypeKey": "T-EMPTY", "DocType": "Never Used", "cnt_open": 0, "cnt_closed": 0},
    ], {"project_code": "P1"})
    wh.save_rows(conn, "sf_doc_type_summary", [
        {"DocTypeKey": "T-RCPT", "DocType": "Receipt", "cnt_open": 0, "cnt_closed": 330},
    ], {"project_code": "P2"})
    conn.commit()

    # Busiest first, counting across every project asked for — Receipt totals 498 to the PO's 314.
    assert sweeper.doc_type_targets("all", ["P1", "P2"]) == [
        ("T-RCPT", "Receipt"), ("T-PO", "PO/Contracts")]
    # A project that was never swept contributes nothing rather than raising.
    assert sweeper.doc_type_targets("all", ["P3"]) == []


def test_doc_type_targets_accepts_explicit_keys(conn):
    sweeper = _probe_sweeper(conn, {})
    assert sweeper.doc_type_targets("T-A, T-B", ["P1"]) == [("T-A", ""), ("T-B", "")]
