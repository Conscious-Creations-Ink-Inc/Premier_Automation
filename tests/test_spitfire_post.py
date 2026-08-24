"""The post chain, driven end to end without a network.

Every assertion here is about a failure that has actually happened, or that the API makes easy to
walk into. The write client is faked rather than mocked at the HTTP layer, because what needs
proving is the *orchestration* — the order of the nine calls, what is written to the ledger
between them, and what happens when one of them fails halfway. The HTTP shapes themselves were
proven against the live server and are pinned by `test_spitfire_write_allowlist.py`.
"""

import io
import sqlite3

import pytest

from pipeline import post_ledger, spitfire_post, state_db


class FakeWriteClient:
    """Records the calls and returns what the real server returned on 2026-08-14.

    `fail_on` makes any named step raise, which is how the partial-post paths are exercised —
    those are the ones that leave a real document on Premier's training instance, so they matter
    more than the happy path.
    """

    def __init__(self, fail_on: str = "", sub_contract: str = "212614"):
        self.fail_on = fail_on
        self.sub_contract = sub_contract
        self.calls: list = []
        self.attachments: list = []
        self.uploads: dict = {}
        self._next_file = 0

    def _maybe_fail(self, step: str) -> None:
        self.calls.append(step)
        if self.fail_on == step:
            raise RuntimeError(f"boom in {step}")

    def whoami(self):
        self._maybe_fail("whoami")
        return "api@consciouscreations.ai"

    def create_receipt(self, project_id, po_number, receipt_type_key=None):
        self._maybe_fail("create_receipt")
        return "11111111-2222-3333-4444-555555555555"

    def set_title(self, doc_key, title):
        self._maybe_fail("set_title")
        assert title.startswith("CC-TEST"), "every training artefact must be marked"

    def read_header(self, doc_key):
        self._maybe_fail("read_header")
        return {"DocNo": "0007", "SubContract": self.sub_contract}

    def add_line(self, doc_key, **kwargs):
        self._maybe_fail("add_line")
        self.line = kwargs

    def upload_file(self, content, filename, keywords="", when=None):
        self._maybe_fail("upload_file")
        self._next_file += 1
        key = f"file-key-{self._next_file}"
        self.uploads[key] = (content, filename)
        return key

    def verify_upload(self, file_key, expected_md5):
        self._maybe_fail("verify_upload")
        return True

    def attach_file(self, doc_key, file_key, note="", cat_type=None):
        self._maybe_fail("attach_file")
        self.attachments.append({"DocKey": file_key})

    def link_document(self, doc_key, target_doc_key, note="", cat_type=None):
        self._maybe_fail("link_document")
        self.attachments.append({"DocKey": "", "AttachedDocMaster": target_doc_key})

    def find_documents(self, project_id, doc_type_key, doc_no_like="", limit=25):
        self._maybe_fail("find_documents")
        return [{"DocMasterKey": "pay-req-1", "DocNo": "0002", "SubContract": "212614"}]

    def read_attachments(self, doc_key):
        self._maybe_fail("read_attachments")
        return list(self.attachments)

    def audit_rows(self):
        return [{"method": "POST", "path": s, "status": 200} for s in self.calls]


def pod_pdf(po_number: str) -> bytes:
    """A real PDF with a real text layer that `parsing/pod.py` reads as a proof of delivery.

    Not a `b"%PDF-1.4"` stub any more. `spitfire_post._pod_for` now chooses the POD by parsing
    each candidate rather than by taking the first attachment of an acceptable kind, so a stub
    that no longer parses is correctly ignored — and these tests would then be exercising the
    "no POD" path while claiming to exercise the chain.
    """
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer)
    y = 800
    for line in ("The following is the proof-of-delivery for tracking number: 7497809572",
                 "Delivery Information:",
                 "Status: Delivered Delivery date: Jan 20, 2026 10:18",
                 "Signed for by: J SMITH",
                 f"Purchase Order {po_number} : 1"):
        c.drawString(40, y, line)
        y -= 16
    c.save()
    return buffer.getvalue()


@pytest.fixture
def conn():
    c = state_db.get_connection(":memory:")
    c.execute(
        """INSERT INTO extracted_records
           (id, source_email_id, po_number, spec_code, item_description, vendor_name,
            quantity_received, unit_of_measure, pod_stated_date, received_by, po_line_number,
            email_date, extraction_source, extraction_confidence, created_at)
           VALUES (1, 'mail-1', '212614', 'LT-03b', 'LT-03B Frosted Replacement',
                   'Archipelago Lighting', 19.0, 'EA', '2026-01-20', 'J Smith', 1,
                   '2026-01-20', 'test', 1.0, '2026-01-20')""")
    c.execute(
        """INSERT INTO spitfire_po_index (po_number, doc_master_key, project_code, refreshed_at)
           VALUES ('212614', '4b186a21-59be-4c0a-8221-6f20363e6191', 'MRC024PB100003', 'now')""")
    c.execute(
        """INSERT INTO spitfire_po_lines
           (line_key, po_number, line_number, spec_code, description, unit_of_measure,
            qty_ordered, qty_received, qty_in_transit, cost_code, refreshed_at)
           VALUES ('k1','212614',1,'LT-03b','LT-03B Frosted Replacement','EA',
                   19.0, 0.0, 0.0, 'MAT-FDP', 'now')""")
    # The POD, with its bytes — the cache path attachment_bytes.resolve tries first.
    c.execute(
        """INSERT INTO mail_attachment
           (email_id, ordinal, filename, content_type, kind, size_bytes, is_inline, content)
           VALUES ('mail-1', 0, 'POD_212614.pdf', 'application/pdf', 'pdf', 9, 0, ?)""",
        (pod_pdf("212614"),))
    c.execute(
        """INSERT INTO attachment_ledger
           (email_id, depth, ordinal, filename, sniffed_kind, disposition, is_inline,
            first_seen_at)
           VALUES ('mail-1', 0, 0, 'POD_212614.pdf', 'pdf', 'extracted', 0, 'now')""")
    c.commit()
    return c


@pytest.fixture
def record(conn):
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM extracted_records WHERE id = 1").fetchone()


@pytest.fixture(autouse=True)
def no_chrome(monkeypatch):
    """The report is a PDF from headless Chrome, which is not a dependency these tests should
    have — the rendering itself is exercised in `test_report_pdf.py`."""
    monkeypatch.setattr(spitfire_post.report_pdf, "render", lambda html, **kw: b"%PDF-fake")


class OfflineReadClient:
    """A read client that answers nothing, so `po_verify` falls back to the mirror.

    Returning `None` rather than raising is the important part: an exception sets
    `RecordVerification.error` and the decision flags "Spitfire could not be read", which would
    make every test here assert the same failure. A `None` document is the ordinary
    "not reachable live" path, and `po_verify` then builds the comparison from
    `spitfire_po_lines` — which the fixture populates.
    """

    def resolve_po(self, po_number):
        return None

    def read_po(self, doc_key):
        return None


@pytest.fixture
def offline():
    return OfflineReadClient


def test_a_clean_record_posts_and_records_every_key(conn, record, offline):
    client = FakeWriteClient()
    result = spitfire_post.post_record(conn, record, read_client_factory=offline, client=client)

    assert result.ok, result.message
    assert result.receipt_doc_no == "0007"
    assert "posted to Spitfire as receipt 0007" in result.message

    # The order of the chain is the point: the receipt exists before anything is hung on it, and
    # the read-back is last because it is the only step that proves the rest.
    assert client.calls.index("create_receipt") < client.calls.index("upload_file")
    assert client.calls.index("attach_file") < client.calls.index("read_attachments")

    # Two files: the POD and the report. Spitfire does not distinguish them.
    assert len(client.uploads) == 2
    attempt = post_ledger.existing_for_record(conn, 1)[0]
    assert attempt.state == post_ledger.POSTED
    assert attempt.pod_file_key and attempt.report_file_key
    assert attempt.receipt_doc_no == "0007"


def test_the_record_reaches_the_last_rung_of_the_lifecycle(conn, record, offline):
    spitfire_post.post_record(conn, record, read_client_factory=offline, client=FakeWriteClient())
    status = conn.execute("SELECT status FROM extracted_records WHERE id = 1").fetchone()[0]
    assert status == "pushed_to_spitfire"


def test_posting_the_same_delivery_twice_is_refused(conn, record, offline):
    first = spitfire_post.post_record(conn, record, read_client_factory=offline, client=FakeWriteClient())
    assert first.ok

    second_client = FakeWriteClient()
    second = spitfire_post.post_record(conn, record, read_client_factory=offline, client=second_client)

    assert not second.ok
    assert "already posted" in second.message
    # The guard has to bite before the network, not after: Spitfire would happily create a second
    # receipt and a second copy of both files.
    assert second_client.calls == []


def test_a_failure_after_the_receipt_exists_is_partial_not_failed(conn, record, offline):
    """The distinction is the whole point of the state.

    FAILED means nothing was created and the delivery can be posted again. PARTIAL means a real
    document is sitting on Premier's instance, incomplete — retrying would add a second one
    beside it, so it needs a person.
    """
    result = spitfire_post.post_record(conn, record, read_client_factory=offline, client=FakeWriteClient(fail_on="attach_file"))

    assert not result.ok
    assert result.state == post_ledger.PARTIAL
    assert result.receipt_key, "a partial post must say which document to go and look at"

    attempt = post_ledger.existing_for_record(conn, 1)[0]
    assert attempt.state == post_ledger.PARTIAL
    assert attempt.receipt_key == result.receipt_key
    assert attempt.pod_file_key, "the uploaded file is catalog litter and must be recorded"


def test_a_failure_before_anything_is_created_is_failed_and_retryable(conn, record, offline):
    result = spitfire_post.post_record(conn, record, read_client_factory=offline, client=FakeWriteClient(fail_on="whoami"))
    assert result.state == post_ledger.FAILED
    assert not result.receipt_key


def test_an_unlinked_receipt_stops_the_chain(conn, record, offline):
    """`forBatch` is the only thing tying a receipt to its PO. If SubContract comes back wrong the
    document is an orphan, and hanging a POD on it would assert a delivery against nothing."""
    client = FakeWriteClient(sub_contract="")
    result = spitfire_post.post_record(conn, record, read_client_factory=offline, client=client)

    assert not result.ok
    assert "not linked to the purchase order" in result.message
    assert "upload_file" not in client.calls, "nothing should be uploaded onto an orphan receipt"


def test_a_record_with_no_stored_pod_bytes_is_flagged_not_posted(conn, record, offline):
    """17 of 35 live attachments carry no bytes. A receipt asserting delivery with no proof
    attached is worse than no receipt."""
    # The ledger row stays. This is the "we know the file existed and we no longer have it" case,
    # which is ours to investigate — deleting the row too would be "the email carried nothing",
    # a different situation with a different fix, and `_pod_absence_reason` now tells them apart.
    conn.execute("UPDATE mail_attachment SET content = NULL WHERE email_id = 'mail-1'")
    conn.execute("""UPDATE attachment_ledger SET is_pod = 1, pod_po_numbers = '212614'
                     WHERE email_id = 'mail-1'""")
    conn.commit()

    client = FakeWriteClient()
    result = spitfire_post.post_record(conn, record, read_client_factory=offline, client=client)

    assert not result.ok
    assert "no stored bytes" in result.message
    assert client.calls == []


def test_an_email_that_carried_no_attachments_says_exactly_that(conn, record, offline):
    """A different situation from the one above, and the message used to conflate them — sending
    people to look in the attachment store for a file Premier never sent."""
    conn.execute("DELETE FROM mail_attachment")
    conn.execute("DELETE FROM attachment_ledger")
    conn.commit()

    result = spitfire_post.post_record(conn, record, read_client_factory=offline,
                                       client=FakeWriteClient())

    assert not result.ok
    assert "carried no attachments" in result.message


def test_a_quantity_disagreement_flags_with_both_numbers(conn, record, offline):
    conn.execute("UPDATE spitfire_po_lines SET qty_ordered = 12.0 WHERE line_key = 'k1'")
    conn.commit()
    result = spitfire_post.post_record(conn, record, read_client_factory=offline, client=FakeWriteClient())

    assert not result.ok
    assert "19" in result.message and "12" in result.message, \
        "a person needs the two numbers, not the word 'mismatch'"


def test_link_failures_do_not_discard_a_good_receipt(conn, record, offline):
    """By the time the links are made the receipt already carries the POD and the report, which is
    the evidence Premier needs. A missing pay-request link is worth reporting, not worth throwing
    that away."""
    client = FakeWriteClient(fail_on="find_documents")
    result = spitfire_post.post_record(conn, record, read_client_factory=offline, client=client)

    assert result.ok
    assert any("could not search for pay requests" in s for s in result.steps)


def test_any_file_kind_can_be_the_pod_once_something_has_read_it(conn, record, offline):
    """A POD is chosen by what the file says, not by its extension. An image or a legacy `.doc`
    is proof of delivery exactly when a reader recorded it as one — proven live on 2026-08-17,
    when a `.png` and a `.doc` both reached receipts 0002 and 0003 on PO 212559."""
    conn.execute("""UPDATE attachment_ledger SET sniffed_kind='doc', filename='signed.doc',
                        is_pod=1, pod_po_numbers='212614' WHERE email_id='mail-1'""")
    conn.execute("UPDATE mail_attachment SET kind='doc', filename='signed.doc'"
                 " WHERE email_id='mail-1'")
    conn.commit()

    result = spitfire_post.post_record(conn, record, read_client_factory=offline,
                                       client=FakeWriteClient())
    assert result.ok, result.message


def test_a_pod_naming_another_purchase_order_is_refused(conn, record, offline):
    """The interesting failure: the paperwork and the record disagree. Rejected rather than used
    as a fallback, because on this corpus two byte-identical PDFs arrive under filenames citing
    different specs — the filename cannot be trusted and neither can proximity."""
    conn.execute("""UPDATE attachment_ledger SET sniffed_kind='image', is_pod=1,
                        pod_po_numbers='999999' WHERE email_id='mail-1'""")
    conn.commit()

    client = FakeWriteClient()
    result = spitfire_post.post_record(conn, record, read_client_factory=offline, client=client)

    assert not result.ok
    assert "names purchase order 999999" in result.message
    assert client.calls == [], "nothing should be created for the wrong purchase order"


def test_an_email_whose_attachments_were_never_read_says_so(conn, record, offline):
    """Distinct from "the bytes are missing", which is what this used to say for every case. One
    is a gap in the readers; the other is a storage problem. They need different fixes."""
    conn.execute("""UPDATE attachment_ledger SET sniffed_kind='xlsx', filename='tracker.xlsx',
                        is_pod=0, pod_po_numbers='' WHERE email_id='mail-1'""")
    conn.commit()

    result = spitfire_post.post_record(conn, record, read_client_factory=offline,
                                       client=FakeWriteClient())

    assert not result.ok
    assert "was read as a proof of delivery" in result.message
    assert "tracker.xlsx" in result.message, "name the file so the gap can be closed"


def test_a_file_already_marked_is_not_marked_twice(conn, record, offline):
    """The first live run put `CC-TEST.CC-TEST.POD.FedEx.pdf` in Premier's attachments list."""
    assert spitfire_post._safe_name("CC-TEST.POD.pdf", "x") == "CC-TEST.POD.pdf"
    assert spitfire_post._safe_name("POD.pdf", "x") == "CC-TEST.POD.pdf"


def test_a_refusal_leaves_a_row_and_the_record_still_posts_once_fixed(conn, record, offline):
    """The full recovery loop, which is what the flagged state exists for.

    A record refused for a missing delivery date must record why, stay non-blocking, and post the
    moment the date is filled in — with nothing to clear by hand.

    The field was `received_by` until 2026-08-21; it is `DERIVED` now and no longer blocks, so the
    loop is exercised on the POD date instead — which is the field this gate still exists for.
    """
    conn.execute("UPDATE extracted_records SET pod_stated_date = NULL WHERE id = 1")
    conn.commit()
    conn.row_factory = sqlite3.Row
    broken = conn.execute("SELECT * FROM extracted_records WHERE id = 1").fetchone()

    refused = spitfire_post.post_record(conn, broken, read_client_factory=offline,
                                        client=FakeWriteClient())
    assert not refused.ok and "POD date" in refused.message
    flagged = post_ledger.existing_for_record(conn, 1)[0]
    assert flagged.state == post_ledger.FLAGGED
    assert "POD date" in flagged.detail

    conn.execute("UPDATE extracted_records SET pod_stated_date = '2025-09-10' WHERE id = 1")
    conn.commit()
    fixed = conn.execute("SELECT * FROM extracted_records WHERE id = 1").fetchone()

    posted = spitfire_post.post_record(conn, fixed, read_client_factory=offline,
                                       client=FakeWriteClient())
    assert posted.ok, posted.message
    assert post_ledger.existing_for_record(conn, 1)[0].state == post_ledger.POSTED
    assert post_ledger.blocked(conn) == []


def test_a_partial_delivery_books_what_arrived(conn, record, offline):
    """2 of 19 posts, and the receipt line carries 2 — not 19."""
    conn.execute("UPDATE spitfire_po_lines SET qty_ordered = 19.0 WHERE line_key = 'k1'")
    conn.execute("UPDATE extracted_records SET quantity_received = 2.0 WHERE id = 1")
    conn.commit()
    conn.row_factory = sqlite3.Row
    partial = conn.execute("SELECT * FROM extracted_records WHERE id = 1").fetchone()

    client = FakeWriteClient()
    result = spitfire_post.post_record(conn, partial, read_client_factory=offline, client=client)

    assert result.ok, result.message
    assert client.line["quantity"] == 2.0


def test_nothing_in_the_chain_ever_routes(conn, record, offline):
    """Creating a receipt stages three real Premier employees. Dispatching is a separate call, and
    it must never appear here."""
    client = FakeWriteClient()
    spitfire_post.post_record(conn, record, read_client_factory=offline, client=client)
    assert not any("route" in call or "Status" in call for call in client.calls)


# --- which file is the proof ------------------------------------------------------------------
#
# `_pod_for` used to take the first non-inline attachment of an acceptable kind, in ledger order,
# and the acceptable kinds included xlsx, msg, html and text. Measured against Premier's live store
# on 2026-08-17, that would have uploaded a tracker spreadsheet to the ERP as the proof of delivery
# for 15 of the 18 records that had any candidate at all. These tests hold the fix.


def _attach(conn, ordinal, filename, kind, content, *, is_inline=0, disposition="extracted"):
    conn.execute(
        """INSERT INTO mail_attachment
           (email_id, ordinal, filename, content_type, kind, size_bytes, is_inline, content)
           VALUES ('mail-1', ?, ?, 'application/octet-stream', ?, ?, ?, ?)""",
        (ordinal, filename, kind, len(content), is_inline, content))
    conn.execute(
        """INSERT INTO attachment_ledger
           (email_id, depth, ordinal, filename, sniffed_kind, disposition, is_inline, first_seen_at)
           VALUES ('mail-1', 0, ?, ?, ?, ?, ?, 'now')""",
        (ordinal, filename, kind, disposition, is_inline))
    conn.commit()


def test_a_spreadsheet_is_never_chosen_as_the_proof_of_delivery(conn, record):
    """The failure this rule exists for. A tracker sitting at a lower ordinal than the POD used to
    win on position alone and be uploaded to Premier's ERP as evidence of delivery."""
    conn.execute("DELETE FROM mail_attachment")
    conn.execute("DELETE FROM attachment_ledger")
    _attach(conn, 0, "Pending Receipt Confirmation Orders.xlsx", "xlsx", b"PK\x03\x04tracker")

    assert spitfire_post._pod_for(conn, record) is None, "a spreadsheet was offered as proof"


def test_the_pod_wins_over_an_earlier_spreadsheet(conn, record):
    conn.execute("DELETE FROM mail_attachment")
    conn.execute("DELETE FROM attachment_ledger")
    _attach(conn, 0, "tracker.xlsx", "xlsx", b"PK\x03\x04tracker")
    _attach(conn, 6, "POD_212614.pdf", "pdf", pod_pdf("212614"), disposition="dropped_duplicate")

    chosen = spitfire_post._pod_for(conn, record)

    assert chosen is not None and chosen.filename == "POD_212614.pdf", \
        "position beat content again"


def test_a_pod_for_a_different_purchase_order_is_refused(conn, record):
    """Two byte-identical PODs arrive on this corpus under filenames citing different specs and
    tracking numbers, so neither the filename nor the fact that it is *a* POD is enough."""
    conn.execute("DELETE FROM mail_attachment")
    conn.execute("DELETE FROM attachment_ledger")
    _attach(conn, 0, "POD_999999.pdf", "pdf", pod_pdf("999999"))

    assert spitfire_post._pod_for(conn, record) is None


def test_a_pdf_that_is_not_a_pod_is_not_the_proof(conn, record):
    conn.execute("DELETE FROM mail_attachment")
    conn.execute("DELETE FROM attachment_ledger")
    _attach(conn, 0, "invoice.pdf", "pdf", b"%PDF-1.4\nnot a delivery note at all\n")

    assert spitfire_post._pod_for(conn, record) is None


def test_an_unreadable_pdf_does_not_stop_the_search(conn, record):
    """A malformed attachment must not throw away a good POD sitting behind it."""
    conn.execute("DELETE FROM mail_attachment")
    conn.execute("DELETE FROM attachment_ledger")
    _attach(conn, 0, "corrupt.pdf", "pdf", b"%PDF-1.4\n\x00\x01\x02 truncated")
    _attach(conn, 1, "POD_212614.pdf", "pdf", pod_pdf("212614"))

    chosen = spitfire_post._pod_for(conn, record)

    assert chosen is not None and chosen.filename == "POD_212614.pdf"


def test_an_inline_image_is_never_the_proof(conn, record):
    """Signature logos and letterhead are inline; they are not evidence of anything."""
    conn.execute("DELETE FROM mail_attachment")
    conn.execute("DELETE FROM attachment_ledger")
    _attach(conn, 0, "signature.png", "image", b"\x89PNG\r\n", is_inline=1)

    assert spitfire_post._pod_for(conn, record) is None


def test_an_image_pod_is_honoured_from_the_stored_verdict(conn, record):
    """A POD is not a file type. This one is a photograph — no text layer, nothing a PDF parser
    could read — and it is the proof because OCR recognised it at ingest and the ledger kept the
    verdict. Re-deciding here would mean paying for OCR on every page render."""
    conn.execute("DELETE FROM mail_attachment")
    conn.execute("DELETE FROM attachment_ledger")
    _attach(conn, 0, "tracker.xlsx", "xlsx", b"PK\x03\x04tracker")
    _attach(conn, 1, "IMG_2479.jpeg", "image", b"\xff\xd8\xff\xe0 photographed delivery note")
    conn.execute("""UPDATE attachment_ledger
                       SET is_pod = 1, pod_po_numbers = '212614',
                           pod_delivery_date = '2026-01-20', pod_signed_by = 'J SMITH'
                     WHERE filename = 'IMG_2479.jpeg'""")
    conn.commit()

    chosen = spitfire_post._pod_for(conn, record)

    assert chosen is not None and chosen.filename == "IMG_2479.jpeg", \
        "an image POD must be usable; a POD is decided by content, not by extension"


def test_a_stored_verdict_for_another_po_does_not_win(conn, record):
    conn.execute("DELETE FROM mail_attachment")
    conn.execute("DELETE FROM attachment_ledger")
    _attach(conn, 0, "someone_elses_pod.jpeg", "image", b"\xff\xd8\xff\xe0 other delivery")
    conn.execute("""UPDATE attachment_ledger SET is_pod = 1, pod_po_numbers = '999999'
                     WHERE filename = 'someone_elses_pod.jpeg'""")
    conn.commit()

    assert spitfire_post._pod_for(conn, record) is None


def test_a_docx_pod_is_honoured_too(conn, record):
    """The scan-inside-a-Word-document case. Same rule, no type allowlist anywhere."""
    conn.execute("DELETE FROM mail_attachment")
    conn.execute("DELETE FROM attachment_ledger")
    _attach(conn, 0, "Delivery Receipt.docx", "docx", b"PK\x03\x04 word doc with a scan")
    conn.execute("""UPDATE attachment_ledger SET is_pod = 1, pod_po_numbers = '212614'
                     WHERE filename = 'Delivery Receipt.docx'""")
    conn.commit()

    chosen = spitfire_post._pod_for(conn, record)
    assert chosen is not None and chosen.filename == "Delivery Receipt.docx"


# --- the two stages ------------------------------------------------------------------------
#
# Posting is two decisions a person makes separately: the proof of delivery, then the receiver
# report. What has to survive the split is the ordering guarantee — a report describes a delivery
# whose proof is already filed, so it can neither precede that proof nor stand in for it.


def test_posting_the_pod_rests_at_pod_posted_with_no_report(conn, record, offline):
    client = FakeWriteClient()
    result = spitfire_post.post_pod(conn, record, client=client, read_client_factory=offline)

    assert result.ok and result.state == post_ledger.POD_POSTED
    assert result.pod_file_key and not result.report_file_key
    attempt = post_ledger.existing_for_record(conn, 1)[0]
    assert attempt.state == post_ledger.POD_POSTED
    assert attempt.pod_file_key and not attempt.report_file_key
    assert attempt.receipt_key, "the receipt is real and the report step needs its key"


def test_the_pod_stage_never_builds_a_report(conn, record, offline):
    """The whole point of stopping: nothing about the report may be done on Premier's behalf until
    somebody asks for it."""
    client = FakeWriteClient()
    spitfire_post.post_pod(conn, record, client=client, read_client_factory=offline)
    assert len(client.uploads) == 1, "only the POD was uploaded"
    assert "Receiver_Report" not in "".join(name for _, name in client.uploads.values())


def test_a_second_pod_post_does_not_create_a_second_receipt(conn, record, offline):
    """`POD_POSTED` blocks for the same reason `PARTIAL` does — the receipt exists."""
    spitfire_post.post_pod(conn, record, client=FakeWriteClient(), read_client_factory=offline)
    second = FakeWriteClient()
    result = spitfire_post.post_pod(conn, record, client=second, read_client_factory=offline)

    assert not result.ok
    assert "create_receipt" not in second.calls
    assert len([a for a in post_ledger.existing_for_record(conn, 1)
                if a.state == post_ledger.POD_POSTED]) == 1


def test_the_report_cannot_be_posted_before_the_pod(conn, record):
    """The ordering guarantee, asserted rather than assumed. Nothing reaches Spitfire at all."""
    client = FakeWriteClient()
    result = spitfire_post.post_report(conn, record, client=client)

    assert not result.ok and result.state == "flagged"
    assert "post the POD first" in result.message
    assert client.calls == [], "not one call was issued"


def test_the_report_stage_settles_posted_and_keeps_the_pod_key(conn, record, offline):
    client = FakeWriteClient()
    spitfire_post.post_pod(conn, record, client=client, read_client_factory=offline)
    result = spitfire_post.post_report(conn, record, client=client)

    assert result.ok and result.state == post_ledger.POSTED
    assert result.pod_file_key and result.report_file_key
    attempt = post_ledger.find(conn, post_ledger.existing_for_record(conn, 1)[0].idempotency_key)
    assert attempt.state == post_ledger.POSTED
    assert attempt.pod_file_key and attempt.report_file_key


def test_awaiting_report_lists_the_half_finished_receipt(conn, record, offline):
    """A receipt in Premier's ERP carrying a POD and no report is a state the atomic post could not
    produce. It must be listed, not left to be noticed."""
    assert post_ledger.awaiting_report(conn) == []
    spitfire_post.post_pod(conn, record, client=FakeWriteClient(), read_client_factory=offline)
    waiting = post_ledger.awaiting_report(conn)
    assert [a.record_id for a in waiting] == [1]

    spitfire_post.post_report(conn, record, client=FakeWriteClient())
    assert post_ledger.awaiting_report(conn) == [], "it drops off once the report lands"


def test_post_record_still_does_both_in_order(conn, record, offline):
    """The atomic path is kept for headless callers, and the order is the same one the split
    enforces: the POD is uploaded and attached before the report is built."""
    client = FakeWriteClient()
    result = spitfire_post.post_record(conn, record, client=client, read_client_factory=offline)

    assert result.ok and result.state == post_ledger.POSTED
    assert client.calls.index("verify_upload") < client.calls.index("link_document")
    assert len(client.uploads) == 2


def test_a_failed_pod_hash_means_no_report_is_ever_built(conn, record, offline):
    """The hash check is what stands between a corrupted upload and a receipt that cites it as
    proof. If it fails, nothing downstream may happen."""
    class BadHash(FakeWriteClient):
        def verify_upload(self, file_key, expected_md5):
            self.calls.append("verify_upload")
            return False

    client = BadHash()
    result = spitfire_post.post_record(conn, record, client=client, read_client_factory=offline)

    assert not result.ok
    assert len(client.uploads) == 1, "the report was never uploaded"
    assert post_ledger.awaiting_report(conn) == [], "and it did not come to rest as POD-posted"


# --- verifying what landed -----------------------------------------------------------------


def test_verify_pod_confirms_the_file_is_present_and_intact(conn, record, offline):
    client = FakeWriteClient()
    spitfire_post.post_pod(conn, record, client=client, read_client_factory=offline)
    result = spitfire_post.verify_pod(conn, record, client=client)

    assert result.ok and result.state == "verified"
    assert "bytes we sent" in result.message


def test_verify_pod_fails_when_the_catalog_hash_no_longer_matches(conn, record, offline):
    """The check a 200 never made. A file replaced in the catalog after upload is invisible to
    every other signal we hold."""
    client = FakeWriteClient()
    spitfire_post.post_pod(conn, record, client=client, read_client_factory=offline)

    class Tampered(FakeWriteClient):
        def __init__(self, attachments):
            super().__init__()
            self.attachments = attachments

        def verify_upload(self, file_key, expected_md5):
            return False

    result = spitfire_post.verify_pod(conn, record, client=Tampered(client.attachments))
    assert not result.ok
    assert "no longer matches" in result.message


def test_verify_pod_notices_the_file_missing_from_the_receipt(conn, record, offline):
    client = FakeWriteClient()
    spitfire_post.post_pod(conn, record, client=client, read_client_factory=offline)

    class Empty(FakeWriteClient):
        def read_attachments(self, doc_key):
            return []

    result = spitfire_post.verify_pod(conn, record, client=Empty())
    assert not result.ok
    assert "not on receipt" in result.message


def test_verify_pod_says_so_when_nothing_was_ever_posted(conn, record):
    result = spitfire_post.verify_pod(conn, record, client=FakeWriteClient())
    assert not result.ok
    assert "nothing has been posted" in result.message


def test_the_same_delivery_cannot_post_twice_under_a_new_record_id(conn, record, offline):
    """The duplicate that actually happened. PO 212559 collected eight receipts on training,
    each from a re-extraction of one delivery: `idempotency_key` carries the record id, and
    reprocessing a mail erases records and re-extracts them under new ids, so every attempt hashed
    to a new key and nothing could see they were the same goods.

    Spitfire cannot answer this either — `ReceiptInProgressUnits` reads 0.0 against the unapproved
    receipts we create — so the ledger has to, on (PO, line, POD hash).
    """
    first = spitfire_post.post_pod(conn, record, client=FakeWriteClient(),
                                   read_client_factory=offline)
    assert first.ok

    # The same delivery, re-extracted: a new record id, identical PO, line and POD bytes.
    conn.execute("""INSERT INTO extracted_records
                      (id, source_email_id, po_number, spec_code, item_description, vendor_name,
                       quantity_received, unit_of_measure, pod_stated_date, received_by,
                       po_line_number, email_date, extraction_source, extraction_confidence,
                       created_at)
                    SELECT 99, source_email_id, po_number, spec_code, item_description,
                           vendor_name, quantity_received, unit_of_measure, pod_stated_date,
                           received_by, po_line_number, email_date, extraction_source,
                           extraction_confidence, created_at
                      FROM extracted_records WHERE id = 1""")
    conn.commit()
    conn.row_factory = sqlite3.Row
    reextracted = conn.execute("SELECT * FROM extracted_records WHERE id = 99").fetchone()

    second = FakeWriteClient()
    result = spitfire_post.post_pod(conn, reextracted, client=second, read_client_factory=offline)

    assert not result.ok, "a second receipt for the same delivery must be refused"
    assert "create_receipt" not in second.calls, "and refused before Spitfire is touched"


def test_a_genuinely_different_delivery_on_the_same_line_still_posts(conn, record, offline):
    """The other half of the rule: different paperwork means different goods. Guarding on the PO
    line alone would block a real second shipment against a partially-received line."""
    spitfire_post.post_pod(conn, record, client=FakeWriteClient(), read_client_factory=offline)

    # Same PO and line, a different proof of delivery — a second shipment.
    conn.execute("""INSERT INTO mail_attachment
                      (email_id, ordinal, filename, content_type, kind, size_bytes, is_inline,
                       content)
                    VALUES ('mail-2', 0, 'POD2_212614.pdf', 'application/pdf', 'pdf', 9, 0, ?)""",
                 (pod_pdf("212614") + b"\n% second shipment",))
    conn.execute("""INSERT INTO attachment_ledger
                      (email_id, depth, ordinal, filename, sniffed_kind, disposition, is_inline,
                       first_seen_at)
                    VALUES ('mail-2', 0, 0, 'POD2_212614.pdf', 'pdf', 'extracted', 0, 'now')""")
    conn.execute("""INSERT INTO extracted_records
                      (id, source_email_id, po_number, spec_code, item_description, vendor_name,
                       quantity_received, unit_of_measure, pod_stated_date, received_by,
                       po_line_number, email_date, extraction_source, extraction_confidence,
                       created_at)
                    VALUES (98, 'mail-2', '212614', 'LT-03b', 'LT-03B Frosted Replacement',
                            'Archipelago Lighting', 5.0, 'EA', '2026-02-02', 'J Smith', 1,
                            '2026-02-02', 'test', 1.0, '2026-02-02')""")
    conn.commit()
    conn.row_factory = sqlite3.Row
    second_shipment = conn.execute("SELECT * FROM extracted_records WHERE id = 98").fetchone()

    client = FakeWriteClient()
    result = spitfire_post.post_pod(conn, second_shipment, client=client,
                                    read_client_factory=offline)
    assert result.ok, f"a distinct delivery must not be blocked: {result.message}"
    assert "create_receipt" in client.calls


# --- the reviewer's chosen proof of delivery ----------------------------------------------------
#
# `_pod_for` decides by reading each candidate, which is right for automation and blind in three
# real cases: a photographed delivery note (it re-reads PDFs only, because OCR is a paid call), a
# POD naming a different purchase order (rejected outright), and a proof that is not a carrier POD
# at all. `pod_ledger_id` is how a person settles it.


def _attach_and_id(conn, ordinal, filename, kind, content) -> int:
    """`_attach` above, plus the ledger id — which is what a reviewer's choice is recorded as."""
    _attach(conn, ordinal, filename, kind, content)
    return conn.execute(
        "SELECT id FROM attachment_ledger WHERE email_id = 'mail-1' AND ordinal = ?",
        (ordinal,)).fetchone()[0]


def _row(conn):
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM extracted_records WHERE id = 1").fetchone()


def test_a_reviewer_s_chosen_attachment_outranks_what_the_files_say(conn):
    """A photographed delivery note the ingest-time OCR never flagged. Nothing in the automatic
    rules can reach it — it is not a PDF, so it is skipped without being read — and it is the
    single most common shape of real proof Premier receives."""
    photo = _attach_and_id(conn, 1, "signed-bol.jpg", "image",
                           b"\xff\xd8\xff\xe0 a photo of a signed delivery note")
    conn.execute("UPDATE extracted_records SET pod_ledger_id = ? WHERE id = 1", (photo,))
    conn.commit()

    chosen = spitfire_post._pod_for(conn, _row(conn))
    assert chosen is not None and chosen.filename == "signed-bol.jpg"


def test_with_no_choice_made_the_automatic_rules_are_untouched(conn):
    """The guard on the override. With `pod_ledger_id` null the order is exactly what it was, so
    the 210634 bug — a tracker spreadsheet at ordinal 0 uploaded to Premier's ERP as the proof —
    cannot return through this door."""
    _attach(conn, 1, "tracker.xlsx", "xlsx", b"PK\x03\x04 not a pod")

    chosen = spitfire_post._pod_for(conn, _row(conn))
    assert chosen is not None and chosen.filename == "POD_212614.pdf"


def test_a_chosen_attachment_belonging_to_another_message_is_refused(conn):
    """A ledger id names a row in a table covering every message. Without the `email_id` guard a
    stale or wrong id would attach some other delivery's proof to this receipt."""
    other = conn.execute(
        """INSERT INTO attachment_ledger
           (email_id, depth, ordinal, filename, sniffed_kind, disposition, is_inline, first_seen_at)
           VALUES ('mail-999', 0, 0, 'someone-elses-pod.pdf', 'pdf', 'extracted', 0, 'now')"""
    ).lastrowid
    conn.execute("UPDATE extracted_records SET pod_ledger_id = ? WHERE id = 1", (other,))
    conn.commit()

    assert spitfire_post._pod_for(conn, _row(conn)) is None


def test_a_choice_whose_bytes_are_missing_does_not_fall_back(conn):
    """It reports no POD rather than quietly uploading the file the rules would have picked. A
    person who chose one file and got a different one attached would have no way to know."""
    ghost = conn.execute(
        """INSERT INTO attachment_ledger
           (email_id, depth, ordinal, filename, sniffed_kind, disposition, is_inline, first_seen_at)
           VALUES ('mail-1', 0, 7, 'never-stored.pdf', 'pdf', 'extracted', 0, 'now')"""
    ).lastrowid
    conn.execute("UPDATE extracted_records SET pod_ledger_id = ? WHERE id = 1", (ghost,))
    conn.commit()

    assert spitfire_post._pod_for(conn, _row(conn)) is None


# --- the body-only delivery ---------------------------------------------------------------------
#
# Most Authority Inbound notifications state everything in a line table and attach nothing.
# Refusing those for ever meant a real delivery could never be received — but automation must not
# make that call, so a named person waives it and nothing else can.


def _strip_the_pod(conn):
    """Leave the record with no attachment of any kind, as a body-only notification arrives."""
    conn.execute("DELETE FROM mail_attachment WHERE email_id = 'mail-1'")
    conn.execute("DELETE FROM attachment_ledger WHERE email_id = 'mail-1'")
    conn.commit()


def test_automation_will_not_post_a_body_only_delivery(conn, offline):
    """The rule that must survive everything else in this file."""
    _strip_the_pod(conn)

    result = spitfire_post.post_pod(conn, _row(conn), read_client_factory=offline,
                                    client=FakeWriteClient())
    assert not result.ok
    assert "proof of delivery" in result.message
    assert post_ledger.existing_for_record(conn, 1)[0].state == post_ledger.FLAGGED


def test_a_waived_body_only_delivery_posts_without_an_upload(conn, offline):
    """Steps 4 and 5 are skipped; steps 1-3 and the read-back are not. The receipt is real, its
    line is on it, and nothing was uploaded."""
    _strip_the_pod(conn)
    conn.execute("UPDATE extracted_records SET pod_waived_by = 'M Gutierrez', "
                 "pod_source = 'email_body' WHERE id = 1")
    conn.commit()

    client = FakeWriteClient()
    result = spitfire_post.post_pod(conn, _row(conn), read_client_factory=offline, client=client)

    assert result.ok, result.message
    assert result.state == post_ledger.POD_POSTED
    assert "upload_file" not in client.calls and "attach_file" not in client.calls
    assert "create_receipt" in client.calls and "add_line" in client.calls
    assert not result.pod_file_key
    assert "M Gutierrez" in result.message


def test_the_ledger_names_who_accepted_a_receipt_with_no_proof(conn, offline):
    """Six months later, "why does this receipt carry nothing?" has to be answerable from the
    ledger alone — the screen that asked the question is long gone."""
    _strip_the_pod(conn)
    conn.execute("UPDATE extracted_records SET pod_waived_by = 'M Gutierrez' WHERE id = 1")
    conn.commit()

    spitfire_post.post_pod(conn, _row(conn), read_client_factory=offline, client=FakeWriteClient())

    detail = post_ledger.existing_for_record(conn, 1)[0].detail
    assert "no POD" in detail and "M Gutierrez" in detail


def test_a_body_only_delivery_cannot_post_twice(conn, offline):
    """The duplicate guard with no hash to key on — the case the evidence key exists for. Without
    it every body-only delivery on one PO line would key alike, or to nothing."""
    _strip_the_pod(conn)
    conn.execute("UPDATE extracted_records SET pod_waived_by = 'M Gutierrez' WHERE id = 1")
    conn.execute(
        """INSERT INTO mail_body (email_id, subject, sender, received_at, body_text, source,
                                  cached_at)
           VALUES ('mail-1', 'Inbound', 'a@b.com', 'now', '19 EA LT-03b delivered', 'graph', 'now')""")
    conn.commit()

    first = spitfire_post.post_pod(conn, _row(conn), read_client_factory=offline,
                                   client=FakeWriteClient())
    assert first.ok, first.message

    again = spitfire_post.post_pod(conn, _row(conn), read_client_factory=offline,
                                   client=FakeWriteClient())
    assert not again.ok
    assert len([a for a in post_ledger.existing_for_record(conn, 1)
                if a.state == post_ledger.POD_POSTED]) == 1


def test_the_ledger_key_is_unchanged_whenever_a_pod_exists(conn, offline):
    """The compatibility guarantee. Ten deliveries are already posted in Premier's live store under
    a key that is the POD's MD5; a key that had changed shape would orphan every one of them and
    the guard would let them all post again."""
    import hashlib

    from pipeline import dedupe

    pod = spitfire_post._pod_for(conn, _row(conn))
    expected = hashlib.md5(pod.content).hexdigest().upper()
    assert dedupe.evidence_key(pod_md5=expected) == expected
    assert not dedupe.is_body_evidence(expected)

    spitfire_post.post_pod(conn, _row(conn), read_client_factory=offline, client=FakeWriteClient())
    stored = conn.execute("SELECT pod_md5 FROM spitfire_post WHERE record_id = 1").fetchone()[0]
    assert stored == expected
