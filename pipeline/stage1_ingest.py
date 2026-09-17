from datetime import datetime, timedelta, timezone
from typing import List, Optional

from config import settings
from connectors.mailbox import Mailbox
from pipeline import mail_arrivals, state_db
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

    for email in _recover_unreachable(mailbox, conn, skip=seen | this_batch):
        if email.email_id in this_batch or state_db.has_seen(conn, email.email_id):
            continue
        this_batch.add(email.email_id)
        fresh.append(email)
    return fresh


def _recover_unreachable(mailbox: Mailbox, conn, skip: set) -> List[RawEmail]:
    """Mail the watermark has left behind, fetched by id instead of by date.

    The listing above asks the server only for mail newer than the last clean run. That is a cost
    optimisation, and it turned into silent data loss: a message the pipeline never settled, but
    whose `receivedDateTime` now sits behind the window, cannot appear in a listing again however
    many times the run repeats.

    Measured on Premier's live store on 2026-09-03: **24 messages** unreadable this way, reaching
    back to 2026-08-31, one of them `URGENT Re: Dorado Beach PO 214336`. The watermark was
    12:04:44Z, the window opened at 11:04:44Z, and every one of the 24 predated it. All 24 were
    sitting in `mail_arrivals` with no `email_log` row, and the Mail page was already printing the
    count — nothing acted on it.

    Widening the window cannot fix this: the loss is by id, so the recovery is by id.

    `skip` is passed so a message the listing already returned is not fetched twice in one run —
    the caller filters again anyway, but a duplicate fetch here costs a Graph request and a full
    attachment download.

    **`skip` is compared on `mail_arrivals.match_key`, not on the ids themselves.** `skip` is built
    from `state_db.seen_ids()` and from this run's listing, both of which hold the full id Graph
    returns when `$select` includes the body; `unreachable()` returns ids the arrival watch wrote,
    truncated at 255 characters. Comparing them raw, a candidate whose id runs past 255 never
    matched anything in `skip` — so a message already settled would be handed to `fetch_by_ids`
    and re-fetched, with its body and every attachment byte, on every run for ever.
    """
    try:
        seen_keys = {mail_arrivals.match_key(i) for i in skip}
        candidates = [i for i in mail_arrivals.unreachable(conn, settings.RECOVER_PER_RUN)
                      if mail_arrivals.match_key(i) not in seen_keys]
    except Exception:                                              # noqa: BLE001
        return []                     # a recovery that cannot list is not worth failing a run over
    if not candidates:
        return []

    result = mailbox.fetch_by_ids(candidates)

    # Only ids the server *answered about and did not have* are recorded as gone, which is why the
    # connector reports them explicitly rather than letting this infer absence from what is
    # missing. Inferring it would mark a message permanently unreadable on any transient failure —
    # an expired token, a throttle, a dropped connection — and the whole point here is that a
    # purchase-order email must not be lost. An id in neither list is simply retried next run.
    #
    # Recording them matters because otherwise a deleted message is re-fetched every run for ever,
    # and `recoverable_count` never reaches zero, so the run-health warning built on it stays
    # permanently lit.
    if result.absent:
        mail_arrivals.mark_missing(
            conn, result.absent, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return result.emails
