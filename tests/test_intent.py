"""The delivery/verification lexicon.

Every string in this file is a real line from Premier's mailbox, not an invention. That is the
point of the module under test: it exists because four sentences that all contain "receiv" were
being read as the same thing.
"""

from pipeline.parsing import intent
from pipeline.parsing.intent import Intent


def _verdict(text):
    return intent.classify(text).intent


# --- the four sentences that motivated the module -----------------------------------------------

def test_a_question_is_never_a_receipt():
    """The sentence that staged three false receipts. It contains "were received"."""
    assert _verdict(
        "I hope you're doing well. Could you please verify whether the fabrics listed below "
        "were received for Attic Stock?"
    ) is Intent.VERIFICATION


def test_a_scoped_yes_confirms_only_what_it_names():
    result = intent.classify(
        "We can confirm that we have only received the Sheer Fabric (GR-350c-WTF) attic stock."
    )
    assert result.intent is Intent.DELIVERY
    assert result.scope_specs == ("GR-350c-WTF",)
    assert not result.scope_uncertain


def test_a_future_delivery_is_not_a_delivery():
    assert _verdict(
        "Driver is onsite now getting loaded This will be delivered to jobsite tomorrow."
    ) is Intent.SCHEDULED


def test_a_receipt_of_a_cheque_is_not_a_receipt_of_goods():
    assert _verdict(
        "Update: I am confirming receipt of check 1000781 for $87,663.85 today, thank you!"
    ) is Intent.NON_GOODS


# --- the rest of the precedence -----------------------------------------------------------------

def test_an_explicit_negative_outranks_any_affirmative_beside_it():
    assert _verdict("*Pallet missing for delivery to property") is Intent.NEGATIVE


def test_a_plain_confirmation_is_a_delivery():
    assert _verdict("Delivered: 09/10/2025 Signed by: U ALI") is Intent.DELIVERY
    assert _verdict("Proof of delivery attached.") is Intent.DELIVERY
    assert _verdict("The Singer Ashland delivered and was installed.") is Intent.DELIVERY


def test_internal_chatter_asserts_nothing():
    assert _verdict("Premier Monthly Celebration") is Intent.NEITHER
    assert _verdict("You've joined the Premier PM All Associates group") is Intent.NEITHER


def test_scheduling_mail_is_recognised_without_any_affirmative_vocabulary():
    """"schedule delivery" contains no list-A phrase. Falling through to NEITHER would be safe but
    would lose the fact that this is live delivery mail, which the attachment rules still need."""
    text = "Team, the freight company contacted me today to schedule delivery for June 3, 2026."
    assert _verdict(text) is Intent.SCHEDULED
    assert intent.is_delivery_topic(text)


def test_a_promise_to_confirm_later_is_not_a_confirmation():
    assert _verdict("I will be on-site Friday. I will confirm then.") is Intent.VERIFICATION


def test_an_unresolvable_scope_word_is_flagged_rather_than_widened():
    """"only" with nothing recoverable to scope to must not quietly become a blanket yes."""
    result = intent.classify("I only wanted to say everything was received fine.")
    assert result.intent is Intent.DELIVERY
    assert result.scope_uncertain
    assert not result.is_receipt_evidence


def test_the_matched_phrase_is_reported_so_a_reason_string_can_quote_it():
    result = intent.classify("Kindly double check on your warehouse and confirm receipt when possible!")
    assert result.intent is Intent.VERIFICATION
    assert "Kindly double check" in result.matched


# --- topic is a different question from assertion -----------------------------------------------

def test_topic_and_assertion_are_separate_questions():
    assert intent.is_delivery_topic("schedule delivery for June 3") is True
    assert intent.is_delivery_topic("Premier Monthly Huddle") is False


# --- resolving a whole thread -------------------------------------------------------------------

class _Hop:
    def __init__(self, depth, sender, body):
        self.depth, self.sender_address, self.body = depth, sender, body


class _Thread:
    def __init__(self, hops):
        self.hops = hops


def _attic_stock_thread():
    """The real six-hop ATTIC STOCK conversation, in the order it actually happened.

    Depth 0 is the forwarding wrapper and strips to nothing — which is why reading "the newest
    hop" and reading "the newest hop that said something" are different rules, and only the second
    one works on this mailbox.
    """
    return _Thread([
        _Hop(0, "rahul@consciouscreations.ai", ""),
        _Hop(1, "mariagutierrez@premierpm.com", "Fabrics vendor receipt confirmation"),
        _Hop(2, "mariagutierrez@premierpm.com",
             "Hello Elber,\n\nKindly find attached Main Drapery Fabric GR-350a-WTF POD and "
             "delivery notification for 202 yards (6 yards of overage)\n\nThere's also "
             "884603885067 POD attached which belongs to GR-350d-WTF 78 yards from Daniel "
             "Stuart.\n\nKindly double check on your warehouse and confirm receipt when possible!"),
        _Hop(3, "elber@5starinterior.com",
             "Hi Maria,\n\nHappy Monday!\nWe can confirm that we have only received the Sheer "
             "Fabric (GR-350c-WTF) attic stock."),
        _Hop(4, "mariagutierrez@premierpm.com",
             "Good morning team, I hope you had a great thanksgiving!\n\nFriendly reminder of this."),
        _Hop(5, "mariagutierrez@premierpm.com",
             "Hello 5 Star Team,\n\nI hope you're doing well. Could you please verify whether the "
             "fabrics listed below were received for Attic Stock?"),
    ])


GRID_SPECS = ("GR-350a-WTF", "GR-350c-WTF", "GR-350d-WTF")


def test_an_empty_forwarding_wrapper_does_not_decide_the_thread():
    verdict = intent.resolve_thread(_attic_stock_thread(), known_specs=GRID_SPECS)
    assert [hop.depth for hop in verdict.hops][0] == 1, "depth 0 strips to nothing and must be skipped"


def test_the_thread_gives_three_different_answers_about_three_lines():
    """One email, three POs, three verdicts. Any email-level answer gets at least two wrong."""
    verdict = intent.resolve_thread(_attic_stock_thread(), known_specs=GRID_SPECS)

    # The property confirmed this one by name.
    assert verdict.verdict_for("GR-350c-WTF") is Intent.DELIVERY
    # "only" excludes both of the others. The POD for GR-350a-WTF overrides this downstream —
    # that is the extractor's job, not the reader's, and keeping them apart is deliberate.
    assert verdict.verdict_for("GR-350a-WTF") is Intent.NEGATIVE
    assert verdict.verdict_for("GR-350d-WTF") is Intent.NEGATIVE


def test_the_thread_overall_still_needs_a_person():
    """The newest hop that says anything asks Elber to double-check, so the matter is open."""
    verdict = intent.resolve_thread(_attic_stock_thread(), known_specs=GRID_SPECS)
    assert verdict.overall is Intent.VERIFICATION


def test_a_line_nobody_mentioned_is_never_a_receipt():
    verdict = intent.resolve_thread(_attic_stock_thread(), known_specs=GRID_SPECS)
    assert verdict.verdict_for("LOB-203-PI") is Intent.NEITHER


# --- a bare "received" needs to say what arrived ------------------------------------------------
#
# List A carried "received" unqualified, so any sentence containing the word was a receipt. These
# are all real lines from the mailbox, and every one of them was being read as goods arriving.

def test_a_received_payment_is_not_a_received_delivery():
    assert _verdict("our accounting department just confirmed the check was received "
                    "and processed") is not Intent.DELIVERY


def test_a_received_approval_is_not_a_received_delivery():
    assert _verdict("I have received approval for the following claims") is not Intent.DELIVERY


def test_a_bare_acknowledgement_is_not_a_receipt():
    """"Received thanks." — Atlas acknowledging an email, on a thread about a delivery."""
    assert _verdict("Received thanks.") is not Intent.DELIVERY


def test_paperwork_closed_out_is_not_a_receipt():
    assert _verdict("Warranty Letter and Care Instructions for PO 213257 — "
                    "Yes, received and closed out") is not Intent.DELIVERY


def test_a_received_notice_is_not_a_receipt():
    assert _verdict("I received a notice on the dashboard") is not Intent.DELIVERY


def test_goods_named_beside_the_affirmative_make_it_a_receipt():
    assert _verdict("The 4 chairs were received at the warehouse on 9/2") is Intent.DELIVERY


def test_a_plural_noun_still_supplies_the_context():
    """The list is written singular; "fabrics" has to reach "fabric"."""
    assert _verdict("we received the fabrics listed below") is Intent.DELIVERY


def test_premiers_own_nouns_supply_the_context():
    """A WRR and a COM are goods to Premier and appear on no general furniture list."""
    assert _verdict("Please see attached for a copy of the WRR noting we received the items") \
        is Intent.DELIVERY
    assert _verdict("after we received the remaining COMs, production can start") \
        is Intent.DELIVERY


def test_a_self_sufficient_phrase_needs_no_object():
    """"POD attached" names the document that proves it; nothing more is required."""
    assert _verdict("POD attached for PO 214312") is Intent.DELIVERY
    assert _verdict("please see the attached packing slip 261103") is Intent.DELIVERY
