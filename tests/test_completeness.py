"""The record completeness contract.

Written against the live failure it exists to prevent: on Premier's mailbox, three records were
labelled "Ready to process further" while missing every proof-of-delivery field there is.
"""

from pipeline import completeness, receipt_log

FULL = {
    "po_number": "210634", "vendor_name": "P. Kaufmann", "po_line_number": 1,
    "item_description": "Main Drapery Fabric", "quantity_received": 202.0,
    "unit_of_measure": "YD", "spec_code": "GR-350a-WTF", "pod_stated_date": "2025-09-10",
    "received_by": "U ALI", "carrier_name": "GlobalTranz", "tracking_number": "31457971",
}


def test_a_fully_populated_record_is_complete():
    assert completeness.gaps(FULL).is_complete
    assert completeness.gaps(FULL).missing_advisory == []


def test_a_po_only_record_is_missing_everything_else():
    """Ten of the thirteen live records looked exactly like this — a PO number and nothing at all
    to compare against it."""
    result = completeness.gaps({"po_number": "206481"})
    assert result.is_complete is False
    assert set(result.missing_required) == set(completeness.REQUIRED) - {"po_number"}


def test_the_fabric_record_shape_is_incomplete_without_its_notification():
    """PO, spec, description and quantity, read from the request table — but no POD. This passed
    `_READY_CLAUSE` and is the exact record that prompted the contract."""
    result = completeness.gaps({
        "po_number": "210634", "spec_code": "GR-350a-WTF",
        "item_description": "Main Drapery Fabric", "quantity_received": 196.0,
        "unit_of_measure": "YD",
    })
    assert result.is_complete is False
    assert set(result.missing_required) == {
        "vendor_name", "po_line_number", "pod_stated_date", "received_by"}


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
    assert "received_by" in completeness.gaps({**FULL, "received_by": "   "}).missing_required


def test_describe_names_the_fields():
    text = completeness.gaps({**FULL, "pod_stated_date": None, "received_by": None}).describe()
    assert text == "missing: POD date, received-by"


def test_describe_is_empty_for_a_complete_record():
    assert completeness.gaps(FULL).describe() == ""


def test_advisory_fields_are_marked_as_such_when_asked_for():
    text = completeness.gaps({**FULL, "carrier_name": None}).describe(advisory=True)
    assert "carrier (advisory)" in text


def test_it_reads_objects_as_well_as_mappings():
    class Row:
        po_number = "210634"

    assert "po_number" not in completeness.gaps(Row()).missing_required


def test_required_still_matches_what_the_receipt_log_prints():
    """The drift guard. `REQUIRED` is `receipt_log._HEADERS` read backwards, so a column added or
    renamed there has to be answered here rather than quietly going unfilled.

    `Order Qty` is absent because it comes from the purchase order in Spitfire, not from the mail;
    `Net` and `Final` are computed from the others.
    """
    printed = {name.strip() for _, name in receipt_log._HEADERS}
    sourced_from_a_record = {
        "DocNo": "po_number", "Vendor": "vendor_name", "Line": "po_line_number",
        "Description": "item_description", "Received": "quantity_received", "Receiver": "received_by",
    }
    assert set(sourced_from_a_record) <= printed, "a Receipt Log column was renamed or removed"
    for column, field_name in sourced_from_a_record.items():
        assert field_name in completeness.REQUIRED, f"{column} has no required field behind it"
