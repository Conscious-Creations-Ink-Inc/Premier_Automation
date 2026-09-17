"""The synthetic dataset, written out explicitly rather than randomly generated.

Explicit fixtures beat a random generator here: the demo has to tell a coherent story (these
five reconciled themselves, those eight need a person, and *this* is why), the tests assert
against known rows, and a screenshot taken today matches one taken next month.

Everything is modelled on the real artefacts in the discovery docs — Northgate Supply delivery
notes, seagrass mirrors, the Example guestroom conversion — with PO numbers that satisfy
`settings.PO_TOKEN_REGEX` and spec codes that satisfy `settings.SPEC_TOKEN_REGEX`.
"""

PROJECT_EXAMPLE = ("PRJ-001-PB-1-00002", "Example Guestroom Conversion", "Example Hotel Downtown")
PROJECT_PNW = ("MRC-031-PS-2-00007", "PNW Public Space Refresh", "Sound Hotel Seattle")

WAREHOUSE_DOMAIN = "example-logistics.test"
FREIGHT_DOMAIN = "fedex.com"
VENDOR_DOMAIN = "pbhhospitality.com"
PROPERTY_DOMAIN = "example-pm.test"


def _line(po, num, spec, description, vendor, qty, uom, cost_code, project, agent, terms="Net 30"):
    code, name, ship_to = project
    return {
        "po_number": po, "line_number": num, "spec_code": spec, "description": description,
        "vendor_name": vendor, "unit_of_measure": uom, "qty_ordered": qty, "qty_received": 0.0,
        "cost_code": cost_code, "project_code": code, "project_name": name,
        "line_status": "Open", "expected_date": "2026-07-18", "ship_to": ship_to,
        "assigned_agent": agent, "pay_terms": terms,
    }


# --- The PO lines reconciliation matches against -------------------------------
# Cost codes starting with "1" are material lines (settings.MATERIAL_COST_CODE_PREFIX).

PO_LINES = [
    _line("912456", 1, "STE-402-LT", "Meridian 402 Low Table, walnut", "Northgate Supply", 12, "EA", "1100", PROJECT_EXAMPLE, "T. Turner"),
    _line("912456", 2, "STE-402-UP", "Meridian 402 Upper Shelf Unit", "Northgate Supply", 4, "EA", "1100", PROJECT_EXAMPLE, "T. Turner"),
    _line("912456", 3, "FIT-902-TV", "Harbourt 902 TV Wall Mount, tilting", "Northgate Supply", 24, "EA", "1100", PROJECT_EXAMPLE, "T. Turner"),
    _line("912547", 1, "GR-350a", "Grayson 350 Accent Chair, boucle ivory", "Akula Living", 18, "EA", "1200", PROJECT_PNW, "M. Reyes"),
    _line("912547", 2, "GR-350b", "Grayson 350 Ottoman, boucle ivory", "Akula Living", 18, "EA", "1200", PROJECT_PNW, "M. Reyes"),
    _line("912547", 3, "LI-12", "Linden 12 Floor Lamp, brushed brass", "Akula Living", 30, "EA", "1200", PROJECT_PNW, "M. Reyes"),
    _line("906534", 1, "MR-251a-SGF", "Mirror 251 Seagrass Frame, 36in", "Lakeside Hospitality", 40, "EA", "1300", PROJECT_EXAMPLE, "T. Turner"),
    _line("906534", 2, "MR-252a-SGF", "Mirror 252 Seagrass Frame, 24in", "Lakeside Hospitality", 40, "EA", "1300", PROJECT_EXAMPLE, "T. Turner"),
    _line("998033", 1, "PRT-101", "Construction document set, full size prints", "Example Reprographics", 6, "SET", "1400", PROJECT_EXAMPLE, "T. Turner"),
    _line("913987", 1, "EXT-901-AC", "Exterior Acoustic Cladding Panel", "Cora Seal", 60, "EA", "1500", PROJECT_PNW, "M. Reyes", "CBD"),
    _line("913987", 2, "EXT-902-AC", "Exterior Acoustic Corner Trim", "Cora Seal", 24, "EA", "1500", PROJECT_PNW, "M. Reyes", "CBD"),
    _line("914902", 1, "OSE-410", "OS&E Guestroom Linen Package", "Cintas", 120, "SET", "1600", PROJECT_PNW, "M. Reyes"),
]


def _record(email_id, po, spec, description, vendor, carrier, qty, pod, source, confidence,
            snippet, tracking=None, uom="EA", shipment=None, location=None, comments=None):
    return {
        "source_email_id": email_id, "po_number": po, "shipment_number": shipment,
        "spec_code": spec, "parent_spec_code": None, "sub_spec_suffix": None,
        "item_description": description, "vendor_name": vendor, "carrier_name": carrier,
        "tracking_number": tracking, "quantity_received": qty, "unit_of_measure": uom,
        "pod_stated_date": pod, "email_date": "2026-07-20T09:00:00+00:00",
        "delivery_location": location, "comments": comments, "extraction_source": source,
        "extraction_confidence": confidence, "raw_snippet": snippet,
    }


# --- Records that reconcile cleanly (3 signals, nothing missing) ----------------
# These auto-approve at seed time, exactly as the automation would.

CLEAN_RECORDS = [
    _record("em-1001", "912456", "STE-402-LT", "Meridian 402 Low Table, walnut", "Northgate Supply",
            "FedEx", 12, "2026-07-14", "html", 1.0,
            "PO 912456 | STE-402-LT | Meridian 402 Low Table, walnut | Qty 12 | Received 07/14/26",
            tracking="7749 8821 0043", shipment="SHP-88120", location="Example Hotel Downtown"),
    _record("em-1002", "912547", "LI-12", "Linden 12 Floor Lamp, brushed brass", "Akula Living",
            "Old Dominion", 30, "2026-07-15", "pdf", 0.95,
            "Delivery note 44120 — PO 912547 — LI-12 — Linden 12 Floor Lamp — 30 EA",
            tracking="ODFL-44120", shipment="SHP-44120", location="Sound Hotel Seattle"),
    _record("em-1003", "906534", "MR-251a-SGF", "Mirror 251 Seagrass Frame, 36in", "Lakeside Hospitality",
            "UPS", 40, "2026-07-16", "html", 0.98,
            "PO 906534 / MR-251a-SGF / Mirror 251 Seagrass Frame 36in / 40 EA received",
            tracking="1Z9993AA0012", location="Example Hotel Downtown"),
    _record("em-1004", "998033", "PRT-101", "Construction document set, full size prints",
            "Example Reprographics", "FedEx", 6, "2026-07-13", "excel", 0.92,
            "998033 | PRT-101 | Construction document set | 6 SET | delivered 7/13",
            uom="SET", location="Example Hotel Downtown"),
    _record("em-1005", "913987", "EXT-901-AC", "Exterior Acoustic Cladding Panel", "Cora Seal",
            "RXO", 60, "2026-07-17", "pdf", 0.94,
            "PO 913987 EXT-901-AC Exterior Acoustic Cladding Panel qty 60 POD 17/07/2026",
            tracking="RXO-77341", shipment="SHP-77341", location="Sound Hotel Seattle"),
]


# --- Records that need a person -------------------------------------------------
# Each one exercises a distinct failure mode, so the exception queue shows the full range of
# reasons a reviewer will actually meet.

FLAGGED_RECORDS = [
    # Spec absent and the description only weakly resembles a line -> low confidence.
    _record("em-2001", "912456", None, "Northgate Supply mount", "Northgate Supply", "FedEx", 11,
            "2026-07-14", "ocr", 0.55,
            "scanned POD: PO 912456 ... 11 units ... Northgate Supply mount ... signed 07/14",
            tracking="7749 8821 0044", location="Example Hotel Downtown"),
    # Certain line match, but the vendor never stated a quantity.
    _record("em-2002", "912547", "GR-350a", "Grayson 350 Accent Chair, boucle ivory",
            "Akula Living", "Old Dominion", None, "2026-07-15", "freetext", 0.6,
            "\"Chairs for PO 912547 (GR-350a) arrived this morning\" — no count given"),
    # Certain line match, but no proof-of-delivery date anywhere in the mail.
    _record("em-2003", "906534", "MR-252a-SGF", "Mirror 252 Seagrass Frame, 24in",
            "Lakeside Hospitality", "UPS", 40, None, "html", 0.75,
            "PO 906534 / MR-252a-SGF / 40 EA / (no received date column in table)"),
    # PO number does not exist in the catalogue at all -> nothing to match against.
    _record("em-2004", "999111", None, "Assorted guestroom furniture", "Unknown vendor", None, 5,
            "2026-07-12", "freetext", 0.4,
            "\"5 boxes delivered against PO 999111\" — PO not found in Spitfire"),
    # Three signals and complete, but receiving more than the line has outstanding.
    _record("em-2005", "913987", "EXT-902-AC", "Exterior Acoustic Corner Trim", "Cora Seal",
            "RXO", 30, "2026-07-18", "pdf", 0.9,
            "PO 913987 EXT-902-AC qty 30 delivered 18/07/2026 (line ordered 24)",
            tracking="RXO-77398"),
    # Description matches well but no spec, so the exact line is still unresolved.
    _record("em-2006", "914902", None, "Guestroom linen package", "Cintas", "DHL", 120,
            "2026-07-19", "html", 0.7,
            "PO 914902 — guestroom linen package — 120 SET — delivered 19 Jul", uom="SET"),
    # Spec resolves the line, but there is no description and no POD date.
    _record("em-2007", "912456", "STE-402-UP", None, "Northgate Supply", "FedEx", 4, None, "excel", 0.65,
            "912456 | STE-402-UP | 4 | (description and date columns blank)"),
    # Loose free text: no spec, description below the fuzzy threshold.
    _record("em-2008", "912547", None, "grayson ottoman boucle", "Akula Living", "Old Dominion", 18,
            "2026-07-20", "freetext", 0.45,
            "\"the grayson ottoman boucle came in, 18 of them\" — PO 912547"),
]


def _email(email_id, day, sender, domain, subject, snippet, notification_type, triage_category,
           rule, status_keyword, po=None, attachment=False, reason=""):
    return {
        "email_id": email_id, "received_at": f"2026-07-{day:02d}T08:30:00+00:00",
        "sender_address": sender, "sender_domain": domain, "subject": subject,
        "body_snippet": snippet, "notification_type": notification_type,
        "triage_category": triage_category, "matched_rule": rule, "reason": reason,
        "status_keyword": status_keyword, "po_number": po, "has_attachment": attachment,
    }


# --- The inbox -----------------------------------------------------------------
# One per triage archetype. `status_keyword` drives the delivery report's three buckets;
# `triage_category` drives which folder the organizer proposes.

EMAILS = [
    _email("em-1001", 14, "inbound@example-logistics.test", WAREHOUSE_DOMAIN,
           "WH Inbound - PO 912456 - STE-402-LT - Delivered", "Received 12 EA, signed for 07/14.",
           "warehouse_inbound", "surface", "rule_1_warehouse_table", "delivered", "912456", True),
    _email("em-1002", 15, "inbound@example-logistics.test", WAREHOUSE_DOMAIN,
           "WH Inbound - PO 912547 - Linden floor lamps", "30 EA received, delivery note attached.",
           "warehouse_inbound", "surface", "rule_1_warehouse_table", "delivered", "912547", True),
    _email("em-1003", 16, "inbound@example-logistics.test", WAREHOUSE_DOMAIN,
           "WH Inbound - PO 906534 - Seagrass mirrors", "40 EA received in good condition.",
           "warehouse_inbound", "surface", "rule_1_warehouse_table", "received", "906534", True),
    _email("em-1004", 13, "docs@example-logistics.test", WAREHOUSE_DOMAIN,
           "Inbound notification - PO 998033 print set", "Print set delivered to site.",
           "inbound_notification", "surface", "rule_3_warehouse_no_table", "delivered", "998033"),
    _email("em-1005", 17, "inbound@example-logistics.test", WAREHOUSE_DOMAIN,
           "WH Inbound - PO 913987 - cladding panels", "60 EA cladding panels received.",
           "warehouse_inbound", "surface", "rule_1_warehouse_table", "received", "913987", True),
    _email("em-2001", 14, "scans@example-logistics.test", WAREHOUSE_DOMAIN,
           "Signed POD - PO 912456", "Scanned proof of delivery attached.",
           "warehouse_inbound", "surface", "rule_1_warehouse_table", "delivered", "912456", True),
    _email("em-2002", 15, "frontdesk@example-pm.test", PROPERTY_DOMAIN,
           "RE: PO 912547 - chairs arrived", "Chairs for PO 912547 arrived this morning.",
           "property_confirmation", "hold", "rule_4_property_reply", "received", "912547"),
    _email("em-2003", 16, "ap@pbhhospitality.com", VENDOR_DOMAIN,
           "PO 906534 - shipment confirmation", "Mirrors shipped and delivered, see table.",
           "vendor_confirmation", "hold", "rule_5_vendor_confirmation", "delivered", "906534", True),
    _email("em-2004", 12, "unknown@somewhere.example", "somewhere.example",
           "Delivery today", "5 boxes delivered against PO 999111.",
           "unknown", "route", "rule_6_unknown", "delivered", None, False,
           "no PO reference found — cannot resolve automatically"),
    _email("em-2005", 18, "shipping@coraseal.com", "coraseal.com",
           "PO 913987 - corner trim delivered", "30 corner trims delivered 18/07.",
           "vendor_confirmation", "hold", "rule_5_vendor_confirmation", "delivered", "913987", True),
    _email("em-2006", 19, "service@cintas.com", "cintas.com",
           "PO 914902 - linen package delivered", "120 SET guestroom linen delivered.",
           "vendor_confirmation", "hold", "rule_5_vendor_confirmation", "delivered", "914902"),
    _email("em-2007", 17, "docs@example-logistics.test", WAREHOUSE_DOMAIN,
           "PO 912456 - shelf units", "Spreadsheet attached, some columns blank.",
           "inbound_notification", "surface", "rule_3_warehouse_no_table", "received", "912456", True),
    _email("em-2008", 20, "frontdesk@example-pm.test", PROPERTY_DOMAIN,
           "RE: PO 912547 ottomans", "The grayson ottoman boucle came in, 18 of them.",
           "property_confirmation", "hold", "rule_4_property_reply", "received", "912547"),
    # Freight noise — correctly hidden, never a receiving event.
    _email("em-3001", 13, "tracking@fedex.com", FREIGHT_DOMAIN,
           "FedEx shipment 7749 8821 0043 out for delivery", "Your shipment is out for delivery.",
           "delivered_shipped", "hide", "rule_2_freight_status", "out_for_delivery", "912456", False,
           "intermediate freight status notice, not a true receiving event"),
    _email("em-3002", 14, "tracking@fedex.com", FREIGHT_DOMAIN,
           "FedEx shipment 7749 8821 0043 delivered", "Delivered, signed by front desk.",
           "delivered_shipped", "hide", "rule_2_freight_status", "delivered", "912456", False,
           "intermediate freight status notice, not a true receiving event"),
    _email("em-3003", 16, "tracking@ups.com", "ups.com",
           "UPS 1Z9993AA0012 out for delivery", "Out for delivery today.",
           "delivered_shipped", "hide", "rule_2_freight_status", "out_for_delivery", "906534", False,
           "intermediate freight status notice, not a true receiving event"),
    # Cancellations stay in the inbox — they need a manual PO update, not a receipt.
    _email("em-4001", 15, "purchasing@akulaliving.com", "akulaliving.com",
           "PO 912547 line 2 cancelled", "Ottoman line has been cancelled by the vendor.",
           "order_cancellation", "route", "rule_0_order_cancellation", "cancelled", "912547", False,
           "order cancellation notice — requires manual PO update in Spitfire"),
    _email("em-4002", 18, "purchasing@coraseal.com", "coraseal.com",
           "Cancellation - PO 913987 partial", "Remaining trim quantity is no longer required.",
           "order_cancellation", "route", "rule_0_order_cancellation", "cancelled", "913987", False,
           "order cancellation notice — requires manual PO update in Spitfire"),
]

# Which mails the organizer leaves alone. The inbox should end up holding only confirmations,
# cancellations and anything unclear — everything else is repetitive status noise.
KEEP_IN_INBOX_TYPES = {
    "order_cancellation", "unknown", "property_confirmation", "vendor_confirmation",
}
