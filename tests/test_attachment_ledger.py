"""The no-silent-drop invariant, end to end through `process_new_mail`.

`test_attachment_universality.py` proves each *kind* reaches a verdict. This proves the
*orchestrator* records one for every attachment on every email — including the routed and
hidden mail that never reaches extraction at all, which is where the failures that matter most
used to disappear.
"""

import io
import zipfile
from typing import List

import pytest

from connectors.mailbox import Mailbox
from pipeline import attachment_ledger, ingest_orchestrator, state_db
from pipeline.models import Attachment, RawEmail
from pipeline.parsing import sniff
from tests import corpus_fixtures as fx


class FakeMailbox(Mailbox):
    def __init__(self, emails: List[RawEmail]):
        self._emails = emails
        self.processed_calls: List[tuple] = []

    def fetch_new(self, skip_ids=None, since=None) -> List[RawEmail]:
        # Deliberately ignores `skip_ids`: it is advisory, and a connector that disregards it
        # must still be deduplicated by `stage1_ingest.fetch_new_emails`.
        return self._emails

    def mark_processed(self, email_id: str, folder: str) -> None:
        self.processed_calls.append((email_id, folder))


@pytest.fixture
def conn():
    connection = state_db.get_connection(":memory:")
    yield connection
    connection.close()


def attach(filename: str, data: bytes, **kwargs) -> Attachment:
    result = sniff.sniff(data, filename)
    return Attachment(
        filename=filename, content_type="", content_bytes=data,
        sha256=result.sha256, size_bytes=len(data), sniffed_kind=result.kind, **kwargs
    )


def make_xlsx() -> bytes:
    import openpyxl
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Vendor", "PO#", "Spec#", "QTY", "Item Description", "Confirmed Received: Yes or No"])
    sheet.append(["Daniel Stuart", "207030", "LOB-203-PI", 12, "Throw Pillow", "yes"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_every_attachment_is_ledgered_even_on_a_routed_email(conn):
    """A routed email never reaches extraction, so anything recorded there structurally cannot
    see it — and routing is exactly where a photographed POD with no readable text ends up."""
    photo = b"\xff\xd8\xff\xe0" + b"\x00" * (700 * 1024)
    email = RawEmail(
        email_id="msg-routed", received_at="2026-08-03T00:00:00Z",
        sender_address="johngallo@premierpm.com", sender_domain="premierpm.com",
        subject="Fw: Cameo Harbour Delivery",
        body_html=None, body_text="Attached are the BOL and Packing slips.",
        attachments=[attach("IMG_2479.jpeg", photo)],
    )
    ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=conn)

    rows = attachment_ledger.for_email(conn, "msg-routed")
    assert len(rows) == 1
    assert rows[0].sniffed_kind == sniff.KIND_IMAGE
    assert rows[0].disposition in attachment_ledger.TERMINAL
    assert attachment_ledger.orphans(conn) == []


def test_a_mixed_email_accounts_for_every_attachment(conn):
    """One message carrying a real tracker, a logo, a duplicate of the tracker, a zip and an
    unidentifiable blob. Every one must be accounted for exactly once."""
    tracker = make_xlsx()
    logo = b"\x89PNG\r\n\x1a\n" + b"\x00" * 2000

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes.txt", "PO 208491 received, spec STE-402-LT-B, 11 EA")
    bundle = buffer.getvalue()

    email = fx.inbound_email(email_id="msg-mixed", po_numbers=("208491",))
    email.attachments = [
        attach("Cameo Receivers.xlsx", tracker),
        attach("image001.png", logo, drop_hint="decorative:tiny"),
        attach("copy of tracker.xlsx", tracker, drop_hint="duplicate:abc123"),
        attach("bundle.zip", bundle),
        attach("mystery.dat", b"\x00\x01\xfe\xff" + bytes(range(200, 256)) * 4),
    ]

    ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=conn)
    rows = attachment_ledger.for_email(conn, "msg-mixed")
    by_name = {r.filename: r for r in rows}

    assert by_name["Cameo Receivers.xlsx"].disposition == attachment_ledger.EXTRACTED
    assert by_name["Cameo Receivers.xlsx"].records_extracted >= 1
    assert by_name["image001.png"].disposition == attachment_ledger.DROPPED_DECORATIVE
    assert by_name["copy of tracker.xlsx"].disposition == attachment_ledger.DROPPED_DUPLICATE
    assert by_name["bundle.zip"].disposition == attachment_ledger.CONTAINER_EXPANDED
    assert by_name["mystery.dat"].disposition == attachment_ledger.UNSUPPORTED_FORMAT

    assert "notes.txt" in by_name, "a zip member needs its own row"
    assert by_name["notes.txt"].depth == 1
    assert attachment_ledger.orphans(conn) == []

    # Dropped attachments carry metadata only — the point is the record, not the bytes.
    assert by_name["image001.png"].size_bytes > 0
    assert by_name["image001.png"].sniffed_kind == sniff.KIND_IMAGE


def test_an_oversize_attachment_is_quarantined_not_dropped(conn, monkeypatch):
    monkeypatch.setattr("config.settings.MAX_ATTACHMENT_BYTES", 1024)
    big = b"\xff\xd8\xff\xe0" + b"\x00" * 5000

    email = fx.inbound_email(email_id="msg-big", po_numbers=("208491",))
    email.attachments = [attach("huge.jpg", big)]

    mailbox = FakeMailbox([email])
    ingest_orchestrator.process_new_mail(mailbox, conn=conn)

    row = attachment_ledger.for_email(conn, "msg-big")[0]
    assert row.disposition == attachment_ledger.DROPPED_OVERSIZE
    assert row.size_bytes == len(big), "the size must survive even though the bytes do not"
    assert attachment_ledger.orphans(conn) == []


def test_unreadable_attachments_surface_for_review(conn):
    """A corrupt workbook must reach a person, with a stated reason."""
    email = fx.inbound_email(email_id="msg-corrupt", po_numbers=("208491",))
    email.attachments = [attach("broken.xlsx", b"PK\x03\x04" + b"xl/" + b"\x00" * 200)]

    ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=conn)

    attention = attachment_ledger.list_needing_attention(conn)
    assert [r.filename for r in attention] == ["broken.xlsx"]
    assert attention[0].disposition in (attachment_ledger.CORRUPT, attachment_ledger.UNREADABLE)
    assert attention[0].review_status == attachment_ledger.REVIEW_PENDING
    assert attention[0].disposition_detail


def make_xlsx_for_po(po_number: str, spec: str = "LOB-203-PI", quantity: int = 12) -> bytes:
    import openpyxl
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Vendor", "PO#", "Spec#", "QTY", "Item Description", "Confirmed Received: Yes or No"])
    sheet.append(["Daniel Stuart", po_number, spec, quantity, "Throw Pillow", "yes"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_a_tracker_line_reaches_staging_alongside_the_notification(conn):
    """An attachment line for this delivery, on a spec the notification does not mention, is
    staged in its own right."""
    from pipeline import extracted_records_store

    email = fx.inbound_email(email_id="msg-count", po_numbers=("208491",))
    email.attachments = [attach("Cameo Receivers.xlsx", make_xlsx_for_po("208491"))]

    ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=conn)

    ledger_total = sum(r.records_extracted for r in attachment_ledger.for_email(conn, "msg-count"))
    assert ledger_total >= 1
    staged = extracted_records_store.get_pending(conn)
    assert any(r.record.spec_code == "LOB-203-PI" for r in staged)


def test_the_same_line_from_two_sources_collapses_to_one(conn):
    """The notification body and an attached tracker describing the same PO line agree, so one
    record is staged, not two — the attachment is still recorded as read."""
    from pipeline import extracted_records_store

    email = fx.inbound_email(email_id="msg-dupline", po_numbers=("208491",))
    # The fixture's own line is STE-402-LT-B, 11 EA; the tracker restates it with the same qty.
    email.attachments = [
        attach("Cameo Receivers.xlsx", make_xlsx_for_po("208491", "STE-402-LT-B", quantity=11))
    ]

    ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=conn)

    row = next(r for r in attachment_ledger.for_email(conn, "msg-dupline")
               if r.filename == "Cameo Receivers.xlsx")
    assert row.disposition == attachment_ledger.EXTRACTED, "the read is recorded even when the record loses"

    staged = extracted_records_store.get_pending(conn)
    matching = [r for r in staged if r.record.spec_code == "STE-402-LT-B"]
    assert len(matching) == 1, "one physical line must not become two receipts"
    assert matching[0].record.extraction_source == "authority_inbound", \
        "the richer source wins — it states the Spitfire line number"


def test_disagreeing_quantities_are_flagged_never_silently_resolved(conn):
    """The notification says 11, the tracker says 12. Both are kept and marked, because the
    corpus shows quantities legitimately disagreeing — overage, split part shipments — and
    picking one would be a guess dressed up as a receipt."""
    from pipeline import extracted_records_store

    email = fx.inbound_email(email_id="msg-conflict", po_numbers=("208491",))
    email.attachments = [
        attach("Cameo Receivers.xlsx", make_xlsx_for_po("208491", "STE-402-LT-B", quantity=12))
    ]

    ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=conn)

    staged = [r for r in extracted_records_store.get_pending(conn)
              if r.record.spec_code == "STE-402-LT-B"]
    assert len(staged) == 2
    assert all("quantity_conflict" in r.record.extraction_source for r in staged)
    assert {r.record.quantity_received for r in staged} == {11.0, 12.0}


def test_the_ledger_counts_reads_not_receipts(conn):
    """A tracker row for a PO other than this delivery's is read, ledgered, and then correctly
    excluded from staging.

    These are different numbers on purpose: the ledger answers "did we read this file", while
    staging answers "does this line belong to this delivery". Conflating them would either hide
    a genuine read or invent a receipt against the wrong PO.
    """
    from pipeline import extracted_records_store

    email = fx.inbound_email(email_id="msg-otherpo", po_numbers=("208491",))
    email.attachments = [attach("Cameo Receivers.xlsx", make_xlsx_for_po("207030"))]

    ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=conn)

    row = next(r for r in attachment_ledger.for_email(conn, "msg-otherpo")
               if r.filename == "Cameo Receivers.xlsx")
    assert row.disposition == attachment_ledger.EXTRACTED
    assert row.records_extracted == 1, "the read happened and is recorded"

    staged = extracted_records_store.get_pending(conn)
    assert not any(r.record.po_number == "207030" for r in staged), \
        "a line for another PO must never be staged against this delivery"


def test_forget_message_allows_a_fixed_reader_to_retry(conn):
    """Without this the ledger is a graveyard: `is_new_message` marks mail seen permanently, so
    an attachment we could not read today would never be retried after the reader is written."""
    email = fx.inbound_email(email_id="msg-retry", po_numbers=("208491",))
    ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=conn)
    assert state_db.is_new_message(conn, "msg-retry", "2026-08-03") is False

    state_db.forget_message(conn, "msg-retry")
    assert state_db.is_new_message(conn, "msg-retry", "2026-08-03") is True
