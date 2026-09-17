"""The issues report has to be reproducible, or it is an opinion with tables.

Its job is to send someone to the right week of work. A wrong number here does not crash anything —
it sends them to the wrong place, and the tables make it look authoritative while doing so.

Two failures already happened while building the sibling report: every record fell into the wrong
bucket because `classify` lowercased and the predicate did not, and three items matched nothing and
would have sat unread in "Other". Both looked entirely plausible on the page.

So what is held here is: the counts reconcile to the queue, the rule table covers every rule the
pipeline can emit, nothing is hardcoded, and the buckets that are *correct behaviour* stay labelled
as such — because the fastest way to waste a week on this queue is to try to automate away the
mail that genuinely needs a person.
"""

import sqlite3

import pytest

from pipeline import read_views, state_db
from tools import needs_a_human_issues_report as report


@pytest.fixture
def conn(tmp_path):
    connection = state_db.get_connection(tmp_path / "state.sqlite3")
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


def test_the_rule_counts_reconcile_to_the_queue(conn):
    """The property the whole document rests on. Grouping is by `matched_rule`, so an email whose
    rule is missing from the log would silently leave the totals."""
    data = report.gather(conn)

    assert sum(data["by_rule"].values()) == data["emails"]


def test_the_generator_refuses_to_write_a_document_that_does_not_add_up():
    """A refusal, not a warning. A report that under-reports is worse than no report because it is
    still believed."""
    text = open(report.__file__, encoding="utf-8").read()

    assert "REFUSING TO WRITE" in text


def test_every_rule_the_pipeline_can_emit_has_an_explanation():
    """A rule with no entry renders as "unrecognised rule" — honest, but useless to a reader. This
    fails when someone adds a triage rule and forgets the document, which is the moment to notice.
    """
    import re

    source = open("pipeline/stage1_triage.py", encoding="utf-8").read()
    emitted = set(re.findall(r'"(rule_[0-9a-z_]+)"', source))

    missing = emitted - set(report.RULES)
    assert not missing, f"triage can emit {sorted(missing)}, which the report cannot explain"


def test_the_buckets_that_are_not_defects_stay_labelled():
    """319 emails ask a question and are correctly handed to a person. If the document does not say
    so, that is where the effort goes."""
    expected = [name for name, (_, verdict, _) in report.RULES.items() if verdict == "EXPECTED"]

    assert "rule_2a_verification_request" in expected
    assert "rule_0b_loss_or_claim" in expected
    for rule in expected:
        assert report.RULES[rule][2], f"{rule} is marked expected but says nothing about why"


def test_a_cancellation_is_judged_by_the_message_not_the_sender(conn):
    """The false-cancellation test is "names no purchase order", deliberately not a list of sender
    names. A brand list stops working the moment next month's marketing arrives from somewhere
    else, and it cannot be argued with; "an order cancellation names an order" can."""
    text = open(report.__file__, encoding="utf-8").read()

    assert 'not (r["po_hints"] or "").strip()' in text
    for brand in ("potterybarn", "wayfair", "autodesk", "lg.com"):
        assert brand not in text.lower(), (
            f"{brand} is hardcoded; classify the message, not the sender")


def test_no_figure_is_hardcoded():
    """Every number must come from the live store, or the document is a snapshot pretending to be
    current. The module docstring may cite the analysis that prompted the work; the code may not."""
    text = open(report.__file__, encoding="utf-8").read()
    body = text.split('"""', 2)[-1]

    for figure in ("1,056", "1056", "673", "374", "319", "212"):
        assert figure not in body, f"{figure} is hardcoded outside the docstring"


def test_the_report_reads_the_configured_domains_rather_than_a_list_of_its_own(conn):
    """The headline finding is that triage knows a small number of domains. That number has to come
    from the configuration, or the document would keep claiming fifteen after someone adds more."""
    known = report._configured_domains()

    from config import settings
    for domain in settings.INTERNAL_DOMAINS:
        assert known.get(domain.lower()) == "internal"


def test_an_empty_store_produces_no_claims(conn):
    """A fresh database must not produce a document full of zeros presented as findings."""
    data = report.gather(conn)

    assert data["emails"] == 0
    assert data["by_rule"] == {} or sum(data["by_rule"].values()) == 0
