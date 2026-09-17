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
from urllib.parse import parse_qs, urlsplit
import sqlite3

import pytest
from fastapi.testclient import TestClient

from api.main import app
from pipeline import mail_view
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
                    VALUES ('mail-1', 'Delivered 912559', 'vendor@example.com',
                            '2026-08-17T09:00:00Z', 'surface', '', '', 'Processed', 'now')""")
    rows = [
        (0, "POD.pdf", "pdf", 0, "extracted", 1, "912559", "aaaa1111"),
        (1, "FedEx 884603885067.pdf", "pdf", 0, "dropped_duplicate", 1, "912559", "aaaa1111"),
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


def test_every_attachment_is_reachable_and_none_is_silently_dropped(store):
    """An inline logo was still downloaded and still takes up disk, so it must stay reachable and
    stay counted.

    It is no longer *rendered* by default. On the live store 10,109 of 11,012 rows are signature
    logos, and shipping all of them to show a page of 25 cost 25 MB of HTML — so the default view
    loads the real files and the other two views are a click away. What must not change is that
    nothing disappears: the logo is one link away and its count is printed on the page.
    """
    client = TestClient(app)
    body = client.get("/ui/attachments").text

    assert len(rows_of(body)) == 2, "the default view is the real files"
    assert "POD.pdf" in body
    assert 'href="/ui/attachments?view=all"' in body, "no way back to everything"

    everything = client.get("/ui/attachments?view=all").text
    assert len(rows_of(everything)) == 3
    assert "POD.pdf" in everything and "logo.png" in everything


def test_the_logos_can_be_put_aside_from_the_view_links(store):
    """Signature logos outnumber real attachments better than two to one on the live store, so a
    page that only ever showed everything would bury what matters.

    This was `?inline=hide`, a link that reloaded the whole page to hide rows it had already
    rendered, then a dropdown filtering rows the browser held. It is a view of the same rows, so it
    is one of three views — and each says the thing the old link could not: how many rows it holds.
    None of them is rendered and then hidden; the row set is chosen by the query.
    """
    client = TestClient(app)
    body = client.get("/ui/attachments").text

    # Put aside by not being loaded at all, rather than by being rendered and then hidden with a
    # class — which is what made the page cost 25 MB to show 25 rows.
    assert len(rows_of(body)) == 2, "the logo should not be in the default page at all"
    assert "logo.png" not in body
    assert 'href="/ui/attachments?view=inline"' in body, "the logos on their own"

    logos = client.get("/ui/attachments?view=inline").text
    assert "logo.png" in logos and "POD.pdf" not in logos

    # The row still says what it is, and the two ways of narrowing stay independent: a file can be
    # both a signature logo and one nothing could read, and must not fall through the gap between
    # the two views.
    #
    # This used to be read off `data-choice-value`, a space-separated state stamped on every row so
    # a dropdown in the browser could match against it. That went when the page moved server-side —
    # the browser holds 25 rows now, so a control matching what it holds could only ever narrow
    # those. The same two dimensions are `view=` and `state=` on the query, and being separate
    # clauses in SQL is what makes them compose.
    assert ">inline<" in logos, "the inline view does not badge its rows as inline"
    both = client.get("/ui/attachments?view=inline&state=unread").text
    assert len(rows_of(both)) <= len(rows_of(logos)),         "narrowing to unreadable files did not narrow the inline view"


def test_the_same_bytes_under_two_names_are_visibly_the_same_file(store):
    """The failure this page exists to make visible. Two PDFs arrived on the 910634 thread under
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

    assert "912559" in body
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


def test_every_attachment_url_has_exactly_one_query_string():
    """A URL has one `?`. These had two, and the second one was silently eaten.

    `api/ui/routes.py` hands `mail_view.render` an `attachment_url` that already carries `src=`,
    and the two places that added `id=` and `n=` wrote `?` unconditionally — producing
    `/ui/mail/attachment?src=inbox?id=<...>&n=3`. The server read that as `src="inbox?id=<...>"`
    with **no `id` at all**, `_store_for` sent the unrecognised `src` to the sample corpus, and it
    404'd. Every inline image in every stored message body was a broken icon, along with the
    Download link and the thumbnail beside it.

    Asserted by parsing rather than by matching text, because the broken form *contains* the right
    substrings — `src=` and `id=` and `n=` are all present in it. Only a parser notices.
    """
    for base in ("/ui/mail/attachment?src=inbox", "/ui/mail/attachment"):
        url = mail_view._url(base, id="<DM4PR14MB4831@namprd14.prod.outlook.com>", n=3)
        assert url.count("?") == 1, url
        query = parse_qs(urlsplit(url).query, keep_blank_values=True)
        assert query["id"] == ["<DM4PR14MB4831@namprd14.prod.outlook.com>"], url
        assert query["n"] == ["3"], url
        if "src" in base:
            assert query["src"] == ["inbox"], "changing view must not lose the store"


def test_the_payload_viewer_reads_the_key_the_serializer_writes():
    """`stage2_accumulate` writes `content_b64`; this read `content_bytes`, a key no payload has
    ever carried, so held mail never showed its own inline copies."""
    import inspect

    assert '"content_b64"' in inspect.getsource(mail_view._from_accumulation)


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
    # Asked of every row there is, not only the default view's, so the control is proved present
    # on an inline row too.
    body = TestClient(app).get("/ui/attachments?view=all").text
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
    """The PO cell of the row holding `filename`.

    The column is found by its heading rather than by a hard-coded index. It was `cells[7]`, and
    reordering the table to the agreed layout moved it to 4 — a fixed index in a test is the same
    trap `table(date_column=...)` exists to avoid in the page itself.
    """
    table = re.search(r'id="attachments-table".*?</table>', body, re.S).group(0)
    heads = [re.sub(r"<[^>]+>", "", h).strip()
             for h in re.findall(r"<th[^>]*>(.*?)</th>", table, re.S)]
    column = heads.index("PO")
    at = table.find(filename)
    row = table[table.rfind("<tr", 0, at):table.find("</tr>", at)]
    cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
    return cells[column]


def test_a_po_named_by_the_file_itself_is_shown_plainly(store):
    """The strong claim, and the only one `spitfire_post._pod_for` will act on: the document says
    which order it is proof for."""
    assert _po_cell(TestClient(app).get("/ui/attachments").text, "POD.pdf") == "912559"


def test_a_po_known_only_from_the_email_is_marked_as_such(store):
    """`email_log.po_hints` was populated all along and never surfaced, which is why this page
    first shipped showing an em dash for forty-eight of fifty-five files."""
    store.execute("UPDATE email_log SET po_hints = '907030, 911169' WHERE email_id = 'mail-1'")
    store.execute("UPDATE attachment_ledger SET is_pod = 0, pod_po_numbers = ''")
    store.commit()

    cell = _po_cell(TestClient(app).get("/ui/attachments").text, "logo.png")

    assert cell.startswith("email:"), cell
    assert "907030" in cell


def test_a_po_known_only_from_the_records_is_marked_weakest(store):
    """One tracker spreadsheet reaches five purchase orders this way, on nothing but the envelope
    it arrived in. Showing it is useful; showing it as equal to the file's own word would not be."""
    store.execute("UPDATE attachment_ledger SET is_pod = 0, pod_po_numbers = ''")
    store.execute("""INSERT INTO extracted_records
                     (source_email_id, po_number, email_date, extraction_source,
                      extraction_confidence, status, created_at)
                     VALUES ('mail-1', '906993', '2026-08-17', 'test', 1.0, 'pending', 'now')""")
    store.commit()

    cell = _po_cell(TestClient(app).get("/ui/attachments").text, "logo.png")

    assert cell.startswith("records:"), cell
    assert "906993" in cell


def test_the_file_outranks_the_email_when_both_name_one(store):
    """A file naming 912559 on a message about 999999 is proof for 912559. The email's claim is
    still counted, so nothing is hidden — it is just not what the cell leads with."""
    store.execute("UPDATE email_log SET po_hints = '999999' WHERE email_id = 'mail-1'")
    store.commit()

    cell = _po_cell(TestClient(app).get("/ui/attachments").text, "POD.pdf")

    assert cell.startswith("912559"), cell
    assert "more on the email" in cell


def test_no_evidence_anywhere_is_an_em_dash(store):
    """True of a signature logo, and true of the five photographed delivery notes whose purchase
    order OCR could not read. Inventing an association for those would be worse than a blank."""
    store.execute("UPDATE attachment_ledger SET is_pod = 0, pod_po_numbers = ''")
    store.execute("UPDATE email_log SET po_hints = ''")
    store.commit()

    assert _po_cell(TestClient(app).get("/ui/attachments").text, "logo.png") == "—"
