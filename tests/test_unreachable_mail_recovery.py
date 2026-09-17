"""Twenty-four messages the pipeline could never read, and the page that called it healthy.

`stage1_ingest.listing_window_start()` narrows the Graph listing to mail newer than the last clean
run, minus an overlap. That is a cost optimisation, and it became silent data loss: a message the
pipeline never settled, whose `receivedDateTime` has since fallen behind the window, cannot appear
in a listing again however many times the run repeats.

Measured on Premier's live store on 2026-09-03 — watermark `12:04:44Z`, window opening
`11:04:44Z`, and **24 unread arrivals reaching back to 2026-08-31**, one of them
`URGENT Re: Dorado Beach PO 214336 COM's to Mittman`. Every one was already in `mail_arrivals`
with no `email_log` row, and the Mail page was already printing the count. Nothing acted on it, and
the automation page called those runs healthy: *"everything in the inbox has already been through
the pipeline."*

Widening the window cannot fix it — the miss is by id, so the recovery is by id.
"""

import sqlite3

import pytest

from config import settings
from connectors.mailbox import Mailbox, RecoveredMail
from pipeline import mail_arrivals, stage1_ingest, state_db
from pipeline.models import RawEmail


@pytest.fixture
def conn(tmp_path):
    connection = state_db.get_connection(tmp_path / "state.sqlite3")
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


class _Mailbox(Mailbox):
    """A mailbox whose listing honours a window, so the gap is reproducible rather than asserted."""

    def __init__(self, listed=(), holds=(), fails=()):
        self._listed = list(listed)
        self._holds = {e.email_id: e for e in holds}
        self.fails = set(fails)
        self.asked_for = []

    def fetch_new(self, skip_ids=None, since=None):
        return list(self._listed)

    def fetch_by_ids(self, email_ids):
        self.asked_for.append(list(email_ids))
        found = RecoveredMail()
        for i in email_ids:
            if i in self._holds:
                found.emails.append(self._holds[i])
            elif i in self.fails:
                continue          # request failed: neither found nor proved absent
            else:
                found.absent.append(i)
        return found

    def mark_processed(self, email_id, folder):
        raise AssertionError("read-only: nothing may be moved or marked")


def _email(email_id: str, received: str) -> RawEmail:
    return RawEmail(email_id=email_id, received_at=received, sender_address="v@example-tile.test",
                    sender_domain="example-tile.test", subject="Delivery", body_html="",
                    body_text="", attachments=[])


def _arrived(conn, email_id: str, received: str) -> None:
    conn.execute("INSERT INTO mail_arrivals (email_id, received_at, first_seen_at) VALUES (?,?,?)",
                 (email_id, received, "2026-09-03T12:00:00Z"))
    conn.commit()


# --- the gap, and closing it ----------------------------------------------

def test_mail_behind_the_window_is_fetched_by_id(conn):
    """The regression. The listing returns nothing; the message is recovered anyway."""
    old = _email("<dorado@example-tile.test>", "2026-09-02T14:06:53Z")
    _arrived(conn, old.email_id, "2026-09-02T14:06:53Z")
    box = _Mailbox(listed=[], holds=[old])

    fresh = stage1_ingest.fetch_new_emails(box, conn=conn)

    assert [e.email_id for e in fresh] == [old.email_id], (
        "a message the window has passed must still be reachable by id, or it is lost for good")


def test_nothing_is_fetched_twice(conn):
    """A message the listing already returned must not also be fetched by id — that is a wasted
    Graph request and a second full attachment download."""
    both = _email("<both@example-tile.test>", "2026-09-03T12:30:00Z")
    _arrived(conn, both.email_id, "2026-09-03T12:30:00Z")
    box = _Mailbox(listed=[both], holds=[both])

    fresh = stage1_ingest.fetch_new_emails(box, conn=conn)

    assert [e.email_id for e in fresh] == [both.email_id]
    assert box.asked_for in ([], [[]]), f"asked to re-fetch what it already had: {box.asked_for}"


def test_already_settled_mail_is_never_recovered(conn):
    """`seen_message_ids` still governs re-processing; recovery must not reopen settled mail."""
    settled = _email("<done@example-tile.test>", "2026-09-01T09:00:00Z")
    _arrived(conn, settled.email_id, "2026-09-01T09:00:00Z")
    state_db.mark_seen(conn, settled.email_id, "2026-09-01T10:00:00Z")
    box = _Mailbox(listed=[], holds=[settled])

    assert stage1_ingest.fetch_new_emails(box, conn=conn) == []


def test_recovery_is_capped_and_takes_the_oldest_first(conn, monkeypatch):
    """One Graph request per id, so an unbounded list is a request storm. Oldest first, because
    that is the message in most danger of never being read at all."""
    monkeypatch.setattr(settings, "RECOVER_PER_RUN", 2)
    for day in ("01", "02", "03"):
        _arrived(conn, f"<m{day}@example-tile.test>", f"2026-09-{day}T08:00:00Z")
    box = _Mailbox(listed=[], holds=[])

    stage1_ingest.fetch_new_emails(box, conn=conn)

    assert box.asked_for == [["<m01@example-tile.test>", "<m02@example-tile.test>"]]


def test_a_message_gone_from_the_mailbox_stops_being_asked_for(conn):
    """Otherwise it is re-fetched every run for ever — and, worse, the "arrived but never read"
    count can never reach zero, leaving the run-health warning permanently lit."""
    _arrived(conn, "<deleted@example-tile.test>", "2026-09-01T08:00:00Z")
    box = _Mailbox(listed=[], holds=[])          # asked for, never returned

    stage1_ingest.fetch_new_emails(box, conn=conn)
    assert box.asked_for == [["<deleted@example-tile.test>"]]

    assert mail_arrivals.recoverable_count(conn) == 0, "a vanished message must leave the work list"
    box.asked_for.clear()
    stage1_ingest.fetch_new_emails(box, conn=conn)
    assert box.asked_for in ([], [[]]), "it must not be asked for again on the next run"

    assert mail_arrivals.pending_count(conn) == 1, (
        "the Mail page must still show it — a message may not vanish from the screen silently")


def test_a_local_folder_mailbox_needs_no_recovery(conn):
    """The default returns nothing, so a connector with no window is not forced to implement a
    method it cannot need."""
    from connectors.mailbox import LocalFolderMailbox

    result = Mailbox.fetch_by_ids(LocalFolderMailbox.__new__(LocalFolderMailbox), ["<x@y.test>"])
    assert (result.emails, result.absent) == ([], []), (
        "the default must claim nothing found AND nothing proved absent")


def test_a_broken_recovery_never_fails_the_run(conn, monkeypatch):
    """Recovery is a repair, not the job. If it cannot list, the run still reads its mail."""
    listed = _email("<normal@example-tile.test>", "2026-09-03T12:30:00Z")
    monkeypatch.setattr(mail_arrivals, "unreachable",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("boom")))

    fresh = stage1_ingest.fetch_new_emails(_Mailbox(listed=[listed]), conn=conn)

    assert [e.email_id for e in fresh] == [listed.email_id]


# --- the two counts are different questions -------------------------------

def test_the_health_count_excludes_what_can_never_be_read(conn):
    """`pending_count` is what the Mail page asks; `recoverable_count` is what run health asks.

    Conflated, a deleted message would light the warning for ever, and a warning that cannot clear
    is one people stop reading.
    """
    _arrived(conn, "<gone@example-tile.test>", "2026-09-01T08:00:00Z")
    _arrived(conn, "<here@example-tile.test>", "2026-09-02T08:00:00Z")

    assert (mail_arrivals.pending_count(conn), mail_arrivals.recoverable_count(conn)) == (2, 2)

    mail_arrivals.mark_missing(conn, ["<gone@example-tile.test>"], "2026-09-03T12:00:00Z")

    assert mail_arrivals.pending_count(conn) == 2
    assert mail_arrivals.recoverable_count(conn) == 1


# --- one timestamp shape, so comparisons mean something -------------------

def test_arrival_instants_are_normalised_on_write(conn):
    """The column had two shapes and string comparison between them inverts.

    1,606 rows were space-separated to minute precision and 11 were ISO with a Z. A space sorts
    before a `T`, so on the same day the space form always compared as earlier regardless of the
    actual time — and every query that orders or filters this column assumes it sorts.
    """
    mixed = [
        ("2026-07-13 13:40", "2026-07-13T13:40:00Z"),
        ("2026-08-28T15:47:18Z", "2026-08-28T15:47:18Z"),
        ("2026-09-03 12:08:58", "2026-09-03T12:08:58Z"),
    ]
    for given, expected in mixed:
        assert mail_arrivals.normalise_instant(given) == expected, given


def test_the_comparison_no_longer_inverts():
    """The specific bug, as an assertion. It skewed a real measurement on this column."""
    later = mail_arrivals.normalise_instant("2026-09-03 12:08")
    earlier = mail_arrivals.normalise_instant("2026-09-03T11:04:44Z")

    assert "2026-09-03 12:08" < "2026-09-03T11:04:44Z", "the raw inversion this fixes"
    assert not later < earlier, "12:08 must not compare as earlier than 11:04 once normalised"


def test_an_unparseable_instant_is_kept_rather_than_invented(conn):
    """A timestamp we cannot read is still evidence of when something arrived. Defaulting it to
    now, or to empty, would move a message in the ordering the recovery walks."""
    assert mail_arrivals.normalise_instant("last Tuesday") == "last Tuesday"
    assert mail_arrivals.normalise_instant("") == ""
    assert mail_arrivals.normalise_instant(None) == ""


def test_a_failed_request_is_never_recorded_as_gone(conn):
    """The distinction the connector exists to make, and the one that could lose a PO email.

    An expired token, a throttle or a dropped connection all mean "we did not get it" — and if that
    is recorded as "gone from the mailbox", the message is suppressed for ever while sitting in the
    Inbox. Only a server answering that it does not have the message may end the search.
    """
    _arrived(conn, "<flaky@example-tile.test>", "2026-09-01T08:00:00Z")
    box = _Mailbox(listed=[], holds=[], fails=["<flaky@example-tile.test>"])

    stage1_ingest.fetch_new_emails(box, conn=conn)

    assert mail_arrivals.recoverable_count(conn) == 1, (
        "a request that failed must leave the message on the work list, not bury it")

    box.asked_for.clear()
    stage1_ingest.fetch_new_emails(box, conn=conn)
    assert box.asked_for == [["<flaky@example-tile.test>"]], "it must be asked for again"
