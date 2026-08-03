from typing import List

from connectors.mailbox import Mailbox
from pipeline import extracted_records_store, ingest_orchestrator, state_db
from pipeline.models import Attachment, RawEmail
from pipeline.stage3_extract.excel_adapter import EXCEL_CONTENT_TYPE
from tests import corpus_fixtures as fx
from tests.test_extract import make_pdf_bytes


class FakeMailbox(Mailbox):
    def __init__(self, emails: List[RawEmail]):
        self._emails = emails
        self.processed_calls: List[tuple] = []

    def fetch_new(self) -> List[RawEmail]:
        return self._emails

    def mark_processed(self, email_id: str, folder: str) -> None:
        self.processed_calls.append((email_id, folder))


def make_email(**overrides):
    defaults = dict(
        email_id="msg-1",
        received_at="2026-06-08T14:00:00Z",
        sender_address="someone@example.com",
        sender_domain="example.com",
        subject="",
        body_html=None,
        body_text=None,
        attachments=[],
    )
    defaults.update(overrides)
    return RawEmail(**defaults)


def new_conn():
    return state_db.get_connection(":memory:")


def pending_records(c):
    return [row.record for row in extracted_records_store.get_pending(c)]


def test_clean_warehouse_delivery_staged_end_to_end():
    email = fx.inbound_email(email_id="msg-warehouse-1", notice="239475",
                             po_numbers=("208491",), shipment="50052 : 1")
    c = new_conn()
    mailbox = FakeMailbox([email])
    count = ingest_orchestrator.process_new_mail(mailbox, conn=c)
    assert count == 1
    record = pending_records(c)[0]
    assert record.po_number == "208491"
    assert record.spec_code == "STE-402-LT-B"
    # The Inbound format states the Spitfire line number outright, which is what makes the
    # downstream match exact instead of fuzzy.
    assert record.po_line_number == 300
    assert record.quantity_received == 11.0 and record.unit_of_measure == "EA"
    # 11 CTN is the carton count from the Package column, never the receivable quantity.
    assert record.package_quantity == 11.0 and record.package_uom == "CTN"
    assert record.carrier_name == "Nolan Transportation"
    assert record.tracking_number == "8840455"
    assert record.pod_stated_date == "2025-10-01"
    assert record.received_by == "Miguel C."
    assert mailbox.processed_calls == [("msg-warehouse-1", "Processed")]


def test_no_po_email_is_routed_and_nothing_staged():
    email = make_email(
        email_id="msg-wayfair-1",
        sender_address="marketing@wayfair.com", sender_domain="wayfair.com",
        subject="It's delivery day!", body_text="Your order is arriving today.",
    )
    c = new_conn()
    mailbox = FakeMailbox([email])
    count = ingest_orchestrator.process_new_mail(mailbox, conn=c)
    assert count == 0
    assert pending_records(c) == []
    assert mailbox.processed_calls == [("msg-wayfair-1", "Routed")]


def test_freight_status_email_is_hidden_and_moved_to_hidden_folder():
    email = make_email(
        email_id="msg-fedex-1",
        sender_address="tracking@fedex.com", sender_domain="fedex.com",
        subject="Your package has shipped", body_text="Your package has been shipped and is on its way.",
    )
    c = new_conn()
    mailbox = FakeMailbox([email])
    count = ingest_orchestrator.process_new_mail(mailbox, conn=c)
    assert count == 0
    assert pending_records(c) == []
    assert mailbox.processed_calls == [("msg-fedex-1", "Hidden")]


def test_triage_failure_moves_email_to_errors_folder(monkeypatch):
    email = make_email(email_id="msg-broken-1")
    monkeypatch.setattr(ingest_orchestrator, "triage", lambda e: (_ for _ in ()).throw(ValueError("boom")))
    c = new_conn()
    mailbox = FakeMailbox([email])
    count = ingest_orchestrator.process_new_mail(mailbox, conn=c)
    assert count == 0
    assert mailbox.processed_calls == [("msg-broken-1", "Errors")]


def test_mailbox_move_failure_does_not_crash_the_run(monkeypatch):
    email = fx.inbound_email(email_id="msg-warehouse-2", po_numbers=("208491",))

    class FlakyMailbox(FakeMailbox):
        def mark_processed(self, email_id, folder):
            raise RuntimeError("simulated Graph API hiccup")

    c = new_conn()
    count = ingest_orchestrator.process_new_mail(FlakyMailbox([email]), conn=c)
    assert count == 1
    assert pending_records(c)[0].po_number == "208491"


def test_same_delivery_in_body_and_attachment_deduplicates_to_one():
    """The Inbound body and its packing-slip PDF describe the same line. Agreeing quantities
    collapse to the higher-confidence record rather than staging two receipts."""
    pdf_bytes = make_pdf_bytes([["PO", "Spec", "Qty"], ["208491", "STE-402-LT-B", "11"]])
    email = fx.inbound_email(
        email_id="msg-dup-1", po_numbers=("208491",),
        attachments=[Attachment(filename="Packing Slip - Inbound 239336.pdf",
                                content_type="application/pdf", content_bytes=pdf_bytes)],
    )
    c = new_conn()
    count = ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=c)
    assert count == 1
    assert pending_records(c)[0].po_number == "208491"


def test_quantity_conflict_across_sources_keeps_both_flagged():
    """Disagreeing quantities are never silently resolved — both records are staged and flagged,
    because the corpus shows quantities legitimately disagreeing (overage, split part
    shipments) and picking one would be a guess."""
    pdf_bytes = make_pdf_bytes([["PO", "Spec", "Qty"], ["208491", "STE-402-LT-B", "5"]])
    email = fx.inbound_email(
        email_id="msg-conflict-1", po_numbers=("208491",),
        attachments=[Attachment(filename="Packing Slip.pdf",
                                content_type="application/pdf", content_bytes=pdf_bytes)],
    )
    c = new_conn()
    count = ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=c)
    assert count == 2
    records = pending_records(c)
    assert all("+quantity_conflict" in r.extraction_source for r in records)
    assert {r.quantity_received for r in records} == {11.0, 5.0}


def test_unrecognized_attachment_type_is_not_staged_as_a_fake_record():
    # Regression guard for a real bug found via real dummy test documents: an attachment type
    # no adapter recognizes must be ignored, never staged as a fabricated empty ExtractedRecord.
    email = fx.inbound_email(
        email_id="msg-unknown-attachment-1", po_numbers=("208491",),
        lines=[{"po": "208491", "line": "1", "part": "", "item": "no quantity, no spec"}],
        attachments=[Attachment(filename="archive.zip", content_type="application/zip",
                                content_bytes=b"whatever")],
    )
    c = new_conn()
    count = ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=c)
    records = pending_records(c)
    assert all(r.extraction_source != "unknown" for r in records)
    assert not any("archive.zip" in (r.raw_snippet or "") for r in records)


def test_one_corrupt_attachment_does_not_block_the_others():
    good_pdf = make_pdf_bytes([["PO", "Spec", "Qty"], ["208491", "LOB-203-PI", "3"]])
    email = fx.inbound_email(
        email_id="msg-mixed-1", po_numbers=("208491",),
        attachments=[
            Attachment(filename="corrupt.xlsx", content_type=EXCEL_CONTENT_TYPE,
                       content_bytes=b"PKnot a real xlsx"),
            Attachment(filename="pod.pdf", content_type="application/pdf", content_bytes=good_pdf),
        ],
    )
    c = new_conn()
    count = ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=c)
    assert count >= 1
    records = pending_records(c)
    assert any(r.spec_code == "LOB-203-PI" for r in records)
