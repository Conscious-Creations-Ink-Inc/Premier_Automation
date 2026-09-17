"""The report has to be trustworthy before it is useful.

A document that explains a 3,797-item backlog gets acted on: someone reads "690 records are blocked
only on the delivery date" and spends a week on it. So the failure that matters is not a crash — it
is a plausible-looking wrong number.

Both such failures have already happened while building it:

  * every record fell into "missing fields other than the POD date" (1,799 of them), because
    `classify` lowercases the reason and the predicates compared against "POD date". The page
    looked entirely reasonable.
  * three items matched no bucket at all and would have sat in "Other" unread.

These tests hold the two properties that make the rest believable: nothing is lost between the
queue and the document, and the POD buckets actually track the POD field.
"""

import re

import pytest

from tools import needs_a_human_report as report


class _Item:
    """The shape `read_views.manual_queue` returns, reduced to what the classifier reads."""

    def __init__(self, kind, reason):
        self.kind = kind
        self.reason = reason


def test_every_item_lands_in_exactly_one_bucket():
    """The property the whole document rests on. An item that matches nothing must surface as
    "Other" — never be dropped, because a dropped item is invisible in a total."""
    reasons = [
        "missing: POD date",
        "missing: description, POD date",
        "missing: description",
        "missing: spec code, description, quantity, POD date; confidence 0.0 — nothing recognisable",
        "missing: POD date; quantity conflict — two sources state different quantities",
        "something nobody has written a rule for",
    ]
    buckets = [report.classify(r, report.RECORD_BUCKETS) for r in reasons]

    assert len(buckets) == len(reasons), "every item must be classified"
    assert buckets[-1] == "Other", "an unmatched reason must surface, not vanish"
    assert all(b for b in buckets)


def test_the_pod_buckets_actually_match_the_pod_field():
    """The regression that made the first draft wrong.

    `classify` lowercases before matching, so a predicate comparing against "POD date" matched
    nothing and every record fell through to the catch-all. Asserted through `classify` rather than
    against `_fields` directly, because the case mismatch only appears when the two are combined.
    """
    assert report.classify("missing: POD date", report.RECORD_BUCKETS) == \
        "Blocked on the POD date alone"

    assert report.classify("missing: description, POD date", report.RECORD_BUCKETS) == \
        "POD date plus other fields"

    assert report.classify("missing: description", report.RECORD_BUCKETS) == \
        "Missing fields other than the POD date"


def test_a_record_is_bucketed_by_its_most_important_blocker():
    """Buckets are priority-ordered and exclusive, so the counts can be summed. A quantity conflict
    outranks a missing date: the date has an agreed rule, the conflict needs a decision."""
    reason = "missing: POD date; quantity conflict — two sources state different quantities"

    assert report.classify(reason, report.RECORD_BUCKETS) == \
        "Quantity conflict — two sources disagree"


def test_things_that_are_not_defects_are_labelled_as_such():
    """314 emails ask a question and 33 are cancellations. If the document does not say plainly
    that these are correct behaviour, someone will spend a week trying to automate them away."""
    for reason in ("asks whether goods were received — needs a human answer",
                   "order cancellation notice — needs a manual PO update in Spitfire",
                   "lost / damaged / claim / replacement-PO thread"):
        name = report.classify(reason, report.EMAIL_BUCKETS)
        _, fix = report._bucket_help(name, report.EMAIL_BUCKETS)
        assert "NOT A DEFECT" in fix, f"{name} must be marked as expected behaviour"


def test_the_field_parser_reads_a_missing_clause():
    assert report._fields("missing: description, POD date") == {"description", "POD date"}
    assert report._fields("missing: spec code") == {"spec code"}
    assert report._fields("no missing clause here") == set()
    assert report._fields("") == set()


def test_the_generator_refuses_to_write_a_document_that_does_not_add_up():
    """The last line of defence, and the reason it is a refusal rather than a warning.

    If the buckets do not sum to the queue total, something has been lost. A report that
    under-reports is worse than no report, because it is still believed — so it must not be
    produced at all.
    """
    source = (report.__file__ or "")
    text = open(source, encoding="utf-8").read() if source else ""

    assert "REFUSING TO WRITE" in text
    assert re.search(r"if counted != data\[.total.\]", text), (
        "the sum check must compare bucket totals against the queue total before writing")


def test_no_figure_in_the_report_is_hardcoded():
    """Every number must come from the live store. A stale figure in a document like this is not a
    cosmetic problem — it is a wrong decision taken on old data."""
    text = open(report.__file__, encoding="utf-8").read()
    body = text.split('"""', 2)[-1]          # skip the module docstring, which cites the analysis

    for figure in ("3,797", "2,443", "2,331", "1,340"):
        assert figure not in body, (
            f"{figure} is hardcoded outside the docstring; the report must read it live")
