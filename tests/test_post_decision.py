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
    "id": 1, "po_number": "212614", "vendor_name": "Archipelago Lighting", "po_line_number": 1,
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
                 VALUES ('212614', '4b186a21', 'MRC024PB100003', 'now')""")
    c.execute("""INSERT INTO spitfire_po_lines (line_key, po_number, line_number, cost_code,
                                                refreshed_at)
                 VALUES ('k1', '212614', 1, 'MAT-FDP', 'now')""")
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
    return po_verify.RecordVerification(record_id=1, po_number="212614", po_found=po_found,
                                        matched=check if po_found else None, error=error)


def test_a_clean_record_may_post(conn):
    decision = post_decision.decide(conn, COMPLETE, verification(), pod_md5="A")
    assert decision.may_post
    assert decision.project_code == "MRC024PB100003"
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
    PO 212559's own lines are 4/4/2/2 across separate shipments — so that rejected a large share
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


def test_a_po_outside_the_known_projects_is_refused_by_name(conn):
    """Not a retry and not a spinner: the project list cannot be discovered — `POST /api/projects`
    returns an empty list for this account — so a PO in a fourth project is terminal until Premier
    grants the permission."""
    decision = post_decision.decide(conn, COMPLETE, verification(po_found=False), pod_md5="A")
    assert not decision.may_post
    assert "not found in the projects" in decision.reason


def test_an_unknown_project_stops_the_post(conn):
    """`forProject` is required to create the receipt at all, and it comes from the mirror rather
    than from Spitfire, which will not say which project a PO belongs to."""
    conn.execute("DELETE FROM spitfire_po_index")
    conn.commit()
    decision = post_decision.decide(conn, COMPLETE, verification(), pod_md5="A")
    assert not decision.may_post
    assert "project" in decision.reason


def test_an_already_posted_delivery_is_refused_with_its_receipt_number(conn):
    attempt = post_ledger.claim(conn, record_id=1, po_number="212614", line_number=1,
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
    row = dict(COMPLETE, pod_waived_by="M Gutierrez", pod_source="email_body")
    decision = post_decision.decide(conn, row, verification(), pod_md5="")
    assert decision.may_post, decision.reason
    assert decision.pod_waived_by == "M Gutierrez"


def test_a_blank_waiver_is_not_a_waiver(conn):
    """Whitespace is how an empty form field arrives. Treating it as a name would let a POST with
    nothing typed in it permit a receipt carrying no proof."""
    for empty in ("", "   ", None):
        row = dict(NO_DELIVERY_EVIDENCE, pod_waived_by=empty)
        assert not post_decision.decide(conn, row, verification(), pod_md5="").may_post


def test_a_waiver_does_not_excuse_anything_else(conn):
    """It is a waiver of the *proof document*, not of the delivery date, and not of the quantity
    checks. Conflating them would make it a way to post anything at all."""
    undated = dict(COMPLETE, pod_waived_by="M Gutierrez", pod_stated_date=None)
    assert "POD date" in post_decision.decide(conn, undated, verification(), pod_md5="").reason

    over = dict(COMPLETE, pod_waived_by="M Gutierrez")
    decision = post_decision.decide(conn, over, verification(email_qty=99.0, ordered=12.0),
                                    pod_md5="")
    assert not decision.may_post and "over-receive" in decision.reason


def test_a_record_with_a_pod_never_reports_a_waiver(conn):
    """`pod_waived_by` can be left set on a record that later acquires a POD — from a reprocess, or
    from a reviewer choosing an attachment afterwards. The decision must then say the receipt
    carries proof, because it does."""
    row = dict(COMPLETE, pod_waived_by="M Gutierrez")
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
    row = dict(COMPLETE, pod_waived_by="M Gutierrez")
    decision = post_decision.decide(conn, row, verification(), pod_md5="")

    assert decision.pod_waived_by == "M Gutierrez"
    assert decision.body_evidence == ""
