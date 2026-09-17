"""One document, one OCR call — however many attachments carry it.

Mail repeats itself and the ledger says so plainly. On 2026-09-09 the 533 attachments waiting on
OCR were **227 distinct blobs**: one signature banner accounted for thirty of them, one EMCO bill
of lading for two, and 99 documents appeared exactly once. Reading by ledger row therefore meant
paying 2.3 times over for the same pages — and because the page budget is finite, a run that spent
itself on repeats left genuinely unread documents still unread behind it.
"""

import pytest

from pipeline.stage3_extract.ocr_adapter import (
    CachingOcrClient,
    OcrBudgetExhausted,
    OcrQuotaExhausted,
    OcrResult,
)


class CountingClient:
    def __init__(self, result=None, error=None):
        self.calls = 0
        self.result = result if result is not None else OcrResult(raw_text="PO 212696")
        self.error = error

    def analyze(self, content_bytes):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def test_the_same_document_is_read_once_however_many_rows_carry_it():
    inner = CountingClient()
    cache = CachingOcrClient(inner)

    for _ in range(30):
        assert cache.analyze(b"the same signature banner").raw_text == "PO 212696"

    assert inner.calls == 1, "thirty ledger rows, one billed page"
    assert (cache.misses, cache.hits) == (1, 29)


def test_two_different_documents_never_share_an_entry():
    inner = CountingClient()
    cache = CachingOcrClient(inner)

    cache.analyze(b"one bill of lading")
    cache.analyze(b"a different bill of lading")

    assert inner.calls == 2


def test_a_refusal_about_the_document_is_cached_too():
    """A GIF the service will not accept is refused identically the next twenty-nine times.

    Re-asking spends a round trip and a slice of the tier's rate allowance to be told the same
    thing — which is how one unsupported container turned into twenty-one ledger rows.
    """
    inner = CountingClient(error=ValueError("InvalidContent"))
    cache = CachingOcrClient(inner)

    for _ in range(5):
        with pytest.raises(ValueError):
            cache.analyze(b"an animated gif")

    assert inner.calls == 1


@pytest.mark.parametrize("error", [
    OcrBudgetExhausted("page budget exhausted"),
    OcrQuotaExhausted("no call volume left"),
])
def test_a_budget_or_quota_stop_is_never_cached_against_a_document(error):
    """Neither is a fact about the document — one is our ceiling, the other the subscription.

    Cached against the blob, a stop that happened to land on a document's turn would follow that
    document for the rest of the run, and would still be there after the budget was raised. The
    only thing that would have been learned is which blob was unlucky.
    """
    inner = CountingClient(error=error)
    cache = CachingOcrClient(inner)

    for _ in range(3):
        with pytest.raises(type(error)):
            cache.analyze(b"an unlucky document")

    assert inner.calls == 3, "the stop is re-asked, never remembered against this blob"
    assert cache.hits == 0
