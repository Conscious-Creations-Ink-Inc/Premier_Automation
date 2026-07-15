from datetime import datetime, timezone
from typing import List

from connectors.mailbox import Mailbox
from pipeline import state_db
from pipeline.models import RawEmail


def fetch_new_emails(mailbox: Mailbox) -> List[RawEmail]:
    """Pulls everything from the mailbox, then drops anything with a Message-ID already seen —
    the same-email-resent case. This does NOT catch "three different real emails about the same
    delivery" — that's Stage 2's job, deliberately kept separate (see BuildPlan/STAGE_2_ACCUMULATE.md).
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = state_db.get_connection()
    try:
        return [email for email in mailbox.fetch_new() if state_db.is_new_message(conn, email.email_id, now)]
    finally:
        conn.close()
