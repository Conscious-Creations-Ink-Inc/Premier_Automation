"""The receiver report: what the delivery emails actually told us, laid out like Spitfire's own
Receipt Log so the two can be read side by side.

One `build()` produces the structure; `to_html()` and `to_xlsx()` render it. Both renderers walk
the same objects, so the on-screen preview cannot drift from the downloaded file.

**The three empty columns are not one problem — they are three different ones.** Measured against
Premier's own export (`dev_reports/SampleReceiverFiles/_Spitfire-LIVE_General_Receipt Log.xlsx`,
486 line rows):

* **Order Qty** lives on the purchase order inside Spitfire. No delivery email carries it. It stays
  empty rather than zero-filled or guessed: a sheet in this layout reads as authoritative, and an
  invented order quantity is worse than a gap. This is the one thing blocked on the Spitfire read.
* **Net** is `Order Qty - Received`, and that held for all 486 rows with no exceptions. It needs no
  source of its own, so it is computed here and lights up the moment Order Qty arrives.
* **Final** is a flag a person sets in Spitfire, and it is *not derivable* — do not try. In the
  reference, 118 fully-received lines carry no `*` and 5 partially-received lines do. Any rule
  based on received-versus-ordered would therefore be wrong 123 times in 486. It stays blank even
  after Order Qty arrives.

The group row above each PO is Spitfire's **project code** in Premier's export
(`PRJ-001-PB-1-00002 : ...`). We do not have it — it comes from the PO — so that row carries the
PO's own title until `pipeline/spitfire_mirror.py` can supply the real one.

The grouping — PO, then line, then the receipts underneath — is a first pass that lives here
because the report needs it today. `pipeline/stage4_match.py` is where real matching belongs, and
this should be replaced by it rather than grown into it.

It lives in `pipeline/` rather than beside the interface because the pipeline and the UI both reach
for it, and `pipeline/` is the only place both may import from. That is also why `to_html()` returns
a plain `str` escaped with the standard library rather than `api.ui.html.Raw`: the caller wraps the
result in its own marker, and this module stays ignorant of it.
"""

import sqlite3
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from io import BytesIO
from typing import List, Optional, Sequence

# Premier's own file reads "Premier Design to Completion Report" in H1 — the word was missing here.
SHEET_TITLE = "Premier Design to Completion Report"
SHEET_SUBTITLE = "Receipt Log"
SHEET_NAME = "Receipt Log"

# Lifted from Premier's own export at full precision, not rounded and not guessed. Excel stores
# widths in fractional character units, so 22.3 and 22.28515625 are different columns — close
# enough to miss by eye, far enough to misalign two files read side by side.
#
# D and G are absent on purpose: the reference does not store them either, leaving both at Excel's
# default. Note that openpyxl's `column_dimensions` is a defaultdict — reading a missing column
# invents one at width 13.0 — so check membership, never truthiness, when comparing against it.
_COL_WIDTHS = {"A": 12.0, "B": 22.28515625, "C": 6.85546875, "E": 24.0, "F": 12.0,
               "H": 1.42578125, "I": 13.42578125, "J": 5.140625, "K": 20.5703125,
               "L": 0.42578125}
_DATE_FORMAT = "[$-10409]mm/dd/yyyy"
_HEADERS = [(1, "DocNo"), (2, "Vendor"), (3, "Line"), (4, "Description"),
            (6, "Order Qty"), (7, "Received "), (9, "Net"), (10, "Final "), (11, "Receiver")]


@dataclass
class Receipt:
    reference: str
    date: str
    quantity: Optional[float]
    receiver: str
    source_email: str = ""


@dataclass
class Line:
    line_number: Optional[int]
    spec: str
    description: str
    received: Optional[float] = None
    uom: str = ""
    package: str = ""
    receipts: List[Receipt] = field(default_factory=list)

    # From the purchase order inside Spitfire, once we can read it. None until then.
    order_qty: Optional[float] = None
    # Not derivable — a flag someone sets in Spitfire. See the module docstring for the counts that
    # rule out computing it from received-versus-ordered.
    final: bool = False

    @property
    def net(self) -> Optional[float]:
        """What is still outstanding on this line: `Order Qty - Received`.

        A property rather than a stored field because it is not an independent fact — it held for
        every one of the 486 line rows in Premier's own export. Stays `None` while `order_qty` is
        None, so the cell renders blank rather than showing the whole received quantity as though
        it were an overage.
        """
        if self.order_qty is None:
            return None
        return self.order_qty - (self.received or 0)

    @property
    def label(self) -> str:
        """Spitfire shows line numbers zero-padded to four digits."""
        if self.line_number is None:
            return ""
        return f"{int(self.line_number):04d}"


@dataclass
class PurchaseOrder:
    po_number: str
    vendor: str
    title: str
    lines: List[Line] = field(default_factory=list)

    @property
    def received_total(self) -> float:
        return sum(l.received or 0 for l in self.lines)


@dataclass
class Report:
    purchase_orders: List[PurchaseOrder] = field(default_factory=list)
    generated_at: str = ""
    empty_message: str = "No purchase orders yet — run the automation first."
    """Why there is nothing to show. The caller knows things this module cannot: which mailbox
    was read, whether it has been read at all, and whether the mail simply was not about
    deliveries. An empty report is usually correct rather than broken, and it should say which."""

    @property
    def line_count(self) -> int:
        return sum(len(po.lines) for po in self.purchase_orders)

    @property
    def receipt_count(self) -> int:
        return sum(len(l.receipts) for po in self.purchase_orders for l in po.lines)

    @property
    def is_empty(self) -> bool:
        return not self.purchase_orders


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

_SELECT = """
    SELECT po_number, po_line_number, spec_code, item_description, vendor_name,
           quantity_received, unit_of_measure, package_quantity, package_uom,
           pod_stated_date, email_date, received_by, notification_number, shipment_number,
           carrier_name, tracking_number, source_email_id, extraction_source,
           COALESCE(origin, 'auto') AS origin, created_by
      FROM extracted_records
     WHERE TRIM(COALESCE(po_number,'')) <> ''
       {record_filter}
     ORDER BY po_number, COALESCE(po_line_number, 999999), spec_code
"""


def _receiver(r) -> str:
    """What goes in the Receiver column — and where a manual record is distinguishable from an
    automated one.

    Premier's own export puts phrases in these cells, not just names: `Vendor Email`,
    `Property confirmed`, `Corina confirmed`, `WH Inventory report`. So a word here is their
    practice rather than our invention, and it is why the origin flag needs **no new column** —
    the report keeps its exact nine-column layout and stays conformant with their template.

    Order: whoever signed for the goods, then whoever entered the record, then how it was made.
    A named person always wins, because that is the more specific truth about who took delivery.
    """
    signed_for_by = (r["received_by"] or "").strip()
    if signed_for_by:
        return signed_for_by
    if str(r["origin"] or "auto") == "manual":
        return (r["created_by"] or "").strip() or "Manual entry"
    return "Automation"


def _fill_from_purchase_orders(conn: sqlite3.Connection, by_po) -> None:
    """Fill Vendor, UOM and Order Qty from the mirrored purchase order — blanks only, never over.

    Three of this sheet's columns describe the *order*, not the delivery, and no email carries
    them. They were blank on every row until `spitfire_po_lines` existed to answer them, and
    `Net` — which is `Order Qty - Received` and held for all 486 rows of Premier's own export —
    was therefore blank too. Filling Order Qty lights Net up on its own.

    **Fallback only.** A value the record already carries is what a reviewer has been looking at
    and is left exactly as it is; this only reaches cells nothing else filled. That is also what
    keeps the change safe: a store with no mirrored lines produces precisely the report it
    produced before.

    `Final` is deliberately not touched. It is a flag a person sets in Spitfire — 118 fully
    received lines in the reference carry no asterisk and 5 partially received ones do, so any
    received-versus-ordered rule would be wrong 123 times in 486.
    """
    if not by_po:
        return
    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" * len(by_po))
        lines = conn.execute(
            f"""SELECT po_number, line_number, spec_code, vendor_name, unit_of_measure, qty_ordered
                  FROM spitfire_po_lines WHERE po_number IN ({placeholders})""",
            list(by_po)).fetchall()
    except sqlite3.OperationalError:
        # A store without the mirror table at all — the demo database, and any caller holding a
        # connection this module did not open. The report is still correct, just less filled in.
        return
    finally:
        conn.row_factory = prior_factory

    by_line, by_spec = {}, {}
    for row in lines:
        po_number = str(row["po_number"]).strip()
        if row["line_number"] is not None:
            by_line[(po_number, int(row["line_number"]))] = row
        spec = (row["spec_code"] or "").strip().upper()
        if spec:
            # First writer wins: two lines can share a spec (a fabric and its tariff surcharge),
            # and picking arbitrarily between them is how a quantity gets attributed to the wrong
            # one. A line number is exact and is tried first below.
            by_spec.setdefault((po_number, spec), row)

    for po in by_po.values():
        for line in po.lines:
            match = None
            if line.line_number is not None:
                match = by_line.get((po.po_number, int(line.line_number)))
            if match is None and line.spec:
                match = by_spec.get((po.po_number, line.spec.strip().upper()))
            if match is None:
                continue
            if line.order_qty is None and match["qty_ordered"] is not None:
                line.order_qty = float(match["qty_ordered"])
            if not line.uom and match["unit_of_measure"]:
                line.uom = str(match["unit_of_measure"]).strip()
            if not po.vendor and match["vendor_name"]:
                po.vendor = str(match["vendor_name"]).strip()


def build(conn: sqlite3.Connection, *, generated_at: Optional[str] = None,
          record_ids: Optional[Sequence[int]] = None) -> Report:
    """Group everything the emails produced into PO -> line -> receipts.

    `record_ids` narrows it to named rows, which is what the Spitfire post attaches: the report
    that goes onto a receipt must describe *that* delivery and nothing else. Attaching the whole
    log would put every other purchase order's quantities on a document Premier reads as evidence
    for one — and a receipt carrying a report about someone else's PO is worse than no report.

    None means the whole store, which is what the Receiver report page has always shown.
    """
    # Rows are read by name below, so the factory is set here rather than assumed of the caller.
    # The console sets it; `api.deps.get_pipeline_conn` does not, and the failure without this is a
    # TypeError deep in the grouping loop rather than anything that names the real cause.
    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    # Placeholders are generated from the id count and the ids are bound, never interpolated —
    # they arrive from a URL path on the post route.
    ids = [int(i) for i in (record_ids or [])]
    clause = f"AND id IN ({','.join('?' * len(ids))})" if ids else ""
    try:
        rows = conn.execute(_SELECT.format(record_filter=clause), ids).fetchall()
    finally:
        conn.row_factory = prior_factory

    by_po: "OrderedDict[str, PurchaseOrder]" = OrderedDict()
    for r in rows:
        po_number = str(r["po_number"]).strip()
        po = by_po.get(po_number)
        if po is None:
            po = PurchaseOrder(po_number=po_number, vendor="", title="")
            by_po[po_number] = po
        if not po.vendor and r["vendor_name"]:
            po.vendor = str(r["vendor_name"]).strip()

        # One line per stated line number, else per spec, else per description. Two rows that
        # share none of those are two different things and must not be merged.
        spec = (r["spec_code"] or "").strip()
        description = (r["item_description"] or "").strip()
        if r["po_line_number"] is not None:
            key = ("line", int(r["po_line_number"]))
        elif spec:
            key = ("spec", spec.upper())
        else:
            key = ("desc", description.lower()[:60])

        line = next((l for l in po.lines if getattr(l, "_key", None) == key), None)
        if line is None:
            line = Line(
                line_number=int(r["po_line_number"]) if r["po_line_number"] is not None else None,
                spec=spec,
                description=description,
                uom=(r["unit_of_measure"] or "").strip(),
            )
            setattr(line, "_key", key)
            po.lines.append(line)
        if not line.description and description:
            line.description = description
        if not line.spec and spec:
            line.spec = spec

        qty = r["quantity_received"]
        if qty is not None:
            line.received = (line.received or 0) + float(qty)

        # Packages are carried separately on purpose: a header saying "41 CTN" against a line
        # saying "11 EA" means eleven items in forty-one cartons. Receiving the carton count is
        # the mistake this split exists to prevent.
        if r["package_quantity"] is not None and not line.package:
            line.package = f"{_num(r['package_quantity'])} {(r['package_uom'] or '').strip()}".strip()

        reference = (str(r["notification_number"] or "").strip()
                     or str(r["shipment_number"] or "").strip()
                     or "Email confirmation")
        line.receipts.append(Receipt(
            reference=reference,
            date=_date_only(r["pod_stated_date"] or r["email_date"] or ""),
            quantity=float(qty) if qty is not None else None,
            receiver=_receiver(r),
            source_email=str(r["source_email_id"] or ""),
        ))

    _fill_from_purchase_orders(conn, by_po)

    for po in by_po.values():
        po.title = f"PO {po.po_number}" + (f" {po.vendor}" if po.vendor else "")
        po.lines.sort(key=lambda l: (l.line_number is None, l.line_number or 0, l.spec))

    return Report(
        purchase_orders=list(by_po.values()),
        generated_at=generated_at or datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


# --------------------------------------------------------------------------
# render: HTML preview
# --------------------------------------------------------------------------

                                                # DocNo Vendor Line Desc Date OQty Recd Net Fin Rcvr
_HTML_COLUMNS = 10


def to_html(report: Report) -> str:
    """The on-screen preview, as a plain escaped string.

    Returns `str`, not either UI's `Raw`: both the console and `/ui` have their own `Raw` marker
    class and their own escaper, and this module must not depend on either. Callers wrap the result
    — `view.Raw(to_html(r))` in the console, `html.Raw(to_html(r))` under `/ui`. Everything
    interpolated below goes through `escape()` first, so wrapping it is safe.

    **The preview carries one column the sheet does not: Date.** Spitfire reuses column F —
    `Order Qty` — to hold a receipt's date, and marks the switch with the `Receipt# / On`
    sub-header and the merged cells around it. That reads on paper and does not read on screen: a
    date sitting under a heading that says Order Qty looks like a broken column, whatever label is
    above it. So the preview splits Date out and leaves Order Qty holding only order quantities.

    `to_xlsx()` is deliberately *not* changed to match. The download is what gets compared against
    Premier's own export, cell for cell, and `tests/test_receipt_log_conformance.py` holds it to
    that. The preview is read by a person, the sheet is read beside Spitfire's; they answer to
    different masters and the one extra column is the whole of the difference.
    """
    e = escape
    span = _HTML_COLUMNS
    # `<tbody>` is written out rather than left to the parser. Browsers insert one for you, so this
    # changes nothing on screen — but "every row of this sheet lives in a tbody" is now a property
    # of the markup instead of a fixup rule, and the UI's pager selects `tbody tr`.
    out = ['<table class="sheet"><tbody>']
    out.append(f'<tr class="r-title"><td colspan="{span}">{e(SHEET_TITLE)}</td></tr>')
    out.append(f'<tr class="r-sub"><td colspan="{span}">{e(SHEET_SUBTITLE)} · generated '
               f'{e(report.generated_at)}</td></tr>')
    out.append(
        '<tr class="r-head"><td>DocNo</td><td>Vendor</td><td>Line</td><td>Description</td>'
        '<td class="d">Date</td>'
        '<td class="n">Order Qty</td><td class="n">Received</td><td class="n">Net</td>'
        '<td>Final</td><td>Receiver</td></tr>'
    )

    if report.is_empty:
        out.append(f'<tr><td colspan="{span}">{e(report.empty_message)}</td></tr>')
    for po in report.purchase_orders:
        # Every row belonging to one purchase order carries the same `data-group`, so the UI can
        # page and filter this sheet by purchase order rather than by row. A page boundary landing
        # between a PO's header and its receipt lines would split exactly the block a reader is
        # holding beside Spitfire's own Receipt Log, which is the only thing this sheet is for.
        #
        # The attribute is inert in the .xlsx path and in any other consumer; nothing here depends
        # on a UI existing. `escape()` because a PO number reaches this from mail.
        group = f' data-group="{e(po.po_number)}"'
        out.append(f'<tr class="r-project"{group}><td colspan="{span}">{e(po.title)}</td></tr>')
        # The title stops at Description because the sheet merges C:E and no further. Running it
        # through the numeric columns put text under headers that only ever hold numbers.
        out.append(
            f'<tr class="r-po"{group}><td>{e(po.po_number)}</td><td>{e(po.vendor) or "&mdash;"}</td>'
            f'<td colspan="2">{e(po.title)}</td>'
            f'<td></td><td></td><td></td><td></td><td></td><td></td></tr>'
        )
        for line in po.lines:
            spec_desc = " ".join(x for x in (line.spec, line.description) if x)
            # One cell, not "{qty} {uom}" glued together: with both empty that produced a lone
            # space, which is not the same thing as a blank cell to anyone reading a total.
            received = f"{_num(line.received)} {line.uom}".strip()
            # A line has no date of its own — only its receipts do. Blank, not the newest receipt's
            # date: a line received across three deliveries has three dates and no single one.
            out.append(
                f'<tr class="r-line"{group}><td></td><td></td><td class="c">{e(line.label)}</td>'
                f'<td>{e(spec_desc)}</td><td class="d"></td>'
                f'<td class="n">{e(_blank(line.order_qty))}</td>'
                f'<td class="n">{e(received)}</td>'
                f'<td class="n">{e(_blank(line.net))}</td>'
                f'<td class="c">{"*" if line.final else ""}</td><td></td></tr>'
            )
            # The receipts under a line are a sub-table, which is why they keep Spitfire's own
            # `Receipt# / On` vocabulary even though Date now heads the column: it is what someone
            # holding the real Receipt Log will look for. `r-subhead`/`r-rcpt` carry the rail and
            # tint that mark where the sub-table starts and stops.
            out.append(
                f'<tr class="r-subhead"{group}><td></td><td></td><td></td>'
                '<td class="ref">Receipt#</td><td class="d">On</td>'
                '<td></td><td class="n">Qty</td>'
                '<td></td><td></td><td>Receiver</td></tr>'
            )
            for rec in line.receipts:
                out.append(
                    f'<tr class="r-rcpt"{group}><td></td><td></td><td></td>'
                    f'<td class="ref">{e(rec.reference)}</td>'
                    f'<td class="d">{e(rec.date)}</td><td></td>'
                    f'<td class="n">{e(_num(rec.quantity))}</td><td></td><td></td>'
                    f'<td>{e(rec.receiver)}</td></tr>'
                )
    out.append("</tbody></table>")
    return "".join(out)


# --------------------------------------------------------------------------
# render: xlsx
# --------------------------------------------------------------------------

def to_xlsx(report: Report) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME

    head_fill = PatternFill("solid", fgColor="E8EDF2")
    project_fill = PatternFill("solid", fgColor="DCE5F0")

    for col, width in _COL_WIDTHS.items():
        ws.column_dimensions[col].width = width

    ws.merge_cells("A1:D2")
    ws.merge_cells("H1:L1")
    ws.merge_cells("H2:L3")
    ws["H1"] = SHEET_TITLE
    ws["H1"].font = Font(bold=True, size=12.95)
    ws["H1"].alignment = Alignment(horizontal="right")
    ws["H2"] = SHEET_SUBTITLE
    ws["H2"].font = Font(size=11)
    ws["H2"].alignment = Alignment(horizontal="right")
    ws.row_dimensions[1].height = 19.35
    ws.row_dimensions[2].height = 9.95

    # Rows 3 and 4 are left empty, as they are in Premier's export. They used to carry a generated-on
    # line and a note about the blank columns; both belong on screen, where /ui/records already says
    # them, not inside a file Premier forwards to someone who will read it as Spitfire's own.

    row = 5
    for col, label in _HEADERS:
        cell = ws.cell(row, col, label)
        cell.font = Font(bold=True, size=8)
        cell.fill = head_fill
    for col in (2, 5, 8):
        ws.cell(row, col).fill = head_fill
    ws.cell(row, 3).alignment = Alignment(horizontal="center")
    ws.cell(row, 6).alignment = Alignment(horizontal="left")
    for col in (7, 9, 10):
        ws.cell(row, col).alignment = Alignment(horizontal="center")
    ws.merge_cells(start_row=row, start_column=4, end_row=row, end_column=5)
    ws.merge_cells(start_row=row, start_column=7, end_row=row, end_column=8)
    ws.row_dimensions[row].height = 18

    row = 7
    if report.is_empty:
        # A downloaded empty sheet must carry its own explanation. Detached from the screen it
        # was generated on, blank rows and a header read as a broken export.
        ws.cell(row, 1, report.empty_message).font = Font(size=9, italic=True, color="7A8794")
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=11)
        ws.row_dimensions[row].height = 30

    for po in report.purchase_orders:
        ws.cell(row, 1, po.title).font = Font(bold=True, size=10)
        for col in range(1, 12):
            ws.cell(row, col).fill = project_fill
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
        ws.row_dimensions[row].height = 18
        row += 1

        ws.cell(row, 1, po.po_number).font = Font(bold=True, size=9)
        ws.cell(row, 2, po.vendor).font = Font(bold=True, size=9)
        ws.cell(row, 3, po.title).font = Font(bold=True, size=9)
        ws.merge_cells(start_row=row, start_column=3, end_row=row, end_column=5)
        ws.row_dimensions[row].height = 18
        row += 1

        for line in po.lines:
            c = ws.cell(row, 3, line.label)
            c.font = Font(size=9)
            c.alignment = Alignment(horizontal="center")
            desc = " ".join(x for x in (line.spec, line.description) if x)
            ws.cell(row, 4, desc).font = Font(size=9)
            ws.merge_cells(start_row=row, start_column=4, end_row=row, end_column=5)
            # F (Order Qty) and J (Final) are left untouched: one is Spitfire's to supply, the
            # other is Spitfire's to decide. I (Net) is written because it is derived — it is None
            # today and becomes a real number the moment Order Qty arrives, with no edit here.
            ws.cell(row, 6, line.order_qty).font = Font(size=9)
            g = ws.cell(row, 7, line.received)
            g.font = Font(size=9)
            g.alignment = Alignment(horizontal="right")
            ws.merge_cells(start_row=row, start_column=7, end_row=row, end_column=8)
            net = ws.cell(row, 9, line.net)
            net.font = Font(size=9)
            net.alignment = Alignment(horizontal="right")
            ws.row_dimensions[row].height = 18
            row += 1

            ws.cell(row, 4, "Receipt#").font = Font(bold=True, size=9)
            on = ws.cell(row, 6, "On")
            on.font = Font(bold=True, size=9)
            on.alignment = Alignment(horizontal="center")
            ws.merge_cells(start_row=row, start_column=4, end_row=row, end_column=5)
            ws.merge_cells(start_row=row, start_column=7, end_row=row, end_column=8)
            ws.row_dimensions[row].height = 14.45
            row += 1

            for rec in line.receipts:
                ws.cell(row, 4, rec.reference).font = Font(size=9)
                ws.merge_cells(start_row=row, start_column=4, end_row=row, end_column=5)
                date_cell = ws.cell(row, 6, _as_date(rec.date))
                date_cell.font = Font(size=9)
                if isinstance(date_cell.value, datetime):
                    date_cell.number_format = _DATE_FORMAT
                qty = ws.cell(row, 7, rec.quantity)
                qty.font = Font(size=9)
                qty.alignment = Alignment(horizontal="right")
                ws.merge_cells(start_row=row, start_column=7, end_row=row, end_column=8)
                ws.cell(row, 11, rec.receiver).font = Font(size=9)
                ws.row_dimensions[row].height = 12.6
                row += 1
            row += 1
        row += 1

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# --------------------------------------------------------------------------

def _num(value) -> str:
    """SQLite stores quantities as REAL, so 11 comes back as 11.0. Printing '11.0 EA' where the
    packing slip says '11 EA' invites the reader to wonder which one we actually read."""
    if value is None:
        return ""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(f)) if f.is_integer() else f"{f:g}"


def _blank(value) -> str:
    return "" if value is None else _num(value)


def _date_only(value: str) -> str:
    return str(value or "")[:10]


def _as_date(value: str):
    """A real date where we can, so Excel sorts and formats it; the original string otherwise."""
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return value or ""
