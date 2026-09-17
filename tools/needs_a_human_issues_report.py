"""Why so much mail needs a human — the causes behind the queue, not the counts.

`tools/needs_a_human_report.py` answers *what* is in the queue. This answers *why*, for the email
arm specifically, because that is the arm nobody could explain: roughly a thousand messages waiting
on a person, with no way to tell whether that is a thousand problems or a handful.

It is a handful. The single largest cause is that the pipeline has been configured to recognise
fifteen sender domains, and most of the queue arrives from domains it has never been told about —
so triage cannot place them and the catch-all hands them to a person. Some of those unknown senders
are real logistics partners; most are software notifications and retail marketing that should never
have entered a receiving queue at all.

**Every figure is read live.** Nothing is hardcoded from the analysis that prompted this, and a test
enforces that. Grouping is by `email_log.matched_rule` — the pipeline's own verdict — so this
document cannot quietly disagree with the system it describes.

    python -m tools.needs_a_human_issues_report
    python -m tools.needs_a_human_issues_report --out "d:/Premier/dev_reports"

Read-only. Opens the pipeline store, writes one .docx, changes nothing.
"""

import argparse
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from config import settings
from pipeline import read_views, state_db
from pipeline.parsing import intent

DEFAULT_OUT = Path(r"d:/Premier/dev_reports")

# What each triage rule means, and whether landing here is a defect or the system working. Keyed on
# the rule name `stage1_triage` records, so a renamed rule shows up as unknown rather than silently
# losing its explanation.
RULES = {
    "rule_7_unknown": (
        "Nothing recognised the message",
        "DEFECT",
        "The catch-all. Triage could not place the sender or the shape, so it hands the mail to a "
        "person rather than guessing."),
    "rule_2a_verification_request": (
        "Someone asks whether goods arrived",
        "EXPECTED",
        "A question needing a reply, not a receipt. The pipeline is right to hand these over."),
    "rule_2a_verification_no_reference": (
        "A question naming no purchase order",
        "EXPECTED",
        "The same question with nothing to receive against — no PO anywhere in the thread and no "
        "attachment that could carry one. Set aside as not delivery mail (Premier, 2026-09-15) "
        "rather than queued, so it should not appear here at all."),
    "rule_4_property_reply": (
        "A property replied on a delivery thread",
        "REVIEW",
        "Held for a partner notice. settings.py records this rule staging 126 records from one "
        "report email, so its breadth is a known question."),
    "rule_5a_tracker_attachment": (
        "A tracker spreadsheet came with it",
        "REVIEW",
        "Held: a spreadsheet may be a delivery or may be a worklist of goods still in transit."),
    "rule_5b_image_only_evidence": (
        "Photographed evidence that could not be read",
        "DEFECT",
        "OCR ran and found nothing usable in the image."),
    "rule_0a_order_cancellation": (
        "Reads as an order cancellation",
        "DEFECT",
        "Matches cancellation vocabulary anywhere in the message. See the false-cancellation "
        "table: most name no purchase order, so none of them can be actioned."),
    "rule_0b_loss_or_claim": (
        "Damage, loss or a claim",
        "EXPECTED",
        "Explicitly out of Phase 1 scope; a person handles it."),
    "rule_6_unrecognized_with_po": (
        "A purchase order, but an unfamiliar shape",
        "DEFECT",
        "The PO was found, so the message is probably real work the parsers do not yet read."),
    "rule_5c_internal_noise": (
        "Internal announcement",
        "EXPECTED",
        "Suppressed as noise — an announcement, not a delivery."),
    "rule_5g_internally_authored": (
        "Written inside Premier",
        "EXPECTED",
        "Premier wrote this message, so it cannot be evidence that Premier's own goods arrived. "
        "Expediting reports, inventory sheets and \"has this landed yet\" chasers all carry "
        "purchase orders and item lines, and none of them says anything arrived. Applied after "
        "every rule has had its turn, so a cancellation, a loss-or-claim thread, a parsed "
        "Authority notice and photographed evidence nobody has read are all left alone — see "
        "stage1_triage.AUTHORSHIP_YIELDS_TO. Keyed on the origin sender, never the envelope: this "
        "mailbox forwards everything, so the envelope reads premierpm.com on a warehouse "
        "receiving report too."),
    "rule_5f_possible_advertising": (
        "Possibly advertising",
        "REVIEW",
        "Promotional in shape, but something about it says otherwise or it is simply not clear "
        "enough to file away. Queued deliberately: this is a request for a person to look, not a "
        "verdict. Two clicks either way from the row."),
    "rule_5e_bulk_mail_noise": (
        "Advertising and bulk mail",
        "EXPECTED",
        "Suppressed as noise — the body carries the opt-out block commercial bulk mail is obliged "
        "to provide, and nothing else in the message is recognisable: no purchase order, no "
        "attachment worth reading, and no hop claiming goods arrived."),
    # The identifying rules. These normally settle a message rather than queue it, so a message
    # reaching the queue under one of them means the *identification* worked and something after it
    # did not — which is a more interesting failure than the catch-all, and a rarer one.
    "rule_1a_authority_inbound": (
        "Warehouse booked the goods in",
        "DEFECT",
        "The receiver trigger itself. Identified correctly, so anything queued here failed after "
        "triage — in extraction or in matching."),
    "rule_1b_authority_delivered": (
        "Carrier reported the goods delivered",
        "DEFECT",
        "Identified correctly; the failure is downstream of triage."),
    "rule_1c_authority_status_report": (
        "Periodic warehouse status summary",
        "EXPECTED",
        "A recurring PO summary, not a delivery event."),
    "rule_2_freight_status": (
        "Carrier status update",
        "REVIEW",
        "A movement update. Whether it is a delivery depends on what the update says."),
    "rule_3_vendor_confirmation": (
        "Vendor confirmed an order or a shipment",
        "REVIEW",
        "A confirmation from a configured vendor domain."),
    "rule_0c_report_sender": (
        "A mailbox that only sends scheduled reports",
        "EXPECTED",
        "Suppressed by REPORT_SENDER_ADDRESSES — the narrow fix for a report email that once "
        "staged 126 records."),
    "rule_5d_no_delivery_claim": (
        "Says no delivery happened",
        "EXPECTED",
        "The message itself denies a delivery."),
}


def _domain(address) -> str:
    return (address or "").split("@")[-1].lower().strip()


def _configured_domains() -> dict:
    """Every domain the pipeline has been told about, and what it was told."""
    known = {}
    for label, name in (("internal", "INTERNAL_DOMAINS"),
                        ("warehouse", "WAREHOUSE_SENDER_DOMAINS"),
                        ("freight", "FREIGHT_SENDER_DOMAINS"),
                        ("vendor", "VENDOR_CONFIRMATION_DOMAINS"),
                        ("property", "PROPERTY_DOMAINS")):
        for domain in getattr(settings, name, []) or []:
            known.setdefault(domain.lower().strip(), label)
    return known


def gather(conn) -> dict:
    queue = read_views.manual_queue(conn)
    email_items = [item for item in queue if item.kind == "email"]
    ids = {item.email_id for item in email_items}

    rows = [dict(r) for r in conn.execute(
        "SELECT email_id, subject, sender, matched_rule, category, po_hints, attachment_count "
        "  FROM email_log").fetchall() if r["email_id"] in ids]

    known = _configured_domains()
    data = {
        "total_queue": len(queue),
        "emails": len(email_items),
        "rows": rows,
        "known_domains": known,
        "by_rule": Counter(r["matched_rule"] or "(none)" for r in rows),
        "by_domain": Counter(_domain(r["sender"]) for r in rows),
    }

    data["unknown_sender"] = [r for r in rows if _domain(r["sender"]) not in known]

    # The catch-all, opened up by sender and by whether anything was attached. "No attachment at
    # all" is the fact that separates a company announcement from mail we simply could not read.
    catch = [r for r in rows if r["matched_rule"] == "rule_7_unknown"]
    per_sender = {}
    for r in catch:
        slot = per_sender.setdefault(_domain(r["sender"]), {"n": 0, "with": 0, "subject": ""})
        slot["n"] += 1
        slot["with"] += 1 if (r["attachment_count"] or 0) else 0
        slot["subject"] = slot["subject"] or (r["subject"] or "")
    data["catch_all"] = catch
    data["catch_all_by_sender"] = sorted(
        per_sender.items(), key=lambda kv: -kv[1]["n"])[:15]
    data["catch_all_no_attachment"] = sum(1 for r in catch if not (r["attachment_count"] or 0))

    # A cancellation that names no purchase order cannot be an order cancellation. Deliberately
    # *not* a list of sender names: the test is a property of the message, so it keeps working when
    # next month's marketing arrives from somewhere else.
    cancels = [r for r in rows if r["matched_rule"] == "rule_0a_order_cancellation"]
    data["cancellations"] = cancels
    data["false_cancellations"] = [r for r in cancels if not (r["po_hints"] or "").strip()]

    # Mail rule 5c was written to hide and did not: internal, nothing attached, no PO named, and no
    # delivery vocabulary in the subject — yet it reached the catch-all instead.
    internal = set(d for d, kind in known.items() if kind == "internal")
    data["rule_5c_misses"] = [
        r for r in catch
        if _domain(r["sender"]) in internal
        and not (r["attachment_count"] or 0)
        and not (r["po_hints"] or "").strip()
        and not intent.is_delivery_topic(r["subject"] or "")]

    data["ocr_blocked"] = sum(
        1 for item in email_items
        if "out of call volume" in (item.reason or "").lower()
        or "service_unavailable" in (item.reason or "").lower())
    data["attachment_items"] = sum(1 for item in queue if item.kind == "attachment")
    data["record_items"] = sum(1 for item in queue if item.kind == "record")
    return data


def issues(data: dict) -> list:
    """The issue table, computed. Each row: name, items, cause, fix, who can fix it."""
    by_rule = data["by_rule"]
    return [
        ("Senders the pipeline has never been told about",
         len(data["unknown_sender"]),
         f"Triage recognises {len(data['known_domains'])} domains. Everything else has no route, "
         "so it falls to the catch-all.",
         "Add the real trading partners to the warehouse/vendor/property domain lists. settings.py "
         "already records these as owed (checklist B7).",
         "Configuration"),
        ("External non-delivery mail still reaching the queue",
         by_rule.get("rule_7_unknown", 0),
         "rule_5e_bulk_mail_noise now suppresses external mail carrying an opt-out block, but it "
         "fires on that signal alone. Software notifications that provide no opt-out, and ordinary "
         "correspondence from senders triage has no route for, still fall to the catch-all.",
         "Read the remaining senders here before adding another rule: a share of this count is "
         "real mail from real people, which belongs on the queue.",
         "Code"),
        ("Cancellations that name no purchase order",
         len(data["false_cancellations"]),
         "CANCELLATION_KEYWORDS_REGEX matches 'cancel' anywhere, including an unsubscribe footer.",
         "Require a PO reference before rule_0a can fire. A cancellation that names no order "
         "cannot be actioned even when it is genuine.",
         "Code"),
        ("Internal announcements rule 5c should hide",
         len(data["rule_5c_misses"]),
         "Every documented condition holds, yet the rule did not fire — the remaining candidate is "
         "delivery vocabulary in the body, most likely a signature or footer.",
         "Confirm against a live message, then narrow what the body test reads.",
         "Code, after confirming"),
        ("OCR quota exhausted",
         data["ocr_blocked"],
         "Azure Document Intelligence returned 403 Out of call volume quota.",
         "Raise the Azure tier, then recover the backlog with tools/reextract.py.",
         "Account"),
        ("Questions needing a human answer",
         by_rule.get("rule_2a_verification_request", 0),
         "Someone asks whether goods arrived. A reply is the outcome, not a receipt.",
         "Nothing to fix. Expect this bucket to stay non-zero.",
         "Not a defect"),
        ("Property replies held for a partner notice",
         by_rule.get("rule_4_property_reply", 0),
         "rule_4 is broad; settings.py records it staging 126 records from a single report email.",
         "Review with Premier what a property reply should be allowed to trigger.",
         "Premier's call"),
    ]


def _table(document, headers, rows, widths=None):
    table = document.add_table(rows=1, cols=len(headers))
    table.style = "Light Grid Accent 1"
    for cell, header in zip(table.rows[0].cells, headers):
        cell.text = str(header)
        for run in cell.paragraphs[0].runs:
            run.bold = True
    for row in rows:
        cells = table.add_row().cells
        for cell, value in zip(cells, row):
            cell.text = str(value)
    return table


def _clean(text, limit=58):
    """Subjects carry emoji and smart quotes; Word is fine with them, a console is not."""
    return (text or "").replace("\n", " ").strip()[:limit]


def build(data: dict, out_path: Path) -> Path:
    from docx import Document

    doc = Document()
    doc.add_heading("Why mail needs a human — the issues behind the queue", level=0)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    intro = doc.add_paragraph()
    intro.add_run(f"Snapshot {stamp}. ").bold = True
    intro.add_run(
        f"{data['total_queue']:,} items are waiting for a person, of which {data['emails']:,} are "
        f"emails (the rest are {data['record_items']:,} records and "
        f"{data['attachment_items']:,} attachments). This document explains the email arm: why "
        "there are so many, and which causes can be resolved. Every figure is read from the live "
        "store at the snapshot time above.")

    doc.add_paragraph(
        f"The headline: the pipeline is configured to recognise {len(data['known_domains'])} "
        f"sender domains, and {len(data['unknown_sender']):,} of the {data['emails']:,} queued "
        "emails arrive from domains it has never been told about. Some are real logistics "
        "partners whose mail cannot be triaged properly. Most are software notifications and "
        "retail marketing that should never have reached a receiving queue.",
        style="Intense Quote")

    # 1 --------------------------------------------------------------------
    doc.add_heading("1. The issues, worst first", level=1)
    doc.add_paragraph(
        "Read this table first. Every other table in this document exists to support a row in it.")
    _table(doc, ["Issue", "Items", "Root cause", "What would fix it", "Fixable by"],
           [[name, f"{n:,}", cause, fix, who] for name, n, cause, fix, who in issues(data)])

    # 2 --------------------------------------------------------------------
    doc.add_heading("2. Which triage rule sent each email here", level=1)
    doc.add_paragraph(
        "The pipeline's own verdict, not an interpretation of it. 'Expected' means the rule is "
        "working and the mail genuinely needs a person — those buckets are not work to be "
        "eliminated.")
    total = max(1, data["emails"])
    _table(doc, ["Triage rule", "Emails", "% of queue", "What it means", "Defect?"],
           [[rule,
             f"{n:,}",
             f"{100 * n / total:.0f}%",
             RULES.get(rule, ("unrecognised rule", "REVIEW", ""))[0],
             RULES.get(rule, ("", "REVIEW", ""))[1]]
            for rule, n in data["by_rule"].most_common()])

    # 3 --------------------------------------------------------------------
    doc.add_heading("3. Who is sending this mail", level=1)
    doc.add_paragraph(
        "'Configured as' is what the pipeline has been told about the domain. 'Not configured' is "
        "the root of the largest issue: triage has no route for a sender it does not know, so the "
        "mail reaches the catch-all whatever it contains.")
    known = data["known_domains"]
    _table(doc, ["Sender domain", "Emails", "Configured as"],
           [[domain or "(none)", f"{n:,}", known.get(domain, "NOT CONFIGURED")]
            for domain, n in data["by_domain"].most_common(25)])

    # 4 --------------------------------------------------------------------
    doc.add_heading("4. The catch-all, opened up", level=1)
    doc.add_paragraph(
        f"{data['by_rule'].get('rule_7_unknown', 0):,} emails reached rule_7_unknown, and "
        f"{data['catch_all_no_attachment']:,} of them carry no attachment at all. A message with "
        "nothing attached and nothing recognisable in it is almost never a delivery.")
    _table(doc, ["Sender domain", "Emails", "With attachment", "Without", "Example subject"],
           [[domain or "(none)", f"{s['n']:,}", s["with"], s["n"] - s["with"],
             _clean(s["subject"])]
            for domain, s in data["catch_all_by_sender"]])

    # 5 --------------------------------------------------------------------
    doc.add_heading("5. Cancellations that name no purchase order", level=1)
    doc.add_paragraph(
        f"{len(data['cancellations']):,} emails matched the order-cancellation rule and "
        f"{len(data['false_cancellations']):,} of them name no purchase order anywhere. "
        "None of these can be actioned: cancelling an order in Spitfire requires knowing which "
        "order. Most are retail marketing whose footer offers to cancel a subscription; the rest "
        "are internal threads where the word appears in passing. They matter more than ordinary "
        "noise, because a reviewer is being told an order was cancelled and that Spitfire needs a "
        "manual update.")
    _table(doc, ["Subject", "Sender", "Names a PO?"],
           [[_clean(r["subject"]), _domain(r["sender"]), "no"]
            for r in data["false_cancellations"][:25]])

    # 6 --------------------------------------------------------------------
    doc.add_heading("6. Internal announcements that should already be hidden", level=1)
    doc.add_paragraph(
        "rule_5c_internal_noise exists to hide company announcements and calendar invites; its own "
        "documentation names several of these by subject. Each row below satisfies every condition "
        "that rule documents — internal sender, nothing attached, no PO named, no delivery "
        "vocabulary in the subject — and still reached the catch-all. The remaining candidate is "
        "delivery vocabulary in the message body, most likely a signature or footer.")
    _table(doc, ["Subject", "Sender", "Attachments", "Names a PO?", "Delivery subject?"],
           [[_clean(r["subject"]), _domain(r["sender"]), r["attachment_count"] or 0, "no", "no"]
            for r in data["rule_5c_misses"][:25]])

    # 7 --------------------------------------------------------------------
    doc.add_heading("7. What is not a defect", level=1)
    doc.add_paragraph(
        "These buckets are the pipeline working correctly. They are listed so the effort does not "
        "go here — trying to automate them away would mean posting receipts nobody can support.")
    _table(doc, ["Bucket", "Emails", "Why this is correct"],
           [[RULES[rule][0], f"{data['by_rule'].get(rule, 0):,}", RULES[rule][2]]
            for rule in RULES
            if RULES[rule][1] == "EXPECTED" and data["by_rule"].get(rule)])

    doc.add_heading("How to reproduce these numbers", level=1)
    doc.add_paragraph(
        "Every count comes from read_views.manual_queue() and email_log in "
        "state/pipeline_state.sqlite3, read at the snapshot time above. Grouping is by "
        "email_log.matched_rule, the verdict triage itself recorded. Re-run "
        "python -m tools.needs_a_human_issues_report to refresh.")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return out_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="directory for the .docx")
    args = parser.parse_args(argv)

    conn = state_db.get_connection(settings.PIPELINE_STATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        data = gather(conn)
    finally:
        conn.close()

    # Same refusal as the sibling report. If the rules do not account for every queued email,
    # something has been dropped, and a document that under-reports is worse than none because it
    # is still believed.
    counted = sum(data["by_rule"].values())
    if counted != data["emails"]:
        print(f"REFUSING TO WRITE: rules total {counted} but the queue holds {data['emails']} "
              "emails")
        return 1

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out = Path(args.out) / f"needs-a-human-issues-{stamp}.docx"
    build(data, out)

    print(f"emails in the queue      : {data['emails']:,}")
    print(f"from unconfigured senders: {len(data['unknown_sender']):,}")
    print(f"reached the catch-all    : {data['by_rule'].get('rule_7_unknown', 0):,} "
          f"({data['catch_all_no_attachment']:,} with no attachment)")
    print(f"false cancellations      : {len(data['false_cancellations']):,} "
          f"of {len(data['cancellations']):,}")
    print(f"rule 5c misses           : {len(data['rule_5c_misses']):,}")
    print(f"written: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
