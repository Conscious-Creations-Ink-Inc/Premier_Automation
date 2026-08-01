"""Fixtures for the dashboard API tests.

Follows the convention the existing pipeline tests use: an in-memory SQLite database, so no
test ever touches the on-disk demo or pipeline state. The whole test shares one connection
because ":memory:" gives a *new* empty database per connection — handing the same one to the
FastAPI dependency is what lets a request see the seeded rows.
"""
import pytest
from fastapi.testclient import TestClient

from api import db, deps
from api.demo import seed as demo_seed
from api.main import app
from api.stores.po_lines_store import POLineRow
from pipeline.models import ExtractedRecord, POLine


@pytest.fixture
def demo_db(tmp_path):
    """A throwaway database file per test.

    Not ":memory:" here: TestClient serves requests on a different thread, and a single
    sqlite3 connection cannot cross threads. Backing the tests with a temp file lets each
    request open its own connection — which is exactly what production does — instead of
    smuggling one connection between threads.
    """
    return tmp_path / "demo_test.sqlite3"


@pytest.fixture
def conn(demo_db):
    connection = db.get_demo_connection(demo_db)
    demo_seed.seed_demo(connection)
    yield connection
    connection.close()


@pytest.fixture
def client(conn, demo_db):
    """TestClient deliberately not used as a context manager: entering it would run the app's
    lifespan, which opens the real on-disk demo database. The tests only need the routes."""
    def override_get_conn():
        request_conn = db.get_demo_connection(demo_db)
        try:
            yield request_conn
        finally:
            request_conn.close()

    app.dependency_overrides[deps.get_conn] = override_get_conn
    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def record_factory():
    """Exposed as a fixture so tests need no cross-package import of this module."""
    return make_record


@pytest.fixture
def line_factory():
    return make_line


def make_record(**overrides) -> ExtractedRecord:
    """A record that matches the seeded PO line 212456/STE-402-LT on all three signals. Override
    a field to build the failure case under test."""
    values = dict(
        source_email_id="em-test", po_number="212456", shipment_number=None,
        spec_code="STE-402-LT", parent_spec_code=None, sub_spec_suffix=None,
        item_description="Steelcase 402 Low Table, walnut", vendor_name="Peerless-AV",
        carrier_name="FedEx", tracking_number="TRACK-1", quantity_received=2.0,
        unit_of_measure="EA", pod_stated_date="2026-07-14",
        email_date="2026-07-14T00:00:00+00:00", delivery_location=None, comments=None,
        extraction_source="html", extraction_confidence=1.0, raw_snippet="test snippet",
    )
    values.update(overrides)
    return ExtractedRecord(**values)


def make_line(line_id: int = 1, **overrides) -> POLineRow:
    values = dict(
        po_number="212456", line_number=1, line_key="key-1", spec_code="STE-402-LT",
        description="Steelcase 402 Low Table, walnut", vendor_name="Peerless-AV",
        unit_of_measure="EA", qty_ordered=12.0, qty_received=0.0, cost_code="1100",
        project_code="MRC-024", project_name="Hilton LXR", line_status="Open",
        expected_date="2026-07-18", ship_to="LXR Cameo", assigned_agent="T. Turner",
        pay_terms="Net 30",
    )
    values.update(overrides)
    return POLineRow(id=line_id, line=POLine(**values))
