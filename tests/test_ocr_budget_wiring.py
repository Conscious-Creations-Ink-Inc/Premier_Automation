"""The per-run OCR budget, and the leak that made it fifteen.

`BudgetedOcrClient` is the only real spend stop in this project. Its ceiling used to come from
`tools.ingest_corpus.DEFAULT_MAX_OCR_PAGES` — a *tool* default of 15, written for driving a corpus
by hand from a terminal — and `operations/runner.py` never overrode it. So every scheduled run
against Premier's live mailbox stopped after fifteen pages and recorded everything after that as
`service_unavailable`, detail "OCR page budget of 15 exhausted".

104 attachments accumulated that way while the Azure credentials were valid and the service was
answering. The failure looked like an outage and was a default.

These tests pin the two halves: the budget is a setting, and the runner actually passes it.
"""

import pytest

from config import settings
from tools.ingest_corpus import BudgetedOcrClient


class _Counting:
    def __init__(self):
        self.seen = 0

    def analyze(self, content_bytes):
        self.seen += 1
        return None


def test_the_budget_refuses_past_its_ceiling_and_says_why():
    """It raises rather than returning empty, so `dispatch` records the reason on the ledger row
    and the manual page reads "we stopped, and here is why" instead of "the image was blank"."""
    inner = _Counting()
    client = BudgetedOcrClient(inner, max_pages=2)

    client.analyze(b"one")
    client.analyze(b"two")
    with pytest.raises(RuntimeError) as caught:
        client.analyze(b"three")

    assert "budget of 2 exhausted" in str(caught.value)
    assert inner.seen == 2, "the refused page must not reach the paid client"
    assert (client.calls, client.refused) == (2, 1)


def test_a_production_run_uses_the_configured_budget_not_the_tool_default():
    """The regression itself. `runner` passing nothing meant the tool default governed production,
    and nothing in the settings file said so — the number could not be found by looking where every
    other spend cap lives."""
    from tools.ingest_corpus import DEFAULT_MAX_OCR_PAGES

    assert hasattr(settings, "OCR_PAGES_PER_RUN"), "the per-run ceiling belongs in settings"
    assert settings.OCR_PAGES_PER_RUN > DEFAULT_MAX_OCR_PAGES, (
        "a live mailbox sees far more than a hand-driven corpus run; if this is ever lowered to "
        "the tool default again, the backlog it caused will come back")


def test_the_runner_passes_the_budget_to_both_ingest_paths(monkeypatch):
    """Asserted by driving `runner` and capturing the call, because the bug was not a wrong value
    — it was the argument being absent, which no assertion about settings alone can catch."""
    import tools.ingest_mailbox as ingest_mailbox
    import tools.ingest_corpus as ingest_corpus

    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop here — the call is what is under test")

    monkeypatch.setattr(ingest_mailbox, "run_once", fake)
    monkeypatch.setattr(ingest_corpus, "run_once", fake)

    from operations import runner, store

    for source in (store.SOURCE_MAILBOX, store.SOURCE_SAMPLE):
        seen.clear()
        try:
            runner._run_locked("test", source)
        except Exception:                                          # noqa: BLE001
            pass
        assert seen.get("max_ocr_pages") == settings.OCR_PAGES_PER_RUN, (
            f"{source}: the run must carry the configured budget, not inherit the tool default")
