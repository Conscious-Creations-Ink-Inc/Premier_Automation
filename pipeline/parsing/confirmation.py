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

* **One PO per row.** A single table carries 910634, 910635 and 910636. Extraction is per row,
  never per document.
* **An explicit confirmation column** that can say no. A `no` row is still emitted — with no
  quantity and zero confidence, so it lands in the exception queue — because silently dropping
  it would erase the one record that says the goods did *not* arrive.
"""

from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional, Sequence

from pipeline.models import ExtractedRecord
from pipeline.parsing import pod
from pipeline.parsing import receipt, tables as tbl
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
    receipt. All three real grids clear the bar: the Example Hotel tracker on its `Confirmed Received`
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
    if not (has_identity and has_quantity and has_workflow_column):
        return False

    # A fourth condition, and the one that was missing. Everything above is satisfied by an
    # expediting tracker: Premier's weekly sheets carry `Description of Item`, `SPEC # or Phase
    # Code`, `Qty` *and* `Qty Delivered`, which is the same shape as the real confirmation grid.
    # Measured on the live store, that let 2,850 report rows — 62.8% of every record held —
    # become receipts for goods nobody had said arrived.
    #
    # `receipt` separates them on the columns a receipt document never has: the order lifecycle.
    # See `parsing/receipt.py`.
    #
    # Only STATUS_REPORT is rejected, not NO_RECEIPT_COLUMN. Premier's own request table —
    # `Description of Item | SPEC # or Phase Code | UOM | Qty | P.O.# | Vendor` — has nowhere to
    # record a receipt by design: it is the question, and the answer arrives in the covering
    # email or from a carrier's POD. Those rows must still be read, as open questions; the
    # per-row gate below is what refuses them a received quantity. See `tests/test_receipt_gate.py`.
    return receipt.classify_grid(header)[0] != receipt.STATUS_REPORT


@dataclass(frozen=True)
class ReceiptEvidence:
    """What is independently known to have arrived, so a grid row can be checked against it.

    A grid states what somebody wants to know. Whether it *happened* is a different question, and
    answering it from the grid alone is the defect this exists to close: Premier's request table
    carries a quantity column and nothing else, so every unanswered request read as a receipt.

    Empty by default, and that default is deliberate. With nothing known, no row may claim a
    receipt from a bare `Qty` — a caller that forgets to pass evidence under-claims, which queues
    work for a person, rather than over-claiming, which posts a receiver for goods nobody got.
    """

    pod_po_numbers: frozenset = frozenset()
    """Purchase orders a proof of delivery names. Precedence 0 — outranks anything the prose says."""

    spec_verdicts: Mapping[str, Any] = field(default_factory=dict)
    """spec code -> `intent.Intent`, from `intent.resolve_thread`: what the newest hop that spoke
    about this line actually said."""

    def confirms(self, po_number: str, spec: Optional[str]) -> bool:
        if po_number and po_number in self.pod_po_numbers:
            return True
        return spec is not None and _is_delivered(self.spec_verdicts.get(spec))

    def denies(self, spec: Optional[str]) -> bool:
        return spec is not None and _is_negative(self.spec_verdicts.get(spec))


def _is_delivered(verdict) -> bool:
    return verdict is not None and getattr(verdict, "value", verdict) == "delivery"


def _is_negative(verdict) -> bool:
    return verdict is not None and getattr(verdict, "value", verdict) == "negative"


def records_from_grid(
    header: Sequence[str],
    body_rows: Sequence[Sequence[str]],
    source_email_id: str,
    email_date: str,
    extraction_source: str,
    default_po: Optional[str] = None,
    *,
    evidence: Optional[ReceiptEvidence] = None,
) -> List[ExtractedRecord]:
    """One record per data row. `default_po` supplies the PO for grids that have no PO column
    (a per-PO request table), and is ignored wherever the row states one.

    `evidence` decides, **per row**, whether a quantity may be called received. One grid routinely
    needs three different answers — the ATTIC STOCK request carries three POs of which one has a
    carrier POD, one was confirmed by the property in the covering email, and one the property
    explicitly excluded — so this cannot be judged per document.
    """
    if not is_confirmation_grid(header):
        return []
    evidence = evidence or ReceiptEvidence()

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

    # `Qty` on these grids is the ordered quantity, not the received one — and it is no longer in
    # this list. It used to be the last fallback "when the sheet has no such column at all", which
    # is exactly the shape of Premier's own request table: one `Qty` column, no `Qty Delivered`,
    # no confirmation column. Every row of every unanswered request therefore staged a receipt for
    # the number somebody was asking about. It goes to `quantity_ordered` now, and a row reaches
    # `quantity_received` only past the gate below.
    # `Qty to be Received` is the outstanding balance — what has *not* arrived. It was in this
    # list, so a sheet with no `Qty Delivered` column had its outstanding figure read as the
    # received one, and `stated is not None` then satisfied route 4 of the gate below. On the
    # Spitfire expediting export that column is usually 0, which staged receipts reading
    # "0 delivered" against live purchase orders. It stays indexed for the note it can carry,
    # but it may never supply a received quantity.
    quantity_columns = [c for c in (idx_qty_delivered,) if c is not None]

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

        ordered = _number(tbl.cell(row, idx_qty)) if idx_qty is not None else None

        stated = None
        for column in quantity_columns:
            stated = _number(tbl.cell(row, column))
            if stated is not None:
                break

        spec_full = spec.full if spec else None

        # --- the gate ---------------------------------------------------------------------------
        # A row may carry a received quantity only where receipt is actually established for *this
        # row's* purchase order. Four ways in, in descending order of what they prove:
        #
        #   1. a proof of delivery names the PO — a carrier says so, and nothing in the prose
        #      outranks that
        #   2. the thread's newest word about this spec is an affirmative
        #   3. the sheet's own confirmation column says yes
        #   4. the sheet states a delivered quantity — an answer in column form
        #
        # Anything else keeps its ordered quantity and becomes an open question. That is the whole
        # guarantee: misreading a sentence then costs a queued mail, never a receipt for goods
        # nobody received.
        # A proof of delivery outranks the prose, and only the prose. `evidence.denies` is a
        # verdict read out of somebody's sentence — "we have only received the Sheer Fabric"
        # excludes the other two lines — and a carrier document beats it, because a question or an
        # omission in an email cannot un-deliver goods somebody signed for. An explicit `no` in the
        # sheet's own confirmation column is a different thing: that is a direct answer, it is not
        # overridden, and the disagreement is surfaced for a person rather than resolved here.
        by_pod = bool(po_number) and po_number in evidence.pod_po_numbers
        denied = confirmed is False or (evidence.denies(spec_full) and not by_pod)
        proven = (not denied) and (confirmed is True
                                   or evidence.confirms(po_number, spec_full)
                                   or stated is not None)
        quantity = (stated if stated is not None else ordered) if proven else None

        note_parts = [tbl.cell(row, idx_comments).strip()] if idx_comments is not None else []
        if confirmed is not None:
            note_parts.append({True: "property/vendor confirmed receipt",
                               False: "property/vendor answered NOT received"}[confirmed])
        elif evidence.denies(spec_full) and not by_pod:
            note_parts.append("the reply names other items as the ones received; this is excluded")
        elif not proven:
            # Emitted even when the grid has no confirmation column at all. This note used to be
            # written only where that column existed and was blank, so the one case where nothing
            # whatsoever had been confirmed — a bare request table — was also the one case that
            # said nothing about it.
            note_parts.append("awaiting property/vendor confirmation" if idx_confirmed is not None
                              else "no confirmation column and no delivery evidence for this line")
        elif evidence.confirms(po_number, spec_full):
            note_parts.append("receipt evidenced by a proof of delivery" if by_pod
                              else "receipt confirmed in the reply on this thread")

        if by_pod and evidence.denies(spec_full):
            note_parts.append("NOTE: the reply excludes this line but a proof of delivery names "
                              "it — needs a person")
        if proven and confirmed is not True and stated is None and ordered is not None:
            # Staged, but say so. Nobody stated a delivered figure; this is the ordered one, and a
            # reviewer comparing it against a POD is exactly how the 196-vs-202 gap gets caught.
            #
            # Not said when the sheet's own confirmation column answered yes: there the answer and
            # the quantity sit on the same row and whoever ticked it saw both.
            note_parts.append("quantity is the ordered figure — no delivered quantity was stated")

        if denied:
            # Explicitly not received. Kept as a record so the negative answer is auditable, but
            # with no quantity — nothing here may become a receipt.
            confidence = 0.0
        elif proven and quantity is not None and spec is not None:
            confidence = 0.85
        elif ordered is not None and spec is not None:
            confidence = 0.4   # a complete line, but nothing yet says it arrived
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
            quantity_ordered=ordered,
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
