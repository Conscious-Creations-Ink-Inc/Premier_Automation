"""End-to-end against Premier's real `.msg` corpus.

Skipped when the corpus folder is absent (it is Premier's data, not ours, and is not committed),
so a checkout without it still runs green. When it is present these are the only tests that
prove the pipeline works on real messages rather than on shapes we invented.

`tools/run_corpus.py` scores the same run in more detail and writes the report; this module
pins the handful of properties that must never regress silently.
"""

from pathlib import Path

import pytest

from connectors.msg_file import MsgFileMailbox
from pipeline.models import TriageCategory
from pipeline.stage1_triage import triage

CORPUS_DIR = Path(r"D:\Premier\Documents\Premier\5,8 june")

pytestmark = pytest.mark.skipif(
    not CORPUS_DIR.exists(), reason=f"real corpus not available at {CORPUS_DIR}"
)


@pytest.fixture(scope="module")
def emails():
    # read_only: a regression run must never move Premier's source files into Processed/Routed.
    return MsgFileMailbox(CORPUS_DIR, read_only=True).fetch_new()


@pytest.fixture(scope="module")
def by_file(emails):
    mailbox = MsgFileMailbox(CORPUS_DIR, read_only=True)
    mailbox.fetch_new()
    return {Path(path).name: email_id for email_id, path in mailbox._path_by_email_id.items()}


def find(emails, name_fragment):
    for email in emails:
        if name_fragment.lower() in (email.subject or "").lower():
            return email
    raise AssertionError(f"no corpus message whose subject contains {name_fragment!r}")


def test_every_message_parses(emails):
    assert len(emails) == 14
    assert all(email.email_id for email in emails)
    assert all("\x00" not in a.filename for email in emails for a in email.attachments)


def test_every_message_is_triaged_without_falling_through_to_unknown(emails):
    results = [triage(email) for email in emails]
    assert all(r.matched_rule != "rule_7_unknown" for r in results), \
        [r.matched_rule for r in results if r.matched_rule == "rule_7_unknown"]


def test_four_inbound_notifications_surface_and_nothing_else_does(emails):
    surfaced = [triage(e) for e in emails]
    surfaced = [r for r in surfaced if r.category == TriageCategory.SURFACE]
    assert len(surfaced) == 4
    assert {r.matched_rule for r in surfaced} == {"rule_1a_authority_inbound"}


def test_nested_message_attachments_are_recursed_into(emails):
    """`5star Fabric Verification Response.msg` carries an attached Delivered Notification which
    itself carries the FedEx POD PDFs. Without recursion the PODs are lost."""
    email = find(emails, "Verification of Fabric Receipt")
    names = [a.filename for a in email.attachments]
    assert any(name.endswith(".pdf") for name in names), names
    assert any("Delivered Notification" in name for name in names), names


def test_identical_pods_under_two_filenames_collapse_to_one(emails):
    """Two attachments on that message are byte-identical PODs saved under different names — one
    of the two names is simply wrong. Content is truth, so only one is read.

    Both are still *reported*: the duplicate is carried with a `drop_hint` so the attachment
    ledger shows both slots and which one was actually extracted. Removing it outright, as this
    used to, meant a human auditing the mail could not tell the second file had ever existed.
    """
    email = find(emails, "Verification of Fabric Receipt")
    pdfs = [a for a in email.attachments if a.filename.endswith(".pdf")]
    assert len(pdfs) == 2

    readable = [a for a in pdfs if not a.drop_hint]
    assert len(readable) == 1
    assert readable[0].content_bytes, "the kept copy must still carry its bytes"

    duplicate = next(a for a in pdfs if a.drop_hint)
    assert duplicate.drop_hint.startswith("duplicate:")
    assert duplicate.content_bytes == b"", "a dropped duplicate carries metadata only"


def test_signature_logos_are_dropped_and_real_photos_are_kept(emails):
    """The same 15,948-byte Premier logo appears 30 times across the corpus; the pallet photos
    are 2.6 MB and up. Sending logos to OCR is cost with no signal; dropping a photo loses the
    only POD there is."""
    all_attachments = [a for email in emails for a in email.attachments]
    assert not any(len(a.content_bytes) == 15948 for a in all_attachments)

    photos = find(emails, "Cameo Harbour Delivery")
    assert len([a for a in photos.attachments if a.filename.startswith("IMG_")]) == 5


def test_the_same_notice_direct_and_forwarded_agrees_on_every_key(emails):
    """Notice 239336 arrives twice — once from Authority, once forwarded by an expeditor under a
    different Message-ID."""
    copies = [triage(e) for e in emails if "239336" in (e.subject or "")]
    assert len(copies) == 2
    assert {c.email.sender_domain for c in copies} == {"authoritylogistics.com", "premierpm.com"}
    for field in ("category", "notification_type", "matched_rule", "extracted_po_hints",
                  "extracted_shipment_hint", "notification_number", "origin_sender_address"):
        assert len({str(getattr(c, field)) for c in copies}) == 1, field


def test_the_delivered_inbound_pair_shares_one_shipment_key(emails):
    """Delivered 50009 and Inbound 239260 are the same physical delivery. If their keys differ,
    Stage 2 creates two receivers for it."""
    delivered = triage(find(emails, "50009 - Delivered Notification"))
    inbound = triage(find(emails, "239260 - Inbound Notification"))
    assert delivered.extracted_shipment_hint == inbound.extracted_shipment_hint == "50009"
    assert delivered.category == TriageCategory.HOLD
    assert inbound.category == TriageCategory.SURFACE


def test_the_inbound_number_is_never_returned_as_a_po(emails):
    inbound = triage(find(emails, "239260 - Inbound Notification"))
    assert inbound.extracted_po_hints == ["206725", "207665"]
    assert "239260" not in inbound.extracted_po_hints


def test_the_status_report_thread_is_routed_to_a_person(emails):
    """It arrives from an authoritylogistics.com address and discusses a lost item and a
    replacement PO — recognisable as delivery mail, out of scope, human only."""
    result = triage(find(emails, "Purchase Order Status Report"))
    assert result.category == TriageCategory.ROUTE


def test_no_attachment_in_premiers_real_mail_goes_unclaimed():
    """The headline promise, measured against their actual mail rather than my fixtures.

    `no_adapter` means nothing recognised the file — a genuine coverage gap. It is distinct
    from `unsupported_format` (a decision we made and can explain) and from `empty` (we read it
    and it held nothing). Any occurrence here is a real production hole.
    """
    import tempfile

    from tools.run_corpus import run

    with tempfile.TemporaryDirectory() as out_dir:
        report = run(CORPUS_DIR, Path(out_dir))

    ledger = report["attachments"]
    assert ledger["unclaimed"] == [], f"unclaimed attachments: {ledger['unclaimed']}"
    assert ledger["orphans"] == 0, "an attachment escaped the dispatcher without a verdict"

    # Every attachment accounted for, and the trackers genuinely read rather than guessed at.
    assert len(ledger["rows"]) == 72
    extracted = {r["filename"]: r["records_extracted"]
                 for r in ledger["rows"] if r["disposition"] == "extracted"}
    assert extracted.get("Cameo Receivers.xlsx") == 17
    assert extracted.get("Public Space - Pending Receipt Confirmation Orders.xlsx") == 91


def test_the_full_run_scores_clean():
    """The scored regression run, as `tools/run_corpus.py` performs it."""
    import tempfile

    from tools.run_corpus import run

    with tempfile.TemporaryDirectory() as out_dir:
        report = run(CORPUS_DIR, Path(out_dir))
    score = report["score"]
    assert score["failures"] == [], score["failures"]
    assert score["unlabelled_files"] == []
    totals = score["totals"]
    assert totals["triage_ok"] == totals["files"] == 14
    assert totals["parsed_ok"] == totals["parsed_scored"]
    assert totals["staged_ok"] == totals["staged_scored"]
