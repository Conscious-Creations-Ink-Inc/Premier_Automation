"""Telling "Spitfire has no such purchase order" apart from "we could not ask it".

`resolve_po` tries three strategies and every one of them swallows its own HTTP failure and moves
on, so the caller received `None` whether Spitfire had answered or nothing had answered at all.
`po_verify` turned that into `po_found = False`, and the reviewer was told the purchase order "was
not found in the projects this connector can search" — a claim about Premier's data resting
entirely on the state of our connection.

That is not hypothetical. Probing ten unresolved POs against the live endpoint on 2026-09-03
returned ten clean "no match" results that were nothing of the kind: the session cookie had
expired, every request was rejected, and the swallowing turned an authentication failure into ten
statements that Premier's purchase orders do not exist. Under "a missing PO is nobody's work" that
would have quietly closed the entire backlog during an outage.
"""

import pytest
import requests

from connectors.spitfire import PO_ABSENT, PO_FOUND, PO_UNREACHABLE
from pipeline import po_verify, post_decision


class _Row(dict):
    """An `extracted_records` row, addressable either way like the sqlite3.Row it stands in for."""
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def record_row(**overrides):
    row = _Row(
        id=1, po_number="210497", spec_code="GR-350a-WTF", parent_spec_code=None,
        item_description="Sheer Fabric", quantity_received=12.0, unit_of_measure="YD",
        package_quantity=None, package_uom=None, pod_stated_date="2026-08-14",
        pod_waived_by=None, po_line_number=None, source_email_id="msg-1",
    )
    row.update(overrides)
    return row


# --- the client -------------------------------------------------------------

def test_a_clean_negative_from_the_endpoint_is_an_absent_po():
    """`DocMasterAlt` answering with an empty string is Spitfire saying it has no such order."""
    from connectors.spitfire import SpitfireReadClient

    client = SpitfireReadClient.__new__(SpitfireReadClient)
    client._post_json = lambda path, body: ""

    assert client._resolve_po_alt_with_outcome("210497") == (None, True)


def test_a_failed_call_is_not_an_absent_po():
    """The distinction the whole change exists for."""
    from connectors.spitfire import SpitfireReadClient

    def boom(path, body):
        raise requests.HTTPError("401 Unauthorized")

    client = SpitfireReadClient.__new__(SpitfireReadClient)
    client._post_json = boom

    key, answered = client._resolve_po_alt_with_outcome("210497")
    assert key is None
    assert answered is False, "a rejected request must never read as 'no such purchase order'"


# --- po_verify --------------------------------------------------------------

class _Unreachable:
    def resolve_po_with_outcome(self, po_number):
        return None, PO_UNREACHABLE

    def read_po(self, key):                                        # pragma: no cover
        raise AssertionError("must not be reached when the lookup did not complete")


class _Absent:
    def resolve_po_with_outcome(self, po_number):
        return None, PO_ABSENT

    def read_po(self, key):                                        # pragma: no cover
        raise AssertionError("there is no key to read")


class _OnlyOldContract:
    """A client predating the outcome-aware form — the cassettes and the existing test doubles."""
    def __init__(self, key=None):
        self.key = key

    def resolve_po(self, po_number):
        return self.key


def verify(conn, client_cls):
    return po_verify.verify_records(conn, [record_row()], client_factory=client_cls)[0]


def test_a_lookup_that_could_not_complete_is_reported_as_an_error(tmp_path):
    from pipeline import state_db

    conn = state_db.get_connection(str(tmp_path / "t.sqlite3"))
    result = verify(conn, _Unreachable)

    assert result.error, "an outage must surface as an error, not as a missing purchase order"
    assert not result.po_found
    conn.close()


def test_a_purchase_order_spitfire_does_not_have_is_not_an_error(tmp_path):
    from pipeline import state_db

    conn = state_db.get_connection(str(tmp_path / "t.sqlite3"))
    result = verify(conn, _Absent)

    assert result.error is None
    assert not result.po_found
    conn.close()


def test_a_client_without_the_outcome_form_still_works(tmp_path):
    """Recorded cassettes must keep replaying; this is what proves the change is additive."""
    from pipeline import state_db

    conn = state_db.get_connection(str(tmp_path / "t.sqlite3"))
    result = po_verify.verify_records(conn, [record_row()],
                                      client_factory=lambda: _OnlyOldContract())[0]

    assert result.error is None
    assert not result.po_found
    conn.close()


# --- the sentence a reviewer reads -------------------------------------------

def test_the_refusal_names_spitfires_data_not_our_configuration(tmp_path):
    from pipeline import state_db

    conn = state_db.get_connection(str(tmp_path / "t.sqlite3"))
    # A POD hash, so the earlier gates pass and this test is about the one it names.
    decision = post_decision.decide(conn, record_row(), client_factory=_Absent,
                                    pod_md5="a" * 32)

    assert decision.verdict == post_decision.FLAG
    assert "Spitfire has no purchase order 210497" in decision.reason
    assert "connector can search" not in decision.reason
    assert "MRC024PB1" not in decision.reason, "our project ids are not the reviewer's problem"
    conn.close()


def test_an_outage_never_reads_as_a_missing_purchase_order(tmp_path):
    from pipeline import state_db

    conn = state_db.get_connection(str(tmp_path / "t.sqlite3"))
    decision = post_decision.decide(conn, record_row(), client_factory=_Unreachable,
                                    pod_md5="a" * 32)

    assert decision.verdict == post_decision.FLAG
    assert "has no purchase order" not in decision.reason
    conn.close()


@pytest.mark.parametrize("outcome", [PO_FOUND, PO_ABSENT, PO_UNREACHABLE])
def test_the_outcomes_are_distinct(outcome):
    assert len({PO_FOUND, PO_ABSENT, PO_UNREACHABLE}) == 3
    assert outcome in {PO_FOUND, PO_ABSENT, PO_UNREACHABLE}
