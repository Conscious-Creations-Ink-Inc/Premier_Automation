from typing import List

import pytest

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

    def fetch_new(self, skip_ids=None, since=None) -> List[RawEmail]:
        self.skip_ids_seen = skip_ids
        # Deliberately returns everything regardless: `skip_ids` is a fetch-cost optimisation,
        # not the correctness boundary. The seen-id filter in `fetch_new_emails` is.
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
    email = fx.inbound_email(email_id="msg-warehouse-1", notice="939475",
                             po_numbers=("908491",), shipment="90052 : 1")
    c = new_conn()
    mailbox = FakeMailbox([email])
    count = ingest_orchestrator.process_new_mail(mailbox, conn=c)
    assert count == 1
    record = pending_records(c)[0]
    assert record.po_number == "908491"
    assert record.spec_code == "STE-402-LT-B"
    # The Inbound format states the Spitfire line number outright, which is what makes the
    # downstream match exact instead of fuzzy.
    assert record.po_line_number == 300
    assert record.quantity_received == 11.0 and record.unit_of_measure == "EA"
    # 11 CTN is the carton count from the Package column, never the receivable quantity.
    assert record.package_quantity == 11.0 and record.package_uom == "CTN"
    assert record.carrier_name == "Example Freight"
    assert record.tracking_number == "9940455"
    assert record.pod_stated_date == "2025-10-01"
    assert record.received_by == "Jordan T."
    assert mailbox.processed_calls == [("msg-warehouse-1", "Processed")]


def test_no_po_email_is_routed_and_nothing_staged():
    email = make_email(
        email_id="msg-example-retail-1",
        sender_address="marketing@example-retail.test", sender_domain="example-retail.test",
        subject="It's delivery day!", body_text="Your order is arriving today.",
    )
    c = new_conn()
    mailbox = FakeMailbox([email])
    count = ingest_orchestrator.process_new_mail(mailbox, conn=c)
    assert count == 0
    assert pending_records(c) == []
    assert mailbox.processed_calls == [("msg-example-retail-1", "Routed")]


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
    email = fx.inbound_email(email_id="msg-warehouse-2", po_numbers=("908491",))

    class FlakyMailbox(FakeMailbox):
        def mark_processed(self, email_id, folder):
            raise RuntimeError("simulated Graph API hiccup")

    c = new_conn()
    count = ingest_orchestrator.process_new_mail(FlakyMailbox([email]), conn=c)
    assert count == 1
    assert pending_records(c)[0].po_number == "908491"


def test_same_delivery_in_body_and_attachment_deduplicates_to_one():
    """The Inbound body and its packing-slip PDF describe the same line. Agreeing quantities
    collapse to the higher-confidence record rather than staging two receipts."""
    pdf_bytes = make_pdf_bytes([["PO", "Spec", "Qty"], ["908491", "STE-402-LT-B", "11"]])
    email = fx.inbound_email(
        email_id="msg-dup-1", po_numbers=("908491",),
        attachments=[Attachment(filename="Packing Slip - Inbound 939336.pdf",
                                content_type="application/pdf", content_bytes=pdf_bytes)],
    )
    c = new_conn()
    count = ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=c)
    assert count == 1
    assert pending_records(c)[0].po_number == "908491"


def test_quantity_conflict_across_sources_keeps_both_flagged():
    """Disagreeing quantities are never silently resolved — both records are staged and flagged,
    because the corpus shows quantities legitimately disagreeing (overage, split part
    shipments) and picking one would be a guess."""
    pdf_bytes = make_pdf_bytes([["PO", "Spec", "Qty"], ["908491", "STE-402-LT-B", "5"]])
    email = fx.inbound_email(
        email_id="msg-conflict-1", po_numbers=("908491",),
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
        email_id="msg-unknown-attachment-1", po_numbers=("908491",),
        lines=[{"po": "908491", "line": "1", "part": "", "item": "no quantity, no spec"}],
        attachments=[Attachment(filename="archive.zip", content_type="application/zip",
                                content_bytes=b"whatever")],
    )
    c = new_conn()
    count = ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=c)
    records = pending_records(c)
    assert all(r.extraction_source != "unknown" for r in records)
    assert not any("archive.zip" in (r.raw_snippet or "") for r in records)


# --- the seen-marker: written after processing, not while fetching -----------


def test_a_processed_email_is_marked_seen_and_not_processed_twice():
    email = fx.inbound_email(email_id="msg-seen-1", po_numbers=("908491",), shipment="90052 : 1")
    c = new_conn()
    ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=c)

    assert state_db.has_seen(c, "msg-seen-1")
    assert ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=c) == 0


def test_an_interrupted_run_leaves_the_unprocessed_mail_unseen(monkeypatch):
    """The failure this guards: the seen-marker used to be written while *filtering*, so a poll
    that died halfway through marked its whole batch processed. Against a live mailbox that is
    silently lost mail, recoverable only by knowing which ids to hand to `forget_message`.

    KeyboardInterrupt rather than a normal exception on purpose — the per-email `except Exception`
    is meant to catch and record ordinary failures. This is the case that escapes it: Ctrl-C, or
    the process being killed.
    """
    first = fx.inbound_email(email_id="msg-ok-1", po_numbers=("908491",), shipment="90052 : 1")
    second = fx.inbound_email(email_id="msg-interrupted-1", po_numbers=("908492",))
    third = fx.inbound_email(email_id="msg-never-reached-1", po_numbers=("908493",))

    real_triage = ingest_orchestrator.triage

    def triage_then_die(email, **kwargs):
        if email.email_id == "msg-interrupted-1":
            raise KeyboardInterrupt
        return real_triage(email, **kwargs)

    monkeypatch.setattr(ingest_orchestrator, "triage", triage_then_die)

    c = new_conn()
    with pytest.raises(KeyboardInterrupt):
        ingest_orchestrator.process_new_mail(FakeMailbox([first, second, third]), conn=c)

    assert state_db.has_seen(c, "msg-ok-1"), "work already finished must not be redone"
    assert not state_db.has_seen(c, "msg-interrupted-1")
    assert not state_db.has_seen(c, "msg-never-reached-1")


def test_an_email_that_fails_triage_is_still_marked_seen():
    """A failure is a verdict: it is filed to Errors and recorded in email_log, so retrying it
    every poll would just re-file it forever. Only an *interrupted* email comes back."""
    email = make_email(email_id="msg-boom-1", attachments=[
        Attachment(filename="x.pdf", content_type="application/pdf", content_bytes=b"%PDF-1.4 junk"),
    ])
    c = new_conn()
    mailbox = FakeMailbox([email])
    ingest_orchestrator.process_new_mail(mailbox, conn=c)
    assert state_db.has_seen(c, "msg-boom-1")


def test_the_same_id_twice_in_one_fetch_is_processed_once():
    """Nothing is marked seen until the end of the email, so the in-run guard is what stops a
    duplicated Inbox entry being ingested twice within a single poll."""
    email = fx.inbound_email(email_id="msg-dupe-1", po_numbers=("908491",), shipment="90052 : 1")
    c = new_conn()
    mailbox = FakeMailbox([email, email])
    ingest_orchestrator.process_new_mail(mailbox, conn=c)

    assert len(mailbox.processed_calls) == 1


def test_one_corrupt_attachment_does_not_block_the_others():
    good_pdf = make_pdf_bytes([["PO", "Spec", "Qty"], ["908491", "LOB-203-PI", "3"]])
    email = fx.inbound_email(
        email_id="msg-mixed-1", po_numbers=("908491",),
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


# --- The listing watermark ----------------------------------------------------
# A cost optimisation, never a correctness boundary: `seen_message_ids` is what makes
# re-processing impossible. What this buys is a narrower server-side listing, which is what stops
# MAX_PAGES_PER_POLL quietly capping a never-emptied Inbox at a thousand messages.

def test_a_clean_run_advances_the_watermark_to_the_newest_mail_it_settled():
    conn = new_conn()
    mailbox = FakeMailbox([
        make_email(email_id="msg-old", received_at="2026-06-08T14:00:00Z"),
        make_email(email_id="msg-new", received_at="2026-06-09T09:30:00Z"),
    ])

    ingest_orchestrator.process_new_mail(mailbox, conn=conn)

    assert state_db.get_watermark(conn) == "2026-06-09T09:30:00Z"


def test_the_watermark_never_moves_backwards():
    conn = new_conn()
    state_db.set_ingest_state(conn, state_db.WATERMARK_KEY, "2026-07-01T00:00:00Z")

    ingest_orchestrator.process_new_mail(
        FakeMailbox([make_email(email_id="msg-old", received_at="2026-06-08T14:00:00Z")]), conn=conn)

    assert state_db.get_watermark(conn) == "2026-07-01T00:00:00Z"


def test_a_run_that_dies_leaves_the_watermark_alone(monkeypatch):
    """Advancing on a half-finished run would narrow the next listing past mail that was never
    read. Re-listing the same window costs one request; skipping it loses mail for good."""
    conn = new_conn()
    mailbox = FakeMailbox([make_email(email_id="msg-1", received_at="2026-06-09T09:30:00Z")])

    def explode(*args, **kwargs):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(ingest_orchestrator.attachment_ledger, "close_open_rows", explode)

    with pytest.raises(RuntimeError):
        ingest_orchestrator.process_new_mail(mailbox, conn=conn)

    assert state_db.get_watermark(conn) is None


def test_the_first_run_of_a_fresh_store_asks_for_everything():
    from pipeline import stage1_ingest

    assert stage1_ingest.listing_window_start(new_conn()) is None


def test_the_window_is_backdated_from_the_watermark():
    """Clock skew between Exchange hops, or a message delayed in transit, lands mail *behind* a
    watermark that has already moved past it. The overlap is what catches that, and `skip_ids`
    makes the re-listed rows free."""
    from config import settings
    from pipeline import stage1_ingest

    conn = new_conn()
    state_db.set_ingest_state(conn, state_db.WATERMARK_KEY, "2026-06-09T09:30:00Z")

    assert stage1_ingest.listing_window_start(conn) < "2026-06-09T09:30:00Z"
    assert settings.INGEST_OVERLAP_MINUTES >= 5


def test_an_unparseable_watermark_reads_everything_rather_than_nothing():
    from pipeline import stage1_ingest

    conn = new_conn()
    state_db.set_ingest_state(conn, state_db.WATERMARK_KEY, "not a date")

    assert stage1_ingest.listing_window_start(conn) is None


# --- one delivery, many item lines --------------------------------------------------------------
#
# The defect these cover: an Authority notice naming six lines of one purchase order used to become
# six unrelated rows, each of which went on to create its own Spitfire receipt. PO 912559 collected
# eight receipts in one afternoon that way. The rows are still one per item — that is what an item
# line *is* — but they now all point at one `deliveries` row, which is the thing a receipt is
# created against.


def deliveries(c):
    c.row_factory = None
    return c.execute("SELECT id, po_number, delivery_ref, delivery_rung FROM deliveries "
                     "ORDER BY id").fetchall()


def test_every_line_of_one_notice_shares_one_delivery():
    """Six items on one purchase order are one delivery, not six."""
    email = fx.inbound_email(
        email_id="msg-multi-line", notice="939260", po_numbers=("906725",), shipment="90009",
        lines=[{"po": "906725", "line": str(n), "part": f"EXT-92{n}-AC",
                "item": f'2 EA - EXT-92{n}-AC Linear Planter'} for n in range(5, 9)])
    c = new_conn()
    count = ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=c)
    assert count == 4, "four item lines"

    rows = deliveries(c)
    assert len(rows) == 1, f"four lines must share one delivery, got {rows}"
    delivery_id, po_number, ref, rung = rows[0]
    assert po_number == "906725"
    assert ref == "shipment:90009", "stated shipment number identifies the delivery"
    assert rung == "shipment"

    c.row_factory = None
    attached = c.execute("SELECT COUNT(*) FROM extracted_records WHERE delivery_id = ?",
                         (delivery_id,)).fetchone()[0]
    assert attached == 4, "every staged line points at it"


def test_two_purchase_orders_on_one_notice_are_two_deliveries():
    """One notice, two POs — `only_po` releases an event each, so each PO gets its own delivery and
    its own receipt. Merging them would put one PO's quantities on the other's receipt."""
    email = fx.inbound_email(
        email_id="msg-two-pos", notice="939260", po_numbers=("906725", "907665"), shipment="90009",
        lines=[{"po": "906725", "line": "1", "part": "EXT-925-AC", "item": "2 EA - EXT-925-AC Planter"},
               {"po": "907665", "line": "1", "part": "POOL-925-AC", "item": "3 EA - POOL-925-AC Planter"}])
    c = new_conn()
    ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=c)

    rows = deliveries(c)
    assert {r[1] for r in rows} == {"906725", "907665"}
    assert len(rows) == 2, "one delivery per purchase order, not one per notice"


def test_a_delivery_is_not_created_for_an_event_that_stages_nothing():
    """An event whose every line is a duplicate must leave no delivery behind — a row asserting
    goods arrived with nothing under it saying what is worse than no row."""
    email = fx.inbound_email(email_id="msg-dup-1", notice="939475", po_numbers=("908491",),
                             shipment="90052 : 1")
    c = new_conn()
    ingest_orchestrator.process_new_mail(FakeMailbox([email]), conn=c)
    assert len(deliveries(c)) == 1

    # The same delivery again, re-extracted under a new message id.
    again = fx.inbound_email(email_id="msg-dup-2", notice="939475", po_numbers=("908491",),
                             shipment="90052 : 1")
    ingest_orchestrator.process_new_mail(FakeMailbox([again]), conn=c)
    assert len(deliveries(c)) == 1, "the re-send must not create a second delivery"


# --- One spec, many lines --------------------------------------------------------------------
#
# PO 907514 carries 29 Spitfire lines that all read `LOB-900-SI`, distinguished only by their
# description. Grouping on (PO, spec) alone put 23 delivered signage items in one group, saw 12
# different quantities, and flagged every one `+quantity_conflict`. Those 23 plus 21 more on
# PO 907249 were 35% of every record in the store.

def _record(po, spec, description, quantity, *, source="excel:Hoja1", confidence=0.5):
    from pipeline.models import ExtractedRecord
    return ExtractedRecord(
        source_email_id="msg-sig", po_number=po, shipment_number=None, spec_code=spec,
        parent_spec_code=spec, sub_spec_suffix=None, item_description=description,
        vendor_name=None, carrier_name=None, tracking_number=None,
        quantity_received=quantity, unit_of_measure="EA", pod_stated_date=None, email_date="",
        delivery_location=None, comments=None, extraction_source=source,
        extraction_confidence=confidence, raw_snippet="")


def _flagged(records):
    return [r for r in records if "quantity_conflict" in r.extraction_source]


def test_one_document_listing_many_items_under_one_spec_is_not_a_quantity_conflict():
    """The signage case. Different items, so different lines — not one line with 12 quantities."""
    records = [
        _record("907514", "LOB-900-SI", "P.S. Small Directional", 3.0),
        _record("907514", "LOB-900-SI", "Common Room ID", 15.0),
        _record("907514", "LOB-900-SI", "Restroom Door Placard", 8.0),
    ]
    out = ingest_orchestrator.reconcile_cross_source_duplicates(records)
    assert len(out) == 3, "three different signs are three receipts"
    assert _flagged(out) == [], "differing quantities for different items are not a conflict"


def test_a_subset_description_does_not_merge_two_different_lines():
    """`token_set_ratio` scores "Exit" against "Exit Route" at 100, but they are Spitfire lines 18
    and 24. Same for "Accesible Lift" against "Handicap Lift Accesible" (lines 22 and 30)."""
    records = [
        _record("907514", "LOB-900-SI", "Exit", 4.0),
        _record("907514", "LOB-900-SI", "Exit Route", 4.0),
        _record("907514", "LOB-900-SI", "Accesible Lift", 1.0),
        _record("907514", "LOB-900-SI", "Handicap Lift Accesible", 1.0),
    ]
    out = ingest_orchestrator.reconcile_cross_source_duplicates(records)
    assert len(out) == 4, "agreeing quantities must not hide that these are four distinct lines"


def test_descriptions_differing_only_in_digits_are_different_items():
    """"Medicine Ball 4 Kg" and "6 Kg" score 94 on one changed character, yet they are separate
    Spitfire lines each ordered 1. Merging agreed on quantity, so receipts were discarded."""
    records = [
        _record("907249", "FIT-900-FIT", "Medicine Ball 4 Kg", 1.0),
        _record("907249", "FIT-900-FIT", "Medicine Ball 6 Kg", 1.0),
        _record("907249", "FIT-900-FIT", "Medicine Ball 9 Kg", 1.0),
        _record("907249", "FIT-900-FIT", "Medicine Ball 11 Kg", 1.0),
    ]
    out = ingest_orchestrator.reconcile_cross_source_duplicates(records)
    assert len(out) == 4, "four weights are four line items"


def test_the_same_item_worded_differently_in_one_document_still_collapses():
    """The split must not be so eager that one item listed twice becomes two receipts."""
    records = [
        _record("907514", "LOB-900-SI", "Maximum Occupancy", 6.0),
        _record("907514", "LOB-900-SI", "Max Occupancy", 6.0, confidence=0.4),
    ]
    out = ingest_orchestrator.reconcile_cross_source_duplicates(records)
    assert len(out) == 1, "one sign named two ways is one receipt"


def test_differing_descriptions_across_sources_still_collapse_to_one_line():
    """The behaviour this grouping exists for, and the one most at risk from the split above.

    The Authority notice calls line STE-402-LT-B "BASE, Floor Lamp 2 (Linen Drum Shade) at
    Sectional"; the tracker spreadsheet beside it says "Throw Pillow". One physical line.
    """
    records = [
        _record("908491", "STE-402-LT-B", "BASE, Floor Lamp 2 (Linen Drum Shade) at Sectional",
                11.0, source="authority_inbound", confidence=0.9),
        _record("908491", "STE-402-LT-B", "Throw Pillow", 11.0, source="excel:Sheet", confidence=0.5),
    ]
    out = ingest_orchestrator.reconcile_cross_source_duplicates(records)
    assert len(out) == 1, "wording differs across sources for reasons that are not identity"
    assert out[0].extraction_source == "authority_inbound", "the richer source wins"


def test_a_real_quantity_conflict_on_one_line_is_still_flagged():
    """Two sources, same line, disagreeing quantities — still nobody's guess to make."""
    records = [
        _record("908491", "STE-402-LT-B", "Dining Chair Oak", 11.0, source="authority_inbound"),
        _record("908491", "STE-402-LT-B", "Dining Chair Oak", 9.0, source="authority_delivered"),
    ]
    out = ingest_orchestrator.reconcile_cross_source_duplicates(records)
    assert len(out) == 2
    assert len(_flagged(out)) == 2, "a genuine disagreement must still reach a person"


def test_reconciling_the_same_records_twice_appends_one_marker():
    """`EvidenceCache.records_for` hands back the *same* record objects every time it is asked, so
    an email whose mail fires several delivery events is reconciled several times over one object.
    An unguarded concatenation wrote
    `ocr+quantity_conflict+quantity_conflict+quantity_conflict+quantity_conflict` into the live
    store. Every reader uses `in`, so it never changed a verdict — it is a record of how many times
    a thing happened that only happened once, and it made the source unreadable at a glance.
    """
    records = [
        _record("908491", "STE-402-LT-B", "Dining Chair Oak", 11.0, source="ocr"),
        _record("908491", "STE-402-LT-B", "Dining Chair Oak", 9.0, source="ocr"),
    ]
    for _ in range(3):
        out = ingest_orchestrator.reconcile_cross_source_duplicates(records)

    assert len(_flagged(out)) == 2, "it is still a conflict"
    for r in out:
        assert r.extraction_source.count("quantity_conflict") == 1, r.extraction_source


def test_a_missing_description_never_splits_a_group():
    """A Delivered notice frequently names no item, and must still find its Inbound."""
    records = [
        _record("908491", "STE-402-LT-B", "Dining Chair Oak", 11.0, source="authority_inbound",
                confidence=0.9),
        _record("908491", "STE-402-LT-B", None, 11.0, source="authority_delivered", confidence=0.6),
    ]
    out = ingest_orchestrator.reconcile_cross_source_duplicates(records)
    assert len(out) == 1


# --- A POD is evidence for a line, not a line of its own ---------------------

def _rec(po, *, spec=None, qty=None, desc=None, source="authority_inbound", conf=1.0,
         pod_date=None, received_by=None, carrier=None, tracking=None):
    from pipeline.models import ExtractedRecord
    return ExtractedRecord(
        source_email_id="m1", po_number=po, shipment_number=None, spec_code=spec,
        parent_spec_code=None, sub_spec_suffix=None, item_description=desc, vendor_name=None,
        carrier_name=carrier, tracking_number=tracking, quantity_received=qty,
        unit_of_measure=None, pod_stated_date=pod_date, email_date="2026-08-17T00:00:00Z",
        delivery_location=None, comments=None, extraction_source=source,
        extraction_confidence=conf, raw_snippet="", received_by=received_by)


def test_a_pod_does_not_stage_a_line_less_twin_beside_the_line_it_proves():
    """PO 908705 staged two records from one email: the Authority line (spec POOL-152-WT, 2 EA —
    complete, and since posted to Spitfire) and a POD-only twin with spec, description and
    quantity all null.

    `_group_records` keys the first on its spec and the second on an empty description, so they
    never met. The twin could never be completed from the POD it came from, and its gap list
    `missing: spec code, description, quantity` was rendered against the *message*, so a message
    that had parsed perfectly read as unparsed on the queue.
    """
    from pipeline.ingest_orchestrator import reconcile_cross_source_duplicates

    line = _rec("908705", spec="POOL-152-WT", qty=2.0, desc="Drapery Fabrication")
    pod = _rec("908705", source="pdf:carrier_pod", conf=0.7, pod_date="2026-08-17",
               received_by="R ALVAREZ", carrier="Cascade Freight Lines", tracking="7401882330")

    kept = reconcile_cross_source_duplicates([line, pod])

    assert kept == [line], "the POD must not stage a record of its own beside the line"
    # Its evidence is not lost — it moves onto the line, filling only what was blank.
    assert line.pod_stated_date == "2026-08-17"
    assert line.carrier_name == "Cascade Freight Lines"
    assert line.tracking_number == "7401882330"


def test_the_line_keeps_its_own_evidence_when_it_already_has_some():
    """The notification's own `Received By` is the better answer — it is the warehouse's record of
    who signed, not an OCR reading of a scrawl."""
    from pipeline.ingest_orchestrator import reconcile_cross_source_duplicates

    line = _rec("908705", spec="POOL-152-WT", qty=2.0, desc="Drapery", received_by="R. Alvarez")
    pod = _rec("908705", source="pdf:carrier_pod", conf=0.7, received_by="R ALVAREZ")

    reconcile_cross_source_duplicates([line, pod])
    assert line.received_by == "R. Alvarez"


def test_a_pod_with_no_line_to_attach_to_still_stages():
    """A POD-only email is the only evidence there is. Dropping it would lose the delivery."""
    from pipeline.ingest_orchestrator import reconcile_cross_source_duplicates

    pod = _rec("911846", source="pdf:carrier_pod", conf=0.7, pod_date="2026-08-17",
               received_by="R ALVAREZ")
    assert reconcile_cross_source_duplicates([pod]) == [pod]


# --- attributing a line that names no purchase order -------------------------

def _event_for(po):
    """The smallest DeliveryEvent `_record_for_event` reads: it touches only `key.po_number`."""
    from pipeline.models import AccumulationKey, DeliveryEvent, NotificationType
    return DeliveryEvent(
        key=AccumulationKey(po_number=po, shipment_number=None,
                            delivery_ref="message:<m1>", delivery_rung="message"),
        trigger_notification_type=NotificationType.DELIVERED_SHIPPED,
        emails=[], released_at="2026-09-10T00:00:00Z", release_reason="test")


def test_a_line_naming_no_po_is_adopted_when_the_message_has_only_one():
    """The case the adoption rule was written for, and it still holds. A carrier POD names a
    purchase order; the free-text pass over the same page may name none, and that line is
    evidence for the one delivery on the message."""
    event = _event_for("906725")
    mine = ingest_orchestrator._record_for_event(_rec(""), event, {"906725"})

    assert mine is not None
    assert mine.po_number == "906725"


def test_a_line_naming_no_po_is_refused_when_the_message_has_two():
    """With two purchase orders in play, adopting is a guess wearing the costume of a fact.

    Measured: a receiving report covering two orders had every unattributed line claimed by
    whichever event ran first, so seven lines were recorded against goods they had nothing to do
    with. `tools/reextract.py` and `tools/replay_parse.py` have always refused here; so does this.
    """
    event = _event_for("906725")
    assert ingest_orchestrator._record_for_event(_rec(""), event, {"906725", "907665"}) is None


def test_adopting_a_line_does_not_rewrite_the_shared_record():
    """The evidence cache hands the same instances to every event, so stamping one in place
    changed what the next event saw — and made attribution depend on iteration order."""
    record = _rec("")
    mine = ingest_orchestrator._record_for_event(record, _event_for("906725"), {"906725"})

    assert mine is not record, "the event gets a copy"
    assert record.po_number == "", "and the cached original is left as it was found"


def test_a_line_naming_another_po_is_never_taken():
    assert ingest_orchestrator._record_for_event(
        _rec("907665"), _event_for("906725"), {"906725", "907665"}) is None
