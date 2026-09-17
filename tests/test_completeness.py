"""The record completeness contract.

Written against the live failure it exists to prevent: on Premier's mailbox, three records were
labelled "Ready to process further" while missing every proof-of-delivery field there is.
"""

from pipeline import completeness, receipt_log

FULL = {
    "po_number": "910634", "vendor_name": "P. Kaufmann", "po_line_number": 1,
    "item_description": "Main Drapery Fabric", "quantity_received": 202.0,
    "unit_of_measure": "YD", "spec_code": "GR-350a-WTF", "pod_stated_date": "2025-09-10",
    "received_by": "U ALI", "carrier_name": "GlobalTranz", "tracking_number": "91457971",
}


def test_a_fully_populated_record_is_complete():
    assert completeness.gaps(FULL).is_complete
    assert completeness.gaps(FULL).missing_advisory == []


def test_a_po_only_record_is_missing_everything_else():
    """Ten of the thirteen live records looked exactly like this — a PO number and nothing at all
    to compare against it."""
    result = completeness.gaps({"po_number": "906481"})
    assert result.is_complete is False
    assert set(result.missing_required) == set(completeness.REQUIRED) - {"po_number"}


def test_the_fabric_record_shape_is_incomplete_without_its_notification():
    """PO, spec, description and quantity, read from the request table — but no delivery date.
    This passed `_READY_CLAUSE` and is the exact record that prompted the contract.

    Since 2026-08-21 the only thing still missing here is the date: vendor, line and received-by
    moved to `DERIVED`. That is the point — the record is *still incomplete*, and for the one
    reason that matters. A receiver asserting a delivery with no date is a claim with nothing
    behind it, and no other system can supply that date.
    """
    result = completeness.gaps({
        "po_number": "910634", "spec_code": "GR-350a-WTF",
        "item_description": "Main Drapery Fabric", "quantity_received": 196.0,
        "unit_of_measure": "YD",
    })
    assert result.is_complete is False
    assert set(result.missing_required) == {"pod_stated_date"}


def test_the_four_derived_fields_never_block_a_record():
    """Vendor, UOM, line number and received-by are all absent, and the record is still complete.

    Each has a source that already holds the authoritative value — the matched Spitfire PO line,
    or the POD's own signature block — so asking a person to type them invites a wrong answer where
    none was previously possible. Measured on Premier's 94 live records, `received_by` alone
    blocked 22 of them.
    """
    without = {k: v for k, v in FULL.items() if k not in completeness.DERIVED}
    result = completeness.gaps(without)
    assert result.is_complete, result.describe()
    assert set(completeness.DERIVED).isdisjoint(completeness.REQUIRED)


def test_carrier_and_tracking_are_advisory_never_blocking():
    """A warehouse Inbound moves inside the 3PL's own network and states neither."""
    result = completeness.gaps({**FULL, "carrier_name": None, "tracking_number": ""})
    assert result.is_complete
    assert set(result.missing_advisory) == {"carrier_name", "tracking_number"}


def test_a_delivered_quantity_of_zero_is_present_not_missing():
    """An empty shipment, or a line cancelled at the dock, is a real statement — and exactly the
    case a person most needs to see rather than have filtered away as "no quantity"."""
    assert "quantity_received" not in completeness.gaps({**FULL, "quantity_received": 0}).missing_required


def test_whitespace_is_absence():
    assert "spec_code" in completeness.gaps({**FULL, "spec_code": "   "}).missing_required


def test_describe_names_the_fields():
    text = completeness.gaps({**FULL, "spec_code": None, "pod_stated_date": None}).describe()
    assert text == "missing: spec code, POD date"


def test_describe_is_empty_for_a_complete_record():
    assert completeness.gaps(FULL).describe() == ""


def test_advisory_fields_are_marked_as_such_when_asked_for():
    text = completeness.gaps({**FULL, "carrier_name": None}).describe(advisory=True)
    assert "carrier (advisory)" in text


def test_it_reads_objects_as_well_as_mappings():
    class Row:
        po_number = "910634"

    assert "po_number" not in completeness.gaps(Row()).missing_required


def test_every_receipt_log_column_still_has_a_source():
    """The drift guard. Every Receipt Log column a record is responsible for must be answered by
    `REQUIRED` or by `DERIVED` — so a column added or renamed there cannot quietly go unfilled.

    Before 2026-08-21 this asserted all six were `REQUIRED`. Four of them moved to `DERIVED`, which
    is a change in *who supplies the value*, not in whether the column gets filled — so the guard
    now checks the union. `Order Qty` is absent from both because it comes from the purchase order
    and never from a record; `Net` and `Final` are computed.
    """
    printed = {name.strip() for _, name in receipt_log._HEADERS}
    sourced_from_a_record = {
        "DocNo": "po_number", "Vendor": "vendor_name", "Line": "po_line_number",
        "Description": "item_description", "Received": "quantity_received", "Receiver": "received_by",
    }
    assert set(sourced_from_a_record) <= printed, "a Receipt Log column was renamed or removed"
    answerable = set(completeness.REQUIRED) | set(completeness.DERIVED)
    for column, field_name in sourced_from_a_record.items():
        assert field_name in answerable, f"{column} has no field behind it"


def test_a_missing_pod_file_is_not_a_completeness_gap():
    """The POD *file* is `post_decision`'s gate, not this module's, because it has a remedy nothing
    here has: a person may waive it for a delivery stated entirely in the email body. Nothing in
    `REQUIRED` is waivable, and conflating the two would make the waiver look like a way to skip
    the delivery date too."""
    assert "pod_ledger_id" not in completeness.REQUIRED
    assert "pod_source" not in completeness.REQUIRED
    assert completeness.gaps(FULL).is_complete
