"""A re-read after an OCR outage must leave records behind, not just a tidier ledger.

`tools/reextract.py` re-read every attachment the outage refused, updated its ledger row, printed
"N record(s) available" — and dropped the records on the floor. `dispatch.dispatch_source` returns
records and stamps the ledger; it has never written `extracted_records`, and nothing in the tool
did either. Recovering 737 attachments would have spent the OCR budget and staged nothing.

These are the tests whose absence let that ship.
"""

import io as _io
import sqlite3

import pytest

from pipeline import email_log, state_db
from pipeline.models import ExtractedRecord
from tools import reextract


def make_record(**overrides) -> ExtractedRecord:
    defaults = dict(
        source_email_id="msg-ocr-1", po_number="210634", shipment_number=None,
        spec_code="GR-350a-WTF", parent_spec_code=None, sub_spec_suffix=None,
        item_description="Sheer Fabric", vendor_name=None, carrier_name=None,
        tracking_number=None, quantity_received=202.0, unit_of_measure="YD",
        pod_stated_date="2026-08-12", email_date="2026-08-12T11:05:00Z",
        delivery_location=None, comments=None, extraction_source="ocr",
        extraction_confidence=0.6, raw_snippet="",
    )
    defaults.update(overrides)
    return ExtractedRecord(**defaults)


def add_accumulation(conn, po_number, email_id, delivery_ref):
    conn.execute(
        "INSERT INTO accumulation (po_number, shipment_number, email_id, notification_type, "
        "category, received_at, payload_json, delivery_ref, delivery_rung) "
        "VALUES (?, NULL, ?, 'DELIVERED', 'delivery', '2026-08-12T11:05:00Z', '{}', ?, 'notice')",
        (po_number, email_id, delivery_ref))


def seeded_conn(*, po_number="210634", email_id="msg-ocr-1", is_pod=0):
    """A store holding one accumulated delivery and the ledger row for its scanned attachment."""
    conn = state_db.get_connection(":memory:")
    add_accumulation(conn, po_number, email_id, "notice-49985")
    conn.execute(
        "INSERT INTO attachment_ledger (id, email_id, ordinal, depth, filename, sniffed_kind, "
        "  disposition, claimed_by, is_inline, is_pod, pod_po_numbers, first_seen_at) "
        "VALUES (77, ?, 0, 0, 'POD.pdf', 'pdf', 'service_unavailable', 'OcrAdapter', 0, ?, ?, "
        "        '2026-08-12T11:05:00Z')",
        (email_id, is_pod, po_number if is_pod else ""))
    conn.commit()
    return conn


def ledger_row(conn, ledger_id=77):
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM attachment_ledger WHERE id = ?", (ledger_id,)).fetchone()


def staged(conn):
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM extracted_records ORDER BY id").fetchall()


def test_a_reread_stages_the_records_it_produced():
    """The whole point. Before the fix this left the table empty."""
    conn = seeded_conn()
    ids, unattributed = reextract._stage(conn, [make_record()], ledger_row(conn),
                                         "2026-09-02T12:00:00Z")

    assert len(ids) == 1, "the record the re-read produced was not written anywhere"
    assert unattributed == 0
    row = staged(conn)[0]
    assert row["po_number"] == "210634"
    assert row["quantity_received"] == 202.0
    assert row["status"] == "pending"


def test_the_staged_record_is_joined_to_its_delivery():
    """A record with no `delivery_id` is a receipt belonging to no delivery — it shows on no page."""
    conn = seeded_conn()
    reextract._stage(conn, [make_record()], ledger_row(conn), "2026-09-02T12:00:00Z")

    row = staged(conn)[0]
    assert row["delivery_id"] is not None
    assert row["delivery_key"], "without a delivery key the dedupe guard cannot see this row"

    delivery = conn.execute("SELECT * FROM deliveries WHERE id = ?",
                            (row["delivery_id"],)).fetchone()
    assert delivery["po_number"] == "210634"
    assert delivery["delivery_ref"] == "notice-49985", "must reuse the ref Stage 2 accumulated under"


def test_a_pod_on_the_message_is_linked_to_the_record():
    """`pod_ledger_id` is what tells "a carrier proved this" apart from "nobody confirmed it"."""
    conn = seeded_conn(is_pod=1)
    reextract._stage(conn, [make_record()], ledger_row(conn), "2026-09-02T12:00:00Z")

    row = staged(conn)[0]
    assert row["pod_ledger_id"] == 77
    assert row["pod_source"] == "attachment"


def test_running_the_same_recovery_twice_stages_one_record():
    """A re-run after a partial outage must not double-count the delivery."""
    conn = seeded_conn()
    first, _ = reextract._stage(conn, [make_record()], ledger_row(conn), "2026-09-02T12:00:00Z")
    second, _ = reextract._stage(conn, [make_record()], ledger_row(conn), "2026-09-02T12:05:00Z")

    assert len(first) == 1 and second == []
    assert len(staged(conn)) == 1
    assert len(conn.execute("SELECT id FROM deliveries").fetchall()) == 1


def test_a_record_for_a_po_this_message_never_accumulated_is_not_staged():
    """Better on the exception queue than attributed to a delivery it has nothing to do with."""
    conn = seeded_conn(po_number="210634")
    ids, unattributed = reextract._stage(conn, [make_record(po_number="999999")],
                                         ledger_row(conn), "2026-09-02T12:00:00Z")

    assert ids == [] and unattributed == 1
    assert staged(conn) == []


def test_a_record_with_no_po_adopts_the_only_delivery_on_the_message():
    """A carrier POD names a PO; a free-text pass may name none. One candidate is unambiguous."""
    conn = seeded_conn()
    ids, unattributed = reextract._stage(conn, [make_record(po_number="")],
                                         ledger_row(conn), "2026-09-02T12:00:00Z")

    assert len(ids) == 1 and unattributed == 0
    assert staged(conn)[0]["po_number"] == "210634"


def test_a_record_with_no_po_on_a_multi_po_message_is_left_for_a_person():
    """Two candidates and no way to choose: guessing here posts goods against the wrong order."""
    conn = seeded_conn()
    add_accumulation(conn, "210635", "msg-ocr-1", "notice-49986")
    conn.commit()

    ids, unattributed = reextract._stage(conn, [make_record(po_number="")],
                                         ledger_row(conn), "2026-09-02T12:00:00Z")

    assert ids == [] and unattributed == 1


def test_an_unaccumulated_message_stages_nothing_but_is_not_an_error():
    """`route` and `hold` mail has no delivery to hang a record on — the ledger keeps the verdict."""
    conn = state_db.get_connection(":memory:")
    conn.execute(
        "INSERT INTO attachment_ledger (id, email_id, ordinal, depth, filename, sniffed_kind, "
        "  disposition, claimed_by, is_inline, first_seen_at) "
        "VALUES (78, 'msg-routed-1', 0, 0, 'photo.jpg', 'image', 'service_unavailable', "
        "        'OcrAdapter', 0, '2026-08-12T11:05:00Z')")
    conn.commit()

    ids, unattributed = reextract._stage(conn, [make_record(source_email_id="msg-routed-1")],
                                         ledger_row(conn, 78), "2026-09-02T12:00:00Z")

    assert ids == []
    assert unattributed == 1, "counted, so the run reports it rather than losing it silently"


@pytest.mark.parametrize("size, floor, skipped", [
    ((40, 40), 307200, True),        # a signature logo
    ((1600, 1200), 307200, False),   # a photographed packing slip
    ((40, 40), 0, False),            # floor off: nothing is skipped
])
def test_the_size_floor_skips_logos_and_keeps_documents(size, floor, skipped):
    from PIL import Image
    buffer = _io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")

    assert reextract._too_small_to_be_a_document(buffer.getvalue(), floor) is skipped


def test_bytes_that_are_not_an_image_are_never_skipped_by_the_floor():
    """Unreadable here is the adapter's call, not the filter's — refusing it would lose the file."""
    assert reextract._too_small_to_be_a_document(b"%PDF-1.4 not an image", 307200) is False


# --- the delivery-date fallback ---------------------------------------------
# `pod_stated_date` is one of the five fields `completeness` requires and the only one no other
# system holds, so a document that states no date strands an otherwise complete receipt. The mail
# bounds when the delivery happened — it cannot have been reported before it occurred — so the
# message's own date stands in, and the row records which date it used.

def test_a_document_without_a_date_borrows_the_messages_own():
    conn = seeded_conn()
    email_log.record(conn, email_id="msg-ocr-1", subject="RR", sender="a@b.com",
                     email_date="2026-08-26T10:00:00Z", origin_sent_at="",
                     category="delivery", folder="Processed",
                     processed_at="2026-08-26T10:05:00Z")

    reextract._stage(conn, [make_record(pod_stated_date=None)], ledger_row(conn),
                     "2026-09-02T12:00:00Z")

    row = staged(conn)[0]
    assert row["pod_stated_date"] == "2026-08-26"
    assert row["pod_source"] == "email_received_date", "the substitution must be visible"


def test_a_forwarded_thread_takes_the_date_premier_received_it():
    """The received date, even when the message was written days earlier.

    This asserted the opposite until 2026-09-03: `_fallback_delivery_date` preferred
    `origin_sent_at`, on the reasoning that a thread forwarded later still describes the delivery
    its author saw. Premier then stated the rule — document date if given, otherwise the date the
    mail was *received* — and that reasoning, however good, is not the rule.

    It matters on 48 of the mailbox's 1,642 messages, where the two dates fall on different days.
    Kept as a test rather than deleted because the forwarded thread is exactly the case where the
    two diverge, so it is the case that proves which rule is in force.
    """
    conn = seeded_conn()
    email_log.record(conn, email_id="msg-ocr-1", subject="Fw: RR", sender="a@b.com",
                     email_date="2026-08-26T10:00:00Z", origin_sent_at="2026-08-20T09:00:00Z",
                     category="delivery", folder="Processed",
                     processed_at="2026-08-26T10:05:00Z")

    reextract._stage(conn, [make_record(pod_stated_date=None)], ledger_row(conn),
                     "2026-09-02T12:00:00Z")

    row = staged(conn)[0]
    assert row["pod_stated_date"] == "2026-08-26", "the received date, not the sent date"
    assert row["pod_source"] == "email_received_date"


def test_a_date_the_document_states_is_never_replaced():
    conn = seeded_conn()
    email_log.record(conn, email_id="msg-ocr-1", subject="RR", sender="a@b.com",
                     email_date="2026-08-26T10:00:00Z", origin_sent_at="",
                     category="delivery", folder="Processed",
                     processed_at="2026-08-26T10:05:00Z")

    reextract._stage(conn, [make_record(pod_stated_date="2026-08-14")], ledger_row(conn),
                     "2026-09-02T12:00:00Z")

    row = staged(conn)[0]
    assert row["pod_stated_date"] == "2026-08-14"
    assert row["pod_source"] is None, "nothing was substituted, so nothing to record"


def test_a_linked_pod_keeps_its_own_provenance():
    """`pod_source` says which file proved the receipt; a borrowed date must not overwrite that."""
    conn = seeded_conn(is_pod=1)
    email_log.record(conn, email_id="msg-ocr-1", subject="RR", sender="a@b.com",
                     email_date="2026-08-26T10:00:00Z", origin_sent_at="",
                     category="delivery", folder="Processed",
                     processed_at="2026-08-26T10:05:00Z")

    reextract._stage(conn, [make_record(pod_stated_date=None)], ledger_row(conn),
                     "2026-09-02T12:00:00Z")

    row = staged(conn)[0]
    assert row["pod_source"] == "attachment"
    assert row["pod_ledger_id"] == 77
