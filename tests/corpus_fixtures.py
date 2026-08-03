"""Email fixtures shaped like the real June corpus.

The tests used to build emails with invented grammar — subjects like `Inbound - PO 213987`,
senders at `hospitalitylogistics.com`, tables headed `PO | Spec | Qty`. None of those shapes
occur in Premier's mail, so a green suite proved nothing about production behaviour, and the
placeholder sender domains were themselves the top-ranked go-live blocker in
docs/CODE-ANALYSIS-FINDINGS.md (C6).

Everything here mirrors a real message from `Documents/Premier/5,8 june`, field for field:
the subject grammars, the two-table Inbound layout, the `PO # / Line #` cell, the `n EA -`
item prefix, the forwarding wrapper Premier's expeditors add, and the confirmation-grid
headers. Where a test needs a value the corpus doesn't supply, it is changed from a real one
rather than made up wholesale, so the *shape* always stays honest.
"""

from typing import List, Optional, Sequence

from pipeline.models import Attachment, RawEmail

WAREHOUSING = "warehousing@authoritylogistics.com"
ROUTING = "routing@authoritylogistics.com"
EXPEDITOR = "mariagutierrez@premierpm.com"

# --- Authority Inbound (class A) --------------------------------------------

_INBOUND_HEADER_TABLE = """
<table>
  <tr><td>Received Date:</td><td>{received_date}</td></tr>
  <tr><td>Received at:</td><td>{received_at}</td></tr>
  <tr><td>Received By:</td><td>{received_by}</td></tr>
  <tr><td>Returned:</td><td>No</td></tr>
  <tr><td>ALS Shipment #:</td><td>{shipment}</td></tr>
  <tr><td>Carrier:</td><td>{carrier}</td></tr>
  <tr><td>Tracking:</td><td>{tracking}</td></tr>
  <tr><td>Quantity:</td><td>{package_qty}</td></tr>
  <tr><td>Weight:</td><td>{weight}</td></tr>
</table>
"""

_INBOUND_LINE_HEADER = (
    "<tr><th>PO # / Line #</th><th>Supplier</th><th>Part #</th>"
    "<th>Item</th><th>Package</th><th>Comments</th></tr>"
)

_DELIVERED_HEADER_TEXT = """
<p>Authority #:&nbsp;{notice}</p>
<p>Carrier:&nbsp;{carrier}</p>
<p>Tracking:&nbsp;{tracking}</p>
<p>From:&nbsp;{ship_from}</p>
<p>Delivered:&nbsp;{delivered}</p>
<p>Signed by:&nbsp;{signed_by}</p>
<p>To:&nbsp;{ship_to}</p>
"""

_DELIVERED_LINE_HEADER = "<tr><th>Package</th><th>PO #</th><th>Item</th><th>Supplier</th></tr>"

DEFAULT_WAREHOUSE = "Crown Worldwide Moving &amp; Storage - Mira Loma 4550 Wineville Ave. Unit B Mira Loma, CA 91752"


def inbound_subject(notice: str, po_numbers: Sequence[str], project: str = "2985",
                    project_name: str = "LXR Cameo Beverly Hills (Guestrooms) - MRC Los Angeles CA") -> str:
    return f"[External] {notice} - Inbound Notification - {', '.join(po_numbers)} - {project} : {project_name}"


def delivered_subject(notice: str, po_numbers: Sequence[str], project: Optional[str] = "2978",
                      project_name: str = "LXR Cameo Beverly Hills (Public Space) - MRC") -> str:
    """Note the doubled dash — Authority leaves an empty slot after the label, and that empty
    slot is what distinguishes the Delivered grammar from the Inbound one."""
    tail = f" - {project} - {project_name}" if project else ""
    return f"[External] {notice} - Delivered Notification -  - {', '.join(po_numbers)}{tail}"


def inbound_email(
    *,
    email_id: str = "msg-inbound-1",
    notice: str = "239336",
    po_numbers: Sequence[str] = ("208491",),
    lines: Sequence[dict] = (),
    shipment: str = "50052 : 1",
    received_date: str = "10/01/2025",
    carrier: str = "Nolan Transportation",
    tracking: str = "8840455",
    package_qty: str = "41 CTN",
    forwarded: bool = False,
    attachments: Optional[List[Attachment]] = None,
    received_at: str = "2025-10-01T18:00:00Z",
) -> RawEmail:
    """A class-A Inbound Notification.

    Each entry in `lines` is `{"po": ..., "line": ..., "supplier": ..., "part": ..., "item": ...,
    "package": ..., "comments": ...}`, matching the real column order.
    """
    rows = [_INBOUND_LINE_HEADER]
    for line in lines or [_default_inbound_line(po_numbers[0])]:
        rows.append(
            "<tr>"
            f"<td>{line['po']} : {line['line']}</td>"
            f"<td>{line.get('supplier', 'Light Annex')}</td>"
            f"<td>{line.get('part', '')}</td>"
            f"<td>{line['item']}</td>"
            f"<td>{line.get('package', '')}</td>"
            f"<td>{line.get('comments', '')}</td>"
            "</tr>"
        )

    body = (
        _INBOUND_HEADER_TABLE.format(
            received_date=received_date, received_at=DEFAULT_WAREHOUSE, received_by="Miguel C.",
            shipment=shipment, carrier=carrier, tracking=tracking, package_qty=package_qty,
            weight="1240.00",
        )
        + "<table>" + "".join(rows) + "</table>"
    )
    subject = inbound_subject(notice, po_numbers)
    return _wrap(email_id, WAREHOUSING, subject, body, forwarded, attachments, received_at)


def delivered_email(
    *,
    email_id: str = "msg-delivered-1",
    notice: str = "50009",
    po_numbers: Sequence[str] = ("206725",),
    lines: Sequence[dict] = (),
    delivered: str = "09/15/2025",
    carrier: str = "Nolan Transportation",
    tracking: str = "8801592",
    forwarded: bool = True,
    attachments: Optional[List[Attachment]] = None,
    received_at: str = "2025-09-15T18:00:00Z",
) -> RawEmail:
    """A class-B Delivered Notification — the carrier reached the warehouse door, which is not
    a receiving event on its own."""
    rows = [_DELIVERED_LINE_HEADER]
    for index, line in enumerate(lines or [_default_delivered_line(po_numbers[0])]):
        rows.append(
            "<tr>"
            f"<td>{line.get('package', '9 SKID - 3084.00 lb') if index == 0 else ''}</td>"
            f"<td>{line['po']}</td>"
            f"<td>{line['item']}</td>"
            f"<td>{line.get('supplier', 'Tournesol Siteworks, LLC')}</td>"
            "</tr>"
        )

    body = (
        _DELIVERED_HEADER_TEXT.format(
            notice=notice, carrier=carrier, tracking=tracking,
            ship_from="Tournesol Siteworks : 1540 Leader International Dr, Port Orchard, WA",
            delivered=delivered, signed_by="",
            ship_to="Crown Worldwide Moving &amp; Storage - Mira Loma : 4550 Wineville Ave., Mira Loma, CA",
        )
        + "<table>" + "".join(rows) + "</table>"
    )
    subject = delivered_subject(notice, po_numbers)
    return _wrap(email_id, ROUTING, subject, body, forwarded, attachments, received_at)


def status_report_email(email_id: str = "msg-status-1", project: str = "2978") -> RawEmail:
    """The weekly summary — same sender as the receiver trigger, told apart only by subject."""
    subject = (f"[External] Purchase Order Status Report - Summary : {project}: "
               f"LXR Cameo Beverly Hills (Public Space) - MRC Los Angeles, CA")
    body = "<table><tr><th>PO</th><th>Status</th></tr><tr><td>208491</td><td>Open</td></tr></table>"
    return _wrap(email_id, WAREHOUSING, subject, body, forwarded=True, attachments=None,
                 received_at="2026-06-05T20:38:00Z")


def confirmation_request_email(
    email_id: str = "msg-confirm-1",
    sender: str = "elber@5starinterior.com",
    reply_text: str = "We can confirm that we have only received the Sheer Fabric (GR-350c-WTF) attic stock.",
    rows: Sequence[dict] = (),
) -> RawEmail:
    """A class-D vendor verification: a free-text reply above the request grid Premier sent,
    with the PO/spec/qty living only in the quoted grid."""
    rows = rows or [
        {"description": "Main Drapery Fabric", "spec": "GR-350a-WTF", "uom": "YD", "qty": "196",
         "po": "210634", "vendor": "P. Kaufmann"},
        {"description": "Sheer Fabric", "spec": "GR-350c-WTF", "uom": "YD", "qty": "84",
         "po": "210635", "vendor": "Fil Doux Inc"},
    ]
    grid = ["<tr><th>Description of Item</th><th>SPEC # or Phase Code</th><th>UOM</th>"
            "<th>Qty</th><th>P.O.#</th><th>Vendor</th></tr>"]
    for row in rows:
        grid.append(
            f"<tr><td>{row['description']}</td><td>{row['spec']}</td><td>{row['uom']}</td>"
            f"<td>{row['qty']}</td><td>{row['po']}</td><td>{row['vendor']}</td></tr>"
        )
    body = (
        f"<p>{reply_text}</p>"
        "<p>From:<br/>Gutierrez, Maria &lt;MariaGutierrez@premierpm.com&gt;</p>"
        "<p>Sent:<br/>Wednesday, November 26, 2025 1:35 PM</p>"
        "<p>To:<br/>Yvette &lt;yvette@5starinterior.com&gt;</p>"
        "<p>Subject:<br/>Verification of Fabric Receipt for ATTIC STOCK</p>"
        "<table>" + "".join(grid) + "</table>"
    )
    return _wrap(email_id, sender, "[External] RE: Verification of Fabric Receipt for ATTIC STOCK",
                 body, forwarded=True, attachments=None, received_at="2025-12-01T20:14:00Z")


# --- helpers ----------------------------------------------------------------


def _default_inbound_line(po_number: str) -> dict:
    return {
        "po": po_number, "line": "300", "supplier": "Light Annex", "part": "STE-402-LT-B",
        "item": "11 EA - STE-402-LT-B STE-402-LT-B - BASE, Floor Lamp 2 (Linen Drum Shade) at Sectional",
        "package": "11 CTN - 550.00 lb", "comments": "STE-402-LT",
    }


def _default_delivered_line(po_number: str) -> dict:
    return {
        "po": po_number,
        "item": '2 EACH - EXT-925-AC-Linear Planter w/Pocket(s) 96"Lx30"Wx24"',
        "supplier": "Tournesol Siteworks, LLC",
    }


_FORWARD_WRAPPER = """
<p>{annotation}</p>
<p>Best regards,</p><p>Maria J Gutierrez</p><p>Project Expeditor</p>
<p>14185 Dallas Parkway, Suite 1400 | Dallas, TX 75254</p>
<p>NOTICE: This email contains confidential information solely for the use of the intended
recipient(s). If you are not said recipient your use, disclosure or other distribution of any
information included herewith is STRICTLY PROHIBITED, and you are instructed to notify the
sender immediately and delete this email, all copies and attachments.</p>
<p>From:<br/>{origin} &lt;{origin}&gt;</p>
<p>Sent:<br/>Wednesday, October 1, 2025 1:59 PM</p>
<p>To:<br/>Johnson, Kamilah &lt;kamilahjohnson@premierpm.com&gt;; warehousing@authoritylogistics.com
&lt;warehousing@authoritylogistics.com&gt;; nick.beasley@goarmstrong.com
&lt;nick.beasley@goarmstrong.com&gt;</p>
<p>Subject:<br/>{subject}</p>
"""


def _wrap(
    email_id: str,
    origin_sender: str,
    subject: str,
    body: str,
    forwarded: bool,
    attachments: Optional[List[Attachment]],
    received_at: str,
) -> RawEmail:
    """Wrap the notice in the internal forward Premier's expeditors add, or deliver it direct.

    Both shapes occur in the corpus for the same notification (239336 arrives once directly and
    once forwarded), so every rule has to work either way.
    """
    if forwarded:
        html = _FORWARD_WRAPPER.format(
            annotation="straightforward, WH rec'd", origin=origin_sender,
            subject=subject.replace("[External] ", ""),
        ) + body
        sender = EXPEDITOR
        delivered_subject_line = f"Fw: {subject}"
    else:
        html = body
        sender = origin_sender
        delivered_subject_line = subject

    return RawEmail(
        email_id=email_id,
        received_at=received_at,
        sender_address=sender,
        sender_domain=sender.split("@")[-1],
        subject=delivered_subject_line,
        body_html=html,
        body_text=None,
        attachments=attachments or [],
    )
