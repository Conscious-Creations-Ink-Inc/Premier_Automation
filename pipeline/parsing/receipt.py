"""Did goods arrive? — one question, one definition, for tabular documents.

This module exists because the pipeline had no such question. Stage 1 asked whether an email was
*about* a delivery (`intent.is_delivery_topic`, deliberately permissive), and extraction then
treated any table with recognisable columns as a set of receipts. Nothing in between ever asked
whether the document in hand actually *records goods arriving*.

Measured on the live store before this module existed: 2,850 of 4,537 records — 62.8% — came from
spreadsheets that are not delivery documents at all. Sheets named `Expediting`, `qPOExpeditor` and
`Combined Forecast` accounted for all of it. None of those 2,850 records carried a `received_by`;
none linked to a proof of delivery; 1,597 carried a `pod_stated_date` read out of a column that
means something else.

**The discriminator.** A delivery document says what arrived: `Qty Delivered`, `Actual Delivery
Date`, `RECEIVED? YES or NO`, `QTY REC'D`, a warehouse `RR #`. A status report tracks a purchase
order's whole life, and receipt is one column among thirty: `Target Ship Date`, `Production Start
Date`, `Shop Drawing due from Vendor`, `Flame Cert.`, `Next Call to Vendor`, `Dep Check Sent`.

Both shapes carry `Qty Delivered`, so the receipt column alone cannot separate them — which is
precisely the mistake that produced the 2,850. What separates them is the *lifecycle* columns,
which a receipt document never has. Premier's real confirmation grid
(`Public Space - Pending Receipt Confirmation Orders.xlsx`) has none; the ` EXPEDITING REPORT`
sheet has twenty-one.

That asymmetry is the whole test, and it is a per-document judgement rather than a per-row one.
A weekly expediting report is a snapshot of state Premier already holds, not somebody asserting
that goods turned up — so it may not mint receipts, however many delivered quantities it lists.
"""

from typing import Optional, Sequence, Tuple

from pipeline.parsing import tables as tbl

# --- columns that record an actual receipt ------------------------------------------------------

RECEIPT_HEADERS = [
    "Qty Delivered", "Quantity Delivered", "Qty Rcvd", "Qty Received", "QTY REC'D", "Qty Rec'd",
    "Actual Delivery Date", "Date Delivered", "Date Received", "Received Date",
    "RECEIVED? YES or NO", "Confirmed Received: Yes or No", "Confirmed Received",
    "Received?", "Received By", "Signed By",
    # The short forms. `confirmation.CONFIRMED_HEADERS` carries these too and cannot be imported
    # from here — it is the module that depends on this one — so they are restated. Fuzzy header
    # matching does not bridge the gap on its own: "Confirmed Y/N" against "Confirmed Received"
    # scores below the threshold, and a tracker headed that way is a receipt document.
    "Confirmed", "Confirmed Y/N", "Received Y/N",
    "RR #", "RR#", "WH RR#", "Receiving Report", "POD",
]
"""A column that only exists because somebody is recording goods that arrived."""

# --- columns that only a status tracker has -----------------------------------------------------

LIFECYCLE_HEADERS = [
    "Estimated Delivery (Date)", "Estimated Delivery Date", "Est. Delivery", "Target Ship Date",
    "Actual Ship Date", "Target Port Arrival Date", "Actual Port Arrival Date", "Port Pick-Up Date",
    "Production Start Date", "Production End Date",
    "Shop Drawing due from Vendor", "Shop Drawing Final Sign off Due from Design",
    "Flame Cert.", "Finish Sample Requested", "Fabric Sample/Strike-Off", "Seaming Diagram",
    "Hardware Sample", "Next Call to Vendor", "Last Call to Vendor", "Next Call Hidden",
    "Dep Check Tracked", "Dep Check Sent", "Ack Rec'd", "P.O. Created",
    "SCLineAmount", "Paid_Amnt", "perc_paid", "Actual Unit Cost", "Total Committed",
    "Overage Amount", "Change Since", "Prior Value", "New Value",
    # A tracker reports a *state* per line and flags what moved; a receipt document records a
    # fact and has nothing to be "in progress" about. These three are what separate an
    # expediting dashboard — a rendered summary of the sheets above, carrying no system keys and
    # only two date columns — from the confirmation grids, which score zero on this whole list.
    "Status", "Flag", "Significance",
]
"""Columns that track a purchase order's life rather than its arrival. A receipt document has
none of these; an expediting tracker has most of them."""

# --- columns that mark a system export ----------------------------------------------------------

SYSTEM_KEY_HEADERS = [
    "DocItemNum", "DocItemKey", "DocMasterKey", "DocTypeKey", "POLinkedItemKey", "POLinkedDocKey",
    "LinkedDocKey", "LinkedItemKey", "UniReferenceKey", "DocBatchNo",
]
"""Spitfire's own primary keys. Their presence means the sheet is a database export — a mirror of
state the system already holds — not a document somebody wrote to report a delivery."""

COMMERCIAL_HEADERS = [
    "Unit Price", "Extended Price", "Unit Cost", "Total Cost", "Line Total", "Price/UM",
    "Unit Budget", "Total Budget", "Extended Cost", "Amount", "Bid", "Proposed Unit Cost",
]
"""Columns that price a line rather than receive it.

A bid analysis, a budget comparison and a vendor quote all carry Item/Description/Qty/UOM, which
is the same identity-plus-quantity shape as Premier's request table. Neither has a receipt column,
so the lifecycle test does not separate them — but a receiving document never quotes a unit price.

Two rather than one, because a genuine grid can carry a single `Amount`-ish column without being
a commercial document.
"""

MIN_COMMERCIAL_COLUMNS = 2

MIN_LIFECYCLE_COLUMNS = 3
"""How many lifecycle columns make a document a status report.

Three rather than one: a genuine confirmation grid may legitimately carry a single planning column
(`Estimated Delivery (Date)` next to `Actual Delivery Date` is a reasonable thing to put on a
receipt sheet). Three distinct ones is no longer a receipt document by any reading. Measured
against the real corpus the two populations are nowhere near this boundary — Premier's confirmation
grid scores 0 and the expediting sheets score 15 to 21 — so the exact value is not load-bearing.
"""

DELIVERY_DOCUMENT = "delivery_document"
STATUS_REPORT = "status_report"
NO_RECEIPT_COLUMN = "no_receipt_column"


def _present(header: Sequence[str], names: Sequence[str]) -> bool:
    return tbl.column_index(header, names) is not None


def lifecycle_columns(header: Sequence[str]) -> Tuple[str, ...]:
    """The lifecycle columns this header carries, for a reason string a person reads."""
    found = []
    for name in LIFECYCLE_HEADERS:
        index = tbl.column_index(header, [name])
        if index is not None:
            cell = (header[index] or "").strip()
            if cell and cell not in found:
                found.append(cell)
    return tuple(found)


def system_key_columns(header: Sequence[str]) -> Tuple[str, ...]:
    """The internal key columns this header carries."""
    found = []
    for name in SYSTEM_KEY_HEADERS:
        index = tbl.column_index(header, [name])
        if index is not None:
            cell = (header[index] or "").strip()
            if cell and cell not in found:
                found.append(cell)
    return tuple(found)


def has_receipt_column(header: Sequence[str]) -> bool:
    """Does this grid have anywhere to record that goods arrived?"""
    return _present(header, RECEIPT_HEADERS)


def classify_grid(header: Sequence[str]) -> Tuple[str, str]:
    """`(kind, reason)` for one header row.

    Order matters. A system export is judged before its columns are read, because an export
    carrying `Qty Delivered` is still a mirror of state rather than a claim about it. Then the
    lifecycle test, then the requirement that there be somewhere to record a receipt at all.
    """
    keys = system_key_columns(header)
    if keys:
        return STATUS_REPORT, (
            f"a system export — carries the internal key column(s) "
            f"{', '.join(keys[:3])}, so it mirrors state already held rather than reporting "
            f"a delivery"
        )

    lifecycle = lifecycle_columns(header)
    if len(lifecycle) >= MIN_LIFECYCLE_COLUMNS:
        return STATUS_REPORT, (
            f"a status tracker — {len(lifecycle)} lifecycle columns including "
            f"{', '.join(lifecycle[:3])}; a receipt document tracks arrival, not the whole order"
        )

    commercial = [n for n in COMMERCIAL_HEADERS if tbl.column_index(header, [n]) is not None]
    if len(commercial) >= MIN_COMMERCIAL_COLUMNS:
        return STATUS_REPORT, (
            f"a priced document — {', '.join(commercial[:3])}; a receiving document does not "
            f"quote a unit price"
        )

    if not has_receipt_column(header):
        return NO_RECEIPT_COLUMN, (
            "no column records a receipt — nothing here says goods arrived, only what was "
            "ordered. Rows may still be read as open questions; none may become a receipt"
        )

    return DELIVERY_DOCUMENT, "records a receipt and does not track the wider order lifecycle"


def is_delivery_document(header: Sequence[str]) -> bool:
    """True only where rows may become receipt records."""
    return classify_grid(header)[0] == DELIVERY_DOCUMENT
