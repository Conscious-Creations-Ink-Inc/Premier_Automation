"""What `rule_5e_bulk_mail_noise` would have done to mail already in the store, and nothing else.

A triage rule only ever applies to mail that arrives *next*: `seen_message_ids` stops anything being
processed twice, so the messages already sitting on the queue as `rule_7_unknown` keep the verdict
they were given. That is correct, and it means shipping the rule tells you nothing about whether it
works on Premier's real mail.

This answers that question without changing anything. It re-reads the selected messages, runs the
real `stage1_triage.triage` over them, and prints which ones the new rule would take and — more
useful — which guard stopped it on the rest.

**It is not `tools/reprocess_mail.py`.** That tool erases `email_log`, `attachment_ledger`,
`accumulation`, `extracted_records` and `deliveries` for the mail it selects, clears any human
verdict in `mail_overrides`, and rewinds the watermark so Graph is asked for it all again. Applying
this rule retroactively means running that, and it is a destructive decision for a person to take
deliberately. This tool writes nothing.

    python -m tools.preview_bulk_mail_rule                        # the normal use
    python -m tools.preview_bulk_mail_rule --limit 50
    python -m tools.preview_bulk_mail_rule --since 2026-08-01
    python -m tools.preview_bulk_mail_rule --out d:/Premier/dev_reports
    python -m tools.preview_bulk_mail_rule --cache-bodies --yes    # the only writing mode there is

**A dry run is free of spend but not free of reads.** Bodies are not kept at ingest — `mail_body`
holds only what somebody has opened in the UI — so anything not already cached costs one Graph
request. The count is printed before any of them are made.

No OCR is reachable from here: it calls `triage(email, evidence=None)` and never `evidence.gather`,
so no adapter and no paid client is ever constructed.
"""

import argparse
import csv
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from config import settings
from pipeline import mail_cache, stage1_triage, state_db
from pipeline.models import RawEmail
from pipeline.stage1_triage import triage

RULE = "rule_5e_bulk_mail_noise"
DEFAULT_OUT = Path("d:/Premier/dev_reports")


def _select(conn, rule: str, since: str, limit: int) -> list:
    """The mail to look at, chosen by the pipeline's own verdict.

    Keyed on `matched_rule` like `reprocess_mail._select` and the issues report, so this preview
    cannot quietly disagree with the system it describes about which mail is in question.
    """
    where, params = ["matched_rule = ?"], [rule]
    if since:
        where.append("COALESCE(email_date, '') >= ?")
        params.append(since)
    sql = ("SELECT email_id, email_date, subject, sender, matched_rule, category, "
           "       po_hints, attachment_count "
           f"  FROM email_log WHERE {' AND '.join(where)} ORDER BY email_date")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


def _as_email(row, body_html, body_text) -> RawEmail:
    """A `RawEmail` from a cached body.

    Deliberately no attachments: the cache stores them separately and this path only ever decides
    the *negative* case, where the guards have already proved there is nothing to read. Any cached
    candidate whose `email_log` row records attachments is marked in the output rather than counted
    as certain — see `run`.
    """
    sender = row["sender"] or ""
    return RawEmail(
        email_id=row["email_id"], received_at=row["email_date"] or "",
        sender_address=sender, sender_domain=sender.split("@")[-1] if "@" in sender else "",
        subject=row["subject"] or "", body_html=body_html, body_text=body_text,
        attachments=[],
    )


def _domain(address: str) -> str:
    """Sender domain, never the full address. A report is a document that circulates, and the
    domain is the part carrying the finding — the same choice `needs_a_human_issues_report` makes."""
    return (address or "").split("@")[-1].lower()


def _blocked_by(email: RawEmail, row) -> str:
    """Which guard stopped the rule, for the reader — never for the verdict.

    The verdict comes from `triage()` and only from `triage()`. This re-derives the conditions
    purely to explain a decision already made, because "241 carried no opt-out block" is the number
    that says whether the rule is worth having, and a list of unchanged subjects is not.
    """
    body = stage1_triage.text.body_text_of(email)
    if not stage1_triage._bulk_opt_out_phrase(email, body):
        return "no opt-out block in the body"
    if row["po_hints"]:
        return "names a purchase order"
    readable, images = stage1_triage._attachment_kinds(email)
    if readable or images:
        return "carries a readable attachment or a photograph"
    return "a hop in the thread claims goods arrived"


def run(*, rule: str = "rule_7_unknown", since: str = "", limit: int = 0,
        out: str = "", cache_bodies: bool = False, dry_run: bool = True,
        db_path=None) -> int:
    conn = state_db.get_connection(db_path or settings.PIPELINE_STATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = _select(conn, rule, since, limit)
    if not rows:
        print(f"no mail currently matched by {rule}.")
        conn.close()
        return 0

    cached, to_fetch = {}, []
    for row in rows:
        hit = mail_cache.get_cached_mail(conn, row["email_id"])
        if hit is not None:
            cached[row["email_id"]] = hit
        else:
            to_fetch.append(row["email_id"])

    print(f"selected   {len(rows)} message(s) currently {rule}")
    print(f"bodies     {len(cached)} from the local mail cache, {len(to_fetch)} to fetch from "
          f"Graph (one request each)")
    print("           read-only: no move, no flag, no folder created, no OCR, nothing written")
    print()

    fetched, absent = {}, []
    if to_fetch:
        from connectors.mailbox import GraphMailbox
        recovered = GraphMailbox(read_only=True).fetch_by_ids(to_fetch)
        fetched = {e.email_id: e for e in recovered.emails}
        absent = list(recovered.absent)

    would, unchanged, unreadable, reasons, taken = [], 0, 0, {}, {}
    for index, row in enumerate(rows, 1):
        email_id = row["email_id"]
        if email_id in fetched:
            email, source = fetched[email_id], "graph"
        elif email_id in cached:
            hit = cached[email_id]
            email, source = _as_email(row, hit["body_html"], hit["body_text"]), "cache"
        else:
            unreadable += 1
            continue

        verdict = triage(email, evidence=None)
        if verdict.matched_rule == RULE:
            phrase = stage1_triage._bulk_opt_out_phrase(
                email, stage1_triage.text.body_text_of(email)) or ""
            # A cached reconstruction carries no attachments, so the live path could still read a PO
            # out of one. Marked, never silently counted as certain.
            warn = ("  (!) carries an attachment the live path would read"
                    if source == "cache" and row["attachment_count"] else "")
            would.append((row, phrase, source))
            taken[_domain(row["sender"])] = taken.get(_domain(row["sender"]), 0) + 1
            print(f"  [{index:3}/{len(rows)}] WOULD SET ASIDE  {_domain(row['sender'])[:30]:<30} "
                  f"{(row['subject'] or '')[:34]:<34} {phrase!r}{warn}")
        else:
            unchanged += 1
            why = _blocked_by(email, row)
            reasons[why] = reasons.get(why, 0) + 1

    print()
    print(f"would be set aside      {len(would)}")
    print(f"unchanged               {unchanged}")
    print(f"absent from the mailbox {len(absent)}   <- the server answered and does not have it")
    print(f"could not be read       {unreadable}   <- transient, NOT absent: run again")

    if taken:
        print("\nwould be set aside, by sender domain")
        for dom, count in sorted(taken.items(), key=lambda kv: -kv[1]):
            print(f"  {count:4}  {dom}")
    if reasons:
        print("\nwhy the rest stayed")
        for why, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"  {count:4}  {why}")

    if out:
        target = Path(out) / "bulk_mail_rule_preview.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["email_date", "sender_domain", "subject", "today_rule",
                             "would_be_rule", "matched_phrase", "body_source"])
            for row, phrase, source in would:
                writer.writerow([row["email_date"], _domain(row["sender"]), row["subject"],
                                 row["matched_rule"], RULE, phrase, source])
        print(f"\nwrote {target}")

    if cache_bodies and fetched:
        if dry_run:
            print("\nRefusing to write without --yes. (--cache-bodies stores only the bodies it "
                  "just read, so a second run costs no Graph calls and a person can check each "
                  "candidate by eye in the UI.)")
        else:
            size = sum(len((e.body_html or "") + (e.body_text or "")) for e in fetched.values())
            print(f"\ncaching {len(fetched)} recovered body/bodies (~{size // 1024} KB) so they "
                  f"can be opened in the UI without another Graph call")
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for email in fetched.values():
                mail_cache.cache_mail(
                    conn, email_id=email.email_id, subject=email.subject,
                    sender=email.sender_address, received_at=email.received_at,
                    body_html=email.body_html, body_text=email.body_text,
                    source="preview", cached_at=now, attachments=[])
            conn.commit()

    print("\nNothing else was written. This is a preview: the rule applies to mail arriving next.")
    print("Making it apply to these means tools/reprocess_mail.py, which erases and re-downloads —")
    print("a separate, destructive decision for a person to take deliberately.")
    conn.close()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rule", default="rule_7_unknown",
                        help="which verdict to preview against (default rule_7_unknown)")
    parser.add_argument("--since", default="", help="only mail dated at or after this ISO instant")
    parser.add_argument("--limit", type=int, default=0, help="stop after this many messages")
    parser.add_argument("--out", default="", help=f"also write a CSV here, e.g. {DEFAULT_OUT}")
    parser.add_argument("--cache-bodies", action="store_true",
                        help="store the recovered bodies so they can be opened in the UI")
    parser.add_argument("--dry-run", action="store_true", help="the default; changes nothing")
    parser.add_argument("--yes", action="store_true",
                        help="required by --cache-bodies, the only write this tool can make")
    args = parser.parse_args(argv)
    return run(rule=args.rule, since=args.since, limit=args.limit, out=args.out,
               cache_bodies=args.cache_bodies, dry_run=args.dry_run or not args.yes)


if __name__ == "__main__":
    sys.exit(main())
