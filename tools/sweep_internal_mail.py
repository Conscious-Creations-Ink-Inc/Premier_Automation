"""Set aside the internally-authored mail already sitting on the manual queue.

`stage1_triage.INTERNALLY_AUTHORED` stops new internal mail reaching the queue at all. It says
nothing about the mail already here, whose `email_log` rows were written by the rules that ran at
the time. This is the same judgement applied to those.

    python -m tools.sweep_internal_mail --dry-run     # what it would set aside, and what it holds back
    python -m tools.sweep_internal_mail --yes         # write it

**It writes one `mail_overrides` verdict per message, signed `automation`.** That route rather than
back-dating `email_log.not_a_delivery`, for two reasons: a verdict is what retires the message's
records (`read_views._NOT_SET_ASIDE` keys on this table, deliberately not on triage's derived flag),
and every row reverses from `/ui/not-deliveries` like any other.

**What it holds back, and why that is the point.** Authorship does not outrank a rule that
identified real work. `stage1_triage.AUTHORSHIP_YIELDS_TO` names them — cancellations, loss and
claim threads, parsed Authority notices, and photographed evidence nobody has read — and this tool
reads that same set rather than keeping a second copy. It additionally skips any message whose
attachments have never been read, because setting those aside is deciding on no information.

Read-only against Graph, Spitfire and the mailbox. Nothing is deleted.
"""

import argparse
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone

from config import settings
from pipeline import authorship, mail_overrides, read_views, stage1_triage, state_db

UNREAD_CODES = {"needs_ocr", "unreadable", "corrupt", "encrypted"}
"""Queue codes meaning the file has not been read. `attachment_ledger` has recorded a failure or an
outage, so nothing — human or machine — has seen what is inside."""

NOTE = ("bulk sweep: written inside Premier, so it cannot be evidence that Premier's own goods "
        "arrived (was {rule})")


def _classify(conn):
    """`(sweep, held)` — the messages to set aside, and the ones to leave alone with a reason."""
    rules = {r["email_id"]: (r["matched_rule"] or "", r["origin_sender"] or "", r["subject"] or "")
             for r in conn.execute(
                 "SELECT email_id, matched_rule, origin_sender, subject FROM email_log")}

    by_email = {}
    for item in read_views.manual_queue(conn):
        by_email.setdefault(item.email_id, []).append(item)

    already = mail_overrides.ids_with(conn, mail_overrides.NOT_DELIVERY)
    sweep, held = [], []
    for email_id, items in by_email.items():
        rule, origin, subject = rules.get(email_id, ("", "", ""))
        if email_id in already:
            continue
        if not authorship.authored_internally(origin):
            continue
        if stage1_triage._authorship_yields_to(rule):
            held.append((email_id, subject, rule))
            continue
        if any(i.code in UNREAD_CODES for i in items):
            held.append((email_id, subject, "an attachment nobody has read"))
            continue
        sweep.append((email_id, subject, rule, sum(i.rolled_up for i in items), len(items)))
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

    print(f"HELD BACK  {len(held)} message(s) — authorship does not outrank these:")
    for rule, n in Counter(r for _e, _s, r in held).most_common():
        print(f"             {n:>4}  {rule}")

    print(f"\nSET ASIDE  {len(sweep)} message(s), retiring "
          f"{sum(r for _e, _s, _r, r, _n in sweep)} record(s):")
    for rule, n in Counter(r for _e, _s, r, _rec, _n in sweep).most_common():
        print(f"             {n:>4}  was {rule}")
    print("\n  heaviest first:")
    for email_id, subject, rule, recs, _n in sorted(sweep, key=lambda s: -s[3])[:15]:
        print(f"    {recs:>5} records  {subject[:62]}")

    if not writing:
        print("\nNothing was written. Re-run with --yes to apply.")
        return 0

    backup = db_path.with_name(f"{db_path.name}.bak-{datetime.now():%Y%m%d_%H%M%S}")
    shutil.copy2(db_path, backup)
    print(f"\nbackup     {backup.name}")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    for email_id, _subject, rule, _recs, _n in sweep:
        mail_overrides.set_verdict(
            conn, email_id=email_id, verdict=mail_overrides.NOT_DELIVERY,
            decided_by=args.by, at=now, note=NOTE.format(rule=rule or "no rule"))
    print(f"written    {len(sweep)} verdict(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
