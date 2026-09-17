"""The backfill that gives a document's second purchase order the records it already proves.

The pipeline fix only changes mail read from now on. These cover the two judgements the repair
makes about mail already in the store: which purchase orders are worth seeding a delivery for, and
what seeding one actually writes.
"""

import sqlite3

from pipeline import state_db
from tools import repair_multi_po_documents as repair


def test_a_second_order_on_the_same_document_is_worth_seeding():
    """The measured case: one receiving report, ten lines, two orders. The message accumulated
    only the first, so the other seven lines have no delivery to be staged against."""
    named = {"22723:WarehouseReceivingReport.pdf": {"212401", "212454"}}

    assert repair._corroborated_missing(named, {"212401"}) == {"212454"}


def test_a_number_on_a_document_that_names_nothing_we_know_is_not_seeded():
    """`COTA 211798 RR#3.pdf` heads a band `Customer PO #` over `453295`, which is its **bill of
    lading** — `Bill Of Lading Number: 4532957` appears further down the same page. The order it is
    really against already has its delivery.

    Seeding here would invent a purchase order out of a BOL, which is precisely what the per-cell
    mirror check in `stage3_extract/base.py` exists to prevent. A document that corroborates
    nothing we already believe is not evidence for creating a delivery.
    """
    named = {"598:COTA 211798 RR#3.pdf": {"453295"}}

    assert repair._corroborated_missing(named, {"213993"}) == set()


def test_only_the_corroborating_document_speaks_for_its_own_numbers():
    """One document vouching for itself says nothing about a different attachment's numbers."""
    named = {
        "1:receiving report.pdf": {"212401", "212454"},   # names one we hold -> its other order counts
        "2:some freight bill.pdf": {"999999"},            # names none we hold -> ignored
    }

    assert repair._corroborated_missing(named, {"212401"}) == {"212454"}


def test_seeding_copies_the_delivery_the_message_already_has():
    """The seeded rows must share the message's `delivery_ref`.

    `deliveries` is keyed `UNIQUE(po_number, delivery_ref)`, so sharing the ref is what makes one
    shipment become one delivery *per purchase order* rather than two unrelated deliveries — the
    shape Premier asked for, and the shape the grouped post relies on.
    """
    conn = state_db.get_connection(":memory:")
    conn.row_factory = sqlite3.Row
    ref = "message:<m1@mail.dll>"
    conn.execute(
        "INSERT INTO accumulation (po_number, shipment_number, email_id, notification_type, "
        "category, received_at, payload_json, delivery_ref, delivery_rung) "
        "VALUES ('212401', NULL, '<m1@mail.dll>', 'property_confirmation', 'hold', "
        "'2026-09-11T10:00:00Z', '', ?, 'message')", (ref,))
    conn.commit()

    assert repair._seed_delivery(conn, "<m1@mail.dll>", "212454", "2026-09-17T00:00:00Z")

    seeded = conn.execute(
        "SELECT * FROM accumulation WHERE email_id = ? AND po_number = '212454'",
        ("<m1@mail.dll>",)).fetchone()
    assert seeded["delivery_ref"] == ref, "same shipment, so the same delivery ref"
    assert seeded["delivery_rung"] == "message"
    assert seeded["notification_type"] == "property_confirmation", "copied, not invented"

    released = conn.execute(
        "SELECT * FROM released_events WHERE po_number = '212454'").fetchone()
    assert released is not None, "the delivery must be released or nothing stages against it"
    assert "repair" in released["release_reason"], \
        "a backfilled row that reads like Stage 2's own decision is one nobody can audit"


def test_seeding_a_message_that_never_accumulated_does_nothing():
    """No row to copy means no delivery to share. Inventing a ref here would be a second opinion
    about a message Stage 2 has not finished with."""
    conn = state_db.get_connection(":memory:")
    conn.row_factory = sqlite3.Row

    assert repair._seed_delivery(conn, "<unknown@mail.dll>", "212454", "2026-09-17T00:00:00Z") is False
