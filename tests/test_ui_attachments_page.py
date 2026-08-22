"""The page listing every attachment we have downloaded.

`/ui/manual` shows the attachments needing attention and the mail dialog shows one message's
worth. Neither can answer "what have we actually got?", and neither can show that the same bytes
arrived twice under two filenames — which is the thing on this corpus that a person most needs to
see. These tests hold the properties that make the page worth opening.

The generic page invariants — one script block, paged, searchable, a date filter bound to a real
column, the nav entry resolving — are swept over every page in `test_ui_html.py`, which this page
is now part of. What is here is what is specific to it.
"""

import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.ui import routes as ui_routes
from pipeline import read_views, state_db


def rows_of(body: str) -> list:
    """The clickable data rows of the attachments table, ignoring the rest of the page."""
    table = re.search(r'id="attachments-table".*?</table>', body, re.S)
    return re.findall(r'<tr[^>]*class="clickable"', table.group(0)) if table else []


@pytest.fixture
def store(tmp_path):
    """A store with one email carrying three attachments: a POD, a byte-identical copy of it under
    another name, and an inline logo. That is the corpus in miniature.

    On disk rather than `:memory:`, because `TestClient` serves the request on another thread and
    SQLite refuses a connection across threads — and because opening one connection per request is
    what production does.
    """
    db = tmp_path / "attachments.sqlite3"
    conn = state_db.get_connection(db)
    conn.execute("""INSERT INTO email_log (email_id, subject, sender, email_date, category,
                                           matched_rule, reason, folder, processed_at)
                    VALUES ('mail-1', 'Delivered 212559', 'vendor@example.com',
                            '2026-08-17T09:00:00Z', 'surface', '', '', 'Processed', 'now')""")
    rows = [
        (0, "POD.pdf", "pdf", 0, "extracted", 1, "212559", "aaaa1111"),
        (1, "FedEx 884603885067.pdf", "pdf", 0, "dropped_duplicate", 1, "212559", "aaaa1111"),
        (2, "logo.png", "image", 1, "dropped_decorative", 0, "", "bbbb2222"),
    ]
    for ordinal, name, kind, inline, disposition, is_pod, pos, sha in rows:
        conn.execute(
            """INSERT INTO attachment_ledger
               (email_id, depth, ordinal, filename, sniffed_kind, disposition, is_inline,
                size_bytes, sha256, blob_sha256, is_pod, pod_po_numbers, pod_delivery_date,
                pod_signed_by, records_extracted, first_seen_at)
               VALUES ('mail-1', 0, ?, ?, ?, ?, ?, 20535, ?, ?, ?, ?, '2026-08-17', 'U ALI', 0,
                       'now')""",
            (ordinal, name, kind, disposition, inline, sha, sha, is_pod, pos))
    conn.commit()

    # Override the dependency itself rather than monkeypatching the module attribute: FastAPI
    # captured the original function at import time, so a patched attribute is a different object
    # and the route goes on using the real store.
    from api import deps

    def _this_store():
        request_conn = state_db.get_connection(db)
        try:
            yield request_conn
        finally:
            request_conn.close()

    app.dependency_overrides[deps.get_pipeline_conn] = _this_store
    yield conn
    app.dependency_overrides.pop(deps.get_pipeline_conn, None)
    conn.close()


def test_every_attachment_is_listed_including_the_inline_one(store):
    """"All the attachments we have downloaded" means all of them. An inline logo was still
    downloaded and still takes up disk, so the count on screen matches the count in the table."""
    body = TestClient(app).get("/ui/attachments").text

    assert len(rows_of(body)) == 3
    assert "POD.pdf" in body and "logo.png" in body


def test_the_inline_toggle_puts_the_logos_aside(store):
    """Signature logos outnumber real attachments better than two to one on the live store, so a
    page that only ever showed everything would bury what matters."""
    body = TestClient(app).get("/ui/attachments?inline=hide").text

    assert len(rows_of(body)) == 2
    assert "logo.png" not in body
    assert "POD.pdf" in body


def test_the_same_bytes_under_two_names_are_visibly_the_same_file(store):
    """The failure this page exists to make visible. Two PDFs arrived on the 210634 thread under
    filenames citing different specs and tracking numbers and are byte-identical; nothing in the
    per-message view could show that, because it shows one message at a time and neither name is
    wrong on its face."""
    body = TestClient(app).get("/ui/attachments").text

    assert body.count("aaaa1111") == 2, "the shared content hash is what gives the copy away"
    assert "dropped duplicate" in body


def test_a_pod_says_which_purchase_order_it_names(store):
    """Not that it is *a* POD — which one it is proof for. A delivery note naming another PO is
    the thing that must never be attached to this receipt."""
    body = TestClient(app).get("/ui/attachments").text

    assert "212559" in body
    assert "U ALI" in body and "2026-08-17" in body


def test_a_file_whose_bytes_are_gone_says_so_rather_than_offering_a_dead_link(store):
    """`attachment_store.put` returns None on a write failure and the ledger row is written
    anyway, so "we have a record of it" and "we have it" are different questions."""
    store.execute("UPDATE attachment_ledger SET sha256 = '', blob_sha256 = NULL WHERE ordinal = 0")
    store.commit()

    body = TestClient(app).get("/ui/attachments").text

    assert "not stored" in body


def test_each_row_opens_its_own_file_not_its_email(store):
    """The mail page already opens messages. What no other page can do is open the attachment, and
    the URL is keyed by ordinal — the ledger id would 404."""
    body = TestClient(app).get("/ui/attachments").text

    assert 'data-frag="/ui/mail/attachment/view?id=mail-1&amp;n=0&amp;src=inbox"' in body
    assert "download=1" in body, "and offer the bytes themselves"


def test_the_store_is_named_on_every_attachment_url(store):
    """`_store_for("")` resolves an absent `src` to the retired sample corpus, so a URL without it
    is a silent 404. The same omission is what broke the manual queue's mail links."""
    body = TestClient(app).get("/ui/attachments").text

    for url in re.findall(r'/ui/mail/attachment[^"\']*', body):
        assert "src=" in url, url


def test_the_view_returns_plain_rows_for_an_empty_store(monkeypatch):
    conn = state_db.get_connection(":memory:")
    try:
        assert read_views.attachments(conn) == []
        assert read_views.attachments(conn, include_inline=False) == []
    finally:
        conn.close()


def test_the_view_does_not_multiply_rows_when_an_email_is_missing(store):
    """The join to `email_log` is a LEFT JOIN for a reason: an attachment whose email row was
    cleared must still be listed once, not dropped and not duplicated."""
    store.execute("DELETE FROM email_log")
    store.commit()

    assert len(read_views.attachments(store)) == 3


# --- the View control -------------------------------------------------------------------------


def test_every_row_offers_view_as_well_as_download(store):
    """The in-page viewer existed all along — a PDF renders in an iframe, an image in an img — but
    the only way in was clicking the filename, which is styled as text. A page whose only visible
    control is Download reads as download-only, and people downloaded files to look at them."""
    body = TestClient(app).get("/ui/attachments").text
    table = re.search(r'id="attachments-table".*?</table>', body, re.S).group(0)

    assert table.count(">View<") == 3, "one View per row"
    assert table.count(">Download<") == 3
    assert 'data-frag="/ui/mail/attachment/view?id=mail-1&amp;n=0&amp;src=inbox"' in table


def test_view_is_a_button_and_download_is_a_link(store):
    """Deliberately different elements. View fetches a fragment into the shared dialog, which is
    the one POST-free path the single inline script knows; Download navigates to bytes, which a
    button cannot do without script this app does not spend."""
    table = re.search(r'id="attachments-table".*?</table>',
                      TestClient(app).get("/ui/attachments").text, re.S).group(0)

    assert re.search(r'<button[^>]*data-frag="[^"]*attachment/view[^"]*"[^>]*>View</button>', table)
    assert re.search(r'<a[^>]*href="[^"]*download=1"[^>]*>Download</a>', table)


# --- which purchase order a file belongs to, and on whose word ---------------------------------


def _po_cell(body: str, filename: str) -> str:
    table = re.search(r'id="attachments-table".*?</table>', body, re.S).group(0)
    at = table.find(filename)
    row = table[table.rfind("<tr", 0, at):table.find("</tr>", at)]
    cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
    return cells[7]


def test_a_po_named_by_the_file_itself_is_shown_plainly(store):
    """The strong claim, and the only one `spitfire_post._pod_for` will act on: the document says
    which order it is proof for."""
    assert _po_cell(TestClient(app).get("/ui/attachments").text, "POD.pdf") == "212559"


def test_a_po_known_only_from_the_email_is_marked_as_such(store):
    """`email_log.po_hints` was populated all along and never surfaced, which is why this page
    first shipped showing an em dash for forty-eight of fifty-five files."""
    store.execute("UPDATE email_log SET po_hints = '207030, 211169' WHERE email_id = 'mail-1'")
    store.execute("UPDATE attachment_ledger SET is_pod = 0, pod_po_numbers = ''")
    store.commit()

    cell = _po_cell(TestClient(app).get("/ui/attachments").text, "logo.png")

    assert cell.startswith("email:"), cell
    assert "207030" in cell


def test_a_po_known_only_from_the_records_is_marked_weakest(store):
    """One tracker spreadsheet reaches five purchase orders this way, on nothing but the envelope
    it arrived in. Showing it is useful; showing it as equal to the file's own word would not be."""
    store.execute("UPDATE attachment_ledger SET is_pod = 0, pod_po_numbers = ''")
    store.execute("""INSERT INTO extracted_records
                     (source_email_id, po_number, email_date, extraction_source,
                      extraction_confidence, status, created_at)
                     VALUES ('mail-1', '206993', '2026-08-17', 'test', 1.0, 'pending', 'now')""")
    store.commit()

    cell = _po_cell(TestClient(app).get("/ui/attachments").text, "logo.png")

    assert cell.startswith("records:"), cell
    assert "206993" in cell


def test_the_file_outranks_the_email_when_both_name_one(store):
    """A file naming 212559 on a message about 999999 is proof for 212559. The email's claim is
    still counted, so nothing is hidden — it is just not what the cell leads with."""
    store.execute("UPDATE email_log SET po_hints = '999999' WHERE email_id = 'mail-1'")
    store.commit()

    cell = _po_cell(TestClient(app).get("/ui/attachments").text, "POD.pdf")

    assert cell.startswith("212559"), cell
    assert "more on the email" in cell


def test_no_evidence_anywhere_is_an_em_dash(store):
    """True of a signature logo, and true of the five photographed delivery notes whose purchase
    order OCR could not read. Inventing an association for those would be worse than a blank."""
    store.execute("UPDATE attachment_ledger SET is_pod = 0, pod_po_numbers = ''")
    store.execute("UPDATE email_log SET po_hints = ''")
    store.commit()

    assert _po_cell(TestClient(app).get("/ui/attachments").text, "logo.png") == "—"
