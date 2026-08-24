"""The synthetic dataset, written out explicitly rather than randomly generated.

Explicit fixtures beat a random generator here: the demo has to tell a coherent story (these
five reconciled themselves, those eight need a person, and *this* is why), the tests assert
against known rows, and a screenshot taken today matches one taken next month.

Everything is modelled on the real artefacts in the discovery docs — Peerless-AV delivery
notes, seagrass mirrors, the Hilton LXR conversion — with PO numbers that satisfy
`settings.PO_TOKEN_REGEX` and spec codes that satisfy `settings.SPEC_TOKEN_REGEX`.
"""

PROJECT_LXR = ("MRC-024-PB-1-00002", "Hilton LXR Guestroom Conversion", "LXR Cameo Beverly Hills")
PROJECT_PNW = ("MRC-031-PS-2-00007", "PNW Public Space Refresh", "Sound Hotel Seattle")

WAREHOUSE_DOMAIN = "authoritylogistics.com"
FREIGHT_DOMAIN = "fedex.com"
VENDOR_DOMAIN = "pbhhospitality.com"
PROPERTY_DOMAIN = "premierpm.com"


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
    _line("212456", 1, "STE-402-LT", "Steelcase 402 Low Table, walnut", "Peerless-AV", 12, "EA", "1100", PROJECT_LXR, "T. Turner"),
    _line("212456", 2, "STE-402-UP", "Steelcase 402 Upper Shelf Unit", "Peerless-AV", 4, "EA", "1100", PROJECT_LXR, "T. Turner"),
    _line("212456", 3, "FIT-902-TV", "Fitzgerald 902 TV Wall Mount, tilting", "Peerless-AV", 24, "EA", "1100", PROJECT_LXR, "T. Turner"),
    _line("212547", 1, "GR-350a", "Grayson 350 Accent Chair, boucle ivory", "Akula Living", 18, "EA", "1200", PROJECT_PNW, "M. Reyes"),
    _line("212547", 2, "GR-350b", "Grayson 350 Ottoman, boucle ivory", "Akula Living", 18, "EA", "1200", PROJECT_PNW, "M. Reyes"),
    _line("212547", 3, "LI-12", "Linden 12 Floor Lamp, brushed brass", "Akula Living", 30, "EA", "1200", PROJECT_PNW, "M. Reyes"),
    _line("206534", 1, "MR-251a-SGF", "Mirror 251 Seagrass Frame, 36in", "PBH Hospitality", 40, "EA", "1300", PROJECT_LXR, "T. Turner"),
    _line("206534", 2, "MR-252a-SGF", "Mirror 252 Seagrass Frame, 24in", "PBH Hospitality", 40, "EA", "1300", PROJECT_LXR, "T. Turner"),
    _line("198033", 1, "PRT-101", "Construction document set, full size prints", "Precision Reprographics", 6, "SET", "1400", PROJECT_LXR, "T. Turner"),
    _line("213987", 1, "EXT-901-AC", "Exterior Acoustic Cladding Panel", "Cora Seal", 60, "EA", "1500", PROJECT_PNW, "M. Reyes", "CBD"),
    _line("213987", 2, "EXT-902-AC", "Exterior Acoustic Corner Trim", "Cora Seal", 24, "EA", "1500", PROJECT_PNW, "M. Reyes", "CBD"),
    _line("214902", 1, "OSE-410", "OS&E Guestroom Linen Package", "Cintas", 120, "SET", "1600", PROJECT_PNW, "M. Reyes"),
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
    _record("em-1001", "212456", "STE-402-LT", "Steelcase 402 Low Table, walnut", "Peerless-AV",
            "FedEx", 12, "2026-07-14", "html", 1.0,
            "PO 212456 | STE-402-LT | Steelcase 402 Low Table, walnut | Qty 12 | Received 07/14/26",
            tracking="7749 8821 0043", shipment="SHP-88120", location="LXR Cameo Beverly Hills"),
    _record("em-1002", "212547", "LI-12", "Linden 12 Floor Lamp, brushed brass", "Akula Living",
            "Old Dominion", 30, "2026-07-15", "pdf", 0.95,
            "Delivery note 44120 — PO 212547 — LI-12 — Linden 12 Floor Lamp — 30 EA",
            tracking="ODFL-44120", shipment="SHP-44120", location="Sound Hotel Seattle"),
    _record("em-1003", "206534", "MR-251a-SGF", "Mirror 251 Seagrass Frame, 36in", "PBH Hospitality",
            "UPS", 40, "2026-07-16", "html", 0.98,
            "PO 206534 / MR-251a-SGF / Mirror 251 Seagrass Frame 36in / 40 EA received",
            tracking="1Z9993AA0012", location="LXR Cameo Beverly Hills"),
    _record("em-1004", "198033", "PRT-101", "Construction document set, full size prints",
            "Precision Reprographics", "FedEx", 6, "2026-07-13", "excel", 0.92,
            "198033 | PRT-101 | Construction document set | 6 SET | delivered 7/13",
            uom="SET", location="LXR Cameo Beverly Hills"),
    _record("em-1005", "213987", "EXT-901-AC", "Exterior Acoustic Cladding Panel", "Cora Seal",
            "RXO", 60, "2026-07-17", "pdf", 0.94,
            "PO 213987 EXT-901-AC Exterior Acoustic Cladding Panel qty 60 POD 17/07/2026",
            tracking="RXO-77341", shipment="SHP-77341", location="Sound Hotel Seattle"),
]


# --- Records that need a person -------------------------------------------------
# Each one exercises a distinct failure mode, so the exception queue shows the full range of
# reasons a reviewer will actually meet.

FLAGGED_RECORDS = [
    # Spec absent and the description only weakly resembles a line -> low confidence.
    _record("em-2001", "212456", None, "Peerless AV mount", "Peerless-AV", "FedEx", 11,
            "2026-07-14", "ocr", 0.55,
            "scanned POD: PO 212456 ... 11 units ... Peerless AV mount ... signed 07/14",
            tracking="7749 8821 0044", location="LXR Cameo Beverly Hills"),
    # Certain line match, but the vendor never stated a quantity.
    _record("em-2002", "212547", "GR-350a", "Grayson 350 Accent Chair, boucle ivory",
            "Akula Living", "Old Dominion", None, "2026-07-15", "freetext", 0.6,
            "\"Chairs for PO 212547 (GR-350a) arrived this morning\" — no count given"),
    # Certain line match, but no proof-of-delivery date anywhere in the mail.
    _record("em-2003", "206534", "MR-252a-SGF", "Mirror 252 Seagrass Frame, 24in",
            "PBH Hospitality", "UPS", 40, None, "html", 0.75,
            "PO 206534 / MR-252a-SGF / 40 EA / (no received date column in table)"),
    # PO number does not exist in the catalogue at all -> nothing to match against.
    _record("em-2004", "999111", None, "Assorted guestroom furniture", "Unknown vendor", None, 5,
            "2026-07-12", "freetext", 0.4,
            "\"5 boxes delivered against PO 999111\" — PO not found in Spitfire"),
    # Three signals and complete, but receiving more than the line has outstanding.
    _record("em-2005", "213987", "EXT-902-AC", "Exterior Acoustic Corner Trim", "Cora Seal",
            "RXO", 30, "2026-07-18", "pdf", 0.9,
            "PO 213987 EXT-902-AC qty 30 delivered 18/07/2026 (line ordered 24)",
            tracking="RXO-77398"),
    # Description matches well but no spec, so the exact line is still unresolved.
    _record("em-2006", "214902", None, "Guestroom linen package", "Cintas", "DHL", 120,
            "2026-07-19", "html", 0.7,
            "PO 214902 — guestroom linen package — 120 SET — delivered 19 Jul", uom="SET"),
    # Spec resolves the line, but there is no description and no POD date.
    _record("em-2007", "212456", "STE-402-UP", None, "Peerless-AV", "FedEx", 4, None, "excel", 0.65,
            "212456 | STE-402-UP | 4 | (description and date columns blank)"),
    # Loose free text: no spec, description below the fuzzy threshold.
    _record("em-2008", "212547", None, "grayson ottoman boucle", "Akula Living", "Old Dominion", 18,
            "2026-07-20", "freetext", 0.45,
            "\"the grayson ottoman boucle came in, 18 of them\" — PO 212547"),
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
    _email("em-1001", 14, "inbound@authoritylogistics.com", WAREHOUSE_DOMAIN,
           "WH Inbound - PO 212456 - STE-402-LT - Delivered", "Received 12 EA, signed for 07/14.",
           "warehouse_inbound", "surface", "rule_1_warehouse_table", "delivered", "212456", True),
    _email("em-1002", 15, "inbound@authoritylogistics.com", WAREHOUSE_DOMAIN,
           "WH Inbound - PO 212547 - Linden floor lamps", "30 EA received, delivery note attached.",
           "warehouse_inbound", "surface", "rule_1_warehouse_table", "delivered", "212547", True),
    _email("em-1003", 16, "inbound@authoritylogistics.com", WAREHOUSE_DOMAIN,
           "WH Inbound - PO 206534 - Seagrass mirrors", "40 EA received in good condition.",
           "warehouse_inbound", "surface", "rule_1_warehouse_table", "received", "206534", True),
    _email("em-1004", 13, "docs@authoritylogistics.com", WAREHOUSE_DOMAIN,
           "Inbound notification - PO 198033 print set", "Print set delivered to site.",
           "inbound_notification", "surface", "rule_3_warehouse_no_table", "delivered", "198033"),
    _email("em-1005", 17, "inbound@authoritylogistics.com", WAREHOUSE_DOMAIN,
           "WH Inbound - PO 213987 - cladding panels", "60 EA cladding panels received.",
           "warehouse_inbound", "surface", "rule_1_warehouse_table", "received", "213987", True),
    _email("em-2001", 14, "scans@authoritylogistics.com", WAREHOUSE_DOMAIN,
           "Signed POD - PO 212456", "Scanned proof of delivery attached.",
           "warehouse_inbound", "surface", "rule_1_warehouse_table", "delivered", "212456", True),
    _email("em-2002", 15, "frontdesk@premierpm.com", PROPERTY_DOMAIN,
           "RE: PO 212547 - chairs arrived", "Chairs for PO 212547 arrived this morning.",
           "property_confirmation", "hold", "rule_4_property_reply", "received", "212547"),
    _email("em-2003", 16, "ap@pbhhospitality.com", VENDOR_DOMAIN,
           "PO 206534 - shipment confirmation", "Mirrors shipped and delivered, see table.",
           "vendor_confirmation", "hold", "rule_5_vendor_confirmation", "delivered", "206534", True),
    _email("em-2004", 12, "unknown@somewhere.example", "somewhere.example",
           "Delivery today", "5 boxes delivered against PO 999111.",
           "unknown", "route", "rule_6_unknown", "delivered", None, False,
           "no PO reference found — cannot resolve automatically"),
    _email("em-2005", 18, "shipping@coraseal.com", "coraseal.com",
           "PO 213987 - corner trim delivered", "30 corner trims delivered 18/07.",
           "vendor_confirmation", "hold", "rule_5_vendor_confirmation", "delivered", "213987", True),
    _email("em-2006", 19, "service@cintas.com", "cintas.com",
           "PO 214902 - linen package delivered", "120 SET guestroom linen delivered.",
           "vendor_confirmation", "hold", "rule_5_vendor_confirmation", "delivered", "214902"),
    _email("em-2007", 17, "docs@authoritylogistics.com", WAREHOUSE_DOMAIN,
           "PO 212456 - shelf units", "Spreadsheet attached, some columns blank.",
           "inbound_notification", "surface", "rule_3_warehouse_no_table", "received", "212456", True),
    _email("em-2008", 20, "frontdesk@premierpm.com", PROPERTY_DOMAIN,
           "RE: PO 212547 ottomans", "The grayson ottoman boucle came in, 18 of them.",
           "property_confirmation", "hold", "rule_4_property_reply", "received", "212547"),
    # Freight noise — correctly hidden, never a receiving event.
    _email("em-3001", 13, "tracking@fedex.com", FREIGHT_DOMAIN,
           "FedEx shipment 7749 8821 0043 out for delivery", "Your shipment is out for delivery.",
           "delivered_shipped", "hide", "rule_2_freight_status", "out_for_delivery", "212456", False,
           "intermediate freight status notice, not a true receiving event"),
    _email("em-3002", 14, "tracking@fedex.com", FREIGHT_DOMAIN,
           "FedEx shipment 7749 8821 0043 delivered", "Delivered, signed by front desk.",
           "delivered_shipped", "hide", "rule_2_freight_status", "delivered", "212456", False,
           "intermediate freight status notice, not a true receiving event"),
    _email("em-3003", 16, "tracking@ups.com", "ups.com",
           "UPS 1Z9993AA0012 out for delivery", "Out for delivery today.",
           "delivered_shipped", "hide", "rule_2_freight_status", "out_for_delivery", "206534", False,
           "intermediate freight status notice, not a true receiving event"),
    # Cancellations stay in the inbox — they need a manual PO update, not a receipt.
    _email("em-4001", 15, "purchasing@akulaliving.com", "akulaliving.com",
           "PO 212547 line 2 cancelled", "Ottoman line has been cancelled by the vendor.",
           "order_cancellation", "route", "rule_0_order_cancellation", "cancelled", "212547", False,
           "order cancellation notice — requires manual PO update in Spitfire"),
    _email("em-4002", 18, "purchasing@coraseal.com", "coraseal.com",
           "Cancellation - PO 213987 partial", "Remaining trim quantity is no longer required.",
           "order_cancellation", "route", "rule_0_order_cancellation", "cancelled", "213987", False,
           "order cancellation notice — requires manual PO update in Spitfire"),
]

# Which mails the organizer leaves alone. The inbox should end up holding only confirmations,
# cancellations and anything unclear — everything else is repetitive status noise.
KEEP_IN_INBOX_TYPES = {
    "order_cancellation", "unknown", "property_confirmation", "vendor_confirmation",
}
