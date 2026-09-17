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
    if attrs:
        rendered_attrs = "".join(
            f' {_ATTR_NAMES[key]}="{esc(value)}"'
            for key, value in attrs.items()
            if value is not None
        )
    else:
        rendered_attrs = ""
    if name in _VOID:
        return Raw(f"<{name}{rendered_attrs}>")
    # One child is overwhelmingly the common case — a cell holding a string, a span holding a word
    # — and going through `str.join` for it costs a generator and an iterator for nothing. This is
    # called 94,338 times to render `/ui/mails`, so the constant factor is the whole cost.
    if len(children) == 1:
        return Raw(f"<{name}{rendered_attrs}>{esc(children[0])}</{name}>")
    body = "".join([esc(child) for child in children])
    return Raw(f"<{name}{rendered_attrs}>{body}</{name}>")


class _AttributeNames(dict):
    """`class_` -> `class`, `data_mail_id` -> `data-mail-id`, worked out once per spelling.

    A `dict` subclass with `__missing__` rather than a function with a cache, so the lookup on the
    hot path is a plain subscript. The same twenty-odd attribute names are rendered on every row of
    every table — `rstrip` and `replace` ran 168,419 times on one page to produce twenty answers.
    """

    def __missing__(self, key: str) -> str:
        self[key] = name = key.rstrip("_").replace("_", "-")
        return name


_ATTR_NAMES = _AttributeNames()


def badge(text: str, kind: str = "") -> Raw:
    return tag("span", text, class_=f"badge badge-{kind or 'plain'}")


def field(name: str, label: str, value="", *, kind: str = "text", required: bool = False,
          hint: str = "", placeholder: str = "", readonly: bool = False,
          source: str = "") -> Raw:
    """One labelled input.

    Built here rather than as an f-string in `routes.py`, for the reason the module docstring gives:
    markup is assembled in this file or not at all. The values these carry come straight off a
    delivery email — a PO number, a description, a spec code — which is attacker-influenced text by
    definition, and `tag()` escapes every one of them.

    `source` names where a read-only value came from ("from the purchase order"). A greyed field
    with no explanation reads as broken; one that says who supplied it reads as settled.
    """
    control = tag("input", type=kind, name_=name, id=f"f-{name}",
                  value="" if value is None else str(value),
                  placeholder=placeholder or None,
                  required="required" if required else None,
                  readonly="readonly" if readonly else None,
                  class_="fld" + (" fld-ro" if readonly else ""))
    parts = [tag("label", label, tag("span", " *", class_="req") if required else "",
                 for_=f"f-{name}"), control]
    if source:
        parts.append(tag("p", source, class_="fld-src"))
    if hint:
        parts.append(tag("p", hint, class_="fld-hint"))
    return tag("div", *parts, class_="field")


def textarea(name: str, label: str, value="", *, hint: str = "", rows: int = 3) -> Raw:
    return tag("div",
               tag("label", label, for_=f"f-{name}"),
               tag("textarea", "" if value is None else str(value), name_=name, id=f"f-{name}",
                   rows=str(rows), class_="fld"),
               tag("p", hint, class_="fld-hint") if hint else "",
               class_="field")


def note_presets(field: str, options, *, label: str = "Common reasons") -> Raw:
    """Chips that fill a textarea, for a field whose answers repeat.

    The same trick as `_date_presets`: pressing one writes into the box the form already submits,
    rather than being a second way of saying the thing. The textarea stays editable and stays the
    only thing that is read — so a preset is a shortcut for typing, never a separate value the
    server has to know about, and a reason nobody anticipated is still just typed.

    `options` is `(chip label, the sentence it writes)`. They differ because the button has to be
    scannable at a glance and the stored reason has to still make sense to somebody reading it back
    months later with none of this screen around it.

    Toggling, not filling: pressing a lit chip takes its sentence back out, and two chips write two
    clauses joined by `; `. Most of these messages are set aside for more than one reason at once —
    an advert with no PO in it is both — and a control that could only ever say one of them would be
    the reason people went on typing.

    The pressed chip is `.sel` and never `.on`, which is reserved for the active sidebar entry and
    counted across the whole document by `test_the_active_nav_entry_is_marked_on_every_page`.
    """
    chips = [tag("button", text, type="button", class_="chip", aria_pressed="false",
                 data_preset=phrase, data_preset_for=f"f-{field}")
             for text, phrase in options]
    return tag("div",
               tag("span", label, class_="preset-label"),
               tag("div", *chips, class_="chips chips-wrap", role="group", aria_label=label),
               class_="presets")


def radio(name: str, value: str, label, *, checked: bool = False, hint="") -> Raw:
    """One option in a group. `label` may be built markup — the POD chooser puts a whole row of
    filename, type, badges and an Open control inside its labels."""
    return tag("label",
               tag("input", type="radio", name_=name, value=value,
                   checked="checked" if checked else None),
               tag("span", label, class_="radio-label"),
               tag("span", hint, class_="radio-hint") if hint else "",
               class_="radio")


def form(action: str, *children, submit: str = "Save", cancel: str = "",
         cancel_label: str = "Cancel") -> Raw:
    """A POST form. Everything that changes state is a POST — a link would let a prefetch press it.

    Deliberately a whole-page form rather than a dialog fetch: a refused submission has to come back
    carrying what the reviewer typed, and re-rendering the page does that with no JavaScript at all.
    These pages spend exactly one inline script and it is not for this.
    """
    buttons = [tag("button", submit, type="submit", class_="btn")]
    if cancel:
        # `data-back-to` lets the script make this a history step when the previous entry really is
        # that page, so Cancel hands back the queue exactly as it was left — filter, page and scroll
        # — instead of fetching a fresh one. The href is what happens without a script.
        buttons.append(tag("a", cancel_label, href=cancel, class_="btn ghost", data_back_to=cancel))
    return tag("form", *children,
               # Where to go once this is recorded. Filled in by the script as the form leaves, and
               # validated by the server, which never redirects anywhere a page merely asked for.
               # Empty here, so a submission with no script lands on the route's own destination.
               tag("input", type="hidden", name_="return_to", value=""),
               tag("div", *buttons, class_="form-actions"),
               method="post", action=action, class_="uform")


def errors(message: str, items=()) -> Raw:
    """Why a submission was refused, above the form that was refused.

    Named fields rather than a count: "missing: spec code, POD date" tells somebody which cells to
    fill, where "2 errors" tells them only that something is wrong — the same reason the manual
    queue stopped printing "confidence 0.0".
    """
    listed = [tag("li", item) for item in items]
    return tag("div", tag("p", message),
               tag("ul", *listed) if listed else "", class_="errors")


def origin_badge(origin, created_by="") -> Raw:
    """Whether a record was extracted or entered by a person, and by whom.

    Both states are labelled. Badging only the manual ones would make "no badge" mean two things —
    automated, or a row from before this column existed — and the difference matters to anyone
    auditing what reached Premier's ERP.

    The same distinction rides the report's Receiver column, so the screen and the PDF agree
    without the sheet needing a tenth column. See `pipeline.receipt_log._receiver`.
    """
    if str(origin or "auto") == "manual":
        who = str(created_by or "").strip()
        return badge(f"Manual · {who}" if who else "Manual", "record")
    return badge("Automated", "plain")


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
          no_sort: Sequence[str] = (), date_column: Optional[str] = None,
          choice_column: Optional[str] = None, pane: bool = False,
          num_columns: Sequence[str] = (), select_ids: Optional[Sequence] = None,
          sort_urls: Optional[dict] = None, sorted_by: Sequence = ("", ""),
          select_noun: str = "row", choice_values: Optional[Sequence] = None,
          reason_values: Optional[Sequence] = None,
          po_values: Optional[Sequence] = None,
          email_ids: Optional[Sequence] = None,
          group_keys: Optional[Sequence] = None,
          ) -> Raw:
    """Rows can be made clickable two ways, and both end up as one thing.

    `email_ids` — one per row — says which message the row came from, without making it a link.
    That is what lets a verdict taken in the popup clear every row of that message from the table
    it is sitting over.

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

    `choice_column` is the same idea for a `choice_filter()` — the one column a dropdown matches
    against, by heading rather than position, so the filter cannot drift onto its neighbour.

    `num_columns` names the headings whose cells hold quantities, and right-aligns both the cell and
    its heading. Named rather than indexed for the same reason as the two above. It is what makes
    `175.04` and `6` line up on their last digit instead of on their first, which is the only way a
    column of quantities can be compared by eye.

    `select_ids` — one id per row — puts a checkbox in front of every row and a select-the-page
    checkbox in the heading. The selection is not a form: it is read by whatever control acts on it
    (`verify_button(selection=...)`), so the page never posts a list of ids it did not mean to.

    `choice_values` — one string per row — is what a `choice_filter()` matches against *instead of*
    a column's text. It is for a state that is not any one cell: "this record needs a person" is a
    reading of a badge, a button and a tooltip together, and a filter matching the rendered text of
    that cell would be matching "3 gaps Fill". Rows carry it as `data-choice-value`.

    A row's value may be **several space-separated words**, and an option matches if it names any
    of them. A state is not always exclusive: an attachment can be a signature logo *and* a file
    nothing could read, and it has to be found under either.

    `reason_values` is the second such dimension, for `reason_filter()`. Two are needed rather than
    one because the queue is narrowed two independent ways at once — what kind of thing this is,
    and why it is here — and folding them into one control would make picking a reason silently
    clear the kind.
    """
    numeric = {header for header in num_columns}
    if numeric - set(headers):
        raise ValueError(f"num_columns {sorted(numeric - set(headers))} are not among {list(headers)}")
    number_at = {index for index, header in enumerate(headers) if header in numeric}

    if select_ids is not None and not table_id:
        raise ValueError("select_ids needs a table_id — the controls that read the selection find "
                         "it by the table's id")

    # Materialised because the total is read twice — once to stamp `data-row-total`, once by the
    # pager — and `rows` may be a generator that can only be walked once.
    #
    # There is no cap here any more. This used to send only the newest 500 rows and print a note
    # saying so, with a "Load all" link under the table. Every table now ships every row it stands
    # for, so the browser's search, its filters and its sort all cover the whole set rather than a
    # slice of it. What the cap was really guarding — the weight of the response — is handled where
    # it belongs: gzip (`api/main.py`), and server-side paging for the one table too large to send
    # at all (`read_views.attachments`, which has taken `q`/`limit`/`offset` all along).
    rows = list(rows)
    total = len(rows)

    body_rows = []
    for index, row in enumerate(rows):
        cells = [tag("td", cell, class_="num" if position in number_at else None)
                 for position, cell in enumerate(row)]
        if select_ids is not None:
            picked = select_ids[index] if index < len(select_ids) else ""
            cells.insert(0, tag("td", tag(
                "input", type="checkbox", class_="pick", data_pick_for=table_id,
                value=str(picked), aria_label=f"Select {select_noun} {picked}"), class_="pick"))
        url, title = None, frag_title
        if frag_urls is not None and index < len(frag_urls) and frag_urls[index]:
            url = frag_urls[index]
        elif mail_ids is not None and index < len(mail_ids) and mail_ids[index]:
            target = mail_ids[index]
            email_id, reason = target if isinstance(target, (tuple, list)) else (target, "")
            url = f"/ui/mail?id={quote(str(email_id))}&reason={quote(str(reason or ''))}"
            title = "Open this message"
        value = (choice_values[index] if choice_values is not None and index < len(choice_values)
                 else None)
        why = (reason_values[index] if reason_values is not None and index < len(reason_values)
               else None)
        # `data-group` makes every row of one delivery a single unit to the client: searching a
        # purchase order brings back its whole block rather than the one line the digits sat on,
        # and a page boundary cannot fall between an item and the delivery it arrived on. The
        # receipt sheet has grouped this way since it was written; this threads it through
        # `table()` so an ordinary table can do it too.
        group = (group_keys[index] if group_keys is not None and index < len(group_keys) else None)
        # `data-po` is what a bare number in the search box is matched against, instead of the
        # row's text. One email's subject can name seven purchase orders — the property confirmation
        # names 907505, 907514, 912559, 907249, 908705, 912560 and 912614 — so searching `912614`
        # returned 46 rows on the queue and **not one of them was that PO**. Every row still
        # carried its own PO in its own column; the search was reading the subject.
        po = (po_values[index] if po_values is not None and index < len(po_values) else None)
        # Which message this row came from — not a link, an identity. A message set aside in the
        # popup takes its rows off the page in place, and without this the only way to find out
        # which rows those were is to fetch the whole page again, which is the reload the popup
        # exists to avoid. Distinct from `data-mail`, which is a URL to open.
        mail_of = (email_ids[index] if email_ids is not None and index < len(email_ids) else None)
        if url:
            body_rows.append(tag("tr", *cells, class_="clickable", tabindex="0",
                                 data_frag=url, title=title, data_choice_value=value,
                                 data_reason=why, data_group=group, data_po=po,
                                 data_mail_id=mail_of))
        else:
            body_rows.append(tag("tr", *cells, data_choice_value=value, data_reason=why,
                                 data_group=group, data_po=po, data_mail_id=mail_of))
    if not body_rows:
        return tag("p", empty, class_="empty")
    if date_column is not None and date_column not in headers:
        raise ValueError(f"date_column {date_column!r} is not one of {list(headers)}")
    if choice_column is not None and choice_column not in headers:
        raise ValueError(f"choice_column {choice_column!r} is not one of {list(headers)}")

    # Every sortable heading names its own column by position, so the checkbox column shifts them
    # all by one. Computed here rather than at the call site: a page adding a selection should not
    # have to know that sorting counts cells.
    offset = 1 if select_ids is not None else 0
    cells = []
    if select_ids is not None:
        cells.append(tag("th", tag("input", type="checkbox", class_="pick-all",
                                   data_pick_all=table_id,
                                   aria_label=f"Select every {select_noun} on this page"),
                         class_="pick"))
    for index, header in enumerate(headers):
        if sort_urls is not None:
            # Server-sorted: the heading is a link, because the order is decided by the query
            # string and not by rearranging rows the browser happens to hold. A heading with no
            # entry in `sort_urls` is a column there is no sensible ORDER BY for -- the PO cell is
            # three different kinds of evidence stitched together -- and stays plain text.
            url = sort_urls.get(header)
            column, way = (list(sorted_by) + ["", ""])[:2]
            state = ("ascending" if way == "asc" else "descending") if header == column else "none"
            cells.append(tag(
                "th",
                tag("a", header, tag("span", "", class_="sort-arrow"), href=url,
                    class_="sort-btn") if url else header,
                class_="num" if header in numeric else None,
                aria_sort=state if url else None))
            continue
        if not table_id or header in no_sort:
            cells.append(tag("th", header, class_="num" if header in numeric else None,
                             data_date="1" if header == date_column else None,
                             data_choice="1" if header == choice_column else None))
            continue
        cells.append(tag(
            "th",
            tag("button", header, tag("span", "", class_="sort-arrow"),
                type="button", class_="sort-btn"),
            class_="num" if header in numeric else None,
            data_sort=str(index + offset), aria_sort="none",
            data_date="1" if header == date_column else None,
            data_choice="1" if header == choice_column else None,
        ))
    block = scroll_block(
        tag("table", Raw(str(tag("thead", tag("tr", *cells)))
                         + str(tag("tbody", *body_rows)))),
        table_id=table_id, page_size=page_size, pane=pane,
        unit="group" if group_keys is not None else "row",
        total=total,
    )
    return block


SERVER_PAGE_SIZES = (25, 50, 100, 250)
"""What a server-paged table offers. No "All": this control exists for the one table that cannot
be sent whole -- 29,766 rows is 25 MB of HTML -- and an All that re-creates exactly that is a
button whose only function is to hang the tab."""


def query_string(params: dict) -> str:
    """`?a=b&c=d` from the values that are actually set, in a stable order.

    Stable because these end up as hrefs all over one page: two links that mean the same thing
    should be the same string, or the browser treats them as different places and the back button
    starts retracing steps nobody took.
    """
    pairs = [(key, str(value)) for key, value in sorted(params.items())
             if value not in (None, "", 0)]
    return ("?" + "&".join(f"{quote(k)}={quote(v)}" for k, v in pairs)) if pairs else ""


def server_search(action: str, value: str, params: dict, *, table_id: str,
                  placeholder: str = "Search…", label: str = "Search") -> Raw:
    """A search box that asks the server, for a table the browser was never sent in full.

    `search_box()` is the other one, and the difference is the whole point of this pair: that one
    filters rows already in the document, which is only honest when every row is there. This one
    submits, so what it searches is the table, not the slice.

    A plain `<form method="get">`, so it works with the keyboard, with the back button, and with
    JavaScript off. The other filters ride along as hidden fields: searching must not silently
    drop the view you were looking at.
    """
    hidden = [tag("input", type="hidden", name_=key, value=str(val))
              for key, val in sorted(params.items()) if val not in (None, "", 0) and key != "q"]
    return tag(
        "form",
        *hidden,
        tag("label", label, for_=f"q-{table_id}", class_="sr-only"),
        tag("span", _SEARCH_ICON, class_="search-icon"),
        # `data-server-filter` is how the sweep in `test_every_paged_table_is_also_searchable`
        # recognises this as the searchable half of a paged table, exactly as `data-filter` marks
        # the client-side one.
        tag("input", type="search", id=f"q-{table_id}", name_="q", value=value or "",
            placeholder=placeholder, class_="filter", data_server_filter=table_id,
            autocomplete="off"),
        tag("button", "Search", type="submit", class_="btn small"),
        method="get", action=action, class_="search server-search")


def server_pager(action: str, params: dict, *, table_id: str, page: int, size: int,
                 total: int, unit: str = "row") -> Raw:
    """Which slice of the table this is, and the way to the rest -- as links, not script.

    Says the honest total in its own first sentence. That is what makes this different from the
    cap note it replaces: "Showing 1-25 of 29,766" is a position, and "the 500 most recent of
    29,766 are loaded" was an apology. Nothing here is hidden from the search box beside it,
    because that box asks the server too.

    Real `<a href>`s throughout, so every page of this table can be linked to, opened in a new tab
    and reached with JavaScript off.
    """
    pages = max(1, -(-total // size)) if size else 1
    page = min(max(1, page), pages)
    first = (page - 1) * size + 1 if total else 0
    last = min(page * size, total)

    def link_to(target: int, text, extra: str = "") -> Raw:
        if target == page:
            return tag("span", text, class_="pager-num on", aria_current="page")
        return tag("a", text, href=action + query_string({**params, "page": target}),
                   class_=("pager-num " + extra).strip())

    numbers = []
    for number in _page_window(page, pages):
        numbers.append(Raw("…") if number is None else link_to(number, str(number)))

    sizes = [tag("span", str(choice), class_="size on") if choice == size
             else tag("a", str(choice),
                      href=action + query_string({**params, "size": choice, "page": 1}),
                      class_="size")
             for choice in SERVER_PAGE_SIZES]

    return tag(
        "div",
        tag("span",
            (f"Showing {first:,}–{last:,} of {total:,} {unit}{'' if total == 1 else 's'}"
             if total else f"No {unit}s"),
            class_="pager-count"),
        tag("span",
            link_to(max(1, page - 1), Raw("&lsaquo;"), "prev") if page > 1 else Raw(""),
            *numbers,
            link_to(min(pages, page + 1), Raw("&rsaquo;"), "next") if page < pages else Raw(""),
            class_="pager-nums"),
        tag("span", "Rows ", *sizes, class_="pager-size-links"),
        class_="pager server-pager", data_server_pager_for=table_id)


def _page_window(page: int, pages: int):
    """First, last, and a few either side of where you are; `None` where a gap is elided.

    1,191 pages of attachments cannot all be links. Always showing the first and the last means
    "go back to the beginning" and "how deep does this go" stay one click away.
    """
    if pages <= 9:
        return list(range(1, pages + 1))
    window = {1, pages}
    window.update(n for n in range(page - 2, page + 3) if 1 <= n <= pages)
    out, previous = [], 0
    for number in sorted(window):
        if number - previous > 1:
            out.append(None)
        out.append(number)
        previous = number
    return out


def scroll_block(content: Raw, *, table_id: str = "", page_size: int = 0, unit: str = "row",
                 plain: bool = False, pane: bool = False, total: Optional[int] = None) -> Raw:
    """A table in its horizontal scroller, with a pager under it if one was asked for.

    Split out of `table()` because the receiver report sheet is not built by `table()` — it comes
    from `receipt_log.to_html` as a finished string — and it needs the same scroller, id and pager
    without being rebuilt cell by cell.

    `plain` leaves the `.scroll` zebra and header rules off. The sheet carries its own row classes
    (`r-po`, `r-line`, `r-rcpt`) and picking up a stripe on top of them is what the comment above
    the table CSS has always warned about.

    `pane` gives the block a height of its own so it scrolls *inside* the page rather than growing
    it: the heading row stays put, and the pager under it stays on screen instead of being fifty
    rows below the fold. Both scrollbars then belong to the same box, which is why a short page
    still shows the box at its full height rather than collapsing onto the rows it happens to hold.

    `total` is how many rows the table stands for. Stamped as `data-row-total` so the count is
    readable from the document itself rather than only from the prose under it.

    Only on a table that has an id. A table with no id is one nothing can search, page or point at,
    and `test_a_table_without_an_id_is_unchanged` holds those to exactly `<div class="scroll">` —
    an attribute they have no use for is still an attribute they gained.
    """
    classes = "scroll plain" if plain else "scroll"
    if pane:
        classes += " pane"
    block = tag("div", content, class_=classes, id=table_id or None,
                data_row_total=str(total) if total is not None and table_id else None)
    if not (table_id and page_size > 0):
        return block
    return Raw(str(block) + str(pager(table_id, page_size, unit=unit)))


DATE_PRESETS = (("Today", "0"), ("7d", "7"), ("30d", "30"), ("All", ""))
"""Preset ranges, as (label, days-back). `""` means no range at all, not zero days.

Ordered shortest-first so the row reads as a widening scale left to right, and `All` last because
it is the way out rather than one more choice among the others.
"""


def _date_presets(targets: str, label: str, extra: Raw = Raw("")) -> Raw:
    """Chips that fill the From/To boxes rather than filtering by themselves.

    This is the whole trick: pressing one writes a `YYYY-MM-DD` into the same `input.date-from`
    the range already uses and fires its `change`, so `refreshView` narrows the table by exactly
    the path it always has. No second filtering rule exists, and the two controls cannot disagree
    about what "the last 7 days" means because only one of them decides.

    The pressed chip must not be `class="on"` — that string is reserved for the active sidebar
    entry, and `test_every_page_lights_exactly_one_nav_entry` counts it across the whole document.
    """
    chips = [
        tag("button", text, type="button",
            # `sel`, not `on`. See above.
            class_="chip sel" if days == "" else "chip",
            aria_pressed="true" if days == "" else "false",
            data_range=days, data_range_for=targets)
        for text, days in DATE_PRESETS
    ]
    # `extra` is the exact-range segment, and it goes *inside* the group rather than beside it.
    # Standing outside as an unlabelled square, it read as a stray button nobody could name — the
    # question it answers ("when?") is the same question the four chips answer, so it belongs in
    # the same control.
    return tag("div", *chips, extra, class_="chips", role="group",
               aria_label=f"Quick {label} ranges")


def date_filter(target, label: str = "date", *, presets: bool = False) -> Raw:
    """From / To, narrowing a table to the rows whose date falls between them.

    Which column that is comes from `table(date_column=...)`, not from here — the control should
    not have to know that Received is the second column on Mail and POD date the eleventh on
    Records, and it should not break when someone inserts a column.

    Either end may be left empty: From alone means "since", To alone means "up to". Both are
    inclusive, which is what "1st to 5th" means to everyone who is not a database.

    `type="date"` gives a real picker for free and, more usefully, normalises whatever the browser
    shows the reader into `YYYY-MM-DD` — so the comparison never has to guess at a locale.

    `presets` adds Today/7d/30d/All in front. The two boxes stay either way: they are what the
    filter engine binds to (`data-from~=` / `data-to~=`), several tests require them on every list
    page, and a preset cannot express "the 3rd to the 9th".
    """
    targets = " ".join([target] if isinstance(target, str) else list(target))
    boxes = (
        tag("label", "From", for_=f"from-{targets.split()[0]}", class_="date-label"),
        tag("input", type="date", class_="date-from", data_from=targets,
            id=f"from-{targets.split()[0]}", aria_label=f"Earliest {label}"),
        tag("label", "to", for_=f"to-{targets.split()[0]}", class_="date-label"),
        tag("input", type="date", class_="date-to", data_to=targets,
            id=f"to-{targets.split()[0]}", aria_label=f"Latest {label}"),
        tag("button", "Clear", type="button", class_="btn ghost small", data_date_clear=targets),
    )
    if not presets:
        return tag("div", *boxes, class_="date-bar")
    # With presets in front, the two boxes fold into a popover behind a calendar button. Four chips
    # answer nearly every question anyone asks of this table, and From/To/Clear laid out beside them
    # is three more controls' worth of width for the rare case — which is what pushed the search box
    # into a third of the row. `<details>` and not a script: this app spends one inline block and
    # a disclosure widget is not what that budget is for.
    return tag(
        "div",
        _date_presets(
            targets, label,
            tag("details",
                # `data-range-for` with no `data-range`: this is the segment that lights when the
                # range came from the two boxes rather than from a preset — see `markChips`.
                tag("summary", _CALENDAR_ICON, tag("span", "Custom", class_="chip-cal-lbl"),
                    class_="chip chip-cal", data_range_for=targets,
                    title=f"Pick an exact {label} range"),
                tag("div", *boxes, class_="date-custom"),
                class_="date-more"),
        ),
        class_="date-bar",
    )


_CALENDAR_ICON = Raw(
    '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" '
    'stroke-width="1.5" stroke-linecap="round" aria-hidden="true">'
    '<rect x="2" y="3.2" width="12" height="11" rx="1.6"/>'
    '<path d="M2 6.6h12M5.4 1.8v2.6M10.6 1.8v2.6"/></svg>'
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

    Three parts, in the order someone reads them: what is on screen now, the way to another page,
    and how many rows a page holds. The numbered buttons between Prev and Next are written by the
    script, because how many there are depends on what the filter left — a server-rendered "1 2 3"
    would be wrong the moment anyone typed in the search box.
    """
    sizes = sorted({page_size, *PAGE_SIZES})
    options = [tag("option", str(size), value=str(size),
                   selected="selected" if size == page_size else None) for size in sizes]
    options.append(tag("option", "All", value="0"))
    return tag(
        "div",
        tag("span", "", class_="pager-showing", role="status", aria_live="polite"),
        tag("div",
            tag("button", Raw("&lsaquo;"), type="button", class_="page-btn", data_page="prev",
                title="Previous page", aria_label="Previous page"),
            tag("span", "", class_="pager-nums"),
            tag("button", Raw("&rsaquo;"), type="button", class_="page-btn", data_page="next",
                title="Next page", aria_label="Next page"),
            class_="pager-pages"),
        tag("div",
            tag("label", "Rows", for_=f"ps-{target}", class_="pager-size-label"),
            tag("select", *options, id=f"ps-{target}", class_="pager-size"),
            class_="pager-rows"),
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
    # The glyph sits inside the field rather than beside it. A bare bordered box above a grid reads
    # as one more form input; the magnifier is what says "type here to narrow what is below".
    return tag("div", tag("span", _SEARCH_ICON, field, class_="search"),
               tag("span", "", class_="filter-count", data_count_for=targets),
               class_="filter-bar")


_SEARCH_ICON = Raw(
    '<svg class="search-ico" viewBox="0 0 16 16" width="15" height="15" fill="none" '
    'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true">'
    '<circle cx="7" cy="7" r="4.3"/><path d="m10.3 10.3 3.2 3.2"/></svg>'
)


def choice_filter(target, options: Sequence[tuple], *, label: str,
                  all_label: str = "All", icon: Raw = Raw(""), boxed: bool = False) -> Raw:
    """A dropdown narrowing a table to the rows whose marked column holds the chosen value.

    Which column that is comes from `table(choice_column=...)`, exactly as the date range takes its
    column from `date_column` — the control names a value, never a position, so inserting a column
    cannot silently start filtering the wrong one.

    It composes with the search box and the date range rather than replacing them: all three narrow
    the same set, and `narrowed()` counts this one too, so the readout beside the search box speaks
    for a dropdown-only filter instead of leaving a two-thirds-empty table unexplained.

    `options` is `(value, label)`, and callers are expected to build it from the rows actually on
    the page — offering a verdict that nothing in the table carries is a filter whose only possible
    outcome is an empty table.

    A value may name several column values separated by `|`, which is how one entry can stand for a
    queue rather than a verdict: "My triage queue" is `hold|not read yet`, both of which mean a
    person still has to look.

    `boxed` folds the label into the control and marks it with a star — the option text says what
    the view is, so a second word in front of it is a word of chrome. `icon` puts a glyph of your
    own inside an unboxed one.
    """
    targets = " ".join([target] if isinstance(target, str) else list(target))
    items = [tag("option", all_label, value="")]
    items += [tag("option", text, value=value) for value, text in options]
    return tag(
        "label",
        _STAR_ICON if boxed else icon,
        tag("span", label, class_="choice-label vh" if boxed else "choice-label"),
        tag("select", *items, class_="choice-filter", data_choice_for=targets, aria_label=label),
        class_="choice-bar boxed" if boxed else "choice-bar",
    )


DOWNLOAD_ICON = Raw(
    '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" '
    'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M8 2v8m0 0 3-3m-3 3-3-3M2.8 13.2h10.4"/></svg>'
)
"""A download arrow, for a control that hands the reader a file rather than another page.

Lives here rather than at the call site because every glyph in this app is markup, and markup is
built in this module — see the module rules in `routes`.
"""

_STAR_ICON = Raw(
    '<svg class="choice-ico" viewBox="0 0 16 16" width="13" height="13" fill="currentColor" '
    'aria-hidden="true"><path d="m8 1.9 1.85 3.9 4.15.6-3 3 .71 4.3L8 11.65 4.29 13.7 5 9.4l-3-3 '
    '4.15-.6z"/></svg>'
)


def reason_filter(target, counts: Sequence[tuple], *, label: str = "Reason") -> Raw:
    """One chip per stated reason, each carrying how many rows give it.

    A dropdown would have hidden the shape of the queue behind a click. The whole point of this row
    is that "4 need OCR and 1 is corrupt" is readable without touching anything — the filtering is
    what you do *after* the counts have told you where the work is.

    Single-select: pressing one narrows to it, pressing it again clears. Not multi-select, because
    two reasons at once is a question nobody on this page has asked, and the chips would then need
    a way to say "none of these" that is different from "all of these".

    A reason nothing carries is still shown, greyed and unpressable. `Unreadable · 0` is worth
    saying: it means that class of failure is not happening, which a missing chip cannot say.

    The counts are of the whole queue, not of what the other controls have left — said so on hover,
    because a number that changed as you typed in the search box would be a third thing to track.

    They do not sum to the number of rows, and are not meant to. One row can give several reasons —
    a message standing for itself and for four attachments nobody could read gives two — and it is
    counted under each, because the count has to answer "how much work is of this kind" for the
    press that follows it.
    """
    targets = " ".join([target] if isinstance(target, str) else list(target))
    chips = [
        tag("button", text, tag("span", str(n), class_="pill-n"),
            type="button", class_="pill" if n else "pill off",
            disabled="disabled" if not n else None,
            aria_pressed="false", data_reason=value, data_reason_for=targets,
            title=(f"{n} rows in the queue give this reason" if n
                   else "nothing in the queue gives this reason"))
        for value, text, n in counts
    ]
    return tag("div", tag("span", f"{label}:", class_="reason-label"), *chips,
               class_="reason-bar", role="group", aria_label=f"{label} filter")


def stats(pairs: Sequence[tuple]) -> Raw:
    """The header strip: one figure per pair, label underneath, each in its own card.

    A pair may carry a third element — a tone: `good`, `warn`, or `alert`. It tints the figure so
    the strip reads at a glance instead of as eight identical numbers, and `alert` also fills the
    card, which is reserved for the one figure that means a person has to do something.

    Cards rather than a bare row of numbers because the strip sits directly above a table of the
    same numbers; without an edge each figure ran into its neighbour's label.
    """
    cells = []
    for pair in pairs:
        label, value = pair[0], pair[1]
        tone = pair[2] if len(pair) > 2 else ""
        cells.append(tag("div", tag("b", value), tag("span", label),
                         class_=f"stat stat-{tone}" if tone else "stat"))
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

    An empty `title` emits no heading at all. That is for the one case where a page holds a single
    section and the page's own title already names it — repeating "Mail" under a heading that says
    "Mail" is furniture, and an empty `<h2>` is worse: screen readers announce a heading with
    nothing in it.
    """
    if not title:
        head = Raw("")
    elif action is not None:
        head = tag("div", tag("h2", title), action, class_="sec-head")
    else:
        head = tag("h2", title)
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


def progress_label(phase: str) -> str:
    """The human name for a pipeline phase. `/ui/run-progress` sends this rather than the raw key,
    so the wording lives in one place instead of being duplicated in JavaScript."""
    return _PROGRESS_LABELS.get(phase, "Working")


def progress_ring(phase: str, done: int, total: int, note: str = "",
                  hidden: bool = False) -> Raw:
    """Where the run has got to: a ring, what it is doing, and the count behind it.

    Two states, and the difference is honest rather than cosmetic. With a `total` the ring is a
    real percentage. Without one — the mailbox fetch is a single call with no interior milestones,
    and it is most of a run's elapsed time — it spins instead, because a determinate bar frozen at
    0%% reads as stalled, which is the opposite of the truth.

    **This is server-rendered once and then advanced from the browser**, by the ticker in `_JS`
    against `/ui/run-progress`. It used to say "No JavaScript … the page's meta-refresh is what
    advances it", and that was true until the refresh was removed — after which nothing advanced it
    at all and the ring sat frozen for the whole of a multi-minute run. The `data-` hooks below are
    what the ticker writes into; the markup and the wording stay here, so the script sets values
    rather than composing sentences.

    Still no second script and no library: `test_pages_carry_exactly_one_first_party_script_and_
    nothing_else` is unaffected, because the ticker lives in the one inline block that already
    exists.

    `hidden` renders the whole block ready but not shown, for a page drawn while nothing is running.
    A run started from another tab can then be revealed in place instead of needing a reload.
    """
    label = progress_label(phase)
    if total > 0:
        percent = max(0, min(100, round(done * 100 / total)))
        ring = tag("div", tag("b", f"{percent}%", data_prog_pct="1"), class_="ring",
                   style=f"--pct:{percent}", data_prog_ring="1")
        detail = f"{done} of {total}"
    else:
        # `aria-hidden` on the ring, not the text: a spinner announces nothing useful, and the
        # phase beside it is the part worth reading aloud.
        ring = tag("div", tag("b", "", data_prog_pct="1"), class_="ring spin",
                   aria_hidden="true", data_prog_ring="1")
        # Reading used to be one call with nothing to count. The connector now reports each Graph
        # call as it returns, so what can be said honestly is how many have come back — a number
        # that moves on a slow read and stands still on a hung one.
        detail = (f"{done} mailbox call{'' if done == 1 else 's'} so far" if done
                  else "connecting to the mailbox")
    if note:
        detail = f"{detail} · {note}"
    return tag(
        "div",
        ring,
        tag("div",
            tag("div", label, class_="prog-what", data_prog_what="1"),
            tag("div", detail, class_="prog-detail", data_prog_detail="1")),
        class_="prog", data_prog="1", hidden="hidden" if hidden else None,
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
                style: str = "", glyph: str = "", busy_label: str = "",
                hidden_fields: Optional[dict] = None, disabled: bool = False,
                title: str = "") -> Raw:
    """A one-button form. Anything that changes state is a POST, so it cannot be a link — a link
    would let a prefetch or a crawler press it.

    `glyph` gives the button a one-character alternative shown only when the sidebar is collapsed.
    Without it "Stop automation" overflows a 58px rail, and the control an operator reaches for in
    a hurry is the last one that should be hard to read.

    `busy_label` is what the button says once pressed, while the request is in flight. For a button
    whose work is measured in milliseconds this is noise; for "Check now", which reaches Microsoft
    and can take twenty seconds, its absence was the whole complaint — the page sat unchanged and
    the only honest reading was that the click had not registered. `_JS` also disables the button,
    so the second and third presses that silence invited cannot start a second mailbox walk.
    """
    children = [tag("span", label, class_="lbl")]
    if glyph:
        children.append(tag("span", glyph, class_="glyph", aria_hidden="true"))
    # Escaped by `tag()` like any other attribute, which is what lets a Message-ID — 255 characters
    # of angle brackets and `@`, chosen by whoever sent the mail — be carried safely.
    fields = [tag("input", type="hidden", name_=name, value=str(value))
              for name, value in (hidden_fields or {}).items()]
    # The same empty field `form()` carries: the script writes the page this was pressed from into
    # it, and the handler validates it before redirecting anywhere. A handler that does not read it
    # is unaffected — an unread form field costs nothing.
    fields.append(tag("input", type="hidden", name_="return_to", value=""))
    button = tag("button", *children, type="submit", class_=cls, title=title or label,
                 data_busy_label=busy_label or None, disabled="disabled" if disabled else None)
    return tag("form", *fields, button,
               method=method, action=action, class_="inline-form", style=style or None)


def verify_button(url: str, label: str, *, title: str = "", small: bool = False,
                  ghost: bool = False, primary: bool = False, selection: str = "") -> Raw:
    """A button that fetches `url` and shows the answer in the shared dialog.

    Not a `button_form`, because the result is something to *read* rather than a page to land on —
    a form post would navigate away from the table the reader is working through. Not a link
    either: the endpoint calls out to Spitfire and refreshes the local mirror, so it must not be a
    URL a prefetch or a crawler can trip. The script posts it.

    `data-verify` carries the URL and `data-verify-title` the dialog heading; both are escaped by
    `tag()`, and the script only ever reads them back out as attribute values.

    `selection` names a table whose row checkboxes (`table(select_ids=...)`) narrow what this acts
    on: the script appends the ticked ids to the URL and relabels the button to say how many it is
    about to touch. With nothing ticked it keeps its own label and its own meaning — everything —
    so the control has one name in both states rather than being disabled until someone guesses
    that a checkbox is what turns it on.
    """
    classes = ["btn"]
    if primary:
        classes.append("primary")
    if ghost:
        classes.append("ghost")
    if small:
        classes.append("small")
    return tag("button", label, type="button", class_=" ".join(classes),
               title=title or label, data_verify=url, data_verify_title=title or label,
               data_verify_selection=selection or None,
               data_verify_all=label if selection else None)


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
              title: str = "", cls: str = "po-link") -> Raw:
    """A value in a table cell that opens the message it came from, in the shared dialog.

    A `button` rather than an `a`, for the reasons `verify_button` gives: the target is something
    to *read* rather than a page to land on, and a real link would navigate away from the table the
    reader is working through — `/ui/mail` returns a bare fragment, so landing on it gives an
    unstyled page with no way back.

    It also has to be a `button` to coexist with a clickable row. The row handler bails out on any
    click inside an `a` or a `button`, and the `[data-mail]` branch in `_JS` is checked before it —
    the same arrangement `[data-open]` and `[data-verify]` already rely on.

    `cls` is for the one place this is a control at the end of a row rather than a value inside
    one — the Attachments page, where it sits beside View and Download and has to read as their
    third sibling rather than as a stray underlined word among three buttons.
    """
    return tag("button", label, type="button", class_=cls,
               title=title or "Open the email this came from",
               data_mail=mail_url(email_id, reason, src))


def split(main, aside) -> Raw:
    """Two columns: the work on the left, what it is being done from on the right.

    `page()` stacks its sections vertically in one `<main>`, so anything meant to sit beside
    something else has to arrive as a single section with both halves already inside it.
    """
    return tag("div", tag("div", main, class_="split-main"), aside, class_="split")


def mail_pane(host_url: str, body, *, title: str = "The message", note: str = "",
              pane_id: str = "mail-pane") -> Raw:
    """A message rendered *into the page* beside a form, rather than in the popup over it.

    The popup is right for a table: you are scanning rows and want one of them full size for a
    moment. It is wrong for a form built out of the message, which is what Create a record is — you
    read a quantity off the proof and type it into a field the popup is covering.

    `body` is the fragment `routes._mail_fragment_html` returns, already rendered server-side: no
    flash on first paint, no round trip, and the message is readable even if the fetch path breaks.

    **`data-frag-host` is the whole mechanism**, and it carries a URL rather than being a bare
    marker. It says both "load fragments into me instead of the dialog" and "this is the message I
    am showing" — so `_JS` can send an attachment's viewer here, and send "‹ Back to message" back
    to exactly what was here first, without a layer stack or a second round of server plumbing.
    """
    head = tag("div",
               tag("h2", title, data_pane_title="1"),
               tag("p", note, class_="note") if note else Raw(""),
               class_="pane-head")
    return tag("aside", head,
               tag("div", Raw(body), class_="pane-body", id=pane_id, data_frag_host=host_url),
               class_="pane")


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

    `data-back` marks it as a return rather than a new destination. The dialog keeps a stack of the
    views opened over one another, and this button pops it instead of pushing — otherwise ten
    open-and-backs leave ten layers behind. The `data-frag` stays and is still used when there is
    nothing to pop: opened from `/ui/attachments` there is no message underneath to return to.

    `body` is `Raw` because `pipeline/attachment_view.py` did its own escaping — the same contract
    `mail_view.render` has, and the reason both return `str` rather than importing this module.
    """
    head = tag(
        "div",
        tag("button", "‹ Back to message", type="button", class_="btn ghost small",
            data_frag=mail_url, data_back="1"),
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
                data_frag=mail_url, data_back="1"),
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
        # Beside Mail because that is where these files came from. It is the only view of the whole
        # ledger — the mail dialog shows one message's attachments and cannot show that the same
        # bytes arrived twice under two names.
        ("/ui/attachments", "Attachments"),
        # Under Mail and pointedly not under "Needs action": nothing on this page is queued, and
        # filing it as work would contradict the only claim the page makes about itself.
        ("/ui/not-deliveries", "Not deliveries"),
    )),
    ("Needs action", (
        ("/ui/manual", "Needs a human"),
        # Beside the manual queue because it is the same kind of thing — a list somebody works
        # through — and not under Operations, which is where automated work lives. Nothing on this
        # page is automated: the purchase-order update it asks for is made in Spitfire by a person.
        ("/ui/cancellations", "Cancellations"),
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
    # A paperclip, which is what every mail client has used for this for thirty years.
    "/ui/attachments": _icon(
        '<path d="M10.5 6 6 10.5a2 2 0 0 0 2.8 2.8l5-5a3.5 3.5 0 0 0-5-5l-5 5a5 5 0 0 0 7 7L14 11"/>'),
    # The Mail envelope, struck through. Derived from it rather than given an icon of its own so the
    # relationship reads on the collapsed rail, where the icon is the whole label.
    "/ui/not-deliveries": _icon(
        '<rect x="1.5" y="3.5" width="13" height="9" rx="1.5"/><path d="m2 4.5 6 4 6-4"/>'
        '<path d="M2.5 13.5 13.5 2.5"/>'),
    # A flag, not an alert circle: this is a queue someone works through, not a fault to clear.
    "/ui/manual": _icon('<path d="M3.6 14.5V1.8M3.6 2.6h8.6l-1.7 2.9 1.7 2.9H3.6z"/>'),
    # A document with a line struck through it: an order that was placed and then withdrawn.
    "/ui/cancellations": _icon(
        '<path d="M3.5 1.5h6l3 3v10h-9z"/><path d="M9.5 1.5v3h3"/><path d="M5 10.5h6"/>'),
    "/ui/automation": _icon('<circle cx="8" cy="8" r="6.2"/><path d="m6.5 5.5 4 2.5-4 2.5z"/>'),
    "/ui/report": _icon('<path d="M3.5 1.5h6l3 3v10h-9z"/><path d="M9.5 1.5v3h3M5.5 8h5M5.5 11h3"/>'),
}

# Must not contain the sequence "</style>" — that is the one injection hole a constant CSS
# block can have, and it is why the CSS lives here as a constant rather than being composed.
_CSS = """
/* The rest of a conversation, offered with the decision that is about to be taken. */
.thread-offer { margin:14px 0 4px; padding:12px 14px; border:1px solid var(--line); border-radius:8px;
  background:#fbfaf7; }
.thread-head { margin:0 0 8px; font-weight:600; font-size:13px; }
.thread-list { list-style:none; margin:0; padding:0; max-height:280px; overflow-y:auto; }
.thread-row { padding:6px 0; border-top:1px solid var(--line); }
.thread-row:first-child { border-top:0; }
.thread-row label { display:flex; gap:9px; align-items:flex-start; cursor:pointer; }
.thread-row.held { opacity:.66; display:flex; gap:10px; justify-content:space-between; }
.thread-text { min-width:0; }
.thread-subject { display:block; font-size:13px; overflow-wrap:anywhere; }
.thread-meta, .thread-held { display:block; font-size:11.5px; color:var(--ink-soft); }
.toast-undo { font:inherit; font-size:12px; padding:2px 10px; margin-left:2px; cursor:pointer;
  border:1px solid #6d6a63; border-radius:999px; background:transparent; color:#fdfcf9; }
.toast-undo:hover { background:#3d3a35; }
/* A filter this page restored rather than one the reader just typed. It sits beside the count,
   because that readout is where someone looks when a table shows less than they expected. */
.place-clear { font:inherit; font-size:12px; margin-left:8px; padding:2px 8px; cursor:pointer;
  border:1px solid var(--line); border-radius:999px; background:#fff; color:var(--ink-soft); }
.place-clear:hover { border-color:var(--gold); color:var(--ink); }
/* What just happened, on the page you land on rather than the one you left. */
.toast { position:fixed; left:50%; bottom:24px; transform:translateX(-50%); z-index:60;
  max-width:min(560px, 92vw); padding:10px 16px; border-radius:8px; font-size:13px;
  background:#2c2a26; color:#fdfcf9; box-shadow:0 6px 20px rgba(0,0,0,.28); }
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
/* The gutter is reserved whether or not the page is currently scrollable, so hiding the page
   scrollbar under a popup (html.modal-open, below) cannot shift the layout sideways. */
html { scrollbar-gutter: stable; }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif; }

/* ---- the shell ---------------------------------------------------------- */
/* `stretch`, not `flex-start`: the content column has to be as tall as the shell before anything
   inside it can size itself against what is left. */
.shell { display:flex; align-items:stretch; min-height:100vh; }
/* min-width:0 is load-bearing: without it a 16-column table forces the flex item wider than the
   viewport and the whole page scrolls sideways instead of the table scrolling inside itself. */
.content { flex:1 1 auto; min-width:0; }

/* A page whose table sizes itself to the window is a column of fixed bands with one that gives.
   Only such a page changes shape, hence `:has()` — every other page stays a plain block flow.
   This replaced `height:calc(100vh - 400px)` on the pane, which could only ever be right for one
   window height: the 400px was measured against a 908px-tall window, so on a 642px laptop the
   table fell to its minimum and showed three and a half rows. Nothing here counts pixels. */
/* `> section:only-child` and not just `.scroll.pane`: a page whose table is the only thing on it
   can give that table the whole window, but a page with more sections underneath cannot. Needs a
   human has five — the queue, then what is awaiting a report, posted, blocked, and the line
   pointing at the mail set aside — and with the chain matching on the pane alone all five shared
   the viewport between them, which left the queue two rows tall and the headings below it
   overlapping its own pager. */
.shell:has(main > section:only-child .scroll.pane) { height:100vh; }
.content:has(main > section:only-child .scroll.pane) { display:flex; flex-direction:column;
    height:100vh; min-height:0; }
main:has(> section:only-child .scroll.pane) { flex:1 1 auto; min-height:0; display:flex;
    flex-direction:column; }
/* `min-height:0` on both, twice over: a flex item's floor is its content, so without it the table
   pushes the column taller than the viewport instead of scrolling inside itself. */
main:has(> section:only-child .scroll.pane) > section { flex:1 1 auto; min-height:0; display:flex;
    flex-direction:column; }

.side { flex:0 0 var(--rail); width:var(--rail); align-self:stretch; position:sticky; top:0;
    height:100vh; background:var(--nav); color:var(--nav-fg); display:flex; flex-direction:column; }
.side .brand { display:flex; align-items:center; gap:8px; padding:16px 14px 14px; }
.side .brand b { font-size:13px; font-weight:700; letter-spacing:.14em; text-transform:uppercase;
    color:#fff; line-height:1.1; }
/* Uppercase and widely tracked, so the two words read as one wordmark rather than as a title with
   a subtitle under it. */
.side .brand span { display:block; font-size:9.5px; letter-spacing:.19em; color:var(--nav-muted);
    font-weight:600; text-transform:uppercase; margin-top:2px; }
.rail-toggle { margin-left:auto; border:1px solid rgba(255,255,255,.13); background:transparent;
    color:var(--nav-muted); cursor:pointer; font-size:15px; line-height:1; padding:3px 8px;
    border-radius:7px; }
.rail-toggle:hover { background:var(--nav-2); color:#fff; border-color:rgba(255,255,255,.3); }

.nav { flex:1 1 auto; overflow-y:auto; padding:4px 0 14px; }
.nav-label { font-size:9.5px; letter-spacing:.11em; text-transform:uppercase; color:var(--nav-muted);
    padding:14px 16px 5px; font-weight:700; }
.nav a { display:flex; align-items:center; gap:10px; padding:8px 16px; color:var(--nav-fg);
    text-decoration:none; font-size:13px; border-left:3px solid transparent; }
.nav a:hover { background:var(--nav-2); color:#fff; }
.nav a.on { background:var(--nav-2); color:var(--nav-on); border-left-color:var(--gold);
    font-weight:600; }
/* The clicked entry, lit by the script the instant it is pressed rather than when the next document
   arrives. These pages are server-rendered, so a click leaves the OLD page fully painted with the
   OLD entry still highlighted for the whole render — which reads as a click that did not register,
   and is what made people press twice. `.going` is a separate class from `.on` on purpose: `.on` is
   the server's statement of where you are, and `test_the_active_nav_entry_is_marked_on_every_page`
   counts that string across the document. This one is only ever added at runtime. */
.nav a.going { background:var(--nav-2); color:var(--nav-on); border-left-color:var(--gold);
    font-weight:600; }
/* And the entry being LEFT gives the highlight up while that is happening. Without this both are
   lit identically mid-navigation — the page you are on and the page you are going to — which says
   "one of these two" rather than "this one", and is barely better than lighting neither. */
html.nav-busy .nav a.on:not(.going) { background:transparent; border-left-color:transparent;
    color:var(--nav-fg); font-weight:400; }
html.nav-busy .nav a.on:not(.going) .ico { opacity:.85; }
.nav a .ico { flex:0 0 16px; opacity:.85; }
.nav a.on .ico, .nav a.going .ico { opacity:1; }
/* Rides the top edge of the whole window, not the content column, so it is visible wherever the eye
   happens to be. Indeterminate by necessity: a server render has no progress to report, and a bar
   that pretended otherwise would be inventing one. */
.load-bar { position:fixed; top:0; left:0; right:0; height:2px; z-index:60; background:transparent;
    overflow:hidden; pointer-events:none; }
/* Explicit, for the same reason `.live-pill[hidden]` is: this rule sets other properties on the
   element, and relying on the user agent's `[hidden]` alone is one added `display` away from a bar
   that never turns off. */
.load-bar[hidden] { display:none; }
.load-bar::after { content:""; position:absolute; top:0; left:0; height:100%; width:40%;
    background:var(--gold); animation:load-slide 1.1s ease-in-out infinite; }
@keyframes load-slide {
    0%   { transform:translateX(-100%); }
    100% { transform:translateX(350%); }
}
/* Someone who has asked for less motion still needs to know the click landed; the lit nav entry
   already says that, so the bar simply holds still rather than disappearing. */
@media (prefers-reduced-motion: reduce) {
    .load-bar::after { animation:none; width:100%; opacity:.55; }
}
.nav a .lbl { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.nav a .count { margin-left:auto; background:var(--gold); color:#3A3014; font-size:10.5px;
    font-weight:700; border-radius:20px; padding:0 6px; min-width:18px; text-align:center; }
/* The queue, said in words at the top of the rail. Gold is still chrome: it edges and badges this
   card, it does not fill it — a filled gold panel would outshout every status colour in the app. */
.side-alert { display:flex; align-items:center; gap:9px; margin:2px 12px 6px; padding:8px 10px;
    border-radius:8px; background:var(--nav-2); border:1px solid rgba(217,165,33,.4);
    color:var(--nav-fg); text-decoration:none; }
.side-alert:hover { border-color:var(--gold); }
.side-alert-ico { flex:0 0 18px; height:18px; border-radius:50%; background:var(--gold);
    color:#3A3014; font-size:12px; font-weight:800; line-height:18px; text-align:center; }
.side-alert .lbl { min-width:0; line-height:1.25; }
.side-alert .lbl b { display:block; font-size:12.5px; font-weight:600; white-space:nowrap; }
.side-alert .lbl span { display:block; font-size:11px; color:var(--nav-muted); }
.side-alert .count { margin-left:auto; background:var(--gold); color:#3A3014; font-size:10.5px;
    font-weight:700; border-radius:20px; padding:0 6px; min-width:18px; text-align:center; }
.side-foot .signed-in { display:flex; flex-direction:column; gap:3px; margin-top:9px; }
.side-foot .signed-in .who { font-size:10.5px; color:var(--nav-muted); overflow:hidden;
  text-overflow:ellipsis; white-space:nowrap; }
.side-foot .signed-in .sign-out { background:none; border:0; padding:0; font:inherit;
  font-size:10.5px; color:var(--nav-muted); text-align:left; cursor:pointer;
  text-decoration:underline; }
.side-foot .signed-in .sign-out:hover { color:var(--nav-on); }
/* Collapsed rail: the name goes, the way out stays. */
.rail .side-foot .signed-in .who { display:none; }
.server-search { display:flex; align-items:center; gap:6px; }
.server-pager { display:flex; align-items:center; gap:14px; flex-wrap:wrap; }
.server-pager .pager-num, .server-pager .size { padding:2px 7px; border-radius:5px;
  text-decoration:none; color:var(--muted); }
.server-pager .pager-num:hover, .server-pager .size:hover { background:var(--zebra);
  color:var(--fg); }
.server-pager .pager-num.on, .server-pager .size.on { background:var(--accent); color:#fff; }
.server-pager .pager-size-links { margin-left:auto; color:var(--muted); font-size:.78rem; }
.sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden;
  clip:rect(0 0 0 0); white-space:nowrap; border:0; }
.side-foot { border-top:1px solid rgba(255,255,255,.11); padding:10px 12px; }
.side-foot .btn { width:100%; justify-content:center; }
.side-foot .kill-note { display:block; font-size:11px; color:var(--nav-muted); margin-top:7px;
    text-align:center; }
/* When the pipeline last ran, in the space the rail already had. Quiet on purpose: it is standing
   status, not something anyone came here to read. */
.side-foot .last-run { display:block; font-size:10.5px; color:var(--nav-muted); margin-top:9px;
    text-align:center; }
.side-foot .powered { display:block; font-size:9.5px; letter-spacing:.08em; text-transform:uppercase;
    color:var(--nav-muted); margin-top:10px; text-align:center; opacity:.75; }

/* Collapsed: the rail keeps the icons and drops everything that needs width. `title` on each link
   is what carries the label at this size — see `page()`.

   The class sits on <html>, not <body>, so the script in <head> can set it before the sidebar is
   parsed and a collapsed rail never flashes open. `--rail` is inherited from here either way. */
.rail { --rail:58px; }
.rail .side .brand b, .rail .nav-label, .rail .nav a .lbl, .rail .side-foot .lbl,
.rail .nav a .count, .rail .side-foot .kill-note, .rail .side-foot .powered,
.rail .side-foot .last-run,
.rail .side-alert .lbl, .rail .side-alert .count { display:none; }
/* The card keeps only its glyph at 58px; its `title` carries the count that the badge did. */
.rail .side-alert { margin:2px 8px 6px; padding:8px 0; justify-content:center; }
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
/* No rule under the header and no surface of its own. The title, the figures and the table below
   them are one column of related things; a border across the top third cut the page in half at the
   exact point where nothing changes. */
header { background:transparent; }
/* Every value in this band was cut on 2026-08-24, and the reason is the same one everywhere: the
   chrome above and below the table came to 394px, which on a 642px laptop left room for three and
   a half rows of a page whose whole purpose is rows. Nothing was removed — it is the same title,
   subtitle and figures, closer together. */
.bar { display:flex; align-items:center; gap:18px; padding:10px 26px 0; }
.bar-text { min-width:0; }
.bar h1 { font-size:19px; margin:0; font-weight:660; letter-spacing:-.02em; }
.bar .sub { margin:2px 0 0; font-size:12.5px; color:var(--muted); }
/* `margin-left:auto` rather than a spacer: the title block keeps its natural width, so a long
   subtitle wraps under the title instead of shoving the controls off the right edge. */
.bar-actions { margin-left:auto; display:flex; align-items:center; gap:9px; flex:none; }
/* The one filled button in the app. It is the page's own verb — "Check now" on Mail — and it is
   dark rather than gold because gold is chrome here and a gold button would read as furniture. */
.btn.primary { background:var(--nav-2); border-color:var(--nav-2); color:#fff; font-weight:560; }
.btn.primary:hover { background:var(--nav); border-color:var(--nav); color:#fff; }
/* The way out of a page you went *into*. `flex:none` so it keeps its size when the title beside it
   is long, and `margin-right` rather than relying on `.bar`'s gap alone, so it reads as attached
   to the title it precedes rather than as one more item in the row. */
.back-link { flex:none; display:inline-flex; align-items:center; margin-right:-6px;
    font-size:12.5px; font-weight:600; color:var(--muted); text-decoration:none;
    padding:5px 11px 5px 9px; border:1px solid var(--line); border-radius:7px;
    background:var(--surface); white-space:nowrap; }
.back-link:hover { color:var(--fg); border-color:var(--gold); background:#FBF7EC; }
.back-link:focus-visible { outline:2px solid var(--gold); outline-offset:2px; }
form.run { margin-left:auto; }
button, .btn { font:inherit; display:inline-flex; align-items:center; gap:7px; padding:6px 13px;
    border:1px solid var(--line); border-radius:7px; background:var(--surface); color:var(--fg);
    cursor:pointer; text-decoration:none; }
button:hover, .btn:hover { border-color:var(--gold-deep); color:var(--gold-deep); }
.btn.ghost { background:transparent; }
.btn.small { padding:4px 10px; font-size:12px; }
/* A control that is present, explains itself, and does nothing. `aria-disabled` rather than
   the `disabled` attribute on purpose: a disabled button is not focusable and browsers
   suppress its tooltip, and on the Records page the tooltip is the whole message — it says
   which receipt already exists. The `:hover` rule is needed because the shared
   `button:hover` above would otherwise light it gold as though it were live. */
.btn[aria-disabled="true"] { opacity:.45; cursor:not-allowed; }
.btn[aria-disabled="true"]:hover { border-color:var(--line); color:inherit; }
/* An unbulleted, unindented list for prose rows — the edit form's correction history. */
.plain-list { list-style:none; margin:0; padding:0; }
.plain-list li { padding:3px 0; border-bottom:1px solid var(--line); font-size:12.5px; }
.plain-list li:last-child { border-bottom:0; }
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

/* One card per figure. Eight numbers in a bare row ran into each other's labels — the eye could
   not tell whether "6" belonged to the word above it or the one below. */
.stats { display:flex; flex-wrap:wrap; gap:8px; padding:8px 26px 0; background:transparent; }
.stat { flex:0 0 auto; min-width:70px; padding:5px 13px; background:var(--surface);
    border:1px solid var(--line); border-radius:10px; }
.stat b { display:block; font-size:17px; font-weight:660; line-height:1.2;
    font-variant-numeric:tabular-nums; }
.stat span { display:block; color:var(--muted); font-size:10.5px; margin-top:1px; }
/* The verdict figures borrow the badge ramp below, so "6 surface" and a `surface` pill in the
   table are the same green rather than two greens that nearly match. */
.stat-good b { color:#1d6b4f; }
.stat-warn b { color:#8a6216; }
/* The one figure that means a person has to do something is the one that fills its card. */
.stat-alert { background:#FDF6E6; border-color:#E6D2A0; }
.stat-alert b { color:#8a6216; }
.stat-alert span { color:#8a6216; opacity:.85; }
.statrow { display:flex; flex-wrap:wrap; gap:30px; margin:12px 0 0; }
.stopped-banner { background:#fdf0ec; border-bottom:1px solid #eec2b7; color:#7d2b1c;
    padding:11px 26px; font-size:13px; }
main { padding:10px 26px 10px; }
section { margin-bottom:32px; }
/* The gap belongs *between* sections. On the last one it is 32px of scrollbar on a page whose
   table sizes itself to the viewport precisely so that nothing scrolls. */
main > section:last-child { margin-bottom:0; }
.card { background:var(--surface); border:1px solid var(--line); border-radius:11px;
    padding:16px 18px 18px; border-left:3px solid var(--line); }
.card.good { border-left-color:#1d6b4f; }
.card.warn { border-left-color:var(--gold); }
.card.bad  { border-left-color:var(--danger); }
h2 { font-size:14px; margin:0 0 4px; font-weight:660; }
.note, .empty { color:var(--muted); margin:2px 0 8px; }
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
/* A pane has a height of its own, so the page behind it never scrolls and the pager stays where
   someone left it instead of dropping fifty rows below the fold. The heading row sticks, because a
   grid whose column names have scrolled away is a grid of unlabelled numbers. `height` and not
   `max-height`: the box keeps its shape on a page holding six rows, so switching pages does not
   move every control under it. */
/* Takes the height the bands above and below it did not want — see the `:has()` chain up in the
   shell. A browser without `:has()` gets no flex parent, so this falls back to a table at its
   natural height and a page that scrolls: the behaviour from before the pane existed, not a
   broken layout. */
/* On a page with sections below it the pane cannot fill the window, so it takes a bounded share
   of it and the page scrolls past — still a box with its own scrollbars, still a pager that does
   not sit fifty rows down. The single-section pages override the cap and flex instead. */
.scroll.pane { overflow:auto; min-height:150px; max-height:62vh; }
main:has(> section:only-child .scroll.pane) .scroll.pane { flex:1 1 0; max-height:none; }
.scroll.pane thead th { position:sticky; top:0; z-index:2; }
/* One row, one line. A pane already scrolls sideways, so a cell holding a button and a badge — or
   a purchase order and the envelope beside it — should claim the width it needs rather than wrap
   into a third line and make every *other* row on the page that tall. The free-text columns are
   clipped before they get here (`_clipped`), so nothing is hidden by this that was not already
   hidden by the column width. */
.scroll.pane td { white-space:nowrap; vertical-align:middle; }
/* The scrollbars belong to the box, so they are drawn as part of it rather than as the OS's own
   slab across the bottom of a white panel. */
.scroll.pane::-webkit-scrollbar { width:11px; height:11px; }
.scroll.pane::-webkit-scrollbar-track { background:transparent; }
.scroll.pane::-webkit-scrollbar-thumb { background:#DCD6C7; border-radius:999px;
    border:3px solid var(--surface); }
.scroll.pane::-webkit-scrollbar-thumb:hover { background:#C6BFAB; }
.scroll.pane::-webkit-scrollbar-corner { background:transparent; }
/* Identifiers — timestamps, rule names, spec codes — set in the one face where a column of them
   lines up character for character. Prose in the next column stays in the text face. */
.mono { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
    font-size:12.2px; }
/* Subject over sender, in one cell. They were two columns, and the sender column was three times
   the width it needed because one long address set it — while the subject, the thing anyone
   actually scans for, was squeezed next to it. */
.cell-subject { line-height:1.35; }
.cell-subject .subj { color:var(--nav); font-weight:560; }
/* A filename in this cell is a control, but it is first of all the row's name. The underline every
   other `.link-btn` carries turned a column of filenames into a column of rules; it arrives on
   hover, where it says the thing is clickable at the moment that matters. */
.cell-subject .link-btn.subj { font-size:13px; text-decoration:none; }
.cell-subject .link-btn.subj:hover { text-decoration:underline; text-decoration-color:var(--gold);
    text-underline-offset:3px; }
.cell-subject .from { display:block; color:var(--muted); font-size:12px; margin-top:2px; }
table { border-collapse:collapse; width:100%; font-size:13px; }
th { text-align:left; font-weight:660; color:var(--fg); font-size:11px; text-transform:uppercase;
     letter-spacing:.05em; padding:11px 14px; border-bottom:1px solid var(--line); white-space:nowrap; }
.scroll th { background:var(--head); border-bottom:2px solid var(--gold); }
td { padding:11px 14px; border-bottom:1px solid #F2EFE7; vertical-align:top; }
tr:last-child td { border-bottom:0; }
.scroll tbody tr:nth-child(even) { background:var(--zebra); }
.scroll tbody tr:hover { background:#F6F1E4; }
td.num, .scroll td.num { text-align:right; font-variant-numeric:tabular-nums; }
/* The heading goes with its column. A right-aligned column under a left-aligned heading reads as
   two columns that happen to overlap. The sort button fills the cell, so it has to turn too. */
.scroll th.num, th.num { text-align:right; }
th.num .sort-btn { justify-content:flex-end; }
/* ---- row selection ------------------------------------------------------ */
/* The column is chrome, not data: it takes the least width that still gives the tick a comfortable
   target, and it never widens because a heading is long. */
th.pick, td.pick { width:34px; padding-left:14px; padding-right:0; }
td.pick { vertical-align:middle; }
input.pick, input.pick-all { width:15px; height:15px; margin:0; cursor:pointer;
    accent-color:var(--nav-2); }
input.pick:focus-visible, input.pick-all:focus-visible { outline:2px solid var(--gold);
    outline-offset:2px; }
/* A ticked row says so without a colour of its own: the tint is the hover tint, held. */
.scroll tbody tr:has(input.pick:checked) { background:#F6F1E4; }

/* A value that is also a way in. Underlining every one of them turned a column of purchase-order
   numbers into a column of blue rules; the colour says it is a link and the underline arrives when
   the pointer does. */
.scroll tbody a { color:var(--accent); text-decoration:none; }
.scroll tbody a:hover { color:var(--gold-deep); text-decoration:underline; text-underline-offset:2px; }
.nw { white-space:nowrap; }
.muted { color:var(--muted); }

/* ---- table filter ------------------------------------------------------- */
/* Gold stays chrome: the focus ring is the only gold here, matching the sidebar and the rule
   under a table head. The input is not styled as a search "pill" — it is a field above a grid. */
/* One row for every control that narrows the table below it. Each of them still owns its own
   margin when used alone on a page that has not been through this pass yet, so the reset here is
   scoped rather than applied to the components themselves. */
/* The row reads left to right as "narrow this — by text, then by date, then to a saved view".
   The search box no longer takes the slack: a 790px field for a PO number is a field that looks
   like it wants a sentence, and the width was doing nothing but separating the two things at
   either end of the row. It keeps a third of that; the slack becomes the gap, and the date and
   view controls hold the right edge. */
.controls { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin:0 0 8px; }
.controls .filter-bar, .controls .date-bar { margin:0; }
.controls .filter-bar { flex:0 1 auto; min-width:0; }
.controls .search { flex:0 0 270px; max-width:100%; }
.controls .date-bar { margin-left:auto; }
/* Says "these rows are one delivery" on the first row of each block. Quiet by design: it is a
   fact about the grouping, not a status, and the status badges beside it have to keep meaning
   more than it does. */
.delivery-tag { display:inline-block; margin-left:7px; padding:0 6px; border-radius:20px;
    background:var(--head); border:1px solid var(--line); color:var(--muted);
    font-size:10.5px; font-weight:600; white-space:nowrap; vertical-align:1px; }
.filter-bar { display:flex; align-items:center; gap:12px; margin:0 0 10px; }
.search { position:relative; display:flex; align-items:center; flex:1 1 auto; min-width:0; }
.search-ico { position:absolute; left:11px; color:var(--muted); pointer-events:none; }
input.filter { font:inherit; font-size:13px; padding:8px 11px 8px 33px; width:100%;
    border:1px solid var(--line); border-radius:9px; background:var(--surface); color:var(--fg); }
input.filter::placeholder { color:#9a9689; }
input.filter:focus { outline:2px solid var(--gold); outline-offset:1px; border-color:var(--gold); }
input.filter::-webkit-search-cancel-button { cursor:pointer; }
.filter-count { color:var(--muted); font-size:12px; font-variant-numeric:tabular-nums;
    white-space:nowrap; }
/* Present for a screen reader, absent from the layout. Used where the control's own value already
   says what it is and a label in front of it would be a word of chrome. */
.vh { position:absolute; width:1px; height:1px; margin:-1px; padding:0; overflow:hidden;
    clip:rect(0 0 0 0); clip-path:inset(50%); white-space:nowrap; border:0; }
tr.filtered-out, tr.paged-out { display:none; }
/* Two classes, not one. A row can be off-screen because it did not match the search or because it
   is on another page, and collapsing them would make each pass clobber the other's decision. */

/* ---- sortable headings --------------------------------------------------- */
/* A real button inside the th, not a click handler on the th: a cell is not focusable, so a
   header nobody can reach by keyboard is a column nobody can sort without a mouse. */
th:has(.sort-btn) { padding:0; }
.sort-btn { font:inherit; font-size:11px; font-weight:660; text-transform:uppercase;
    letter-spacing:.05em; color:var(--fg); background:transparent; border:0; cursor:pointer;
    padding:11px 14px; width:100%; text-align:left; display:flex; align-items:center; gap:5px;
    white-space:nowrap; }
.sort-btn:hover { background:#F4EDDC; }
.sort-btn:focus-visible { outline:2px solid var(--gold); outline-offset:-2px; }
/* Reserved whether or not this column is the sorted one, so the heading row does not shift
   sideways as you click between columns. */
/* Reserved but invisible until it is wanted. Twelve permanent double-arrows across a heading row
   read as decoration, and they were the loudest thing in a table whose content is the point; on
   hover — or once a column is actually sorted — the glyph is there. */
.sort-arrow { width:8px; display:inline-block; color:var(--muted); }
.sort-arrow::before { content:"\\2195"; opacity:0; }
th:hover .sort-arrow::before, .sort-btn:focus-visible .sort-arrow::before { opacity:.45; }
th[aria-sort="ascending"]  .sort-arrow::before { content:"\\2191"; opacity:1; color:var(--gold); }
th[aria-sort="descending"] .sort-arrow::before { content:"\\2193"; opacity:1; color:var(--gold); }
th[aria-sort="ascending"], th[aria-sort="descending"] { background:#F4EDDC; }

/* ---- date range --------------------------------------------------------- */
.date-bar { position:relative; display:flex; align-items:center; gap:8px; margin:0 0 10px;
    font-size:12.5px; color:var(--muted); flex-wrap:wrap; }
/* One segmented control, so the four read as a single choice rather than four buttons that happen
   to sit together. The pressed one is `.sel`, never `.on` — see `_date_presets`. */
/* No `overflow:hidden`: the exact-range panel is absolutely positioned inside this group and would
   be clipped to a 33px sliver by it. The ends are rounded by hand instead, which is what the
   overflow was buying. */
.chips { display:inline-flex; border:1px solid var(--line); border-radius:9px;
    background:var(--surface); margin-right:0; }
.chips > :first-child { border-top-left-radius:8px; border-bottom-left-radius:8px; }
.chips > :last-child, .chips > :last-child > summary { border-top-right-radius:8px;
    border-bottom-right-radius:8px; }
.chips .chip { border:0; border-radius:0; background:transparent; color:var(--muted);
    font-size:12.5px; font-weight:560; padding:7px 15px; }
.chips .chip + .chip, .chips .date-more > summary { border-left:1px solid var(--line); }
.chips .chip:hover { background:var(--head); color:var(--fg); }
.chips .chip.sel { background:var(--nav-2); color:#fff; }
.chips .chip.sel:hover { background:var(--nav); color:#fff; }
.chips .chip:focus-visible { outline:2px solid var(--gold); outline-offset:-2px; }
/* The wrapping variant, for preset chips on a form. The date group is four short segments welded
   into one pill and must never wrap; these are whole phrases, there are seven of them, and on a
   narrow window they have to fall onto a second line rather than push the form sideways. So the
   group loses its own border and each chip carries one. */
.chips-wrap { display:flex; flex-wrap:wrap; gap:6px; border:0; background:transparent; }
.chips-wrap .chip { border:1px solid var(--line); border-radius:8px; background:var(--surface);
    padding:5px 11px; cursor:pointer; }
.chips-wrap .chip + .chip { border-left:1px solid var(--line); }
.presets { margin:0 0 14px; }
.presets .preset-label { display:block; font-size:12px; color:var(--muted); margin:0 0 5px; }
/* The exact range, folded away behind one square. Open, it is a panel over the table rather than a
   row that pushes the table down — the point of putting it away was to stop it taking that space.
   `list-style:none` twice: WebKit uses a marker pseudo-element the standard property misses. */
/* A fifth segment of the group, not a button standing next to it: "Today", "7d", "30d", "All" and
   "Custom" are five answers to one question. `list-style:none` twice — WebKit uses a marker
   pseudo-element the standard property misses. */
.date-more { position:relative; display:flex; }
.date-more > summary { display:inline-flex; align-items:center; gap:6px; padding:7px 13px;
    border:0; border-radius:0; background:transparent; color:var(--muted); font-size:12.5px;
    font-weight:560; cursor:pointer; list-style:none; }
.date-more > summary::-webkit-details-marker { display:none; }
.date-more > summary:hover { background:var(--head); color:var(--fg); }
.date-more > summary:focus-visible { outline:2px solid var(--gold); outline-offset:-2px; }
/* Open is not the same as active: the panel being on screen is a lighter state than a range
   actually narrowing the table, which is `.sel` below and comes from `markChips`. */
.date-more[open] > summary { background:var(--head); color:var(--fg); }
.date-more > summary.sel { background:var(--nav-2); color:#fff; }
.date-more > summary.sel:hover { background:var(--nav); color:#fff; }
.date-custom { position:absolute; z-index:20; top:calc(100% + 6px); right:0; display:flex;
    align-items:center; gap:7px; padding:10px 12px; background:var(--surface);
    border:1px solid var(--line); border-radius:10px; box-shadow:0 8px 24px rgba(60,50,20,.14);
    white-space:nowrap; }
/* ---- reason chips -------------------------------------------------------- */
/* Its own row under the controls, because it is a readout as much as a control: the counts say
   where the work is before anyone presses anything. Separate pills rather than a segmented group —
   these are not one choice among four, they are four independent facts about the queue. */
.reason-bar { display:flex; align-items:center; gap:7px; flex-wrap:wrap; margin:0 0 10px;
    font-size:12.5px; }
.reason-label { color:var(--muted); margin-right:1px; }
.pill { border:1px solid var(--line); border-radius:999px; background:var(--surface);
    color:var(--fg); font-size:12px; font-weight:560; padding:4px 11px; gap:6px; }
.pill:hover { border-color:var(--gold-deep); color:var(--gold-deep); }
.pill .pill-n { color:var(--muted); font-variant-numeric:tabular-nums; }
.pill.sel { background:var(--nav-2); border-color:var(--nav-2); color:#fff; }
.pill.sel:hover { background:var(--nav); border-color:var(--nav); color:#fff; }
.pill.sel .pill-n { color:rgba(255,255,255,.72); }
/* Shown, not hidden: "Unreadable · 0" says that class of failure is not happening, which a chip
   that is simply absent cannot say. */
.pill.off, .pill.off:hover { color:var(--muted); border-color:var(--line); opacity:.55;
    cursor:default; }
.pill:focus-visible { outline:2px solid var(--gold); outline-offset:2px; }

/* ---- one-column dropdown filter ----------------------------------------- */
.choice-bar { display:inline-flex; align-items:center; gap:7px; font-size:12.5px;
    color:var(--muted); }
.choice-label { white-space:nowrap; }
select.choice-filter { font:inherit; font-size:12.5px; padding:6px 9px; border:1px solid var(--line);
    border-radius:7px; background:var(--surface); color:var(--fg); }
select.choice-filter:focus { outline:2px solid var(--gold); outline-offset:1px; }
/* Boxed: the glyph and the chosen option are one control. The select loses its own border and
   ground so the box around both is the only edge, and the star is the thing that says this is a
   view of the table rather than one more field. */
.choice-bar.boxed { gap:6px; height:33px; padding:0 10px 0 11px; border:1px solid var(--line);
    border-radius:9px; background:var(--surface); color:var(--fg); cursor:pointer; }
.choice-bar.boxed:hover { border-color:var(--gold-deep); }
.choice-bar.boxed .choice-ico { color:var(--gold); flex:none; }
.choice-bar.boxed select.choice-filter { border:0; background:transparent; padding:0;
    font-size:12.5px; font-weight:560; color:var(--fg); cursor:pointer; max-width:170px; }
.choice-bar.boxed:focus-within { outline:2px solid var(--gold); outline-offset:1px; }
.choice-bar.boxed select.choice-filter:focus { outline:none; }
.date-label { display:inline; margin:0; font-size:12.5px; }
input.date-from, input.date-to { font:inherit; font-size:12.5px; padding:5px 8px;
    border:1px solid var(--line); border-radius:7px; background:var(--surface); color:var(--fg); }
input.date-from:focus, input.date-to:focus { outline:2px solid var(--gold); outline-offset:1px; }

/* ---- pager -------------------------------------------------------------- */
/* Three columns, not a row of five things: what is on screen sits at the left edge, the page
   buttons stay centred under the table whatever that sentence says, and the size control keeps the
   right edge. A flex row put the centre wherever the left-hand text happened to end. */
.pager { display:grid; grid-template-columns:1fr auto 1fr; align-items:center; gap:10px;
    margin:8px 0 0; font-size:12.5px; color:var(--muted); }
.pager-showing { font-variant-numeric:tabular-nums; }
.pager-showing b { color:var(--fg); font-weight:660; }
.pager-pages { display:flex; align-items:center; gap:5px; }
.pager-nums { display:flex; align-items:center; gap:5px; }
.pager .page-btn { justify-content:center; min-width:28px; height:28px; padding:0 8px;
    font-size:12.5px; font-weight:560; border-radius:8px; color:var(--fg);
    font-variant-numeric:tabular-nums; }
.pager .page-btn.on { background:var(--nav-2); border-color:var(--nav-2); color:#fff; }
.pager .page-btn.on:hover { background:var(--nav); border-color:var(--nav); color:#fff; }
.pager-gap { color:var(--muted); padding:0 2px; user-select:none; }
.pager-rows { display:flex; align-items:center; gap:7px; justify-self:end; }
.pager-size-label { display:inline; margin:0; }
.pager-size { font:inherit; font-size:12.5px; padding:5px 7px; border:1px solid var(--line);
    border-radius:8px; background:var(--surface); color:var(--fg); }
.pager button[disabled] { opacity:.4; cursor:default; }
.pager button[disabled]:hover { background:transparent; color:inherit; border-color:var(--line); }
@media (max-width: 760px) {
  .pager { grid-template-columns:1fr; justify-items:center; }
  .pager-rows { justify-self:center; }
}
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
/* Read out of Junk Email. Loud on purpose, and not the quiet grey `sample` gets: a delivery
   notification Exchange filed as spam is not scaffolding, it is a message that would have been
   lost outright before the reader looked in that folder. The colour is what makes "Premier's
   tenant is junking warehouse mail" visible on the page instead of a fact somebody has to go and
   query Graph to discover — which is exactly how it was found the first time. */
.badge-junk   { color:#8a5a00; border-color:#e8d09a; background:#fdf6e6; }
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

tr.clickable { cursor:pointer; }
.scroll tbody tr.clickable:hover { background:#F4EDDC; }
tr.clickable:focus { outline:2px solid var(--gold-deep); outline-offset:-2px; }

/* One scroll container, not four. The dialog is a flex column that clips: the head is a fixed
   row and the body takes what is left and scrolls. It used to be `overflow:auto` from the UA
   sheet on the dialog, `position:sticky` on the head, and `max-height:calc(94vh - 42px)` on the
   body — arithmetic that hard-coded the head's height, so a head that wrapped on a narrow window
   turned the dialog itself into a second scroller and pushed the sticky head out of view. */
dialog.modal { border:none; border-radius:12px; padding:0; width:min(1200px,96vw); max-height:94vh;
    overflow:hidden; box-shadow:0 24px 60px rgba(20,30,25,.26); color:var(--fg); }
/* `[open]` is load-bearing, not decoration. `display` on a bare `dialog.modal` outranks the UA
   sheet's `dialog:not([open]) { display:none }`, so a CLOSED dialog stays laid out across the
   page and silently eats every click underneath it — the popup opens once and the page behind is
   dead from then on. Anything that sets `display` here must stay scoped to the open state. */
dialog.modal[open] { display:flex; flex-direction:column; }
dialog.modal::backdrop { background:rgba(20,30,25,.45); }
/* `showModal()` does not scroll-lock the document — the page behind kept its own scrollbar and a
   wheel gesture over the backdrop scrolled the table underneath. openDialog adds this class. */
html.modal-open { overflow:hidden; }
.modal-head { display:flex; justify-content:space-between; align-items:center; gap:14px;
    flex:0 0 auto; padding:8px 10px 8px 16px; border-bottom:1px solid var(--line);
    background:#fff; }
.modal-head h2 { margin:0; font-size:11px; font-weight:600; color:var(--muted);
    letter-spacing:.05em; text-transform:uppercase; }
.close-x { border:none; background:transparent; color:var(--muted); font-size:22px; line-height:1;
    cursor:pointer; padding:4px 10px; border-radius:7px; }
.close-x:hover { background:#f1f0ec; color:var(--fg); }
/* min-height:0 is load-bearing: a flex item defaults to min-content height and would refuse to
   shrink below a long message, pushing the dialog past its own max-height. */
.modal-body { padding:14px 18px 22px; flex:1 1 auto; min-height:0; overflow:auto; font-size:13px; }

.mail-head { border-bottom:1px solid var(--line); padding-bottom:9px; margin-bottom:11px; }
.mail-head h3 { margin:0 0 2px; font-size:15px; line-height:1.35; }
.mail-head .note { margin:0; font-size:12px; }
.mail-head .why { margin:8px 0 0; background:#fdf6e6; color:#7a5000; border-radius:7px;
    padding:7px 10px; font-size:12.5px; }
/* The message renders in its own frame at the sender's own sizing; the chrome above must not leak
   into it. The frame is sandboxed without allow-scripts — see pipeline/mail_view.py.
   The height here is the pre-measure fallback ONLY: fitFrames() grows the frame to its content so
   it never scrolls internally and .modal-body stays the single scroller. It applies when the
   measurement is unavailable (frame not yet parsed and no load event, or a cross-origin throw). */
iframe.mail-body { display:block; width:100%; height:min(64vh,760px); border:1px solid var(--line);
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
/* The PDF keeps a fixed height: the browser's PDF plugin owns its own scroller and paging, and
   there is no content height to measure from out here. */
.view-pdf { display:block; width:100%; height:min(72vh,820px); border:1px solid var(--line);
    border-radius:8px; background:#fff; }
/* The HTML preview is ours and measurable, so it grows like iframe.mail-body — same fallback
   rule: this height applies only until fitFrames() measures it. */
.view-frame { display:block; width:100%; height:min(72vh,820px); border:1px solid var(--line);
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
/* Text cells — spec, description, vendor. Descriptions run to a thousand characters and carry
   model numbers with no spaces in them, so they wrap on any character rather than pushing the
   dialog sideways. Slightly smaller than the numeric rows because they are prose, not figures. */
.vq .t { font-size:12.5px; line-height:1.45; overflow-wrap:anywhere; }
.vq .same { background:#eef8f2; }
.vq .diff { background:#fdf0ec; }
/* A Records-table cell that disagrees with Spitfire. Same red as `.vq .diff` above, so the flag
   means the same thing on the table as it does in the Verify popup. The reason is the `title`. */
.mm { background:#fdf0ec; color:#8c1220; padding:1px 4px; border-radius:3px; font-weight:600; }
/* The alternative-lines chooser, collapsed by default: it is there for the reader who doubts the
   match, and open by default it would bury the comparison it exists to question. */
.valt { margin:2px 0 10px; }
.valt > summary { cursor:pointer; font-size:12.5px; color:var(--muted); padding:2px 0; }
.valt > summary:hover { color:var(--fg); }
.valt table { margin-top:8px; }
.vfindings { margin:0 0 6px; padding-left:18px; font-size:13px; line-height:1.55; }
.vfindings li { margin:3px 0; }
.vrec { border-top:1px solid var(--line); padding-top:14px; margin-top:18px; }
.vrec:first-child { border-top:none; padding-top:0; margin-top:0; }
.vrec h3 { margin:0 0 3px; font-size:14.5px; }

/* ---- form beside its source ---------------------------------------------- */
/* Create a record asks for five facts that exist only in one email and its proof of delivery, and
   used to show neither: the message opened in the popup, on top of the fields it was meant to
   fill. The form keeps the left column; the message gets the right one and stays pinned, because
   the last two questions on the form (which file is the proof, and your name) are below the fold
   and the delivery date is usually on the proof. */
.split { display:flex; align-items:flex-start; gap:24px; }
/* min-width:0 on both, for the reason `.content` gives: a wide attachment preview or a long
   unbroken subject would otherwise force the column past the viewport instead of scrolling. */
.split-main { flex:1 1 640px; min-width:0; }
/* Same flex-column-that-clips shape as dialog.modal, and for the same reason — the head stays put
   and .pane-body is the single scroller. min-height:0 is load-bearing here too. */
.pane { flex:1 1 560px; min-width:0; position:sticky; top:22px; max-height:calc(100vh - 44px);
    display:flex; flex-direction:column; background:var(--surface); border:1px solid var(--line);
    border-radius:11px; overflow:hidden; }
.pane-head { flex:0 0 auto; padding:12px 16px; border-bottom:1px solid var(--line); }
.pane-head h2 { margin:0; font-size:11px; font-weight:600; color:var(--muted);
    letter-spacing:.05em; text-transform:uppercase; }
.pane-head .note { margin:5px 0 0; font-size:12.5px; }
.pane-body { flex:1 1 auto; min-height:0; overflow:auto; padding:14px 16px 18px; font-size:13px; }
/* The pane already draws the surface these sit on; a second border inside it reads as a box in a
   box. The popup needs them because its body is the only frame there. */
.pane-body .mail-head { padding-top:0; }
.pane-body .att-title { margin-top:16px; }
/* Deliberately its own block rather than folded into the shell's breakpoint further up: these
   rules override `.pane` above, and a media query of equal specificity that comes EARLIER in the
   sheet loses the cascade. Written up there it applied `order` and `flex-direction` — which no
   base rule contests — while `position:static` was silently overruled by the `position:sticky`
   below it, leaving the panel pinned on a screen with nothing to pin it against. */
@media (max-width: 900px) {
  /* No room for two columns. `order:-1` puts the message ABOVE the form rather than below it:
     stacked the other way you would scroll past every field to reach the thing you are copying
     from, which is worse than the popup this replaced. */
  .split { flex-direction:column; }
  .pane { order:-1; position:static; max-height:70vh; width:100%; }
}

/* Forms. Only one page has them beyond the operations controls — creating a record by hand — and
   it is a long form, so the fields are readable at a glance rather than dense. */
.uform { max-width:720px; }
.field { margin:0 0 14px; }
.field label { display:block; font-weight:600; font-size:13px; margin:0 0 4px; }
.field .req { color:var(--gold); }
.fld { width:100%; box-sizing:border-box; padding:7px 9px; font:inherit; font-size:13.5px;
    border:1px solid var(--line); border-radius:4px; background:var(--surface); color:inherit; }
.fld:focus { outline:2px solid var(--gold); outline-offset:1px; }
.fld-ro { background:#f4f3ef; color:var(--muted); }
.fld-src, .fld-hint { margin:3px 0 0; font-size:12px; color:var(--muted); }
.form-actions { display:flex; gap:10px; align-items:center; margin:18px 0 0; }
.errors { border-left:3px solid #b3261e; background:#fdf3f2; padding:10px 14px; margin:0 0 16px;
    border-radius:0 4px 4px 0; }
.errors p { margin:0; font-weight:600; font-size:13.5px; }
.errors ul { margin:6px 0 0 18px; padding:0; font-size:13px; }
.radio { display:flex; align-items:flex-start; gap:9px; padding:9px 11px; margin:0 0 6px;
    border:1px solid var(--line); border-radius:4px; cursor:pointer; }
.radio:hover { background:#faf9f6; }
.radio input { margin:2px 0 0; flex:none; }
.radio-label { flex:1; font-size:13.5px; }
.radio-hint { color:var(--muted); font-size:12px; }
@media (prefers-color-scheme:dark) {
  .fld-ro { background:#26262a; }
  .errors { background:#2a1d1c; }
  .radio:hover { background:#26262a; }
}

/* Run progress. All CSS: these pages spend exactly one inline script and it is not for this.
   The ring is a conic-gradient sweep over a masked disc — one element, no SVG, no canvas. */
.prog { display:flex; align-items:center; gap:16px; margin:2px 0 14px; }
/* Explicit, because `display:flex` above beats the user agent's `[hidden]`. The block is shipped
   on the Automation page even when nothing is running, so a run started in another tab can be
   revealed in place by the ticker rather than needing a reload. */
.prog[hidden] { display:none; }
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
#
# The close button is NOT `<form method="dialog">` like `modal()`'s is. This dialog has layers —
# open an attachment and the message it came from is swapped out of the same body — and a native
# form-submit close cannot know that. It dismissed the message along with the attachment, so
# getting back meant finding the row again. `[data-dialog-back]` is handled in _JS: it steps back
# one layer if there is one, and only closes at the bottom of the stack. Escape does the same via
# the dialog's `cancel` event.
_MAIL_DIALOG = (
    '<dialog id="mailbox-dialog" class="modal">'
    '<div class="modal-head"><h2 id="mailbox-title">Message</h2>'
    '<button type="button" class="close-x" data-dialog-back id="mailbox-close" '
    'aria-label="Close message" title="Close">&times;</button></div>'
    '<div class="modal-body" id="mailbox-body"></div></dialog>'
)

# The only JavaScript in these pages. No libraries, no build step, and nothing here interpolates
# anything a mail server sent us — every value it handles comes from a data- attribute the server
# already escaped. The message body itself renders in a sandboxed iframe that cannot run scripts.
_JS = """
// Every dialog on these pages opens through here, so the two things a modal owes the page behind
// it are hung off this one function rather than sprinkled at each call site:
//   - the document is scroll-locked. `showModal()` does not do this; without it a wheel gesture
//     over the backdrop scrolls the table underneath, which reads as the popup itself moving.
//   - the body is emptied on close. Nothing used to reset it, so a closed message's markup and
//     its live frame stayed in the DOM until the next one replaced them.
function openDialog(id) {
  var d = document.getElementById(id);
  if (!d || !d.showModal) return;
  if (d.open) return;                      // already modal: showModal() would throw
  // Bound once per dialog rather than per open: the close handler can put the dialog straight
  // back (see below), and a one-shot listener would not survive that to run a second time.
  if (!d.__closeBound) {
    d.__closeBound = true;
    d.addEventListener('close', onDialogClosed);
  }
  document.documentElement.classList.add('modal-open');
  d.showModal();
}

function onDialogClosed(e) {
  var d = e.currentTarget;
  // Escape can close through a layer that should only have stepped back. The `cancel` handler
  // below catches the ordinary case, but Chrome makes that event cancelable only when there is
  // fresh user activation and Escape grants none — so a second Escape with no click in between
  // arrives here instead. Put the layer back rather than lose the message under it; this runs
  // before the next paint, so the dialog does not visibly blink.
  if (d.id === 'mailbox-dialog' && window.__fragStack.length) {
    d.showModal();
    goBackOneFragment();
    return;
  }
  document.documentElement.classList.remove('modal-open');
  if (d.id === 'mailbox-dialog') resetFragmentStack(true);
}

document.addEventListener('click', function (e) {
  // The message dialog's ✕. Checked first because it is inside the dialog and matches nothing
  // else. It means "back" while an attachment is open over the message, and "close" only at the
  // bottom of the stack — see _MAIL_DIALOG.
  if (e.target.closest('[data-dialog-back]')) {
    e.preventDefault();
    if (!goBackOneFragment()) {
      var dlg = document.getElementById('mailbox-dialog');
      if (dlg && dlg.close) dlg.close();
    }
    return;
  }
  // A button that opens a dialog already on the page — the receiver-report preview. This is
  // checked FIRST because the row handler below bails out on any click inside a `button`, so a
  // later branch would never be reached. `modal()` renders the dialog; nothing is fetched.
  var opener = e.target.closest('[data-open]');
  if (opener) {
    e.preventDefault();
    openDialog(opener.getAttribute('data-open'));
    return;
  }
  var images = e.target.closest('[data-load-images]');
  if (images) {
    e.preventDefault();
    // In a panel the message's own URL is on the host, which is both simpler and the only thing
    // that works here: `__lastMailRow` is set by a table-row click and nothing else, so on any
    // page reached another way — the create form among them — this button did nothing at all.
    var imageHost = fragHostFor(images);
    if (imageHost) {
      loadFragment(imageHost.getAttribute('data-frag-host') + '&images=1', {host: imageHost});
    } else if (window.__lastMailRow) {
      showFragment(window.__lastMailRow, true);
    }
    return;
  }
  // Verify against Spitfire. Checked before the row handler for the same reason as [data-open]:
  // these buttons sit inside table rows, and the row handler bails on any click within a button,
  // so a later branch would never run. POSTed, because the endpoint reads Spitfire and rewrites
  // the local mirror — see verify_button().
  var check = e.target.closest('[data-verify]');
  if (check) {
    e.preventDefault();
    // Ticked rows narrow it; nothing ticked means everything, which is what the button says when
    // it is not carrying a count.
    var verifyUrl = check.getAttribute('data-verify');
    var scope = check.getAttribute('data-verify-selection');
    if (scope) {
      var picked = pickedIn(scope);
      if (picked.length) {
        verifyUrl += (verifyUrl.indexOf('?') === -1 ? '?' : '&')
          + 'ids=' + encodeURIComponent(picked.join(','));
      }
    }
    loadFragment(verifyUrl, {
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
  // A link that also names a fragment: the reclassify control. It stays an `<a href>` so a browser
  // with no script lands on the full page, and opens over the queue when there is one. Modified
  // clicks are left alone — those mean "open somewhere else", and honouring them here would open a
  // popup on a page the reader is not looking at.
  var fragLink = e.target.closest('a[data-frag]');
  if (fragLink && !(e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0)) {
    e.preventDefault();
    loadFragment(fragLink.getAttribute('data-frag'), {
      push: true, title: fragLink.getAttribute('data-frag-title') || 'Message'});
    return;
  }
  var control = e.target.closest('button[data-frag]');
  if (control) {
    e.preventDefault();
    var target = control.getAttribute('data-frag');
    if (!target) return;
    // Rendering into a panel on the page, not the popup. Checked before the `data-back` branch
    // below: a panel has no layer stack, and a "‹ Back to message" inside one must never pop the
    // dialog's — that would rewind a popup the reader is not even looking at. The panel goes back
    // by reloading the message its host names, which is always exactly what it first showed.
    var host = fragHostFor(control);
    if (host) {
      loadFragment(control.hasAttribute('data-back')
                     ? host.getAttribute('data-frag-host') : target,
                   {host: host, waiting: 'Opening\\u2026',
                    title: control.hasAttribute('data-back') ? 'The message' : 'Attachment'});
      return;
    }
    // "‹ Back to message" pops the layer it was opened from rather than fetching a new one —
    // otherwise ten open-and-backs leave ten layers to press Escape through. It still carries a
    // `data-frag`, and falls through to the fetch below when there is nothing to pop: the viewer
    // opened cold from /ui/attachments has no message behind it, and that link must still work.
    if (control.hasAttribute('data-back') && goBackOneFragment()) return;
    loadFragment(target, {
      title: control.classList.contains('att-open') || control.classList.contains('thumb-btn')
        ? 'Attachment' : 'Message',
      waiting: 'Opening\\u2026',
      // Opened from inside the dialog, so the view underneath is kept and returned to.
      push: true
    });
    return;
  }
  var row = e.target.closest('tr[data-frag]');
  // A control inside a row is its own action — the PO cell must not also open the dialog, and
  // neither must the checkbox in front of it. `input` covers the tick; without it, selecting a row
  // also opened the popup over the table you were selecting in.
  if (!row || e.target.closest('a,button,input,label')) return;
  showFragment(row);
});

// --- row selection ------------------------------------------------------------
// A selection is not a form. Nothing is posted until a control that reads it is pressed, and that
// control names the table it reads — so a page can never submit a list of ids it did not mean to.

function pickedIn(id) {
  return Array.prototype.slice.call(
    document.querySelectorAll('input.pick[data-pick-for="' + id + '"]:checked')
  ).map(function (box) { return box.value; });
}

// The boxes on screen — this page of the table, minus anything the filter is hiding. The
// select-the-page control speaks for exactly these, while the count on the button speaks for every
// ticked row in the table: a selection made on page one is still a selection on page two.
function pageBoxes(id) {
  return Array.prototype.slice.call(
    document.querySelectorAll('input.pick[data-pick-for="' + id + '"]')
  ).filter(function (box) {
    var row = box.closest('tr');
    return !row || !(row.classList.contains('paged-out') || row.classList.contains('filtered-out'));
  });
}

// Everything the selection changes, in one pass: the select-the-page box's own three states, and
// the label on every control that acts on this table's ticked rows.
function refreshSelection(id) {
  var onPage = pageBoxes(id);
  var pickedHere = onPage.filter(function (box) { return box.checked; }).length;
  var picked = pickedIn(id);
  var all = document.querySelector('input.pick-all[data-pick-all="' + id + '"]');
  if (all) {
    // Measured against this page, because that is what pressing it does. Measured against the
    // whole table, it read "indeterminate" immediately after selecting the page — so the next
    // press selected the page again instead of clearing it.
    all.checked = onPage.length > 0 && pickedHere === onPage.length;
    // Some but not all: neither ticked nor empty is the truth, and a box that claims either is
    // lying about what pressing it will do.
    all.indeterminate = pickedHere > 0 && pickedHere < onPage.length;
  }
  document.querySelectorAll('[data-verify-selection="' + id + '"]').forEach(function (button) {
    var whole = button.getAttribute('data-verify-all') || button.textContent;
    button.textContent = picked.length ? 'Verify ' + picked.length + ' selected' : whole;
  });
}

document.addEventListener('change', function (e) {
  if (!e.target.matches) return;
  if (e.target.matches('input.pick')) {
    refreshSelection(e.target.getAttribute('data-pick-for'));
    return;
  }
  if (!e.target.matches('input.pick-all')) return;
  var id = e.target.getAttribute('data-pick-all');
  var wanted = e.target.checked;
  // The page, not the table. Ticking a box that silently selected forty rows on other pages —
  // including ones the filter is hiding — would make the count beside the button a surprise.
  pageBoxes(id).forEach(function (box) { box.checked = wanted; });
  refreshSelection(id);
});

document.addEventListener('keydown', function (e) {
  if (e.key !== 'Enter') return;
  var row = e.target.closest && e.target.closest('tr[data-frag]');
  if (row) showFragment(row);
});

// Two things that need the document parsed. This script is emitted in the head — deliberately, so
// the collapsed rail lands before the sidebar paints — so neither the dialog nor a panel exists
// yet at this point.
//
//   - Escape. `cancel` fires before the dialog closes itself, so preventing it is what lets
//     Escape mean "back one layer" while an attachment is open.
//   - Any panel's first content is server-rendered markup that no fetch ever touched, so nothing
//     has measured its frame. Without this the message sits at its fallback height and scrolls
//     inside a panel that is also scrolling — the doubled scrollbar, one layer further in.
(function () {
  function onReady() {
    document.querySelectorAll('[data-frag-host]').forEach(fitFrames);
    var d = document.getElementById('mailbox-dialog');
    if (!d) return;
    d.addEventListener('cancel', function (e) {
      if (!window.__fragStack || !window.__fragStack.length) return;
      // Only step back here if the close can actually be stopped. Chrome fires this uncancelable
      // when there is no fresh user activation — Escape grants none, so a second Escape with no
      // click between arrives that way. Popping regardless would take the layer off the stack
      // AND let the dialog close, losing the message; onDialogClosed handles that case instead.
      if (!e.cancelable) return;
      e.preventDefault();
      goBackOneFragment();
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', onReady);
  } else {
    onReady();
  }
})();

// The row names the fragment to fetch, so one path serves both the message popup and a purchase
// order's progress bar. The URL was built and escaped server-side; nothing is assembled here.
function showFragment(row, withImages) {
  window.__lastMailRow = row;
  loadFragment(row.getAttribute('data-frag') + (withImages ? '&images=1' : ''),
               {title: 'Message', waiting: 'Opening\\u2026'});
}

// ---- the dialog's layers ---------------------------------------------------------------------
// One dialog serves a message, an attachment opened from inside that message, a member inside
// that attachment's archive. Each swaps `#mailbox-body`, so without a record of what was there
// before, ✕ and Escape could only throw the lot away — closing an attachment closed the message
// under it. The stack holds the markup of each view navigated away from, so going back is a
// restore rather than a refetch: instant, and the reader lands where they left off.
window.__fragStack = [];

function resetFragmentStack(alsoClearBody) {
  window.__fragStack = [];
  if (alsoClearBody) {
    var body = document.getElementById('mailbox-body');
    if (body) body.innerHTML = '';
  }
  syncCloseAffordance();
}

// The ✕ has to say which of its two jobs it is about to do.
function syncCloseAffordance() {
  var x = document.getElementById('mailbox-close');
  if (!x) return;
  var nested = window.__fragStack.length > 0;
  x.title = nested ? 'Back' : 'Close';
  x.setAttribute('aria-label', nested ? 'Back to the previous view' : 'Close message');
}

// Returns false when there is nothing to go back to, which is the caller's signal to close (the
// ✕) or to fetch instead (a "Back to message" in a viewer opened cold from /ui/attachments).
function goBackOneFragment() {
  if (!window.__fragStack.length) return false;
  var prev = window.__fragStack.pop();
  var body = document.getElementById('mailbox-body');
  var head = document.getElementById('mailbox-title');
  if (head) head.textContent = prev.title;
  body.innerHTML = prev.html;
  // The same two passes the fetch path runs: the restored markup can carry a frame to measure and
  // a paged table to re-page, and it is being parsed fresh here.
  fitFrames(body);
  refreshEveryView();
  restoreBodyScroll(body, prev.scrollTop);
  syncCloseAffordance();
  return true;
}

// Landing back where you left off is the whole point of restoring rather than refetching, and a
// single assignment does not achieve it: fitFrames grows the frame asynchronously, so at this
// instant the body is one frame-height tall and any scrollTop past that clamps to the bottom.
// Re-apply until it takes, then stop — capped, so a view that genuinely got shorter cannot spin.
function restoreBodyScroll(body, top) {
  if (!top) return;
  var tries = 0;
  (function again() {
    body.scrollTop = top;
    if (body.scrollTop >= top || tries++ > 20) return;
    requestAnimationFrame(again);
  })();
}

// Where a control renders what it fetches. Two ways to be somewhere other than the popup, because
// there are two kinds of control: one inside the panel (an attachment in the message's own list),
// and one outside it that still belongs to it — a POD radio's Open button, over in the form
// column, which has to name its target because its position cannot imply one.
function fragHostFor(control) {
  var named = control.getAttribute('data-frag-into');
  return named ? document.getElementById(named) : control.closest('[data-frag-host]');
}

// One fetch path for every kind of fragment. `opts` names the method, the heading and the text to
// show while the request is in flight — the verify endpoints take seconds, and a dialog that says
// only "Opening" for eight of them reads as hung. `opts.push` says this view was opened from
// inside the dialog, so the one underneath is kept; anything else starts a fresh stack.
//
// `opts.host` renders into a panel on the page instead of the dialog. A panel needs no layer stack
// and no scroll-lock: it has neither a ✕ nor an Escape to steal the view out from under you, and
// its "‹ Back to message" just reloads the URL its `data-frag-host` names. Everything the stack
// exists to protect against is a dialog problem.
function loadFragment(url, opts) {
  opts = opts || {};
  var host = opts.host || null;
  var body = host || document.getElementById('mailbox-body');
  var head = host ? host.parentNode.querySelector('[data-pane-title]')
                  : document.getElementById('mailbox-title');
  if (!host) {
    var dlg = document.getElementById('mailbox-dialog');
    if (opts.push && dlg && dlg.open) {
      window.__fragStack.push({
        title: head ? head.textContent : 'Message',
        html: body.innerHTML,
        scrollTop: body.scrollTop
      });
    } else if (!opts.push) {
      window.__fragStack = [];
    }
    syncCloseAffordance();
  }
  // The dialog falls back to "Message" because it is reused for everything and a stale heading
  // mislabels what is under it. A panel keeps whatever it says until something renames it: a
  // reload of the same message (the remote-images button) is not a change of subject.
  if (head && (opts.title || !host)) head.textContent = opts.title || 'Message';
  body.innerHTML = '<p class="note">' + (opts.waiting || 'Opening\\u2026') + '</p>';
  if (!host) openDialog('mailbox-dialog');
  fetch(url, {method: opts.method || 'GET'})
    .then(function (r) { return r.text(); })
    // `refreshEveryView` because a fragment can carry its own paged table — a spreadsheet
    // attachment preview is one. The first paint runs at page load, which was long before this
    // markup existed, so without this the table arrives showing every row it has.
    .then(function (html) { body.innerHTML = html; fitFrames(body); refreshEveryView();
                            prefillVerdictName(); })
    .catch(function (err) {
      body.innerHTML = '<p class="empty">Could not open this: ' + err + '</p>';
    });
}

// The body frame has no scripts of its own, so the page sizes it from out here. Growing it to its
// content is what leaves .modal-body as the only scroller in the popup — a frame at a fixed height
// inside a scrolling body is two scrollbars for one message, and the reader has to work out which
// one to grab. Failing to measure is survivable, not broken: the CSS height takes over.
//
// This used to attach a `load` listener and nothing else, which meant it almost never ran: the
// frames arrive by innerHTML with their document in `srcdoc`, so by the time this is called the
// load event has usually already fired and the listener waits for a second one that never comes.
// Measure now if the document is there, and keep the listener for the case where it is not.
function fitFrames(scope) {
  scope.querySelectorAll('iframe.mail-body, iframe.view-frame').forEach(function (f) {
    function measure() {
      try {
        var doc = f.contentDocument;
        if (!doc || !doc.body) return;
        var h = Math.max(doc.body.scrollHeight, doc.documentElement.scrollHeight);
        // The cap is a runaway guard, not a viewport: the dialog's own max-height is what bounds
        // what you see, and .modal-body scrolls the rest.
        f.style.height = Math.min(Math.max(h + 32, 220), 20000) + 'px';
      } catch (err) { /* cross-origin or torn down: leave the default height */ }
    }
    // Inline and remote images finish after the document does, and each one that lands makes the
    // real height larger than what was just measured. Watching the document element re-measures
    // without polling. allow-same-origin is on the sandbox, so this is reachable — see
    // pipeline/mail_view.py.
    function watch() {
      try {
        if (typeof ResizeObserver !== 'function' || f.__fitObserver) return;
        var root = f.contentDocument && f.contentDocument.documentElement;
        if (!root) return;
        var ro = new ResizeObserver(measure);
        ro.observe(root);
        // Nothing else holds this frame, so the observer dies with it when the body is replaced.
        f.__fitObserver = ro;
      } catch (err) { /* no observer: the measurements above stand */ }
    }
    f.addEventListener('load', function () { measure(); watch(); });
    // Both paths must watch, and only one of them runs: an already-parsed frame gets no second
    // load event, and a frame still parsing has no document element to observe yet.
    if (f.contentDocument && f.contentDocument.readyState === 'complete') { measure(); watch(); }
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

// Did my click land? Until this existed the answer was "you cannot tell for about two seconds".
//
// Every page here is server-rendered and every nav entry is a plain `<a href>`, so a click starts a
// document navigation and the browser then keeps the CURRENT page fully painted — old content, old
// entry still lit — until the response arrives. On the heavier pages that is well over a second
// with no on-screen change at all, which reads as a dead button and is exactly why people press it
// twice.
//
// So: light the entry that was clicked, and run a bar along the top edge. Both are undone by
// leaving the page, and by `pageshow` for the one case where we come back to this document rather
// than leaving it (below).
function markNavigating(link) {
  if (link) link.classList.add('going');
  // On the root element, which is also what the CSS uses to stand the departing entry down — the
  // rail must not read as two current pages while one is becoming the other.
  document.documentElement.classList.add('nav-busy');
  var bar = document.querySelector('.load-bar');
  if (bar) bar.hidden = false;
}

function clearNavigating() {
  document.querySelectorAll('.nav a.going, .side-alert.going').forEach(function (a) {
    a.classList.remove('going');
  });
  // A button left disabled by a submission this page never came back from — Back out of a slow
  // POST and the restored document would otherwise hand back a control that can never be pressed.
  document.querySelectorAll('[data-busy-label][disabled]').forEach(function (b) {
    b.disabled = false;
    b.removeAttribute('aria-busy');
    var l = b.querySelector('.lbl') || b;
    if (b.title) l.textContent = b.title;
  });
  document.documentElement.classList.remove('nav-busy');
  var bar = document.querySelector('.load-bar');
  if (bar) bar.hidden = true;
}

document.addEventListener('click', function (e) {
  var link = e.target.closest('.nav a, .side-alert');
  if (!link) return;
  // A modified click opens somewhere else — a new tab, a new window, a download. THIS page is not
  // going anywhere, so marking it as leaving would light an entry that never becomes current and
  // leave the bar running for ever. `button !== 0` covers the middle-click that also opens a tab.
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
  if (link.target && link.target !== '_self') return;
  if (e.defaultPrevented) return;
  markNavigating(link);
});

// A button whose work is slow enough to look broken. "Check now" reaches Microsoft and can take
// twenty seconds; until this existed the page sat completely unchanged for all of it, which reads
// as a dead button and invited the second and third presses that each started another mailbox walk.
//
// `submit`, not `click`: the form submits by Enter as well, and both need the same feedback.
//
// **Synchronously, not in a `setTimeout(…, 0)`.** The deferred version was written to be careful —
// a button disabled during a *click* is never submitted — but by the time `submit` fires the
// browser has already gathered the form data, so disabling here cannot cancel anything. Deferring
// it meant the callback raced the navigation and usually lost: measured in real Chrome, the label
// never changed at all on an actual press, only when the submission was cancelled. A feedback
// mechanism that works everywhere except the case it exists for is worse than none, because it
// tests green.
document.addEventListener('submit', function (e) {
  var button = e.target.querySelector('[data-busy-label]');
  if (!button || button.disabled) return;
  var label = button.querySelector('.lbl') || button;
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  label.textContent = button.getAttribute('data-busy-label');
});

// Back from the browser's cache restores this document exactly as it was left — including, without
// this, a nav entry still lit for a page we navigated away from and a bar still sliding. `pageshow`
// fires for both a fresh load and a bfcache restore, so one listener covers both.
window.addEventListener('pageshow', clearNavigating);

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

// A bare number is a purchase order, and is matched against the row's own PO rather than its text.
//
// One email's subject can name several purchase orders at once — the property delivery confirmation
// names seven — so a text search for `912614` returned 46 queue rows of which **none** were that
// PO: they were 907514's and 907249's, matched on a subject they happened to share. Every one of
// them displayed its true PO in its own column, so the page was honest and the search was not.
//
// Only rows that declare a PO are held to this. A row without `data-po` (an attachment, a message
// with no PO resolved) falls back to text, so a number in a tracking reference is still findable.
function termMatches(row, hay, term) {
  if (/^\\d{5,}$/.test(term)) {
    var po = row.getAttribute('data-po');
    if (po) { return po.toLowerCase().indexOf(term) !== -1; }
  }
  return hay.indexOf(term) !== -1;
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

// The column a `choice_filter()` matches against, from `table(choice_column=...)`. Same contract as
// dateColumnOf: -1 means this table has none, so the dropdown leaves it alone rather than hiding
// every row by comparing against a column that is not there.
function choiceColumnOf(scope) {
  var heads = scope.querySelectorAll('thead th');
  for (var i = 0; i < heads.length; i++) {
    if (heads[i].getAttribute('data-choice')) return i;
  }
  return -1;
}

// A local YYYY-MM-DD. `toISOString()` is UTC, so east of Greenwich it hands back yesterday for
// most of the working day — and "Today" would then hide everything that arrived this morning.
function isoDay(date) {
  var m = date.getMonth() + 1, d = date.getDate();
  return date.getFullYear() + '-' + (m < 10 ? '0' + m : m) + '-' + (d < 10 ? '0' + d : d);
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

// Which page numbers to offer. Windowed rather than every page: a table of two thousand rows would
// otherwise put eighty buttons under it, which is not a control any more. 0 stands for a gap.
function pageNumbers(page, pages) {
  var out = [], n;
  if (pages <= 7) {
    for (n = 1; n <= pages; n++) out.push(n);
    return out;
  }
  var lo = Math.max(2, page - 1), hi = Math.min(pages - 1, page + 1);
  out.push(1);
  if (lo > 2) out.push(0);
  for (n = lo; n <= hi; n++) out.push(n);
  if (hi < pages - 1) out.push(0);
  out.push(pages);
  return out;
}

// The numbered buttons, written here rather than by the server: how many pages there are depends
// on what the filter left, so a server-rendered "1 2 3" would be wrong the moment anyone typed.
// Built as elements, never as a string of markup — nothing on this page assembles HTML from
// values, and a page count is not the place to start.
function paintPages(control, page, pages, size) {
  var host = control.querySelector('.pager-nums');
  if (!host) return;
  host.textContent = '';
  // "All" is one page, and a lone "1" beside Prev and Next says nothing.
  if (size === 0) return;
  pageNumbers(page, pages).forEach(function (n) {
    if (!n) {
      var gap = document.createElement('span');
      gap.className = 'pager-gap';
      gap.textContent = '\\u2026';
      host.appendChild(gap);
      return;
    }
    var button = document.createElement('button');
    button.type = 'button';
    button.className = n === page ? 'page-btn on' : 'page-btn';
    button.setAttribute('data-page', String(n));
    button.setAttribute('aria-label', 'Page ' + n);
    if (n === page) button.setAttribute('aria-current', 'page');
    button.textContent = String(n);
    host.appendChild(button);
  });
}

// One pass does filtering, paging and the zebra together. They were two independent passes at
// first, and each kept overwriting the other's mind about which rows were visible.
//
// Every whitespace-separated word must match, so "908491 delivered" narrows rather than widens. A
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

  var choiceEl = document.querySelector('select.choice-filter[data-choice-for~="' + id + '"]');
  var choice = choiceEl ? choiceEl.value.toLowerCase() : '';
  // One option may name several column values, separated by `|`. That is what lets an entry stand
  // for a queue — the triage view is every verdict meaning a person still has to look — rather
  // than for one verdict.
  var choices = choice ? choice.split('|') : [];
  var choiceColumn = choice ? choiceColumnOf(scope) : -1;
  // The reason chips, which narrow by a second, independent dimension: what kind of thing this is
  // (the dropdown) and why it is here (these) are different questions, and picking one must not
  // silently clear the other.
  var pill = document.querySelector('.pill.sel[data-reason-for~="' + id + '"]');
  var reason = pill ? pill.getAttribute('data-reason') : '';
  // A row may carry its own value instead, for a state that is not any one cell — see
  // `table(choice_values=...)`. Checked once for the table rather than once per row.
  var rowValued = choice ? !!scope.querySelector('tbody tr[data-choice-value]') : false;

  var matched = [];
  parts.units.forEach(function (unit) {
    var hit = !terms.length || unit.some(function (row) {
      var hay = rowHaystack(row);
      return terms.every(function (t) { return termMatches(row, hay, t); });
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
    // Exact match on one column, not a substring of the row: "route" appears in plenty of Why
    // text, and a contains-match would keep rows whose verdict is something else entirely.
    if (hit && (choiceColumn >= 0 || rowValued)) {
      hit = unit.some(function (row) {
        var own = row.getAttribute('data-choice-value');
        if (own !== null) {
          // A row may carry several words at once — a signature logo that nothing could read is
          // both `inline` and `unread` — so this is an intersection, not an equality. Comparing
          // the whole attribute made a row in two states match neither of them.
          var words = own.trim().toLowerCase().split(/\\s+/);
          return choices.some(function (want) { return words.indexOf(want) !== -1; });
        }
        if (choiceColumn < 0) return false;
        var cell = row.children[choiceColumn];
        return !!cell && choices.indexOf(cell.textContent.trim().toLowerCase()) !== -1;
      });
    }
    if (hit && reason) {
      // Token match, not equality: a merged queue row carries every reason it stands for, space
      // separated, the same shape `data-reason-for~=` uses above. Equality found such a row only
      // under the one reason its badge happened to show.
      hit = unit.some(function (row) {
        var carried = ' ' + (row.getAttribute('data-reason') || '') + ' ';
        return carried.indexOf(' ' + reason + ' ') !== -1;
      });
    }
    unit.forEach(function (row) { row.classList.toggle('filtered-out', !hit); });
    if (hit) matched.push(unit);
  });
  parts.chrome.forEach(function (row) { row.classList.remove('filtered-out', 'paged-out'); });
  // Any narrowing at all, not just typed text. `nth-child(even)` counts hidden rows, so a table
  // narrowed by date or verdict alone striped at random while this only watched the search box.
  scope.classList.toggle('filtering',
    terms.length > 0 || dateColumn >= 0 || choiceColumn >= 0 || (choices.length > 0 && rowValued)
    || reason !== '');

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
  // Says which rows are on screen out of how many there are, not just "page 2 of 5" — the count is
  // the part that tells you whether the thing you are looking for is even in this table.
  var showing = control.querySelector('.pager-showing');
  if (showing) {
    showing.textContent = '';
    if (!matched.length) {
      showing.textContent = 'No ' + noun + 's';
    } else {
      var span = document.createElement('b');
      span.textContent = size > 0
        ? (first + 1) + '\\u2013' + Math.min(first + size, matched.length)
        : '1\\u2013' + matched.length;
      showing.appendChild(document.createTextNode('Showing '));
      showing.appendChild(span);
      showing.appendChild(document.createTextNode(' of ' + matched.length));
    }
  }
  paintPages(control, page, pages, size);
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
  else writeCount(readout, ids, matched, total, noun);
}

// The count for a narrowed set, in words that are true about what was searched.
function writeCount(readout, ids, matched, total, noun) {
  // Plainly, because there is nothing left to qualify. This used to carry a second branch for
  // a capped table: it said 'No match in the 500 loaded' and offered a link to search the
  // rest, because the browser filter could only ever see the slice the server had sent. No
  // table is capped any more, so every one of these counts is about every row that exists.
  if (!matched) readout.textContent = 'No ' + noun + 's match — ' + total + ' hidden';
  else readout.textContent = 'Showing ' + matched + ' of ' + total + ' ' + noun
    + (total === 1 ? '' : 's');
}

// Is anything narrowing this set — text typed, either end of a date range set, or a dropdown off
// its "All"? Every filter that can hide a row has to be listed here, or that filter hides rows
// while the count sits blank, which is the silent-hiding problem the count exists to prevent.
function narrowed(input) {
  if (input && input.value.trim()) return true;
  var ids = input ? targetsOf(input) : [];
  return ids.some(function (id) {
    var f = document.querySelector('input.date-from[data-from~="' + id + '"]');
    var t = document.querySelector('input.date-to[data-to~="' + id + '"]');
    var c = document.querySelector('select.choice-filter[data-choice-for~="' + id + '"]');
    var r = document.querySelector('.pill.sel[data-reason-for~="' + id + '"]');
    return (f && f.value) || (t && t.value) || (c && c.value) || !!r;
  });
}

function refreshEveryView() {
  document.querySelectorAll('.scroll[id]').forEach(function (s) { refreshView(s.id); });
  document.querySelectorAll('input.filter').forEach(function (i) { refreshFor(i, false); });
  // After paging or filtering, "every row on this page" means a different set of rows — so the
  // select-the-page box has to say something different about it.
  document.querySelectorAll('input.pick-all').forEach(function (box) {
    refreshSelection(box.getAttribute('data-pick-all'));
  });
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

// Light the chip that matches the range now in the boxes. `days === null` means "none of them" —
// a hand-typed range is nobody's preset, and leaving a chip lit would have it claim a range it did
// not set and could not restore.
function markChips(targets, days) {
  document.querySelectorAll('.chip[data-range-for="' + targets + '"]').forEach(function (chip) {
    var own = chip.getAttribute('data-range');
    // The Custom segment carries no range of its own. It is the one that lights when the dates
    // came from the two boxes (`days === null`) rather than from a preset — otherwise the group
    // shows nothing selected while a range is actively hiding two thirds of the table.
    var on = own === null ? days === null : (days !== null && own === days);
    chip.classList.toggle('sel', on);
    // Only the presets are buttons; `aria-pressed` on a <summary> is not a state it can have.
    if (chip.tagName === 'BUTTON') chip.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
}

// Redraw every table a control names, from page one. The three date controls all end here.
function refreshTargets(targets) {
  targets.split(/\\s+/).filter(Boolean).forEach(function (id) {
    pageOf[id] = 1;
    refreshOwning(id);
  });
}

document.addEventListener('change', function (e) {
  if (!e.target.matches || !e.target.matches('input.date-from, input.date-to')) return;
  var targets = e.target.getAttribute('data-from') || e.target.getAttribute('data-to') || '';
  markChips(targets, null);
  refreshTargets(targets);
});

// The presets do not filter. They fill the same two boxes the range already uses and let the range
// do the work, so there is one definition of "the last 7 days" rather than two that can drift.
document.addEventListener('click', function (e) {
  var chip = e.target.closest && e.target.closest('.chip[data-range]');
  if (!chip) return;
  var targets = chip.getAttribute('data-range-for') || '';
  var days = chip.getAttribute('data-range');
  var from = '';
  if (days !== '') {
    var since = new Date();
    since.setDate(since.getDate() - parseInt(days, 10));
    from = isoDay(since);
  }
  // Only the From end. A preset is "since", and pinning To to today would drop anything carrying
  // a date in the future — which a stated delivery date legitimately can be.
  document.querySelectorAll('input.date-from, input.date-to').forEach(function (i) {
    if ((i.getAttribute('data-from') || i.getAttribute('data-to')) !== targets) return;
    i.value = i.classList.contains('date-from') ? from : '';
  });
  markChips(targets, days);
  refreshTargets(targets);
});

// Preset reasons. The chips do not carry the answer — the textarea does, and it is the only thing
// submitted. Pressing one writes its sentence in, pressing it again takes it out, and anything
// typed by hand is left alone as another clause of the same list.
function presetClauses(box) {
  return (box.value || '').split(';').map(function (part) { return part.trim(); })
                          .filter(function (part) { return part !== ''; });
}

// Lit from the text, never from what was last clicked. Deleting a clause by hand has to unlight its
// chip, or the control starts claiming something the box does not say.
function markPresets(box) {
  var clauses = presetClauses(box);
  document.querySelectorAll('.chip[data-preset-for="' + box.id + '"]').forEach(function (chip) {
    var on = clauses.indexOf(chip.getAttribute('data-preset')) !== -1;
    chip.classList.toggle('sel', on);
    chip.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
}

document.addEventListener('click', function (e) {
  var chip = e.target.closest && e.target.closest('.chip[data-preset]');
  if (!chip) return;
  var box = document.getElementById(chip.getAttribute('data-preset-for') || '');
  if (!box) return;
  var phrase = chip.getAttribute('data-preset');
  var clauses = presetClauses(box);
  var at = clauses.indexOf(phrase);
  if (at === -1) { clauses.push(phrase); } else { clauses.splice(at, 1); }
  box.value = clauses.join('; ');
  markPresets(box);
  box.focus();
});

document.addEventListener('input', function (e) {
  if (e.target.matches && e.target.matches('textarea.fld')) markPresets(e.target);
});

document.addEventListener('click', function (e) {
  var clear = e.target.closest && e.target.closest('[data-date-clear]');
  if (!clear) return;
  var targets = clear.getAttribute('data-date-clear');
  document.querySelectorAll('input.date-from, input.date-to').forEach(function (i) {
    if ((i.getAttribute('data-from') || i.getAttribute('data-to')) === targets) i.value = '';
  });
  markChips(targets, '');
  refreshTargets(targets);
});

document.addEventListener('change', function (e) {
  if (!e.target.matches || !e.target.matches('select.choice-filter')) return;
  refreshTargets(e.target.getAttribute('data-choice-for') || '');
});

// Reason chips. Single-select: pressing the pressed one clears it, which is the only way back to
// "all reasons" without a fifth chip that says so.
document.addEventListener('click', function (e) {
  var pill = e.target.closest && e.target.closest('.pill[data-reason-for]');
  if (!pill || pill.disabled) return;
  var targets = pill.getAttribute('data-reason-for') || '';
  var wanted = !pill.classList.contains('sel');
  document.querySelectorAll('.pill[data-reason-for="' + targets + '"]').forEach(function (other) {
    var on = wanted && other === pill;
    other.classList.toggle('sel', on);
    other.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
  refreshTargets(targets);
});

document.addEventListener('click', function (e) {
  var button = e.target.closest && e.target.closest('.pager [data-page]');
  if (!button || button.disabled) return;
  var control = button.closest('.pager');
  var id = control.getAttribute('data-pager-for');
  var want = button.getAttribute('data-page');
  var current = pageOf[id] || 1;
  // Prev and Next step; a numbered button goes straight there.
  pageOf[id] = want === 'next' ? current + 1
             : want === 'prev' ? current - 1
             : (parseInt(want, 10) || 1);
  refreshOwning(id);
  // A pane scrolls inside itself, so page two would otherwise open half-way down its own rows.
  var pane = document.getElementById(id);
  if (pane) pane.scrollTop = 0;
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

// --- deciding what a message is, without leaving the queue ---------------------------------------
// The decision used to be a page: press "Not a delivery" on a row, land on a form, submit, land on
// another list. Three navigations, and the filter, the page and the scroll position were gone by
// the end of them — on a queue of 3,500 rows that is the row you were reading, lost.
//
// Here the same form arrives in the popup already open over the queue, and the answer comes back as
// data rather than as a page. The rows the verdict retires are taken out of the tables in place.
// The server is unchanged in what it decides; this changes only where the reader is standing.

function verdictForm() {
  var dialog = document.getElementById('mailbox-dialog');
  if (!dialog || !dialog.open) return null;
  return dialog.querySelector('form.uform[action="/ui/mail/verdict"]');
}

// Every row of these messages, across every table on the page: the message's own row, its
// attachments, and each record read out of it.
function rowsOfMessages(ids) {
  var wanted = {};
  ids.forEach(function (id) { wanted[id] = true; });
  return Array.prototype.filter.call(
    document.querySelectorAll('tr[data-mail-id]'),
    function (row) { return wanted[row.getAttribute('data-mail-id')] === true; });
}

function takeRowsAway(ids) {
  var gone = rowsOfMessages(ids);
  gone.forEach(function (row) { row.remove(); });
  // The counts, the paging and the search all read the rows that are there, so one repaint is what
  // makes the page true again. Nothing is refetched.
  refreshEveryView();
  return gone.length;
}

// What was just decided, and the way back out of it. Undo posts the opposite verdict for exactly
// the messages that were set aside, then reloads — the rows have to come back from the server,
// and `restorePlace()` puts the filter and the scroll back as they were.
function sayWhatWasDecided(answer, rows) {
  var messages = answer.email_ids || [];
  var said = (answer.verdict === 'not_delivery' ? 'Set aside ' : 'Updated ')
    + messages.length + (messages.length === 1 ? ' message' : ' messages')
    + (rows ? ' · ' + rows + (rows === 1 ? ' row' : ' rows') + ' off this page' : '');
  var box = document.createElement('div');
  box.className = 'toast';
  box.setAttribute('role', 'status');
  box.appendChild(document.createTextNode(said + ' · '));
  var undo = document.createElement('button');
  undo.type = 'button';
  undo.className = 'toast-undo';
  undo.textContent = 'Undo';
  undo.addEventListener('click', function () {
    undo.disabled = true;
    undo.textContent = 'Undoing…';
    var back = answer.verdict === 'not_delivery' ? 'delivery' : 'not_delivery';
    var undone = 0;
    messages.forEach(function (id) {
      var body = 'email_id=' + encodeURIComponent(id) + '&to=' + encodeURIComponent(back)
        + '&by=' + encodeURIComponent(answer.by || 'undo')
        + '&note=' + encodeURIComponent('undone from the queue');
      fetch('/ui/mail/verdict', {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'},
        body: body
      }).then(function () {
        if (++undone === messages.length) { savePlace(); location.reload(); }
      });
    });
  });
  box.appendChild(undo);
  document.body.appendChild(box);
  setTimeout(function () { box.remove(); }, 12000);
}

document.addEventListener('submit', function (e) {
  var form = e.target;
  if (form !== verdictForm()) return;
  e.preventDefault();
  var button = form.querySelector('button[type="submit"]');
  var label = button ? button.textContent : '';
  if (button) { button.disabled = true; button.textContent = 'Recording…'; }
  var by = form.querySelector('[name="by"]');
  // Remembered so the second decision of a session is one press and a reason, not a name retyped
  // thirty times. `localStorage`, because it is the person, not the page they are standing on.
  if (by && by.value) { try { localStorage.setItem('premier-verdict-by', by.value); } catch (err) {} }

  fetch(form.getAttribute('action'), {
    method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'},
    body: new URLSearchParams(new FormData(form)).toString()
  }).then(function (r) { return r.json(); }).then(function (answer) {
    if (!answer || !answer.ok) {
      // Refused — an unsigned decision, most often. The form stays open carrying what was typed.
      if (button) { button.disabled = false; button.textContent = label; }
      var problem = document.createElement('div');
      problem.className = 'errors';
      problem.appendChild(Object.assign(document.createElement('p'),
        {textContent: (answer && answer.problem) || 'That was not recorded. Try again.'}));
      form.parentNode.insertBefore(problem, form);
      return;
    }
    answer.by = by ? by.value : '';
    var dialog = document.getElementById('mailbox-dialog');
    // `resetFragmentStack` runs off the dialog's own close handler, so the layers this decision
    // was opened through are cleared with it rather than left behind.
    if (dialog && dialog.open && dialog.close) { window.__fragStack = []; dialog.close(); }
    sayWhatWasDecided(answer, takeRowsAway(answer.email_ids || []));
  }).catch(function () {
    // The network, not the decision. Nothing was recorded, so the form is handed back with what
    // was typed still in it and the full page form one link away.
    if (button) { button.disabled = false; button.textContent = label; }
    var problem = document.createElement('div');
    problem.className = 'errors';
    problem.appendChild(Object.assign(document.createElement('p'),
      {textContent: 'That did not reach the server, and nothing was recorded. Try again.'}));
    form.parentNode.insertBefore(problem, form);
  });
});

// The name this person signed with last time, filled in as the form arrives in the popup.
function prefillVerdictName() {
  var form = verdictForm();
  if (!form) return;
  var by = form.querySelector('[name="by"]');
  var remembered = null;
  try { remembered = localStorage.getItem('premier-verdict-by'); } catch (err) { /* private mode */ }
  if (by && !by.value && remembered) by.value = remembered;
}


// --- keeping your place -------------------------------------------------------
// Every page here is server-rendered, so acting on a row is a navigation: Not a delivery, Waive,
// Create, and the Back link out of each of them. The filter text, the table page, the sort and the
// scroll position lived only in the document, so all four were gone on the way back — and the
// person landed on 500 unfiltered rows hunting for the one they had been reading.
//
// So the page writes down what it looks like and puts it back. `sessionStorage`, not `local`: this
// is where *this tab* was standing. It should not follow the operator into a second window opened
// to compare two queues, and it should not still be filtering the queue tomorrow morning.
//
// The key is the full path and query, so /ui/records and /ui/manual remember separately, and
// ?all=1 is a different view of a page rather than the same one.
var PLACE_PREFIX = 'premier-place:';
var PLACE_TTL_MS = 43200000;          // 12 hours: a filter older than a shift is not where you are
var NAV_KEY = 'premier-nav';
var TRAIL_PREFIX = 'premier-trail:';
var navIndex = 0;
var TOAST_KEY = 'premier-toast';

// Every read and write goes through these two. Private mode, and a browser set to block site data,
// both throw on the first touch — and a page that remembers nothing must still be a working page.
function readStore(key) {
  try { return sessionStorage.getItem(key); } catch (err) { return null; }
}

function writeStore(key, value) {
  try {
    if (value === null) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, value);
  } catch (err) { /* storage disabled: the page works, it just does not remember */ }
}

function placeKey() { return PLACE_PREFIX + location.pathname + location.search; }

// What the page looks like right now, as data. Only what a person set: an empty box is not a state
// worth restoring, and storing it would make "nothing is filtered" look like a decision.
function collectPlace() {
  var state = {filters: {}, dates: {}, choice: {}, reason: {}, pages: {}, sort: {},
               scrollY: window.scrollY, savedAt: Date.now()};
  document.querySelectorAll('input.filter').forEach(function (input) {
    if (input.value !== '') state.filters[input.getAttribute('data-filter') || ''] = input.value;
  });
  document.querySelectorAll('input.date-from, input.date-to').forEach(function (input) {
    var from = input.classList.contains('date-from');
    var targets = input.getAttribute(from ? 'data-from' : 'data-to') || '';
    if (input.value) state.dates[(from ? 'from ' : 'to ') + targets] = input.value;
  });
  document.querySelectorAll('select.choice-filter').forEach(function (select) {
    if (select.value) state.choice[select.getAttribute('data-choice-for') || ''] = select.value;
  });
  document.querySelectorAll('.pill.sel[data-reason-for]').forEach(function (pill) {
    state.reason[pill.getAttribute('data-reason-for') || ''] = pill.getAttribute('data-reason') || '';
  });
  Object.keys(pageOf).forEach(function (id) {
    if (pageOf[id] > 1) state.pages[id] = pageOf[id];
  });
  document.querySelectorAll('.scroll[id]').forEach(function (scope) {
    var th = scope.querySelector('thead th[aria-sort="ascending"], thead th[aria-sort="descending"]');
    if (th && th.getAttribute('data-sort')) {
      state.sort[scope.id] = {column: parseInt(th.getAttribute('data-sort'), 10),
                              direction: th.getAttribute('aria-sort')};
    }
  });
  return state;
}

function placeIsEmpty(state) {
  return !Object.keys(state.filters).length && !Object.keys(state.dates).length
    && !Object.keys(state.choice).length && !Object.keys(state.reason).length
    && !Object.keys(state.pages).length && !Object.keys(state.sort).length
    && !state.scrollY;
}

function savePlace() {
  var state = collectPlace();
  writeStore(placeKey(), placeIsEmpty(state) ? null : JSON.stringify(state));
}

// Saved a moment after the change rather than on every keystroke: typing a PO number is eight
// events and one state worth keeping.
var placeTimer = null;
function savePlaceSoon() {
  if (placeTimer) clearTimeout(placeTimer);
  placeTimer = setTimeout(savePlace, 300);
}

document.addEventListener('input', savePlaceSoon);
document.addEventListener('change', savePlaceSoon);
// Paging, sorting and the reason pills are clicks, and each one runs its own listener first.
document.addEventListener('click', savePlaceSoon);
// The scroll position is only interesting at the moment of leaving, and `pagehide` fires for a
// navigation, a reload and a tab close alike.
window.addEventListener('pagehide', savePlace);

function storedPlace() {
  var raw = readStore(placeKey());
  if (!raw) return null;
  var state = null;
  try { state = JSON.parse(raw); } catch (err) { return null; }
  if (!state || !state.savedAt || Date.now() - state.savedAt > PLACE_TTL_MS) return null;
  return state;
}

function restoreFilters(state) {
  document.querySelectorAll('input.filter').forEach(function (input) {
    var saved = state.filters[input.getAttribute('data-filter') || ''];
    if (saved) input.value = saved;
  });
  var ranges = {};
  document.querySelectorAll('input.date-from, input.date-to').forEach(function (input) {
    var from = input.classList.contains('date-from');
    var targets = input.getAttribute(from ? 'data-from' : 'data-to') || '';
    var saved = state.dates[(from ? 'from ' : 'to ') + targets];
    if (saved) { input.value = saved; ranges[targets] = true; }
  });
  // A restored range with no chip lit would leave the group claiming no range while one is hiding
  // two thirds of the table. `null` is the Custom segment, which is what a remembered range is.
  Object.keys(ranges).forEach(function (targets) { markChips(targets, null); });
  document.querySelectorAll('select.choice-filter').forEach(function (select) {
    var saved = state.choice[select.getAttribute('data-choice-for') || ''];
    if (saved) select.value = saved;
  });
  Object.keys(state.reason).forEach(function (targets) {
    document.querySelectorAll('.pill[data-reason-for="' + targets + '"]').forEach(function (pill) {
      var on = pill.getAttribute('data-reason') === state.reason[targets];
      pill.classList.toggle('sel', on);
      pill.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  });
}

// Sort before page, because sorting is what decides which rows page two holds.
function restorePaging(state) {
  Object.keys(state.sort).forEach(function (id) {
    var how = state.sort[id];
    if (how && typeof how.column === 'number') sortBy(id, how.column, how.direction);
  });
  Object.keys(state.pages).forEach(function (id) { pageOf[id] = state.pages[id]; });
}

// Beside the count, and only when something was restored. A filter the person did not just type is
// hiding rows, and the difference between "nothing matched" and "nothing is here" has to be one
// press away.
function offerClearFilters() {
  document.querySelectorAll('.filter-bar').forEach(function (bar) {
    if (bar.querySelector('.place-clear')) return;
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'place-clear';
    button.textContent = 'Clear filters';
    button.title = 'This page was left filtered. Clear it and show everything.';
    bar.appendChild(button);
  });
}

function clearPlace() {
  document.querySelectorAll('input.filter').forEach(function (input) { input.value = ''; });
  var ranges = {};
  document.querySelectorAll('input.date-from, input.date-to').forEach(function (input) {
    var from = input.classList.contains('date-from');
    ranges[input.getAttribute(from ? 'data-from' : 'data-to') || ''] = true;
    input.value = '';
  });
  Object.keys(ranges).forEach(function (targets) { markChips(targets, ''); });
  document.querySelectorAll('select.choice-filter').forEach(function (select) { select.value = ''; });
  document.querySelectorAll('.pill.sel[data-reason-for]').forEach(function (pill) {
    pill.classList.remove('sel');
    pill.setAttribute('aria-pressed', 'false');
  });
  Object.keys(pageOf).forEach(function (id) { pageOf[id] = 1; });
  writeStore(placeKey(), null);
  document.querySelectorAll('.place-clear').forEach(function (button) { button.remove(); });
  refreshEveryView();
}

document.addEventListener('click', function (e) {
  if (e.target.closest && e.target.closest('.place-clear')) clearPlace();
});

// Puts the page back the way it was left. Returns whether it did, because the caller repaints once
// either way and doing it twice is the paged table flashing every row it has.
function restorePlace() {
  var state = storedPlace();
  if (!state) return false;
  restoreFilters(state);
  restorePaging(state);
  refreshEveryView();
  if (Object.keys(state.filters).length || Object.keys(state.dates).length
      || Object.keys(state.choice).length || Object.keys(state.reason).length) {
    offerClearFilters();
  }
  window.scrollTo(0, state.scrollY || 0);
  return true;
}

// Where this page sits in the tab's history, and what was on the entries before it.
//
// This app has always used a real link rather than `history.back()`, for a reason that still
// holds: a refused form is a POST landing on the form's OWN url, so one step back is the form
// again rather than the queue behind it. The trail below is what turns the shortcut from an
// assumption into something checkable — a step back is taken only when the previous entry is
// provably the link's destination, and every other case follows the href exactly as before.
//
// The index is stamped on the history entry itself, so a page the browser restores by Back keeps
// the one it was given rather than being counted as a new visit.
function trackPlace() {
  var here = location.pathname + location.search;
  var state = null;
  try { state = history.state; } catch (err) { state = null; }
  if (state && state.premierNav) {
    navIndex = state.premierNav;
  } else {
    navIndex = (parseInt(readStore(NAV_KEY), 10) || 0) + 1;
    writeStore(NAV_KEY, String(navIndex));
    try { history.replaceState({premierNav: navIndex}, ''); } catch (err) { /* no history api */ }
  }
  writeStore(TRAIL_PREFIX + navIndex, here);
}

// The entry immediately before this one: the only thing `history.back()` can be relied on to reach.
function entryBefore() {
  return navIndex > 1 ? readStore(TRAIL_PREFIX + (navIndex - 1)) : null;
}

// The last page that was not this one, which is where an action should return to. Not the same as
// `entryBefore`: a refusal re-renders this url, so the entry right before can be this very form.
function pageBefore() {
  for (var back = navIndex - 1; back > 0 && back > navIndex - 12; back--) {
    var was = readStore(TRAIL_PREFIX + back);
    if (was && was.split('?')[0] !== location.pathname) return was;
  }
  return null;
}

// A history step when the entry behind us really is this link's destination: the browser then hands
// back the page it already has, filters, scroll and all, with no request at all. Otherwise the href
// does its ordinary job and `restorePlace()` puts the filters back on arrival.
document.addEventListener('click', function (e) {
  var link = e.target.closest && e.target.closest('a[data-back-to]');
  if (!link) return;
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
  if (link.getAttribute('data-back-to') !== entryBefore() || history.length < 2) return;
  e.preventDefault();
  savePlace();
  history.back();
});

// Where a POST should send them afterwards, filled in as it leaves. The server validates it — a
// page cannot be trusted to name its own redirect — and falls back to today's landing page.
document.addEventListener('submit', function (e) {
  var form = e.target;
  var field = form.querySelector && form.querySelector('input[name="return_to"]');
  if (field && !field.value) field.value = pageBefore() || '';
  var said = form.getAttribute('data-done');
  if (said) writeStore(TOAST_KEY, said);
});

// What just happened, said once on the page they land on. It rides in storage rather than the query
// string so the address bar stays clean and a reload does not repeat it.
function showToast() {
  var said = readStore(TOAST_KEY);
  if (!said || !document.body) return;
  writeStore(TOAST_KEY, null);
  var box = document.createElement('div');
  box.className = 'toast';
  box.setAttribute('role', 'status');
  box.textContent = said;
  document.body.appendChild(box);
  setTimeout(function () { box.remove(); }, 6000);
}

// The first paint. Without it a paged table renders every row until someone touches a control,
// which is exactly the state paging exists to avoid.
//
// Preset chips are lit here too, for the same reason: a refused submission comes back carrying what
// was typed, and chips that ignored it would show nothing selected above a box that already holds
// two of their sentences.
function firstPaint() {
  trackPlace();
  // `restorePlace` repaints when it has something to put back, so this does not repaint twice.
  if (!restorePlace()) refreshEveryView();
  document.querySelectorAll('textarea.fld').forEach(markPresets);
  showToast();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', firstPaint);
} else {
  firstPaint();
}

// New mail, without anyone pressing F5. These pages are server-rendered and had no client-side
// data fetching at all, so a row could sit in the database for twenty minutes while the tab
// showing it stayed blank. /ui/version is a two-integer read and touches no Graph API, which is
// what makes a ten-second poll affordable.
//
// The page reloads *itself* only when doing so cannot interrupt anyone: scrolled to the top, no
// message dialog open, tab in the foreground, and no ingest run in flight. Otherwise the pill
// waits to be clicked. Reloading under someone reading a POD is worse than being slightly out of
// date — and during a run the token moves on nearly every poll, so "reload when it moved" would
// mean reloading for the whole run.
(function () {
  var POLL_MS = 10000;

  // **Deferred, because this script runs in `<head>`.**
  //
  // It read `document.body` at parse time, where `body` does not exist yet — so `baseline` was
  // always null, the guard below returned, and `setInterval` was never reached. Measured in real
  // Chrome over CDP: **zero polls in twenty-five seconds.** This poller had therefore never run,
  // which means the "New mail — click to load" pill could never appear either.
  //
  // It went unnoticed because the one page that visibly depended on staying current had a
  // a meta-refresh tag doing the job instead. Remove that refresh — as the two-second
  // reload had to be — and the page simply froze, still claiming to be working long after the run
  // had finished. Two bugs that concealed each other.
  //
  // The rest of `_JS` already waits for the DOM (see `refreshEveryView` below); only this block
  // did not.
  function start() {
    var body = document.body;
    var baseline = body && body.getAttribute('data-version');
    if (!baseline) return;
    setUp(baseline, body.getAttribute('data-running') === '1');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }

  function setUp(baseline, startedRunning) {

  // Seeded from the server render, not from the first poll. A page opened during a run and then
  // left in a background tab polls nothing while it is hidden — so if the run finishes in that
  // time, the first poll on return would see `running:false` with nothing to compare against and
  // conclude no transition had happened. The page would sit reading "Running now..." for a run
  // that ended long before. Starting from what the server actually rendered closes that hole.
  var wasRunning = startedRunning;

  function pill() { return document.getElementById('live-pill'); }

  function offerPill() {
    var p = pill();
    if (p) p.hidden = false;
  }

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
        if (!v) return;

        var running = v.running === true;
        var justFinished = wasRunning && !running;
        wasRunning = running;

        // A run that has just ended, handled BEFORE the token is even looked at.
        //
        // Gating this on the token would reintroduce the bug it is here to fix: the token is
        // `boot:newest_email_log_id:count:run_id:arrivals`, and a run that read nothing new moves
        // none of those — `start_run` inserted the row (so `run_id` was already in the baseline
        // this page rendered with) and `finish_run` only updates it. So a quiet run can begin and
        // end with an identical token, and a page waiting for the token to move would show
        // "Running now..." for ever. Observed exactly that: run 1085 finished at 17:31:07 and the
        // page still claimed it was running at 17:49.
        //
        // This is the one automatic reload left, and it is an edge rather than a level: it fires
        // once, on the true -> false transition, not for as long as the run is over.
        if (justFinished) {
          if (safeToReloadWithoutAsking()) { location.reload(); return; }
          offerPill();      // mid-read: let them choose the moment, same as for new mail
          return;
        }

        if (v.token === baseline || !v.token || v.token === 'unavailable') return;

        // While a run is in flight, never reload on our own — offer the pill instead. The token
        // carries email_log's count and highest id, so during a pass it changes on almost every
        // poll: auto-reloading here would put the page back to reloading every ten seconds, which
        // is the behaviour removed from /ui/automation (where it was a two-second <meta refresh>
        // that ignored all of the etiquette above).
        if (running) { offerPill(); return; }

        if (safeToReloadWithoutAsking()) { location.reload(); return; }
        offerPill();
      })
      .catch(function () { /* a failed poll must never break the page */ });
  }

  document.addEventListener('click', function (e) {
    if (e.target.closest('#live-pill')) location.reload();
  });

    setInterval(check, POLL_MS);
  }
})();

// The progress ring, advancing while a run is in flight.
//
// This is the half the removed meta-refresh tag used to do, without the half that made it
// unacceptable. That tag reloaded the whole document every two seconds for the length of a run —
// hundreds of reloads, each throwing away scroll position, open dialogs and table filters, and it
// went on doing so for twelve hours at a stretch when a run wedged in Graph auth. So the poller
// above is deliberately forbidden from reloading while `running` is true.
//
// Patching four bits of text in place has none of those costs. Nothing is thrown away because
// nothing is replaced: no reload, no scroll reset, no dialog closed, no filter lost. The etiquette
// `safeToReloadWithoutAsking()` has to work out does not apply, because there is nothing to ask
// about.
//
// It talks to `/ui/run-progress`, not `/ui/version`: that endpoint reads two SQLite databases and
// runs an anti-join, which is affordable at ten seconds and not at two. This one reads a dict in
// the server's memory.
(function () {
  var TICK_MS = 2000;

  function el(name) { return document.querySelector('[data-' + name + ']'); }

  function paint(p) {
    var block = el('prog');
    if (!block) return;
    block.hidden = !p.running;
    var stopWhenIdle = el('run-stop');
    if (stopWhenIdle && !p.running) {
      var idleBtn = stopWhenIdle.querySelector('button');
      if (idleBtn) {
        idleBtn.disabled = true;
        var idleLbl = idleBtn.querySelector('.lbl');
        if (idleLbl) idleLbl.textContent = 'Stop this run';
      }
    }
    if (!p.running) return;

    // The card AROUND the ring, not just the ring. A page drawn while nothing was running says
    // "Last run 58 minutes ago" over `0 emails read`; showing a live ring inside that card without
    // touching it produced a screen claiming both at once — a run at 100% above the words "last run
    // 58 minutes ago". The server already renders exactly this state when it draws during a run
    // (headline, warn tone, and `—` for figures nobody can know yet), so this brings the page to
    // the state the server would have rendered, rather than inventing a third one.
    //
    // Only ever on the way IN. When the run ends, the ten-second poller's `justFinished` edge is
    // what restores the page — it reloads when that is not rude and offers the pill when it is,
    // which is a judgement this ticker has no business making. Cost: up to ten seconds where the
    // ring is gone but the headline still reads "Running now…".
    var card = block.closest('.card');
    if (card) {
      card.className = 'card warn';
      var head = card.querySelector('h2');
      if (head) head.textContent = 'Running now\\u2026';
      var detail = card.querySelector('[data-run-detail]');
      if (detail) {
        detail.textContent = 'The automation is working through the mail. You can leave this '
          + 'page \\u2014 it keeps going, and the progress above advances as it does.';
      }
      // Every figure in this strip comes from a run row inserted BEFORE the pass, with each count
      // defaulting to zero. Mid-run they are not small numbers, they are unknown ones.
      card.querySelectorAll('.statrow b').forEach(function (b) { b.textContent = '\\u2014'; });
    }

    var ring = el('prog-ring');
    var pct = el('prog-pct');
    if (ring) {
      if (p.total > 0) {
        var value = Math.max(0, Math.min(100, Math.round(p.done * 100 / p.total)));
        ring.classList.remove('spin');
        ring.style.setProperty('--pct', value);
        if (pct) pct.textContent = value + '%';
      } else {
        // No denominator yet — reading the mailbox is one call with no interior milestones, and a
        // determinate ring frozen at 0% reads as stalled. Spin instead, exactly as the server-
        // rendered form does.
        ring.classList.add('spin');
        ring.style.setProperty('--pct', 0);
        if (pct) pct.textContent = '';
      }
    }
    var what = el('prog-what');
    if (what) what.textContent = p.label || 'Working';
    var detail = el('prog-detail');
    if (detail) {
      var text = p.total > 0 ? p.done + ' of ' + p.total
               : (p.done > 0 ? p.done + ' mailbox call' + (p.done === 1 ? '' : 's') + ' so far'
                             : 'connecting to the mailbox');
      if (p.note) text += ' \\u00b7 ' + p.note;
      if (p.elapsed_seconds !== null && p.elapsed_seconds !== undefined) {
        text += ' \\u00b7 ' + elapsed(p.elapsed_seconds);
      }
      // Only once it is worth saying. A few seconds between calls is normal; a figure that keeps
      // climbing is how a stuck run shows itself before the watchdog steps in.
      if (p.silent_seconds !== null && p.silent_seconds !== undefined && p.silent_seconds >= 30) {
        text += ' \\u00b7 no activity for ' + elapsed(p.silent_seconds).replace('running ', '');
      }
      if (p.stop_requested) text = 'stopping \\u2014 finishing the call it is in \\u00b7 ' + text;
      detail.textContent = text;
    }
    var stop = el('run-stop');
    if (stop) {
      var btn = stop.querySelector('button');
      if (btn) {
        btn.disabled = !!p.stop_requested;
        var lbl = btn.querySelector('.lbl');
        if (lbl) lbl.textContent = p.stop_requested ? 'Stopping\\u2026' : 'Stop this run';
      }
    }
  }

  // Server-computed seconds, formatted here. The count comes from the server because the browser's
  // clock is not ours, and "running for 2 minutes" off a skewed clock is worse than no number.
  function elapsed(seconds) {
    if (seconds < 90) return 'running ' + seconds + 's';
    var mins = Math.floor(seconds / 60);
    if (mins < 90) return 'running ' + mins + ' min';
    var hours = Math.floor(seconds / 3600);
    return 'running ' + hours + (hours === 1 ? ' hour' : ' hours');
  }

  function tick() {
    // A background tab advances nothing. Nobody is looking, and the ring is repainted from the
    // next poll the moment it comes back.
    if (document.visibilityState !== 'visible') return;
    fetch('/ui/run-progress', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(paint)
      .catch(function () { /* a failed tick must never break the page */ });
  }

  function start() {
    // Only on a page that carries the block. Every other page would be polling for something it
    // has nowhere to display.
    if (!el('prog')) return;
    setInterval(tick, TICK_MS);
    tick();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
"""

# Hidden until the poller has something to say. Rendered by the shell so every page gets it
# without asking, and `aria-live` so it is announced rather than only seen.
_LIVE_PILL = (
    '<button id="live-pill" class="live-pill" hidden aria-live="polite" '
    'title="Reload to show the new mail">New mail — click to load</button>'
)

# Shipped hidden on every page and revealed by the script when a navigation starts. Rendered rather
# than created in JS so there is nothing to build on the click — the whole point is that the
# feedback lands in the same frame as the press.
#
# `aria-hidden`: the lit nav entry is what a screen reader should hear, and a bar that reports no
# progress has nothing to announce.
_LOAD_BAR = '<div class="load-bar" hidden aria-hidden="true"></div>'

_LOGIN_CSS = """
  /* Only the centring and the two things the rest of the app has no equivalent of. The card, the
     fields and the button are `.card`, `.field`/`.fld` and `.btn.primary`, which already exist —
     a login screen that invented its own would drift away from the app it fronts. */
  .login-wrap { min-height:100vh; display:flex; align-items:center; justify-content:center;
                padding:24px; box-sizing:border-box; }
  .login-card { width:100%; max-width:340px; }
  .login-card h1 { font-size:1.1rem; margin:0 0 2px; }
  .login-card .sub { color:var(--muted); font-size:.82rem; margin:0 0 16px; }
  /* `button.btn`, not `button`. The bare selector also matched the reveal button inside the
     password box and beat `.reveal` on specificity, so the eye was stretched to the full
     300px of the field and its flex centring parked it in the middle of the text. */
  .login-card button.btn { width:100%; margin-top:4px; }
  .login-error { border-left:3px solid var(--danger); background:var(--surface);
                 padding:8px 10px; margin:0 0 14px; font-size:.82rem; }
  .login-note { color:var(--muted); font-size:.75rem; margin:14px 0 0; line-height:1.45; }
"""

_LOGIN_MESSAGES = {
    # Keyed by a flag, never by text from the query string: a message taken from the caller is a
    # sentence an attacker gets to write onto Premier's login page and mail to somebody.
    "1": "That username and password do not match.",
    "setup": "Check the username, and that both passwords match and run to 12 characters.",
    "reset": "Check the username exists, and that both passwords match and run to 12 characters.",
}


_EYE_ON = _icon('<path d="M1 8s2.6-4.5 7-4.5S15 8 15 8s-2.6 4.5-7 4.5S1 8 1 8z"/>'
                '<circle cx="8" cy="8" r="1.9"/>')

_EYE_OFF = _icon('<path d="M1 8s2.6-4.5 7-4.5c1 0 1.9.2 2.7.5"/>'
                 '<path d="M13.3 6.1c1.1 1 1.7 1.9 1.7 1.9s-2.6 4.5-7 4.5c-1.3 0-2.4-.4-3.3-.9"/>'
                 '<path d="M2 14 14 2"/>')


def password_field(name: str, label: str, *, autocomplete: str = "current-password") -> Raw:
    """A password box with an eye button that shows what was typed.

    Built here rather than as `field(kind="password")` because the button has to sit *inside* the
    box, which means the control needs a positioned wrapper of its own -- and `field()` is used by
    every other form in the app, none of which wants one.

    The button is a real `<button type="button">`: `type` matters, because a button inside a form
    submits it by default, and an eye that signs you in on the way past is worse than no eye. It
    carries `aria-pressed` so a screen reader announces the state rather than just the press, and
    both icons ship in the markup with CSS choosing between them, so toggling costs no redraw and
    no second request.

    What it does *not* do is remember the choice. Revealing a password is a deliberate act for the
    moment you are checking a typo, and a page that came back with the password already showing --
    because you revealed one three days ago -- is a page that shows it to whoever is behind you.
    """
    control_id = f"f-{name}"
    return tag(
        "div",
        tag("label", label, tag("span", " *", class_="req"), for_=control_id),
        tag("div",
            tag("input", type="password", name_=name, id=control_id, required="required",
                autocomplete=autocomplete, class_="fld"),
            tag("button",
                tag("span", _EYE_ON, class_="eye-on"),
                tag("span", _EYE_OFF, class_="eye-off"),
                type="button", class_="reveal", data_reveal=control_id,
                aria_controls=control_id, aria_pressed="false",
                aria_label=f"Show {label.lower()}", title=f"Show {label.lower()}"),
            class_="fld-wrap"),
        class_="field")


_REVEAL_CSS = """
  .fld-wrap { position:relative; display:block; }
  .fld-wrap .fld { width:100%; box-sizing:border-box; padding-right:38px; }
  .reveal { position:absolute; top:1px; right:1px; bottom:1px; width:34px; display:flex;
            align-items:center; justify-content:center; padding:0; border:0; cursor:pointer;
            background:none; color:var(--muted); border-radius:0 5px 5px 0;
            min-width:34px; max-width:34px; flex:none; }
  .reveal:hover { color:var(--fg); }
  .reveal:focus-visible { outline:2px solid var(--accent); outline-offset:-2px; }
  .reveal .eye-on, .reveal .eye-off { display:flex; }
  .reveal .eye-off { display:none; }
  .reveal[aria-pressed="true"] .eye-on { display:none; }
  .reveal[aria-pressed="true"] .eye-off { display:flex; }
"""

_LOGIN_JS = """
// The eye button, and nothing else. This page deliberately does not load `_JS`: that script polls
// /ui/version every ten seconds against a route a signed-out visitor is refused, which is a
// redirect loop wearing a heartbeat's clothes. So this is its own handful of lines.
//
// Delegated from the document rather than bound per button, and `addEventListener` rather than an
// `onclick` attribute -- inline handlers are forbidden throughout this app, and a login page is
// the last place to make an exception.
document.addEventListener('click', function (event) {
  var button = event.target.closest && event.target.closest('button.reveal');
  if (!button) return;
  var input = document.getElementById(button.getAttribute('data-reveal'));
  if (!input) return;
  var showing = input.type === 'password';
  input.type = showing ? 'text' : 'password';
  button.setAttribute('aria-pressed', showing ? 'true' : 'false');
  var what = (button.getAttribute('aria-label') || '').replace(/^(Show|Hide) /, '');
  button.setAttribute('aria-label', (showing ? 'Hide ' : 'Show ') + what);
  button.setAttribute('title', (showing ? 'Hide ' : 'Show ') + what);
  // Back to the box with the caret where it was, so revealing mid-word does not cost the typist
  // their place.
  var at = input.value.length;
  input.focus();
  try { input.setSelectionRange(at, at); } catch (e) { /* type=password may refuse */ }
});
"""


def login_page(*, next: str = "/ui", error: str = "", first_run: bool = False,
               can_set_up: bool = False, resetting: bool = False,
               can_reset: bool = False) -> str:
    """The sign-in screen. The only page in the app that is not built by `page()`.

    Three things it deliberately does without.

    **No `<script>`.** The form posts natively, so nothing here needs one — and `_JS` would start
    the ten-second `/ui/version` poll against a route this visitor is not allowed to call, which is
    a redirect loop dressed up as a heartbeat. It also keeps the app's one inline script behind the
    gate, where `test_pages_carry_exactly_one_first_party_script_and_nothing_else` guards it.

    **No sidebar and no stat strip.** Both come from `_chrome(conn)`, which reads the queue and the
    ledger. Drawing those counts for somebody who has not signed in would publish the shape of
    Premier's mailbox — how much is waiting, how much was routed — to anyone who can reach the port.

    **No way to tell a bad username from a bad password.** `error` is a flag and prints one fixed
    line from `_LOGIN_MESSAGES`; the caller never supplies the words.

    `can_set_up` draws the create-the-first-account form instead. It is passed only when the store
    is empty *and* the request came from loopback, and `api.login` re-checks both before it writes
    anything — this decides which form is drawn, never what is allowed.
    """
    if resetting:
        heading, subtitle = "Reset the password", \
            "Offered only on the machine serving this app."
        action, verb = "/login/reset", "Set new password"
        note = ("At least 12 characters. The old password is not needed and could not be checked "
                "anyway — only a PBKDF2-SHA256 hash of it is stored, and that is not reversible. "
                "Every signed-in session is ended.")
    elif can_set_up:
        heading, subtitle = "Create the first account", \
            "No account exists yet. This form is offered only on this machine."
        action, verb = "/login/set-up", "Create account"
        note = "At least 12 characters. Stored only as a PBKDF2-SHA256 hash, never as text."
    else:
        heading, subtitle = "Premier Receiver", "Sign in to continue."
        action, verb = "/login", "Sign in"
        note = ("No account exists yet. Run tools/create_admin.py on the machine serving this "
                "app to create one." if first_run else
                "Accounts are created with tools/create_admin.py.")

    entering = "new-password" if (can_set_up or resetting) else "current-password"
    fields = [field("username", "Username", required=True),
              password_field("password", "Password", autocomplete=entering)]
    if can_set_up or resetting:
        fields.append(password_field("confirm", "Repeat password", autocomplete="new-password"))
    else:
        # Carried in the form rather than left in the URL, so submitting cannot drop it.
        fields.append(tag("input", type="hidden", name_="next", value=next or "/ui"))

    return (
        _DOCTYPE
        + _VIEWPORT
        + str(tag("title", "Sign in · Premier Receiver"))
        + "<style>" + _CSS + "</style>"
        + "<style>" + _LOGIN_CSS + _REVEAL_CSS + "</style>"
        + "<script>" + _LOGIN_JS + "</script>"
        + "</head><body>"
        + str(tag("div",
                  tag("div",
                      tag("h1", heading),
                      tag("p", subtitle, class_="sub"),
                      (tag("p", _LOGIN_MESSAGES.get(error, _LOGIN_MESSAGES["1"]),
                           class_="login-error") if error else Raw("")),
                      tag("form", *fields,
                          tag("button", verb, type="submit", class_="btn primary"),
                          method="post", action=action),
                      # Only where `api.login` would actually honour it. Drawn off this machine it
                      # would be a link to a route that silently does nothing, which reads as the
                      # app being broken rather than as the guard working.
                      (tag("p", tag("a", "Forgotten the password?", href="/login?reset=1"),
                           class_="login-note") if can_reset and not resetting else Raw("")),
                      (tag("p", tag("a", "Back to sign in", href="/login"),
                           class_="login-note") if resetting else Raw("")),
                      tag("p", note, class_="login-note"),
                      class_="login-card card"),
                  class_="login-wrap"))
        + "</body></html>"
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


def _sidebar(active: str, counts: Optional[dict] = None, last_run: str = "",
             user: str = "") -> Raw:
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
    # `last_run` used to be a `<footer>` under the table, where its 40px of padding cost a whole row
    # of every grid on every page to carry a line that changes a few times a day. The rail has
    # vertical space it was never going to use.
    foot = tag("div", _kill_control(),
               tag("span", last_run, class_="last-run") if last_run else Raw(""),
               _signed_in_control(user),
               tag("span", "Powered by Spitfire", class_="powered"),
               class_="side-foot")
    return tag("aside", brand, _side_alert(counts), tag("nav", *groups, class_="nav"), foot,
               class_="side")


def _signed_in_control(user: str) -> Raw:
    """Who you are, and the way out.

    A `<form method="post">`, not a link. A GET that signs you out is triggered by anything that
    fetches a URL without being asked -- a chat client unfurling a pasted link, a browser
    prefetching what it thinks you will click -- and being signed out at random reads as the app
    being broken.

    No inline handler and no JavaScript: the button submits the form it is in, which is also what
    `test_pages_carry_exactly_one_first_party_script_and_nothing_else` requires of everything here.
    """
    if not user:
        return Raw("")
    return tag("form",
               tag("span", f"Signed in as {user}", class_="who"),
               tag("button", "Sign out", type="submit", class_="sign-out"),
               method="post", action="/logout", class_="signed-in")


def _side_alert(counts: Optional[dict] = None) -> Raw:
    """The queue, said once at the top of the rail instead of only as a badge halfway down it.

    The number is not fetched here — `routes._chrome()` already puts `needs_human` into `counts`
    for the nav badge, and this reads the same value. A second count would be a second query per
    page render for a figure the page is holding.

    Silent at zero, like the badge it doubles: an empty queue is not news, and a card that reads
    "0 waiting" is a permanent alarm nobody can clear.

    It links to `/ui/manual` but never carries `class="on"`, even on that page — the nav entry
    below is the one thing marking where you are, and
    `test_every_page_lights_exactly_one_nav_entry` counts that string across the whole document.
    """
    waiting = (counts or {}).get("/ui/manual")
    if not waiting:
        return Raw("")
    return tag(
        "a",
        tag("span", "!", class_="side-alert-ico", aria_hidden="true"),
        tag("span",
            tag("b", "Needs a human"),
            tag("span", f"{waiting} waiting"),
            class_="lbl"),
        tag("span", waiting, class_="count"),
        href="/ui/manual", class_="side-alert",
        # Carries the whole message when the rail is collapsed to the glyph.
        title=f"{waiting} waiting for a person",
    )


def page(title: str, active: str, *sections, header: Optional[Raw] = None, footer: str = "",
         user: str = "",
         subtitle: str = "", actions: Sequence = (), counts: Optional[dict] = None,
         refresh_seconds: int = 0, back: str = "", back_label: str = "") -> str:
    """The one shell every page goes through.

    `active` is matched against the sidebar's hrefs, so a detail page passes its parent
    (`/ui/po/908491` passes `/ui/po`) and the parent stays lit. `counts` maps an href to a number
    for the badge beside it; only truthy values render, so a queue at zero shows nothing rather
    than a reassuring "0" the eye still has to read.

    `back` is for a page you go *into* rather than navigate to — a form opened from one row of a
    queue. The sidebar cannot serve as the way out of those: the entry that would take you back is
    the one already lit, so it reads as where you are rather than as somewhere to go. A form's
    Cancel is the other half of this and not a substitute — it sits below the last field, so on a
    long form the way out is only reachable by scrolling past everything you did not want to fill
    in. This one is on screen the moment you land.

    `back_label` names the destination for the tooltip and the accessible name. The link itself
    always reads "Back".

    `actions` are page-level controls, rendered at the right of the title row. A control that acts
    on the whole page belongs beside its title, not part-way down the body: `section(action=...)`
    is the equivalent one level in, for something that acts on one section.

    `footer` is one line of standing status — when the pipeline last ran — and it goes in the
    **sidebar**, not at the bottom of the page. It was a `<footer>` under the table until
    2026-08-24, where its padding cost 58px of every screen: on a 642px laptop that is a whole row
    of a grid whose entire job is to show rows. The name is kept because ten call sites pass it
    through `routes._chrome()`.

    `refresh_seconds` emits a `<meta http-equiv="refresh">`, and is meant for one thing: a page
    showing a run in progress, so it redraws itself as the run finishes. It is a meta tag rather
    than a poll in `_JS` because this app spends exactly one inline script and no external ones —
    `test_pages_carry_exactly_one_first_party_script_and_nothing_else` holds that line, and a
    progress refresh is not what that budget is for. **The caller must pass 0 once the run ends**,
    or every page quietly reloads itself for ever.
    """
    # A real `<a href>`, not `history.back()`: a refusal re-render is a POST landing on this same
    # URL, so "one step back" is the form again rather than the queue. The destination is named by
    # the caller, and it is the same one the form's Cancel goes to.
    # Reads "Back". `back_label` names the destination in the tooltip and for a screen reader only:
    # the control sits at the top-left of a page you went into, which is the one place a bare
    # "Back" needs no explaining, and spelling the destination out competes with the title beside
    # it for the same glance.
    back_link = (tag("a", Raw("&lsaquo;&nbsp;"), "Back", href=back, class_="back-link",
                     title=f"Back to {back_label}" if back_label else "Back",
                     aria_label=f"Back to {back_label}" if back_label else None,
                     # Read by the script: when the page behind this one *is* `back`, it steps back
                     # through history, which restores that page as it was rather than re-fetching
                     # it with the filter cleared. The href stands for everyone else.
                     data_back_to=back)
                 if back else Raw(""))
    # Title and subtitle stack; the actions sit at the far right. They were siblings in one flex
    # row before, which put the subtitle *beside* the title and left nowhere for a page-level
    # control to go except buried in the body — which is where Mail's "Check now" was.
    titles = tag("div", tag("h1", title),
                 tag("p", subtitle, class_="sub") if subtitle else Raw(""),
                 class_="bar-text")
    head_children = [tag("div", back_link, titles,
                         Raw(str(_run_form()) if active in _CORPUS_PAGES else ""),
                         tag("div", *actions, class_="bar-actions") if actions else Raw(""),
                         class_="bar")]
    if header is not None:
        head_children.append(header)

    content = tag(
        "div",
        tag("header", *head_children),
        _kill_banner(),
        tag("main", *sections),
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
        + f'<body data-version="{esc(_version_token())}" data-running="{_running_flag()}">'
        + _LOAD_BAR
        + str(tag("div", _sidebar(active, counts, footer, user), content, class_="shell"))
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


def _running_flag() -> str:
    """`"1"` while an ingest run is in flight, stamped on `<body>` for the poller to start from.

    The poller needs to spot the moment a run *ends*, which is an edge and not a state — so it has
    to know what was true when the page was drawn. Reading it from the first poll instead leaves a
    real hole: a page opened during a run and then left in a background tab polls nothing while
    hidden, so a run finishing in that window would have no transition to detect on return, and the
    page would keep claiming to be running indefinitely.

    Never raises, and answers `"0"` if it cannot tell. A page renderer must not fail over a hint,
    and `"0"` only costs a reload that does not happen — whereas raising would cost the whole page.
    """
    try:
        from operations import runner

        return "1" if runner.is_running() else "0"
    except Exception:                                              # noqa: BLE001
        return "0"


def _run_form() -> Raw:
    # Names the corpus explicitly: these pages also show mail read from the live mailbox, and a
    # button labelled "run pipeline" would read as though it polls that too.
    return tag("form", tag("button", "Re-run .msg corpus (mock OCR)", type="submit"),
               class_="run", method="post", action="/ui/run")
