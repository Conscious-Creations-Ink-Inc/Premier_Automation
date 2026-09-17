"""Every email must leave exactly one row, on every exit path — including the failure one.

The bug this guards: a ROUTE or HIDE email exits before extraction, and an email with no
attachments creates no attachment_ledger row either, so it used to leave no trace at all beyond
an id in seen_message_ids. The categories that most need a person were invisible.
"""
import pytest

from pipeline import email_log, ingest_orchestrator, state_db
from pipeline.models import TriageCategory
from tests import corpus_fixtures as fx
from tests.test_ingest_orchestrator import FakeMailbox, make_email, new_conn


def route_email(email_id="msg-route"):
    """Rule 7 — no PO reference anywhere, so triage routes it to a person."""
    return make_email(
        email_id=email_id,
        subject="Your order has shipped!",
        sender_address="marketing@example-retail.test",
        sender_domain="example-retail.test",
        body_text="Thanks for shopping with us. Track your package in your account.",
    )


def hide_email(email_id="msg-hide"):
    """Rule 2 — freight status noise, discarded."""
    return make_email(
        email_id=email_id,
        subject="FedEx Shipment 771234567890: Delivered",
        sender_address="TrackingUpdates@fedex.com",
        sender_domain="fedex.com",
        body_text="Your package was delivered.",
    )


def test_every_exit_path_writes_exactly_one_row(monkeypatch):
    surface = fx.inbound_email(email_id="msg-surface", notice="939475",
                               po_numbers=("908491",), shipment="90052 : 1")
    boom = make_email(email_id="msg-boom", subject="detonates in triage")

    real_triage = ingest_orchestrator.triage

    def exploding_triage(email, evidence=None):
        if email.email_id == "msg-boom":
            raise ValueError("triage blew up")
        return real_triage(email, evidence=evidence)

    monkeypatch.setattr(ingest_orchestrator, "triage", exploding_triage)

    c = new_conn()
    mailbox = FakeMailbox([surface, route_email(), hide_email(), boom])
    ingest_orchestrator.process_new_mail(mailbox, conn=c)

    rows = {row.email_id: row for row in email_log.list_all(c)}
    assert len(rows) == 4, "one row per email, no more and no fewer"
    assert email_log.count(c) == 4

    assert rows["msg-surface"].folder == "Processed"
    assert rows["msg-route"].folder == "Routed"
    assert rows["msg-route"].category == TriageCategory.ROUTE.value
    assert rows["msg-hide"].folder == "Hidden"
    assert rows["msg-hide"].category == TriageCategory.HIDE.value

    failed = rows["msg-boom"]
    assert failed.folder == "Errors"
    assert failed.category == email_log.CATEGORY_ERROR
    assert failed.error_type == "ValueError"
    assert "triage blew up" in failed.reason
    # Triage never returned, so the row's only source of identity is the raw email itself.
    assert failed.subject == "detonates in triage"


def test_routed_email_with_no_attachments_is_still_visible():
    """The exact shape of the original bug: nothing else in the schema records this email."""
    c = new_conn()
    email = route_email(email_id="msg-invisible")
    assert email.attachments == []

    ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=c)

    assert c.execute("SELECT COUNT(*) FROM attachment_ledger").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM extracted_records").fetchone()[0] == 0

    row = email_log.get(c, "msg-invisible")
    assert row is not None
    assert row.category == "route"
    assert row.reason.strip(), "a routed email without a stated reason is not actionable"
    assert row.matched_rule


def test_every_row_states_why():
    c = new_conn()
    mailbox = FakeMailbox([
        fx.inbound_email(email_id="msg-1", notice="939475", po_numbers=("908491",), shipment="90052 : 1"),
        route_email(),
        hide_email(),
    ])
    ingest_orchestrator.process_new_mail(mailbox, conn=c)
    for row in email_log.list_all(c):
        assert row.reason.strip(), f"{row.email_id} recorded no reason"
        assert row.matched_rule.strip(), f"{row.email_id} recorded no rule"


def test_reprocess_updates_the_row_rather_than_duplicating():
    c = new_conn()
    email = route_email(email_id="msg-again")

    ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=c)
    assert email_log.count(c) == 1
    first = email_log.get(c, "msg-again").processed_at

    state_db.forget_message(c, "msg-again")
    ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=c)

    assert email_log.count(c) == 1, "UNIQUE(email_id) makes this current-verdict, not history"
    assert email_log.get(c, "msg-again").processed_at >= first


def test_a_second_run_without_forgetting_processes_nothing():
    """Documents the seen_message_ids behaviour the runner's --reset flag exists for."""
    c = new_conn()
    email = route_email(email_id="msg-once")

    ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=c)
    ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=c)

    assert email_log.count(c) == 1
