from typing import List

from connectors.mailbox import Mailbox
from pipeline import extracted_records_store, ingest_orchestrator, state_db
from pipeline.models import Attachment, RawEmail
from pipeline.stage3_extract.excel_adapter import EXCEL_CONTENT_TYPE
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
    email = make_email(
        email_id="msg-warehouse-1",
        sender_address="notify@authoritylogistics.com", sender_domain="authoritylogistics.com",
        subject="Inbound - PO 213987",
        body_html="<table><tr><th>PO</th><th>Spec</th><th>Qty</th></tr><tr><td>213987</td><td>LI-12</td><td>1</td></tr></table>",
    )
    c = new_conn()
    mailbox = FakeMailbox([email])
    count = ingest_orchestrator.process_new_mail(mailbox, conn=c)
    assert count == 1
    records = pending_records(c)
    assert records[0].po_number == "213987"
    assert records[0].spec_code == "LI-12"
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
    email = make_email(
        email_id="msg-warehouse-2",
        sender_address="notify@authoritylogistics.com", sender_domain="authoritylogistics.com",
        subject="Inbound - PO 213987",
        body_html="<table><tr><th>PO</th><th>Spec</th><th>Qty</th></tr><tr><td>213987</td><td>LI-12</td><td>1</td></tr></table>",
    )

    class FlakyMailbox(FakeMailbox):
        def mark_processed(self, email_id, folder):
            raise RuntimeError("simulated Graph API hiccup")

    c = new_conn()
    count = ingest_orchestrator.process_new_mail(FlakyMailbox([email]), conn=c)
    assert count == 1
    assert pending_records(c)[0].po_number == "213987"


def test_same_delivery_in_body_and_attachment_deduplicates_to_one():
    pdf_bytes = make_pdf_bytes([["PO", "Spec", "Qty"], ["208491", "LI-1", "2"]])
    email = make_email(
        email_id="msg-dup-1",
        sender_address="notify@authoritylogistics.com", sender_domain="authoritylogistics.com",
        subject="Inbound - PO 208491",
        body_html="<table><tr><th>PO</th><th>Spec</th><th>Qty</th></tr><tr><td>208491</td><td>LI-1</td><td>2</td></tr></table>",
        attachments=[Attachment(filename="pod.pdf", content_type="application/pdf", content_bytes=pdf_bytes)],
    )
    c = new_conn()
    count = ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=c)
    assert count == 1
    assert pending_records(c)[0].po_number == "208491"


def test_quantity_conflict_across_sources_keeps_both_flagged():
    pdf_bytes = make_pdf_bytes([["PO", "Spec", "Qty"], ["208491", "LI-1", "5"]])
    email = make_email(
        email_id="msg-conflict-1",
        sender_address="notify@authoritylogistics.com", sender_domain="authoritylogistics.com",
        subject="Inbound - PO 208491",
        body_html="<table><tr><th>PO</th><th>Spec</th><th>Qty</th></tr><tr><td>208491</td><td>LI-1</td><td>2</td></tr></table>",
        attachments=[Attachment(filename="pod.pdf", content_type="application/pdf", content_bytes=pdf_bytes)],
    )
    c = new_conn()
    count = ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=c)
    assert count == 2
    records = pending_records(c)
    assert all("+quantity_conflict" in r.extraction_source for r in records)
    assert {r.quantity_received for r in records} == {2.0, 5.0}


def test_one_corrupt_attachment_does_not_block_the_others():
    good_pdf = make_pdf_bytes([["PO", "Spec", "Qty"], ["300111", "AB-1", "3"]])
    email = make_email(
        email_id="msg-mixed-1",
        sender_address="notify@authoritylogistics.com", sender_domain="authoritylogistics.com",
        subject="Inbound - PO 300111",
        body_text="Inbound received for PO 300111.",
        attachments=[
            Attachment(filename="corrupt.xlsx", content_type=EXCEL_CONTENT_TYPE, content_bytes=b"not a real xlsx"),
            Attachment(filename="pod.pdf", content_type="application/pdf", content_bytes=good_pdf),
        ],
    )
    c = new_conn()
    count = ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=c)
    assert count >= 1
    records = pending_records(c)
    assert any(r.po_number == "300111" and r.spec_code == "AB-1" for r in records)
