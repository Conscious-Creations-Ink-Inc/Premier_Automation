"""580 ledger rows that were one fact about the subscription.

`HTTP 403: Out of call volume quota` is Azure saying the account has nothing left this period. It
is true of the *account*, not of the document — so the next page cannot succeed either, nor the
six hundredth. Nothing recognised that. Every remaining attachment was uploaded anyway, refused
identically, and filed on its own ledger row as `service_unavailable`.

The live ledger on 2026-09-03 carried **580 of those rows** (plus 147 for our own page budget)
against 902 successful extractions — so roughly 45% of real attachments were never read, and on
screen it looked like hundreds of separately broken documents rather than one expired quota with a
date on it.

Two behaviours pinned here: the first refusal latches, so the run stops paying round trips to learn
the same thing; and the run says so once, in its own words, at the top level.
"""

import pytest

from config import settings
from operations import runner, store
from pipeline.stage3_extract.ocr_adapter import (
    AzureOcrError, OcrBudgetExhausted, OcrQuotaExhausted, _is_retryable, is_quota_exhausted)
from tools.ingest_corpus import BudgetedOcrClient


def _quota_error() -> AzureOcrError:
    return AzureOcrError(403, "403", "Out of call volume quota for FormRecognizer F0", "", None)


class _AlwaysOutOfQuota:
    def __init__(self):
        self.round_trips = 0

    def analyze(self, content_bytes):
        self.round_trips += 1
        raise _quota_error()


# --- telling this failure from every other one ----------------------------

def test_only_a_quota_403_counts_as_quota_exhaustion():
    """403 alone is ambiguous — a wrong key and a firewalled endpoint are also 403, and those are
    worth trying against the next document. The message is what disambiguates."""
    assert is_quota_exhausted(_quota_error()) is True
    assert is_quota_exhausted(AzureOcrError(403, "", "Forbidden by firewall", "", None)) is False
    assert is_quota_exhausted(AzureOcrError(400, "InvalidContent", "corrupt", "", None)) is False
    assert is_quota_exhausted(RuntimeError("something else")) is False


def test_quota_exhaustion_is_never_retried():
    """It is raised without contacting Azure at all, so a retry re-asks a decision already taken
    locally — multiplying the round trips the latch exists to remove."""
    assert _is_retryable(OcrQuotaExhausted("x")) is False
    assert _is_retryable(OcrBudgetExhausted("x")) is False
    assert _is_retryable(AzureOcrError(429, "", "", "", None)) is True, (
        "throttling must still be retried; the guard has to stay narrow")


# --- the latch ------------------------------------------------------------

def test_the_first_refusal_stops_the_rest_being_sent():
    """The regression itself, measured in round trips rather than in outcomes.

    Every one of these pages fails either way. What changed is that only the first one pays for an
    upload to find out.
    """
    inner = _AlwaysOutOfQuota()
    client = BudgetedOcrClient(inner, max_pages=500)

    with pytest.raises(AzureOcrError):
        client.analyze(b"the page that discovers it")

    for _ in range(20):
        with pytest.raises(OcrQuotaExhausted):
            client.analyze(b"and twenty more behind it")

    assert inner.round_trips == 1, (
        f"{inner.round_trips} uploads for one exhausted subscription; the latch is not holding")
    assert client.quota_exhausted is True
    assert client.refused == 20


def test_the_page_that_discovered_it_still_records_azures_own_words():
    """The latch must not swallow the original error. That first ledger row is the evidence of
    what Azure actually said, and `dispatch` writes it from the exception it catches."""
    client = BudgetedOcrClient(_AlwaysOutOfQuota(), max_pages=500)

    with pytest.raises(AzureOcrError) as caught:
        client.analyze(b"first")

    assert "Out of call volume quota" in str(caught.value)


def test_the_budget_and_the_quota_stay_distinct():
    """Our spend ceiling and Azure's subscription limit have different remedies — raise
    `--max-ocr-pages` versus raise the tier — so they must not collapse into one message."""
    client = BudgetedOcrClient(_AlwaysOutOfQuota(), max_pages=0)

    with pytest.raises(OcrBudgetExhausted) as caught:
        client.analyze(b"page")

    assert "budget" in str(caught.value)
    assert "quota" not in str(caught.value).lower()


# --- the run says it once -------------------------------------------------

def test_the_run_reports_the_quota_rather_than_leaving_it_to_the_ledger(monkeypatch, tmp_path):
    """One run-level sentence naming the account, the consequence and the remedy."""
    import tools.ingest_mailbox as ingest_mailbox

    monkeypatch.setattr(store, "CONSOLE_DB_PATH", tmp_path / "console.sqlite3")

    class _Summary:
        emails_processed = 3
        records_staged = 0
        ocr_calls = 1
        ocr_quota_exhausted = True
        orphans = []
        mailbox_address = "a@b.test"
        read_only = True
        would_have_moved = {}
        folders = []

    monkeypatch.setattr(ingest_mailbox, "run_once", lambda **kw: _Summary())

    outcome = runner._run_locked("test", store.SOURCE_MAILBOX)

    assert outcome.error, "an exhausted quota must not read as a clean run"
    assert "quota" in outcome.error.lower()
    assert "reextract" in outcome.error, "the message should name the way to recover the backlog"
    assert "operator" not in outcome.error, "nobody asked for this; it is not an operator stop"


def test_a_healthy_run_says_nothing_about_quota(monkeypatch, tmp_path):
    """The guard must not fire on every run — the assertion that catches an inverted condition."""
    import tools.ingest_mailbox as ingest_mailbox

    monkeypatch.setattr(store, "CONSOLE_DB_PATH", tmp_path / "console.sqlite3")
    monkeypatch.setattr(settings, "RUN_MAX_MINUTES", 60)

    class _Summary:
        emails_processed = 5
        records_staged = 2
        ocr_calls = 4
        ocr_quota_exhausted = False
        orphans = []
        mailbox_address = "a@b.test"
        read_only = True
        would_have_moved = {}
        folders = []

    monkeypatch.setattr(ingest_mailbox, "run_once", lambda **kw: _Summary())

    outcome = runner._run_locked("test", store.SOURCE_MAILBOX)

    assert outcome.error is None
    assert outcome.ok is True
