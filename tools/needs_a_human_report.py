"""Explain the "needs a human" queue: what is missing, why, and what clears it.

The console shows one number — 3,7xx items need a person — and no way to see inside it. That number
moves with every run, so from the screen alone it is impossible to tell whether it is one problem
repeated three thousand times or three thousand separate problems.

It is the former. On the 2026-09-03 store, 2,331 of 2,443 record items were blocked on the same
field, and four extraction sources produced 1,098 of the 1,340 missing descriptions. This writes
that breakdown to a Word document so the causes can be worked through one at a time.

**Every figure is read live.** Nothing here is hardcoded from that analysis: re-run it next week and
it describes next week's backlog. A report that quietly goes stale is worse than no report, because
it is still trusted.

    python -m tools.needs_a_human_report
    python -m tools.needs_a_human_report --out "d:/Premier/dev_reports"

Read-only. Opens the pipeline store, writes one .docx, changes nothing.
"""

import argparse
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from config import settings
from pipeline import read_views, state_db

DEFAULT_OUT = Path(r"d:/Premier/dev_reports")


# --- classification --------------------------------------------------------
#
# Reasons are free text written for a person to read, so classification is substring matching. Two
# rules keep it honest:
#
#   * every item lands in exactly one bucket, so the buckets sum to the queue total;
#   * anything unmatched goes to "Other" and is *listed*, never dropped. An item silently vanishing
#     between the queue and the report is the one failure that would make the whole document
#     untrustworthy, and it is invisible unless the totals are checked.

RECORD_BUCKETS = [
    ("Quantity conflict — two sources disagree",
     lambda r: "quantity conflict" in r,
     "Two documents state different quantities for the same line and neither is authoritative.",
     "A person picks the right quantity. Not automatable — it is a judgement about which document "
     "to believe."),
    ("Nothing recognisable was read from the source",
     lambda r: "confidence 0.0" in r,
     "The document was opened and parsed, but no PO, spec, quantity or date could be recognised "
     "in it.",
     "Adapter work: the format is being read but not understood. Group these by extraction source "
     "and fix the largest source first."),
    ("Blocked on the POD date alone",
     lambda r: _fields(r) == {"pod date"},
     "Every other required field is present. The only thing missing is the delivery date.",
     "Premier's rule: document date if stated, otherwise the mail receiving date. Applying it "
     "retroactively completes these records outright — see tools/backfill_pod_fallback.py."),
    ("POD date plus other fields",
     lambda r: "pod date" in _fields(r) and len(_fields(r)) > 1,
     "The delivery date is missing and so is at least one of description, quantity or spec code.",
     "The POD backfill removes the date half. What remains is adapter work on the other fields."),
    ("Missing fields other than the POD date",
     lambda r: bool(_fields(r)) and "pod date" not in _fields(r),
     "The delivery date is known; description, quantity or spec code is not.",
     "Adapter work, concentrated in a few sources — see the extraction-source table."),
]

EMAIL_BUCKETS = [
    ("No PO number anywhere in the thread",
     lambda r: "no po reference found" in r,
     "Nothing in the subject, body or attachments looks like a purchase order number.",
     "A person identifies the PO, or the sender is asked to quote one. Not automatable without a "
     "PO — there is nothing to match against."),
    ("Asks a question — needs a human answer",
     lambda r: "asks whether goods were received" in r,
     "The message asks Premier to confirm a delivery. It is a question, not a receipt.",
     "NOT A DEFECT. The pipeline is correct to hand these to a person; a reply is the outcome, "
     "not a record. Expect this bucket to stay non-zero."),
    ("Order cancellation — manual Spitfire update",
     lambda r: "cancellation" in r,
     "The message cancels or voids an order.",
     "NOT A DEFECT. A person updates the PO in Spitfire. Out of scope for receipt automation."),
    ("Loss, damage or claim thread",
     lambda r: "lost / damaged" in r or "claim" in r,
     "The thread is about damage, loss or a replacement PO.",
     "NOT A DEFECT. Explicitly out of Phase 1 scope."),
    ("Read as delivery mail, nothing extracted",
     lambda r: "nothing was extracted" in r,
     "Triage judged it a delivery message, but no PO, spec, quantity or POD was found in it.",
     "The most valuable bucket to investigate: triage and extraction disagree, so one of them is "
     "wrong on these messages."),
    ("Already delivered on an earlier message",
     lambda r: "already came through on an earlier message" in r,
     "This delivery was already released and staged from a previous email in the thread.",
     "NOT A DEFECT. Dedupe caught a repeat and asked a person to confirm rather than staging the "
     "same receipt twice — double-counting receipts is the failure this exists to prevent."),
    ("PO found, message shape unrecognised",
     lambda r: "message shape is unrecognized" in r,
     "A PO number is present but the message does not match any known layout.",
     "Adapter work: sample these and decide whether the shape is worth supporting."),
    ("Document read in full, no delivery content",
     lambda r: "found no delivery in it" in r,
     "The attachment was parsed completely and genuinely contains no delivery information.",
     "Often correct — quotes, invoices and specifications legitimately contain no delivery. Sample "
     "before treating as a defect."),
    ("OCR failed — Azure quota exhausted",
     lambda r: "out of call volume" in r or "service_unavailable" in r,
     "Azure Document Intelligence refused the page: the account has no call volume left.",
     "Raise the Azure tier, then recover the backlog with tools/reextract.py. One account change "
     "clears every item in this bucket."),
    ("OCR stopped — our own per-run page budget",
     lambda r: "ocrbudgetexhausted" in r or "page budget" in r,
     "The run hit settings.OCR_PAGES_PER_RUN and stopped sending pages.",
     "Raise OCR_PAGES_PER_RUN once the Azure quota allows it, then re-extract."),
    ("Photographed attachment with no text layer",
     lambda r: "photographed attachment" in r,
     "A photo of a delivery note. OCR read it and found nothing usable.",
     "Image quality or OCR tuning. Sample these before investing — some photos are genuinely "
     "unreadable."),
    ("Unsupported or unreadable file",
     lambda r: "unsupported_format" in r or "oversize" in r or "nesting" in r
     or "duplicate_content_mismatch" in r,
     "The format is recognised but not readable (.doc binary, oversize, nested too deep), or two "
     "copies of one file disagree.",
     "Mostly small counts. Convert or open by hand; only worth code if one format dominates."),
]

ATTACHMENT_BUCKETS = EMAIL_BUCKETS


def _fields(reason: str) -> set:
    """The field names in a `missing: a, b, c` clause, as a set.

    **Lower case**, because `classify` lowercases the reason before handing it to the predicates.
    Comparing against "POD date" here matched nothing at all, and every record fell through to
    "missing fields other than the POD date" — 1,799 of them, in a report whose entire purpose is
    to say which field is missing. It looked plausible on the page, which is what made it dangerous.
    """
    match = re.search(r"missing:\s*([^;]+)", reason or "")
    if not match:
        return set()
    return {f.strip() for f in match.group(1).split(",") if f.strip()}


def classify(reason: str, buckets) -> str:
    text = (reason or "").lower()
    for name, matches, _, _ in buckets:
        try:
            if matches(text):
                return name
        except Exception:                                          # noqa: BLE001
            continue
    return "Other"


def _bucket_help(name: str, buckets) -> tuple:
    for bucket_name, _, what, fix in buckets:
        if bucket_name == name:
            return what, fix
    return ("Not matched by any rule in this report.",
            "Read the raw reasons listed below and add a bucket for them — an item nobody has "
            "classified is an item nobody is working on.")


# --- the numbers -----------------------------------------------------------

def gather(conn) -> dict:
    queue = read_views.manual_queue(conn)
    by_kind = {"record": [], "email": [], "attachment": []}
    for item in queue:
        by_kind.setdefault(item.kind, []).append(item)

    data = {
        "total": len(queue),
        "counts": {k: len(v) for k, v in by_kind.items()},
        "buckets": {},
        "unmatched_reasons": Counter(),
    }

    for kind, buckets in (("record", RECORD_BUCKETS), ("email", EMAIL_BUCKETS),
                          ("attachment", ATTACHMENT_BUCKETS)):
        counts = Counter()
        for item in by_kind.get(kind, []):
            name = classify(item.reason, buckets)
            counts[name] += 1
            if name == "Other":
                data["unmatched_reasons"][(item.reason or "")[:110]] += 1
        data["buckets"][kind] = counts

    # Field-level counts: deliberately NOT mutually exclusive, because "how many records lack a
    # spec code" is the question being asked and a record can lack several things at once.
    fields = Counter()
    for item in by_kind.get("record", []):
        for field in _fields(item.reason):
            fields[field] += 1
    data["fields"] = fields

    data["sources"] = conn.execute("""
        SELECT COALESCE(extraction_source,'(none)') AS src, COUNT(*) AS n,
               SUM(CASE WHEN COALESCE(item_description,'')='' THEN 1 ELSE 0 END) AS no_desc,
               SUM(CASE WHEN quantity_received IS NULL THEN 1 ELSE 0 END) AS no_qty,
               SUM(CASE WHEN COALESCE(spec_code,'')='' THEN 1 ELSE 0 END) AS no_spec
          FROM extracted_records WHERE status='pending'
      GROUP BY src ORDER BY n DESC LIMIT 12""").fetchall()

    data["pod"] = conn.execute("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN pods > 0 THEN 1 ELSE 0 END) AS had_pod,
               SUM(CASE WHEN pods = 0 AND atts > 0 THEN 1 ELSE 0 END) AS atts_no_pod,
               SUM(CASE WHEN atts = 0 THEN 1 ELSE 0 END) AS no_atts
          FROM (SELECT r.id,
                  (SELECT COUNT(*) FROM attachment_ledger l
                    WHERE l.email_id = r.source_email_id AND l.is_pod = 1) AS pods,
                  (SELECT COUNT(*) FROM attachment_ledger l
                    WHERE l.email_id = r.source_email_id
                      AND l.disposition NOT IN ('dropped_decorative','dropped_duplicate')) AS atts
                  FROM extracted_records r
                 WHERE COALESCE(r.pod_stated_date,'') = '')""").fetchone()

    data["emails_total"] = conn.execute("SELECT COUNT(*) FROM email_log").fetchone()[0]
    data["emails_no_record"] = conn.execute("""
        SELECT COUNT(*) FROM email_log e
         WHERE NOT EXISTS (SELECT 1 FROM extracted_records x
                            WHERE x.source_email_id = e.email_id)""").fetchone()[0]
    data["delivery_mail_no_record"] = conn.execute("""
        SELECT COUNT(*) FROM email_log e
         WHERE e.category IN ('hold','surface')
           AND NOT EXISTS (SELECT 1 FROM extracted_records x
                            WHERE x.source_email_id = e.email_id)""").fetchone()[0]
    data["categories"] = conn.execute(
        "SELECT category, COUNT(*) AS n FROM email_log GROUP BY category ORDER BY n DESC").fetchall()
    return data


# --- the document ----------------------------------------------------------

def _table(document, headers, rows):
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


def build(data: dict, out_path: Path) -> Path:
    from docx import Document
    from docx.shared import Pt

    document = Document()
    document.add_heading("Receiver automation — what needs a human, and why", level=0)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    intro = document.add_paragraph()
    intro.add_run(f"Snapshot {stamp}. ").bold = True
    intro.add_run(
        f"{data['total']:,} items are waiting for a person: {data['counts'].get('record', 0):,} "
        f"records, {data['counts'].get('email', 0):,} emails and "
        f"{data['counts'].get('attachment', 0):,} attachments. This count changes with every run — "
        "regenerate this document to refresh it. Every figure below is read from the live store; "
        "none is copied from a previous analysis.")

    document.add_heading("The short version", level=1)
    document.add_paragraph(
        "This is not three thousand separate problems. A small number of causes account for almost "
        "all of it, and they are listed here worst-first so they can be cleared one at a time.",
        style="Intense Quote")

    top = []
    for kind in ("record", "email", "attachment"):
        for name, count in data["buckets"].get(kind, Counter()).most_common(3):
            top.append((count, f"{name} ({kind}s)"))
    for count, label in sorted(top, reverse=True)[:6]:
        document.add_paragraph(f"{count:,} — {label}", style="List Bullet")

    # --- records
    document.add_heading("Records waiting on a missing field", level=1)
    document.add_paragraph(
        f"{data['counts'].get('record', 0):,} extracted records are incomplete. Read the two "
        "tables differently. The first counts fields and overlaps on purpose: a record missing "
        "both a description and a date appears in both rows, because the question being asked is "
        "'how many records lack a spec code'. The second counts records and does not overlap — "
        "each record appears once, in the bucket describing the most important thing standing in "
        "its way. So a record that is missing a delivery date AND has a quantity conflict is "
        "counted under the conflict, which is why the POD buckets total less than the POD field "
        "count.")

    _table(document, ["Field missing", "Records"],
           [[f, f"{n:,}"] for f, n in data["fields"].most_common()])

    document.add_paragraph()
    _table(document, ["Bucket", "Records"],
           [[b, f"{n:,}"] for b, n in data["buckets"].get("record", Counter()).most_common()])

    pod = data["pod"]
    document.add_heading("Why the POD date is the biggest single cause", level=2)
    document.add_paragraph(
        f"Of {pod['total']:,} records with no delivery date, only {pod['had_pod']} came from an "
        f"email that carried a recognised proof of delivery. {pod['atts_no_pod']:,} had "
        f"attachments where none was a POD, and {pod['no_atts']} had no attachment at all. "
        "The documents simply do not state a delivery date, so no amount of parsing will find one.")
    document.add_paragraph(
        "Premier's rule: if the document gives a delivery date, that is the date; if it does not, "
        "the date the mail was received is the date. That rule is live for newly staged records. "
        "The records above predate it and are corrected by tools/backfill_pod_fallback.py, which "
        "applies the same rule retroactively and never overwrites a date a document stated.")

    # --- emails
    document.add_heading("Emails that produced nothing", level=1)
    document.add_paragraph(
        f"{data['emails_total']:,} emails have been processed. {data['emails_no_record']:,} "
        "produced no record — but most were never meant to: routed and hidden mail is not delivery "
        f"mail. The real gap is the {data['delivery_mail_no_record']:,} messages triaged as "
        "delivery mail that still produced nothing.")

    _table(document, ["Triage category", "Emails"],
           [[r["category"], f"{r['n']:,}"] for r in data["categories"]])

    document.add_paragraph()
    _table(document, ["Bucket", "Emails"],
           [[b, f"{n:,}"] for b, n in data["buckets"].get("email", Counter()).most_common()])

    # --- attachments
    document.add_heading("Attachments that could not be used", level=1)
    _table(document, ["Bucket", "Attachments"],
           [[b, f"{n:,}"] for b, n in data["buckets"].get("attachment", Counter()).most_common()])

    # --- sources
    document.add_heading("Which extraction sources produce the gaps", level=1)
    document.add_paragraph(
        "Missing descriptions and quantities are not spread evenly. A few sources account for most "
        "of them, which is what makes them fixable one adapter at a time.")
    _table(document, ["Extraction source", "Pending", "No description", "No quantity", "No spec"],
           [[r["src"], f"{r['n']:,}", f"{r['no_desc']:,}", f"{r['no_qty']:,}", f"{r['no_spec']:,}"]
            for r in data["sources"]])

    # --- what to do
    document.add_heading("What clears each bucket", level=1)
    for kind, buckets in (("record", RECORD_BUCKETS), ("email", EMAIL_BUCKETS),
                          ("attachment", ATTACHMENT_BUCKETS)):
        counts = data["buckets"].get(kind, Counter())
        if not counts:
            continue
        document.add_heading(f"{kind.title()}s", level=2)
        for name, count in counts.most_common():
            what, fix = _bucket_help(name, buckets)
            heading = document.add_paragraph()
            heading.add_run(f"{name} — {count:,}").bold = True
            document.add_paragraph(f"What is missing: {what}")
            document.add_paragraph(f"What clears it: {fix}")

    # --- honesty section
    if data["unmatched_reasons"]:
        document.add_heading("Items this report could not classify", level=1)
        document.add_paragraph(
            "These did not match any rule above. They are listed rather than dropped: an item "
            "nobody has classified is an item nobody is working on.")
        _table(document, ["Reason", "Items"],
               [[reason, f"{n:,}"] for reason, n in data["unmatched_reasons"].most_common(20)])

    document.add_heading("How to reproduce these numbers", level=1)
    document.add_paragraph(
        "Every count comes from read_views.manual_queue() and the extracted_records / email_log "
        "tables in state/pipeline_state.sqlite3, read at the snapshot time above. Re-run "
        "python -m tools.needs_a_human_report to regenerate.")

    for paragraph in document.paragraphs:
        for run in paragraph.runs:
            if run.font.size is None:
                run.font.size = Pt(11)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(out_path))
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

    # The check that makes the document trustworthy. Buckets are mutually exclusive by
    # construction, so if they do not sum to the queue total something has been dropped — and a
    # report that under-reports is worse than none, because it is still believed.
    counted = sum(sum(c.values()) for c in data["buckets"].values())
    if counted != data["total"]:
        print(f"REFUSING TO WRITE: buckets total {counted} but the queue holds {data['total']}")
        return 1

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out = Path(args.out) / f"needs-a-human-{stamp}.docx"
    build(data, out)

    print(f"{data['total']:,} items: " + ", ".join(
        f"{k} {v:,}" for k, v in sorted(data["counts"].items())))
    if data["unmatched_reasons"]:
        print(f"unclassified: {sum(data['unmatched_reasons'].values())} "
              f"across {len(data['unmatched_reasons'])} distinct reasons")
    print(f"written: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
