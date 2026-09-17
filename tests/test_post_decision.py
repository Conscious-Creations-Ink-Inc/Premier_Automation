"""The gate. What may be written to Premier's ERP without a human looking at it first.

`pipeline/po_verify.py` deliberately declines to give a verdict — it states figures and leaves
judgement to the reader, which is right for a comparison screen and useless for an automatic
post. This module makes the ruling instead, and these tests hold each gate to a refusal.

The reasons are asserted, not just the verdict. A flag that says "mismatch" tells the person
reading the dialog nothing they can act on; one that names the two numbers does.
"""

import pytest

from pipeline import post_decision, post_ledger, po_verify, state_db

COMPLETE = {
    "id": 1, "po_number": "912614", "vendor_name": "Archipelago Lighting", "po_line_number": 1,
    "item_description": "LT-03B Frosted Replacement", "quantity_received": 19.0,
    "unit_of_measure": "EA", "spec_code": "LT-03b", "pod_stated_date": "2026-01-20",
    "received_by": "J Smith",
}


# Complete in every field, but with nothing showing that anyone took delivery: no signer, no
# carrier reference. This is the shape of a row on one of Premier's pending-confirmation
# spreadsheets — 57 of the 92 records in the corpus look like this, and none of them has arrived.
NO_DELIVERY_EVIDENCE = {k: v for k, v in COMPLETE.items() if k != "received_by"}


@pytest.fixture
def conn():
    c = state_db.get_connection(":memory:")
    c.execute("""INSERT INTO spitfire_po_index (po_number, doc_master_key, project_code,
                                                refreshed_at)
                 VALUES ('912614', '4b186a21', 'PRJ001PB100003', 'now')""")
    c.execute("""INSERT INTO spitfire_po_lines (line_key, po_number, line_number, cost_code,
                                                refreshed_at)
                 VALUES ('k1', '912614', 1, 'MAT-FDP', 'now')""")
    c.commit()
    return c


def verification(*, email_qty=19.0, ordered=19.0, outstanding=None, uom="EA",
                 spec_resolved=True, po_found=True, error=None, reviewer_chose=False):
    check = po_verify.LineCheck(
        line_number=1, spec_code="LT-03b", description="LT-03B Frosted Replacement",
        unit_of_measure="EA", qty_ordered=ordered, qty_received=0.0, qty_in_transit=0.0,
        qty_outstanding=ordered if outstanding is None else outstanding,
        record_quantity=email_qty, record_uom=uom, spec_resolved=spec_resolved,
        reviewer_chose=reviewer_chose)
    return po_verify.RecordVerification(record_id=1, po_number="912614", po_found=po_found,
                                        matched=check if po_found else None, error=error)


def test_a_clean_record_may_post(conn):
    decision = post_decision.decide(conn, COMPLETE, verification(), pod_md5="A")
    assert decision.may_post
    assert decision.project_code == "PRJ001PB100003"
    assert decision.cost_code == "MAT-FDP", "the receipt must post against the PO's own cost code"


def test_an_incomplete_record_is_refused_and_names_the_missing_field(conn):
    """`completeness.py` leaves a standing instruction for exactly this: refuse a record whose
    `is_complete` is False, or a receiver with no POD date reaches Premier's ERP."""
    row = dict(COMPLETE, pod_stated_date=None)
    decision = post_decision.decide(conn, row, verification(), pod_md5="A")
    assert not decision.may_post
    assert "POD date" in decision.reason


def test_a_record_with_no_pod_bytes_is_refused(conn):
    """Measured 2026-08-14: only 17 of 35 stored attachments carry their bytes. A receipt asserting
    a delivery with no proof attached is worse than no receipt."""
    decision = post_decision.decide(conn, NO_DELIVERY_EVIDENCE, verification(), pod_md5="")
    assert not decision.may_post
    assert "no proof of delivery" in decision.reason


def test_the_callers_reason_is_shown_rather_than_the_generic_one(conn):
    """This module cannot tell "Premier sent nothing" from "we lost the bytes" — it never sees the
    attachments. `spitfire_post._pod_absence_reason` can, and those need different fixes, so when
    it supplies a reason that is the sentence the reviewer reads."""
    decision = post_decision.decide(
        conn, NO_DELIVERY_EVIDENCE, verification(), pod_md5="",
        pod_reason="the delivery email carried no attachments, so there is no proof of delivery")

    assert not decision.may_post
    assert decision.reason == ("the delivery email carried no attachments, so there is no proof "
                               "of delivery")


def test_completeness_is_checked_before_the_network(conn):
    """The cheapest, most certain gate runs first, so a record missing a POD date never costs a
    call to Spitfire — and the reason shown is the first thing wrong, not the last thing checked.
    Passing no verification would force a live read if this gate did not stop it.

    The missing field is the POD date rather than received-by since 2026-08-21: received-by is
    `DERIVED` now and no longer blocks anything. The delivery date is what this gate still exists
    for.
    """
    row = dict(COMPLETE, pod_stated_date=None)
    decision = post_decision.decide(conn, row, pod_md5="A")
    assert "POD date" in decision.reason


def test_a_derived_field_never_costs_a_call_to_spitfire(conn):
    """The inverse of the gate above, and the reason the split was made: a record with no vendor,
    no UOM and no received-by is not incomplete, so `decide` must get *past* completeness. It then
    stops at the next gate rather than here."""
    row = dict(COMPLETE, vendor_name=None, unit_of_measure=None, received_by=None)
    decision = post_decision.decide(conn, row, verification(), pod_md5="A")
    assert "incomplete" not in decision.reason


def test_a_quantity_difference_states_both_numbers(conn):
    decision = post_decision.decide(conn, COMPLETE, verification(email_qty=19.0, ordered=12.0),
                                    pod_md5="A")
    assert not decision.may_post
    assert "19" in decision.reason and "12" in decision.reason


def test_a_partial_delivery_posts(conn):
    """The case the first version of this gate rejected outright.

    It compared the email against `qty_ordered` and demanded equality, so 2 arriving against 19
    ordered was refused permanently with no way to accept it. Partial deliveries are ordinary —
    PO 912559's own lines are 4/4/2/2 across separate shipments — so that rejected a large share
    of real traffic. Receiving part of a line books what arrived and leaves the rest outstanding.
    """
    decision = post_decision.decide(conn, dict(COMPLETE, quantity_received=2.0),
                                    verification(email_qty=2.0, ordered=19.0), pod_md5="A")
    assert decision.may_post, decision.reason
    assert decision.quantity == 2.0, "the receipt books what arrived, not what was ordered"


def test_a_full_delivery_still_posts(conn):
    decision = post_decision.decide(conn, COMPLETE, verification(email_qty=19.0, ordered=19.0),
                                    pod_md5="A")
    assert decision.may_post, decision.reason


def test_a_second_partial_against_what_is_left_posts(conn):
    """17 already received of 19, and 2 more arrive. This is the normal shape of a split shipment
    and the reason the comparison is against outstanding rather than against the order."""
    decision = post_decision.decide(conn, dict(COMPLETE, quantity_received=2.0),
                                    verification(email_qty=2.0, ordered=19.0, outstanding=2.0),
                                    pod_md5="A")
    assert decision.may_post, decision.reason


def test_an_over_receipt_is_refused_with_both_figures(conn):
    """202 against 196 is in the corpus and is a conversation with the vendor, not something to
    book unattended."""
    decision = post_decision.decide(conn, dict(COMPLETE, quantity_received=20.0),
                                    verification(email_qty=20.0, ordered=19.0), pod_md5="A")
    assert not decision.may_post
    assert "over-receive" in decision.reason
    assert "20" in decision.reason and "19" in decision.reason


def test_a_zero_quantity_is_refused(conn):
    decision = post_decision.decide(conn, dict(COMPLETE, quantity_received=0.0),
                                    verification(email_qty=0.0, ordered=19.0), pod_md5="A")
    assert not decision.may_post
    assert "nothing to receive" in decision.reason


def test_the_over_receipt_tolerance_is_zero(conn):
    """Zero is defensible for "how far past what is owed will we book without a human", where it
    was never defensible for "is this the full order"."""
    assert post_decision.OVER_RECEIPT_TOLERANCE == 0.0


def test_a_line_reached_by_description_alone_is_refused(conn):
    """A fuzzy description match is a weaker claim than anything writing to an ERP should rest on.
    The Verify screen already labels it as such for a human; here it stops the post."""
    decision = post_decision.decide(conn, COMPLETE, verification(spec_resolved=False),
                                    pod_md5="A")
    assert not decision.may_post
    assert "description alone" in decision.reason


def test_a_line_a_reviewer_chose_is_accepted(conn):
    """The only recovery path there is when the matcher picks the wrong line, and it used to be
    blocked. `po_verify` marks a hand-picked line `spec_resolved=False, reviewer_chose=True`,
    and refusing that as "matched on description alone" was both a block on the recovery and a
    false statement — a person choosing is a stronger claim than a fuzzy description match."""
    decision = post_decision.decide(
        conn, COMPLETE, verification(spec_resolved=False, reviewer_chose=True), pod_md5="A")
    assert decision.may_post, decision.reason


def test_a_contradicted_unit_is_refused(conn):
    """19 EA and 19 CS are not the same delivery."""
    decision = post_decision.decide(conn, COMPLETE, verification(uom="CS"), pod_md5="A")
    assert not decision.may_post
    assert "CS" in decision.reason and "EA" in decision.reason


def test_a_line_with_nothing_outstanding_is_refused(conn):
    decision = post_decision.decide(conn, COMPLETE, verification(outstanding=0), pod_md5="A")
    assert not decision.may_post
    assert "nothing outstanding" in decision.reason


def test_a_po_spitfire_does_not_have_is_refused_by_name(conn):
    """Not a retry and not a spinner: no `forProject` means no receipt can be created at all.

    The refusal names *their* data, not our configuration. It used to read "not found in the
    projects this connector can search (…)" followed by the configured project ids, which
    described our own configuration to somebody who can do nothing about it — and, worse, said
    it just as
    confidently when the lookup had failed rather than answered. `po_verify` now turns a lookup
    that could not complete into an error, so reaching this line means Spitfire answered.
    """
    decision = post_decision.decide(conn, COMPLETE, verification(po_found=False), pod_md5="A")
    assert not decision.may_post
    assert "Spitfire has no purchase order" in decision.reason
    assert "912614" in decision.reason, "a refusal that does not name the order is unactionable"
    assert "connector can search" not in decision.reason


def test_an_unknown_project_stops_the_post(conn):
    """`forProject` is required to create the receipt at all, and it comes from the mirror rather
    than from Spitfire, which will not say which project a PO belongs to."""
    conn.execute("DELETE FROM spitfire_po_index")
    conn.commit()
    decision = post_decision.decide(conn, COMPLETE, verification(), pod_md5="A")
    assert not decision.may_post
    assert "project" in decision.reason


def test_an_already_posted_delivery_is_refused_with_its_receipt_number(conn):
    attempt = post_ledger.claim(conn, record_id=1, po_number="912614", line_number=1,
                                pod_md5="A")
    post_ledger.record_receipt(conn, attempt.idempotency_key, receipt_key="abc",
                              receipt_doc_no="0007")
    post_ledger.settle(conn, attempt.idempotency_key, post_ledger.POSTED, "done")

    decision = post_decision.decide(conn, COMPLETE, verification(), pod_md5="A")
    assert not decision.may_post
    assert "already posted" in decision.reason and "0007" in decision.reason


def test_a_read_failure_is_reported_rather_than_assumed_clean(conn):
    decision = post_decision.decide(conn, COMPLETE, verification(error="connection timed out"),
                                    pod_md5="A")
    assert not decision.may_post
    assert "timed out" in decision.reason


# --- the POD waiver ------------------------------------------------------------------------
#
# Some deliveries are stated entirely in the email body — most Authority Inbound notifications
# are — and refusing those for ever meant a real delivery could never be received. But automation
# must never decide that for itself, so the waiver is a stored fact about one record, set by one
# named person, and null everywhere until they set it.


def test_automation_cannot_post_a_record_with_no_pod(conn):
    """Completeness alone still does not open the POD-less path.

    Premier widened this on 2026-08-22 — a delivery whose particulars are all stated in the mail
    may post without a proof document — but "complete" was never allowed to be the test. A
    pending-confirmation spreadsheet row is complete and nothing has arrived, so what opens the
    path is evidence someone took delivery, not the absence of missing fields.
    """
    decision = post_decision.decide(conn, NO_DELIVERY_EVIDENCE, verification(), pod_md5="")
    assert not decision.may_post
    assert decision.reason == "there is no proof of delivery to upload"


def test_a_named_person_may_waive_the_pod_and_then_it_posts(conn):
    """The body-only path, and the only way past the gate above."""
    row = dict(COMPLETE, pod_waived_by="M Rivera", pod_source="email_body")
    decision = post_decision.decide(conn, row, verification(), pod_md5="")
    assert decision.may_post, decision.reason
    assert decision.pod_waived_by == "M Rivera"


def test_a_blank_waiver_is_not_a_waiver(conn):
    """Whitespace is how an empty form field arrives. Treating it as a name would let a POST with
    nothing typed in it permit a receipt carrying no proof."""
    for empty in ("", "   ", None):
        row = dict(NO_DELIVERY_EVIDENCE, pod_waived_by=empty)
        assert not post_decision.decide(conn, row, verification(), pod_md5="").may_post


def test_a_waiver_does_not_excuse_anything_else(conn):
    """It is a waiver of the *proof document*, not of the delivery date, and not of the quantity
    checks. Conflating them would make it a way to post anything at all."""
    undated = dict(COMPLETE, pod_waived_by="M Rivera", pod_stated_date=None)
    assert "POD date" in post_decision.decide(conn, undated, verification(), pod_md5="").reason

    over = dict(COMPLETE, pod_waived_by="M Rivera")
    decision = post_decision.decide(conn, over, verification(email_qty=99.0, ordered=12.0),
                                    pod_md5="")
    assert not decision.may_post and "over-receive" in decision.reason


def test_a_record_with_a_pod_never_reports_a_waiver(conn):
    """`pod_waived_by` can be left set on a record that later acquires a POD — from a reprocess, or
    from a reviewer choosing an attachment afterwards. The decision must then say the receipt
    carries proof, because it does."""
    row = dict(COMPLETE, pod_waived_by="M Rivera")
    decision = post_decision.decide(conn, row, verification(), pod_md5="ABC123")
    assert decision.may_post and decision.pod_waived_by == ""


# --- a delivery stated in the mail body, with no proof document --------------------------------
#
# Premier's decision, 2026-08-22. The gate stays shut on everything that does not carry positive
# evidence someone took delivery, because completeness alone describes a spreadsheet row just as
# well as it describes a delivery.


def test_a_delivery_signed_for_in_the_mail_may_post_without_a_pod(conn):
    decision = post_decision.decide(conn, COMPLETE, verification(), pod_md5="")

    assert decision.may_post, decision.reason
    assert decision.body_evidence == "signer+date"
    assert decision.pod_waived_by == ""      # not a waiver — nobody was asked


def test_a_carrier_reference_and_a_date_also_count(conn):
    """`authority_delivered` notifications carry no signer but name the carrier and the tracking
    number on 10 of 10 records in the corpus."""
    row = {k: v for k, v in COMPLETE.items() if k != "received_by"}
    row.update(carrier_name="FedEx", tracking_number="884603885067")
    decision = post_decision.decide(conn, row, verification(), pod_md5="")

    assert decision.may_post, decision.reason
    assert decision.body_evidence == "carrier+tracking+date"


def test_a_carrier_with_no_tracking_number_is_not_evidence(conn):
    """Half a carrier reference cannot be checked against the carrier, so it proves nothing."""
    row = {k: v for k, v in COMPLETE.items() if k != "received_by"}
    row.update(carrier_name="FedEx", tracking_number="")

    assert post_decision.body_evidence(row) == ""
    assert not post_decision.decide(conn, row, verification(), pod_md5="").may_post


def test_a_signer_with_no_date_is_not_evidence(conn):
    """A receipt has to state when the goods arrived. 37 of 92 records carry no POD date."""
    row = dict(COMPLETE, pod_stated_date="")

    assert post_decision.body_evidence(row) == ""


def test_body_evidence_is_not_claimed_when_a_pod_exists(conn):
    """The two routes past gate 2 must stay distinguishable in the ledger: a receipt carrying a
    proof document has not used the body-evidence path, whatever the record also happens to say."""
    decision = post_decision.decide(conn, COMPLETE, verification(), pod_md5="ABC123")

    assert decision.may_post
    assert decision.body_evidence == ""


def test_a_waiver_is_recorded_as_a_waiver_not_as_body_evidence(conn):
    """A person's decision and a parsed signal are different facts and must not be conflated —
    a reviewer waived this one, and the ledger has to say so even though the body would qualify."""
    row = dict(COMPLETE, pod_waived_by="M Rivera")
    decision = post_decision.decide(conn, row, verification(), pod_md5="")

    assert decision.pod_waived_by == "M Rivera"
    assert decision.body_evidence == ""


# --- Gate 2's fourth route: a delivery document from outside Premier ------------------------------
#
# Added 2026-09-11. The signer and carrier tests above are a 2026-08-22 proxy for one question —
# did somebody outside Premier say these goods arrived — and they answer it badly: 89 of 4,717
# records in the live store name a signer, so the proxy refuses most real delivery notes for not
# printing a name. `read_views._awaits_confirmation` has asked that question directly since
# 2026-09-07, so `document_evidence` asks the three things that actually have to be true instead.
#
# Every test below is a refusal except the first. That is the shape of the thing: the route exists
# to admit delivery documents, and each exclusion is a measured case it must keep refusing.

DELIVERY_NOTE = dict(
    {k: v for k, v in COMPLETE.items() if k != "received_by"},
    origin_sender="dispatch@example-logistics.test",
    extraction_source="pdf:text",
    pod_source=None,
)
"""A signed-for-by-nobody delivery note: PDF, from a carrier, stating its own delivery date.

Deliberately built from `NO_DELIVERY_EVIDENCE`'s fields — no signer, no carrier reference — so
these tests prove the *new* route admits it and not one of the old two.
"""


def test_a_delivery_note_from_outside_premier_may_post_on_its_stated_date(conn):
    assert post_decision.body_evidence(DELIVERY_NOTE) == "document+date"
    decision = post_decision.decide(conn, DELIVERY_NOTE, verification(), pod_md5="")
    assert decision.may_post
    assert decision.body_evidence == "document+date", "the ledger has to say which route opened it"


def test_a_date_premier_supplied_itself_is_not_evidence(conn):
    """`ingest_orchestrator._fallback_pod_date` stamps `email_received_date` when the document gave
    no date, and says in its own docstring that such a date "cannot smuggle anything into Spitfire
    on its own". This is that sentence enforced.

    Measured 2026-09-10: 58 of the 185 records then on the Records page carried one, including 16
    of the 32 lines of the delivery that prompted this work.
    """
    row = dict(DELIVERY_NOTE, pod_source="email_received_date")
    assert post_decision.body_evidence(row) == ""
    assert not post_decision.decide(conn, row, verification(), pod_md5="").may_post


def test_a_spreadsheet_worklist_is_never_a_delivery_document(conn):
    """The case that decided the shape of this rule.

    32 records on the Records page were read out of a message subject *"[External] FW: Cameo Public
    Space- Delivery Confirmation Required"* — a worklist **asking** whether goods arrived. It is
    forwarded, so it is externally authored and the confirmation gate lets it through; it states a
    date, so a date-only rule would post every line of it. Only the source tells the truth about it.
    """
    row = dict(DELIVERY_NOTE, extraction_source="excel:Hoja1")
    assert post_decision.body_evidence(row) == ""
    assert not post_decision.decide(conn, row, verification(), pod_md5="").may_post


def test_a_message_premier_wrote_itself_is_not_a_delivery_document(conn):
    row = dict(DELIVERY_NOTE, origin_sender="expediting@example-pm.test")
    assert post_decision.body_evidence(row) == ""


def test_an_unresolvable_origin_fails_closed(conn):
    """`authorship.authored_internally` calls an unknown sender external on purpose, so that a
    message nobody can place is not silently withheld from a *queue*. That default is right there
    and wrong here: this answer permits a write to an ERP, so unknown refuses.
    """
    for unknown in (None, "", "   "):
        assert post_decision.body_evidence(dict(DELIVERY_NOTE, origin_sender=unknown)) == "", unknown


def test_a_quantity_conflict_is_never_a_delivery_document(conn):
    row = dict(DELIVERY_NOTE, extraction_source="pdf:text+quantity_conflict")
    assert post_decision.body_evidence(row) == ""


def test_the_older_routes_still_win_where_they_apply(conn):
    """Order matters for the ledger, not for the verdict: a record that has a signer is recorded as
    having one, rather than as a document, so `_why_postable` can name the right sentence."""
    assert post_decision.body_evidence(dict(DELIVERY_NOTE, received_by="J Smith")) == "signer+date"
    assert post_decision.body_evidence(
        dict(DELIVERY_NOTE, carrier_name="XPO", tracking_number="1Z9")) == "carrier+tracking+date"


def test_the_gate_finds_the_sender_a_projection_left_out(conn):
    """The drift this route could most easily have introduced.

    The Records page reads rows from `read_views._records_pending`, which projects `origin_sender`.
    The grouped post reads them from `deliveries_store.lines_for` — `SELECT *` over
    `extracted_records`, which has no such column. Had this been read off the row alone, the button
    would have offered lines the gate then refused, for no reason a person could see.
    """
    conn.execute("INSERT INTO email_log (email_id, origin_sender, category, folder, processed_at) "
                 "VALUES ('m-1', ?, 'delivery', 'Processed', 'now')",
                 ("dispatch@example-logistics.test",))
    conn.commit()
    no_column = {k: v for k, v in DELIVERY_NOTE.items() if k != "origin_sender"}
    no_column["source_email_id"] = "m-1"

    assert post_decision.body_evidence(no_column) == "", "read off the row alone it cannot know"
    assert post_decision.decide(conn, no_column, verification(), pod_md5="").may_post, \
        "but the gate resolves it and admits the same record"
