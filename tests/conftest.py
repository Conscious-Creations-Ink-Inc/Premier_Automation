import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _never_bill_azure_from_the_test_suite():
    """Force the mock OCR client for the whole suite, regardless of what `.env` holds.

    `build_default_adapters` resolves its OCR client through `build_client()`, which prefers the
    real Azure Document Intelligence client whenever credentials are present — correct for a
    production run, and exactly wrong for tests: every image fixture a test happens to push
    through the pipeline would become a billed page against Premier's quota, silently.

    Tests that mean to exercise a real client construct it explicitly; nothing here stops them.
    """
    previous = os.environ.get("PREMIER_OCR_CLIENT")
    os.environ["PREMIER_OCR_CLIENT"] = "mock"

    from config import settings
    previous_setting = settings.OCR_CLIENT
    settings.OCR_CLIENT = "mock"

    yield

    settings.OCR_CLIENT = previous_setting
    if previous is None:
        os.environ.pop("PREMIER_OCR_CLIENT", None)
    else:
        os.environ["PREMIER_OCR_CLIENT"] = previous


@pytest.fixture(autouse=True, scope="session")
def _the_hold_grace_period_is_the_real_one():
    """Pin `HOLD_GRACE_PERIOD_HOURS` to the shipped rule, whatever `.env` says.

    It is env-overridable so a demo can release held mail immediately
    (`PREMIER_HOLD_GRACE_HOURS=0`). Left unpinned, that override silently rewrites what the suite
    is testing: with it set to 0, `test_property_confirmation_only_releases_after_grace_period`
    and the corpus regression both failed — not because anything was broken, but because the rule
    under test had been changed underneath them by a file that is not even in the repository.

    A test asserting a business rule must read that rule from the code, never from the machine it
    happens to be running on.
    """
    from config import settings
    previous = settings.HOLD_GRACE_PERIOD_HOURS
    settings.HOLD_GRACE_PERIOD_HOURS = 48
    yield
    settings.HOLD_GRACE_PERIOD_HOURS = previous
