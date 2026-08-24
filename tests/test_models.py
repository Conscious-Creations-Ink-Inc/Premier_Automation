from pipeline.models import (
    Attachment,
    RawEmail,
    NotificationType,
    TriageCategory,
    TriagedEmail,
    AccumulationKey,
    DeliveryEvent,
    ExtractedRecord,
    POLine,
    MatchResult,
    VerifyResult,
    RouteTarget,
    RoutingDecision,
)


def test_raw_email_round_trip():
    email = RawEmail(
        email_id="msg-1",
        received_at="2026-07-16T10:00:00Z",
        sender_address="notify@authoritylogistics.com",
        sender_domain="authoritylogistics.com",
        subject="PO 208491 inbound",
        body_html="<table></table>",
        body_text=None,
        attachments=[Attachment(filename="pod.pdf", content_type="application/pdf", content_bytes=b"%PDF-1.4")],
    )
    assert email.attachments[0].filename == "pod.pdf"


def test_triaged_email_defaults_reason_empty():
    email = RawEmail(
        email_id="msg-2",
        received_at="2026-07-16T10:00:00Z",
        sender_address="a@b.com",
        sender_domain="b.com",
        subject="test",
        body_html=None,
        body_text="hello",
    )
    triaged = TriagedEmail(
        email=email,
        notification_type=NotificationType.UNKNOWN,
        category=TriageCategory.ROUTE,
        matched_rule="rule_6_unknown",
        extracted_po_hints=[],
    )
    assert triaged.reason == ""
    assert triaged.category == TriageCategory.ROUTE


def test_accumulation_key_is_hashable():
    key = AccumulationKey(po_number="208491", shipment_number="50052")
    assert {key: "value"}[key] == "value"


def test_match_result_and_route_target_shapes():
    extracted = ExtractedRecord(
        source_email_id="msg-3",
        po_number="213987",
        shipment_number=None,
        spec_code="LI-12",
        parent_spec_code="LI-12",
        sub_spec_suffix=None,
        item_description="Lyla Medium Convertible Chandelier",
        vendor_name="PBH Hospitality",
        carrier_name="DHL",
        tracking_number="123",
        quantity_received=1.0,
        unit_of_measure="EA",
        pod_stated_date="2026-06-08",
        email_date="2026-06-08T09:00:00Z",
        delivery_location=None,
        comments=None,
        extraction_source="html",
        extraction_confidence=1.0,
        raw_snippet="<tr>...</tr>",
    )
    line = POLine(
        po_number="213987",
        line_number=1,
        line_key="guid-1",
        spec_code="LI-12",
        description="Lyla Medium Convertible Chandelier",
        vendor_name="PBH Hospitality",
        unit_of_measure="EA",
        qty_ordered=1.0,
        qty_received=0.0,
        cost_code="1000",
        project_code="DBR-025",
        project_name="Meeting Room Renovation",
        line_status="Open",
        expected_date=None,
        ship_to=None,
        assigned_agent=None,
    )
    match = MatchResult(extracted=extracted, po_line=line, signals_matched=3, confidence="high", notes="")
    verify = VerifyResult(
        match=match, passed=True, reason="passed",
        resolved_received_date="2026-06-08", requires_email_confirmation=False, in_scope=True,
    )
    routing = RoutingDecision(
        source_stage="stage7_route", reference={"po_number": "213987"},
        route_to=RouteTarget.AUTO_APPROVED, reason="clean match", logged_at="2026-06-08T09:05:00Z",
    )
    assert verify.passed
    assert routing.route_to == RouteTarget.AUTO_APPROVED


def test_extracted_record_fields_all_persist_through_the_staging_store():
    """A field on the model with no column in the store is silently dropped between Stage 3 and
    Stage 4. That happened to `po_line_number` — the Spitfire line number, the most valuable
    field the Authority format supplies — so the two are pinned together here."""
    import dataclasses

    from pipeline import extracted_records_store
    from pipeline.models import ExtractedRecord

    model_fields = {f.name for f in dataclasses.fields(ExtractedRecord)}
    stored_fields = set(extracted_records_store._COLUMNS)
    assert model_fields == stored_fields, (
        f"only on the model: {sorted(model_fields - stored_fields)}; "
        f"only in the store: {sorted(stored_fields - model_fields)}"
    )


def test_staging_store_round_trips_every_field():
    from pipeline import extracted_records_store, state_db
    from pipeline.models import ExtractedRecord

    conn = state_db.get_connection(":memory:")
    try:
        record = ExtractedRecord(
            source_email_id="msg-1", po_number="208491", shipment_number="50052",
            spec_code="STE-402-LT-B", parent_spec_code="STE-402-LT", sub_spec_suffix="B",
            item_description="BASE, Floor Lamp 2", vendor_name="Light Annex",
            carrier_name="Nolan Transportation", tracking_number="8840455",
            quantity_received=11.0, unit_of_measure="EA", pod_stated_date="2025-10-01",
            email_date="2025-10-01T18:00:00Z", delivery_location="Crown Worldwide - Mira Loma",
            comments="STE-402-LT", extraction_source="authority_inbound",
            extraction_confidence=1.0, raw_snippet="208491 : 300",
            po_line_number=300, received_by="Miguel C.",
            package_quantity=11.0, package_uom="CTN", notification_number="239336",
        )
        extracted_records_store.write_pending(conn, record, "2026-08-03T00:00:00Z")
        restored = extracted_records_store.get_pending(conn)[0].record
        assert restored == record
    finally:
        conn.close()
