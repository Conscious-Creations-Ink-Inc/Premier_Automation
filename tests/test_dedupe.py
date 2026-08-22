"""One delivery, however many times it is described.

Premier's previous attempt at this ended because one physical delivery became two receivers. Every
test here names the specific way that can still happen and the layer that stops it.
"""

import sqlite3

import pytest

from pipeline import dedupe, email_log, state_db

NOW = "2026-08-21 10:00:00"


@pytest.fixture
def conn():
    return state_db.get_connection(":memory:")


def log(conn, email_id, **overrides):
    fields = dict(subject="239260 - Inbound Notification - 206725, 207665",
                  sender="maria@premierpm.com", category="surface", matched_rule="rule_1a",
                  reason="", folder="Processed", processed_at=NOW)
    fields.update(overrides)
    email_log.record(conn, email_id=email_id, **fields)


# --- 1. the same message, arriving twice --------------------------------------------------------

def test_a_forward_of_a_message_fingerprints_the_same_as_the_message():
    """Everything in this corpus arrives forwarded from premierpm.com, often twice. A fingerprint
    that kept the `Fwd:`/`RE:` markers would call two copies of one notification different, which
    is the opposite of useful."""
    original = dedupe.fingerprint(subject="239260 - Inbound Notification",
                                  origin_sender="warehousing@authoritylogistics.com",
                                  origin_sent_at="2025-09-15 09:12")
    forwarded = dedupe.fingerprint(subject="Fwd: FW: RE: 239260 - Inbound Notification",
                                   origin_sender="warehousing@authoritylogistics.com",
                                   origin_sent_at="2025-09-15 09:12")
    assert original == forwarded


def test_attachment_order_and_case_do_not_change_the_fingerprint():
    """Filenames lie — two byte-identical PODs arrive under different names — so identity is
    content, and the order Graph happens to list them in is not meaningful."""
    one = dedupe.fingerprint(subject="s", origin_sender="A@B.com", origin_sent_at="d",
                             attachment_hashes=["BB", "aa"])
    two = dedupe.fingerprint(subject="s", origin_sender="a@b.com", origin_sent_at="d",
                             attachment_hashes=["aa", "bb"])
    assert one == two


def test_two_genuinely_different_notifications_do_not_collide():
    a = dedupe.fingerprint(subject="239260 - Inbound", origin_sender="w@a.com",
                           origin_sent_at="2025-09-15 09:12")
    b = dedupe.fingerprint(subject="239261 - Inbound", origin_sender="w@a.com",
                           origin_sent_at="2025-09-15 09:12")
    assert a != b


def test_the_same_subject_sent_on_different_days_does_not_collide():
    """Vendors reuse subjects. The originating send time is what separates a September delivery
    from a June one — and it is `origin_sent_at`, not the envelope date, because twelve of the
    fourteen corpus files were forwarded on one day."""
    a = dedupe.fingerprint(subject="Delivered", origin_sender="r@a.com",
                           origin_sent_at="2025-09-15 09:12")
    b = dedupe.fingerprint(subject="Delivered", origin_sender="r@a.com",
                           origin_sent_at="2026-06-05 14:03")
    assert a != b


def test_a_duplicate_points_at_the_earliest_copy(conn):
    """A chain of each copy pointing at the one before it makes "how many copies arrived"
    unanswerable without walking it."""
    for email_id in ("mail-1", "mail-2", "mail-3"):
        log(conn, email_id)
    dedupe.stamp(conn, "mail-1", "fp")
    dedupe.stamp(conn, "mail-2", "fp", "mail-1")

    found = dedupe.find_by_fingerprint(conn, "fp", exclude_email_id="mail-3")
    assert found["email_id"] == "mail-1"


def test_a_message_is_not_its_own_duplicate(conn):
    log(conn, "mail-1")
    dedupe.stamp(conn, "mail-1", "fp")

    assert dedupe.find_by_fingerprint(conn, "fp", exclude_email_id="mail-1") is None


def test_nothing_is_deleted_only_linked(conn):
    """A suppression nobody can inspect is a suppression nobody can trust — the same reason the
    internal-chatter rule lists what it filtered rather than hiding it."""
    log(conn, "mail-1")
    log(conn, "mail-2")
    dedupe.stamp(conn, "mail-1", "fp")
    dedupe.stamp(conn, "mail-2", "fp", "mail-1")

    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT email_id, duplicate_of FROM email_log ORDER BY email_id").fetchall()
    assert [r["email_id"] for r in rows] == ["mail-1", "mail-2"]
    assert rows[1]["duplicate_of"] == "mail-1"


def test_inline_attachments_are_left_out_of_the_hashes(conn):
    """A signature logo repeated across a whole mailbox would push unrelated messages towards the
    same fingerprint."""
    log(conn, "mail-1")
    conn.execute(
        """INSERT INTO attachment_ledger
           (email_id, depth, ordinal, filename, sniffed_kind, disposition, is_inline, sha256,
            size_bytes, first_seen_at)
           VALUES ('mail-1', 0, 0, 'pod.pdf', 'pdf', 'extracted', 0, 'real', 1, 'now')""")
    conn.execute(
        """INSERT INTO attachment_ledger
           (email_id, depth, ordinal, filename, sniffed_kind, disposition, is_inline, sha256,
            size_bytes, first_seen_at)
           VALUES ('mail-1', 0, 1, 'logo.png', 'image', 'dropped_decorative', 1, 'logo', 1, 'now')""")
    conn.commit()

    assert dedupe.hashes_for_email(conn, "mail-1") == ["real"]


# --- 3. the same delivery, staged as two records ------------------------------------------------

def test_two_partial_deliveries_on_one_line_are_different_deliveries():
    """PO 208491 line 300 took 11 pieces on 1 October and 1 more on the 9th. Both are real
    receipts, and a key that merged them would lose the second."""
    first = dedupe.delivery_key(po_number="208491", line_number=300, quantity=11,
                                pod_stated_date="2025-10-01")
    second = dedupe.delivery_key(po_number="208491", line_number=300, quantity=1,
                                 pod_stated_date="2025-10-09")
    assert first != second


def test_the_same_delivery_read_twice_keys_the_same():
    twice = [dedupe.delivery_key(po_number="208491", line_number=300, quantity=11,
                                 pod_stated_date="2025-10-01") for _ in range(2)]
    assert twice[0] == twice[1]


def test_a_line_number_is_used_in_preference_to_a_spec():
    """The line is what Spitfire books against and is exact. Two records agreeing on the line are
    the same delivery whatever description each adapter read off the page."""
    assert (dedupe.delivery_key(po_number="1", line_number=300, spec_code="STE-402-LT-B",
                                quantity=1)
            == dedupe.delivery_key(po_number="1", line_number=300, spec_code="", quantity=1))


def test_the_spec_is_used_when_no_line_was_stated():
    """Most mail states no line number, so a key that needed one would catch nothing on real
    traffic."""
    assert (dedupe.delivery_key(po_number="1", spec_code="STE-402-LT-B", quantity=1)
            != dedupe.delivery_key(po_number="1", spec_code="STE-402-LT-SH", quantity=1))


def test_sub_parts_of_one_spec_are_separate_deliveries():
    """`STE-402-LT-B` (bases) and `STE-402-LT-SH` (shades) are Spitfire lines 300 and 301. Treating
    them as one delivery reports a false quantity conflict."""
    assert (dedupe.delivery_key(po_number="1", spec_code="STE-402-LT-B", quantity=11)
            != dedupe.delivery_key(po_number="1", spec_code="STE-402-LT-SH", quantity=12))


def test_a_field_boundary_cannot_be_faked():
    """`("a|b", "c")` hashing the same as `("a", "b|c")` is a collision waiting for the one delivery
    it matters on — which is why the separator is a character no field can contain."""
    assert (dedupe.delivery_key(po_number="208491", spec_code="X", quantity=1)
            != dedupe.delivery_key(po_number="208491X", spec_code="", quantity=1))


def test_a_staged_record_is_found_by_its_key(conn):
    conn.execute(
        """INSERT INTO extracted_records
           (id, source_email_id, po_number, spec_code, item_description, quantity_received,
            pod_stated_date, email_date, extraction_source, extraction_confidence,
            delivery_key, created_at)
           VALUES (1, 'mail-1', '208491', 'STE-402-LT-B', 'Base', 11.0, '2025-10-01',
                   '2025-10-01', 'test', 1.0, 'the-key', 'now')""")
    conn.commit()

    assert [r["id"] for r in dedupe.find_by_delivery_key(conn, "the-key")] == [1]
    assert dedupe.find_by_delivery_key(conn, "the-key", exclude_id=1) == []
    assert dedupe.find_by_delivery_key(conn, "") == []


# --- 4. the same delivery, posted twice ---------------------------------------------------------

def test_the_ledger_key_is_the_pod_hash_whenever_there_is_one():
    """Ten deliveries are already posted in Premier's live store under a key that *is* the POD's
    MD5. A key that changed shape would orphan every one of them and the guard would let them all
    post again."""
    assert dedupe.evidence_key(pod_md5="ABC123") == "ABC123"
    assert not dedupe.is_body_evidence("ABC123")


def test_a_body_only_delivery_still_gets_a_key():
    """Without this, every POD-less delivery on one PO line would key alike, or to nothing — and
    the guard would be blind for exactly the traffic that has no attachment to check."""
    key = dedupe.evidence_key(body_text="19 EA delivered", email_id="mail-1")

    assert key and dedupe.is_body_evidence(key)


def test_two_body_only_deliveries_from_different_messages_key_differently():
    """Two Inbound notifications on the same PO can carry near-identical bodies; the message id is
    what separates them where the text does not."""
    a = dedupe.evidence_key(body_text="19 EA delivered", email_id="mail-1")
    b = dedupe.evidence_key(body_text="19 EA delivered", email_id="mail-2")
    assert a != b


def test_the_same_message_re_derives_the_same_key():
    """The property a rendered PDF could not have: headless Chrome stamps its own metadata, so the
    same body produces different bytes on every render and the key would move under a row that had
    already posted."""
    twice = [dedupe.evidence_key(body_text="19 EA delivered", email_id="mail-1")
             for _ in range(2)]
    assert twice[0] == twice[1]


def test_a_body_key_can_never_be_mistaken_for_a_pod_hash():
    """They share a column. The prefix is what makes a stored row unambiguous about whether the
    receipt it describes carried proof."""
    key = dedupe.evidence_key(body_text="x", email_id="m")

    assert key.startswith("body:")
    assert len(key) != 32, "must not look like an MD5"


# --- 5. keeping automation off a message a person finished --------------------------------------

def test_a_message_a_person_recorded_is_marked(conn):
    log(conn, "mail-1")
    assert dedupe.is_handled_manually(conn, "mail-1") is False

    conn.execute("UPDATE email_log SET handled_manually = 1 WHERE email_id = 'mail-1'")
    conn.commit()
    assert dedupe.is_handled_manually(conn, "mail-1") is True


def test_the_mark_does_not_spread_to_other_messages(conn):
    """Scoped to the one message, never to its thread or its purchase order: the next mail on the
    same thread may be a genuinely separate delivery, and suppressing that would hide a real
    receipt."""
    log(conn, "mail-1")
    log(conn, "mail-2")
    conn.execute("UPDATE email_log SET handled_manually = 1 WHERE email_id = 'mail-1'")
    conn.commit()

    assert dedupe.is_handled_manually(conn, "mail-2") is False
