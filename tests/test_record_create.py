"""Creating a record by hand, from a message the pipeline could not finish.

The rule under nearly every test here is that **nothing is written unless everything checks out**.
A half-saved record is worse than no record: it reaches the Records page, offers a Post button, and
is refused by a gate whose reason names a field the form should never have accepted blank.
"""

import sqlite3

import pytest

from pipeline import completeness, dedupe, email_log, record_create, state_db

NOW = "2026-08-21 10:00:00"

GOOD = {
    "po_number": "908491",
    "spec_code": "STE-402-LT-B",
    "item_description": "BASE, Floor Lamp 2",
    "quantity_received": "11",
    "pod_stated_date": "2025-10-01",
}


@pytest.fixture
def conn():
    c = state_db.get_connection(":memory:")
    email_log.record(c, email_id="mail-1", subject="Delivered - 908491", sender="a@b.com",
                     category="route", matched_rule="rule_7", reason="nothing extractable",
                     folder="Routed", processed_at=NOW)
    c.execute(
        """INSERT INTO attachment_ledger
           (id, email_id, depth, ordinal, filename, sniffed_kind, disposition, is_inline,
            sha256, size_bytes, first_seen_at, is_pod, pod_po_numbers)
           VALUES (7, 'mail-1', 0, 0, 'signed-bol.jpg', 'image', 'extracted', 0,
                   'abc123', 4, 'now', 0, '')""")
    c.execute(
        """INSERT INTO attachment_ledger
           (id, email_id, depth, ordinal, filename, sniffed_kind, disposition, is_inline,
            sha256, size_bytes, first_seen_at, is_pod, pod_po_numbers)
           VALUES (8, 'mail-1', 0, 1, 'logo.png', 'image', 'dropped_decorative', 1,
                   'def456', 2, 'now', 0, '')""")
    c.commit()
    return c


def create(conn, **overrides):
    kwargs = dict(email_id="mail-1", created_by="M Rivera", values=dict(GOOD),
                  pod_ledger_id=7, now=NOW)
    kwargs.update(overrides)
    return record_create.create(conn, **kwargs)


def rows(conn):
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM extracted_records ORDER BY id").fetchall()


# --- the happy path -----------------------------------------------------------------------------

def test_a_complete_form_creates_one_record(conn):
    result = create(conn)

    assert result.ok, result.message
    (row,) = rows(conn)
    assert row["po_number"] == "908491"
    assert row["quantity_received"] == 11.0
    assert row["source_email_id"] == "mail-1"


def test_the_record_says_a_person_made_it_and_who(conn):
    """Written once and never edited, so an audit six months from now can still say which records
    reached Premier's ERP by hand."""
    create(conn)
    (row,) = rows(conn)

    assert row["origin"] == "manual"
    assert row["created_by"] == "M Rivera"
    assert row["extraction_source"] == "manual"


def test_it_reaches_the_records_page_like_any_other(conn):
    """A manual record is not a second class of thing. `_READY_CLAUSE` wants a PO and a confidence
    above zero, and a person having read the message is the strongest claim there is."""
    from pipeline import read_views

    create(conn)
    assert [r["id"] for r in read_views.records_ready(conn)] == [1]


def test_the_spec_is_decomposed_the_way_the_adapters_decompose_it(conn):
    """`STE-402-LT-B` and `STE-402-LT-SH` are Spitfire lines 300 and 301. A record carrying only the
    full code cannot be matched to either, so a typed spec has to split exactly as a parsed one
    does."""
    create(conn)
    (row,) = rows(conn)

    assert row["parent_spec_code"] == "STE-402-LT"
    assert row["sub_spec_suffix"] == "B"


# --- the hard block -----------------------------------------------------------------------------

@pytest.mark.parametrize("field_name", completeness.REQUIRED)
def test_each_mandatory_field_is_refused_on_its_own(conn, field_name):
    """All five, individually. The form's `required` attributes are courtesy — a browser is not a
    validator and a POST can arrive without one."""
    values = dict(GOOD)
    values[field_name] = ""

    result = create(conn, values=values)

    assert not result.ok
    assert completeness.LABELS[field_name] in result.message


@pytest.mark.parametrize("field_name", completeness.REQUIRED)
def test_a_refused_form_leaves_no_row_behind(conn, field_name):
    """The property that matters more than the message. A partially saved record would reach the
    Records page and offer a Post button."""
    values = dict(GOOD)
    values[field_name] = ""
    create(conn, values=values)

    assert rows(conn) == []


def test_a_quantity_of_zero_is_accepted(conn):
    """An empty shipment, or a line cancelled at the dock, is a real statement — and exactly the
    case a person most needs to record rather than have rejected as "no quantity"."""
    result = create(conn, values=dict(GOOD, quantity_received="0"))

    assert result.ok, result.message
    assert rows(conn)[0]["quantity_received"] == 0.0


def test_a_quantity_that_is_not_a_number_is_refused(conn):
    assert not create(conn, values=dict(GOOD, quantity_received="about a dozen")).ok
    assert rows(conn) == []


def test_a_record_must_say_who_created_it(conn):
    assert not create(conn, created_by="  ").ok
    assert rows(conn) == []


def test_a_record_must_come_from_a_message_that_exists(conn):
    """Always born from a specific email, so `source_email_id` is never null and the evidence, the
    POD chooser and the mail popup all keep working on the row afterwards."""
    assert not create(conn, email_id="mail-never-seen").ok
    assert rows(conn) == []


# --- the proof of delivery ----------------------------------------------------------------------

def test_the_chosen_attachment_is_recorded_against_the_record(conn):
    create(conn)
    (row,) = rows(conn)

    assert row["pod_ledger_id"] == 7
    assert row["pod_source"] == "attachment"
    assert row["pod_waived_by"] is None


def test_an_attachment_on_another_message_is_refused(conn):
    """A ledger id names a row in a table covering every email. Unscoped, a stale id would hang
    another delivery's proof on this receipt."""
    conn.execute(
        """INSERT INTO attachment_ledger
           (id, email_id, depth, ordinal, filename, sniffed_kind, disposition, is_inline,
            sha256, size_bytes, first_seen_at)
           VALUES (99, 'mail-other', 0, 0, 'someone-elses.pdf', 'pdf', 'extracted', 0,
                   'zzz', 1, 'now')""")
    conn.commit()

    assert not create(conn, pod_ledger_id=99).ok
    assert rows(conn) == []


def test_deciding_nothing_about_the_proof_is_refused(conn):
    """"Nobody has looked yet" is not a state a record may be created in — it would reach
    `post_decision` indistinguishable from a delivery somebody had actually examined."""
    result = create(conn, pod_ledger_id=None, waive_pod=False)

    assert not result.ok
    assert "proof of delivery" in result.message
    assert rows(conn) == []


def test_both_at_once_is_refused(conn):
    assert not create(conn, pod_ledger_id=7, waive_pod=True).ok


def test_waiving_records_who_waived_it(conn):
    """The only thing that lets a POD-less record reach Spitfire, so it has to name a person."""
    result = create(conn, pod_ledger_id=None, waive_pod=True)

    assert result.ok, result.message
    (row,) = rows(conn)
    assert row["pod_waived_by"] == "M Rivera"
    assert row["pod_waived_at"] == NOW
    assert row["pod_source"] == "email_body"


def test_waiving_says_plainly_what_it_means(conn):
    """The reviewer is accepting that Premier's ERP will hold a receipt with no proof behind it.
    That has to be in the sentence they read back, not only in the dialog they already closed."""
    result = create(conn, pod_ledger_id=None, waive_pod=True)
    assert "no proof of delivery" in result.message


def test_the_chooser_offers_every_non_inline_file(conn):
    """Whatever its type or disposition. The three cases this exists for — a photographed delivery
    note, a POD naming another PO, a proof that is not a carrier POD — are all files the automatic
    rules skip, so a list filtered to what the machine accepts would help nobody."""
    offered = record_create.choosable_attachments(conn, "mail-1")

    assert [r["filename"] for r in offered] == ["signed-bol.jpg"]


# --- duplicates ---------------------------------------------------------------------------------

def test_a_second_record_for_the_same_delivery_is_refused(conn):
    """Two records for one physical delivery become two receipts, which is the failure that ended
    Premier's previous attempt at this."""
    first = create(conn)
    assert first.ok

    again = create(conn)

    assert not again.ok
    assert again.duplicate_of == [first.record_id]
    assert len(rows(conn)) == 1


def test_the_refusal_names_the_record_to_open_instead(conn):
    """A reviewer told only "duplicate" has nowhere to go. The delivery is described back to them in
    the terms they just typed, so they can see whether it really is the same one."""
    create(conn)
    again = create(conn)

    assert "record #1" in again.message
    assert "908491" in again.message and "STE-402-LT-B" in again.message


def test_two_partial_deliveries_on_one_line_are_not_duplicates(conn):
    """PO 908491 line 300 took 11 pieces on 1 October and 1 more on the 9th. Both are real
    receipts, and a duplicate rule that merged them would lose the second."""
    assert create(conn).ok
    second = create(conn, values=dict(GOOD, quantity_received="1",
                                      pod_stated_date="2025-10-09"))

    assert second.ok, second.message
    assert len(rows(conn)) == 2


def test_a_record_created_here_would_also_be_found_by_the_ingest_guard(conn):
    """The key is computed once, by one function, so a record a person made and a record extraction
    staged are comparable. Two different notions of "the same delivery" would agree on nothing."""
    create(conn)
    (row,) = rows(conn)

    assert row["delivery_key"] == dedupe.key_for_row(row, pod_sha256="abc123")


# --- keeping automation off the same message ----------------------------------------------------

def test_the_message_is_marked_so_extraction_does_not_stage_more(conn):
    create(conn)

    handled = conn.execute(
        "SELECT handled_manually FROM email_log WHERE email_id = 'mail-1'").fetchone()[0]
    assert handled == 1


def test_a_refused_form_does_not_mark_the_message(conn):
    """Suppressing extraction on a message nobody successfully recorded anything from would lose
    the delivery entirely."""
    create(conn, values=dict(GOOD, po_number=""))

    handled = conn.execute(
        "SELECT handled_manually FROM email_log WHERE email_id = 'mail-1'").fetchone()[0]
    assert not handled


def test_the_records_a_person_made_can_be_listed_back(conn):
    create(conn)
    made = record_create.records_from(conn, "mail-1")

    assert [r["id"] for r in made] == [1]
    assert made[0]["created_by"] == "M Rivera"


# --- the form opens on what is already known ----------------------------------------------------

def test_the_form_prefills_from_a_record_already_staged(conn):
    """Never a blank form. Making somebody retype what is already on file is slower and a fresh
    chance to get it wrong."""
    create(conn)
    values = record_create.prefill(conn, "mail-1")

    assert values["po_number"] == "908491"
    assert values["spec_code"] == "STE-402-LT-B"


def test_the_form_prefills_a_po_from_triage_when_nothing_was_staged(conn):
    """The case the form exists for: extraction produced no record at all, but triage still found a
    purchase order in the subject."""
    conn.execute("UPDATE email_log SET po_hints = '908491' WHERE email_id = 'mail-1'")
    conn.commit()

    assert record_create.prefill(conn, "mail-1")["po_number"] == "908491"


def test_several_possible_purchase_orders_are_not_guessed_between(conn):
    """One hint is an answer; several is a choice only the reviewer can make, and pre-filling an
    arbitrary one would look like a finding rather than a guess."""
    conn.execute("UPDATE email_log SET po_hints = '906725,907665' WHERE email_id = 'mail-1'")
    conn.commit()

    assert record_create.prefill(conn, "mail-1")["po_number"] == ""


def test_the_form_prefills_the_delivery_date_from_the_proof(conn):
    """The commonest missing field, arriving from the document that asserts it rather than from
    somebody's memory — the same reasoning as `record_completion`."""
    conn.execute("UPDATE attachment_ledger SET is_pod = 1, pod_delivery_date = '2025-10-01' "
                 "WHERE id = 7")
    conn.commit()

    assert record_create.prefill(conn, "mail-1")["pod_stated_date"] == "2025-10-01"


# --- waiving on a record extraction staged ------------------------------------------------------

def test_an_automatically_staged_record_can_be_accepted_without_a_pod(conn):
    """The other half of the body-only path. Extraction stages these from Authority Inbound
    notifications, `post_decision` refuses them, and nothing else can unblock one."""
    create(conn)
    conn.execute("UPDATE extracted_records SET origin = 'auto', pod_waived_by = NULL, "
                 "pod_ledger_id = NULL WHERE id = 1")
    conn.commit()

    result = record_create.waive_pod(conn, 1, by="M Rivera", now=NOW)

    assert result.ok, result.message
    assert rows(conn)[0]["pod_waived_by"] == "M Rivera"


def test_a_waiver_has_to_name_somebody(conn):
    create(conn)
    assert not record_create.waive_pod(conn, 1, by="").ok


def test_waiving_a_record_that_does_not_exist_says_so(conn):
    assert not record_create.waive_pod(conn, 404, by="M Rivera").ok


def test_waiving_twice_does_not_change_who_gave_it(conn):
    """The first name is the one that accepted the risk."""
    create(conn, pod_ledger_id=None, waive_pod=True)
    record_create.waive_pod(conn, 1, by="Somebody Else")

    assert rows(conn)[0]["pod_waived_by"] == "M Rivera"


# --- Settling the proof for a whole delivery ----------------------------------------------------
#
# One signature covering every POD-blocked line of one delivery. The grain is the point: a delivery
# is received as one receipt, and asking for thirty-two signatures on thirty-two rows of one truck
# produces thirty-two reflex presses, not thirty-two decisions.
#
# The properties that matter are all about what these refuse and what they leave alone.


@pytest.fixture
def delivery(conn):
    """One delivery, three lines, two of them on the message the fixture already set up.

    Built by hand rather than through `create`: these functions are reached from the Records page
    for rows *extraction* staged, and a manually created record has already answered the proof
    question at creation.
    """
    conn.execute("INSERT INTO deliveries (id, po_number, delivery_ref, delivery_rung, "
                 "source_email_id, extraction_source, status, created_at) "
                 "VALUES (5, '908491', 'D-1', 'delivered', 'mail-1', 'pdf', 'pending', ?)", (NOW,))
    for record_id, email_id in ((101, "mail-1"), (102, "mail-1"), (103, "mail-2")):
        conn.execute(
            """INSERT INTO extracted_records
               (id, source_email_id, po_number, spec_code, item_description, quantity_received,
                pod_stated_date, status, delivery_id, email_date, extraction_source,
                extraction_confidence, created_at, updated_at)
               VALUES (?, ?, '908491', 'STE-402-LT-B', 'BASE, Floor Lamp 2', 11,
                       '2025-10-01', 'pending', 5, '2025-10-01', 'pdf', 0.9, ?, ?)""",
            (record_id, email_id, NOW, NOW))
    # A second delivery, so the membership guard has something to exclude.
    conn.execute("INSERT INTO deliveries (id, po_number, delivery_ref, delivery_rung, "
                 "source_email_id, extraction_source, status, created_at) "
                 "VALUES (6, '908491', 'D-2', 'delivered', 'mail-1', 'pdf', 'pending', ?)", (NOW,))
    conn.execute(
        """INSERT INTO extracted_records
           (id, source_email_id, po_number, spec_code, item_description, quantity_received,
            pod_stated_date, status, delivery_id, email_date, extraction_source,
            extraction_confidence, created_at, updated_at)
           VALUES (999, 'mail-1', '908491', 'X', 'somebody else', 1, '2025-10-01', 'pending', 6,
                   '2025-10-01', 'pdf', 0.9, ?, ?)""", (NOW, NOW))
    conn.commit()
    return 5


def stored(conn, record_id):
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM extracted_records WHERE id = ?", (record_id,)).fetchone()


def give_bytes(conn, ordinal=0, content=b"a real document"):
    """Put the chosen attachment's bytes where `has_bytes` looks for them."""
    conn.execute(
        "INSERT INTO mail_attachment (email_id, ordinal, filename, content_type, kind, "
        "size_bytes, content, is_inline) VALUES ('mail-1', ?, 'signed-bol.jpg', 'image/jpeg', "
        "'image', ?, ?, 0)", (ordinal, len(content), content))
    conn.commit()


def test_one_signature_covers_every_line_it_names(conn, delivery):
    result = record_create.waive_delivery_pod(conn, delivery, by="Ada Lovelace",
                                              record_ids=[101, 102], now=NOW)

    assert result.ok and result.changed == [101, 102]
    for record_id in (101, 102):
        assert stored(conn, record_id)["pod_waived_by"] == "Ada Lovelace"
        assert stored(conn, record_id)["pod_waived_at"] == NOW
    assert stored(conn, 103)["pod_waived_by"] is None, "a line it did not name must not move"


def test_an_unsigned_waiver_is_refused_and_writes_nothing(conn, delivery):
    result = record_create.waive_delivery_pod(conn, delivery, by="   ", record_ids=[101, 102],
                                              now=NOW)

    assert not result.ok and "who gave it" in result.message
    assert stored(conn, 101)["pod_waived_by"] is None


def test_a_line_on_another_delivery_cannot_be_waived_from_this_one(conn, delivery):
    """The membership guard. The page recomputes its own list and never round-trips it through the
    browser, but a module that trusted its caller here would be one edit from signing against
    somebody else's delivery."""
    result = record_create.waive_delivery_pod(conn, delivery, by="Ada Lovelace",
                                              record_ids=[101, 999], now=NOW)

    assert result.changed == [101]
    assert stored(conn, 999)["pod_waived_by"] is None


def test_the_first_name_on_a_waiver_is_the_one_that_stands(conn, delivery):
    record_create.waive_delivery_pod(conn, delivery, by="Ada Lovelace", record_ids=[101], now=NOW)
    result = record_create.waive_delivery_pod(conn, delivery, by="Someone Else",
                                              record_ids=[101, 102], now="2026-08-22 10:00:00")

    assert stored(conn, 101)["pod_waived_by"] == "Ada Lovelace", "a reversal is not a second signer"
    assert stored(conn, 102)["pod_waived_by"] == "Someone Else"
    assert result.changed == [102]
    assert any("already accepted by Ada Lovelace" in line for line in result.unchanged)


def test_a_second_identical_submit_changes_nothing(conn, delivery):
    record_create.waive_delivery_pod(conn, delivery, by="Ada Lovelace", record_ids=[101, 102],
                                     now=NOW)
    again = record_create.waive_delivery_pod(conn, delivery, by="Ada Lovelace",
                                             record_ids=[101, 102], now="2026-09-01 10:00:00")

    assert again.ok and again.changed == []
    assert stored(conn, 101)["pod_waived_at"] == NOW, "a double-click must not restamp the decision"


# --- nominating a file instead of waiving -------------------------------------------------------


def test_a_chosen_file_becomes_the_proof_for_its_own_message(conn, delivery):
    give_bytes(conn)
    result = record_create.choose_delivery_pod(conn, delivery, ledger_id=7, email_id="mail-1",
                                               record_ids=[101, 102, 103], now=NOW)

    assert result.ok and result.changed == [101, 102]
    assert stored(conn, 101)["pod_ledger_id"] == 7
    assert stored(conn, 101)["pod_source"] == record_create.POD_ATTACHMENT
    assert stored(conn, 103)["pod_ledger_id"] is None, (
        "a file may only be the proof for records read out of its own mail")


def test_an_attachment_from_another_message_is_refused(conn, delivery):
    """`_chosen_pod` scopes a nomination to the record's own email and silently resolves to nothing
    when it does not match — so an unscoped choice would not hang the wrong proof on the receipt,
    it would quietly become "this record has no POD" three screens later."""
    conn.execute(
        """INSERT INTO attachment_ledger
           (id, email_id, depth, ordinal, filename, sniffed_kind, disposition, is_inline,
            sha256, size_bytes, first_seen_at, is_pod, pod_po_numbers)
           VALUES (12, 'mail-2', 0, 0, 'elsewhere.pdf', 'pdf', 'extracted', 0, 'zzz', 9, 'now',
                   1, '')""")
    conn.commit()

    result = record_create.choose_delivery_pod(conn, delivery, ledger_id=12, email_id="mail-1",
                                               record_ids=[101, 102], now=NOW)

    assert not result.ok and "not on this message" in result.message
    assert stored(conn, 101)["pod_ledger_id"] is None


def test_a_file_whose_bytes_were_never_stored_is_refused(conn, delivery):
    """The chooser draws it with a "bytes not stored" badge; this is the same fact enforced. The
    alternative is accepting a decision that the post path then cannot honour."""
    result = record_create.choose_delivery_pod(conn, delivery, ledger_id=7, email_id="mail-1",
                                               record_ids=[101, 102], now=NOW)

    assert not result.ok and "bytes were never stored" in result.message
    assert stored(conn, 101)["pod_ledger_id"] is None


def test_naming_a_proof_leaves_an_earlier_waiver_standing(conn, delivery):
    """`create` refuses a record that both waives and names a proof, but that is about a record
    being *made*. Here the waiver already happened and somebody's name is on it; erasing it would
    erase the audit trail of a risk that was taken. `post_decision` reports the record as posted
    with a proof either way."""
    give_bytes(conn)
    record_create.waive_delivery_pod(conn, delivery, by="Ada Lovelace", record_ids=[101], now=NOW)
    record_create.choose_delivery_pod(conn, delivery, ledger_id=7, email_id="mail-1",
                                      record_ids=[101], now=NOW)

    row = stored(conn, 101)
    assert row["pod_ledger_id"] == 7
    assert row["pod_waived_by"] == "Ada Lovelace"
