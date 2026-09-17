"""The advertising lexicon: what it claims, what it refuses to claim, and what it will not look at.

The lists are data and the bands are the only logic, so these tests are about the boundaries — and
above all about the veto. `ADVERTISING` is the one verdict that hides a message, and it is reachable
only with a business score of exactly zero.
"""
import pytest

from pipeline.parsing import promotional
from pipeline.parsing.promotional import (ADVERTISING, MAYBE, MAYBE_AT, NEITHER,
                                          Message, classify)

UNSUB = '<p><a href="https://x.test/u/unsubscribe?id=9">Unsubscribe</a></p>'


def ad(**overrides) -> Message:
    defaults = dict(subject="UP TO 70% OFF end-of-summer clearance",
                    sender="deals@members.example-retail.test", body_html="<p>Sale.</p>")
    defaults.update(overrides)
    return Message(**defaults)


# --- the bands ----------------------------------------------------------------------------------

def test_clear_advertising_is_called_advertising():
    verdict = classify(ad())
    assert verdict.band == ADVERTISING
    assert verdict.business == 0


def test_the_body_opt_out_block_alone_reaches_the_bar():
    """The original signal, now one term among several rather than the only way in."""
    verdict = classify(Message(subject="Introducing Betty", sender="hello@shop.test",
                               body_html="<p>New range.</p>" + UNSUB))
    assert verdict.band == ADVERTISING


def test_a_discount_subject_reaches_the_bar_without_any_body():
    """The reach this lexicon was written for: 95% of the store has no body on disk, so a rule that
    can only read bodies is inert on nearly all of it."""
    verdict = classify(Message(subject="Labor Day Top Deals: Save 30-50% on Appliances",
                               sender="someone@ordinary.test"))
    assert verdict.band == ADVERTISING


def test_one_weak_signal_is_not_enough_to_hide_anything():
    """`ADVERTISING_AT` is two independent tells, never one."""
    verdict = classify(Message(subject="Our newsletter", sender="someone@ordinary.test"))
    assert verdict.band != ADVERTISING


def test_ordinary_correspondence_claims_nothing():
    verdict = classify(Message(subject="Can you send the revised quote for the lobby?",
                               sender="buyer@vendor.test"))
    assert verdict.band == NEITHER


# --- the veto: the reason this module is safe to act on -----------------------------------------

def test_a_reply_is_never_advertising():
    """The strongest signal measured on the live store: 0% of adverts are replies, 84.6% of genuine
    delivery mail is. Premier's receiving happens in conversations; advertising arrives cold."""
    verdict = classify(ad(subject="RE: UP TO 70% OFF end-of-summer clearance"))
    assert verdict.band == MAYBE
    assert "it is a reply in a conversation" in verdict.matched


def test_a_purchase_order_vetoes_however_promotional_the_rest_is():
    verdict = classify(ad(subject="UP TO 70% OFF — regarding PO 912448", has_po=True))
    assert verdict.band == MAYBE


def test_a_property_name_vetoes():
    verdict = classify(ad(subject="70% off clearance for Sheraton Anchorage"))
    assert verdict.band == MAYBE


def test_an_attachment_vetoes():
    """A promotional subject over a spreadsheet is a vendor's promotion attached to real work."""
    assert classify(ad(has_attachment=True)).band == MAYBE


def test_the_veto_is_absolute_not_a_subtraction():
    """One business point beats any promotional score. If this were arithmetic, a loud enough
    subject would eventually outweigh a purchase order — and that is the one thing it must not do."""
    loud = ad(subject="RE: 70% OFF CLEARANCE — don't miss, shop now, last call, save big!")
    verdict = classify(loud)
    assert verdict.promo > verdict.business
    assert verdict.band == MAYBE


# --- signals measured and deliberately excluded --------------------------------------------------

def test_all_caps_alone_scores_nothing():
    """22.5% of adverts against 30.4% of delivery mail — it discriminates in the wrong direction.
    Premier's own subjects shout: SHERATON ANCHORAGE GUESTROOMS."""
    assert classify(Message(subject="GUESTROOM CASEGOODS UPDATE",
                            sender="someone@ordinary.test")).promo == 0


def test_many_links_score_nothing():
    """60.6% of candidates against 69.3% of delivery bodies — delivery mail carries more links."""
    body = "<p>hello</p>" + "".join(f'<a href="https://x.test/{n}">link</a>' for n in range(20))
    assert classify(Message(subject="Update", sender="a@b.test", body_html=body)).promo == 0


def test_a_tracking_pixel_scores_nothing():
    """57.6% against 38.8%. Too common on both sides to carry weight."""
    body = '<p>hello</p><img src="https://x.test/p.gif" width="1" height="1">'
    assert classify(Message(subject="Update", sender="a@b.test", body_html=body)).promo == 0


# --- the lists are data, and stay readable -------------------------------------------------------

def test_every_term_carries_a_reason_a_person_can_read():
    """A row on the queue explains itself with these strings, so a bare token is not acceptable."""
    for signal in promotional.PROMOTIONAL + promotional.BUSINESS:
        assert signal.weight > 0
        assert len(signal.why.split()) >= 3, signal.why
        assert not signal.why.endswith("."), signal.why


def test_the_verdict_reports_what_fired_heaviest_first():
    verdict = classify(ad(subject="RE: 70% off clearance", has_po=True))
    assert verdict.matched[0] == "it names a purchase order"
    assert "a discount figure in the subject" in verdict.matched


def test_an_empty_message_is_neither():
    assert classify(Message()).band == NEITHER


def test_real_work_carrying_one_stray_promotional_word_is_not_badged():
    """The MAYBE band means "nobody could tell", not "a business email said sale". Measured on the
    live store: gating only on the promotional score badged replies like
    `RE: [External] Marriott Sugarland - Hand Tufted`, which is plainly work. A badge that fires on
    real work is a badge people learn to ignore."""
    verdict = classify(Message(subject="RE: [External] Marriott Sugarland - carpet sale order",
                               sender="megan@carpets.test"))
    assert verdict.promo >= MAYBE_AT
    assert verdict.business > 0
    assert verdict.band == NEITHER


def test_a_vetoed_advertisement_is_still_badged():
    """The other way in: it cleared the advertising bar and a business signal stopped it. That is
    real ambiguity and it is exactly what a person should be shown."""
    verdict = classify(ad(subject="70% off clearance for Sheraton Anchorage"))
    assert verdict.promo >= promotional.ADVERTISING_AT
    assert verdict.business > 0
    assert verdict.band == MAYBE
