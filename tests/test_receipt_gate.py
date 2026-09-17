"""A grid row may only be called *received* where something evidences receipt.

The defect these pin down: Premier drives confirmations with a request table, that table carries
exactly one quantity column, and the grid reader used it as the received quantity whenever no
better column existed. Every row of every unanswered request was therefore staged as a receipt for
the number somebody was asking about — including, on the ATTIC STOCK thread, a line the property
had said in the same email had never arrived.
"""

from pipeline.parsing import confirmation
from pipeline.parsing.confirmation import ReceiptEvidence
from pipeline.parsing.intent import Intent

# Premier's real 5-Star request table: identity, a quantity, a PO — and nothing that answers.
REQUEST_HEADER = ["Description of Item", "SPEC # or Phase Code", "UOM", "Qty", "P.O.#", "Vendor"]
REQUEST_ROWS = [
    ["Main Drapery Fabric", "GR-350a-WTF", "YD", "196", "210634", "P. Kaufmann"],
    ["Sheer Fabric", "GR-350c-WTF", "YD", "84", "210635", "Fil Doux Inc"],
    ["Piping at Blackout Drapery", "GR-350d-WTF", "YD", "78", "210636", "Daniel Stuart Studio"],
]


def _read(evidence=None):
    records = confirmation.records_from_grid(
        REQUEST_HEADER, REQUEST_ROWS, "msg-1", "2026-08-12", "html:confirmation_grid",
        evidence=evidence,
    )
    return {record.po_number: record for record in records}


def test_an_unanswered_request_stages_no_receipts_at_all():
    """The whole bug, in one assertion."""
    for record in _read().values():
        assert record.quantity_received is None
        assert record.extraction_confidence < 0.5


def test_the_ordered_quantity_is_kept_it_is_just_not_a_receipt():
    """It is the figure a reviewer compares an answer against — losing it would help nobody."""
    rows = _read()
    assert rows["210634"].quantity_ordered == 196.0
    assert rows["210635"].quantity_ordered == 84.0
    assert rows["210636"].quantity_ordered == 78.0


def test_a_row_with_no_confirmation_column_says_so():
    """This note used to be written only where the column existed and was blank — so the one case
    where nothing whatsoever had been confirmed was also the one case that said nothing."""
    assert "no confirmation column" in _read()["210634"].comments


def test_one_grid_yields_three_different_verdicts():
    """The acceptance case. A carrier proved 210634; the property confirmed 210635 by name and,
    with the word "only", excluded 210636. Any per-email answer gets at least two of these wrong.
    """
    rows = _read(ReceiptEvidence(
        pod_po_numbers=frozenset({"210634"}),
        spec_verdicts={"GR-350a-WTF": Intent.NEGATIVE,
                       "GR-350c-WTF": Intent.DELIVERY,
                       "GR-350d-WTF": Intent.NEGATIVE},
    ))

    assert rows["210634"].quantity_received == 196.0          # proof of delivery
    assert rows["210635"].quantity_received == 84.0           # the property said so
    assert rows["210636"].quantity_received is None           # "only" excluded it
    assert rows["210636"].extraction_confidence == 0.0


def test_a_proof_of_delivery_outranks_the_prose_and_says_where_they_disagree():
    """A question or an omission in an email cannot un-deliver goods somebody signed for. But the
    disagreement is a fact a person needs, so it is stated rather than silently resolved."""
    rows = _read(ReceiptEvidence(pod_po_numbers=frozenset({"210634"}),
                                 spec_verdicts={"GR-350a-WTF": Intent.NEGATIVE}))
    assert rows["210634"].quantity_received == 196.0
    assert "needs a person" in rows["210634"].comments


def test_an_explicit_no_in_the_sheet_is_not_overridden_by_a_pod():
    """A scope word in somebody's sentence is an inference; a ticked "no" is a direct answer."""
    header = ["Vendor", "PO#", "Spec#", "QTY", "Item Description", "Confirmed Received: Yes or No"]
    rows = [["Amtrend", "206481", "PAT-200-SG", "1", "L-Shaped Banquette", "no"]]
    record = confirmation.records_from_grid(
        header, rows, "msg-1", "2026-01-01", "excel",
        evidence=ReceiptEvidence(pod_po_numbers=frozenset({"206481"})),
    )[0]
    assert record.quantity_received is None
    assert record.extraction_confidence == 0.0


def test_a_quantity_assumed_from_the_ordered_figure_admits_it():
    """PO 210634 was staged at 196 while the POD and the Authority notice both said 202 — the
    thread itself explains the gap as "6 yards of overage". A reviewer can only catch that if the
    record says where its number came from."""
    rows = _read(ReceiptEvidence(spec_verdicts={"GR-350c-WTF": Intent.DELIVERY}))
    assert "no delivered quantity was stated" in rows["210635"].comments


def test_a_stated_delivered_quantity_needs_no_outside_evidence():
    """A `Qty Delivered` figure is an answer in column form."""
    header = ["Description of Item", "SPEC # or Phase Code", "UOM", "Qty", "Qty Delivered",
              "P.O.#", "Vendor"]
    rows = [["Amenity Tray", "BRR-803-AC", "Set", "6", "4", "212559", "Pigeon & Poodle"]]
    record = confirmation.records_from_grid(header, rows, "msg-1", "2026-01-01", "excel")[0]
    assert record.quantity_received == 4.0
    assert record.quantity_ordered == 6.0
