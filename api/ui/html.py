"""A small HTML layer instead of a template engine.

jinja2 is not installed and is not worth adding for three static pages. The one thing a
hand-rolled renderer must not get wrong is escaping — real corpus subjects contain `&`
("OS&E Cintas - Property Confirmation"), and a mail subject is attacker-influenced data by
definition. So escaping is deliberately not a call site's responsibility:

    `esc()` is the only thing that writes into the output. It escapes anything that is not a
    `Raw`. `Raw` is only ever produced by the constructors in this module, from pieces that are
    already escaped. The only unescaped strings that reach a response are the module-level CSS
    and doctype constants below.

That is what makes the whole surface safe by construction rather than by review. Two rules keep
it that way: never build markup with an f-string outside this file, and never construct `Raw`
from anything a user or a mail server supplied. Nesting `tag()` inside `tag()` cannot
double-escape, because `tag()` returns `Raw` and `esc()` passes `Raw` through untouched.
"""
from html import escape as _escape
from typing import Iterable, Optional, Sequence
from urllib.parse import quote


class Raw(str):
    """Markup already known to be safe. The single unescaped path through this module."""


def esc(value) -> str:
    if value is None:
        return ""
    if isinstance(value, Raw):
        return str(value)
    return _escape(str(value), quote=True)


# Elements that must not be given a closing tag. `<input></input>` is tolerated by browsers but it
# is not valid HTML, and `<br></br>` parses as *two* line breaks — a real bug waiting for whoever
# first passes one of these.
_VOID = frozenset({"input", "br", "hr", "img", "meta", "link", "source", "col", "area", "wbr"})


def tag(name: str, *children, **attrs) -> Raw:
    """`tag("td", subject, class_="wide")`. Trailing underscores are stripped from attribute
    names (`class_` -> `class`) and inner underscores become hyphens (`data_id` -> `data-id`).

    That trailing-underscore rule is also how you write an attribute whose name collides with this
    function's own first parameter: `tag("input", name_="enabled")` renders `name="enabled"`.
    """
    rendered_attrs = "".join(
        f' {key.rstrip("_").replace("_", "-")}="{esc(value)}"'
        for key, value in attrs.items()
        if value is not None
    )
    if name in _VOID:
        return Raw(f"<{name}{rendered_attrs}>")
    body = "".join(esc(child) for child in children)
    return Raw(f"<{name}{rendered_attrs}>{body}</{name}>")


def badge(text: str, kind: str = "") -> Raw:
    return tag("span", text, class_=f"badge badge-{kind or 'plain'}")


def muted(text) -> Raw:
    return tag("span", text, class_="muted")


def token(value) -> Raw:
    """An identifier that must not be broken across lines.

    Browsers break after a hyphen, so `EXT-925-AC` becomes three stacked fragments and
    `2026-08-05T11:58` becomes two, the moment a neighbouring column wants the width. That is
    wrong twice over: it triples the height of every row in a grid whose whole value is being
    scannable, and a spec code split over three lines no longer looks like one code. It got worse
    when the sidebar took 232px off the width, and worse again on a tablet.

    Empty values fall through to `muted("—")`, so a missing one is never an invisible cell.
    """
    if value in (None, ""):
        return muted("—")
    return tag("span", value, class_="nw")


def when(value) -> Raw:
    """A timestamp. `token()` under the skin — named separately because "when" is what the call
    site means, and dates are where the wrapping is most obvious."""
    return token(value)


def table(headers: Sequence[str], rows: Iterable[Sequence], *, empty: str = "Nothing here yet.",
          mail_ids: Optional[Sequence] = None, frag_urls: Optional[Sequence] = None,
          frag_title: str = "Open", table_id: str = "", page_size: int = 0,
          no_sort: Sequence[str] = (), date_column: Optional[str] = None) -> Raw:
    """Rows can be made clickable two ways, and both end up as one thing.

    `mail_ids` — one entry per row, an `email_id` or an `(email_id, reason)` pair — opens that
    message. `frag_urls` names a fragment URL outright, which is how the Records page opens a
    purchase order's progress bar instead of its mail.

    Both render the same `data-frag` attribute, so the script has one fetch path rather than a
    branch per kind of row. `tabindex` is what makes Enter work for anyone not using a mouse; a row
    reachable only by clicking is not reachable.

    `table_id` names the `.scroll` wrapper so a `search_box()` and a `pager()` can point at it. It
    goes on the wrapper rather than the `<table>` because filtering also toggles a class there — the
    zebra stripe is `nth-child(even)`, which counts hidden rows and stripes wrongly the moment
    anything is filtered or paged out.

    `page_size` shows that many rows at a time. It needs a `table_id` to attach the control to, and
    is ignored without one.

    **Every column sorts by clicking its heading**, except those named in `no_sort` — which is for
    columns holding a control rather than a value, like the Verify button on Records, where "sort
    by button" means nothing. The heading is a real `<button>`: a `<th>` with a click handler is
    not reachable by keyboard, and inline handlers are forbidden here anyway.

    `date_column` names the heading whose column holds this table's dates, which is what a
    `date_filter()` narrows on. Named rather than indexed on purpose — an index silently points at
    the wrong column the day someone inserts one, and this table has eighteen.
    """
    body_rows = []
    for index, row in enumerate(rows):
        cells = [tag("td", cell) for cell in row]
        url, title = None, frag_title
        if frag_urls is not None and index < len(frag_urls) and frag_urls[index]:
            url = frag_urls[index]
        elif mail_ids is not None and index < len(mail_ids) and mail_ids[index]:
            target = mail_ids[index]
            email_id, reason = target if isinstance(target, (tuple, list)) else (target, "")
            url = f"/ui/mail?id={quote(str(email_id))}&reason={quote(str(reason or ''))}"
            title = "Open this message"
        if url:
            body_rows.append(tag("tr", *cells, class_="clickable", tabindex="0",
                                 data_frag=url, title=title))
        else:
            body_rows.append(tag("tr", *cells))
    if not body_rows:
        return tag("p", empty, class_="empty")
    if date_column is not None and date_column not in headers:
        raise ValueError(f"date_column {date_column!r} is not one of {list(headers)}")

    cells = []
    for index, header in enumerate(headers):
        if not table_id or header in no_sort:
            cells.append(tag("th", header,
                             data_date="1" if header == date_column else None))
            continue
        cells.append(tag(
            "th",
            tag("button", header, tag("span", "", class_="sort-arrow"),
                type="button", class_="sort-btn"),
            data_sort=str(index), aria_sort="none",
            data_date="1" if header == date_column else None,
        ))
    return scroll_block(
        tag("table", Raw(str(tag("thead", tag("tr", *cells)))
                         + str(tag("tbody", *body_rows)))),
        table_id=table_id, page_size=page_size,
    )


def scroll_block(content: Raw, *, table_id: str = "", page_size: int = 0, unit: str = "row",
                 plain: bool = False) -> Raw:
    """A table in its horizontal scroller, with a pager under it if one was asked for.

    Split out of `table()` because the receiver report sheet is not built by `table()` — it comes
    from `receipt_log.to_html` as a finished string — and it needs the same scroller, id and pager
    without being rebuilt cell by cell.

    `plain` leaves the `.scroll` zebra and header rules off. The sheet carries its own row classes
    (`r-po`, `r-line`, `r-rcpt`) and picking up a stripe on top of them is what the comment above
    the table CSS has always warned about.
    """
    classes = "scroll plain" if plain else "scroll"
    block = tag("div", content, class_=classes, id=table_id or None)
    if not (table_id and page_size > 0):
        return block
    return Raw(str(block) + str(pager(table_id, page_size, unit=unit)))


def date_filter(target, label: str = "date") -> Raw:
    """From / To, narrowing a table to the rows whose date falls between them.

    Which column that is comes from `table(date_column=...)`, not from here — the control should
    not have to know that Received is the second column on Mail and POD date the eleventh on
    Records, and it should not break when someone inserts a column.

    Either end may be left empty: From alone means "since", To alone means "up to". Both are
    inclusive, which is what "1st to 5th" means to everyone who is not a database.

    `type="date"` gives a real picker for free and, more usefully, normalises whatever the browser
    shows the reader into `YYYY-MM-DD` — so the comparison never has to guess at a locale.
    """
    targets = " ".join([target] if isinstance(target, str) else list(target))
    return tag(
        "div",
        tag("label", "From", for_=f"from-{targets.split()[0]}", class_="date-label"),
        tag("input", type="date", class_="date-from", data_from=targets,
            id=f"from-{targets.split()[0]}", aria_label=f"Earliest {label}"),
        tag("label", "to", for_=f"to-{targets.split()[0]}", class_="date-label"),
        tag("input", type="date", class_="date-to", data_to=targets,
            id=f"to-{targets.split()[0]}", aria_label=f"Latest {label}"),
        tag("button", "Clear", type="button", class_="btn ghost small", data_date_clear=targets),
        class_="date-bar",
    )


PAGE_SIZES = (25, 50, 100)


def pager(target: str, page_size: int, *, unit: str = "row") -> Raw:
    """Prev / Next / how-many-per-page for the table `target` names.

    Paging happens in the browser, alongside the filter and for the same reason: neither list view
    has a `LIMIT`, so every row is already here. Keeping both client-side is what lets a search span
    the whole table rather than only the page you happen to be on — which is the trap a server-side
    `?page=` would spring, because it would quietly hide matches on other pages while still
    reporting "no rows match".

    `unit="group"` pages over `data-group` blocks instead of rows. The receiver report is grouped by
    purchase order, and a page boundary falling between a PO's header and its receipt lines would
    split the one thing that sheet exists to show whole.

    Everything is `data-` attributes and real buttons: no inline handlers, which
    `test_pages_carry_exactly_one_first_party_script_and_nothing_else` forbids outright.
    """
    sizes = sorted({page_size, *PAGE_SIZES})
    options = [tag("option", str(size), value=str(size),
                   selected="selected" if size == page_size else None) for size in sizes]
    options.append(tag("option", "All", value="0"))
    return tag(
        "div",
        tag("button", "‹ Prev", type="button", class_="btn ghost small", data_page="prev"),
        tag("span", "", class_="pager-label"),
        tag("button", "Next ›", type="button", class_="btn ghost small", data_page="next"),
        tag("label", "per page", for_=f"ps-{target}", class_="pager-size-label"),
        tag("select", *options, id=f"ps-{target}", class_="pager-size"),
        class_="pager", data_pager_for=target, data_page_size=str(page_size), data_unit=unit,
    )


def search_box(target, placeholder: str = "Search…", label: str = "Search") -> Raw:
    """A filter for the table(s) `target` names. Filtering happens in the browser, not the server.

    `mails_across_sources()` and `records_ready()` carry no `LIMIT`, so every row is already in the
    page. Round-tripping a `?q=` would be slower than typing, and it would let the filter and the
    thing being filtered disagree — a server-side query that paged as well would hide matches on
    other pages while still reporting "no rows match". If these tables ever run to thousands of rows
    that trade changes; it has not yet.

    `target` may be several ids. The manual queue is three tables on one page — emails, attachments,
    records — and someone looking for a PO number wants it found in whichever of the three it landed
    in, not in the one they happened to point at.

    The count beside it is not decoration. A filter that hides rows silently is indistinguishable
    from a table that has nothing in it, which is how someone concludes their mail was lost.
    """
    targets = " ".join([target] if isinstance(target, str) else list(target))
    field = tag("input", type="search", class_="filter", data_filter=targets,
                aria_controls=targets, autocomplete="off", spellcheck="false",
                placeholder=placeholder, aria_label=label)
    return tag("div", field, tag("span", "", class_="filter-count", data_count_for=targets),
               class_="filter-bar")


def stats(pairs: Sequence[tuple]) -> Raw:
    """The header strip: one figure per pair, label underneath."""
    cells = [
        tag("div", tag("b", value), tag("span", label), class_="stat")
        for label, value in pairs
    ]
    return tag("div", *cells, class_="stats")


def stepper(stages) -> Raw:
    """The delivery progress bar — an Amazon-style row of named stages with dates underneath.

    Pure CSS. `/ui` has no JavaScript anywhere and this is not the thing that introduces it: the
    connecting line is a `::before` on each node after the first, so the markup stays a plain list
    and nothing has to measure anything at runtime.

    Takes `read_views.Stage` objects but touches only its attributes, so it stays a renderer and
    never a second place where "which node is lit" gets decided.

    A node the mail has not proved reads **"not yet"** rather than being left blank. Blank is
    ambiguous — it reads as "we have no date for a thing that happened" when the truth is "this has
    not happened". A delivery that has not arrived must never look like one that has.
    """
    nodes = []
    for index, stage in enumerate(stages):
        classes = ["step"]
        if stage.reached:
            classes.append("done")
        if getattr(stage, "terminal", False):
            classes.append(f"term term-{stage.key}")
        if index == 0:
            classes.append("first")
        # Three distinct things, and collapsing any two of them misleads:
        #   reached with a date  — it happened, on this day
        #   reached with none    — it happened (a later stage proves it) but no mail dates it;
        #                          "not yet" here would deny an event that demonstrably occurred
        #   not reached          — it has not happened
        # A stated date is the event itself ("Received Date: 10/09/2025"); anything else is the day
        # a notice about it was sent. Titled rather than annotated inline — the bar has room for a
        # date, not for a sentence about where the date came from.
        if getattr(stage, "conflict", ""):
            classes.append("clash")
        if not stage.reached:
            text, why = "not yet", None
        elif stage.on:
            text = stage.on
            why = ("Stated in the mail as the date this happened"
                   if getattr(stage, "stated", False)
                   else "Date the notice proving this was sent")
        else:
            text = "date not stated"
            why = "Reached — a later notice proves it — but no mail gives a date for this stage"
        if getattr(stage, "conflict", ""):
            # The conflict text replaces the tooltip, not the date: the date is still what the
            # evidence says, and hiding it would remove the thing a reader needs to check.
            why = stage.conflict
        when = tag("span", text, title=why)
        nodes.append(tag(
            "li",
            tag("i", ""),                       # the dot; the connector is its ::before
            tag("b", stage.label),
            when,
            class_=" ".join(classes),
        ))
    return tag("ol", *nodes, class_="stepper")


def section(title: str, *children, note: str = "", action=None) -> Raw:
    """A titled block. `action` is a control that belongs to the section as a whole.

    It sits on the heading row rather than in `children`, because `note` renders before the children
    and a note is often a paragraph — the Records page's runs to five lines. A button placed after
    it is below the fold of its own explanation, which is where the Verify control was and why
    nobody could find it. A control the reader is meant to press comes before the prose about it.
    """
    head = tag("div", tag("h2", title), action, class_="sec-head") if action is not None \
        else tag("h2", title)
    parts = [head]
    if note:
        parts.append(tag("p", note, class_="note"))
    parts.extend(children)
    return tag("section", *parts)


def card(*children, title: str = "", note: str = "", tone: str = "") -> Raw:
    """A `section` with a surface behind it, and a tone that colours its left edge.

    Came across from the operations console, where the status of the last run had to be readable
    without reading: `tone` is "good", "warn" or "bad".
    """
    parts = []
    if title:
        parts.append(tag("h2", title))
    if note:
        parts.append(tag("p", note, class_="note"))
    parts.extend(children)
    return tag("section", *parts, class_=f"card {tone}".strip())


_PROGRESS_LABELS = {
    "starting": "Starting up",
    "reading": "Reading the mailbox",
    "processing": "Sorting the mail",
    "extracting": "Reading the deliveries",
    "done": "Finishing up",
}


def progress_ring(phase: str, done: int, total: int, note: str = "") -> Raw:
    """Where the run has got to: a ring, what it is doing, and the count behind it.

    Two states, and the difference is honest rather than cosmetic. With a `total` the ring is a
    real percentage. Without one — the mailbox fetch is a single call with no interior milestones,
    and it is most of a run's elapsed time — it spins instead, because a determinate bar frozen at
    0%% reads as stalled, which is the opposite of the truth.

    No JavaScript. The percentage is a CSS custom property on a conic-gradient, and the page's
    meta-refresh is what advances it; `/ui` spends exactly one inline script and it is not for a
    progress bar. `test_pages_carry_exactly_one_first_party_script_and_nothing_else` holds that.
    """
    label = _PROGRESS_LABELS.get(phase, "Working")
    if total > 0:
        percent = max(0, min(100, round(done * 100 / total)))
        ring = tag("div", tag("b", f"{percent}%"), class_="ring", style=f"--pct:{percent}")
        detail = f"{done} of {total}"
    else:
        # `aria-hidden` on the ring, not the text: a spinner announces nothing useful, and the
        # phase beside it is the part worth reading aloud.
        ring = tag("div", class_="ring spin", aria_hidden="true")
        detail = "no count yet — this step is one call"
    if note:
        detail = f"{detail} · {note}"
    return tag(
        "div",
        ring,
        tag("div", tag("div", label, class_="prog-what"), tag("div", detail, class_="prog-detail")),
        class_="prog",
    )


def stat_row(items: Sequence[tuple]) -> Raw:
    """`(value, label)` pairs, for use inside a card.

    Deliberately not the `.stats` class the page header uses: that one is a full-width strip with
    its own rules, and reusing it inside a card would inherit the strip's borders.
    """
    cells = [tag("div", tag("b", value), tag("span", label), class_="stat")
             for value, label in items]
    return tag("div", *cells, class_="statrow")


def modal(modal_id: str, title: str, body) -> Raw:
    """A native `<dialog>` shipped with its content already in it.

    Distinct from the fetch-filled message dialog below: this one holds something the page already
    rendered — the receiver report preview — so it needs no round trip and survives a page with
    scripting blocked, because a download link is always offered alongside it.
    """
    head = tag("div",
               tag("h2", title),
               Raw('<form method="dialog">'
                   + str(tag("button", Raw("&times;"), class_="close-x",
                             aria_label="Close", title="Close"))
                   + "</form>"),
               class_="modal-head")
    return tag("dialog", head, tag("div", body, class_="modal-body"),
               id=modal_id, class_="modal")


def button_form(action: str, label: str, *, method: str = "post", cls: str = "btn",
                style: str = "", glyph: str = "") -> Raw:
    """A one-button form. Anything that changes state is a POST, so it cannot be a link — a link
    would let a prefetch or a crawler press it.

    `glyph` gives the button a one-character alternative shown only when the sidebar is collapsed.
    Without it "Stop automation" overflows a 58px rail, and the control an operator reaches for in
    a hurry is the last one that should be hard to read.
    """
    children = [tag("span", label, class_="lbl")]
    if glyph:
        children.append(tag("span", glyph, class_="glyph", aria_hidden="true"))
    return tag("form", tag("button", *children, type="submit", class_=cls, title=label),
               method=method, action=action, class_="inline-form", style=style or None)


def verify_button(url: str, label: str, *, title: str = "", small: bool = False,
                  ghost: bool = False) -> Raw:
    """A button that fetches `url` and shows the answer in the shared dialog.

    Not a `button_form`, because the result is something to *read* rather than a page to land on —
    a form post would navigate away from the table the reader is working through. Not a link
    either: the endpoint calls out to Spitfire and refreshes the local mirror, so it must not be a
    URL a prefetch or a crawler can trip. The script posts it.

    `data-verify` carries the URL and `data-verify-title` the dialog heading; both are escaped by
    `tag()`, and the script only ever reads them back out as attribute values.
    """
    classes = ["btn"]
    if ghost:
        classes.append("ghost")
    if small:
        classes.append("small")
    return tag("button", label, type="button", class_=" ".join(classes),
               title=title or label, data_verify=url, data_verify_title=title or label)


DEFAULT_MAIL_SOURCE = "inbox"
"""The store a page means when it says "open this message" — the live mailbox.

Spelled out rather than left to default, because `routes._store_for` resolves an absent `src` to
the **sample corpus** on purpose: "an unknown, missing or crafted value falls back to the corpus,
so a bad query string can never reach the live store". That guard is right, and it means a caller
that forgets `src` does not get an error — it gets a message that reports itself missing, having
searched a retired folder of test files. Every page listing live mail must therefore say so.
"""


def mail_url(email_id, reason: str = "", src: str = DEFAULT_MAIL_SOURCE) -> str:
    """The `/ui/mail` fragment URL for one message.

    One builder for both ways a message gets opened — a whole clickable row (`table(frag_urls=)`)
    and a control inside a cell (`mail_link`) — so the two cannot disagree about which store they
    are asking, which is exactly how the manual page ended up asking the wrong one.
    """
    return (f"/ui/mail?id={quote(str(email_id))}"
            f"&reason={quote(str(reason or ''))}"
            f"&src={quote(str(src or ''))}")


def mail_link(email_id, label, *, reason: str = "", src: str = DEFAULT_MAIL_SOURCE,
              title: str = "") -> Raw:
    """A value in a table cell that opens the message it came from, in the shared dialog.

    A `button` rather than an `a`, for the reasons `verify_button` gives: the target is something
    to *read* rather than a page to land on, and a real link would navigate away from the table the
    reader is working through — `/ui/mail` returns a bare fragment, so landing on it gives an
    unstyled page with no way back.

    It also has to be a `button` to coexist with a clickable row. The row handler bails out on any
    click inside an `a` or a `button`, and the `[data-mail]` branch in `_JS` is checked before it —
    the same arrangement `[data-open]` and `[data-verify]` already rely on.
    """
    return tag("button", label, type="button", class_="po-link",
               title=title or "Open the email this came from",
               data_mail=mail_url(email_id, reason, src))


def mail_url_for(email_id, src: str = "") -> str:
    """`mail_url` without a reason, for going *back* to a message rather than opening one.

    The reason is the sentence explaining why a row is in a queue; it belongs to the row that was
    clicked, and re-stating it when someone closes an attachment would be quoting a justification
    they have already read and acted on.
    """
    return mail_url(email_id, "", src or DEFAULT_MAIL_SOURCE)


def attachment_viewer(*, filename: str, size: int, label: str, verdict: str, body: Raw,
                      download_url: str, mail_url: str, downloadable: bool = True) -> str:
    """One attachment, full size over the message, with a way back.

    Full-screen rather than unfolded under its row: a scanned POD and a wide tracker are both
    unreadable in a third of a dialog, and those are the two things anyone actually opens. The cost
    is that the message goes away, which is what **‹ Back to message** is for — it carries a
    `data-frag` back to `/ui/mail`, so the existing fragment loader swaps the dialog content and no
    new mechanism is needed.

    `body` is `Raw` because `pipeline/attachment_view.py` did its own escaping — the same contract
    `mail_view.render` has, and the reason both return `str` rather than importing this module.
    """
    head = tag(
        "div",
        tag("button", "‹ Back to message", type="button", class_="btn ghost small",
            data_frag=mail_url),
        tag("b", filename or "attachment"),
        tag("span", f"{_size(size)} · {label}", class_="muted"),
        (tag("span", verdict, class_="muted view-verdict") if verdict else Raw("")),
        (tag("a", "Download", class_="btn ghost small", href=download_url)
         if downloadable else Raw("")),
        class_="view-head",
    )
    return str(tag("div", head, body, class_="view"))


def attachment_viewer_missing(*, mail_url: str, message: str = "") -> str:
    """The viewer for something with no bytes. Still a way back, still says why.

    Attachments genuinely are dropped at ingest — decorative logos, duplicates by content hash —
    so this is a real state and not only an error. What it must never be is a dead end.
    """
    return str(tag(
        "div",
        tag("div",
            tag("button", "‹ Back to message", type="button", class_="btn ghost small",
                data_frag=mail_url),
            class_="view-head"),
        tag("p", message or "This attachment was not retained — see the verdict beside it.",
            class_="empty"),
        class_="view",
    ))


def _size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def banner(text, kind: str = "warn") -> Raw:
    """A short statement that frames everything under it — a failed live read, most of all.

    Its own component because the alternative is a `note` paragraph, and a stale-data warning that
    looks like every other caption is a warning nobody reads.
    """
    return tag("p", text, class_=f"banner banner-{kind}")


def facts(pairs: Sequence[tuple]) -> Raw:
    """A label/value list — "Quantity | 196 YD". Values may be `Raw`.

    Rows whose value is None are dropped rather than rendered empty, so a purchase order that
    carries no order date simply has one fewer row instead of a blank one implying a missing value.
    """
    rows = [tag("div", tag("dt", label), tag("dd", value), class_="fact")
            for label, value in pairs if value is not None]
    return tag("div", *rows, class_="facts")


# The sidebar, grouped. Spitfire's own rail is nine ungrouped items with a chevron on each, which
# is a wall to scan; four short groups say what a page is *for* before you read its name.
#
# `nav_links()` is the flat view. Keep using it rather than reaching into this structure — the test
# that every entry resolves to a real page walks it, and so does the renderer.
_NAV = (
    ("Deliveries", (
        ("/ui/po", "Delivery status"),
        ("/ui/records", "Records"),
    )),
    ("Mail", (
        # One entry. Mails and Inbox were two, and read as the same page because neither said where
        # its mail came from — the source is a column now, not a page.
        ("/ui/mails", "Mail"),
    )),
    ("Needs action", (
        ("/ui/manual", "Needs a human"),
    )),
    ("Operations", (
        ("/ui/automation", "Automation"),
        ("/ui/report", "Receiver report"),
    )),
)


def nav_links():
    """Every `(href, label)` in the sidebar, flat and in display order."""
    return [entry for _group, entries in _NAV for entry in entries]


# 16px line icons, inline so the pages stay one self-contained document with no second request and
# no icon font. They carry `aria-hidden`: the link text is the accessible name, and when the rail is
# collapsed the `title` attribute supplies it.
def _icon(path: str) -> Raw:
    return Raw(
        '<svg class="ico" viewBox="0 0 16 16" width="16" height="16" fill="none" '
        'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{path}</svg>'
    )


_ICONS = {
    "/ui/po": _icon('<path d="M1.5 5.5 8 2l6.5 3.5v5L8 14l-6.5-3.5z"/><path d="M1.5 5.5 8 9l6.5-3.5M8 9v5"/>'),
    "/ui/records": _icon('<path d="M2.5 3.5h11M2.5 8h11M2.5 12.5h11"/>'),
    "/ui/mails": _icon('<rect x="1.5" y="3.5" width="13" height="9" rx="1.5"/><path d="m2 4.5 6 4 6-4"/>'),
    "/ui/manual": _icon('<circle cx="8" cy="8" r="6.2"/><path d="M8 5v3.5M8 10.8v.2"/>'),
    "/ui/automation": _icon('<circle cx="8" cy="8" r="6.2"/><path d="m6.5 5.5 4 2.5-4 2.5z"/>'),
    "/ui/report": _icon('<path d="M3.5 1.5h6l3 3v10h-9z"/><path d="M9.5 1.5v3h3M5.5 8h5M5.5 11h3"/>'),
}

# Must not contain the sequence "</style>" — that is the one injection hole a constant CSS
# block can have, and it is why the CSS lives here as a constant rather than being composed.
_CSS = """
/* Spitfire is the reference, not the ceiling. What is borrowed: the dark rail on the left, the
   warm palette, the dense grid. What is deliberately not: Spitfire puts its gold on the sidebar
   AND the table headers AND the page ground, so every surface competes and its own status colours
   have to fight the furniture. Here gold is chrome only — the rail, the active item, the focus
   ring, the rule under a table head. Content surfaces stay quiet, which is what lets the status
   badges further down mean something. */
:root {
  --nav:#4A3E1A; --nav-2:#3A3014; --nav-fg:#E8E1CD; --nav-muted:#A99C79; --nav-on:#F1D98A;
  --gold:#D9A521; --gold-deep:#9A5B00;
  --bg:#FBFAF7; --surface:#fff; --head:#F6F2E8; --zebra:#FCFAF5;
  --fg:#1c1c1a; --line:#E6E2D8; --muted:#77746c; --accent:#2f5d50;
  --danger:#9a3b2c;
  --rail:232px;
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif; }

/* ---- the shell ---------------------------------------------------------- */
.shell { display:flex; align-items:flex-start; min-height:100vh; }
/* min-width:0 is load-bearing: without it a 16-column table forces the flex item wider than the
   viewport and the whole page scrolls sideways instead of the table scrolling inside itself. */
.content { flex:1 1 auto; min-width:0; }

.side { flex:0 0 var(--rail); width:var(--rail); align-self:stretch; position:sticky; top:0;
    height:100vh; background:var(--nav); color:var(--nav-fg); display:flex; flex-direction:column; }
.side .brand { display:flex; align-items:center; gap:8px; padding:16px 14px 14px; }
.side .brand b { font-size:13px; font-weight:700; letter-spacing:.14em; text-transform:uppercase;
    color:#fff; line-height:1.1; }
.side .brand span { display:block; font-size:10.5px; letter-spacing:.06em; color:var(--nav-muted);
    font-weight:500; text-transform:none; }
.rail-toggle { margin-left:auto; border:none; background:transparent; color:var(--nav-muted);
    cursor:pointer; font-size:17px; line-height:1; padding:4px 7px; border-radius:6px; }
.rail-toggle:hover { background:var(--nav-2); color:#fff; }

.nav { flex:1 1 auto; overflow-y:auto; padding:4px 0 14px; }
.nav-label { font-size:9.5px; letter-spacing:.11em; text-transform:uppercase; color:var(--nav-muted);
    padding:14px 16px 5px; font-weight:700; }
.nav a { display:flex; align-items:center; gap:10px; padding:8px 16px; color:var(--nav-fg);
    text-decoration:none; font-size:13px; border-left:3px solid transparent; }
.nav a:hover { background:var(--nav-2); color:#fff; }
.nav a.on { background:var(--nav-2); color:var(--nav-on); border-left-color:var(--gold);
    font-weight:600; }
.nav a .ico { flex:0 0 16px; opacity:.85; }
.nav a.on .ico { opacity:1; }
.nav a .lbl { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.nav a .count { margin-left:auto; background:var(--gold); color:#3A3014; font-size:10.5px;
    font-weight:700; border-radius:20px; padding:0 6px; min-width:18px; text-align:center; }
.side-foot { border-top:1px solid rgba(255,255,255,.11); padding:10px 12px; }
.side-foot .btn { width:100%; justify-content:center; }
.side-foot .kill-note { display:block; font-size:11px; color:var(--nav-muted); margin-top:7px;
    text-align:center; }
.side-foot .powered { display:block; font-size:9.5px; letter-spacing:.08em; text-transform:uppercase;
    color:var(--nav-muted); margin-top:10px; text-align:center; opacity:.75; }

/* Collapsed: the rail keeps the icons and drops everything that needs width. `title` on each link
   is what carries the label at this size — see `page()`.

   The class sits on <html>, not <body>, so the script in <head> can set it before the sidebar is
   parsed and a collapsed rail never flashes open. `--rail` is inherited from here either way. */
.rail { --rail:58px; }
.rail .side .brand b, .rail .nav-label, .rail .nav a .lbl, .rail .side-foot .lbl,
.rail .nav a .count, .rail .side-foot .kill-note, .rail .side-foot .powered { display:none; }
/* The glyph is the mirror of `.lbl`: hidden at full width, shown in its place on the rail. */
.glyph { display:none; }
.rail .glyph { display:inline; }
.rail .side .brand { padding:16px 0 14px; justify-content:center; }
.rail .rail-toggle { margin:0; transform:rotate(180deg); }
.rail .nav a { justify-content:center; padding:10px 0; }
.rail .nav-group + .nav-group { border-top:1px solid rgba(255,255,255,.09); margin-top:6px;
    padding-top:6px; }
.rail .side-foot .btn { padding:6px 0; font-size:11px; }

/* ---- page chrome -------------------------------------------------------- */
header { border-bottom:1px solid var(--line); background:var(--surface); }
.bar { display:flex; align-items:center; gap:18px; padding:15px 26px 14px; }
.bar h1 { font-size:17px; margin:0; font-weight:660; letter-spacing:-.015em; }
.bar .sub { margin:2px 0 0; font-size:12.5px; color:var(--muted); }
form.run { margin-left:auto; }
button, .btn { font:inherit; display:inline-flex; align-items:center; gap:7px; padding:6px 13px;
    border:1px solid var(--line); border-radius:7px; background:var(--surface); color:var(--fg);
    cursor:pointer; text-decoration:none; }
button:hover, .btn:hover { border-color:var(--gold-deep); color:var(--gold-deep); }
.btn.ghost { background:transparent; }
.btn.small { padding:4px 10px; font-size:12px; }
.btn.danger { border-color:#d8b6ae; color:var(--danger); background:#fdf4f1; }
.btn.danger:hover { background:var(--danger); border-color:var(--danger); color:#fff; }
.side-foot .btn { background:transparent; border-color:rgba(255,255,255,.22); color:var(--nav-fg); }
.side-foot .btn:hover { background:#6d2a1e; border-color:#8d3b2b; color:#fff; }
.inline-form { margin:0; }
/* Floating rather than inline: it appears while someone is mid-page, and a control that pushes
   the table down as it arrives moves the row they were reading. `[hidden]` needs the explicit
   `display:none` because the rule above sets `display:inline-flex` on every button. */
/* A value that happens to open something, not a button that happens to sit in a table. It has to
   be a `button` element to survive the row-click handler (see `mail_link`), so the button styling
   above is undone rather than never applied. */
.po-link { display:inline; padding:0; border:0; background:none; color:var(--accent);
    font:inherit; text-decoration:underline; text-underline-offset:2px; cursor:pointer; }
.po-link:hover { color:var(--gold-deep); border:0; background:none; }
.live-pill { position:fixed; left:50%; transform:translateX(-50%); bottom:22px; z-index:40;
    background:var(--accent); border-color:var(--accent); color:#fff; font-size:13px;
    padding:8px 16px; border-radius:999px; box-shadow:0 6px 20px rgba(0,0,0,.18); }
.live-pill:hover { background:#264c41; border-color:#264c41; color:#fff; }
.live-pill[hidden] { display:none; }
:focus-visible { outline:2px solid var(--gold); outline-offset:2px; }

.stats { display:flex; flex-wrap:wrap; gap:26px; padding:11px 26px; border-top:1px solid var(--line);
    background:var(--surface); }
.stat b { display:block; font-size:18px; font-weight:660; }
.stat span { color:var(--muted); font-size:12px; }
.statrow { display:flex; flex-wrap:wrap; gap:30px; margin:12px 0 0; }
.stopped-banner { background:#fdf0ec; border-bottom:1px solid #eec2b7; color:#7d2b1c;
    padding:11px 26px; font-size:13px; }
main { padding:22px 26px 60px; }
section { margin-bottom:32px; }
.card { background:var(--surface); border:1px solid var(--line); border-radius:11px;
    padding:16px 18px 18px; border-left:3px solid var(--line); }
.card.good { border-left-color:#1d6b4f; }
.card.warn { border-left-color:var(--gold); }
.card.bad  { border-left-color:var(--danger); }
h2 { font-size:14px; margin:0 0 4px; font-weight:660; }
.note, .empty { color:var(--muted); margin:4px 0 12px; }
.empty { padding:16px; background:var(--surface); border:1px dashed var(--line); border-radius:9px; }
.row { display:flex; flex-wrap:wrap; align-items:flex-end; gap:16px; }
.check { display:flex; align-items:center; gap:7px; }
label { display:block; font-size:11.5px; color:var(--muted); margin-bottom:3px; }
input[type=number], select { font:inherit; padding:6px 9px; border:1px solid var(--line);
    border-radius:7px; background:var(--surface); color:var(--fg); }

/* ---- tables ------------------------------------------------------------- */
/* Scoped to `.scroll`, which only `table()` emits. The receipt-log sheet below has its own row
   classes and must not pick up the zebra or the header rule. */
.scroll { overflow-x:auto; border:1px solid var(--line); border-radius:9px; background:var(--surface); }
table { border-collapse:collapse; width:100%; font-size:13px; }
th { text-align:left; font-weight:660; color:var(--fg); font-size:11px; text-transform:uppercase;
     letter-spacing:.045em; padding:10px 12px; border-bottom:1px solid var(--line); white-space:nowrap; }
.scroll th { background:var(--head); border-bottom:2px solid var(--gold); }
td { padding:9px 12px; border-bottom:1px solid #F2EFE7; vertical-align:top; }
tr:last-child td { border-bottom:0; }
.scroll tbody tr:nth-child(even) { background:var(--zebra); }
.scroll tbody tr:hover { background:#F6F1E4; }
td.num, .scroll td.num { text-align:right; font-variant-numeric:tabular-nums; }
.nw { white-space:nowrap; }
.muted { color:var(--muted); }

/* ---- table filter ------------------------------------------------------- */
/* Gold stays chrome: the focus ring is the only gold here, matching the sidebar and the rule
   under a table head. The input is not styled as a search "pill" — it is a field above a grid. */
.filter-bar { display:flex; align-items:center; gap:12px; margin:0 0 10px; }
input.filter { font:inherit; font-size:13px; padding:7px 11px; width:min(340px, 100%);
    border:1px solid var(--line); border-radius:7px; background:var(--surface); color:var(--fg); }
input.filter:focus { outline:2px solid var(--gold); outline-offset:1px; border-color:var(--gold); }
input.filter::-webkit-search-cancel-button { cursor:pointer; }
.filter-count { color:var(--muted); font-size:12px; font-variant-numeric:tabular-nums; }
tr.filtered-out, tr.paged-out { display:none; }
/* Two classes, not one. A row can be off-screen because it did not match the search or because it
   is on another page, and collapsing them would make each pass clobber the other's decision. */

/* ---- sortable headings --------------------------------------------------- */
/* A real button inside the th, not a click handler on the th: a cell is not focusable, so a
   header nobody can reach by keyboard is a column nobody can sort without a mouse. */
th:has(.sort-btn) { padding:0; }
.sort-btn { font:inherit; font-size:11px; font-weight:660; text-transform:uppercase;
    letter-spacing:.045em; color:var(--fg); background:transparent; border:0; cursor:pointer;
    padding:10px 12px; width:100%; text-align:left; display:flex; align-items:center; gap:5px;
    white-space:nowrap; }
.sort-btn:hover { background:#F4EDDC; }
.sort-btn:focus-visible { outline:2px solid var(--gold); outline-offset:-2px; }
/* Reserved whether or not this column is the sorted one, so the heading row does not shift
   sideways as you click between columns. */
.sort-arrow { width:8px; display:inline-block; color:var(--muted); }
.sort-arrow::before { content:"\\2195"; opacity:.28; }
th[aria-sort="ascending"]  .sort-arrow::before { content:"\\2191"; opacity:1; color:var(--gold); }
th[aria-sort="descending"] .sort-arrow::before { content:"\\2193"; opacity:1; color:var(--gold); }
th[aria-sort="ascending"], th[aria-sort="descending"] { background:#F4EDDC; }

/* ---- date range --------------------------------------------------------- */
.date-bar { display:flex; align-items:center; gap:8px; margin:0 0 10px; font-size:12.5px;
    color:var(--muted); flex-wrap:wrap; }
.date-label { display:inline; margin:0; font-size:12.5px; }
input.date-from, input.date-to { font:inherit; font-size:12.5px; padding:5px 8px;
    border:1px solid var(--line); border-radius:7px; background:var(--surface); color:var(--fg); }
input.date-from:focus, input.date-to:focus { outline:2px solid var(--gold); outline-offset:1px; }

/* ---- pager -------------------------------------------------------------- */
.pager { display:flex; align-items:center; gap:10px; margin:10px 0 0; font-size:12.5px;
    color:var(--muted); }
.pager-label { font-variant-numeric:tabular-nums; min-width:9em; text-align:center; }
.pager-size-label { display:inline; margin:0 0 0 auto; }
.pager-size { font:inherit; font-size:12.5px; padding:4px 7px; border:1px solid var(--line);
    border-radius:6px; background:var(--surface); color:var(--fg); }
.pager button[disabled] { opacity:.4; cursor:default; }
.pager button[disabled]:hover { background:transparent; color:inherit; border-color:var(--line); }
/* The sheet brings its own row styling (`r-po`, `r-line`, `r-rcpt`); it needs the scroller and
   nothing else from `.scroll`. */
.scroll.plain { border:0; background:transparent; border-radius:0; }
.scroll.plain tbody tr:nth-child(even), .scroll.plain tbody tr:hover { background:transparent; }
/* `nth-child(even)` counts hidden rows, so a filtered table stripes at random. While a filter is
   active the JS assigns `.alt` to every second *visible* row and this turns the CSS rule off. */
.scroll.filtering tbody tr:nth-child(even) { background:transparent; }
.scroll.filtering tbody tr.alt { background:var(--zebra); }
.scroll.filtering tbody tr:hover { background:#F6F1E4; }
.scroll.filtering tbody tr.clickable:hover { background:#F4EDDC; }

@media (max-width: 900px) {
  .shell { display:block; }
  .side { position:static; width:auto; height:auto; flex-basis:auto; }
  .nav { display:flex; flex-wrap:wrap; overflow:visible; padding:0 8px 10px; }
  .nav-group { display:contents; }
  .nav-label { display:none; }
  .nav a { border-left:0; border-bottom:3px solid transparent; border-radius:7px; }
  .nav a.on { border-left:0; border-bottom-color:var(--gold); }
  .rail-toggle { display:none; }
  main, .bar, .stats, .stopped-banner { padding-left:16px; padding-right:16px; }
}
/* `nowrap`: a status is a label, not prose. Without it "Loss or claim" breaks across two lines the
   moment a neighbouring column wants the width, and a two-line pill reads as two states. */
.badge { display:inline-block; padding:1px 8px; border-radius:20px; font-size:11px; font-weight:600;
    border:1px solid; white-space:nowrap; }
/* A failure message is a sentence, so it gets colour rather than a pill — a paragraph of stack
   trace squeezed into a rounded badge is unreadable at any width. */
.err { color:var(--danger); }
.badge-surface { color:#1d6b4f; border-color:#b6ddc9; background:#eef8f2; }
.badge-hold    { color:#8a6216; border-color:#e6d2a0; background:#fdf6e6; }
.badge-route   { color:#9a3b2c; border-color:#eec2b7; background:#fdf0ec; }
.badge-hide    { color:#6a6a6a; border-color:#dedede; background:#f5f5f5; }
.badge-error   { color:#fff;    border-color:#9a3b2c; background:#9a3b2c; }
.badge-email, .badge-attachment, .badge-record, .badge-plain {
    color:var(--muted); border-color:var(--line); background:#f7f7f5; }
/* Where a row's mail came from. `inbox` is Premier's real receiving mailbox and reads solid;
   `sample` is the .msg test corpus and is deliberately quieter — it is scaffolding, and it goes
   away once the live inbox carries real traffic. Neither borrows a triage or delivery colour:
   "this is test data" is a different kind of fact from "this was delivered". */
.badge-inbox  { color:#1f5f86; border-color:#b9d5e8; background:#eef5fa; }
.badge-sample { color:#7a746a; border-color:#ded9cf; background:#f6f3ec; font-style:italic; }
/* The six delivery statuses. Deliberately a separate ramp from the triage verdicts above: a PO
   being "delivered" and an email being "surface" are unrelated facts, and sharing a colour would
   invite reading one as the other. Warehouse sits between transit and delivered, so it gets its
   own shade rather than borrowing either. */
.badge-open              { color:#6a6a6a; border-color:#dedede; background:#f5f5f5; }
.badge-in_transit        { color:#8a6216; border-color:#e6d2a0; background:#fdf6e6; }
.badge-at_warehouse      { color:#7a4fa3; border-color:#d7c4e8; background:#f7f1fc; }
.badge-delivered         { color:#1d6b4f; border-color:#b6ddc9; background:#eef8f2; }
.badge-pod_submitted     { color:#1f5f86; border-color:#b9d5e8; background:#eef5fa; }
.badge-pushed_to_spitfire{ color:#fff;    border-color:#2f5d50; background:#2f5d50; }
/* Terminal outcomes. Loss/claim is amber rather than red on purpose: it is not a cancellation,
   it is an open piece of work someone has to chase. */
.badge-cancelled         { color:#fff;    border-color:#9a3b2c; background:#9a3b2c; }
.badge-loss_or_claim     { color:#8a3d16; border-color:#e8c3a5; background:#fdf1e8; }

/* The delivery progress bar. No JavaScript: the connector is a ::before on every node but the
   first, so the line is drawn by the nodes themselves and nothing measures anything. */
/* The headline above the bar: where the goods are now. Reads as one line — badge, since-when, then
   the place and signer — because that is one answer, not three facts. */
.where { display:flex; align-items:baseline; flex-wrap:wrap; gap:9px; margin:2px 0 4px;
    font-size:14.5px; }
.where b { font-weight:660; }
.where .muted { font-size:13px; }
/* A date that cannot be right. Amber, not red: it is a question for a person, not a failure. */
.where .conflict { font-size:12.5px; color:#8a5a00; background:#fdf6e6; border:1px solid #e6d2a0;
    border-radius:7px; padding:2px 9px; }
.stepper .step.clash span { color:#8a5a00; font-weight:600; }
.stepper .step.clash i { border-color:#b5701f; }
.stepper { display:flex; list-style:none; margin:14px 0 6px; padding:0; overflow-x:auto; }
.stepper .step { position:relative; flex:1 1 0; min-width:110px; text-align:center; padding:0 4px; }
.stepper .step::before { content:""; position:absolute; top:7px; right:50%; left:-50%;
    height:2px; background:var(--line); }
.stepper .step.first::before { display:none; }
.stepper .step.done::before { background:var(--accent); }
/* An unreached node is hollow with a dashed connector: visibly not-yet at a glance, without
   needing the label read. `.na` used to do this for the two Spitfire stages that could never be
   reached; those are off the bar entirely now, so the treatment belongs on any unreached node. */
.stepper .step:not(.done)::before { background:transparent;
    border-top:2px dashed var(--line); height:0; }
.stepper .step i { position:relative; z-index:1; display:block; width:14px; height:14px;
    margin:1px auto 7px; border-radius:50%; background:#fff; border:2px solid var(--line); }
.stepper .step.done i { background:var(--accent); border-color:var(--accent); }
.stepper .step:not(.done) i { border-style:dashed; }
.stepper .step.term i { background:#9a3b2c; border-color:#9a3b2c; }
.stepper .step.term-loss_or_claim i { background:#b5701f; border-color:#b5701f; }
.stepper .step b { display:block; font-size:11.5px; font-weight:600; color:var(--muted); }
.stepper .step.done b { color:var(--fg); }
.stepper .step span { display:block; font-size:10.5px; color:var(--muted); margin-top:2px; }
.stepper .step:not(.done) span { font-style:italic; opacity:.75; }
footer { color:var(--muted); padding:0 26px 40px; font-size:12px; }

tr.clickable { cursor:pointer; }
.scroll tbody tr.clickable:hover { background:#F4EDDC; }
tr.clickable:focus { outline:2px solid var(--gold-deep); outline-offset:-2px; }

dialog.modal { border:none; border-radius:12px; padding:0; width:min(1200px,96vw); max-height:94vh;
    box-shadow:0 24px 60px rgba(20,30,25,.26); color:var(--fg); }
dialog.modal::backdrop { background:rgba(20,30,25,.45); }
.modal-head { display:flex; justify-content:space-between; align-items:center; gap:14px;
    padding:8px 10px 8px 16px; border-bottom:1px solid var(--line); position:sticky; top:0;
    background:#fff; z-index:2; }
.modal-head h2 { margin:0; font-size:11px; font-weight:600; color:var(--muted);
    letter-spacing:.05em; text-transform:uppercase; }
.close-x { border:none; background:transparent; color:var(--muted); font-size:22px; line-height:1;
    cursor:pointer; padding:4px 10px; border-radius:7px; }
.close-x:hover { background:#f1f0ec; color:var(--fg); }
.modal-body { padding:14px 18px 22px; overflow:auto; max-height:calc(94vh - 42px); font-size:13px; }

.mail-head { border-bottom:1px solid var(--line); padding-bottom:9px; margin-bottom:11px; }
.mail-head h3 { margin:0 0 2px; font-size:15px; line-height:1.35; }
.mail-head .note { margin:0; font-size:12px; }
.mail-head .why { margin:8px 0 0; background:#fdf6e6; color:#7a5000; border-radius:7px;
    padding:7px 10px; font-size:12.5px; }
/* The message renders in its own frame at the sender's own sizing; the chrome above must not leak
   into it. The frame is sandboxed without allow-scripts — see pipeline/mail_view.py. */
iframe.mail-body { width:100%; height:min(64vh,760px); border:1px solid var(--line);
    border-radius:8px; background:#fff; }
.remote-note { display:flex; align-items:center; gap:12px; flex-wrap:wrap; background:#f7f7f5;
    border:1px solid var(--line); border-radius:8px; padding:9px 12px; margin:0 0 10px;
    font-size:12.5px; color:var(--muted); }
.att-title { margin:20px 0 9px; font-size:11px; text-transform:uppercase; letter-spacing:.05em;
    color:var(--muted); }
.att { border:1px solid var(--line); border-radius:9px; padding:10px 12px; margin-bottom:10px;
    background:#fbfbfa; }
.att-head { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.att-head b { font-size:13px; }
.att-head a { margin-left:auto; }
/* With an Open button present the Download link is no longer the last child, so `margin-left:auto`
   on the anchor alone would push only one of them. The pair floats right together. */
.att-head [data-frag].btn { margin-left:auto; }
.att-head [data-frag].btn + a { margin-left:0; }
.att-verdict { margin:7px 0 0; font-size:12.5px; }
.att-img { max-width:100%; max-height:460px; margin-top:11px; border-radius:8px;
    border:1px solid var(--line); display:block; }
.att-text { margin-top:11px; white-space:pre-wrap;
    font:12.5px/1.5 ui-monospace,Menlo,Consolas,monospace; background:#fff;
    border:1px solid var(--line); border-radius:8px; padding:12px; max-height:420px; overflow:auto; }
/* A filename that opens the file. Looks like text, behaves like a button — a real `<button>`
   because the target is a fragment to read, not a page to land on. */
.link-btn { font:inherit; font-size:13px; font-weight:660; color:var(--fg); background:none;
    border:0; padding:0; cursor:pointer; text-align:left; text-decoration:underline;
    text-decoration-color:var(--line); text-underline-offset:3px; }
.link-btn:hover { text-decoration-color:var(--gold); }
.link-btn:focus-visible { outline:2px solid var(--gold); outline-offset:2px; border-radius:3px; }
.thumb-btn { display:block; background:none; border:0; padding:0; cursor:pointer; width:100%; }
.thumb-btn:focus-visible { outline:2px solid var(--gold); outline-offset:2px; }

/* ---- the attachment viewer ---------------------------------------------- */
/* Full size over the message: a scanned POD and a wide tracker are both unreadable in a third of
   a dialog, and those are the two things anyone actually opens. */
.view-head { display:flex; align-items:center; gap:12px; flex-wrap:wrap; padding-bottom:11px;
    margin-bottom:14px; border-bottom:1px solid var(--line); }
.view-head b { font-size:13.5px; }
.view-head a { margin-left:auto; }
.view-verdict { font-size:12.5px; }
.view-img { max-width:100%; border-radius:8px; border:1px solid var(--line); display:block;
    margin:0 auto; }
.view-pdf, .view-frame { width:100%; height:min(72vh,820px); border:1px solid var(--line);
    border-radius:8px; background:#fff; }
.view-hdr { border:1px solid var(--line); border-radius:8px; background:#fbfbfa; padding:10px 12px;
    margin-bottom:12px; font-size:12.5px; }
.view-hdr-row { display:flex; gap:10px; padding:2px 0; }
.view-hdr-row span:first-child { min-width:5.5em; }
.view-children { list-style:none; margin:0; padding:0; }
.view-child { display:flex; align-items:center; gap:12px; padding:8px 2px;
    border-bottom:1px solid #F2EFE7; font-size:13px; }
.view-child:last-child { border-bottom:0; }
.view-child .muted { margin-left:auto; font-size:12.5px; }
/* `.btn` and `.btn.small` are defined once, up with the page chrome. They used to be redeclared
   here as well, which meant a restyle of the button had to be made in two places or it silently
   half-applied. */

.sheet { border-collapse:collapse; font-size:12.5px; width:100%; }
.sheet td { border:1px solid var(--line); padding:5px 9px; vertical-align:top; }
.sheet .r-title td { border:none; font-weight:700; font-size:14px; padding-bottom:2px; }
.sheet .r-sub td { border:none; color:var(--muted); padding-top:0; }
.sheet .r-head td { background:#f1f0ec; font-weight:700; font-size:11px; text-transform:uppercase;
    letter-spacing:.03em; color:var(--muted); }
.sheet .r-project td { background:#eceee9; font-weight:700; }
.sheet .r-po td { font-weight:700; background:#fafaf8; }
.sheet .r-subhead td { font-weight:700; color:var(--muted); font-size:11.5px; }
/* The receipts under a line are a sub-table: Spitfire reuses the Order Qty and Received columns
   for a receipt's date and quantity, and says so with the Receipt#/On sub-header alone. The tint
   and the rail on `.ref` are what carry that on screen — without them a receipt date sits under a
   column headed Order Qty and reads as a misalignment rather than as a nested block. */
.sheet .r-subhead td, .sheet .r-rcpt td { background:#faf9f6; }
.sheet .r-subhead .ref, .sheet .r-rcpt .ref { border-left:3px solid #d8d5cc; padding-left:17px; }
.sheet .n { text-align:right; font-variant-numeric:tabular-nums; }
.sheet .c { text-align:center; }
/* A date is not a quantity. Left-aligned so it never lines up with the numeric column edge. */
.sheet .d { text-align:left; font-variant-numeric:tabular-nums; white-space:nowrap; }
.badge-good { color:#1d6b4f; border-color:#b6ddc9; background:#eef8f2; }

/* A section's own control, on the heading row. `section()` renders `note` before its children, and
   the Records note runs to five lines — a button after it sits below the fold of its own
   explanation. Wraps on a narrow screen rather than squeezing the heading. */
.sec-head { display:flex; align-items:baseline; justify-content:space-between; gap:14px;
    flex-wrap:wrap; }
.sec-head h2 { margin-bottom:6px; }

/* Verify against Spitfire. The two sides of the comparison are set apart deliberately: what the
   email said and what the purchase order holds are different claims, and a single merged column
   would let a reader take one for the other. */
.banner { border-radius:8px; padding:9px 12px; margin:0 0 12px; font-size:12.5px; }
.banner-warn { background:#fdf6e6; color:#7a5000; border:1px solid #e6d2a0; }
.banner-bad  { background:#fdf0ec; color:#8a2e1e; border:1px solid #eec2b7; }
.banner-good { background:#eef8f2; color:#1d6b4f; border:1px solid #b6ddc9; }
.facts { display:grid; grid-template-columns:1fr; gap:0; margin:0 0 14px;
    border:1px solid var(--line); border-radius:9px; overflow:hidden; }
.fact { display:flex; gap:12px; padding:7px 12px; border-top:1px solid var(--line); }
.fact:first-child { border-top:none; }
.fact dt { flex:0 0 168px; color:var(--muted); font-size:12px; margin:0; }
.fact dd { margin:0; font-size:13px; }
.vq { border-collapse:collapse; width:100%; margin:0 0 14px; font-size:13px; }
.vq th, .vq td { border:1px solid var(--line); padding:6px 10px; text-align:left; }
.vq thead th { background:#f1f0ec; font-size:11px; text-transform:uppercase; letter-spacing:.03em;
    color:var(--muted); }
.vq tbody th { background:#fafaf8; font-weight:600; width:150px; }
.vq .n { text-align:right; font-variant-numeric:tabular-nums; }
.vq .same { background:#eef8f2; }
.vq .diff { background:#fdf0ec; }
.vfindings { margin:0 0 6px; padding-left:18px; font-size:13px; line-height:1.55; }
.vfindings li { margin:3px 0; }
.vrec { border-top:1px solid var(--line); padding-top:14px; margin-top:18px; }
.vrec:first-child { border-top:none; padding-top:0; margin-top:0; }
.vrec h3 { margin:0 0 3px; font-size:14.5px; }

/* Run progress. All CSS: these pages spend exactly one inline script and it is not for this.
   The ring is a conic-gradient sweep over a masked disc — one element, no SVG, no canvas. */
.prog { display:flex; align-items:center; gap:16px; margin:2px 0 14px; }
.ring { --pct:0; position:relative; width:64px; height:64px; flex:none; border-radius:50%;
    background:conic-gradient(var(--gold) calc(var(--pct) * 1%), #e6e4de 0); }
.ring::after { content:""; position:absolute; inset:7px; border-radius:50%; background:var(--surface); }
.ring b { position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
    font-size:13px; font-weight:700; font-variant-numeric:tabular-nums; z-index:1; }
/* The indeterminate case is a real state, not a fallback: while the mailbox is being read there
   is no denominator to show, and a bar sitting at 0%% would read as stalled. */
.ring.spin { background:conic-gradient(var(--gold) 0 25%, #e6e4de 0); animation:ring-spin 1.1s linear infinite; }
@keyframes ring-spin { to { transform:rotate(360deg); } }
.prog-what { font-weight:600; }
.prog-detail { color:var(--muted); font-size:12.5px; margin-top:2px; }
@media (prefers-reduced-motion:reduce) { .ring.spin { animation:none; } }
"""

# One dialog per page, shipped empty and filled by fetch when a row is clicked. Server-rendering
# every message body into every page instead would put megabytes of quoted mail and inline images
# on a table nobody has clicked yet.
#
# The heading is addressable (`#mailbox-title`) because the same dialog now serves two kinds of
# answer: a message, and a purchase order read back from Spitfire. A popup headed "Message" while
# showing PO quantities mislabels what the reader is looking at.
_MAIL_DIALOG = (
    '<dialog id="mailbox-dialog" class="modal">'
    '<div class="modal-head"><h2 id="mailbox-title">Message</h2>'
    '<form method="dialog"><button class="close-x" aria-label="Close message" '
    'title="Close">&times;</button></form></div>'
    '<div class="modal-body" id="mailbox-body"></div></dialog>'
)

# The only JavaScript in these pages. No libraries, no build step, and nothing here interpolates
# anything a mail server sent us — every value it handles comes from a data- attribute the server
# already escaped. The message body itself renders in a sandboxed iframe that cannot run scripts.
_JS = """
function openDialog(id){var d=document.getElementById(id); if(d&&d.showModal) d.showModal();}

document.addEventListener('click', function (e) {
  // A button that opens a dialog already on the page — the receiver-report preview. This is
  // checked FIRST because the row handler below bails out on any click inside a `button`, so a
  // later branch would never be reached. `modal()` renders the dialog; nothing is fetched.
  var opener = e.target.closest('[data-open]');
  if (opener) {
    e.preventDefault();
    openDialog(opener.getAttribute('data-open'));
    return;
  }
  if (e.target.closest('[data-load-images]')) {
    e.preventDefault();
    if (window.__lastMailRow) showFragment(window.__lastMailRow, true);
    return;
  }
  // Verify against Spitfire. Checked before the row handler for the same reason as [data-open]:
  // these buttons sit inside table rows, and the row handler bails on any click within a button,
  // so a later branch would never run. POSTed, because the endpoint reads Spitfire and rewrites
  // the local mirror — see verify_button().
  var check = e.target.closest('[data-verify]');
  if (check) {
    e.preventDefault();
    loadFragment(check.getAttribute('data-verify'), {
      method: 'POST',
      title: check.getAttribute('data-verify-title') || 'Spitfire',
      waiting: 'Reading the purchase order from Spitfire\\u2026 this takes a few seconds per PO.'
    });
    return;
  }
  // A value inside a row that opens the message it came from — a PO number, a subject. Checked
  // before the row handler for the same reason as the branches above: that handler bails on any
  // click inside a `button`, so a later branch would never run.
  var mail = e.target.closest('[data-mail]');
  if (mail) {
    e.preventDefault();
    loadFragment(mail.getAttribute('data-mail'), {title: 'Message'});
    return;
  }
  // A control carrying its own fragment: an attachment's Open button or filename, a member inside
  // an archive, "Back to message". Checked before the row handler and restricted to buttons,
  // because a whole row uses `data-frag` too and that branch has its own rules about what a click
  // inside it means.
  var control = e.target.closest('button[data-frag]');
  if (control) {
    e.preventDefault();
    var target = control.getAttribute('data-frag');
    if (!target) return;
    loadFragment(target, {
      title: control.classList.contains('att-open') || control.classList.contains('thumb-btn')
        ? 'Attachment' : 'Message',
      waiting: 'Opening\\u2026'
    });
    return;
  }
  var row = e.target.closest('tr[data-frag]');
  // A link inside a row is its own action — the PO cell must not also open the dialog.
  if (!row || e.target.closest('a,button')) return;
  showFragment(row);
});

document.addEventListener('keydown', function (e) {
  if (e.key !== 'Enter') return;
  var row = e.target.closest && e.target.closest('tr[data-frag]');
  if (row) showFragment(row);
});

// The row names the fragment to fetch, so one path serves both the message popup and a purchase
// order's progress bar. The URL was built and escaped server-side; nothing is assembled here.
function showFragment(row, withImages) {
  window.__lastMailRow = row;
  loadFragment(row.getAttribute('data-frag') + (withImages ? '&images=1' : ''),
               {title: 'Message', waiting: 'Opening\\u2026'});
}

// One fetch path for every kind of fragment. `opts` names the method, the dialog heading and the
// text to show while the request is in flight — the verify endpoints take seconds, and a dialog
// that says only "Opening" for eight of them reads as hung.
function loadFragment(url, opts) {
  opts = opts || {};
  var body = document.getElementById('mailbox-body');
  var head = document.getElementById('mailbox-title');
  if (head) head.textContent = opts.title || 'Message';
  body.innerHTML = '<p class="note">' + (opts.waiting || 'Opening\\u2026') + '</p>';
  openDialog('mailbox-dialog');
  fetch(url, {method: opts.method || 'GET'})
    .then(function (r) { return r.text(); })
    // `refreshEveryView` because a fragment can carry its own paged table — a spreadsheet
    // attachment preview is one. The first paint runs at page load, which was long before this
    // markup existed, so without this the table arrives showing every row it has.
    .then(function (html) { body.innerHTML = html; fitFrames(body); refreshEveryView(); })
    .catch(function (err) {
      body.innerHTML = '<p class="empty">Could not open this: ' + err + '</p>';
    });
}

// The body frame has no scripts of its own, so the page sizes it from out here. Without this it
// falls back to the CSS height and scrolls internally, which is a worse read but not a broken one.
function fitFrames(scope) {
  scope.querySelectorAll('iframe.mail-body').forEach(function (f) {
    f.addEventListener('load', function () {
      try {
        var h = f.contentDocument.body.scrollHeight;
        f.style.height = Math.min(Math.max(h + 32, 220), 1400) + 'px';
      } catch (err) { /* leave the default height */ }
    });
  });
}

// The sidebar collapse. It lives in this block rather than a second script tag, because these
// pages promise exactly one first-party script and no `src=` — and a test holds them to it.
//
// The class goes on the root element, not the body, and this whole block is emitted in the head.
// That ordering is the point: the element exists before the sidebar is parsed, so a rail the user
// collapsed last time is never briefly painted open. Everything else here is delegated off
// `document`, which also exists by then, so nothing needs to wait for DOMContentLoaded.
try {
  if (localStorage.getItem('premier-rail') === '1') document.documentElement.classList.add('rail');
} catch (err) { /* storage disabled: start expanded */ }

document.addEventListener('click', function (e) {
  var btn = e.target.closest('.rail-toggle');
  if (!btn) return;
  var collapsed = document.documentElement.classList.toggle('rail');
  btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  btn.setAttribute('title', collapsed ? 'Expand the menu' : 'Collapse the menu');
  try { localStorage.setItem('premier-rail', collapsed ? '1' : '0'); } catch (err) { /* private mode */ }
});

// Table search. One delegated listener for every filter on the page.
//
// The haystack is NOT just the visible text. `_clipped()` in routes.py truncates Subject and Why
// to fit a 13-column grid and keeps the whole string only in `title` — so filtering on rendered
// text alone would miss most subject matches and quietly report "nothing found" for mail that is
// sitting right there. Every descendant `title` is folded in. The row's own title is not, because
// `table()` sets it to "Open this message" on every clickable row, which would match everything.
//
// Built once per row and cached on the element: re-reading textContent of 300 rows on every
// keystroke is what makes a filter feel laggy.
function rowHaystack(row) {
  if (row.__hay === undefined) {
    var parts = [row.textContent];
    row.querySelectorAll('[title]').forEach(function (el) { parts.push(el.getAttribute('title')); });
    row.__hay = parts.join(' ').toLowerCase().replace(/\\s+/g, ' ');
  }
  return row.__hay;
}

function anyFilterActive() {
  return Array.prototype.some.call(document.querySelectorAll('input.filter'),
                                   function (i) { return i.value.trim() !== ''; });
}

// Which page each table is on. Not in the URL: paging is a way of looking at a table, not a place
// you navigated to, and putting it in the address bar would make the ten-second poller's reload
// land somewhere other than where it started.
var pageOf = {};

// The leading YYYY-MM-DD of a cell. Every date this app renders is ISO — `email_date` is
// `2026-07-13T13:40:11Z`, `pod_stated_date` is `2025-12-15`, `html.when` clips to 16 characters —
// so the first ten characters are the day, and comparing them as strings is comparing them as
// dates. Anything that is not a date returns '' and is excluded by a range rather than sorted
// into the middle of one.
function dayOf(text) {
  var m = (text || '').match(/\\d{4}-\\d{2}-\\d{2}/);
  return m ? m[0] : '';
}

// Which column holds this table's dates, from `table(date_column=...)`. -1 when it has none, in
// which case a date range cannot narrow it and is left alone rather than silently hiding rows.
function dateColumnOf(scope) {
  var heads = scope.querySelectorAll('thead th');
  for (var i = 0; i < heads.length; i++) {
    if (heads[i].getAttribute('data-date')) return i;
  }
  return -1;
}

function rowDay(row, column) {
  if (column < 0) return '';
  var cell = row.children[column];
  return cell ? dayOf(cell.textContent) : '';
}

// A table is a list of *units*. Normally a unit is one row. The receiver report is grouped by
// purchase order (`data-group`), and there a unit is the whole block — a page boundary falling
// between a PO's header and its receipt lines would split the one thing that sheet exists to show
// whole. Rows with no group in group mode are the sheet's title and column headings: always shown,
// never counted.
function unitsOf(scope, byGroup) {
  var rows = Array.prototype.slice.call(scope.querySelectorAll('tbody tr'));
  if (!byGroup) return {units: rows.map(function (r) { return [r]; }), chrome: []};
  var order = [], groups = {}, chrome = [];
  rows.forEach(function (row) {
    var key = row.getAttribute('data-group');
    if (!key) { chrome.push(row); return; }
    if (!groups[key]) { groups[key] = []; order.push(key); }
    groups[key].push(row);
  });
  return {units: order.map(function (k) { return groups[k]; }), chrome: chrome};
}

// One pass does filtering, paging and the zebra together. They were two independent passes at
// first, and each kept overwriting the other's mind about which rows were visible.
//
// Every whitespace-separated word must match, so "208491 delivered" narrows rather than widens. A
// unit matches when *any* of its rows does: searching a PO number has to bring back that purchase
// order's whole block, not the single line the digits happen to sit on.
function refreshView(id) {
  var scope = document.getElementById(id);
  if (!scope) return;
  var control = document.querySelector('.pager[data-pager-for="' + id + '"]');
  var byGroup = control && control.getAttribute('data-unit') === 'group';
  var parts = unitsOf(scope, byGroup);
  // `~=` matches one id inside a space-separated list, so a search box covering three tables is
  // found by each of them.
  var input = document.querySelector('input.filter[data-filter~="' + id + '"]');
  var terms = input ? input.value.toLowerCase().split(/\\s+/).filter(function (t) {
    return t !== '';
  }) : [];

  var fromEl = document.querySelector('input.date-from[data-from~="' + id + '"]');
  var toEl = document.querySelector('input.date-to[data-to~="' + id + '"]');
  var from = fromEl ? fromEl.value : '';
  var to = toEl ? toEl.value : '';
  var dateColumn = (from || to) ? dateColumnOf(scope) : -1;

  var matched = [];
  parts.units.forEach(function (unit) {
    var hit = !terms.length || unit.some(function (row) {
      var hay = rowHaystack(row);
      return terms.every(function (t) { return hay.indexOf(t) !== -1; });
    });
    // Both ends inclusive — "1st to 5th" includes the 5th to everyone who is not a database. A row
    // whose date cell is blank is out as soon as a range is set: it cannot be shown to be inside
    // one, and quietly keeping it would make the range a suggestion rather than a filter.
    if (hit && dateColumn >= 0) {
      hit = unit.some(function (row) {
        var day = rowDay(row, dateColumn);
        return day && (!from || day >= from) && (!to || day <= to);
      });
    }
    unit.forEach(function (row) { row.classList.toggle('filtered-out', !hit); });
    if (hit) matched.push(unit);
  });
  parts.chrome.forEach(function (row) { row.classList.remove('filtered-out', 'paged-out'); });
  scope.classList.toggle('filtering', terms.length > 0);

  var size = control ? parseInt(control.getAttribute('data-page-size'), 10) || 0 : 0;
  var pages = size > 0 ? Math.max(1, Math.ceil(matched.length / size)) : 1;
  var page = Math.min(Math.max(pageOf[id] || 1, 1), pages);
  pageOf[id] = page;
  var first = (page - 1) * size;

  var shown = 0;
  matched.forEach(function (unit, index) {
    var off = size > 0 && (index < first || index >= first + size);
    unit.forEach(function (row) {
      row.classList.toggle('paged-out', off);
      if (!off) { row.classList.toggle('alt', shown % 2 === 1); shown++; }
      else { row.classList.remove('alt'); }
    });
  });

  var noun = byGroup ? 'purchase order' : 'row';
  var tally = {matched: matched.length, total: parts.units.length, noun: noun};
  if (!control) return tally;
  var label = control.querySelector('.pager-label');
  // Says what is on screen, not just "page 2 of 5" — the count is the part that tells you whether
  // the thing you are looking for is even in this table.
  if (label) {
    label.textContent = size > 0
      ? 'Page ' + page + ' of ' + pages + ' — ' + matched.length + ' ' + noun
        + (matched.length === 1 ? '' : 's')
      : 'All ' + matched.length + ' ' + noun + (matched.length === 1 ? '' : 's');
  }
  var prev = control.querySelector('[data-page="prev"]');
  var next = control.querySelector('[data-page="next"]');
  if (prev) prev.disabled = size === 0 || page <= 1;
  if (next) next.disabled = size === 0 || page >= pages;
  return tally;
}

function targetsOf(input) {
  return (input.getAttribute('data-filter') || '').split(/\\s+/).filter(function (t) {
    return t !== '';
  });
}

// Refresh every table a search box covers, then write one count across all of them. Three separate
// readouts on the manual queue would make someone add up their own search results.
function refreshFor(input, resetPage) {
  var ids = targetsOf(input);
  var matched = 0, total = 0, noun = 'row';
  ids.forEach(function (id) {
    if (resetPage) pageOf[id] = 1;
    var tally = refreshView(id);
    if (!tally) return;
    matched += tally.matched; total += tally.total; noun = tally.noun;
  });
  var readout = document.querySelector('[data-count-for="' + ids.join(' ') + '"]');
  if (!readout) return;
  // The readout has to speak for the date range too, not just the text box. A range that hides
  // two thirds of a table while the count sits blank is the same silent-hiding problem the count
  // exists to prevent.
  if (!narrowed(input)) readout.textContent = '';
  else if (!matched) readout.textContent = 'No ' + noun + 's match — ' + total + ' hidden';
  else readout.textContent = 'Showing ' + matched + ' of ' + total + ' ' + noun
    + (total === 1 ? '' : 's');
}

// Is anything narrowing this set — text typed, or either end of a date range set?
function narrowed(input) {
  if (input && input.value.trim()) return true;
  var ids = input ? targetsOf(input) : [];
  return ids.some(function (id) {
    var f = document.querySelector('input.date-from[data-from~="' + id + '"]');
    var t = document.querySelector('input.date-to[data-to~="' + id + '"]');
    return (f && f.value) || (t && t.value);
  });
}

function refreshEveryView() {
  document.querySelectorAll('.scroll[id]').forEach(function (s) { refreshView(s.id); });
  document.querySelectorAll('input.filter').forEach(function (i) { refreshFor(i, false); });
}

document.addEventListener('input', function (e) {
  if (!e.target.matches || !e.target.matches('input.filter')) return;
  // Back to page one. Filtering to four matches while parked on page three would otherwise show an
  // empty table, and the reason would be invisible.
  refreshFor(e.target, true);
});

// Redraw through the search box when one covers this table, so its count stays right; otherwise
// the table alone.
function refreshOwning(id) {
  var input = document.querySelector('input.filter[data-filter~="' + id + '"]');
  if (input) refreshFor(input, false); else refreshView(id);
}

// --- sorting ------------------------------------------------------------------
// Sorts the rows in place, so paging and filtering — which both read DOM order — need to know
// nothing about it. Grouped tables (the receiver report) emit no sortable headings at all, which
// is deliberate: it is laid out to be read beside Spitfire's own sheet, and reordering its rows
// would take that away.

// What a cell sorts by. Numbers as numbers ('9' before '10'), dates as their ISO day, everything
// else case-insensitively. Decided per cell rather than per column so a column of numbers with an
// em-dash in the empty rows still sorts numerically.
function sortKey(text) {
  var value = (text || '').trim();
  var day = dayOf(value);
  if (day) return {n: null, s: value.replace(dayOf(value), day)};
  var numeric = value.replace(/[,\\s]/g, '');
  if (numeric !== '' && /^-?\\d*\\.?\\d+%?$/.test(numeric)) {
    return {n: parseFloat(numeric), s: value};
  }
  return {n: null, s: value.toLowerCase()};
}

// An empty cell. `html.muted('—')` is what this app renders for "no value", so testing for '' alone
// would treat forty em-dashes as forty ordinary values and sort them into the middle of the column.
function isBlank(key) {
  return key.n === null && (key.s === '' || key.s === '\\u2014' || key.s === '-');
}

function compareCells(a, b) {
  if (a.n !== null && b.n !== null) return a.n - b.n;
  if (a.n !== null) return -1;
  if (b.n !== null) return 1;
  return a.s < b.s ? -1 : (a.s > b.s ? 1 : 0);
}

function sortBy(id, column, direction) {
  var scope = document.getElementById(id);
  if (!scope) return;
  var tbody = scope.querySelector('tbody');
  var rows = Array.prototype.slice.call(scope.querySelectorAll('tbody tr'));
  var keyed = rows.map(function (row, index) {
    var cell = row.children[column];
    // `index` keeps the sort stable, so rows equal on this column stay in the order the server
    // sent them — which is newest-first almost everywhere and is information in its own right.
    return {row: row, key: sortKey(cell ? cell.textContent : ''), index: index};
  });
  keyed.sort(function (a, b) {
    // Blanks last in BOTH directions, so this is settled before the direction is applied.
    // Folding it into `compareCells` looked equivalent and was not: the descending branch negates
    // whatever that returns, which flipped the rule and led every descending sort with a column of
    // em-dashes — burying the rows you sorted it to see.
    var blankA = isBlank(a.key), blankB = isBlank(b.key);
    if (blankA !== blankB) return blankA ? 1 : -1;
    var by = compareCells(a.key, b.key);
    if (by !== 0) return direction === 'descending' ? -by : by;
    return a.index - b.index;
  });
  keyed.forEach(function (entry) { tbody.append(entry.row); });

  scope.querySelectorAll('thead th').forEach(function (th) {
    var mine = th.getAttribute('data-sort') === String(column);
    th.setAttribute('aria-sort', mine ? direction : 'none');
  });
}

document.addEventListener('click', function (e) {
  var button = e.target.closest && e.target.closest('.sort-btn');
  if (!button) return;
  var th = button.closest('th');
  var scope = button.closest('.scroll');
  if (!th || !scope || !scope.id) return;
  // First click sorts ascending; clicking the sorted column again reverses it.
  var next = th.getAttribute('aria-sort') === 'ascending' ? 'descending' : 'ascending';
  sortBy(scope.id, parseInt(th.getAttribute('data-sort'), 10), next);
  // Back to page one: sorting is done to bring something to the top, and staying on page three
  // would show the rows that are now least interesting.
  pageOf[scope.id] = 1;
  refreshOwning(scope.id);
});

document.addEventListener('change', function (e) {
  if (!e.target.matches || !e.target.matches('input.date-from, input.date-to')) return;
  var targets = e.target.getAttribute('data-from') || e.target.getAttribute('data-to') || '';
  targets.split(/\\s+/).filter(Boolean).forEach(function (id) {
    pageOf[id] = 1;
    refreshOwning(id);
  });
});

document.addEventListener('click', function (e) {
  var clear = e.target.closest && e.target.closest('[data-date-clear]');
  if (!clear) return;
  var targets = clear.getAttribute('data-date-clear');
  document.querySelectorAll('input.date-from, input.date-to').forEach(function (i) {
    if ((i.getAttribute('data-from') || i.getAttribute('data-to')) === targets) i.value = '';
  });
  targets.split(/\\s+/).filter(Boolean).forEach(function (id) {
    pageOf[id] = 1;
    refreshOwning(id);
  });
});

document.addEventListener('click', function (e) {
  var button = e.target.closest && e.target.closest('.pager [data-page]');
  if (!button || button.disabled) return;
  var control = button.closest('.pager');
  var id = control.getAttribute('data-pager-for');
  pageOf[id] = (pageOf[id] || 1) + (button.getAttribute('data-page') === 'next' ? 1 : -1);
  refreshOwning(id);
  control.scrollIntoView({block: 'nearest'});
});

document.addEventListener('change', function (e) {
  if (!e.target.matches || !e.target.matches('.pager-size')) return;
  var control = e.target.closest('.pager');
  var id = control.getAttribute('data-pager-for');
  control.setAttribute('data-page-size', e.target.value);
  pageOf[id] = 1;
  refreshOwning(id);
});

// Escape clears rather than only blurring, so the table comes back without reaching for the mouse.
document.addEventListener('keydown', function (e) {
  if (e.key !== 'Escape') return;
  if (!e.target.matches || !e.target.matches('input.filter')) return;
  if (e.target.value === '') return;
  e.target.value = '';
  refreshFor(e.target, true);
});

// The first paint. Without it a paged table renders every row until someone touches a control,
// which is exactly the state paging exists to avoid.
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', refreshEveryView);
} else {
  refreshEveryView();
}

// New mail, without anyone pressing F5. These pages are server-rendered and had no client-side
// data fetching at all, so a row could sit in the database for twenty minutes while the tab
// showing it stayed blank. /ui/version is a two-integer read and touches no Graph API, which is
// what makes a ten-second poll affordable.
//
// The page reloads *itself* only when doing so cannot interrupt anyone: scrolled to the top, no
// message dialog open, tab in the foreground. Otherwise the pill waits to be clicked. Reloading
// under someone reading a POD is worse than being slightly out of date.
(function () {
  var POLL_MS = 10000;
  var baseline = document.body && document.body.getAttribute('data-version');
  if (!baseline) return;

  function pill() { return document.getElementById('live-pill'); }

  function safeToReloadWithoutAsking() {
    return window.scrollY < 4
      && !document.querySelector('dialog[open]')
      && document.visibilityState === 'visible'
      // Someone mid-search is reading a filtered table. Reloading throws the filter away and
      // drops them back at 300 unfiltered rows, which is worse than being ten seconds stale.
      && !anyFilterActive();
  }

  function check() {
    // A background tab polls nothing. The pill is still there when it comes back.
    if (document.visibilityState !== 'visible') return;
    fetch('/ui/version', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (v) {
        if (!v || !v.token || v.token === baseline || v.token === 'unavailable') return;
        if (safeToReloadWithoutAsking()) { location.reload(); return; }
        var p = pill();
        if (p) p.hidden = false;
      })
      .catch(function () { /* a failed poll must never break the page */ });
  }

  document.addEventListener('click', function (e) {
    if (e.target.closest('#live-pill')) location.reload();
  });

  setInterval(check, POLL_MS);
})();
"""

# Hidden until the poller has something to say. Rendered by the shell so every page gets it
# without asking, and `aria-live` so it is announced rather than only seen.
_LIVE_PILL = (
    '<button id="live-pill" class="live-pill" hidden aria-live="polite" '
    'title="Reload to show the new mail">New mail — click to load</button>'
)

_DOCTYPE = "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
_VIEWPORT = "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"


# Empty since the corpus was retired on 2026-08-12. Every page now reads Premier's live mailbox, so
# there is no page where a "Re-run .msg corpus" button would describe what is on screen — it would
# read as the control for the live data it sits above, which it never was.
#
# Kept as a named empty set rather than deleted: `page()` still asks, and restoring the corpus is
# then one line here plus the `sample` entry in `read_views.MAIL_SOURCES`.
_CORPUS_PAGES: frozenset = frozenset()


def _kill_control() -> Raw:
    """The stop control, rendered by the shell so it is on every screen without each page
    remembering to ask for it.

    Imported inside the function: `killswitch` imports `store`, and putting that at module scope
    would open the operations database merely to render a page.
    """
    from operations import killswitch

    if not killswitch.is_stopped():
        return button_form("/ui/automation/stop", "Stop automation",
                           cls="btn danger small", glyph="■")
    since = killswitch.stopped_since() or ""
    stopped_at = f" at {since[11:16]}" if len(since) >= 16 else ""
    return Raw(
        str(button_form("/ui/automation/resume", "Resume", cls="btn ghost small", glyph="▶"))
        + str(tag("span", f"Stopped{stopped_at}", class_="kill-note"))
    )


def _kill_banner() -> Raw:
    from operations import killswitch

    if not killswitch.is_stopped():
        return Raw("")
    since = killswitch.stopped_since() or ""
    when = f" at {since[11:16]}" if len(since) >= 16 else ""
    return tag(
        "div",
        f"Automation is stopped{when}. Nothing will run — not the schedule, not Run now — until "
        "you press Resume. Any run in progress finishes the email it was on and then halts; "
        "nothing is lost, and unread mail is picked up next time.",
        class_="stopped-banner",
    )


def _sidebar(active: str, counts: Optional[dict] = None) -> Raw:
    groups = []
    for label, entries in _NAV:
        links = []
        for href, text in entries:
            children = [_ICONS.get(href, Raw("")), tag("span", text, class_="lbl")]
            count = (counts or {}).get(href)
            if count:
                children.append(tag("span", count, class_="count"))
            links.append(tag("a", *children, href=href,
                             class_="on" if href == active else None,
                             # Carries the label when the rail is collapsed and the text is hidden.
                             title=text,
                             aria_current="page" if href == active else None))
        groups.append(tag("div", tag("div", label, class_="nav-label"), *links, class_="nav-group"))

    brand = tag(
        "div",
        tag("b", "Premier", tag("span", "Receiver")),
        tag("button", Raw("&lsaquo;"), class_="rail-toggle", type="button",
            title="Collapse the menu", aria_expanded="true", aria_label="Collapse the menu"),
        class_="brand",
    )
    foot = tag("div", _kill_control(), tag("span", "Powered by Spitfire", class_="powered"),
               class_="side-foot")
    return tag("aside", brand, tag("nav", *groups, class_="nav"), foot, class_="side")


def page(title: str, active: str, *sections, header: Optional[Raw] = None, footer: str = "",
         subtitle: str = "", counts: Optional[dict] = None, refresh_seconds: int = 0) -> str:
    """The one shell every page goes through.

    `active` is matched against the sidebar's hrefs, so a detail page passes its parent
    (`/ui/po/208491` passes `/ui/po`) and the parent stays lit. `counts` maps an href to a number
    for the badge beside it; only truthy values render, so a queue at zero shows nothing rather
    than a reassuring "0" the eye still has to read.

    `refresh_seconds` emits a `<meta http-equiv="refresh">`, and is meant for one thing: a page
    showing a run in progress, so it redraws itself as the run finishes. It is a meta tag rather
    than a poll in `_JS` because this app spends exactly one inline script and no external ones —
    `test_pages_carry_exactly_one_first_party_script_and_nothing_else` holds that line, and a
    progress refresh is not what that budget is for. **The caller must pass 0 once the run ends**,
    or every page quietly reloads itself for ever.
    """
    head_children = [tag("div", tag("h1", title),
                         tag("p", subtitle, class_="sub") if subtitle else Raw(""),
                         Raw(str(_run_form()) if active in _CORPUS_PAGES else ""),
                         class_="bar")]
    if header is not None:
        head_children.append(header)

    content = tag(
        "div",
        tag("header", *head_children),
        _kill_banner(),
        tag("main", *sections),
        tag("footer", footer) if footer else Raw(""),
        class_="content",
    )
    return (
        _DOCTYPE
        + _VIEWPORT
        + (f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">'
           if refresh_seconds > 0 else "")
        + str(tag("title", f"{title} · Premier Receiver"))
        + "<style>" + _CSS + "</style>"
        # In <head>, so the rail class lands before the sidebar is parsed. See `_JS`.
        + "<script>" + _JS + "</script>"
        + "</head>"
        + f'<body data-version="{esc(_version_token())}">'
        + str(tag("div", _sidebar(active, counts), content, class_="shell"))
        + _MAIL_DIALOG
        + _LIVE_PILL
        + "</body></html>"
    )


def _version_token() -> str:
    """Stamped on `<body>` so the poller has something to compare against without a second request.

    Imported inside the function for the same reason `_kill_control` does it: `routes` imports this
    module, and a module-scope import back would be circular — and would open a database merely to
    define a page renderer.
    """
    try:
        from api.ui.routes import live_version_token

        return live_version_token()
    except Exception:                                              # noqa: BLE001
        return ""


def _run_form() -> Raw:
    # Names the corpus explicitly: these pages also show mail read from the live mailbox, and a
    # button labelled "run pipeline" would read as though it polls that too.
    return tag("form", tag("button", "Re-run .msg corpus (mock OCR)", type="submit"),
               class_="run", method="post", action="/ui/run")
