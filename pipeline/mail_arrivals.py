"""What is sitting in the mailbox, as distinct from what has been read out of it.

`email_log` answers "what did the orchestrator decide about this email". This answers the cheaper
and much more urgent question "has anything arrived", and it is the whole of the real-time path:
a metadata-only Graph listing writes here in a few hundred milliseconds, the page's version token
changes, and the ten-second poller already in `api/ui/html.py` shows it. The expensive work —
attachments, sniffing, OCR, extraction — happens later on its own schedule and sets `enriched_at`.

Nothing here is a verdict. There is deliberately no category, no rule and no reason column: a row
in this table means "this message exists", and inventing a triage answer for mail nobody has read
is exactly the confusion the two-table split exists to prevent.

Keyed on `internetMessageId`, like every other identity in this system — Graph's own `id` changes
the moment a message is moved. See `connectors/mailbox.py`.
"""
import sqlite3
from datetime import datetime
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

_COLUMNS = ("email_id", "received_at", "sender", "subject", "has_attachments",
            "first_seen_at", "enriched_at", "source_folder")

ID_MATCH_LEN = 255
"""How much of an `internetMessageId` two sides of this system can be sure they agree on.

**Microsoft Graph truncates `internetMessageId` to 255 characters unless `$select` asks for the
message body.** Measured against the live mailbox, same messages, same endpoint, only `$select`
varying:

    $select                                     id length   complete   payload/page
    receivedDateTime,subject,from,…,id            255         no          28 KB     <- the watch
    internetMessageId,bodyPreview                 255         no          30 KB
    internetMessageId,internetMessageHeaders      259         yes      1,204 KB
    internetMessageId,body                        259         yes      9,246 KB     <- the pipeline

So the two writers disagree by construction. `operations/arrivals.py` lists metadata only — that is
the whole reason a 15-second poll is affordable — and records a chopped id here. `connectors/
mailbox.py` selects `body` because it has to triage it, and records the full id in `email_log`.
Comparing the two with `=` reported 26 already-processed messages as unread for ever, and led the
by-id recovery in `stage1_ingest` to declare all 26 deleted from a mailbox they were sitting in.

**Do not "fix" this by adding `body` to the watch's `$select`.** That is 28 KB -> 9,246 KB per page,
330x, every fifteen seconds, and it would cost exactly the property the watch exists to have.
`bodyPreview` is cheap but does not lift the truncation; `internetMessageHeaders` lifts it at 43x
the payload.

255 is therefore not a tuning knob. It is the length Graph itself cuts at.
"""


def match_key(email_id: str) -> str:
    """The comparable part of a message id — see `ID_MATCH_LEN`.

    Identity for every ordinary id, because they are far shorter than 255. Only the pathological
    ones (Teams notifications, at 259) are shortened, and then to exactly what the other side of the
    comparison already holds.
    """
    return (email_id or "")[:ID_MATCH_LEN]


@dataclass(frozen=True)
class Arrival:
    email_id: str
    received_at: str
    sender: str
    subject: str
    has_attachments: bool
    first_seen_at: str
    enriched_at: Optional[str]
    source_folder: str = ""
    """Which mailbox folder listed this message — `inbox` or `junkemail`.

    Not a verdict, which is why it belongs here despite the rule above: it is a fact about where
    the message physically is, known at listing time and costing nothing to record. Defaulted so
    every existing positional constructor, in the tests especially, still works."""

    acknowledged_at: Optional[str] = None
    """When a person accepted that this message is lost — see `acknowledge`."""

    recovery_missing_at: Optional[str] = None
    """When a by-id fetch looked for this message and the mailbox did not have it.

    Carried on the row so the Mail page can tell the two kinds of unread apart. Without it every
    arrival with no verdict reads "waiting for the next run", including mail that has left the
    mailbox and will therefore never be read by any run — a queue that cannot reach zero.

    Still not a verdict: it says what a lookup found on a given day, which is why the page words it
    as that rather than as a flat "gone"."""

    @property
    def is_enriched(self) -> bool:
        return self.enriched_at is not None


def normalise_instant(value: Optional[str]) -> str:
    """One timestamp shape for `received_at`, so comparing two of them means something.

    This column accumulated two formats: 1,606 rows space-separated to minute precision
    (`2026-07-13 13:40`) and 11 in ISO with a Z (`2026-08-28T15:47:18Z`). Mixed, string comparison
    silently inverts, because a space sorts before a `T`:

        '2026-09-03 12:08' < '2026-09-03T11:04:44Z'   ->   True

    12:08 is the later instant. It compares as earlier. That is not hypothetical — it skewed the
    first count of how much mail had fallen behind the listing window, on this very column, which
    is exactly how it would skew a windowing query in the pipeline.

    Normalised to the form the arrivals watermark already parses (`%Y-%m-%dT%H:%M:%SZ`), which
    makes the column lexicographically sortable — the property every query here assumes it had.

    An unparseable value is returned unchanged rather than dropped or defaulted: a timestamp we do
    not understand is still evidence of when something arrived, and inventing one would be worse
    than leaving it odd. `tools/normalise_arrival_instants.py` reports those.
    """
    text = (value or '').strip()
    if not text:
        return ''
    for fmt in ('%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S',
                '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M'):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.strftime('%Y-%m-%dT%H:%M:%SZ')
    return text

def record(conn: sqlite3.Connection, arrivals: Iterable[Arrival], *, now: str) -> int:
    """Upsert a batch and return how many were genuinely new.

    One transaction for the whole batch: this runs every fifteen seconds against a database a
    forty-second ingest pass may be writing at the same time, and a commit per row would multiply
    the chances of colliding with it for no benefit.

    `first_seen_at` is preserved on conflict — it is when *we* first knew, which is the number that
    makes "how far behind is the automation" answerable, and re-stamping it on every poll would
    reset that to now for ever. `enriched_at` is likewise never cleared here; only the pipeline
    sets it, and only `forget` unsets it.
    """
    rows = [
        (a.email_id, normalise_instant(a.received_at), a.sender or "", a.subject or "",
         1 if a.has_attachments else 0, a.first_seen_at or now, a.source_folder or "")
        for a in arrivals if a.email_id
    ]
    if not rows:
        return 0
    known = {r[0] for r in conn.execute(
        "SELECT email_id FROM mail_arrivals WHERE email_id IN "
        f"({', '.join('?' * len(rows))})", [r[0] for r in rows]
    )}
    conn.executemany(
        """
        INSERT INTO mail_arrivals
              (email_id, received_at, sender, subject, has_attachments, first_seen_at,
               source_folder)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(email_id) DO UPDATE SET
              received_at     = excluded.received_at,
              sender          = excluded.sender,
              subject         = excluded.subject,
              has_attachments = excluded.has_attachments,
              -- Updated, not preserved: a message moved out of Junk into the Inbox by a person is
              -- the same message in a new place, and the row should say where it is now.
              source_folder   = excluded.source_folder
        """,
        rows,
    )
    conn.commit()
    return len([r for r in rows if r[0] not in known])


def mark_enriched(conn: sqlite3.Connection, email_id: str, when: str) -> None:
    """Stamp when we noticed the pipeline had read this message. Idempotent, never creates a row.

    An audit stamp, not the authority — see `_PENDING_WHERE`. Whether a message has been read is
    decided by whether `email_log` holds a verdict for it, so this being missing (mail processed
    before the table existed) or stale can never put a processed message back on screen as unread.

    Not creating a row matters: mail read from a source that is not the live Inbox has no arrival,
    and inventing one would claim the watch saw a message it never did.

    Matched on `substr(..., ID_MATCH_LEN)` because the caller is the pipeline, holding a full id,
    while the row was written by the watch, holding a truncated one. With a plain `=` this updated
    **zero rows** for every message whose id runs past 255 characters — silently, since an UPDATE
    that matches nothing is not an error. Harmless in itself (the stamp is not the authority on
    anything; `_PENDING_WHERE` is) but it made the audit column quietly wrong for exactly the
    messages the audit column would have been useful for.

    The id in the row is left alone. Promoting it to the full form would look tidier and would
    break `record()`: that upserts `ON CONFLICT(email_id)`, so the next truncated listing — fifteen
    seconds later — would not conflict and would insert a second row for the same message.
    """
    conn.execute(
        f"UPDATE mail_arrivals SET enriched_at = ? "
        f"WHERE substr(email_id, 1, {ID_MATCH_LEN}) = substr(?, 1, {ID_MATCH_LEN}) "
        f"AND enriched_at IS NULL",
        (when, email_id),
    )
    conn.commit()


# "Has the pipeline read this?" is answered by asking `email_log`, not by trusting `enriched_at`.
#
# The stamp alone was wrong the first time this table was ever filled: the very first poll listed
# 22 messages the pipeline had already processed days earlier, and every one of them rendered
# "not read yet" — because `enriched_at` is only ever set by a *future* run, and those runs had
# already happened. A one-off backfill would have fixed that morning and nothing else; the same
# drift returns whenever a store is copied, an arrival is re-listed after a forget, or the watch
# is enabled on a mailbox with history.
#
# The join cannot drift, because it asks the table that actually holds the verdict. `enriched_at`
# is kept as the audit stamp of *when* we noticed — `email_log.processed_at` answers when it was
# processed, which is a different moment — but it is never the authority on whether.
#
# Joined on `substr(..., ID_MATCH_LEN)` on **both** sides rather than on the ids themselves, because
# the two tables are filled from Graph listings that disagree about how long an id is — see
# `ID_MATCH_LEN`. `substr` is the identity function for every id shorter than 255, so this is the
# same join it always was for all but a handful of rows; for those it is the difference between
# "processed days ago" and "not read yet, for ever".
#
# **Needs `ix_email_log_id_key`** (`pipeline/state_db.py`). Without that expression index SQLite
# cannot use `email_log`'s primary key for this and falls back to a full scan on the inner side:
# measured on the live store at **925ms against 0.7ms**, on a query `/ui/version` runs every ten
# seconds in every open tab. `test_the_pending_join_is_indexed` holds the query plan.
#
# `acknowledged_at` is the one thing other than a verdict that takes a row off this list. A message
# the mailbox no longer has cannot be read by any future run, so without it the count had no way to
# reach zero — and the whole value of the number is that reaching zero means something. It is only
# ever set by a person, and only on a row already known to be gone; see `acknowledge`.
_PENDING_WHERE = f"""
      FROM mail_arrivals a
 LEFT JOIN email_log e
        ON substr(e.email_id, 1, {ID_MATCH_LEN}) = substr(a.email_id, 1, {ID_MATCH_LEN})
     WHERE e.email_id IS NULL
       AND a.acknowledged_at IS NULL
"""


def pending(conn: sqlite3.Connection) -> List[Arrival]:
    """In the mailbox, with no verdict recorded against it — newest first.

    This is the number the Mail page shows as "not processed yet", and it replaces a ~3.2s Graph
    round trip that used to run inside the page render.
    """
    return _query(conn, f"SELECT a.* {_PENDING_WHERE} ORDER BY a.received_at DESC")


def pending_count(conn: sqlite3.Connection) -> int:
    return conn.execute(f"SELECT COUNT(*) {_PENDING_WHERE}").fetchone()[0]


def recoverable_count(conn: sqlite3.Connection) -> int:
    """Unread arrivals that could still be fetched — the number run health should judge on.

    Differs from `pending_count` by excluding messages a recovery already found gone from the
    mailbox, and the distinction is the whole point. `pending_count` answers what the Mail page
    asks: how many arrivals have no verdict. This answers what the automation page asks: how many
    are still the pipeline's problem.

    Conflating them would light the run-health warning for ever on a message nobody can ever
    read, and a warning that cannot clear is one people stop reading. Both counts are kept
    because both questions are real — the Mail page must not silently drop a message just
    because it has gone.
    """
    return conn.execute(
        f"SELECT COUNT(*) {_PENDING_WHERE} AND a.recovery_missing_at IS NULL"
    ).fetchone()[0]

def unreachable(conn: sqlite3.Connection, limit: int) -> List[str]:
    """Ids that arrived, have no verdict, and are not known to be gone — oldest first.

    The work list for `stage1_ingest`'s by-id recovery. `pending()` answers the same question for
    the screen; this answers it for the fetcher, so it returns bare ids, skips messages already
    found missing, and is bounded.

    **Oldest first**, the opposite of `pending()`. On screen the newest arrival is the
    interesting one; for recovery the oldest is the one in most danger of never being read at all,
    and a backlog should drain in the order it accumulated.

    `limit` is required rather than defaulted. Each id costs its own Graph request, so an
    unbounded list turns a large backlog into a request storm inside one run — and the caller is
    the only thing that knows what a run can afford.
    """
    rows = conn.execute(
        f"SELECT a.email_id {_PENDING_WHERE} AND a.recovery_missing_at IS NULL "
        "ORDER BY a.received_at ASC LIMIT ?",
        (limit,),
    ).fetchall()
    return [row[0] for row in rows]


def mark_missing(conn: sqlite3.Connection, email_ids: Sequence[str], at: str) -> None:
    """Record that these ids were looked for by id and were not in the mailbox.

    Stops a deleted message being re-fetched on every run for ever, and — the reason it matters
    more — lets the "arrived but never read" count reach zero. The run-health warning is built on
    that count, and a warning that can never clear is one people learn to ignore.

    Deliberately not an `email_log` verdict. A row there means the pipeline reached a judgement
    about a delivery; "the message is gone" is a fact about the mailbox, and inventing a category
    for it would put a non-delivery into the table every count and report reads from.
    """
    if not email_ids:
        return
    conn.executemany(
        "UPDATE mail_arrivals SET recovery_missing_at = ? WHERE email_id = ?",
        [(at, email_id) for email_id in email_ids],
    )
    conn.commit()

def acknowledge(conn: sqlite3.Connection, email_id: str, at: str) -> int:
    """A person accepting that this message is lost. Returns how many rows it changed.

    **Only a row already marked `recovery_missing_at`.** The action means "I accept this one is
    gone", not "hide this from me": mail still sitting in the mailbox is going to be read by the
    next run, and letting someone dismiss it would turn a self-clearing row into a permanently
    invisible one. The guard is in the SQL rather than the caller, because a route is the wrong
    place to hold an invariant about what this column means.

    Matched on `match_key` like everything else that reaches this table from outside it.

    Nothing in the pipeline calls this. A message is only ever written off by a person, and the
    stamp records that a person did.
    """
    cursor = conn.execute(
        f"UPDATE mail_arrivals SET acknowledged_at = ? "
        f"WHERE substr(email_id, 1, {ID_MATCH_LEN}) = substr(?, 1, {ID_MATCH_LEN}) "
        f"  AND recovery_missing_at IS NOT NULL "
        f"  AND acknowledged_at IS NULL",
        (at, email_id),
    )
    conn.commit()
    return cursor.rowcount


def version_signature(conn: sqlite3.Connection) -> str:
    """`count:newest-first-seen:pending` — what `/ui/version` folds in so the browser notices.

    All three parts earn their place. The count moves when mail arrives; `first_seen_at` moves when
    an arrival is *re*-seen, which the count alone would miss; and the pending count moves when the
    pipeline records a verdict, which is the second update a row makes and the one that clears its
    "not read yet" badge.
    """
    total, newest = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(first_seen_at), '') FROM mail_arrivals").fetchone()
    return f"{total}:{newest}:{pending_count(conn)}"


def get(conn: sqlite3.Connection, email_id: str) -> Optional[Arrival]:
    rows = _query(conn, "SELECT * FROM mail_arrivals WHERE email_id = ?", (email_id,))
    return rows[0] if rows else None


def clear_enrichment(conn: sqlite3.Connection, email_ids: Sequence[str],
                     *, commit: bool = True) -> int:
    """Un-stamp these arrivals: they are back to "in the mailbox, not read".

    The counterpart to `state_db.forget_emails`, and deliberately not a DELETE. Forgetting an email
    means it will be ingested again, so the true state afterwards is exactly what an un-enriched
    arrival says. Deleting the row instead would hide the message from the Mail page until some
    future poll re-listed it — and the arrivals watermark has usually moved past it by then, so for
    older mail that poll never comes and a reprocessed message simply disappears.

    `commit=False` is for `forget_emails`, which erases six tables and one release key as a single
    transaction. Committing in the middle of that would leave a half-forgotten email behind if a
    later step failed.

    Matched on `substr(..., ID_MATCH_LEN)`, like `mark_enriched` and for the same reason: every
    caller holds a pipeline id and every row holds a watch id, and the two differ past 255
    characters. With a plain `=` a forget left the arrival still stamped as read, so a message that
    had just been scheduled for reprocessing did not come back onto the Mail page as unread — and
    the count this returns, which is what `forget_emails` reports to `tools/reprocess_mail.py`,
    said zero rows were touched when it meant "no row matched".
    """
    ids = list(dict.fromkeys(email_ids))
    if not ids:
        return 0
    cursor = conn.execute(
        f"UPDATE mail_arrivals SET enriched_at = NULL "
        f"WHERE substr(email_id, 1, {ID_MATCH_LEN}) IN "
        f"({', '.join('?' * len(ids))})",
        [match_key(i) for i in ids],
    )
    if commit:
        conn.commit()
    return cursor.rowcount


def _query(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> List[Arrival]:
    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return [
            Arrival(
                email_id=row["email_id"],
                received_at=row["received_at"],
                sender=row["sender"],
                subject=row["subject"],
                has_attachments=bool(row["has_attachments"]),
                first_seen_at=row["first_seen_at"],
                enriched_at=row["enriched_at"],
                source_folder=row["source_folder"] or "",
                recovery_missing_at=row["recovery_missing_at"],
                acknowledged_at=row["acknowledged_at"],
            )
            for row in conn.execute(sql, params).fetchall()
        ]
    finally:
        conn.row_factory = prior_factory
