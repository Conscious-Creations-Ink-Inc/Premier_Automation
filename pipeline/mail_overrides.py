"""A person's answer to "is this a delivery notification", where they disagree with the rules.

`stage1_triage` decides this for every message and `email_log.not_a_delivery` records what it
decided. It is wrong in both directions often enough to matter: a thread whose every hop declines to
say goods arrived is sometimes the mail that carries the proof, and a message nobody could classify
is sometimes plainly a calendar invite. Until now triage was the only opinion in the system, and the
"Not a delivery mail" table on `/ui/manual` invited people to "open it and say so" with nothing to
say it *with*.

**A table of its own rather than a column on `email_log`.** `email_log.record` is `INSERT OR REPLACE`
over a fixed column tuple, so any column outside that tuple is erased the next time the message is
processed — `handled_manually` already lives with that. And `not_a_delivery` is *derived* by triage
on every pass and must stay derived, or the rules stop being inspectable. A human verdict has to
outlive both, so it is kept beside them instead of inside them.

**Usually a person, and sometimes a sweep.** `decided_by` is required and free text, and
`tools/sweep_internal_mail.py` signs `automation` across hundreds of rows at once — internally
authored mail, which by `stage1_triage.INTERNALLY_AUTHORED` cannot be evidence that Premier's own
goods arrived. That is a widening of what this table holds, recorded here so the next reader does
not conclude somebody hand-decided 763 messages. The `note` on each such row names the sweep and
the rule that had caught the message, and any one of them reverses from the UI like any other.

One current answer per message, never history. Flipping a verdict overwrites the row, because that
is what a reversal is: a new decision, possibly by a different person, and the name on it should be
the name of whoever decided the thing that is true now. There is no `clear` — flipping is the
reversal, and `state_db.forget_emails` is the only thing that deletes from here.

Ids are compared whole, with no `mail_arrivals.match_key` truncation. Every id that reaches this
table came off an `email_log` row on a page that read `email_log`, so both sides hold the same full
id; the truncation exists only where Graph's 255-character listing meets the pipeline's full one.
"""
import sqlite3
from dataclasses import dataclass
from typing import Optional, Set

NOT_DELIVERY = "not_delivery"
"""A person says this message is not a delivery notification, whatever triage made of it."""

DELIVERY = "delivery"
"""A person says this message *is* one, and triage set it aside wrongly."""

CONFIRMED = "confirmed"
"""A person has confirmed the goods this message describes actually arrived.

**A different question from `DELIVERY`, deliberately not folded into it.** `DELIVERY` answers "is
this a delivery notification at all", which is about the message's *kind*; this answers "did the
delivery happen", which is about the world. Premier's own expediting reports are unambiguously
delivery mail — they are about deliveries, they carry purchase orders and item lines — and they
assert nothing about arrival, because they are written internally to ask. Overloading `DELIVERY`
would have made those two states inexpressible at once, and a person confirming receipt would
have been recorded as having reclassified the mail.

Nothing sets this automatically. `intent.resolve_thread` can read an external reply that states
receipt, and that judgement is what once staged three receipts for goods a property had explicitly
said did not arrive — so a person signs for it here until that path is separately proven.
"""

VERDICTS = (NOT_DELIVERY, DELIVERY, CONFIRMED)

_COLUMNS = ("email_id", "verdict", "decided_by", "note", "decided_at")


@dataclass(frozen=True)
class Override:
    email_id: str
    verdict: str
    decided_by: str
    note: str
    """Why, in the words of whoever decided. Optional, and the most useful thing on the row when
    somebody reads it back in three months."""
    decided_at: str


def set_verdict(conn: sqlite3.Connection, *, email_id: str, verdict: str, decided_by: str,
                at: str, note: str = "") -> None:
    """Record one person's verdict on one message, replacing any verdict already on it.

    Every field is overwritten, not merged: a reversal is a fresh decision, and leaving the first
    person's name against the second person's verdict would misattribute it. Re-submitting the same
    verdict is therefore how a name or a note gets corrected, and is otherwise harmless — which is
    also what makes a double-submit safe.

    Raises rather than defaulting on both an unknown verdict and an unsigned one. An unsigned verdict
    is the single thing this table must not hold, and that invariant belongs here rather than only in
    the route that happens to call it today — the same reasoning `mail_arrivals.acknowledge` gives
    for keeping its own guard in the SQL.
    """
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r} — expected one of {VERDICTS}")
    decided_by = (decided_by or "").strip()
    if not decided_by:
        raise ValueError("a verdict has to say who reached it")

    conn.execute(
        f"INSERT INTO mail_overrides ({', '.join(_COLUMNS)}) VALUES (?, ?, ?, ?, ?) "
        f"ON CONFLICT(email_id) DO UPDATE SET "
        f"  verdict = excluded.verdict, decided_by = excluded.decided_by, "
        f"  note = excluded.note, decided_at = excluded.decided_at",
        (email_id, verdict, decided_by, (note or "").strip(), at),
    )
    conn.commit()


def get(conn: sqlite3.Connection, email_id: str) -> Optional[Override]:
    """The verdict standing on this message, or None where nobody has given one."""
    prior = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM mail_overrides WHERE email_id = ?",
            (email_id,)).fetchone()
    finally:
        conn.row_factory = prior
    return Override(**{col: row[col] for col in _COLUMNS}) if row is not None else None


def ids_with(conn: sqlite3.Connection, verdict: str) -> Set[str]:
    """Every message carrying this verdict, as a set to test membership against.

    One query for a page that would otherwise ask per row: `read_views.manual_queue` builds
    thousands of items and has to drop the ones a person set aside, and the only thing it needs to
    know about each is whether the id is in here.
    """
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r} — expected one of {VERDICTS}")
    return {row[0] for row in conn.execute(
        "SELECT email_id FROM mail_overrides WHERE verdict = ?", (verdict,))}


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM mail_overrides").fetchone()[0]
