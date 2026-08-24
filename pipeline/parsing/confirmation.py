"""Confirmation grids — the request tables and trackers behind corpus classes C and D.

Premier drives property and vendor confirmations with a table. Sometimes it is inline HTML in
the email, sometimes an attached spreadsheet, but the column vocabulary is the same, so both
paths share this module rather than growing two drifting copies.

Real header rows, verified:

    Description of Item | SPEC # or Phase Code | UOM | Qty | P.O.# | Vendor          (5-Star request)
    Vendor | PO# | Spec# | QTY | Item Description | Tracking | Delivery Date | Confirmed Received: Yes or No
    Description of Item | SPEC # or Phase Code | UOM | Qty | Qty Delivered | Qty to be Received |
        P.O.# | Vendor | Actual Delivery Date | (Ship to) GC,WH, Property, | Comments | RECEIVED? YES or NO

Two things these grids do that the Authority format never does:

* **One PO per row.** A single table carries 210634, 210635 and 210636. Extraction is per row,
  never per document.
* **An explicit confirmation column** that can say no. A `no` row is still emitted — with no
  quantity and zero confidence, so it lands in the exception queue — because silently dropping
  it would erase the one record that says the goods did *not* arrive.
"""

from typing import List, Optional, Sequence

from pipeline.models import ExtractedRecord
from pipeline.parsing import pod
from pipeline.parsing import tables as tbl
from pipeline.parsing import tokens

DESCRIPTION_HEADERS = ["Description of Item", "Item Description", "Description", "Item"]
SPEC_HEADERS = ["SPEC # or Phase Code", "Spec#", "Spec #", "Spec", "Phase Code", "Item Number"]
UOM_HEADERS = ["UOM", "Unit", "Unit of Measure"]
QTY_HEADERS = ["Qty", "QTY", "Quantity"]
QTY_DELIVERED_HEADERS = ["Qty Delivered", "Quantity Delivered", "Qty Rcvd", "Qty Received"]
QTY_TO_RECEIVE_HEADERS = ["Qty to be Received", "Qty To Receive", "Qty Outstanding"]
PO_HEADERS = ["P.O.#", "PO#", "PO #", "PO", "Purchase Order"]
VENDOR_HEADERS = ["Vendor", "Supplier"]
DATE_HEADERS = ["Actual Delivery Date", "Delivery Date", "Date Delivered", "Date Received"]
TRACKING_HEADERS = ["Tracking", "Tracking #", "Tracking Number", "PRO", "BOL"]
SHIP_TO_HEADERS = ["(Ship to) GC,WH, Property,", "Ship To", "Delivered To", "Location"]
COMMENTS_HEADERS = ["Comments", "Notes", "Comment"]
CONFIRMED_HEADERS = ["Confirmed Received: Yes or No", "RECEIVED? YES or NO", "Confirmed Received",
                     "Received?", "Confirmed", "Confirmed Y/N", "Received Y/N"]

# A grid is a confirmation grid if it has a spec or description column *and* a quantity column.
# Requiring a PO column would reject the 5-Star request table's siblings that key on spec alone.
SIGNATURE_HEADERS = ["Description of Item", "SPEC # or Phase Code", "Qty", "P.O.#"]
TRACKER_SIGNATURE_HEADERS = ["Vendor", "PO#", "Spec#", "QTY", "Item Description"]

_YES = {"yes", "y", "true", "confirmed", "received", "rec'd", "1"}
_NO = {"no", "n", "false", "not received", "0"}


def _normalize_confirmation(raw: str) -> Optional[bool]:
    """`"yes"` -> True, `"no"` -> False, blank/anything else -> None (still awaiting an answer)."""
    value = (raw or "").strip().lower().rstrip(".")
    if value in _YES:
        return True
    if value in _NO:
        return False
    return None


def _number(raw: str) -> Optional[float]:
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        quantity = tokens.parse_leading_quantity(text)
        return quantity.value if quantity else None


def is_confirmation_grid(header: Sequence[str]) -> bool:
    """Identity + quantity + at least one column that only a confirmation workflow has.

    The third condition matters. Identity-plus-quantity alone also describes a plain
    `PO | Spec | Description | Qty` table — a vendor packing list, say — which belongs to the
    generic row builder, not to a workflow that reasons about who has and hasn't confirmed
    receipt. All three real grids clear the bar: the Cameo tracker on its `Confirmed Received`
    column, the 5-Star request grid on its `UOM` column, the Public Space tracker on both.
    """
    has_identity = (tbl.column_index(header, SPEC_HEADERS) is not None
                    or tbl.column_index(header, DESCRIPTION_HEADERS) is not None)
    has_quantity = any(tbl.column_index(header, names) is not None
                       for names in (QTY_HEADERS, QTY_DELIVERED_HEADERS, QTY_TO_RECEIVE_HEADERS))
    has_workflow_column = any(tbl.column_index(header, names) is not None for names in (
        CONFIRMED_HEADERS, QTY_DELIVERED_HEADERS, QTY_TO_RECEIVE_HEADERS,
        UOM_HEADERS, TRACKING_HEADERS,
    ))
    return has_identity and has_quantity and has_workflow_column


def records_from_grid(
    header: Sequence[str],
    body_rows: Sequence[Sequence[str]],
    source_email_id: str,
    email_date: str,
    extraction_source: str,
    default_po: Optional[str] = None,
) -> List[ExtractedRecord]:
    """One record per data row. `default_po` supplies the PO for grids that have no PO column
    (a per-PO request table), and is ignored wherever the row states one."""
    if not is_confirmation_grid(header):
        return []

    idx_description = tbl.column_index(header, DESCRIPTION_HEADERS)
    idx_spec = tbl.column_index(header, SPEC_HEADERS)
    idx_uom = tbl.column_index(header, UOM_HEADERS)
    idx_qty = tbl.column_index(header, QTY_HEADERS)
    idx_qty_delivered = tbl.column_index(header, QTY_DELIVERED_HEADERS)
    idx_qty_to_receive = tbl.column_index(header, QTY_TO_RECEIVE_HEADERS)
    idx_po = tbl.column_index(header, PO_HEADERS)
    idx_vendor = tbl.column_index(header, VENDOR_HEADERS)
    idx_date = tbl.column_index(header, DATE_HEADERS)
    idx_tracking = tbl.column_index(header, TRACKING_HEADERS)
    idx_ship_to = tbl.column_index(header, SHIP_TO_HEADERS)
    idx_comments = tbl.column_index(header, COMMENTS_HEADERS)
    idx_confirmed = tbl.column_index(header, CONFIRMED_HEADERS)

    # `Qty` on these grids is the ordered quantity, not the received one. Where the sheet
    # distinguishes them, the receivable figure is what was delivered, or failing that what is
    # still outstanding; `Qty` is only used when the sheet has no such column at all.
    quantity_columns = [c for c in (idx_qty_delivered, idx_qty_to_receive, idx_qty) if c is not None]

    records: List[ExtractedRecord] = []
    for row in body_rows:
        if not any((cell or "").strip() for cell in row):
            continue

        po_numbers = tokens.parse_po_list(tbl.cell(row, idx_po)) if idx_po is not None else []
        po_number = po_numbers[0] if po_numbers else (default_po or "")

        spec = tokens.normalize_spec(tbl.cell(row, idx_spec))
        description = tbl.cell(row, idx_description).strip() or None
        if spec is None and description:
            # Rows with an empty Spec# but a description that names one — the corpus has
            # "Lamp Shades for RES-401-LT" — are recoverable rather than lost (finding G10).
            spec = tokens.normalize_spec(description)

        if not po_number and spec is None and description is None:
            continue   # a spacer or a totals row, not data

        confirmed = _normalize_confirmation(tbl.cell(row, idx_confirmed)) if idx_confirmed is not None else None

        quantity = None
        for column in quantity_columns:
            quantity = _number(tbl.cell(row, column))
            if quantity is not None:
                break

        note_parts = [tbl.cell(row, idx_comments).strip()] if idx_comments is not None else []
        if idx_confirmed is not None:
            note_parts.append({True: "property/vendor confirmed receipt",
                               False: "property/vendor answered NOT received",
                               None: "awaiting property/vendor confirmation"}[confirmed])

        if confirmed is False:
            # Explicitly not received. Kept as a record so the negative answer is auditable, but
            # with no quantity — nothing here may become a receipt.
            quantity = None

        if confirmed is True and quantity is not None and spec is not None:
            confidence = 0.85
        elif confirmed is False:
            confidence = 0.0
        elif quantity is not None and spec is not None:
            confidence = 0.5   # data is complete but nobody has confirmed it yet
        else:
            confidence = 0.3

        records.append(ExtractedRecord(
            source_email_id=source_email_id,
            po_number=po_number,
            shipment_number=None,
            spec_code=spec.full if spec else None,
            parent_spec_code=spec.parent if spec else None,
            sub_spec_suffix=spec.sub_part if spec else None,
            item_description=description,
            vendor_name=tbl.cell(row, idx_vendor).strip() or None,
            carrier_name=_carrier_from_tracking(tbl.cell(row, idx_tracking)),
            tracking_number=_tracking_from(tbl.cell(row, idx_tracking)),
            quantity_received=quantity,
            unit_of_measure=(tbl.cell(row, idx_uom).strip().upper() or None),
            pod_stated_date=tokens.normalize_date(tbl.cell(row, idx_date)),
            email_date=email_date,
            delivery_location=tbl.cell(row, idx_ship_to).strip() or None,
            comments="; ".join(part for part in note_parts if part) or None,
            extraction_source=extraction_source,
            extraction_confidence=confidence,
            raw_snippet=" | ".join(str(cell) for cell in row)[:1000],
        ))
    return records


def _tracking_from(cell_text: str) -> Optional[str]:
    """Tracking cells mix carrier and number (`"FedEx 476858924781"`, `"FedEx #473712851296"`)
    and sometimes carry no number at all (`"Shipped via Truck"`)."""
    numbers = tokens.parse_tracking_numbers(cell_text)
    return numbers[0] if numbers else None


def _carrier_from_tracking(cell_text: str) -> Optional[str]:
    return pod.detect_carrier(cell_text)
