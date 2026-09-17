"""The page that settles a delivery's proof of delivery, driven the way a person drives it.

`test_record_create.py` holds the rules this page reaches; this holds the screen. It exists because
the screen it replaces was a dead end: a delivery whose lines gate 2 refused drew a "Post N lines"
button, a dialog saying none of them could post, and nothing else. Measured on the live store
2026-09-10, that was 91 of the 107 rows the Records page believed carried a proof, across 24
deliveries — so the dead end was the ordinary state of the page, not an edge of it.

Nothing here reaches Spitfire, and the autouse guard below keeps it that way. Settling a proof
writes one column on some records; it is the control that *permits* a write later, which is why it
is guarded like one and tested like one.
"""

import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from config import settings
from operations import killswitch
from pipeline import email_log, mail_cache, spitfire_post, state_db

NOW = "2026-09-11 09:00:00"
WAREHOUSE = "dispatch@example-logistics.test"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A throwaway store holding one delivery of three lines that cannot prove itself.

    The same shape as the delivery that prompted this work: an externally-authored message carrying
    files, none of which reads as a proof of delivery — and one of those files a PDF, which is what
    puts the message in `emails_with_a_possible_pod`'s superset and so made the Post button promise
    what it could not keep.

    Pointing `PIPELINE_STATE_DB_PATH` rather than overriding `deps.get_pipeline_conn`, for the
    reason `test_create_record_ui.py` gives: the async POST handlers open their own connection.
    """
    path = tmp_path / "pipeline_state.sqlite3"
    monkeypatch.setattr(settings, "PIPELINE_STATE_DB_PATH", path)

    conn = state_db.get_connection(path)
    email_log.record(conn, email_id="mail-d", subject="[External] FW: PO 908491 delivered",
                     sender="staff@example-pm.test", origin_sender=WAREHOUSE, category="route",
                     matched_rule="rule_7", reason="delivery", folder="Routed", processed_at=NOW,
                     po_hints="908491")
    conn.execute(
        """INSERT INTO attachment_ledger
           (id, email_id, depth, ordinal, filename, sniffed_kind, disposition, is_inline,
            sha256, size_bytes, first_seen_at, is_pod, pod_po_numbers)
           VALUES (21, 'mail-d', 0, 0, 'revision.docx', 'docx', 'extracted', 0,
                   'sha-docx', 40, 'now', 0, '')""")
    conn.execute(
        """INSERT INTO attachment_ledger
           (id, email_id, depth, ordinal, filename, sniffed_kind, disposition, is_inline,
            sha256, size_bytes, first_seen_at, is_pod, pod_po_numbers)
           VALUES (22, 'mail-d', 0, 1, 'BOL-33214.pdf', 'pdf', 'extracted', 0,
                   'sha-bol', 60, 'now', 0, '')""")
    mail_cache.cache_mail(
        conn, email_id="mail-d", subject="[External] FW: PO 908491 delivered", sender=WAREHOUSE,
        received_at=NOW, body_html="<p>Delivered.</p>", body_text=None,
        source="Outlook (read-only)", cached_at=NOW,
        attachments=[{"ordinal": 0, "filename": "revision.docx", "content_type": "application/msword",
                      "kind": "docx", "size_bytes": 40, "is_inline": 0, "content": b"not a pod"},
                     {"ordinal": 1, "filename": "BOL-33214.pdf", "content_type": "application/pdf",
                      "kind": "pdf", "size_bytes": 60, "is_inline": 0, "content": b"%PDF- nothing"}])

    conn.execute("""INSERT INTO deliveries (id, po_number, delivery_ref, delivery_rung,
                                            source_email_id, extraction_source, status, created_at)
                    VALUES (5, '908491', 'D-1', 'delivered', 'mail-d', 'docx', 'pending', ?)""",
                 (NOW,))
    for record_id in (101, 102, 103):
        conn.execute(
            """INSERT INTO extracted_records
               (id, source_email_id, po_number, spec_code, item_description, quantity_received,
                pod_stated_date, status, delivery_id, email_date, extraction_source,
                extraction_confidence, pod_source, created_at, updated_at)
               VALUES (?, 'mail-d', '908491', 'STE-402-LT-B', 'BASE, Floor Lamp 2', 11,
                       '2026-09-04', 'pending', 5, ?, 'docx', 0.9, 'email_received_date', ?, ?)""",
            (record_id, NOW, NOW, NOW))
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def client(store):
    from api.main import app
    return TestClient(app)


@pytest.fixture(autouse=True)
def never_really_post(monkeypatch):
    """The standing guard every route test here keeps. A test that could reach Spitfire to prove
    this page does not would be self-defeating."""
    def refuse(*args, **kwargs):
        raise AssertionError("a test reached the real post chain")
    for name in ("post_pod", "post_report", "post_delivery_pod", "post_delivery_report"):
        monkeypatch.setattr(spitfire_post, name, refuse)


@pytest.fixture(autouse=True)
def switch_released(monkeypatch):
    """Released by default, patched rather than written.

    `killswitch.engage` writes to the operations store, which is a different database from the
    pipeline one these tests point at — so engaging it for real here would mean standing up a
    second store to assert a branch that only reads `is_stopped()`. `test_ui_delivery_post_route.py`
    patches it for the same reason.
    """
    monkeypatch.setattr(killswitch, "is_stopped", lambda: False)


def records(store):
    conn = sqlite3.connect(store)
    conn.row_factory = sqlite3.Row
    try:
        return {int(r["id"]): r for r in
                conn.execute("SELECT * FROM extracted_records ORDER BY id")}
    finally:
        conn.close()


# --- what the page says -------------------------------------------------------------------------


def test_the_page_lists_the_lines_that_cannot_prove_themselves(client):
    page = client.get("/ui/deliveries/5/proof")

    assert page.status_code == 200
    # Apostrophes arrive escaped: `html.tag` escapes every value it is handed, which is the
    # property that makes a delivery email's own text safe to render.
    assert "3 of this delivery" in page.text and "lines cannot be posted" in page.text
    assert "one signature covering 3 lines" in page.text.lower()


def test_the_page_offers_both_ways_out(client):
    """A file on the message, or an explicit statement that there is none. Both, and in that order:
    a real document on the receipt is always the better answer."""
    page = client.get("/ui/deliveries/5/proof").text

    assert "BOL-33214.pdf" in page and "revision.docx" in page
    assert 'value="none"' in page, "the body-is-the-proof option must be offered too"


def test_the_page_spends_one_script_and_no_inline_handlers(client):
    """The rule every page here keeps. A new control had to be a link or a `data-` attribute; an
    `onclick` would have been the easy way and would have broken the whole surface's contract."""
    page = client.get("/ui/deliveries/5/proof").text

    assert page.count("<script") == 1
    assert "onclick=" not in page


def test_a_delivery_with_nothing_to_settle_says_so(client, store):
    """Not an error and not an empty page. Somebody arriving here from a stale tab needs to read
    that the question has already been answered."""
    conn = sqlite3.connect(store)
    conn.execute("UPDATE extracted_records SET pod_waived_by = 'Ada Lovelace' WHERE delivery_id = 5")
    conn.commit()
    conn.close()

    page = client.get("/ui/deliveries/5/proof")

    assert "There is nothing to decide here." in page.text


def test_an_unknown_delivery_is_a_404(client):
    assert client.get("/ui/deliveries/4242/proof").status_code == 404


# --- what the page does -------------------------------------------------------------------------


def test_accepting_without_a_proof_signs_every_line_at_once(client, store):
    answer = client.post("/ui/deliveries/5/proof",
                         data={"email_id": "mail-d", "pod_ledger_id": "none",
                               "by": "Ada Lovelace"}, follow_redirects=False)

    assert answer.status_code == 303
    assert answer.headers["location"] == "/ui/records?settled=5"
    assert all(r["pod_waived_by"] == "Ada Lovelace" for r in records(store).values())


def test_naming_a_file_makes_it_the_proof_for_every_line(client, store):
    answer = client.post("/ui/deliveries/5/proof",
                         data={"email_id": "mail-d", "pod_ledger_id": "22", "by": ""},
                         follow_redirects=False)

    assert answer.status_code == 303
    assert all(r["pod_ledger_id"] == 22 for r in records(store).values())
    assert all(r["pod_waived_by"] is None for r in records(store).values()), (
        "naming a proof is not a waiver and must not be recorded as one")


def test_an_unsigned_waiver_comes_back_to_the_page_and_writes_nothing(client, store):
    answer = client.post("/ui/deliveries/5/proof",
                         data={"email_id": "mail-d", "pod_ledger_id": "none", "by": "   "})

    assert answer.status_code == 200
    assert "who gave it" in answer.text
    assert all(r["pod_waived_by"] is None for r in records(store).values())


def test_choosing_nothing_at_all_is_refused(client, store):
    answer = client.post("/ui/deliveries/5/proof",
                         data={"email_id": "mail-d", "pod_ledger_id": "", "by": "Ada Lovelace"})

    assert answer.status_code == 200
    assert "or say explicitly that there is none" in answer.text
    assert all(r["pod_waived_by"] is None for r in records(store).values())


def test_the_kill_switch_stops_it(client, store, monkeypatch):
    """It sends nothing outbound, and it is still refused. "Stopped" has to mean the state that
    feeds the writer stops changing too, or releasing the switch releases a queue of decisions
    nobody reviewed. The same check `waive_pod` keeps."""
    monkeypatch.setattr(killswitch, "is_stopped", lambda: True)

    answer = client.post("/ui/deliveries/5/proof",
                         data={"email_id": "mail-d", "pod_ledger_id": "none",
                               "by": "Ada Lovelace"})

    assert "kill switch is engaged" in answer.text
    assert all(r["pod_waived_by"] is None for r in records(store).values())


def test_the_list_of_lines_is_recomputed_and_never_taken_from_the_form(client, store):
    """The form carries the message, the choice and the name — never the record ids. A stale tab
    cannot sign for a line that has since been settled, and a crafted POST cannot widen the
    decision past what the server itself finds blocked."""
    conn = sqlite3.connect(store)
    conn.execute("UPDATE extracted_records SET pod_waived_by = 'Someone Earlier' WHERE id = 103")
    conn.commit()
    conn.close()

    client.post("/ui/deliveries/5/proof",
                data={"email_id": "mail-d", "pod_ledger_id": "none", "by": "Ada Lovelace",
                      "record_ids": "101,102,103"}, follow_redirects=False)

    after = records(store)
    assert after[101]["pod_waived_by"] == "Ada Lovelace"
    assert after[103]["pod_waived_by"] == "Someone Earlier", (
        "a line already settled keeps the first name, whatever the form asked for")


# --- the dead end it was built to close ---------------------------------------------------------


def plans(monkeypatch, *, blocked_on_pod):
    """Stand in for `plan_delivery`, which would otherwise read the purchase order live.

    The verdicts are the point here, not how they were reached — `test_spitfire_post_delivery.py`
    holds the gates themselves.
    """
    refusals = [spitfire_post.LinePlan(record_id=r, ok=False, reason="no proof of delivery",
                                       blocked_on_pod=blocked_on_pod)
                for r in (101, 102, 103)]
    monkeypatch.setattr(spitfire_post, "plan_delivery", lambda *a, **k: (refusals, {}))


def test_the_dialog_that_refuses_every_line_offers_the_way_out(client, monkeypatch):
    """The reported failure, asserted. "Nothing to post" listing thirty-two reasons and offering no
    control is what sent somebody looking for a feature that already existed one page away."""
    plans(monkeypatch, blocked_on_pod=True)

    dialog = client.post("/ui/deliveries/5/post-pod/confirm").text

    assert "Nothing to post" in dialog
    assert "/ui/deliveries/5/proof" in dialog
    assert "Settle the proof for 3 lines" in dialog


def test_a_refusal_a_signature_cannot_lift_offers_nothing(client, monkeypatch):
    """An over-receipt is not something a waiver frees. Offering the link anyway would ask somebody
    to sign for a refusal their signature has no bearing on, which is how a control stops being
    read as meaning anything."""
    plans(monkeypatch, blocked_on_pod=False)

    dialog = client.post("/ui/deliveries/5/post-pod/confirm").text

    assert "Nothing to post" in dialog
    assert "/ui/deliveries/5/proof" not in dialog


def test_the_banner_confirms_it(client):
    """`waive_pod` has redirected to `?waived=` since it was written and nothing ever rendered it,
    so accepting a delivery returned somebody to a table of twenty-five rows with no sign it had
    worked. This page must not repeat that."""
    page = client.get("/ui/records?settled=5").text

    assert "delivery #5 is settled" in page
