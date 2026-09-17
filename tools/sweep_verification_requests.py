"""Set aside the bare "did this arrive?" mail already sitting on the manual queue.

`stage1_triage.rule_2a_verification_no_reference` stops new ones reaching the queue at all. It says
nothing about the 800-odd already here, whose `email_log` rows were written by the rule that ran at
the time. This is the same judgement applied to those.

    python -m tools.sweep_verification_requests --dry-run   # what it would set aside, and what it holds back
    python -m tools.sweep_verification_requests --yes       # write it

**One `mail_overrides` verdict per message, signed `automation`** — the same route
`sweep_internal_mail` takes, and for the same reason: a verdict is what retires the message's
records (`read_views._NOT_SET_ASIDE` keys on this table, not on triage's derived flag), and every
row reverses from `/ui/not-deliveries`.

**What it holds back is the whole point.** Premier's rule is "not a delivery *unless* the thread or
a reply carries a purchase order or a delivery". So a message is held back — left on the queue —
when any of these is true:

* `email_log.po_hints` names a purchase order anywhere in the thread or its attachments
* the message already produced an `extracted_records` row, or an `accumulation` row
* it carries a non-inline attachment. A file nobody could read may still hold the PO, and deciding
  on it unread is deciding on no information.

Read-only against Graph, Spitfire and the mailbox. Nothing is deleted; the database is backed up
before the first write.
"""

import argparse
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone

from config import settings
from pipeline import mail_overrides, state_db

RULE = "rule_2a_verification_request"
"""The verdict these messages were given. Its sibling `rule_2a_verification_no_reference` is the
same judgement made at triage time, and needs no sweep."""

NOTE = ("bulk sweep: asks whether goods were received and names no purchase order and attaches "
        "nothing — a question, not delivery mail")


def _classify(conn):
    """`(sweep, held)` — the messages to set aside, and the ones to leave alone with a reason."""
    already = mail_overrides.ids_with(conn, mail_overrides.NOT_DELIVERY)
    sweep, held = [], []
    for r in conn.execute(
            """SELECT e.email_id, e.subject, e.po_hints,
                      (SELECT COUNT(*) FROM extracted_records x
                        WHERE x.source_email_id = e.email_id) AS records,
                      (SELECT COUNT(*) FROM accumulation a
                        WHERE a.email_id = e.email_id) AS accumulated,
                      (SELECT COUNT(*) FROM attachment_ledger t
                        WHERE t.email_id = e.email_id
                          AND COALESCE(t.is_inline, 0) = 0) AS attachments
                 FROM email_log e
                WHERE e.matched_rule = ?
                ORDER BY e.processed_at DESC""", (RULE,)):
        if r["email_id"] in already:
            continue
        subject = r["subject"] or r["email_id"]
        if (r["po_hints"] or "").strip():
            held.append((subject, "names a purchase order"))
        elif r["records"] or r["accumulated"]:
            held.append((subject, "already produced a delivery record"))
        elif r["attachments"]:
            held.append((subject, "carries an attachment that may name one"))
        else:
            sweep.append((r["email_id"], subject))
    return sweep, held


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="report and change nothing (the default)")
    parser.add_argument("--yes", action="store_true", help="required to actually write")
    parser.add_argument("--by", default="automation", help="what to sign the verdicts as")
    parser.add_argument("--limit", type=int, default=0, help="stop after this many messages")
    args = parser.parse_args()

    writing = args.yes and not args.dry_run
    db_path = settings.PIPELINE_STATE_DB_PATH
    conn = state_db.get_connection(db_path)
    conn.row_factory = sqlite3.Row

    sweep, held = _classify(conn)
    if args.limit:
        sweep = sweep[:args.limit]

    print(f"database   {db_path}")
    print(f"mode       {'WRITING' if writing else 'dry run — nothing will be written'}")
    print(f"signed as  {args.by}\n")

    print(f"HELD BACK  {len(held)} message(s) — the thread carries something to receive against:")
    for reason, n in Counter(reason for _s, reason in held).most_common():
        print(f"             {n:>4}  {reason}")

    print(f"\nSET ASIDE  {len(sweep)} message(s):")
    for _email_id, subject in sweep[:15]:
        print(f"             {subject[:70]}")
    if len(sweep) > 15:
        print(f"             … and {len(sweep) - 15} more")

    if not writing:
        print("\nNothing was written. Re-run with --yes to apply.")
        return 0

    backup = db_path.with_name(f"{db_path.name}.bak-{datetime.now():%Y%m%d_%H%M%S}")
    shutil.copy2(db_path, backup)
    print(f"\nbackup     {backup.name}")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    for email_id, _subject in sweep:
        mail_overrides.set_verdict(
            conn, email_id=email_id, verdict=mail_overrides.NOT_DELIVERY,
            decided_by=args.by, at=now, note=NOTE)
    print(f"written    {len(sweep)} verdict(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
