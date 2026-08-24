"""Query layer for the vendor chase template and its (mock) send log.

The template is the answer to "we need one format we actually accept": rather than parsing
whatever a vendor happens to send, we ask every under-reporting vendor for exactly the fields
reconciliation needs — PO, spec, description, quantity, POD date, carrier reference.
"""
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

# Placeholders the renderer substitutes. Kept as data so the UI can show the reviewer which
# tokens are available while they edit.
TEMPLATE_PLACEHOLDERS = (
    "{vendor}", "{po_number}", "{spec_code}", "{description}", "{quantity}", "{missing_fields}",
)

DEFAULT_SUBJECT = "Receipt confirmation required — PO {po_number} ({vendor})"

DEFAULT_BODY = """Hello {vendor},

We are reconciling deliveries against PO {po_number} and the confirmation we received does not
contain everything we need to close the receipt ({missing_fields}).

Please reply to this email using the format below — every line is required:

  PO number        : {po_number}
  Spec / item no.  : {spec_code}
  Description      : {description}
  Quantity received: {quantity}
  Date received    : DD/MM/YYYY
  Carrier / BOL no.: (tracking or bill-of-lading reference)
  Delivered to     : (property or warehouse name)

If any item on this PO was short-shipped, damaged or cancelled, say so on its own line rather
than leaving the quantity blank.

Thank you,
Premier Receiving Automation
"""


@dataclass
class VendorTemplate:
    subject: str
    body: str
    schedule_hours: int
    enabled: bool
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


def get_template(conn: sqlite3.Connection) -> VendorTemplate:
    """Returns the saved template, lazily creating the default row on first read so the UI
    always has something to show."""
    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM vendor_template WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO vendor_template (id, subject, body, schedule_hours, enabled) "
                "VALUES (1, ?, ?, 24, 0)",
                (DEFAULT_SUBJECT, DEFAULT_BODY),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM vendor_template WHERE id = 1").fetchone()
        return VendorTemplate(
            subject=row["subject"], body=row["body"], schedule_hours=row["schedule_hours"],
            enabled=bool(row["enabled"]), updated_at=row["updated_at"], updated_by=row["updated_by"],
        )
    finally:
        conn.row_factory = prior_factory


def save_template(
    conn: sqlite3.Connection,
    now: str,
    operator: str,
    subject: Optional[str] = None,
    body: Optional[str] = None,
    schedule_hours: Optional[int] = None,
    enabled: Optional[bool] = None,
) -> VendorTemplate:
    """Partial update — the schedule controls and the editor save through the same row."""
    current = get_template(conn)
    conn.execute(
        "UPDATE vendor_template SET subject = ?, body = ?, schedule_hours = ?, enabled = ?, "
        "updated_at = ?, updated_by = ? WHERE id = 1",
        (
            current.subject if subject is None else subject,
            current.body if body is None else body,
            current.schedule_hours if schedule_hours is None else schedule_hours,
            int(current.enabled if enabled is None else enabled),
            now,
            operator,
        ),
    )
    conn.commit()
    return get_template(conn)


def render(template: VendorTemplate, values: Dict[str, str]) -> Dict[str, str]:
    """Substitutes the placeholders. Unknown tokens are left untouched rather than raising, so
    a reviewer editing the body can never break the send."""
    def substitute(text: str) -> str:
        for token in TEMPLATE_PLACEHOLDERS:
            key = token.strip("{}")
            text = text.replace(token, str(values.get(key, "") or "—"))
        return text

    return {"subject": substitute(template.subject), "body": substitute(template.body)}


_SEND_COLUMNS = (
    "extracted_record_id", "po_number", "sent_to", "subject", "rendered_body", "sent_at",
    "sent_by", "status",
)


def log_send(conn: sqlite3.Connection, values: Dict) -> int:
    """Records a simulated send. No mail leaves the machine — Graph has no send permission and
    this is demo data."""
    placeholders = ", ".join(["?"] * len(_SEND_COLUMNS))
    cursor = conn.execute(
        f"INSERT INTO vendor_send_log ({', '.join(_SEND_COLUMNS)}) VALUES ({placeholders})",
        tuple(values.get(column) for column in _SEND_COLUMNS),
    )
    conn.commit()
    return cursor.lastrowid


def list_sends(conn: sqlite3.Connection, limit: int = 50) -> List[Dict]:
    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM vendor_send_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.row_factory = prior_factory
