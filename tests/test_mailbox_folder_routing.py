import json

from connectors.mailbox import LocalFolderMailbox
from pipeline import extracted_records_store, ingest_orchestrator, state_db
from tests import corpus_fixtures as fx


def write_email(folder, **overrides):
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
    (folder / f"{defaults['email_id']}.json").write_text(json.dumps(defaults), encoding="utf-8")


def new_conn():
    return state_db.get_connection(":memory:")


def test_dummy_emails_are_sorted_into_the_right_folders_after_one_pass(tmp_path):
    write_email(
        tmp_path, email_id="msg-hidden-1",
        sender_address="tracking@fedex.com", sender_domain="fedex.com",
        subject="Your package has shipped", body_text="Your package has been shipped and is on its way.",
    )
    write_email(
        tmp_path, email_id="msg-routed-1",
        sender_address="marketing@example-retail.test", sender_domain="example-retail.test",
        subject="It's delivery day!", body_text="Your order is arriving today.",
    )
    inbound = fx.inbound_email(email_id="msg-processed-1", po_numbers=("908491",))
    write_email(
        tmp_path, email_id="msg-processed-1",
        sender_address=inbound.sender_address, sender_domain=inbound.sender_domain,
        subject=inbound.subject, body_html=inbound.body_html,
    )

    mailbox = LocalFolderMailbox(tmp_path)
    c = new_conn()
    count = ingest_orchestrator.process_new_mail(mailbox, conn=c)

    assert count == 1
    assert [r.record.po_number for r in extracted_records_store.get_pending(c)] == ["908491"]

    assert not (tmp_path / "msg-hidden-1.json").exists()
    assert not (tmp_path / "msg-routed-1.json").exists()
    assert not (tmp_path / "msg-processed-1.json").exists()
    assert (tmp_path / "Hidden" / "msg-hidden-1.json").exists()
    assert (tmp_path / "Routed" / "msg-routed-1.json").exists()
    assert (tmp_path / "Processed" / "msg-processed-1.json").exists()


def test_moved_dummy_emails_are_never_reprocessed_on_a_second_pass(tmp_path):
    write_email(
        tmp_path, email_id="msg-hidden-2",
        sender_address="tracking@fedex.com", sender_domain="fedex.com",
        subject="Your package has shipped", body_text="Your package has been shipped and is on its way.",
    )
    mailbox = LocalFolderMailbox(tmp_path)
    c = new_conn()

    first_count = ingest_orchestrator.process_new_mail(mailbox, conn=c)
    second_count = ingest_orchestrator.process_new_mail(mailbox, conn=c)

    assert first_count == 0
    assert second_count == 0
    assert (tmp_path / "Hidden" / "msg-hidden-2.json").exists()
