"""Stage 1 — Triage. First matching rule wins; every email leaves with a category and a reason.

Rewritten against the real June corpus. The previous version keyed on `sender_domain` alone,
which the corpus defeats three separate ways:

* Every sample arrives forwarded from `example-pm.test`, so the domain never identifies the
  originator — that has to be recovered from the quoted chain first.
* The receiver trigger (`warehousing@`) and the not-a-receiver notice (`routing@`) share a
  domain, so only the local part separates them.
* The weekly Purchase Order Status Report arrives from `warehousing@` too, so only the
  *subject* separates it from the trigger. A domain rule routes a summary report straight into
  the receiver path.

Hence the rule key is the pair **(sender local part, subject grammar)**, resolved on the origin
hop rather than the delivered envelope.

Categories: HIDE discards, SURFACE fires a delivery event, HOLD waits for a partner notice or
the grace sweep, ROUTE hands it to a person.
"""

import re
from typing import List, Optional, Tuple

from config import settings
from pipeline import authorship
from pipeline.models import NotificationType, RawEmail, TriageCategory, TriagedEmail
from pipeline.parsing import (boilerplate, intent, promotional, sniff, tables, text, thread,
                              tokens)
from pipeline.vendors import authority

# Class E: reads exactly like delivery mail but must never produce a receiver. From the corpus:
# "RES-100b-EQ for PO #908453 was lost by the warehouse... The new PO for the replacement is
# PO #911400" (finding G8).
LOSS_OR_CLAIM_RE = re.compile(
    r"\b(?:lost\s+by|was\s+lost|item\s+lost|missing\s+item|damaged\s+in\s+transit|"
    r"claim\s+(?:filed|number|#)|credit\s+memo|replacement\s+PO|short\s+ship(?:ped|ment)|"
    r"freight\s+claim|concealed\s+damage)\b",
    re.IGNORECASE,
)

CANCELLATION_RE = re.compile(settings.CANCELLATION_KEYWORDS_REGEX, re.IGNORECASE)

# Header signatures of the request/tracker tables Premier sends to properties and vendors. Their
# presence is what makes a thread a confirmation thread — and, crucially, the tables carry the
# PO/spec/qty that the human's one-line reply does not.
CONFIRMATION_TABLE_HEADERS = ["Description of Item", "SPEC # or Phase Code", "UOM", "Qty"]
TRACKER_TABLE_HEADERS = ["Vendor", "PO#", "Spec#", "QTY", "Item Description"]


def _thread_of(email: RawEmail):
    body = text.body_text_of(email)
    parsed = thread.split_thread(body, email.sender_address, email.subject)
    origin = thread.resolve_origin(email.sender_address, email.subject, parsed)
    return body, parsed, origin


def _po_hints_across_thread(email: RawEmail, body: str) -> List[str]:
    """Labelled POs from the subject and *every* hop, plus any PO column in a request table.

    Reading the whole thread is not optional: a property reply is often *"Yes ma'am, this was
    received!"* with the PO living in a table quoted three hops down (finding G4). Scanning only
    the newest hop finds nothing and the email falls through to the unknown rule.
    """
    hints: List[str] = []

    def _add(candidate) -> None:
        """One shape check on the way in, because everything in this list becomes an accumulation
        key. `find_po_numbers` and `parse_po_list` are already 6-digit-only, so this changes
        nothing for them — it is here so *every* path into the list is covered, including the
        attachment-derived POs added by the caller."""
        if tokens.is_po_number(candidate) and candidate not in hints:
            hints.append(candidate)

    for candidate in tokens.find_po_numbers(f"{email.subject}\n{body}"):
        _add(candidate)

    all_tables = tables.extract_tables(email.body_html)
    for header_set in (CONFIRMATION_TABLE_HEADERS, TRACKER_TABLE_HEADERS):
        for grid in tables.find_all_grids(all_tables, header_set):
            po_column = tables.column_index(grid.header, ["P.O.#", "PO#", "PO #", "Purchase Order"])
            if po_column is None:
                continue
            for row in grid.body_rows:
                for candidate in tokens.parse_po_list(tables.cell(row, po_column)):
                    _add(candidate)
    return hints


def _has_confirmation_table(email: RawEmail) -> bool:
    all_tables = tables.extract_tables(email.body_html)
    return bool(tables.find_all_grids(all_tables, CONFIRMATION_TABLE_HEADERS)
                or tables.find_all_grids(all_tables, TRACKER_TABLE_HEADERS))


def _asking_phrases(verdict) -> list:
    """The words the verification verdict rests on, for the reason string a person reads.

    Same principle as the tracker-attachment rule below: an operator working the queue should see
    the sentence that decided this, not the name of the rule that decided it.
    """
    for hop in verdict.hops:
        if hop.result.intent is verdict.overall:
            return [f'"{phrase}"' for phrase in hop.result.matched[:2]]
    return []


def _newest_hop_word_count(parsed) -> int:
    """Words in the newest hop only, boilerplate removed.

    Both halves matter. Measuring the whole thread makes every reply long; leaving the
    signature and the six repeated confidentiality notices in makes a four-word reply measure
    in the hundreds.
    """
    newest = parsed.newest
    return boilerplate.significant_word_count(newest.body) if newest else 0


INTERNALLY_AUTHORED = "rule_5g_internally_authored"
"""Premier wrote this message, so it cannot be evidence that Premier's own goods arrived.

**Not a rung on the ladder, and it could not be one.** Internally-authored mail is recognised at
twelve different rungs — 352 at `rule_4_property_reply`, 317 at `rule_2a_verification_request`,
124 at `rule_7_unknown`, 47 at `rule_5a_tracker_attachment`. A new rung placed early enough to
catch those steals the cancellations, loss-and-claim threads and parsed Authority notices sitting
above it; placed late enough to leave those alone it never sees the 669 messages that are the whole
problem. So it is applied in `result()` instead, which every `return` in the ladder already funnels
through — no rung can bypass it and no rung added later can forget it.

Measured before it existed: 846 of the 4,155 rows on the manual queue were messages Premier wrote
itself, standing for 3,427 records, and not one of them said goods arrived. Across the entire store,
**no internally-authored message has ever carried an attachment recognised as a proof of delivery,
and none has ever produced a receipt posted to Spitfire.** That is what makes this safe rather than
merely convenient.
"""

AUTHORSHIP_YIELDS_TO = frozenset({
    # A cancelled order and a lost or damaged shipment are work whoever wrote them — a PO needs
    # updating in Spitfire either way. `NOT_A_DELIVERY_RULES` excludes them for the same reason.
    "rule_0a_order_cancellation",
    "rule_0b_loss_or_claim",
    # A delivery parsed out of an Authority notice is proved by the carrier. Authorship has nothing
    # to say against it.
    "rule_1a_authority_inbound",
    "rule_1b_authority_delivered",
    # Photographed evidence nobody has read yet. OCR found nothing or was not configured, so hiding
    # it would be deciding on no information — the one thing this override must not do.
    "rule_5b_image_only_evidence",
})
"""Rules whose verdict outranks who wrote the message.

Each is here because hiding the mail it identifies would cost something real, and the cost is not
symmetrical: internal chatter left on the queue is noise, a cancellation or an unread proof filed
under "no action needed" is work nobody is told about.

**Every rule that already says "not a delivery" also yields** — see `_authorship_yields_to`. Those
rules have *identified* the mail, and this override would only make the row less specific: a
scheduled-report mailbox (`rule_0c_report_sender`) and an all-associates broadcast
(`rule_5c_internal_noise`) are both internally authored and both already hidden, so relabelling them
would throw away the more useful sentence and change nothing else.
"""


def _authorship_yields_to(matched_rule: str) -> bool:
    """Whether this rule's verdict survives the authorship override.

    Two reasons to survive, and they are different in kind: the rule identifies real work
    (`AUTHORSHIP_YIELDS_TO`), or the rule already reached the same conclusion by a more specific
    route (`NOT_A_DELIVERY_RULES`). Folding the second in rather than restating its members is what
    keeps a future entry in that set from silently losing its own name.

    `AUTHORSHIP_OUTRANKS` is the exception to the second: both verdicts hide the mail, so nothing
    about the outcome turns on it, and "Premier wrote this" explains the message better than "it
    names no purchase order" does.
    """
    if matched_rule in AUTHORSHIP_OUTRANKS:
        return False
    return matched_rule in AUTHORSHIP_YIELDS_TO or matched_rule in NOT_A_DELIVERY_RULES


AUTHORSHIP_OUTRANKS = frozenset({"rule_2a_verification_no_reference"})
"""Not-a-delivery rules that still give way to the authorship override, for the name alone."""

NOT_A_DELIVERY_RULES = frozenset({
    "rule_0c_report_sender",
    # A question naming no purchase order and carrying no attachment. It *identifies* the mail — it
    # is an enquiry — rather than saying "we could not tell", which is what keeps it in this set and
    # `rule_7_unknown` out. Its sibling `rule_2a_verification_request`, where a PO or a document is
    # present, stays routed: that is a question about a real order, and it is work.
    "rule_2a_verification_no_reference",
    "rule_5d_no_delivery_claim",
    "rule_1c_authority_status_report",
    "rule_2_freight_status",
    "rule_5c_internal_noise",
    "rule_5e_bulk_mail_noise",
    INTERNALLY_AUTHORED,
})
"""Rules whose verdict means *this is positively not a delivery notification*.

The flag on `TriagedEmail` is derived from this set rather than passed at each call site, so a rule
cannot claim it in one branch and forget it in another. What unites them is not the category — all
of them are HIDE and all of them could have been — it is that each one has *identified* the mail, as
against `rule_7_unknown`, which means only "we could not tell". `rule_5e_bulk_mail_noise` identifies
it as commercial bulk mail carrying the opt-out block such mail is obliged to provide.

`rule_0a_order_cancellation` and `rule_0b_loss_or_claim` are deliberately absent. They are not
deliveries either, but both are work: a PO needs updating in Spitfire. Filing them under a heading
that reads "no action needed" would bury exactly the mail somebody has to act on.
"""

_NOT_A_DELIVERY_INTENTS = {
    intent.Intent.SCHEDULED: "states a delivery still to come, not one that happened",
    intent.Intent.NEGATIVE: "states the goods were not received",
    intent.Intent.NON_GOODS: "confirms something other than goods — a payment, a document",
}
"""Thread verdicts that are a *positive statement* this is not a receipt.

**`Intent.NEITHER` is deliberately not here, and that was measured rather than assumed.** It is
absence of evidence, not evidence of absence: it means no hop said anything either way. Including it
hid four kinds of mail that must never be hidden — a photographed POD whose covering note is
wordless, a tracker attachment carrying the only PO, an internal mail naming a purchase order, and
the whole `rule_7_unknown` bucket, which exists precisely to say *we could not tell* and hand the
mail to a person. `tests/test_triage.py::test_internal_mail_naming_a_po_is_never_hidden` is the
guard that caught it.

The three that remain are assertions somebody made: the goods are coming, the goods did not arrive,
or what was confirmed was a payment rather than a shipment. Each is safe to file away because the
message itself says so.

The scheduled report that prompted this work is caught a rung earlier by `rule_0c_report_sender`,
on the sender — which is why this rule does not need to reach for `NEITHER` to earn its place.
"""

_DELIVERY_THREAD_RE = re.compile(
    r"\b(deliver(?:y|ed|ies)|receipt|receiv(?:e|ed|ing)|confirm(?:ation)?|BOL|packing\s+slip|POD|pallet|shipment)\b",
    re.IGNORECASE,
)

_READABLE_KINDS = {sniff.KIND_XLSX, sniff.KIND_DOCX, sniff.KIND_PDF, sniff.KIND_HTML, sniff.KIND_MSG}


def _attachment_kinds(email: RawEmail) -> Tuple[set, int]:
    """(machine-readable kinds present, count of photo-sized images).

    Decorative attachments are already dropped at ingest, so anything still here is either real
    evidence or something no adapter will claim — either way triage should know about it.
    """
    readable, image_count = set(), 0
    for attachment in email.attachments:
        result = sniff.sniff(attachment.content_bytes, attachment.filename, attachment.content_type or "")
        if result.kind in _READABLE_KINDS:
            readable.add(result.kind)
        elif result.is_image:
            image_count += 1
    return readable, image_count


def _is_report_sender(sender_address: Optional[str]) -> bool:
    """A mailbox that only ever sends scheduled reports — see settings.REPORT_SENDER_ADDRESSES."""
    address = (sender_address or "").strip().lower()
    return bool(address) and address in {a.lower() for a in settings.REPORT_SENDER_ADDRESSES}


def _is_internal(sender_address: Optional[str]) -> bool:
    """Premier's own staff. Resolved on the origin hop, because every message in the mailbox is
    forwarded from `example-pm.test` and the envelope sender says so of all of them.

    Delegates to `authorship` so this and the read views cannot drift: the same judgement now
    decides how a message is triaged *and* whether the records read out of it are evidence of a
    delivery or a request for one.
    """
    return authorship.authored_internally(sender_address)


def _looks_like_delivery_thread(subject: str, body: str) -> bool:
    """Guard on the rules that act on attachments alone, so an unrelated thread that happens to
    carry a spreadsheet is not pulled into the receiving pipeline.

    Delegates to `intent.is_delivery_topic` so this vocabulary has exactly one definition. It was
    a second keyword list living here, and the two answered subtly different questions — "is this
    thread about a delivery" and "did somebody say the goods arrived" — while sharing a name.
    """
    return intent.is_delivery_topic(subject + "\n" + body[:4000])


def _bulk_opt_out_phrase(email: RawEmail, body: str) -> Optional[str]:
    """The opt-out phrase this message carries, or None.

    Delegates to `promotional.opt_out_phrase` so the pattern has exactly one definition. It lives
    there because the lexicon scores it as one signal among several; this wrapper stays because the
    reporting tools reach for it by name, and because the fallback to the rendered body for
    plain-text mail is a triage concern rather than a lexicon one.
    """
    return promotional.opt_out_phrase(email.body_html or body or "")


def _and_list(reasons) -> str:
    """`a, b and c` — the matched reasons in one clause, for a sentence a person reads.

    Capped at three. The lexicon can fire eight terms on a loud enough subject, and a reason string
    that lists all of them stops being a sentence and starts being a dump.
    """
    kept = list(reasons)[:3]
    if not kept:
        return "nothing in particular"
    if len(kept) == 1:
        return kept[0]
    return ", ".join(kept[:-1]) + " and " + kept[-1]


def _decide_authority(
    email: RawEmail, notice: authority.AuthorityNotice
) -> Tuple[NotificationType, TriageCategory, str, str]:
    if notice.kind == authority.NoticeKind.STATUS_REPORT:
        return (NotificationType.WAREHOUSE_STATUS_REPORT, TriageCategory.HIDE,
                "rule_1c_authority_status_report",
                "periodic PO status summary from the same sender as the receiver trigger — not a delivery event")

    if notice.kind == authority.NoticeKind.INBOUND:
        return (NotificationType.WAREHOUSE_INBOUND, TriageCategory.SURFACE,
                "rule_1a_authority_inbound",
                "warehouse booked the goods in — the receiver trigger")

    # Class B. Held rather than surfaced: the carrier delivering to the warehouse door is not a
    # receiving event, and the Inbound that follows is. Holding (instead of hiding) means a
    # shipment that only ever produces a Delivered notice still reaches a human via the grace
    # sweep instead of vanishing — the corpus contains exactly that case, annotated
    # "straightforward, WH rec'd", and Premier owes us a written rule on it.
    return (NotificationType.DELIVERED_SHIPPED, TriageCategory.HOLD,
            "rule_1b_authority_delivered",
            "carrier delivered to the warehouse; waiting on the matching Inbound notification")


def triage(email: RawEmail, evidence=None) -> TriagedEmail:
    """Decide what this email is.

    `evidence` is an `EmailEvidence` bundle from `pipeline/evidence.py` — what the attachments
    actually contained, parsed before this ran. It is optional so every existing caller and test
    keeps working, but the orchestrator always supplies it, and it is what turns two of the
    corpus's rules from guesses into readings: a thread whose POs exist only inside an attached
    spreadsheet now has those POs here, and a photographed-evidence email is only routed for OCR
    after OCR has genuinely been tried or declined.
    """
    body, parsed, origin = _thread_of(email)
    origin_subject = origin.subject or email.subject
    po_hints = _po_hints_across_thread(email, body)

    # POs read out of the attachments rank alongside those found in the text — an attachment is
    # not weaker evidence, it is usually the only evidence.
    for candidate in getattr(evidence, "po_numbers", []) or []:
        # Shape-checked like every other route into this list. `evidence.po_numbers` already
        # filters, but this list is what Stage 2 keys accumulations on and it is worth being
        # unable to poison from any direction.
        if tokens.is_po_number(candidate) and candidate not in po_hints:
            po_hints.append(candidate)

    def result(
        notification_type: NotificationType,
        category: TriageCategory,
        matched_rule: str,
        reason: str = "",
        shipment_hint: Optional[str] = None,
        notification_number: Optional[str] = None,
    ) -> TriagedEmail:
        # Authorship has the last word — see `INTERNALLY_AUTHORED` for why this is here and not a
        # rung of its own. Applied to whatever the ladder decided, so the rule that recognised the
        # message is kept in the reason rather than thrown away: a reader needs to know this was a
        # property reply or a verification request *before* authorship overruled it.
        #
        # `origin.sender_address or email.sender_address` is the same expression the row is stamped
        # with two lines down, and the same one `rule_5c_internal_noise` uses. The origin sender is
        # what matters and the envelope is only the fallback: every message in this mailbox is
        # forwarded, so the envelope reads `premierpm.com` on an Atlas receiving report too, and
        # 117 messages in the store are internal by envelope while authored outside. Reading the
        # envelope alone would bury all of them and look like the rule working. `authorship`
        # carries the full reasoning.
        if (not _authorship_yields_to(matched_rule)
                and _is_internal(origin.sender_address or email.sender_address)
                and not getattr(evidence, "has_pod", False)):
            reason = (f"written inside Premier, so it cannot be evidence that Premier's own goods "
                      f"arrived — recognised as {matched_rule} before authorship overruled it"
                      + (f": {reason}" if reason else ""))
            notification_type, category = NotificationType.UNKNOWN, TriageCategory.HIDE
            matched_rule = INTERNALLY_AUTHORED

        return TriagedEmail(
            email=email, notification_type=notification_type, category=category,
            matched_rule=matched_rule, extracted_po_hints=po_hints,
            extracted_shipment_hint=shipment_hint, reason=reason,
            origin_sender_address=origin.sender_address or email.sender_address,
            # No fallback to the envelope date here: None must stay None so the reader downstream
            # can tell "the mail states when it was sent" from "we only know when it arrived".
            # Collapsing them here would make a forward date indistinguishable from a real one.
            origin_sent_at=origin.sent_at,
            notification_number=notification_number,
            # Derived, never passed in — see NOT_A_DELIVERY_RULES.
            not_a_delivery=matched_rule in NOT_A_DELIVERY_RULES,
        )

    # Rule 0c — a mailbox that only ever sends scheduled reports. First, ahead even of the
    # cancellation rule, because report subjects carry its vocabulary as a matter of course
    # (`Cancelled POs was executed at ...`) and `CANCELLATION_RE` would otherwise route a report to
    # a person every morning. Safe to put first only because it is keyed on the *sender*: nothing
    # from a reports mailbox is a cancellation somebody must act on.
    if _is_report_sender(origin.sender_address or email.sender_address):
        return result(NotificationType.WAREHOUSE_STATUS_REPORT, TriageCategory.HIDE,
                      "rule_0c_report_sender",
                      f"scheduled report from {origin.sender_address or email.sender_address} — "
                      f"that mailbox only sends reports, and a report is never a delivery event")

    # Rule 0 — cancellations and loss/claim threads leave first. Both read like delivery mail,
    # and both are out of scope; letting either reach the vendor parsers risks a receiver for
    # goods that were cancelled or never arrived.
    newest_text = boilerplate.strip_boilerplate(parsed.newest.body if parsed.newest else "")
    subject_and_newest = f"{email.subject}\n{newest_text}"

    if CANCELLATION_RE.search(subject_and_newest):
        return result(NotificationType.ORDER_CANCELLATION, TriageCategory.ROUTE,
                      "rule_0a_order_cancellation",
                      "order cancellation notice — needs a manual PO update in Spitfire, not a delivery event")

    if LOSS_OR_CLAIM_RE.search(f"{email.subject}\n{body}"):
        return result(NotificationType.LOSS_OR_CLAIM, TriageCategory.ROUTE,
                      "rule_0b_loss_or_claim",
                      "lost / damaged / claim / replacement-PO thread — out of Phase 1 scope, needs a person")

    # Rule 1 — Authority Logistics, by (local part, subject grammar).
    notice = authority.notice_in_thread(
        origin.sender_address, origin_subject, email.body_html, body, parsed)
    if notice is not None:
        notification_type, category, matched_rule, reason = _decide_authority(email, notice)
        notice_pos = notice.po_numbers
        if notice_pos:
            # The vendor grammar is authoritative over the generic scan — it knows which of the
            # six-digit numbers in this mail are POs and which are the inbound number.
            po_hints = notice_pos
        return result(notification_type, category, matched_rule, reason,
                      shipment_hint=notice.shipment_number,
                      notification_number=notice.notice_number)

    # Rule 2 — carrier status mail (FedEx/UPS "shipped", "out for delivery"). An intermediate
    # movement notice, never a receiving event.
    if origin.sender_domain in settings.FREIGHT_SENDER_DOMAINS:
        return result(NotificationType.DELIVERED_SHIPPED, TriageCategory.HIDE,
                      "rule_2_freight_status",
                      "intermediate carrier status notice, not a receiving event")

    # Rule 2a — the mail is still *asking*. Above rules 3 and 4 because both are reachable by a
    # question: each tests only "does this thread carry a confirmation table or a PO", which is as
    # true of Premier's outbound request as of the answer to it, and rule 4 adds only a word count
    # that a polite enquiry also passes. `Could you please verify whether the fabrics listed below
    # were received for Attic Stock?` was held as a property confirmation and staged three
    # receipts — one for goods the property said in the same thread had never arrived.
    #
    # Resolved across the quoted hops rather than on depth 0, which on a forward is an empty
    # wrapper: `resolve_thread` reports the newest hop that asserted anything at all.
    #
    # Yields to a proof of delivery. This is precedence 0: a carrier proved these goods arrived,
    # and a question in the covering mail cannot un-prove it. The corpus has the exact case —
    # Maria attaches the GR-350a-WTF POD and, in the same breath, asks the property to "kindly
    # double check on your warehouse and confirm receipt when possible". Routing that mail away
    # wholesale would discard a real POD to avoid a false receipt, when the per-PO gate downstream
    # already lets both be true at once: proof for the line that has it, a question for the rest.
    verdict = intent.resolve_thread(parsed)
    if verdict.overall is intent.Intent.VERIFICATION and not getattr(evidence, "has_pod", False):
        quoted = "; ".join(_asking_phrases(verdict)) or "no delivery claim anywhere in the thread"
        # Premier, 2026-09-15: a question with nothing to receive against is not delivery mail at
        # all, and 496 of them were the second-largest thing on the manual queue. What keeps this
        # from hiding real work is the guard below rather than the question itself: a PO named
        # anywhere in the thread (`po_hints` already spans the quoted hops and the attachments), a
        # record already read from it, or an unread attachment that may yet carry one. Only the
        # bare question — no order, no document — is set aside.
        readable, images = _attachment_kinds(email)
        if not (po_hints or getattr(evidence, "has_records", False) or readable or images):
            return result(NotificationType.VERIFICATION_REQUEST, TriageCategory.HIDE,
                          "rule_2a_verification_no_reference",
                          f"asks whether goods were received and names no purchase order and "
                          f"attaches nothing — a question, not delivery mail ({quoted})")
        return result(NotificationType.VERIFICATION_REQUEST, TriageCategory.ROUTE,
                      "rule_2a_verification_request",
                      f"asks whether goods were received — needs a human answer, not a receipt "
                      f"({quoted})")

    # Rule 3 — vendor/outside-warehouse confirmation (Class D). Recognised by the request table
    # Premier sent, not by the reply text, which is free-form and often a partial confirmation
    # ("we have only received the Sheer Fabric").
    if origin.sender_domain in settings.VENDOR_CONFIRMATION_DOMAINS and (po_hints or _has_confirmation_table(email)):
        return result(NotificationType.VENDOR_CONFIRMATION, TriageCategory.HOLD,
                      "rule_3_vendor_confirmation",
                      "outside-warehouse receipt verification — hold for the confirmation to be reconciled")

    # Rule 4 — property confirmation (Class C): a short human reply on a thread that carries a
    # confirmation table or a PO reference.
    if (po_hints or _has_confirmation_table(email)) and _newest_hop_word_count(parsed) <= settings.PROPERTY_REPLY_MAX_WORDS:
        return result(NotificationType.PROPERTY_CONFIRMATION, TriageCategory.HOLD,
                      "rule_4_property_reply",
                      "short human confirmation on a delivery thread — hold pending reconciliation")

    # Rule 5 — the identifying data is in an attachment, not the text. Two real corpus threads
    # carry no PO anywhere in their bodies because the POs live in an attached tracker
    # (`Property Receivers.xlsx`, columns Vendor/PO#/Spec#/QTY/Item Description). Falling through to
    # the unknown rule routes a fully-machine-readable spreadsheet to a human for retyping.
    readable, images = _attachment_kinds(email)
    if readable and _looks_like_delivery_thread(email.subject, body):
        detail = f"({', '.join(sorted(readable))})"
        if getattr(evidence, "has_records", False):
            # Read, not assumed. The reason string carries what was actually found, so the
            # operator sees evidence rather than a heuristic.
            detail = (f"({', '.join(sorted(readable))}) — read {len(evidence.records)} line(s)"
                      f" covering PO(s) {', '.join(evidence.po_numbers) or 'none stated'}")
        return result(NotificationType.PROPERTY_CONFIRMATION, TriageCategory.HOLD,
                      "rule_5a_tracker_attachment",
                      f"delivery thread whose identifying data is in an attachment {detail}")

    # Rule 5c — internal chatter that is not about a delivery at all. Twelve of the fourteen
    # emails in Premier's manual queue were all-associates broadcasts and calendar invites
    # (`Premier Monthly Celebration` five times over, `Premier Monthly Huddle`, `Canceled: Voting
    # Holiday Reward`), each queued as "no PO reference found anywhere in the thread". True, and
    # useless: they buried the two entries that were real work.
    #
    # Placed *above* 5b deliberately. `You've joined the Premier PM All Associates group` carries
    # ten signature logos and was matching the photographed-evidence rule, so it arrived in the
    # queue claiming to be delivery evidence.
    #
    # Every condition must hold, because the cost of the two mistakes is not symmetrical: a
    # broadcast left in the queue is noise, a real delivery hidden is a receiver nobody creates.
    # `Fw: Example Hotel Harbour Delivery` is internal and PO-less too, and stays — it has real photos
    # and delivery vocabulary.
    if (not po_hints
            and not readable and not images
            and _is_internal(origin.sender_address or email.sender_address)
            and not _looks_like_delivery_thread(email.subject, body)):
        return result(NotificationType.UNKNOWN, TriageCategory.HIDE, "rule_5c_internal_noise",
                      "internal mail with no PO, no attachment worth reading and no delivery "
                      "vocabulary anywhere in the thread — an announcement, not a delivery")

    # Rule 5b — photographed PODs and BOLs. Reached only once the evidence pass has already
    # tried to read them, so this says "OCR found nothing", not "we never looked".
    if images and _looks_like_delivery_thread(email.subject, body):
        attempted = getattr(evidence, "ocr_attempted", 0)
        how = (f"OCR read {attempted} of them and recovered no PO"
               if attempted else "no OCR client is configured")
        return result(NotificationType.UNKNOWN, TriageCategory.ROUTE, "rule_5b_image_only_evidence",
                      f"delivery evidence is {images} photographed attachment(s) with no text layer — "
                      f"{how}; needs OCR or a person")

    # Rule 5d — the thread positively states this is not a receipt: the goods are still coming,
    # they did not arrive, or what was confirmed was a payment.
    #
    # **Placed last of the identifying rules, and that position was measured.** It sat above rule 3
    # first, which is where it does the most good and also the most harm: it stole
    # `Del to Property - manual check against BOLs...` — real delivery mail whose proof is
    # photographed and whose covering note reads as a scheduling remark — and the photographed-
    # evidence rule never got a turn. Every rule that reads an *attachment* now runs first, so prose
    # only decides when there is nothing else to go on.
    #
    # Yields to a parsed Authority notice and to a POD for the same reason rule 2a does: a delivery
    # stated in a table, or proved by a carrier, outranks a sentence that says otherwise.
    if (verdict.overall in _NOT_A_DELIVERY_INTENTS
            and notice is None
            and not getattr(evidence, "has_pod", False)):
        return result(NotificationType.UNKNOWN, TriageCategory.HIDE,
                      "rule_5d_no_delivery_claim",
                      _NOT_A_DELIVERY_INTENTS[verdict.overall])

    # Rules 5e and 5f — advertising, and mail that might be.
    #
    # The external twin of 5c: same guards, but qualified by properties of the *message* instead of
    # by `_is_internal`, because a sender list stops working the moment next month's marketing
    # arrives from somewhere else — the argument `tools/needs_a_human_issues_report.py` already
    # makes about this exact bucket. `parsing/promotional` holds the two weighted lists and the
    # measurements behind every weight; this is only where they are applied.
    #
    # **Placed last of the identifying rules, which bounds the change exactly.** Everything above
    # matches first, and rule 6 needs a PO both of these refuse to fire with — so the only mail
    # either can take is mail that would otherwise be `rule_7_unknown`. Nothing else can move.
    #
    # Below 5d on purpose: a footer reading "you are receiving this because you subscribed" already
    # routes there through `intent.NON_GOODS`, and the more specific rule keeps its mail.
    #
    # No `not _is_internal` guard: any internal message satisfying these satisfies 5c's, and 5c runs
    # first. A redundant guard would only invite the reader to think the two could disagree.
    #
    # **The delivery guard is `verdict.overall`, not `_looks_like_delivery_thread` as 5c uses, and
    # that difference is load-bearing.** Measured against the live store: `is_delivery_topic` matches
    # 26 words including delivery, shipment, tracking and receive, and marketing mail is saturated
    # with them — the topic guard spared "CEILING FANS - LABOR DAY DEALS" and five more like it,
    # catching 5 of 33 instead of 11. Mentioning a delivery is not claiming one.
    promo = promotional.classify(promotional.Message(
        subject=email.subject or "", body_html=email.body_html or "",
        sender=origin.sender_address or email.sender_address or "",
        has_po=bool(po_hints), has_attachment=bool(readable or images)))

    if (promo.band == promotional.ADVERTISING
            and not po_hints
            and not readable and not images
            and verdict.overall is not intent.Intent.DELIVERY):
        return result(NotificationType.UNKNOWN, TriageCategory.HIDE, "rule_5e_bulk_mail_noise",
                      f"advertising, not a delivery: {_and_list(promo.matched)}, with no PO, no "
                      f"attachment worth reading and nothing in the thread claiming goods arrived")

    # Rule 5f — promotional in shape, but something about it says otherwise, or it is simply not
    # clear enough. **ROUTE, and deliberately not in `NOT_A_DELIVERY_RULES`:** this is not a claim
    # that the mail is not a delivery, it is a request for a person to look. It exists so that
    # "we are not sure" is a state the queue can show and somebody can clear in two clicks, rather
    # than a message quietly filed under a verdict nobody checked.
    if promo.band == promotional.MAYBE and not po_hints and not readable and not images:
        return result(NotificationType.UNKNOWN, TriageCategory.ROUTE,
                      "rule_5f_possible_advertising",
                      f"this looks like advertising — {_and_list(promo.matched)} — but not clearly "
                      f"enough to file away on its own. Confirm it, or say it is a delivery")

    # Rule 6 — a PO is referenced but nothing above recognised the shape. Better routed to a
    # person than guessed at.
    if po_hints:
        return result(NotificationType.UNKNOWN, TriageCategory.ROUTE, "rule_6_unrecognized_with_po",
                      "PO referenced but the message shape is unrecognized — needs a person")

    return result(NotificationType.UNKNOWN, TriageCategory.ROUTE, "rule_7_unknown",
                  "no PO reference found anywhere in the thread — cannot resolve automatically")
