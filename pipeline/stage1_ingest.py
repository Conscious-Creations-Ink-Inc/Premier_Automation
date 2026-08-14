from datetime import datetime, timedelta, timezone
from typing import List, Optional

from config import settings
from connectors.mailbox import Mailbox
from pipeline import state_db
from pipeline.models import RawEmail


def listing_window_start(conn) -> Optional[str]:
    """How far back a poll should ask the server to look — the watermark, backdated.

    The overlap is the whole point. A watermark used as an exact cut-off loses any message whose
    `receivedDateTime` lands before it but which arrives after: clock skew between Exchange hops,
    a message delayed in transit, or simply two messages in the same second where the run stopped
    between them. Backdating costs a handful of already-seen rows in the listing, each rejected by
    `skip_ids` for the price of a set lookup. Getting it wrong the other way loses mail silently
    and for good, which is the one failure this pipeline must not have.

    None before the first successful run, which asks for the whole Inbox exactly once.
    """
    watermark = state_db.get_watermark(conn)
    if not watermark:
        return None
    try:
        parsed = datetime.fromisoformat(watermark.replace("Z", "+00:00"))
    except ValueError:
        return None                      # unparseable: read everything rather than read nothing
    start = parsed.astimezone(timezone.utc) - timedelta(minutes=settings.INGEST_OVERLAP_MINUTES)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_new_emails(mailbox: Mailbox, conn=None) -> List[RawEmail]:
    """Mail we have not already settled, with the seen ids handed to the mailbox so it can avoid
    fetching them at all.

    Drops anything with a Message-ID already seen — the same-email-resent case. This does NOT catch
    "three different real emails about the same delivery" — that's Stage 2's job, deliberately kept
    separate (see BuildPlan/STAGE_2_ACCUMULATE.md).

    **The seen set goes in, rather than the results being filtered on the way out.** Filtering
    afterwards is correct and was ruinously slow: `GraphMailbox` downloads a body and every
    attachment byte per message as it builds each `RawEmail`, so a poll of this mailbox pulled 18
    messages and 15 MB of attachments — about forty seconds — and then discarded nearly all of it,
    every run, including on a five-minute schedule. Connectors treat `skip_ids` as advisory, so the
    belt-and-braces check below stays: correctness does not depend on the connector honouring it.

    Filtering only *reads* the seen-marker; `process_new_mail` writes it once the email has a
    verdict. Marking here — which is what this did — meant a poll that died halfway through left
    the rest of its mail marked processed but never processed. That is worse against a live
    mailbox in read-only shadow mode, where nothing is moved out of the Inbox and this marker is
    the only thing standing between us and reprocessing everything.

    `conn` is required, not optional: it names which of the two stores this run belongs to
    (Premier's live mail, or the .msg test corpus). A default here would mean a caller that
    forgot silently checked the live store's seen-markers against sample mail.
    """
    if conn is None:
        raise ValueError("conn is required: fetch_new_emails must be told which store to read")
    seen = state_db.seen_ids(conn)
    # `this_batch` covers the same id appearing twice in one fetch: nothing is marked seen
    # until later, so the database cannot yet tell the copies apart.
    this_batch: set = set()
    fresh: List[RawEmail] = []
    for email in mailbox.fetch_new(skip_ids=seen, since=listing_window_start(conn)):
        if email.email_id in this_batch or state_db.has_seen(conn, email.email_id):
            continue
        this_batch.add(email.email_id)
        fresh.append(email)
    return fresh
