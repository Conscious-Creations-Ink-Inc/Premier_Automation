"""Correcting a record by hand, and the two ways that could quietly fail to work.

The queue's record rows had `Fill` and `Verify` and nothing else, and neither can supply a spec
code — seventeen of the twenty-one incomplete records in Premier's live store are missing exactly
that. This is the form that can, and these are the properties that make it worth having.
"""
import sqlite3

import pytest

from api.stores.extracted_store import EDITABLE_FIELDS
from pipeline import completeness, extracted_records_store, read_views, record_edit, state_db
from pipeline.models import ExtractedRecord

NOW = "2026-08-24T12:00:00+00:00"


def new_conn():
    return state_db.get_connection(":memory:")


def make(**overrides) -> ExtractedRecord:
    values = dict(
        source_email_id="msg-1", po_number="208491", shipment_number=None,
        spec_code="STE-402-LT-B", parent_spec_code=None, sub_spec_suffix=None,
        item_description="Base, Floor Lamp", vendor_name="Authority", carrier_name="Nolan",
        tracking_number="123", quantity_received=11.0, unit_of_measure="EA",
        pod_stated_date="2025-10-01", email_date="2025-10-01", delivery_location="WH",
        comments=None, extraction_source="authority_inbound", extraction_confidence=1.0,
        raw_snippet=None, po_line_number=300, received_by="U ALI",
        package_quantity=None, package_uom=None, notification_number="239475",
    )
    values.update(overrides)
    return ExtractedRecord(**values)


def row_for(c, record_id):
    c.row_factory = sqlite3.Row
    return c.execute("SELECT * FROM extracted_records WHERE id = ?", (record_id,)).fetchone()


def test_typing_a_missing_spec_moves_the_record_to_records():
    """The whole journey in one test: a record the queue lists, corrected, then postable.

    This is what `Fill` cannot do. No proof of delivery carries a spec code — a person reads it off
    the purchase order — so before this the row had no way out of the queue at all.
    """
    c = new_conn()
    record_id = extracted_records_store.write_pending(c, make(spec_code=None), NOW)
    assert record_id not in {r["id"] for r in read_views.records_ready(c)}
    assert record_id in {i.ref_id for i in read_views.manual_queue(c) if i.kind == "record"}

    result = record_edit.apply(c, row_for(c, record_id),
                               {"spec_code": "STE-402-LT-B"}, edited_by="Rahul")
    assert result.ok and result.is_complete, result.message

    assert record_id in {r["id"] for r in read_views.records_ready(c)}, "it should now be postable"
    assert record_id not in {i.ref_id for i in read_views.manual_queue(c) if i.kind == "record"}


def test_settling_a_quantity_clears_the_conflict_marker():
    """Without this the largest group in the queue is unfixable by the form built to fix it.

    `_READY_CLAUSE` excludes any record whose `extraction_source` carries `quantity_conflict`, and
    nothing else ever clears it. Forty-four of the sixty-five record rows are in that state, so a
    reviewer could type the right number into every one and watch none of them move.
    """
    c = new_conn()
    record_id = extracted_records_store.write_pending(
        c, make(extraction_source="excel:Hoja1+quantity_conflict"), NOW)
    assert record_id not in {r["id"] for r in read_views.records_ready(c)}

    result = record_edit.apply(c, row_for(c, record_id),
                               {"quantity_received": "12"}, edited_by="Rahul")
    assert result.ok and result.conflict_resolved, result.message
    assert "quantity_conflict" not in row_for(c, record_id)["extraction_source"]
    assert record_id in {r["id"] for r in read_views.records_ready(c)}


def test_confirming_the_quantity_already_there_settles_the_conflict():
    """The number being right already is the common case, not an edge case.

    Of the fifty conflicts in Premier's live store, forty-four carried a quantity the purchase order
    itself confirms — flagged because the grouper had put twenty-three different signage items under
    one spec, not because anyone disagreed about a count. A reviewer opening such a row types the
    number that is already in the box, and under the `new == old` rule that submitted nothing, fired
    nothing, and answered "nothing was changed". The row they had just settled stayed off the
    Records page for ever, with no second thing to try.
    """
    c = new_conn()
    record_id = extracted_records_store.write_pending(
        c, make(quantity_received=11.0, extraction_source="excel:Hoja1+quantity_conflict"), NOW)

    result = record_edit.apply(c, row_for(c, record_id),
                               {"quantity_received": "11"}, edited_by="Rahul")

    assert result.ok and result.conflict_resolved, result.message
    assert "quantity_conflict" not in row_for(c, record_id)["extraction_source"]
    assert row_for(c, record_id)["quantity_received"] == 11.0, "the quantity itself is unchanged"
    assert record_id in {r["id"] for r in read_views.records_ready(c)}


def test_an_empty_quantity_box_does_not_settle_a_conflict():
    """Submitting the form with the quantity cleared is not a reviewer stating a number, and must
    not clear a marker that says nobody has stated one."""
    c = new_conn()
    record_id = extracted_records_store.write_pending(
        c, make(extraction_source="excel:Hoja1+quantity_conflict"), NOW)

    result = record_edit.apply(c, row_for(c, record_id),
                               {"quantity_received": ""}, edited_by="Rahul")

    assert not result.conflict_resolved
    assert "quantity_conflict" in row_for(c, record_id)["extraction_source"]


def test_a_quantity_edit_that_settles_nothing_leaves_the_marker_alone():
    """The marker is cleared by settling the number, not by opening the form. A record with no
    conflict must not gain or lose anything from an unrelated edit."""
    c = new_conn()
    record_id = extracted_records_store.write_pending(c, make(), NOW)
    before = row_for(c, record_id)["extraction_source"]
    record_edit.apply(c, row_for(c, record_id), {"item_description": "Base, Floor Lamp 2"},
                      edited_by="Rahul")
    assert row_for(c, record_id)["extraction_source"] == before


def test_a_date_in_the_wrong_format_is_refused_not_stored():
    """`17/08/2026` reached the live store through the manual form. Everything downstream compares
    dates as ISO strings — the filter, the sort, the receipt log — so it sorts wrongly and does it
    silently."""
    c = new_conn()
    record_id = extracted_records_store.write_pending(c, make(), NOW)
    result = record_edit.apply(c, row_for(c, record_id),
                               {"pod_stated_date": "17/08/2026"}, edited_by="Rahul")
    assert not result.ok
    assert "YYYY-MM-DD" in result.message
    assert row_for(c, record_id)["pod_stated_date"] == "2025-10-01", "nothing may be written"


def test_a_quantity_that_is_not_a_number_is_refused():
    c = new_conn()
    record_id = extracted_records_store.write_pending(c, make(), NOW)
    result = record_edit.apply(c, row_for(c, record_id),
                               {"quantity_received": "eleven"}, edited_by="Rahul")
    assert not result.ok and "not a number" in result.message
    assert row_for(c, record_id)["quantity_received"] == 11.0


def test_an_edit_must_say_who_made_it():
    """A hand-typed quantity that cannot be told from one the machine read is worse than no edit."""
    c = new_conn()
    record_id = extracted_records_store.write_pending(c, make(), NOW)
    result = record_edit.apply(c, row_for(c, record_id), {"spec_code": "X"}, edited_by="  ")
    assert not result.ok and "who" in result.message
    assert row_for(c, record_id)["spec_code"] == "STE-402-LT-B"


def test_a_field_off_the_whitelist_is_ignored_rather_than_written():
    """The whitelist is the safety boundary, and `po_number` is the field it exists for: moving a
    delivery to another purchase order would move its receipt to another budget line."""
    c = new_conn()
    record_id = extracted_records_store.write_pending(c, make(), NOW)
    assert "po_number" not in EDITABLE_FIELDS
    record_edit.apply(c, row_for(c, record_id),
                      {"po_number": "999999", "extraction_confidence": "0.1"}, edited_by="Rahul")
    row = row_for(c, record_id)
    assert row["po_number"] == "208491"
    assert row["extraction_confidence"] == 1.0


def test_the_whitelist_is_exactly_what_somebody_decided_it_should_be():
    """The positive half of the rule above, and the reason it is a set rather than a tuple.

    Order is a presentation choice and may change. Membership is a decision: a thirteenth field
    cannot appear here without somebody editing this line, and the two absences are the point.

    `po_line_number` is absent for a subtler reason than `po_number`. A free-text line number does
    not merely risk a typo — `post_decision` gate 5 refuses a line matched on description alone
    *unless a person chose it*, and `po_verify` reads a record's stored line number as exactly that
    choice. So a transposed digit here would satisfy the check that exists to catch it. The
    sanctioned route is the alternatives table in the Verify popup, which shows what is being
    chosen before it is chosen.
    """
    assert set(EDITABLE_FIELDS) == {
        "spec_code", "item_description", "quantity_received", "unit_of_measure",
        "package_quantity", "package_uom",
        "pod_stated_date", "carrier_name", "tracking_number", "received_by",
        "vendor_name", "notification_number",
    }
    assert "po_number" not in EDITABLE_FIELDS
    assert "po_line_number" not in EDITABLE_FIELDS


def test_a_negative_package_count_is_refused_in_its_own_words():
    """Two numbers on one form need two sentences.

    The refusal was hard-coded to "a delivered quantity cannot be negative", which was correct
    while `quantity_received` was the only number and became a lie the moment `package_quantity`
    joined it. A message naming the wrong box sends somebody to fix a field that is already right.
    """
    c = new_conn()
    record_id = extracted_records_store.write_pending(c, make(package_quantity=41.0), NOW)
    result = record_edit.apply(c, row_for(c, record_id),
                               {"package_quantity": "-3"}, edited_by="Rahul")
    assert not result.ok
    assert "package count" in result.message and "delivered quantity" not in result.message
    assert result.errors == ["package_quantity"]
    assert row_for(c, record_id)["package_quantity"] == 41.0, "nothing should have been written"


def test_the_logistics_fields_are_writable():
    """The reason the whitelist widened: a reviewer looking at the Records table can correct what
    is in front of them, not only the five fields completeness happens to demand."""
    c = new_conn()
    record_id = extracted_records_store.write_pending(c, make(carrier_name="Nolan"), NOW)
    result = record_edit.apply(c, row_for(c, record_id), {
        "carrier_name": "FedEx", "tracking_number": "999", "received_by": "A Khan",
        "package_quantity": "41", "package_uom": "CTN", "vendor_name": "Authority Brands",
        "notification_number": "IN-88",
    }, edited_by="Rahul")
    assert result.ok, result.message
    row = row_for(c, record_id)
    assert (row["carrier_name"], row["tracking_number"], row["received_by"]) == (
        "FedEx", "999", "A Khan")
    assert (row["package_quantity"], row["package_uom"]) == (41.0, "CTN")
    assert (row["vendor_name"], row["notification_number"]) == ("Authority Brands", "IN-88")


def test_resubmitting_a_number_unchanged_is_not_a_correction():
    """`41.0` and `41` are the same package count, and the form shows the second.

    A REAL column reads back as `41.0`; the edit form strips that trailing `.0` before putting the
    value in a box somebody is about to retype. Compared as strings, saving a record nobody touched
    therefore looked like a change — it wrote the column, stamped `updated_at`, and once
    `record_edits` existed it logged a correction that never happened, on every single save.
    """
    c = new_conn()
    record_id = extracted_records_store.write_pending(
        c, make(quantity_received=11.0, package_quantity=41.0), NOW)
    result = record_edit.apply(c, row_for(c, record_id),
                               {"quantity_received": "11", "package_quantity": "41"},
                               edited_by="Rahul")
    assert not result.ok and result.message == "nothing was changed"
    assert edits_on(c, record_id) == []


# --- The audit trail --------------------------------------------------------------------------
#
# `extracted_records` carries `updated_at` and `created_by`, which say that somebody touched the
# row and who. They have never said which field, and never what it used to hold — so a quantity
# that arrived one way and posted another had no trail at all between the two.


def edits_on(c, record_id):
    c.row_factory = sqlite3.Row
    return [(r["field"], r["old_value"], r["new_value"], r["edited_by"])
            for r in c.execute("SELECT * FROM record_edits WHERE record_id = ? ORDER BY field",
                               (record_id,))]


def test_every_changed_field_is_recorded_with_what_it_used_to_hold():
    c = new_conn()
    record_id = extracted_records_store.write_pending(c, make(), NOW)
    result = record_edit.apply(c, row_for(c, record_id),
                               {"carrier_name": "FedEx", "quantity_received": "12"},
                               edited_by="Rahul")
    assert result.ok, result.message
    assert edits_on(c, record_id) == [
        ("carrier_name", "Nolan", "FedEx", "Rahul"),
        ("quantity_received", "11.0", "12", "Rahul"),
    ]


def test_a_cleared_field_is_recorded_as_cleared_not_as_absent():
    """The distinction `completeness._is_present` exists to preserve, kept in the log too."""
    c = new_conn()
    record_id = extracted_records_store.write_pending(c, make(), NOW)
    record_edit.apply(c, row_for(c, record_id), {"carrier_name": ""}, edited_by="Rahul")
    assert edits_on(c, record_id) == [("carrier_name", "Nolan", None, "Rahul")]


def test_a_refused_edit_records_nothing():
    """Both refusal paths — the validator and the unsigned form — must leave no trace. A log that
    records attempts as though they were changes is worse than no log."""
    c = new_conn()
    record_id = extracted_records_store.write_pending(c, make(), NOW)
    record_edit.apply(c, row_for(c, record_id), {"pod_stated_date": "17/08/2026"},
                      edited_by="Rahul")
    record_edit.apply(c, row_for(c, record_id), {"spec_code": "OTHER"}, edited_by="")
    assert edits_on(c, record_id) == []


def test_settling_a_quantity_conflict_is_recorded_as_an_edit():
    """Rewriting a provenance column on a person's behalf is precisely what an auditor asks about,
    and it is the one field on that row nobody typed."""
    c = new_conn()
    record_id = extracted_records_store.write_pending(
        c, make(extraction_source="excel:Hoja1+quantity_conflict"), NOW)
    result = record_edit.apply(c, row_for(c, record_id), {"quantity_received": "11"},
                               edited_by="Rahul")
    assert result.ok and result.conflict_resolved, result.message
    assert ("extraction_source", "excel:Hoja1+quantity_conflict", "excel:Hoja1", "Rahul") in \
        edits_on(c, record_id)


def test_the_column_and_its_audit_row_land_together():
    """The reason the INSERT is inside the same transaction as the UPDATE rather than after it.

    `sqlite3` opens a transaction implicitly on the first write and `apply` commits exactly once,
    so a failure recording the edit rolls back the change it was recording. There is no state in
    which the value moved and nothing says it did.
    """
    c = new_conn()
    record_id = extracted_records_store.write_pending(c, make(), NOW)

    class FailsOnAudit:
        """Everything the connection does, except record the edit. A wrapper rather than a
        monkeypatch because `sqlite3.Connection.executemany` is read-only."""

        def __getattr__(self, name):
            return getattr(c, name)

        def executemany(self, *args, **kwargs):
            raise sqlite3.OperationalError("the audit write failed")

    with pytest.raises(sqlite3.OperationalError):
        record_edit.apply(FailsOnAudit(), row_for(c, record_id), {"carrier_name": "FedEx"},
                          edited_by="Rahul")
    c.rollback()
    assert row_for(c, record_id)["carrier_name"] == "Nolan", "the change should not have survived"
    assert edits_on(c, record_id) == []


def test_history_reads_back_newest_first():
    """What the edit form shows above the boxes, so somebody opening a record can see it has been
    through hands before — the one question `updated_at` could never answer."""
    c = new_conn()
    record_id = extracted_records_store.write_pending(c, make(), NOW)
    record_edit.apply(c, row_for(c, record_id), {"carrier_name": "FedEx"}, edited_by="Rahul")
    record_edit.apply(c, row_for(c, record_id), {"carrier_name": "UPS"}, edited_by="Ayotunde")
    past = record_edit.history(c, record_id)
    assert [(e["field"], e["old_value"], e["new_value"], e["edited_by"]) for e in past] == [
        ("carrier_name", "FedEx", "UPS", "Ayotunde"),
        ("carrier_name", "Nolan", "FedEx", "Rahul"),
    ]
    assert len(record_edit.history(c, record_id, limit=1)) == 1


def test_every_record_the_queue_lists_can_be_opened_in_the_editor():
    """The invariant that catches this whole class of mistake.

    The editor first resolved records through `_READY_CLAUSE`, which withholds a record for a
    quantity conflict — so every conflicted row answered 404, and those are the queue's largest
    group. A row listed as needing a person, with no way to open it, is the dash problem again in
    a new place.
    """
    c = new_conn()
    for source in ("authority_inbound", "excel:Hoja1+quantity_conflict"):
        extracted_records_store.write_pending(c, make(spec_code=None, extraction_source=source), NOW)
    extracted_records_store.write_pending(
        c, make(spec_code=None, extraction_confidence=0.0), NOW)

    queued = {i.ref_id for i in read_views.manual_queue(c) if i.kind == "record"}
    fixable = {r["id"] for r in read_views.records_fixable(c)}
    assert queued, "nothing queued — the fixture no longer exercises this"
    assert queued - fixable == set(), "a queued record the editor cannot open"


def test_a_posted_record_is_not_fixable():
    """Editing one would change what a receipt already sent to Premier says it received."""
    c = new_conn()
    record_id = extracted_records_store.write_pending(c, make(), NOW)
    c.execute("UPDATE extracted_records SET status = 'pushed_to_spitfire' WHERE id = ?",
              (record_id,))
    c.commit()
    assert record_id not in {r["id"] for r in read_views.records_fixable(c)}
