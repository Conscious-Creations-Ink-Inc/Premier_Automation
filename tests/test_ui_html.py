"""The renderer's whole safety claim is that nothing reaches the page unescaped.

Mail subjects are attacker-influenced by definition, and the corpus already contains one with a
bare `&` in it. These tests are what stop a future edit from quietly reintroducing raw
interpolation.
"""
import re
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.ui import html, routes
from pipeline import delivery_status, read_views

HOSTILE = 'OS&E <script>alert("x")</script> "quoted" \'single\''

# Captured at import, before the autouse fixture below can substitute a corpus-carrying tuple.
# This is what Premier actually runs with.
PRODUCTION_MAIL_SOURCES = read_views.MAIL_SOURCES


def column_labels(markup: str):
    """The visible text of each `<th>`, however it is wrapped.

    A sortable heading is a `<button>` inside its `<th>` carrying a sort-arrow `<span>`; a column
    named in `no_sort` stays a bare cell. Anything matching `<th>…</th>` literally therefore sees
    only some of the columns.
    """
    return [re.sub(r"<[^>]+>", "", cell).strip()
            for cell in re.findall(r"<th[^>]*>(.*?)</th>", markup)]


@pytest.fixture(autouse=True)
def _corpus_as_a_test_fixture(monkeypatch):
    """Render these pages against the `.msg` corpus store.

    The corpus stopped being a product surface on 2026-08-12 — every `/ui` page now reads only
    Premier's live mailbox (`read_views.MAIL_SOURCES`, `deps.get_pipeline_conn`). It remains the
    only dataset with enough shape to exercise a renderer: 14 emails, 72 attachments, subjects
    carrying a bare `&`, and several purchase orders sitting at different delivery stages. So it
    moves from being what the product shows to being what the tests read, which is where a fixture
    belonged all along.

    Two things this buys beyond keeping the coverage. These assertions used to track whatever
    happened to be in Premier's inbox that morning, so they would fail for reasons having nothing
    to do with the renderer. And `/ui/mails` reached Microsoft Graph on every render, meaning the
    suite made live network calls; `operations.inbox.load` is stubbed here so it no longer does.

    `test_the_default_mail_source_is_the_live_inbox_only` deliberately opts out of this fixture to
    assert the real production wiring.
    """
    from operations import inbox as inbox_reader

    from api import deps
    from config import settings
    from pipeline import state_db

    def _corpus_conn():
        conn = state_db.get_connection(settings.SAMPLE_STATE_DB_PATH)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[deps.get_pipeline_conn] = _corpus_conn
    monkeypatch.setattr(read_views, "MAIL_SOURCES", (
        ("inbox", "Inbox", "PIPELINE_STATE_DB_PATH"),
        ("sample", "Sample", "SAMPLE_STATE_DB_PATH"),
    ))
    monkeypatch.setattr(
        inbox_reader, "load",
        lambda *a, **k: inbox_reader.InboxView(
            configured=True, mailbox="receiver@premierpm.com", messages=[]),
    )
    yield
    app.dependency_overrides.pop(deps.get_pipeline_conn, None)


def test_a_hostile_subject_is_escaped_in_a_table():
    out = str(html.table(["Subject"], [[HOSTILE]]))
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&amp;" in out
    assert "&quot;" in out


def test_attribute_values_are_escaped():
    out = str(html.tag("a", "click", href='" onmouseover="alert(1)'))
    assert 'onmouseover="alert(1)"' not in out
    assert "&quot;" in out


def test_none_renders_as_empty_not_the_word_none():
    assert str(html.tag("td", None)) == "<td></td>"
    assert html.esc(None) == ""


def test_raw_passes_through_and_nesting_does_not_double_escape():
    inner = html.tag("b", "A&B")
    outer = str(html.tag("td", inner))
    assert outer == "<td><b>A&amp;B</b></td>"
    assert "&amp;amp;" not in outer


def test_badge_and_muted_escape_their_text():
    assert "&lt;i&gt;" in str(html.badge("<i>", "route"))
    assert "&lt;i&gt;" in str(html.muted("<i>"))


def test_a_token_is_unbreakable_and_still_escaped():
    """Spec codes and timestamps are hyphenated, and a browser breaks after a hyphen — `EXT-925-AC`
    becomes three stacked fragments the moment a neighbouring column wants the width."""
    out = str(html.token("EXT-925-AC"))
    assert 'class="nw"' in out
    assert "EXT-925-AC" in out
    assert "&lt;i&gt;" in str(html.token("<i>")), "a token is still untrusted text"


def test_an_absent_token_renders_a_dash_not_an_empty_cell():
    """An empty cell reads as "no column here"; a dash reads as "nothing to report", which is the
    true statement."""
    for empty in (None, ""):
        assert str(html.token(empty)) == str(html.muted("—"))
    assert str(html.when(None)) == str(html.muted("—"))


def test_void_elements_get_no_closing_tag():
    """`<br></br>` renders as two line breaks, and `<input></input>` is invalid. `tag()` has to
    know the difference because the schedule form passes it inputs."""
    assert str(html.tag("input", type="checkbox")) == '<input type="checkbox">'
    assert "</input>" not in str(html.tag("input", name_="enabled"))
    assert str(html.tag("input", name_="enabled")) == '<input name="enabled">', \
        "a trailing underscore is how an attribute named `name` gets through"


def test_attributes_named_class_and_data_are_rendered_correctly():
    out = str(html.tag("div", "x", class_="a b", data_id="7"))
    assert 'class="a b"' in out
    assert 'data-id="7"' in out


def test_none_attributes_are_omitted_entirely():
    assert str(html.tag("a", "x", href=None)) == "<a>x</a>"


def test_css_cannot_close_its_own_style_block():
    assert "</style>" not in html._CSS


def test_the_script_cannot_close_its_own_block_or_open_another():
    """The same hole as the CSS one, on the other constant.

    `</script>` anywhere in the block — even inside a comment — ends the element early and drops
    the rest of the file into the document as markup. A bare `<script>` in a comment is harmless to
    the browser but breaks the one-script count below, which is the check that actually guards
    against smuggled code, so it is not allowed to be spent on prose.
    """
    assert "</script>" not in html._JS
    assert "<script" not in html._JS


def test_page_is_a_complete_document_and_escapes_its_title():
    out = html.page("A&B", "/ui/mails", html.tag("p", "hi"))
    assert out.startswith("<!doctype html>")
    assert out.rstrip().endswith("</html>")
    assert "A&amp;B" in out


# --- the routes themselves ---------------------------------------------------

def test_every_page_renders_before_any_pipeline_run():
    """A fresh checkout has no pipeline database; the pages must say so, not 500."""
    client = TestClient(app)
    for path in ("/ui/mails", "/ui/records", "/ui/manual", "/ui/po"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/html")
        assert "<!doctype html>" in response.text


def test_every_nav_entry_is_a_real_page():
    """The sidebar is a hand-written tuple; a typo in it renders a link to a 404 on every page.

    Walks `nav_links()` rather than `_NAV` itself, because `_NAV` is grouped — going through the
    accessor means adding a group cannot quietly take its links out of this check.
    """
    client = TestClient(app)
    links = html.nav_links()
    assert links, "the sidebar is empty"
    for href, _label in links:
        assert client.get(href).status_code == 200, href


def test_every_nav_entry_appears_in_the_rendered_sidebar():
    """`nav_links()` is only the source of truth if the renderer actually uses it. Without this,
    a link could be checked above and still be missing from the page."""
    body = TestClient(app).get("/ui/mails").text
    for href, label in html.nav_links():
        assert f'href="{href}"' in body, href
        assert label in body, label


def test_the_active_nav_entry_is_marked_on_every_page():
    """A sidebar that never highlights leaves a reader with no idea where they are. Detail pages
    light their parent — `/ui/po/208491` marks `/ui/po`."""
    client = TestClient(app)
    for path, active in [(href, href) for href, _ in html.nav_links()] + [("/ui/po/208491", "/ui/po")]:
        body = client.get(path).text
        assert f'href="{active}" class="on"' in body, path
        assert body.count('class="on"') == 1, f"{path} lights more than one nav entry"


def test_the_delivery_page_declares_what_it_cannot_show():
    """The claim a reader would otherwise get wrong: nothing on this page *recorded* a status. Each
    one is derived from whichever notifications happened to arrive, so it is a reading of the mail
    rather than a fact out of Spitfire. It is in the subtitle, where it is on screen whatever the
    table is showing.

    Two further caveats used to sit in a note above the table — that `Receipt staged` and `In
    Spitfire` cannot be reached at all yet, and that Ordered and Outstanding are empty because they
    live on the purchase order inside Spitfire rather than in any delivery email. Both were dropped
    on 2026-08-22, by decision, when the page was rebuilt to the design. They are still true; this
    page no longer says them.
    """
    body = TestClient(app).get("/ui/po").text
    assert "inferred" in body
    assert "not recorded" in body


def test_quantities_line_up_on_their_last_digit():
    """`175.04` above `6`, both left-aligned, cannot be compared by eye — and a right-aligned column
    under a left-aligned heading reads as two columns that happen to overlap, so the heading turns
    with it."""
    out = str(html.table(["PO", "Received"], [["208491", "175.04"]], table_id="t",
                         num_columns=("Received",)))
    assert '<td class="num">175.04</td>' in out
    assert 'class="num"' in out.split("<tbody>")[0], "the heading did not turn with its column"
    assert '<td>208491</td>' in out, "a column nobody named is left alone"
    assert ".scroll th.num, th.num { text-align:right; }" in html._CSS


def test_a_numeric_column_must_name_a_real_heading():
    """Named rather than indexed, for the same reason as the date and choice columns: an index
    silently points at the wrong column the day someone inserts one."""
    with pytest.raises(ValueError):
        html.table(["A", "B"], [["1", "2"]], table_id="t", num_columns=("Nope",))


def test_the_delivery_page_can_be_narrowed_to_what_has_not_arrived():
    """"What is still coming" is one question. Answering it by picking Ordered, then In transit,
    then At partnered warehouse in turn and adding up is not answering it."""
    body = markup_of("/ui/po")
    assert 'data-choice-for="po-table"' in body
    marked = re.findall(r'<th[^>]*data-choice="1"[^>]*>(.*?)</th>', body)
    assert marked and "Status" in re.sub(r"<[^>]+>", "", marked[0])
    assert ">My triage queue<" in body
    # The entry stands for several statuses at once, and never for one that has already arrived.
    value = re.search(r'<option value="([^"]*)">My triage queue</option>', body).group(1)
    assert value, "the triage entry must name the statuses it stands for"
    assert delivery_status.STATUS_LABELS[delivery_status.DELIVERED] not in value.split("|")


def test_the_delivery_export_carries_every_purchase_order():
    """Same rule as Mail's: the file is the whole table, not the page someone happens to be on."""
    client = TestClient(app)
    response = client.get("/ui/po.csv")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    lines = [line for line in response.text.splitlines() if line.strip()]
    assert len(lines) - 1 == client.get("/ui/po").text.count('<td><a href="/ui/po/')
    # The page shows the release *reason* and hides the moment on hover; a file has no hover.
    assert "Released at" in lines[0]


def test_the_stepper_escapes_its_labels():
    """Stage labels are ours, but the renderer must not be the one place escaping is skipped."""
    class FakeStage:
        key, label, on, reached, unreachable, terminal = "open", 'A&B <x>', "2026-01-01", True, False, False

    out = str(html.stepper([FakeStage()]))
    assert "<x>" not in out
    assert "A&amp;B" in out


def test_the_stepper_needs_no_javascript():
    """The progress bar is CSS. Only the message popup justified adding script to these pages, and
    that budget should not quietly expand."""
    class FakeStage:
        key, label, on, reached, unreachable, terminal = "open", "Ordered", "", True, False, False

    out = str(html.stepper([FakeStage()]))
    assert "<script" not in out and "onclick" not in out


def test_a_po_detail_page_renders_and_unknown_pos_404():
    client = TestClient(app)
    assert client.get("/ui/po/208491").status_code == 200
    missing = client.get("/ui/po/000000")
    assert missing.status_code == 404
    assert "000000" in missing.json()["detail"]


def test_the_detail_page_does_not_match_a_partial_po_number():
    """`2084` must not resolve to 208491 — a PO page showing another order's mail is worse than
    one showing none."""
    assert TestClient(app).get("/ui/po/2084").status_code == 404


def test_the_receiver_report_downloads_as_a_workbook():
    response = TestClient(app).get("/ui/records/receiver.xlsx")
    assert response.status_code == 200
    assert "spreadsheetml" in response.headers["content-type"]
    assert response.headers["content-disposition"].startswith('attachment; filename="Receiver_Report_')
    assert response.content[:2] == b"PK", "an xlsx is a zip container"


def _first_frag(client, path: str, contains: str = "") -> str:
    """The fragment URL the first clickable row on a page names.

    `contains` narrows to a particular kind of row. The Mail page now lists both stores newest-
    first, so "the first row" is whichever store happens to hold the newest mail — fine for
    asserting the link shape, useless for asserting anything about attachments, which only the
    corpus has.
    """
    frags = [unescape(f) for f in re.findall(r'data-frag="([^"]*)"', client.get(path).text)]
    matching = [f for f in frags if contains in f]
    assert matching, f"no clickable row on {path} matching {contains!r}"
    return matching[0]


def test_rows_are_clickable_and_keyboard_reachable():
    """`tabindex` is what makes Enter work. A row reachable only by mouse is not reachable."""
    client = TestClient(app)
    for path in ("/ui/mails", "/ui/records", "/ui/manual"):
        body = client.get(path).text
        assert 'class="clickable"' in body, path
        assert 'tabindex="0"' in body, path
        assert "data-frag=" in body, path


def test_a_records_row_opens_the_delivery_bar_not_the_mail():
    """The row is about a purchase order, so clicking it should answer "where is this delivery",
    not "what did the email say". The message stays reachable from the subject cell."""
    client = TestClient(app)
    assert _first_frag(client, "/ui/records").startswith("/ui/po/")
    assert _first_frag(client, "/ui/mails").startswith("/ui/mail?")
    assert "/ui/mail?id=" in client.get("/ui/records").text, "the subject cell still links the mail"


def test_the_mail_page_shows_both_stores_and_marks_each_row():
    """Mails and Inbox were two pages and read as the same one, because neither said where its mail
    came from. The distinction is load-bearing and about to get harder: once Premier forwards
    delivery mail, the live inbox fills with Inbound Notifications that look identical to the
    corpus's — same senders, same subjects."""
    body = TestClient(app).get("/ui/mails").text
    assert 'class="badge badge-sample"' in body, "no corpus rows are marked"
    assert 'class="badge badge-inbox"' in body, "no live rows are marked"
    rows = re.findall(r'<tr class="clickable"[^>]*>(.*?)</tr>', body, re.S)
    assert rows, "no mail rows at all"
    for row in rows:
        assert "badge-sample" in row or "badge-inbox" in row, "a row rendered with no source"


def test_the_subject_and_its_sender_are_one_cell():
    """They were two columns, and one long address set the width of the sender column — which left
    the subject, the thing anyone actually scans this table for, squeezed beside it. The full
    address stays in `title`, which is also where the search box looks, so nothing is lost by
    shortening what is drawn."""
    body = TestClient(app).get("/ui/mails").text
    headers = re.findall(r"<th[^>]*>(?:<button[^>]*>)?([A-Za-z #][^<]*)", body)
    assert "Subject" in headers
    assert "From" not in headers, "the sender moved into the subject cell"
    assert 'class="cell-subject"' in body
    assert 'class="from nw"' in body


def test_a_shortened_address_keeps_the_domain_and_the_whole_one_on_hover():
    """Which mailbox this came from is what identifies a sender at a glance. A truncation that cut
    the domain off would make every address on the page end in the same meaningless prefix."""
    short = routes._short_address("Rahulconsciouscreations@outlook.com")
    assert short.endswith("@outlook.com")
    assert short.startswith("Rahul")
    assert len(short) < len("Rahulconsciouscreations@outlook.com")
    assert routes._short_address("ap@premierpm.com") == "ap@premierpm.com", "short ones are left"
    cell = str(routes._subject_cell("Delivered", "Rahulconsciouscreations@outlook.com"))
    assert 'title="Rahulconsciouscreations@outlook.com"' in cell


def test_the_mail_export_carries_every_row_and_no_formulas():
    """An export that silently stopped at the page on screen would be worse than none, because
    nothing about the file would say it was partial. And every cell in it is a string a mail server
    chose — a spreadsheet runs one that opens with `=`."""
    client = TestClient(app)
    response = client.get("/ui/mails.csv")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    lines = [line for line in response.text.splitlines() if line.strip()]
    page_rows = client.get("/ui/mails").text.count('<tr class="clickable"')
    assert len(lines) - 1 == page_rows, "the file and the page disagree about how much mail there is"
    assert routes._csv_safe(["=cmd|' /c calc'!A0"])[0].startswith("'=")
    assert routes._csv_safe(["plain"])[0] == "plain"


def test_a_row_opens_the_message_from_its_own_store():
    """The two stores hold different message ids. A live id looked up in the corpus reports the
    mail missing, which is what would happen if every row shared one `src`."""
    client = TestClient(app)
    for src in ("sample", "inbox"):
        frag = _first_frag(client, "/ui/mails", contains=f"src={src}")
        assert client.get(frag).status_code == 200, src


def test_the_old_inbox_url_redirects_rather_than_404s():
    """It was a sidebar entry for weeks; the URL is the kind of thing that ends up in a bookmark."""
    response = TestClient(app).get("/ui/inbox", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/ui/mails"


def test_the_shipped_mail_source_is_the_live_inbox_and_nothing_else(monkeypatch):
    """What Premier actually sees: one source, their own mailbox.

    The corpus was retired on 2026-08-12. This asserts the *production* value captured at import,
    not the one the autouse fixture substitutes — otherwise the fixture would be testing itself and
    a corpus entry could reappear in the shipped tuple with every test still green.
    """
    assert PRODUCTION_MAIL_SOURCES == (("inbox", "Inbox", "PIPELINE_STATE_DB_PATH"),), (
        "the corpus is back on a product surface")

    monkeypatch.setattr(read_views, "MAIL_SOURCES", PRODUCTION_MAIL_SOURCES)
    body = TestClient(app).get("/ui/mails").text
    assert 'class="badge badge-sample"' not in body, "corpus rows are on screen"
    assert "<!doctype html>" in body


def test_the_mail_page_does_not_reach_graph_while_rendering(monkeypatch):
    """The strongest form of the old assertion: the page does not merely *survive* a Graph outage,
    it never asks.

    `operations.inbox.load` walks up to ten pages sequentially at a measured ~3.2s each, cached
    sixty seconds in process — so about once a minute, opening this page meant waiting on Microsoft
    before a row was drawn, and a restart made every first visit pay it. The unprocessed count now
    comes from `mail_arrivals`, which the background watch keeps current.
    """
    from operations import inbox as inbox_reader

    def boom(*args, **kwargs):
        raise AssertionError("the mail page reached Graph while rendering")

    monkeypatch.setattr(inbox_reader, "load", boom)
    body = TestClient(app).get("/ui/mails").text
    assert 'class="badge badge-sample"' in body, "the table must still render"


def test_an_explicit_mailbox_check_survives_a_mailbox_that_cannot_be_read(monkeypatch):
    """`?refresh=1` is the one path left that talks to Graph, and someone has to press it. A Graph
    outage must cost the count, never the table — which is the part that works without a network."""
    from operations import inbox as inbox_reader

    def boom(*args, **kwargs):
        raise RuntimeError("Graph is unreachable")

    monkeypatch.setattr(inbox_reader, "load", boom)
    body = TestClient(app).get("/ui/mails?refresh=1").text
    assert "Could not read the mailbox" in body
    assert 'class="badge badge-sample"' in body, "the table must still render"


def test_a_purchase_order_page_leads_with_where_the_goods_are():
    """The question being asked. The bar underneath shows how it got there and what has not
    happened; neither answers "where is it now" without being read across."""
    body = TestClient(app).get("/ui/po/206725").text
    assert 'class="where"' in body
    assert "since 2025-" in body, "the headline must say since when, with a real date"
    assert "Received by Miguel C. at" in body, "and where, and who signed"


def test_the_bar_no_longer_shows_one_date_on_every_node():
    """The reported symptom: every stage read 2026-06-06, the day Premier forwarded the corpus.
    Asserted on the rendered page because that is where it was seen."""
    body = TestClient(app).get("/ui/po/206725").text
    assert body.count("2026-06-06") == 0, "the forward date must not appear as an event date"
    dates = set(re.findall(r"<span[^>]*>(\d{4}-\d{2}-\d{2})</span>", body))
    assert len(dates) > 1, f"every node still shares one date: {dates}"


def test_an_unreached_node_says_not_yet_rather_than_going_blank():
    """Blank reads as "we have no date for a thing that happened". A delivery that has not
    arrived must never look like one that has."""
    class FakeStage:
        key, label, on, reached, terminal, stated = "delivered", "Delivered", "", False, False, False

    out = str(html.stepper([FakeStage()]))
    assert "not yet" in out
    assert "done" not in out, "an unreached node must not be lit"


def test_a_reached_node_with_no_date_is_distinguished_from_one_that_never_happened():
    """Reaching the warehouse implies the goods were in transit even with no carrier notice. That
    node happened and simply has no date — calling it "not yet" would deny a real event."""
    class Reached:
        key, label, on, reached, terminal, stated = "in_transit", "In transit", "", True, False, False

    out = str(html.stepper([Reached()]))
    assert "date not stated" in out
    assert "not yet" not in out


def test_the_delivery_bar_fragment_carries_the_stages_and_the_receipt_address():
    fragment = TestClient(app).get("/ui/po/208491/bar").text
    assert 'class="stepper"' in fragment
    assert delivery_status.STATUS_LABELS[delivery_status.DELIVERED] in fragment
    # The address is what lets a reader judge whether that was the final destination.
    assert "Received by Miguel C. at Crown Worldwide" in fragment
    assert "/ui/po/208491" in fragment, "a way through to the full purchase order"


def test_the_receiver_sheet_lives_on_one_page_now():
    """It was the same report over the same data as `/ui/report` — one sheet on two pages — and
    that route's own docstring said one of the two should go and that the choice was Premier's.
    Dropped from Records on 2026-08-22 by that decision.

    What must not go with it: the sheet's search, its group pager and a way to the workbook. The
    duplicate was the thing to remove, not the controls that made the sheet readable.
    """
    records = TestClient(app).get("/ui/records").text
    assert 'id="receipt-sheet"' not in records, "the duplicate sheet is back on Records"
    assert "receiver.xlsx" not in records

    report = TestClient(app).get("/ui/report").text
    assert 'id="receipt-sheet"' in report
    assert 'data-filter="receipt-sheet"' in report, "the sheet lost its search on the way over"
    assert 'href="/ui/report.xlsx"' in report


def test_the_receiver_report_offers_its_download_even_when_it_is_empty():
    """The empty state is exactly when someone most wants the file — "there is nothing to receive
    yet, show me". The buttons used to be *replaced* by the empty message, so the one state that
    needed proof was the one state with no way to get it.

    The live mailbox holds only internal broadcasts today, so this page is empty on a real run;
    that is the condition being asserted, not a contrived one.
    """
    client = TestClient(app)
    body = client.get("/ui/report").text
    assert 'href="/ui/report.xlsx"' in body, "no way to download the report"
    assert 'data-open="preview"' in body, "no way to preview it"
    # And it must still say why there is nothing in it, rather than looking broken.
    assert 'class="empty"' in body
    # The file itself is valid whether or not it has rows in it.
    workbook = client.get("/ui/report.xlsx")
    assert workbook.status_code == 200
    assert workbook.content[:2] == b"PK"


def test_the_records_page_offers_verify_per_row_and_for_the_whole_page():
    """Both entry points, on the Records page and nowhere else.

    The per-row button is what a reader reaches for while working down the table; the page-level
    one is what answers "is any of this wrong" without thirteen clicks. Neither is a link — the
    endpoint reads Spitfire and rewrites the local mirror, so a prefetch must not be able to start
    it.
    """
    body = TestClient(app).get("/ui/records").text
    assert 'data-verify="/ui/records/verify"' in body, "no page-level verify button"
    per_row = re.findall(r'data-verify="(/ui/records/\d+/verify)"', body)
    assert per_row, "no per-row verify buttons"
    assert 'href="/ui/records/verify"' not in body, "verify must not be a link"
    for markup in re.findall(r'<button[^>]*data-verify[^>]*>', body):
        assert "onclick" not in markup, markup
        assert 'type="button"' in markup, markup


def test_the_verify_controls_are_where_someone_can_actually_find_them():
    """Placement, pinned — because "the markup is on the page" was true while nobody could find it.

    Both controls shipped in the two worst spots available: the page-level button after a five-line
    note paragraph, and the per-row cell as the eighteenth column of a table that scrolls sideways.
    Presence is not discoverability, so both positions are asserted rather than left to drift back.
    """
    body = TestClient(app).get("/ui/records").text

    # The page-level button sits beside the title, above everything it acts on.
    assert body.index('data-verify="/ui/records/verify"') < body.index('id="records-table"'), \
        "the page-level button is below the table it acts on again"
    assert 'class="bar-actions"' in body

    # Straight after the selection box and "#". Read through the wrapper: a sortable heading is a
    # `<button>` inside its `<th>`, while Verify is in `no_sort` and stays a bare cell — so
    # matching a `<th>` literally sees only half the columns.
    table = body[body.index('id="records-table"'):]
    assert column_labels(table)[:4] == ["", "#", "Verify", "Post"], \
        f"the controls are not where they were pinned: {column_labels(table)[:5]}"


def test_verify_all_narrows_to_the_ticked_rows():
    """One control, two meanings, and the button says which it is about to do: with nothing ticked
    it verifies everything and reads "Verify all against Spitfire"; with rows ticked it verifies
    those and says how many. A control that is disabled until someone guesses a checkbox turns it
    on would be the alternative, and it teaches nobody anything.
    """
    body = TestClient(app).get("/ui/records").text
    assert 'data-verify-selection="records-table"' in body
    assert 'data-verify-all="Verify all against Spitfire"' in body
    assert 'class="pick-all"' in body and 'data-pick-all="records-table"' in body
    picks = re.findall(r'<input type="checkbox" class="pick" data-pick-for="records-table" '
                       r'value="(\d+)"', body)
    assert picks, "no per-row selection boxes"
    # The script is what turns ticks into a narrower request; both halves are pinned, because
    # either alone is a control that quietly acts on everything.
    assert "pickedIn(scope)" in html._JS
    assert "'ids=' + encodeURIComponent" in html._JS


def test_a_selection_can_only_narrow_what_verify_touches():
    """`?ids=` is a filter over what the page was already offering, never a way to name something
    else: an id that is not in `records_ready` is dropped rather than looked up."""
    client = TestClient(app)
    everything = client.post("/ui/records/verify")
    assert everything.status_code == 200
    stranger = client.post("/ui/records/verify?ids=999999")
    assert stranger.status_code == 200
    assert "None of the selected records are still listed here." in stranger.text


def test_the_records_export_carries_every_row():
    """Same rule as Mail's and Delivery status': the file is the whole table."""
    client = TestClient(app)
    response = client.get("/ui/records.csv")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    lines = [line for line in response.text.splitlines() if line.strip()]
    assert len(lines) - 1 == client.get("/ui/records").text.count('class="pick" data-pick-for=')
    # Two columns the page carries as controls rather than as text.
    assert "Complete" in lines[0] and "Posted" in lines[0]


def test_a_row_can_be_in_two_states_at_once():
    """An attachment can be a signature logo *and* a file nothing could read. Comparing the whole
    attribute made such a row match neither view: on the live store three of sixty fell through
    both "Real files only" and "Inline images only" and were reachable from neither."""
    marker = html._JS.split("var own = row.getAttribute('data-choice-value')")[1][:420]
    assert "split(" in marker and "choices.some" in marker,         "the match must be an intersection of words, not an equality"
    body = markup_of("/ui/attachments")
    assert '<option value="inline">Inline images only</option>' in body
    # Every listed row falls under one of the two file/inline views, so neither hides a row from
    # both. That is the arithmetic the bug broke.
    states = re.findall(r'data-choice-value="([^"]*)"', body)
    assert states, "no row carries its state"
    assert all("inline" in w.split() or "file" in w.split() for w in states)


def test_the_records_page_can_be_narrowed_to_what_needs_a_person():
    """The Complete cell holds a badge, a Fill button and a tooltip, so its rendered text is
    "3 gaps Fill" — nothing a dropdown could name. The row carries the state instead."""
    body = markup_of("/ui/records")
    assert 'data-choice-value="gaps"' in body or 'data-choice-value="complete"' in body
    assert '<option value="gaps">My triage queue</option>' in body
    assert "row.getAttribute('data-choice-value')" in html._JS


def test_something_listens_for_data_verify_and_posts_it():
    """The attribute alone does nothing. This is the failure the preview button already shipped
    once — markup with no listener — so the pairing is pinned rather than assumed."""
    assert "data-verify" in html._JS, "nothing listens for data-verify"
    assert "'POST'" in html._JS, "the verify fetch must not be a GET"


def test_a_verify_button_inside_a_row_does_not_also_open_the_row_dialog():
    """The Verify cell sits inside a `tr[data-frag]`, so one click could plausibly do two things.
    The row handler bails on any click landing in an `a` or `button`, and the verify branch is
    checked before it — both halves are needed and both are asserted here."""
    script = html._JS
    assert script.index("data-verify") < script.index("tr[data-frag]"), \
        "the verify branch must be checked before the row branch, or it is unreachable"
    # `input` and `label` joined the list on 2026-08-22 with the selection checkbox: ticking a row
    # also opened the popup over the table you were ticking in.
    assert "closest('a,button,input,label')" in script


def test_every_dialog_opener_has_a_dialog_and_a_handler_to_open_it():
    """`data-open="X"` needs three things to work, and the preview button shipped with only two:
    the attribute, a `<dialog id="X">` on the page, and a listener in `_JS`. The button and the
    dialog came across from the console when the UIs were merged; the listener did not, so
    Preview report did nothing at all.

    Asserted over every page so a fourth dialog cannot be added the same way.
    """
    assert "data-open" in html._JS, "nothing listens for data-open"
    client = TestClient(app)
    seen = 0
    for path in UI_PAGES + ("/ui/report", "/ui/automation", "/ui/inbox"):
        body = client.get(path).text
        for target in set(re.findall(r'data-open="([^"]+)"', body)):
            seen += 1
            assert f'<dialog id="{target}"' in body, f"{path}: no dialog for data-open={target}"
    assert seen, "no dialog openers found at all — has the preview button been removed?"


def test_the_receiver_report_preview_carries_the_sheet_itself():
    """The preview is server-rendered into the dialog rather than fetched, so it must arrive with
    the page. An empty dialog would open and show nothing, which looks identical to a broken one."""
    body = TestClient(app).get("/ui/report").text
    assert '<dialog id="preview"' in body
    assert "Premier Design to Completion Report" in body, "the sheet is not in the dialog"


# --- the message popup's layers, its one scrollbar, and the page behind it ---------------------
# Three defects reported together, all in the same dialog: opening an attachment and closing it
# threw the message away with it; the popup, the frame inside it and the page behind it each had
# their own scrollbar; and the page behind scrolled under the backdrop.


def test_the_message_dialog_does_not_close_itself_natively():
    """`<form method="dialog">` closes unconditionally, with no JS involved and nothing it can be
    told. That is why closing an attachment closed the message underneath it — the dialog has
    layers and a native submit cannot know that. The ✕ must route through `_JS` instead.

    `modal()`'s dialog keeps its native close and is deliberately not covered here: it holds one
    server-rendered sheet and has no layers to step back through."""
    assert 'method="dialog"' not in html._MAIL_DIALOG, \
        "the ✕ closes natively again — layers cannot survive it"
    assert "data-dialog-back" in html._MAIL_DIALOG, "the ✕ carries no attribute for _JS to act on"
    assert "data-dialog-back" in html._JS, "nothing listens for data-dialog-back"


def test_escape_steps_back_through_the_layers_before_closing():
    """Escape on a `<dialog>` is the browser's, not ours. The only place to intervene is `cancel`,
    which fires first and can be prevented — without that, one keystroke from an open attachment
    discards the message and the reader has to find the row again."""
    assert "'cancel'" in html._JS, "nothing handles the dialog's cancel event"
    handler = html._JS.split("'cancel'")[1][:700]
    assert "preventDefault" in handler, \
        "cancel is observed but not prevented, so Escape still closes through the layers"
    # Chrome fires this uncancelable when there is no fresh user activation, and Escape grants
    # none — so a second Escape with no click in between lands there. Stepping back anyway would
    # pop the layer AND let the dialog close, losing the message: the worst of both.
    assert "e.cancelable" in handler, \
        "the handler pops even when it cannot stop the close, which loses the layer it popped"
    assert "onDialogClosed" in html._JS, "nothing recovers the layer an uncancelable Escape took"


def test_back_to_message_pops_a_layer_rather_than_stacking_another():
    """It carries a `data-frag` like every other control, so without a marker the loader would
    treat a return as a new destination and ten open-and-backs would leave ten layers to press
    Escape through. The `data-frag` stays: opened cold from /ui/attachments there is no message
    underneath, and the button must still fetch one."""
    for markup in (
        html.attachment_viewer(filename="pod.pdf", size=10, label="PDF", verdict="",
                               body=html.Raw(""), download_url="/d", mail_url="/ui/mail?id=x"),
        html.attachment_viewer_missing(mail_url="/ui/mail?id=x"),
    ):
        back = re.search(r"<button[^>]*>\u2039 Back to message</button>", markup)
        assert back, "the viewer has no way back at all"
        assert 'data-back="1"' in back.group(0)
        assert 'data-frag="/ui/mail?id=x"' in back.group(0), "the cold-open fallback is gone"

    assert "data-back" in html._JS, "nothing treats data-back as a return"


def test_opening_a_dialog_locks_the_page_behind_it():
    """`showModal()` does not scroll-lock the document. Without this a wheel gesture over the
    dimmed backdrop scrolls the table underneath, which reads as the popup itself moving."""
    assert "modal-open" in html._JS, "openDialog never marks the document"
    assert "html.modal-open { overflow:hidden; }" in html._CSS
    assert "scrollbar-gutter: stable" in html._CSS, \
        "hiding the page scrollbar will shift the layout sideways as the popup opens"


def test_the_popup_has_exactly_one_scroll_container():
    """The dialog clips and the body scrolls. It used to be `max-height:calc(94vh - 42px)` on the
    body — arithmetic hard-coding the head's height — so a head that wrapped turned the dialog
    itself into a second scroller and pushed the sticky head out of view."""
    dialog = re.search(r"dialog\.modal \{[^}]*\}", html._CSS).group(0)
    body = re.search(r"\.modal-body \{[^}]*\}", html._CSS).group(0)

    assert "overflow:hidden" in dialog, "the dialog itself is a scroll container again"
    assert "flex-direction:column" in html._CSS, "nothing lays the head and body out as a column"
    assert "overflow:auto" in body and "min-height:0" in body, \
        "min-height:0 is load-bearing — a flex item will not shrink below its content without it"
    assert "calc(" not in body, "the body is sized by layout now, not by guessing the head's height"


def test_a_closed_dialog_is_never_given_a_display():
    """The trap this test exists to stop anyone falling into twice.

    The UA sheet hides a closed dialog with `dialog:not([open]) { display:none }`. An author rule
    setting `display` on a bare `dialog.modal` outranks that, so the CLOSED dialog stays laid out
    across the page and silently swallows every click underneath it — the popup opens once and the
    page behind is dead from then on. Nothing in the markup or the open popup looks wrong; it took
    a browser and a hit test to find. Every `display` on this dialog must be scoped to `[open]`.
    """
    for match in re.finditer(r"(dialog\.modal[^{,]*)\{([^}]*)\}", html._CSS):
        selector, block = match.group(1).strip(), match.group(2)
        if "display:" in block.replace(" ", ""):
            assert "[open]" in selector, \
                f"`{selector}` sets display on a dialog that may be closed — it will eat clicks"


def test_the_message_frame_is_measured_even_when_it_has_already_parsed():
    """The frames arrive by innerHTML with their document in `srcdoc`, so the load event has
    usually fired before anything can listen for it. Attaching a listener and nothing else meant
    the frame kept its fallback height and scrolled internally — the second scrollbar people
    actually hit."""
    fit = html._JS[html._JS.index("function fitFrames"):]
    fit = fit[:fit.index("\n}\n")]

    assert "readyState" in fit, "fitFrames still waits for a load event that has already fired"
    assert "ResizeObserver" in fit, "nothing re-measures once inline images land"
    assert "view-frame" in fit, "the attachment's HTML preview is left scrolling internally"
    assert "view-pdf" not in fit, "the PDF plugin owns its own scroller and must keep its height"


def test_an_empty_receiver_report_says_where_the_populated_one_is():
    """A page reading 0 / 0 / 0 with no onward path reads as broken. The corpus report has real
    purchase orders in it, and pointing at it is what stops someone concluding nothing works."""
    body = TestClient(app).get("/ui/report").text
    assert 'href="/ui/records"' in body


def test_the_message_popup_renders_a_sandboxed_body():
    client = TestClient(app)
    fragment = client.get(_first_frag(client, "/ui/mails", contains="src=sample")).text

    assert "could not be found" not in fragment
    sandbox = re.search(r'sandbox="([^"]*)"', fragment)
    assert sandbox, "the message body must always be framed and sandboxed"
    assert "allow-scripts" not in sandbox.group(1)
    assert "<script" not in fragment, "the fragment itself must never carry script"


def test_an_attachment_that_was_not_retained_404s_rather_than_serving_nothing():
    """19 of the corpus's 72 attachments were dropped at ingest. A 0-byte 200 labelled image/png
    gives the browser something unrenderable and no reason why."""
    client = TestClient(app)
    email_id = parse_qs(urlparse(_first_frag(client, "/ui/mails", contains="src=sample")).query)["id"][0]
    response = client.get("/ui/mail/attachment", params={"id": email_id, "n": 9999})
    assert response.status_code == 404


def test_attachments_are_served_with_nosniff_and_a_csp():
    """Attacker-supplied files. Without nosniff a browser may decide an HTML attachment is HTML and
    run it against this origin — and the corpus contains exactly such an attachment."""
    client = TestClient(app)
    email_id = parse_qs(urlparse(_first_frag(client, "/ui/mails", contains="src=sample")).query)["id"][0]
    response = client.get("/ui/mail/attachment", params={"id": email_id, "n": 0})
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in response.headers["content-security-policy"]


# --- The attachment viewer ------------------------------------------------------
# `/ui/mail/attachment` serves the raw file and `/ui/mail/attachment/view` renders it. Two routes
# with different jobs, and the split is the security boundary: the viewer made every kind readable
# without relaxing what the raw route will hand a browser.

def _sample_email_id(client) -> str:
    return parse_qs(urlparse(_first_frag(client, "/ui/mails", contains="src=sample")).query)["id"][0]


def test_the_raw_route_still_refuses_to_serve_anything_but_images_and_pdfs_inline():
    """The assertions the CSP test above omits, so a future relaxation fails loudly rather than
    quietly. An HTML attachment served inline from this origin is script running as trusted page
    code — the viewer exists precisely so nobody ever needs to loosen this."""
    client = TestClient(app)
    email_id = _sample_email_id(client)
    for n in range(6):
        response = client.get("/ui/mail/attachment", params={"id": email_id, "n": n,
                                                             "src": "sample"})
        if response.status_code != 200:
            continue
        content_type = response.headers["content-type"].split(";")[0]
        disposition = response.headers["content-disposition"]
        if content_type.startswith("image/") or content_type == "application/pdf":
            assert disposition.startswith("inline"), f"n={n}"
        else:
            assert content_type == "application/octet-stream", f"n={n} served as {content_type}"
            assert disposition.startswith("attachment"), f"n={n}"


def test_download_forces_an_attachment_disposition_even_for_an_image():
    client = TestClient(app)
    email_id = _sample_email_id(client)
    response = client.get("/ui/mail/attachment",
                          params={"id": email_id, "n": 0, "src": "sample", "download": 1})
    if response.status_code == 200:
        assert response.headers["content-disposition"].startswith("attachment")
        assert response.headers["content-type"].split(";")[0] == "application/octet-stream"


def test_the_viewer_opens_an_attachment_and_offers_a_way_back():
    client = TestClient(app)
    email_id = _sample_email_id(client)
    body = client.get("/ui/mail/attachment/view",
                      params={"id": email_id, "n": 0, "src": "sample"}).text
    assert "Back to message" in body
    assert "/ui/mail?id=" in body, "the way back must name the message"
    assert "<script" not in body


def test_the_viewer_is_a_dead_end_for_nothing():
    """Attachments genuinely are dropped at ingest, so 'no bytes' is a real state — but it must
    still offer the way back rather than stranding the reader in an empty dialog."""
    client = TestClient(app)
    email_id = _sample_email_id(client)
    body = client.get("/ui/mail/attachment/view",
                      params={"id": email_id, "n": 9999, "src": "sample"}).text
    assert "Back to message" in body
    assert "not retained" in body or "not inside" in body


def test_the_message_list_makes_every_attachment_openable():
    """Whatever the file is. This list used to carry the preview itself, so the ten kinds with no
    branch showed a filename and nothing else."""
    client = TestClient(app)
    fragment = client.get(_first_frag(client, "/ui/mails", contains="src=sample")).text
    if "att-title" not in fragment:
        pytest.skip("the first sample message has no attachments")
    assert "/ui/mail/attachment/view" in fragment
    assert "Download" in fragment


def test_a_malformed_child_path_is_not_a_partial_one():
    """`child` arrives from a query string. Half-parsing it would walk somewhere nobody asked for."""
    from api.ui import routes

    assert routes._child_path("0.2") == [0, 2]
    assert routes._child_path("") == []
    assert routes._child_path("0.x") == [], "a bad segment must void the whole path"
    assert routes._child_path("../../etc") == []


def test_a_container_child_is_not_offered_a_download_that_would_404():
    """The raw route serves top-level attachments by ordinal; a member inside an archive has no
    bytes of its own there, so offering the link would be offering a 404."""
    out = html.attachment_viewer(filename="inner.txt", size=12, label="Text", verdict="",
                                 body=html.Raw("<p>hi</p>"), download_url="/x",
                                 mail_url="/ui/mail?id=a", downloadable=False)
    assert "Download" not in out
    assert "Back to message" in out


def test_the_records_page_shows_delivery_status_per_row():
    body = TestClient(app).get("/ui/records").text
    # Named "Status" since 2026-08-22 — the same word the Delivery status page uses for the same
    # badge, rather than two names for one fact.
    assert ">Status<" in body
    assert 'href="/ui/po/208491"' in body
    # Delivered, not "At partnered warehouse": the Authority notice states the goods were received
    # and signed for, and that is the event that creates a receiver.
    assert delivery_status.STATUS_LABELS[delivery_status.DELIVERED] in body


def test_the_receiver_report_carries_the_receipt_log_and_its_caveat():
    body = TestClient(app).get("/ui/report").text
    assert "Premier Design to Completion Report" in body
    assert "Receipt Log" in body
    assert "report.xlsx" in body
    # The caveat moved on 2026-08-21: Order Qty and Net now fill from the mirrored purchase order,
    # so the sheet no longer claims they never can. Final still cannot be worked out at all.
    assert "Final is always blank" in body
    assert "cannot be worked out from quantities" in body


def test_the_records_page_says_how_each_record_was_made():
    """Both states are labelled. Badging only the manual ones would make an unbadged row mean two
    things — automated, or a row from before the column existed."""
    body = TestClient(app).get("/ui/records").text
    assert ">Origin<" in body
    assert "Automated" in body


UI_PAGES = ("/ui/mails", "/ui/records", "/ui/manual", "/ui/po", "/ui/po/208491",
            "/ui/attachments")


def test_pages_carry_exactly_one_first_party_script_and_nothing_else():
    """These pages were script-free until the message popup needed `fetch` and `showModal`.

    The property worth holding was never "no script tag" — it was that nothing a mail server sent
    us can execute. So: exactly one inline block, no `src=` pulling code from anywhere, and no
    inline event handlers, which are the attribute a smuggled string could land in.
    """
    client = TestClient(app)
    for path in UI_PAGES:
        body = client.get(path).text
        assert body.count("<script>") == 1, path
        assert "<script src" not in body and "<script\n" not in body, path
        for handler in ("onclick=", "onerror=", "onload=", "onmouseover=", "javascript:"):
            assert handler not in body, f"{path} carries {handler}"


def test_the_script_block_is_the_one_we_wrote():
    """Pins the payload itself: if the block ever stops being `html._JS` verbatim, something is
    injecting into it."""
    body = TestClient(app).get("/ui/mails").text
    assert f"<script>{html._JS}</script>" in body


def test_a_hostile_subject_cannot_reach_the_script_or_a_row_attribute():
    """Subjects and email ids land in `data-` attributes that the script reads back. Escaping is
    what keeps a crafted subject from closing the attribute and writing its own."""
    out = str(html.table(["Subject"], [[HOSTILE]],
                         mail_ids=[('" onmouseover="alert(1)', HOSTILE)]))
    assert 'onmouseover="alert(1)"' not in out
    assert "<script>" not in out
    assert "&quot;" in out


# --- Table search -------------------------------------------------------------
# Filtering runs in the browser because neither list view carries a LIMIT — every row is already
# in the page, so a `?q=` round trip would be slower than typing and could disagree with what is
# on screen. These tests pin the contract the script depends on.

SEARCHABLE_PAGES = {"/ui/mails": "mail-table", "/ui/records": "records-table",
                    "/ui/attachments": "attachments-table"}


@pytest.mark.parametrize("path,table_id", sorted(SEARCHABLE_PAGES.items()))
def test_the_searchable_pages_carry_a_filter_pointed_at_their_table(path, table_id):
    """The input names a table id, and that id must actually exist on the page — a filter pointing
    at nothing fails silently, which is the one failure mode a user would read as "no results"."""
    body = TestClient(app).get(path).text
    assert f'data-filter="{table_id}"' in body, path
    assert f'id="{table_id}"' in body, path
    assert f'data-count-for="{table_id}"' in body, path


def markup_of(path: str) -> str:
    """A page with its one inline script removed.

    The script builds its selectors by concatenation — `'[data-pager-for="' + id + '"]'` — so a
    naive scan for those attributes finds the JavaScript's own string fragments and reports a pager
    pointing at `' + id + '`. These assertions are about the markup, so the script comes out first.
    """
    return re.sub(r"<script>.*?</script>", "", TestClient(app).get(path).text, flags=re.S)


def test_every_filter_on_a_page_points_at_a_table_that_exists():
    """The general form of the assertion above, over every page. `/ui/records` grew a second box
    for the receiver report sheet and `/ui/manual` has one covering three tables at once, so
    counting inputs stopped being the useful check — pointing at a real id is."""
    for path in UI_PAGES:
        body = markup_of(path)
        for targets in re.findall(r'<input type="search"[^>]*data-filter="([^"]+)"', body):
            for table_id in targets.split():
                assert f'id="{table_id}"' in body, f"{path}: filter targets missing {table_id}"


def test_the_filter_adds_no_second_script_and_no_external_one():
    """The filter had to go inside `_JS`. A second block would break the one-script budget these
    pages are held to, and `src=` would let code arrive from somewhere we do not control."""
    assert "<script" not in html._JS
    assert "refreshView" in html._JS
    for path in SEARCHABLE_PAGES:
        body = TestClient(app).get(path).text
        assert body.count("<script>") == 1, path
        assert "<script src" not in body, path


def test_the_search_input_is_escaped_by_construction():
    out = str(html.search_box(HOSTILE, placeholder=HOSTILE, label=HOSTILE))
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_a_row_hidden_by_the_filter_is_hidden_by_class_not_deleted():
    """`filtered-out` is a display rule, so clearing the box restores every row without a reload —
    and nothing that was rendered is ever thrown away."""
    assert ".filtered-out" in html._CSS
    assert "display:none" in html._CSS.split(".filtered-out")[1][:40]


def test_filtering_turns_off_the_nth_child_zebra():
    """`nth-child(even)` counts hidden rows, so a filtered table stripes at random. The script
    assigns `.alt` to visible rows instead and this rule is what lets it win."""
    assert ".scroll.filtering tbody tr:nth-child(even)" in html._CSS
    assert ".scroll.filtering tbody tr.alt" in html._CSS
    assert "row.classList.toggle('alt'" in html._JS


def test_the_filter_searches_title_attributes_not_just_visible_text():
    """`_clipped()` truncates Subject and Why in the DOM and keeps the whole string only in
    `title`. Filtering on rendered text alone would report "nothing found" for mail sitting right
    there, which is the bug this line prevents."""
    assert "querySelectorAll('[title]')" in html._JS


def test_an_active_filter_stops_the_poller_reloading_the_page():
    """The ten-second poller reloads when it is safe to. Someone mid-search is reading a filtered
    table; reloading drops them back at every unfiltered row."""
    assert "anyFilterActive" in html._JS
    guard = html._JS.split("function safeToReloadWithoutAsking")[1][:400]
    assert "!anyFilterActive()" in guard


def test_a_table_without_an_id_is_unchanged():
    """Every existing call site passes no `table_id`, and none of them should gain an attribute."""
    out = str(html.table(["A"], [["x"]]))
    assert out.startswith('<div class="scroll">')
    assert "id=" not in out
    assert "pager" not in out


# --- Pagination ---------------------------------------------------------------
# Paging is in the browser for the same reason the filter is: neither list view has a LIMIT, so
# every row is already here. That is what lets a search span the whole table instead of only the
# page you are on — a server-side ?page= would hide matches on other pages while still saying
# "no rows match".

PAGED_TABLES = {
    "/ui/mails": "mail-table",
    "/ui/records": "records-table",
    "/ui/po": "po-table",
    "/ui/attachments": "attachments-table",
}


@pytest.mark.parametrize("path,table_id", sorted(PAGED_TABLES.items()))
def test_a_paged_table_carries_a_pager_bound_to_it(path, table_id):
    body = TestClient(app).get(path).text
    assert f'data-pager-for="{table_id}"' in body, path
    assert 'data-page="prev"' in body and 'data-page="next"' in body, path


def test_a_pager_needs_a_table_id_to_attach_to():
    """`page_size` without `table_id` has nothing to control. Silently emitting an unbound pager
    would put dead buttons on the page."""
    assert "pager" not in str(html.table(["A"], [["x"]], page_size=10))


def test_every_pager_points_at_a_table_that_exists():
    for path in UI_PAGES:
        body = markup_of(path)
        for table_id in re.findall(r'data-pager-for="([^"]+)"', body):
            assert f'id="{table_id}"' in body, f"{path}: pager targets missing {table_id}"


def test_the_pager_uses_real_buttons_and_no_inline_handlers():
    """Same rule the whole UI is held to: nothing a mail server sent can execute, so no `onclick`
    anywhere and every control is a real element the one script listens for."""
    out = str(html.pager("t", 25))
    for handler in ("onclick=", "onchange=", "javascript:"):
        assert handler not in out
    assert '<button' in out and 'type="button"' in out


def test_the_page_size_offered_includes_the_default_and_all():
    """"All" has to be reachable: the receiver report is meant to be read against Spitfire's own
    sheet, and someone doing that needs the whole thing on one screen."""
    out = str(html.pager("t", 10))
    assert 'value="10"' in out and 'selected="selected"' in out
    assert 'value="0"' in out and ">All<" in out


def test_the_pager_says_which_rows_are_on_screen_and_offers_them_by_number():
    """"Page 2 of 5" does not say whether the thing you are looking for is in this table at all.
    The numbers themselves are written by the script, because how many pages there are depends on
    what the filter left — a server-rendered "1 2 3" would be wrong the moment anyone typed."""
    out = str(html.pager("t", 25))
    assert 'class="pager-showing"' in out
    assert 'class="pager-nums"' in out
    assert "Showing " in html._JS and "paintPages" in html._JS
    assert "'page-btn on'" in html._JS, "the page you are on has to be marked"
    # Prev and Next step; a numbered button goes straight to that page.
    assert "parseInt(want, 10)" in html._JS


def test_a_long_run_of_pages_is_windowed_rather_than_listed():
    """Two thousand rows at 25 a page is eighty buttons, which is not a control any more."""
    numbers = re.search(r"function pageNumbers\(page, pages\) \{(.*?)\n\}", html._JS, re.S)
    assert numbers, "pageNumbers() is what decides, and it has to exist to be pinned"
    assert "pages <= 7" in numbers.group(1)
    assert "out.push(0)" in numbers.group(1), "0 is the gap between the ends and the window"


def test_a_view_may_name_several_verdicts_at_once():
    """"What is waiting on me" is one question, and answering it by picking each verdict in turn
    and adding up is not answering it."""
    out = str(html.choice_filter("t", [("hold|not read yet", "My triage queue")],
                                 label="View", all_label="All mail", boxed=True))
    assert 'value="hold|not read yet"' in out
    assert "choice.split('|')" in html._JS
    assert "choices.indexOf(" in html._JS, "the match is against the list, not the whole string"


def test_paging_and_filtering_are_separate_row_states():
    """A row can be off-screen because it did not match the search or because it is on another
    page. One class for both would make each pass clobber the other's decision."""
    assert "filtered-out" in html._JS and "paged-out" in html._JS
    assert "tr.filtered-out, tr.paged-out" in html._CSS


def test_filtering_resets_to_the_first_page():
    """Filtering to four matches while parked on page three shows an empty table, and the reason
    would be invisible."""
    assert "refreshFor(e.target, true)" in html._JS


def test_the_receiver_report_pages_by_purchase_order_not_by_row():
    """A page boundary inside a purchase order would split the block someone is holding beside
    Spitfire's own Receipt Log, which is the only thing that sheet is for."""
    body = TestClient(app).get("/ui/report").text
    assert 'data-pager-for="receipt-sheet"' in body
    assert 'data-unit="group"' in body
    assert 'data-group="' in body, "the sheet's rows must be grouped for group paging to work"


def test_the_receiver_report_keeps_its_own_row_styling():
    """`.scroll` brings a zebra and a header rule the sheet must not pick up — it has `r-po`,
    `r-line` and `r-rcpt` of its own."""
    body = TestClient(app).get("/ui/report").text
    assert 'class="scroll plain" id="receipt-sheet"' in body
    assert ".scroll.plain tbody tr:nth-child(even)" in html._CSS


ALL_UI_PAGES = UI_PAGES + ("/ui/manual", "/ui/automation", "/ui/report")


def test_every_table_on_every_page_is_paged():
    """The rule, asserted rather than remembered: a table with rows in it gets a pager.

    Written as a sweep over the rendered pages instead of a list of call sites, so a table added
    later fails here rather than quietly shipping unpaged. An empty table renders as a `<p>` and
    never reaches this — there is nothing to page.
    """
    unpaged = []
    for path in ALL_UI_PAGES:
        body = markup_of(path)
        paged = set(re.findall(r'data-pager-for="([^"]+)"', body))
        for table_id in re.findall(r'<div class="scroll(?: plain)?" id="([^"]+)"', body):
            if table_id not in paged:
                unpaged.append(f"{path}:{table_id}")
        # A table rendered without an id could never be paged, because a pager needs one to name.
        assert '<div class="scroll">' not in body, f"{path} has a table with no id, so no pager"
    assert not unpaged, "tables with no pager: " + ", ".join(unpaged)


def test_every_paged_table_is_also_searchable():
    """Paging without searching is a worse table, not a better one: it hides rows and gives you no
    way to find the one you wanted except clicking Next."""
    for path in ALL_UI_PAGES:
        body = markup_of(path)
        searched = set()
        for targets in re.findall(r'<input type="search"[^>]*data-filter="([^"]+)"', body):
            searched.update(targets.split())
        for table_id in re.findall(r'data-pager-for="([^"]+)"', body):
            assert table_id in searched, f"{path}: {table_id} is paged but not searchable"


def test_the_filtered_chatter_table_is_paged_and_searchable(tmp_path, monkeypatch):
    """Neither store holds chatter-filtered mail today, so the sweeps above never reach this table
    — it renders as an empty `<p>`. Seeding one row is what proves the branch works, rather than
    proving only that it is currently unreachable.

    It joins the page's search box on purpose: "did a real delivery get filed as chatter?" is the
    question that brings someone here, and this is the last table they would think to search
    separately.
    """
    import sqlite3

    from api import deps
    from pipeline import email_log, read_views, state_db

    path = tmp_path / "live.sqlite3"
    setup = state_db.get_connection(path)
    try:
        for n in range(3):
            email_log.record(setup, email_id=f"<chatter-{n}@premier>",
                             subject=f"All-associates announcement {n}", sender="hr@premierpm.com",
                             category="hide", matched_rule=read_views.NOISE_RULE,
                             reason="internal chatter", folder="Hidden",
                             processed_at="2026-08-13 09:00:00")
    finally:
        setup.close()

    def live():
        conn = state_db.get_connection(path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[deps.get_pipeline_conn] = live
    try:
        body = re.sub(r"<script>.*?</script>", "", TestClient(app).get("/ui/manual").text, flags=re.S)
    finally:
        app.dependency_overrides.pop(deps.get_pipeline_conn, None)

    assert 'id="manual-filtered"' in body, "the chatter table did not render"
    assert 'data-pager-for="manual-filtered"' in body, "it rendered unpaged"
    targets = re.findall(r'<input type="search"[^>]*data-filter="([^"]+)"', body)
    assert targets and "manual-filtered" in targets[0].split(), "it is not covered by the search box"


def test_the_attachment_pager_matches_the_shared_one():
    """`pipeline/attachment_view.py` spells its pager out by hand — it deliberately imports neither
    UI, so it cannot call `html.pager()`. That is a real duplication, and this is what stops the two
    drifting apart silently.

    It used to live in `mail_view._spreadsheet`, which rendered a workbook inline in the message
    list; the full-size viewer replaced that, so the duplication moved with it.
    """
    from pipeline import attachment_view

    source = (Path(attachment_view.__file__)).read_text(encoding="utf-8")
    shared = str(html.pager("X", 25))
    for marker in ('class="pager"', 'data-page="prev"', 'data-page="next"',
                   'class="pager-showing"', 'class="pager-nums"', 'data-unit=',
                   'data-page-size='):
        assert marker in shared, f"html.pager() no longer emits {marker}"
        assert marker in source, (
            f"mail_view's hand-written pager is missing {marker} — it has drifted from html.pager()"
        )


def test_a_fragment_initialises_its_own_tables():
    """A spreadsheet preview arrives in a dialog long after the page's first paint, so nothing
    would have paged it."""
    assert "refreshEveryView();" in html._JS
    loader = html._JS.split("function loadFragment")[1].split("\nfunction ")[0]
    assert "refreshEveryView()" in loader


def test_run_history_is_no_longer_silently_truncated():
    """It was capped at 25 with nothing on screen saying so — "it has not run since Tuesday" and
    "you are looking at the newest 25 of 40" rendered identically."""
    import inspect

    from api.ui import routes

    assert "recent_runs(conn, limit=200)" in inspect.getsource(routes.automation_page)


# --- Sorting and the date range ------------------------------------------------

def test_every_column_sorts_except_the_ones_holding_a_control():
    """A heading is a real `<button>`: a `<th>` with a click handler is not focusable, so a column
    nobody can sort without a mouse. Verify is excluded because "sort by button" means nothing."""
    body = markup_of("/ui/records")
    section = body[body.index('id="records-table"'):]
    # 1, not 0: the selection checkbox is the first cell and sorts by nothing.
    assert '<th data-sort="1" aria-sort="none"><button' in section
    assert "<th>Verify</th>" in section, "the control column must not become a sort button"
    for handler in ("onclick=", "onkeydown="):
        assert handler not in section


def test_sorting_is_reachable_by_keyboard():
    assert 'class="sort-btn"' in markup_of("/ui/mails")
    assert 'type="button"' in markup_of("/ui/mails")
    assert ".sort-btn:focus-visible" in html._CSS


def test_the_sorted_column_says_so_to_a_screen_reader():
    assert 'aria-sort="none"' in markup_of("/ui/mails")
    assert 'th[aria-sort="ascending"]' in html._CSS
    assert "setAttribute('aria-sort'" in html._JS


def test_the_grouped_report_sheet_has_no_sortable_headings():
    """It is laid out to be read line by line beside Spitfire's own Receipt Log. Reordering its
    rows would take away the only thing it is for."""
    body = markup_of("/ui/report")
    sheet = body[body.index('id="receipt-sheet"'):]
    assert "sort-btn" not in sheet[:sheet.index("</table>")]


DATE_COLUMNS = {
    "/ui/mails": "Received",
    "/ui/records": "POD date",
    "/ui/po": "Last heard",
    "/ui/manual": "When",
    "/ui/attachments": "Received",
    "/ui/automation": "Started",
    "/ui/report": "When",
}


@pytest.mark.parametrize("path,column", sorted(DATE_COLUMNS.items()))
def test_each_page_has_a_date_range_bound_to_its_date_column(path, column):
    body = markup_of(path)
    assert 'class="date-from"' in body, path
    assert 'class="date-to"' in body, path
    assert 'data-date="1"' in body, f"{path}: no column is marked as the date column"
    marked = re.findall(r'<th[^>]*data-date="1"[^>]*>(.*?)</th>', body)
    assert marked, path
    assert column in re.sub(r"<[^>]+>", "", marked[0]), \
        f"{path}: the date column is {marked[0]!r}, expected {column!r}"


def test_the_date_column_must_name_a_real_heading():
    """Named rather than indexed on purpose — an index silently points at the wrong column the day
    someone inserts one, and Records has eighteen."""
    with pytest.raises(ValueError):
        html.table(["A", "B"], [["1", "2"]], table_id="t", date_column="Nope")


def test_a_date_range_narrows_by_the_marked_column_only():
    assert "dateColumnOf" in html._JS
    assert "data-date" in html._JS


def test_a_table_with_no_date_column_is_left_alone_by_a_range():
    """Returning -1 rather than falling back to column 0 — a range must never hide rows by
    comparing a date against a purchase order number."""
    assert "return -1;" in html._JS
    guard = html._JS.split("var dateColumn =")[1][:120]
    assert "dateColumnOf(scope) : -1" in guard


def test_both_ends_of_the_range_are_inclusive():
    """"1st to 5th" includes the 5th to everyone who is not a database."""
    body = html._JS.split("hit = unit.some")[1][:300]
    assert "day >= from" in body and "day <= to" in body


def test_blanks_sort_last_in_both_directions():
    """Both halves of this were real bugs, and neither is visible by reading the markup.

    The blank rule sat inside `compareCells`, whose result the descending branch negates — so
    reversing a column also reversed "blanks last" and led every descending sort with a column of
    em-dashes, burying the rows it was clicked to surface. It has to be settled before the
    direction is applied.
    """
    order = html._JS.split("keyed.sort(")[1][:700]
    assert "isBlank(a.key)" in order
    assert order.index("blankA !== blankB") < order.index("direction === 'descending'"), \
        "the blank rule is inside the direction flip again"


def test_an_em_dash_counts_as_an_empty_cell():
    """`html.muted('—')` is what this app renders for "no value" — testing for '' alone treated
    forty em-dashes as forty ordinary values and sorted them into the middle of the column."""
    assert "function isBlank(" in html._JS
    blank = html._JS.split("function isBlank(")[1][:200]
    assert "\\u2014" in blank, "the em-dash placeholder is not recognised as blank"
    assert str(html.muted("—")) == '<span class="muted">—</span>', \
        "the placeholder changed — isBlank in _JS must follow it"


def test_every_manual_queue_item_carries_a_date():
    """A date range can only narrow rows that have a date, and a row without one is excluded the
    moment either end is set. Attachment items were built with `when=""` hardcoded, so the whole
    attachment category vanished from the manual queue as soon as anyone picked a date — while the
    ledger had been recording `first_seen_at` all along.
    """
    import sqlite3

    from config import settings
    from pipeline import read_views, state_db

    conn = state_db.get_connection(settings.SAMPLE_STATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        queue = read_views.manual_queue(conn)
    finally:
        conn.close()
    undated = [f"{i.kind}:{i.ref[:40]}" for i in queue if not i.when]
    assert not undated, "queue items a date range would silently hide: " + ", ".join(undated[:5])


def test_the_count_speaks_for_the_date_range_too():
    """A range that hides two thirds of a table while the count sits blank is the silent-hiding
    problem the count exists to prevent."""
    assert "function narrowed(" in html._JS
    assert "if (!narrowed(input)) readout.textContent = '';" in html._JS


def test_the_manual_queue_searches_all_three_of_its_tables_at_once():
    """Someone chasing a PO number wants it found in whichever queue it landed in, not in the one
    they happened to point at."""
    body = markup_of("/ui/manual")
    targets = re.findall(r'<input type="search"[^>]*data-filter="([^"]+)"', body)
    assert targets, "the manual queue has no search box"
    assert len(targets[0].split()) > 1, "the box covers only one table"
    assert 'data-filter~=' in html._JS, "the script must match one id inside a list"


def test_every_delivery_status_has_a_badge_style():
    """A status with no matching CSS class renders as an unstyled pill — legible, but it stops
    carrying the one thing a badge is for."""
    for status in delivery_status.LIFECYCLE:
        assert f".badge-{status}" in html._CSS, status


def test_ui_root_redirects_to_mails():
    client = TestClient(app)
    response = client.get("/ui", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/ui/mails"


def test_ui_is_absent_from_the_openapi_schema():
    """The demo API's published contract must not change because of these pages."""
    client = TestClient(app)
    paths = client.get("/openapi.json").json()["paths"]
    assert not [p for p in paths if p.startswith("/ui")]


# --- Live refresh -------------------------------------------------------------
# These pages had no client-side data fetching at all — no setInterval, no SSE, no WebSocket — so
# a row could sit in the database for twenty minutes while the tab showing it stayed blank. The
# only auto-refresh was a meta tag on /ui/automation, and only while a run was in flight.

def test_the_version_endpoint_answers_without_touching_graph():
    """It is polled every ten seconds by every open tab. The Graph inbox listing is a ~3.2s round
    trip; polling that would cost more than the automation it watches."""
    response = TestClient(app).get("/ui/version")
    assert response.status_code == 200
    assert response.json()["token"]


def test_the_version_token_moves_when_mail_arrives():
    from pipeline import email_log

    from api.ui.routes import live_version_token

    before = live_version_token()
    conn = _live_store()
    try:
        email_log.record(conn, email_id="msg-version-probe", subject="probe", sender="a@b.com",
                         category="route", matched_rule="rule_7_unknown", reason="probe",
                         folder="Routed", processed_at="2026-08-12T12:00:00Z")
        assert live_version_token() != before
    finally:
        conn.execute("DELETE FROM email_log WHERE email_id = 'msg-version-probe'")
        conn.commit()
        conn.close()


def _live_store():
    from config import settings
    from pipeline import state_db

    return state_db.get_connection(settings.PIPELINE_STATE_DB_PATH)


def test_a_table_pane_sizes_itself_by_flex_not_by_arithmetic():
    """`height: calc(100vh - 400px)` was measured against a 908px-tall window. On a 642px laptop it
    fell through to its minimum and the page showed three and a half rows of twenty-seven — the
    failure that a number tuned to one screen will always eventually produce.

    The pane takes what the bands above and below it did not want instead, so no screen height is
    the right one and none is wrong.
    """
    pane = html._CSS.split(".scroll.pane {")[1].split("}")[0]
    assert "100vh" not in pane, "the pane is counting pixels against the viewport again"
    # Basis 0, not auto: from `auto` the pane starts at the height of every row it holds and has to
    # be shrunk back down, which lets one long table decide the layout for the whole column.
    assert "flex:1 1 0" in pane
    # And a definite height to grow inside — `min-height:100vh` let the column grow past the
    # window instead, which is the bug this pair was written after.
    assert ".shell:has(.scroll.pane) { height:100vh; }" in html._CSS
    for rule in (".content:has(.scroll.pane)", "main:has(.scroll.pane)",
                 "main:has(.scroll.pane) > section"):
        assert rule in html._CSS, f"{rule} is missing — the pane has no flex parent to grow into"
    # Both floors, or the table pushes the column past the viewport instead of scrolling inside it.
    assert html._CSS.count("min-height:0") >= 2


def test_the_last_run_line_is_in_the_rail_not_under_the_table():
    """It was a `<footer>` with 40px of padding under every table, spending a whole row of every
    grid on a line that changes a few times a day. The rail had the space and was not using it."""
    body = TestClient(app).get("/ui/mails").text
    assert "<footer" not in body, "the footer is back under the table"
    assert 'class="last-run"' in body
    assert body.index('class="last-run"') < body.index('class="content"'), \
        "the line must be inside the sidebar, which is rendered before the content column"
    # Clipped to the minute: the raw value carries microseconds and a UTC offset, and the rail is
    # 232px wide.
    shown = re.search(r'class="last-run">([^<]*)<', body).group(1)
    assert shown.startswith("Last run ") or shown == "Never run."
    assert "." not in shown and "+" not in shown, f"still the raw timestamp: {shown!r}"
    # Collapsed to 58px, the rail drops it like every other label.
    assert ".rail .side-foot .last-run" in html._CSS


def test_every_page_stamps_a_version_for_the_poller_to_compare():
    client = TestClient(app)
    for path in UI_PAGES:
        assert 'data-version="' in client.get(path).text, path


def test_the_pill_starts_hidden():
    """It must never be on screen until there is genuinely something new."""
    body = TestClient(app).get("/ui/mails").text
    assert 'id="live-pill"' in body
    assert "hidden" in body.split('id="live-pill"')[1][:120]


def test_the_poller_lives_in_the_one_inline_script():
    """`test_pages_carry_exactly_one_first_party_script_and_nothing_else` is the budget; the
    poller had to fit inside it rather than buy a second tag."""
    body = TestClient(app).get("/ui/mails").text
    assert body.count("<script>") == 1
    assert "/ui/version" in body
    assert "setInterval" in body


def test_the_page_will_not_reload_over_an_open_dialog_or_a_scrolled_reader():
    """The guard that keeps auto-reload from yanking a POD out from under someone mid-read."""
    body = TestClient(app).get("/ui/mails").text
    assert "dialog[open]" in body
    assert "scrollY" in body
    assert "visibilityState" in body


# --- Opening a message from a table ------------------------------------------
# `/ui/manual` could not open any mail at all. `table(mail_ids=)` cannot express `src=`, and
# `_store_for` resolves an absent `src` to the retired `.msg` corpus on purpose — so every row on
# the page asked a folder of test files for Premier's live mail and, correctly, found nothing.
# The failure was silent: a dialog reading "could not be found in any source we can still read".

MAIL_OPENING_PAGES = ("/ui/manual", "/ui/records", "/ui/mails", "/ui/attachments")


def test_every_control_that_opens_a_message_names_its_store():
    """The regression guard. A `/ui/mail` URL without `src` does not error — it quietly searches
    the wrong database and reports the message missing."""
    client = TestClient(app)
    for path in MAIL_OPENING_PAGES:
        body = client.get(path).text
        urls = re.findall(r'data-(?:frag|mail)="([^"]+)"', body)
        assert urls, f"{path} has no message controls at all"
        for url in urls:
            if "/ui/mail?" in url:
                assert "src=" in url, f"{path} would search the corpus: {url}"


def test_clicking_a_row_on_the_manual_page_looks_in_the_live_mailbox():
    """End to end through the real endpoint, because the thing that broke was the URL and a test
    checking only the URL's shape would have passed while the page stayed broken.

    Asserted on *which store was searched*, not on whether the message was found. Whether Premier
    still holds a given email is their business — they archive and delete — but searching a
    retired folder of `.msg` test files for their live mail is always wrong. The old failure named
    itself in this list: `sample .msg files`.
    """
    client = TestClient(app)
    frags = re.findall(r'data-frag="([^"]+)"', client.get("/ui/manual").text)
    if not frags:
        pytest.skip("no queued mail in this store")

    rendered = client.get(unescape(frags[0])).text
    assert "sample .msg files" not in rendered
    assert "mail-head" in rendered or "Outlook (read-only)" in rendered


def test_the_po_opens_the_email_the_record_was_read_from():
    body = TestClient(app).get("/ui/manual").text
    controls = re.findall(r'data-mail="([^"]+)"', body)
    if not controls:
        pytest.skip("no records with a PO in this store")
    assert all("/ui/mail?id=" in url for url in controls)


def test_a_message_id_is_escaped_into_the_url():
    """Message-ids carry `<`, `>`, `@` and `+`; unescaped they truncate the query string."""
    url = html.mail_url("<CHAP+14@mail.outlook.com>", "why")
    assert "%3C" in url and "%3E" in url and "%2B" in url
    assert url.count("?") == 1


def test_the_mail_branch_is_checked_before_the_row_handler():
    """The row handler bails on any click inside a `button`, so a branch placed after it would
    never run — the reason `[data-open]` and `[data-verify]` sit where they do."""
    script = TestClient(app).get("/ui/manual").text
    assert script.index("[data-mail]") < script.index("tr[data-frag]")


def test_the_po_control_is_a_button_not_a_link():
    """It has to be a `button` to coexist with a clickable row, and because `/ui/mail` returns a
    bare fragment that would otherwise be navigated to as a whole unstyled page."""
    body = TestClient(app).get("/ui/manual").text
    # `href="/ui/mail?` specifically — the sidebar's own link to `/ui/mails` is not this.
    assert 'href="/ui/mail?' not in body
    assert "data-mail=" in body


# --- the page header, the rail's alert card, and the two new table filters -------------------
#
# All four are presentation, and presentation is exactly what nothing else in this file would
# notice breaking. The rules they have to keep are not cosmetic though: one `class="on"` per page,
# the date boxes surviving on every list page, and every filter that can hide a row being counted
# by the readout beside the search box.


def test_the_queue_alert_is_on_every_page_and_never_lights_the_nav():
    """The rail must look the same everywhere.

    Automation and Report built their own header and so passed no `counts` at all — they were the
    two pages with no badge and no card, and a sidebar that changes shape depending on where you
    are reads as a bug in the sidebar.

    The card links to `/ui/manual` and must not carry `class="on"` even on that page; the nav entry
    below it is the single active marker, which `test_the_active_nav_entry_is_marked_on_every_page`
    counts across the whole document.
    """
    client = TestClient(app)
    for path in ALL_UI_PAGES + ("/ui/automation", "/ui/report"):
        body = client.get(path).text
        assert body.count('class="side-alert"') == 1, f"{path}: no queue card in the rail"
        assert 'href="/ui/manual" class="side-alert"' in body, path
        assert body.count('class="on"') == 1, f"{path}: the card lit a second nav entry"


def test_the_collapsed_rail_hides_the_alert_card_text():
    """At 58px the card keeps its glyph and nothing else, or a 232px panel sits in a 58px rail."""
    assert ".rail .side-alert .lbl, .rail .side-alert .count { display:none; }" in html._CSS
    assert ".rail .side-alert {" in html._CSS


def test_page_actions_render_beside_the_title():
    """A control that acts on the whole page belongs next to its name, not part-way down the body.
    `bar-text` wraps the title and subtitle so they stack and the actions can be pushed right."""
    body = TestClient(app).get("/ui/mails").text
    assert '<div class="bar-text">' in body
    assert '<div class="bar-actions">' in body
    assert 'class="btn primary small"' in body, "Check now is no longer the page action"
    assert ".bar-actions { margin-left:auto;" in html._CSS


def test_check_now_left_the_note_but_the_watch_state_did_not():
    """Moving the button up must not take the sentence with it. Whether the arrival watch is on is
    real state, and a page that only says "Check now" cannot explain why it looks stale."""
    body = TestClient(app).get("/ui/mails").text
    assert 'href="/ui/mails?refresh=1"' in body
    assert "the new-mail watch is off" in body.lower() or "last checked" in body.lower()


def test_date_presets_fill_the_range_rather_than_replacing_it():
    """The chips are not a second filter. They write into the same two boxes the range already
    uses, so there is one definition of "the last 7 days" and the two controls cannot disagree.

    The pressed chip is `.sel`. It must never be `class="on"` — that string is counted per page.
    """
    body = TestClient(app).get("/ui/mails").text
    assert 'class="chips"' in body
    for days in ("0", "7", "30"):
        assert f'data-range="{days}"' in body, days
    assert 'class="chip sel" aria-pressed="true" data-range=""' in body, "All starts pressed"
    # The boxes the chips write into are still there — several sweeps require them.
    assert 'class="date-from"' in body and 'class="date-to"' in body
    assert "markChips" in html._JS and "isoDay" in html._JS


def test_an_exact_range_is_the_fifth_segment_of_the_same_control():
    """It shipped as an unlabelled 33px square standing beside the chips, and the first thing the
    reader asked was how you pick a custom range at all. "Today", "7d", "30d", "All" and "Custom"
    are five answers to one question, so they are one control.

    The panel is absolutely positioned inside the group, so the group must not clip it — that is
    what the hand-rounded ends are for.
    """
    body = TestClient(app).get("/ui/mails").text
    chips = body[body.index('class="chips"'):]
    chips = chips[:chips.index("</div>", chips.index("date-custom"))]
    assert "chip chip-cal" in chips, "the exact-range trigger is outside the group again"
    assert ">Custom<" in chips, "the trigger has no visible label"
    group = html._CSS.split(".chips {")[1].split("}")[0]
    assert "overflow:hidden" not in group, "the group would clip its own panel"
    assert ".chips > :first-child" in html._CSS and ".chips > :last-child" in html._CSS


def test_the_custom_segment_lights_when_the_range_came_from_the_boxes():
    """Otherwise the group shows nothing selected while a range is actively hiding two thirds of
    the table — the same silent-hiding problem the count beside the search box exists to prevent.

    It carries `data-range-for` with no `data-range`: it is the segment that means "not one of the
    presets", which is exactly the case `markChips` is handed a null for.
    """
    body = TestClient(app).get("/ui/mails").text
    assert re.search(r'<summary class="chip chip-cal" data-range-for="[^"]+" title=', body), \
        "the segment must be findable by markChips and carry no preset of its own"
    marker = html._JS.split("function markChips")[1].split("\nfunction ")[0]
    assert "own === null ? days === null" in marker
    # `aria-pressed` is a button state; a <summary> must not claim it.
    assert "chip.tagName === 'BUTTON'" in marker


def test_isoday_is_local_not_utc():
    """`toISOString()` is UTC, so east of Greenwich "Today" would hide everything that arrived this
    morning. The helper must build the day from local getters."""
    start = html._JS.index("function isoDay(")
    body = html._JS[start:start + 320]
    assert "getFullYear()" in body and "getMonth()" in body and "getDate()" in body
    assert "toISOString" not in body


def test_the_verdict_dropdown_filters_one_named_column():
    """By heading, never by position — an index quietly points at the wrong column the day someone
    inserts one, and this table has twelve."""
    body = TestClient(app).get("/ui/mails").text
    assert 'class="choice-filter"' in body
    assert 'data-choice-for="mail-table"' in body
    marked = re.findall(r'<th[^>]*data-choice="1"[^>]*>(.*?)</th>', body, re.S)
    assert marked, "no column is marked as the dropdown's column"
    assert "Verdict" in marked[0], marked[0]
    assert "choiceColumnOf" in html._JS


def test_choice_column_must_name_a_real_heading():
    """Same guard `date_column` has. A typo should fail loudly here, not filter nothing at runtime."""
    with pytest.raises(ValueError):
        html.table(["A", "B"], [["1", "2"]], table_id="t", choice_column="Nope")


def test_every_filter_that_hides_rows_is_counted_by_the_readout():
    """A filter that hides rows silently is indistinguishable from an empty table, which is how
    someone concludes their mail was lost. `narrowed()` is the list of things that can hide a row,
    and the dropdown had to join the search box and the date range in it."""
    start = html._JS.index("function narrowed(")
    body = html._JS[start:start + 500]
    assert "date-from" in body and "date-to" in body
    assert "choice-filter" in body, "the dropdown can hide rows without the count admitting it"


def test_narrowing_by_date_or_verdict_also_fixes_the_zebra():
    """`nth-child(even)` counts hidden rows. This watched only the search box, so a table narrowed
    by date alone striped at random — and the dropdown would have been a second way in."""
    start = html._JS.index("scope.classList.toggle('filtering'")
    body = html._JS[start:start + 160]
    assert "dateColumn >= 0" in body and "choiceColumn >= 0" in body


def test_a_section_with_no_title_emits_no_heading():
    """Mail's page title already says "Mail"; an `<h2>` repeating it is furniture, and an empty one
    is worse — a screen reader announces a heading with nothing in it."""
    assert "<h2>" not in str(html.section("", html.tag("p", "body")))
    assert "<h2>Titled</h2>" in str(html.section("Titled", html.tag("p", "body")))
