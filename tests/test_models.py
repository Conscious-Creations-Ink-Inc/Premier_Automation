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
