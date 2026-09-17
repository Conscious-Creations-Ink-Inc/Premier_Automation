"""Tests for `pipeline.mail_overrides` — one person's verdict on one message.

The properties that matter are all about what the table refuses to hold and how it changes: a
verdict must be signed, it must be one of two known words, and flipping it must overwrite rather
than accumulate. Everything the pages do with these rows assumes exactly one current answer per
message.
"""
import pytest

from pipeline import mail_overrides, state_db

AT = "2026-09-07T10:00:00Z"


@pytest.fixture
def conn():
    return state_db.get_connection(":memory:")


def test_a_verdict_comes_back_with_who_gave_it(conn):
    mail_overrides.set_verdict(conn, email_id="mail-1", verdict=mail_overrides.NOT_DELIVERY,
                               decided_by="Ada Lovelace", note="all-associates broadcast", at=AT)

    override = mail_overrides.get(conn, "mail-1")

    assert override.verdict == mail_overrides.NOT_DELIVERY
    assert override.decided_by == "Ada Lovelace"
    assert override.note == "all-associates broadcast"
    assert override.decided_at == AT


def test_an_unknown_message_has_no_verdict(conn):
    assert mail_overrides.get(conn, "mail-nobody-has-seen") is None


def test_an_unsigned_verdict_is_refused(conn):
    """The one thing this table must not hold. The guard lives in the module rather than only in
    the route that calls it, so a second caller cannot quietly skip it."""
    with pytest.raises(ValueError):
        mail_overrides.set_verdict(conn, email_id="mail-1",
                                   verdict=mail_overrides.NOT_DELIVERY, decided_by="   ", at=AT)

    assert mail_overrides.count(conn) == 0


def test_an_unknown_verdict_is_refused(conn):
    with pytest.raises(ValueError):
        mail_overrides.set_verdict(conn, email_id="mail-1", verdict="maybe",
                                   decided_by="Ada Lovelace", at=AT)

    assert mail_overrides.count(conn) == 0


def test_flipping_a_verdict_overwrites_rather_than_accumulating(conn):
    """A reversal is a new decision, and the name on the row has to be whoever decided the thing
    that is true now — not whoever decided the thing that no longer is."""
    mail_overrides.set_verdict(conn, email_id="mail-1", verdict=mail_overrides.NOT_DELIVERY,
                               decided_by="Ada Lovelace", note="looked like chatter", at=AT)
    mail_overrides.set_verdict(conn, email_id="mail-1", verdict=mail_overrides.DELIVERY,
                               decided_by="Grace Hopper", note="the POD is attached",
                               at="2026-09-08T09:00:00Z")

    override = mail_overrides.get(conn, "mail-1")

    assert mail_overrides.count(conn) == 1
    assert override.verdict == mail_overrides.DELIVERY
    assert override.decided_by == "Grace Hopper"
    assert override.note == "the POD is attached"
    assert override.decided_at == "2026-09-08T09:00:00Z"


def test_setting_the_same_verdict_twice_is_harmless(conn):
    """What makes a double-submit safe: the second press is the same row, written again."""
    for _ in range(2):
        mail_overrides.set_verdict(conn, email_id="mail-1", verdict=mail_overrides.NOT_DELIVERY,
                                   decided_by="Ada Lovelace", at=AT)

    assert mail_overrides.count(conn) == 1


def test_ids_with_returns_only_the_verdict_asked_for(conn):
    mail_overrides.set_verdict(conn, email_id="mail-1", verdict=mail_overrides.NOT_DELIVERY,
                               decided_by="Ada Lovelace", at=AT)
    mail_overrides.set_verdict(conn, email_id="mail-2", verdict=mail_overrides.DELIVERY,
                               decided_by="Ada Lovelace", at=AT)

    assert mail_overrides.ids_with(conn, mail_overrides.NOT_DELIVERY) == {"mail-1"}
    assert mail_overrides.ids_with(conn, mail_overrides.DELIVERY) == {"mail-2"}


def test_ids_with_refuses_an_unknown_verdict(conn):
    with pytest.raises(ValueError):
        mail_overrides.ids_with(conn, "maybe")


def test_a_full_message_id_is_stored_whole(conn):
    """No `mail_arrivals.match_key` truncation here — both sides of every comparison this table is
    in came off the same `email_log` row, so shortening would only lose information."""
    long_id = "<" + "a" * 260 + "@example-pm.test>"
    mail_overrides.set_verdict(conn, email_id=long_id, verdict=mail_overrides.DELIVERY,
                               decided_by="Ada Lovelace", at=AT)

    assert mail_overrides.get(conn, long_id).email_id == long_id
