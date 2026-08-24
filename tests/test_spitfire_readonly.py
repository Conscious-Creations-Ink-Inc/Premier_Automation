"""The guardrail that makes "we only read from Spitfire" a checkable claim rather than a promise.

Premier is being asked for an ERP login on the strength of this connector never writing. That is
not something a code review can keep true as the module grows, so it is pinned here: the
allowlist is exhaustive, path matching cannot be tricked into authorising a longer path, and a
rejected request never reaches the network.

The field-mapping tests below are the second half of the same idea — they pin the three traps the
8 August 2026 probe found on PO 212456, each of which silently produces a wrong receiver rather
than an error.
"""

import pytest
import requests

from connectors import spitfire
from connectors.spitfire import (
    PODocument,
    SpitfireReadClient,
    SpitfireReadOnlyViolation,
    is_allowed,
    is_tax_line,
    strip_html,
)


@pytest.fixture
def client(monkeypatch):
    """A client with credentials that would work, and a session that cannot be used.

    Any request that gets past the allowlist blows up loudly instead of quietly reaching
    training.remingtonhotels.com — the test suite must never touch Premier's ERP.
    """
    monkeypatch.setattr(spitfire.settings, "SPITFIRE_UID", "test@example.com", raising=False)
    monkeypatch.setattr(spitfire.settings, "SPITFIRE_PW", "unused", raising=False)
    c = SpitfireReadClient(base_url="https://spitfire.invalid/Training")

    def _no_network(*args, **kwargs):
        raise AssertionError("a request escaped the allowlist and tried to reach the network")

    monkeypatch.setattr(c._session, "request", _no_network)
    return c


# --- the allowlist ------------------------------------------------------------


def test_every_allowlisted_entry_matches_itself():
    """Guards against an entry being added with a typo that silently never matches — which would
    fail closed (an operation we meant to permit gets rejected), but confusingly."""
    for method, template in spitfire._ALLOWED:
        concrete = template.replace("{}", "6aad38da-39f6-41b7-afc2-480f372e1fa4")
        assert is_allowed(method, concrete), f"{method} {template} does not match itself"


@pytest.mark.parametrize("method,path", [
    # The four posting targets specified in the plan. None of them is authorised.
    ("POST", "/api/document/0/0c9a537a-3c41-4d16-ab9f-130ef69ea6c8"),   # create the receipt doc
    ("POST", "/api/document/6aad38da/items"),                            # add received lines
    ("POST", "/api/catalog/upload"),                                     # attach a POD
    ("POST", "/api/document/6aad38da/comments"),                         # audit comment
    ("PUT", "/api/xts/map"),                                             # register the key map
    # Anything else destructive.
    ("DELETE", "/api/document/6aad38da"),
    ("PUT", "/api/document/6aad38da"),
    ("PATCH", "/api/document/6aad38da/Status"),
    ("POST", "/api/document/6aad38da/next"),
    ("POST", "/api/document/6aad38da/workflow/RECEIVE"),
    ("DELETE", "/api/document/6aad38da/items/all"),
])
def test_write_operations_are_rejected(method, path):
    assert not is_allowed(method, path)


def test_read_path_does_not_authorise_a_longer_one():
    """`GET /api/document/{}` must not swallow the suffix and authorise `/items` or anything else.

    Segment-wise comparison is what prevents this; a naive prefix or regex match would not, and
    the difference matters because a document Premier considers sensitive can be readable at the
    header while its items are not.
    """
    assert is_allowed("GET", "/api/document/6aad38da")
    assert is_allowed("GET", "/api/document/6aad38da/items")
    assert not is_allowed("GET", "/api/document/6aad38da/access")
    assert not is_allowed("GET", "/api/document/6aad38da/items/extra")


def test_path_matching_is_case_insensitive_but_method_is_not():
    """The Swagger itself is inconsistent — the same controller serves `/api/document/{id}/items`
    and `/api/Document/{id}/dialog/link` — so path case cannot be load-bearing. Method case is
    normalised, but a different verb is a different operation."""
    assert is_allowed("GET", "/api/Document/6aad38da/dialog/link")
    assert is_allowed("get", "/api/document/6aad38da/dialog/link")
    assert not is_allowed("POST", "/api/document/6aad38da/dialog/link")


def test_query_string_is_ignored_when_matching():
    assert is_allowed("GET", "/api/projects?forUserKey=00000000-0000-0000-0000-000000000000")


def test_post_is_allowed_for_the_searches_that_are_reads():
    """sfPMS uses POST for reads. A "GET only" rule would have been both wrong and useless: it
    would block PO discovery, which is the one thing this phase exists to do."""
    assert is_allowed("POST", "/api/catalog/search/0/contents")
    assert is_allowed("POST", "/api/project/PNW025TB100012/docs")
    assert is_allowed("POST", "/api/document/6aad38da/changes/items")


# --- the chokepoint -----------------------------------------------------------


def test_rejected_request_never_reaches_the_network(client):
    """The point of raising in `_request` rather than relying on the server to 403: a write that
    reaches Spitfire and is refused has still been *attempted* against Premier's ERP, and shows
    up in their audit log as one. This must fail before the socket."""
    with pytest.raises(SpitfireReadOnlyViolation, match="read-only allowlist"):
        client._request("POST", "/api/document/6aad38da/items", json=[{"Description": "x"}])
    assert client.audit_log == []


def test_allowed_request_is_recorded_in_the_audit_log(client, monkeypatch):
    """`audit_log` is the evidence handed to Premier that a run only read. It has to record every
    request that was actually issued, including failures."""
    class _Response:
        status_code = 200

        def json(self):
            return "2023.0.9692.36214"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(client._session, "request", lambda *a, **k: _Response())
    assert client.server_version() == "2023.0.9692.36214"
    assert [(r.method, r.path, r.status) for r in client.audit_log] == [
        ("GET", "/api/system/version", 200)
    ]


def test_connector_exposes_no_write_methods():
    """A second, independent check: even if someone widens the allowlist, there is no method here
    that builds a receipt, an item, an attachment or a comment."""
    forbidden = ("create", "post_", "write", "attach", "upload", "delete", "update", "submit")
    offenders = [
        name for name in dir(SpitfireReadClient)
        if not name.startswith("_") and any(word in name.lower() for word in forbidden)
    ]
    assert offenders == []


def test_five_hundred_is_retried_once_then_raises(client, monkeypatch):
    """An unknown document GUID returns 500 with a body identical to a genuine server fault —
    sfPMS does not 404 for a missing document. So 500 cannot mean "not found", and it cannot be
    retried indefinitely either, or a bad key becomes a hang."""
    calls = []

    class _ServerError:
        status_code = 500

        def json(self):
            return {"Message": "An error has occurred."}

        def raise_for_status(self):
            raise requests.HTTPError("500")

    def _record(*args, **kwargs):
        calls.append(args)
        return _ServerError()

    monkeypatch.setattr(client._session, "request", _record)
    with pytest.raises(requests.HTTPError):
        client._get_json("/api/document/not-a-real-guid")
    assert len(calls) == 2, "expected exactly one retry"


# --- field mapping: the three traps ------------------------------------------


def _doc() -> PODocument:
    return PODocument(
        doc_master_key="6aad38da-39f6-41b7-afc2-480f372e1fa4",
        po_number="212456", project_code="PNW025TB100012",
        project_name="Westin Princeton Public Space", doc_status="M",
        doc_status_label="Committed", source_date=None,
        vendor_name="Peerless Industries Inc", vendor_email="KPetrin@peerless-av.com",
        ship_to="***DO NOT SHIP ON YOUR OWN***", assigned_agent="Delfina Marsetti",
        pay_terms_prose=None,
    )


# Line 0001 of PO 212456, as the API actually returned it: ItemQuantity 0.0 despite ordering 2,
# Specification null, Description wrapped in HTML.
_GOODS_LINE = {
    "DocItemKey": "45fd1906-0b83-4868-b12a-bdcac04a8bfc",
    "DocItemNumber": "0001",
    "SourceItemNumber": "FIT-902-TV",
    "Specification": None,
    "Description": "<div>FIT-902-TV - TV Wall Mount -&nbsp;Exercise Area</div>",
    "ItemQuantity": 0.0,
    "ItemStatus": "N",
    "Due": None,
    "Requested": None,
    "DocItemTask": [{"UOM": "EA", "Quantity": 2.0, "Rate": 75.98,
                     "ProjEntity": "102011249", "AccountCategory": "MAT-FDP"}],
    "RelatedLineDetails": {"ContractUnits": 2.0, "ReceivedUnits": 0.0,
                           "ReceiptInProgressUnits": 0.0, "ContractAmount": 151.96},
}

# Line 0002 is Tax and is shaped exactly like an under-populated goods line.
_TAX_LINE = {
    "DocItemKey": "9c2b1e40-1111-2222-3333-444455556666",
    "DocItemNumber": "0002",
    "SourceItemNumber": "",
    "Description": "<div>Sales Tax</div>",
    "ItemQuantity": 0.0,
    "ItemStatus": None,
    "DocItemTask": [{"UOM": None, "Quantity": 0.0, "ProjEntity": "102011249",
                     "AccountCategory": "TAX-FP0"}],
    "RelatedLineDetails": {"ContractUnits": 0.0, "ReceivedUnits": 0.0},
}


def test_ordered_quantity_never_comes_from_item_quantity():
    """The trap that would under-receive every line on every PO: `ItemQuantity` reads 0.0 on a
    line that orders 2 units. Quantity comes from `RelatedLineDetails.ContractUnits`."""
    line = spitfire._to_po_line(_GOODS_LINE, _doc())
    assert line.qty_ordered == 2.0
    assert _GOODS_LINE["ItemQuantity"] == 0.0, "fixture must keep reproducing the real trap"


def test_quantity_falls_back_to_the_task_when_related_details_are_absent():
    """`RelatedLineDetails` is documented as "often zero, max 1" — a PO line that is not part of a
    commitment family has none, and the task quantity is then the only ordered quantity there is."""
    item = {**_GOODS_LINE}
    item.pop("RelatedLineDetails")
    assert spitfire._to_po_line(item, _doc()).qty_ordered == 2.0


def test_spec_code_comes_from_source_item_number_not_specification():
    """`DocItem.Specification` is null on real POs. Premier's own czx_TPICreate_ReceiptDoc joins
    `TPI.ItemNumber = di.SourceItemNumber`, which independently confirms where the spec lives."""
    line = spitfire._to_po_line(_GOODS_LINE, _doc())
    assert line.spec_code == "FIT-902-TV"


def test_description_is_stripped_of_html():
    """DESC_MATCH_THRESHOLD is 80 on a RapidFuzz token_sort_ratio. Leaving the markup in means
    two unrelated descriptions can clear it on their shared `<div>` and `&nbsp;` alone."""
    line = spitfire._to_po_line(_GOODS_LINE, _doc())
    assert line.description == "FIT-902-TV - TV Wall Mount - Exercise Area"
    assert "<" not in line.description and "&nbsp;" not in line.description


def test_strip_html_tolerates_none_and_plain_text():
    assert strip_html(None) == ""
    assert strip_html("  FIT-902-TV   spacing  ") == "FIT-902-TV spacing"


def test_tax_lines_are_identified_by_account_category():
    """Nothing else distinguishes them: a tax line has a number, an amount and a description, and
    its ContractUnits/UOM/ItemStatus are as empty as an incomplete goods line's."""
    assert is_tax_line(_TAX_LINE)
    assert not is_tax_line(_GOODS_LINE)


def test_tax_line_with_no_task_is_still_caught():
    """`DocItemTask` is only promised to be "often 1-1". `AccountCategory` is carried on
    RelatedLineDetails too, and a tax line that slipped the check would be offered to the matcher
    as receivable."""
    item = {**_TAX_LINE, "DocItemTask": [],
            "RelatedLineDetails": {"AccountCategory": "TAX-FP0", "ContractUnits": 0.0}}
    assert is_tax_line(item)


def test_unapproved_receipts_are_read_as_in_transit_and_reduce_outstanding():
    """ReceiptInProgressUnits is "units tentatively received not yet approved" — a receipt
    document that exists but has not cleared its approval route.

    Missing it is how one physical delivery becomes two receivers, which is the failure that
    ended Premier's previous attempt: the line reads as fully outstanding, so a second receipt
    gets raised against goods that are already booked in.
    """
    item = {**_GOODS_LINE, "RelatedLineDetails": {
        "ContractUnits": 10.0, "ReceivedUnits": 4.0, "ReceiptInProgressUnits": 6.0,
    }}
    line = spitfire._to_po_line(item, _doc())
    assert (line.qty_ordered, line.qty_received, line.qty_in_transit) == (10.0, 4.0, 6.0)
    assert line.qty_outstanding == 0.0, "a fully in-flight line has nothing left to receive"


def test_uom_falls_back_to_related_details():
    """Carried in two places. A line with no task extension would otherwise report a blank UOM,
    and Stage 5's package-vs-item check needs it to tell 41 CTN from 11 EA."""
    item = {**_GOODS_LINE, "DocItemTask": [],
            "RelatedLineDetails": {"ContractUnits": 2.0, "UOM": "EA"}}
    assert spitfire._to_po_line(item, _doc()).unit_of_measure == "EA"


def test_line_number_is_normalised_to_an_integer():
    """Spitfire displays "0001"; Premier's Authority Inbound cites "208491 : 300". Comparing
    ExtractedRecord.po_line_number to a Spitfire line at all depends on this normalisation."""
    assert spitfire._to_po_line(_GOODS_LINE, _doc()).line_number == 1


def test_unparseable_line_number_does_not_abort_the_line():
    """A line whose number is non-numeric is still receivable by spec code; losing the whole line
    to a ValueError would be a worse outcome than losing the exact-line-number match tier."""
    item = {**_GOODS_LINE, "DocItemNumber": "0001-A"}
    line = spitfire._to_po_line(item, _doc())
    assert line.line_number == 0
    assert line.spec_code == "FIT-902-TV"


# --- The order date ---------------------------------------------------------

def test_the_order_date_is_the_headers_doc_date():
    """Established by reading all 28 mirrored POs off the live host. Two candidates were rejected:
    `SourceDate` has a line due before it on 11 of 17 POs, and `Due` is mostly Spitfire's null
    date. `DocDate` fails neither test."""
    header = {"DocDate": "2025-04-28T00:00:00", "Due": "2025-03-25T00:00:00",
              "SourceDate": "2025-10-08T00:00:00"}
    assert spitfire.order_date_of(header) == "2025-04-28"


def test_source_date_is_never_mistaken_for_the_order_date():
    """The regression guard. `SourceDate` reads like the obvious field and is wrong: sorting the
    28 POs by number puts `DocDate` in perfect date order across 27 consecutive pairs and inverts
    `SourceDate` on 11 of them. Spitfire issues PO numbers in sequence, so that ordering is what
    the order date must satisfy."""
    assert spitfire.order_date_of({"SourceDate": "2025-10-08T00:00:00"}) is None


def test_spitfires_null_date_is_treated_as_absent():
    """`0001-01-01` is how Spitfire spells "no date" — `Due` carries it on 12 of the 28 POs.
    Passing it through would claim the order was raised in the year 1."""
    assert spitfire.order_date_of({"DocDate": "0001-01-01T00:00:00"}) is None


def test_a_malformed_header_does_not_raise():
    """It is read over the wire from a system we do not control."""
    assert spitfire.order_date_of({}) is None
    assert spitfire.order_date_of(None) is None
    assert spitfire.order_date_of("not a dict") is None
