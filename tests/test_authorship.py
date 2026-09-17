"""Who wrote a message decides whether its records are evidence or a request.

The whole rule rests on one distinction — origin sender versus envelope sender — and getting it
backwards fails silently: it withholds the warehouse's own receiving reports while looking exactly
like a rule that is working. These cases pin the distinction and the two ways it degrades.
"""
from pipeline import authorship


def test_premiers_own_address_is_internal():
    assert authorship.authored_internally("expeditor@example-pm.test")


def test_a_warehouse_address_is_not():
    assert not authorship.authored_internally("atlas.notifications@example-logistics.test")


def test_the_domain_is_compared_case_insensitively():
    """Mail headers are not normalised, and a capitalised domain is the same organisation."""
    assert authorship.authored_internally("Irene.Renzi@Example-PM.Test")


def test_a_subdomain_is_not_the_internal_domain():
    """`example-pm.test.vendor.example` ends with the internal domain as text and is a different
    organisation entirely. Matching on the whole domain is what keeps a lookalike sender out."""
    assert not authorship.authored_internally("someone@example-pm.test.vendor.example")


def test_an_unknown_origin_is_not_internal():
    """Unknown must not mean internal.

    Blank, None and a bare word all mean the origin hop could not be resolved. Treating that as
    internal would withhold every record on the message and give no reason a person could act on;
    treating it as external leaves it visible, which is the failure that gets noticed.
    """
    assert not authorship.authored_internally(None)
    assert not authorship.authored_internally("")
    assert not authorship.authored_internally("   ")


def test_triage_asks_the_same_question():
    """`stage1_triage._is_internal` must delegate, not keep a second copy.

    The two answered the same question in two places, and a message can only be triaged one way and
    read another if they are allowed to drift.
    """
    from pipeline import stage1_triage
    assert stage1_triage._is_internal("expeditor@example-pm.test")
    assert not stage1_triage._is_internal("atlas.notifications@example-logistics.test")
