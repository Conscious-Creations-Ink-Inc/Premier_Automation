"""The evidence behind the advertising lexicon, re-measured against the store on demand.

`pipeline/parsing/promotional.py` carries weighted term lists and a docstring full of percentages.
Those numbers were measured once, by hand, against a mailbox that grows every day — and a lexicon
argued from memory is a lexicon nobody can correct. This regenerates the whole measurement so the
lists can be revised against evidence: which features actually separate advertising from Premier's
work, which ones only look as though they do, and where every message in the store currently lands.

Four corpora, all read from the pipeline store and nothing else:

* **candidates** — mail reaching the queue as the catch-all, or already classified by the lexicon.
* **known delivery mail** — `surface`/`hold`, or having produced an `extracted_records` row. Nothing
  here may ever be classified as advertising, and that check is the point of the report.
* **delivery bodies** — full HTML recovered from `accumulation.payload_json`. Advertising is never
  accumulated, so this is a clean negative set of several hundred real messages.
* **candidate bodies** — `mail_body`, which holds only what somebody has opened in the UI.

The classification here uses **the pipeline's own stored evidence** — `email_log.po_hints`, derived
at ingest from the whole message, and the attachment ledger — rather than re-deriving them from the
subject. That matters: re-running `triage()` over messages rebuilt from `email_log` alone finds far
fewer purchase orders (94% of the store has no body on disk), so it under-counts the business vetoes
and over-states what the lexicon would hide. These numbers are the conservative, accurate ones.

Read-only. No Graph, no OCR, no writes to the store. Sender **domains** only and never a full
address, and no body text is reproduced — a report is a document that circulates.

    python -m tools.mail_lexicon_report
    python -m tools.mail_lexicon_report --out d:/Premier/dev_reports
"""

import argparse
import html
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from config import settings
from pipeline import state_db
from pipeline.models import RawEmail
from pipeline.parsing import promotional

DEFAULT_OUT = Path("d:/Premier/dev_reports")

# Features measured for the report only. The lexicon does not read this table — it is here to show
# which signals earn their place and, just as usefully, which do not.
SUBJECT_FEATURES = (
    ("names no purchase order", lambda r, h: not r["po_hints"]),
    ("carries no attachment", lambda r, h: not r["attachment_count"]),   # as email_log records it
    ("sender on a bulk-mail subdomain",
     lambda r, h: bool(re.search(r"@(?:e|em|email|mail|members|campaign|news|marketing|attn"
                                 r"|click|reply|travel\d*)\.", r["sender"] or "", re.I))),
    ("a marketing glyph in the subject",
     lambda r, h: any(ord(ch) > 0x2000 for ch in (r["subject"] or ""))),
    ("a % or $ figure in the subject",
     lambda r, h: bool(re.search(r"%|\$\d", r["subject"] or ""))),
    ("a role sender (deals@, news@, editor@)",
     lambda r, h: bool(re.match(r"(?:no-?reply|donotreply|deals?|news|editor|info|hello|marketing)@",
                                r["sender"] or "", re.I))),
    ("subject ends with ! or ?",
     lambda r, h: (r["subject"] or "").strip().endswith(("!", "?"))),
    ("IS A REPLY (Re: / Fw:)",
     lambda r, h: bool(re.match(r"\s*(?:re|fw|fwd)\s*:", r["subject"] or "", re.I))),
    ("names a Premier property",
     lambda r, h: bool(re.search(r"sheraton|marriott|sofitel|hyatt|autograph|homewood|westin"
                                 r"|aramark|anchorage|duluth|cates creek|sugar land",
                                 r["subject"] or "", re.I))),
    ("carries a spec / RR / project code",
     lambda r, h: bool(re.search(r"\b[A-Z]{2,4}-\d{3}[a-z]?-[A-Z]{2,3}\b|\bRR\s*\d{6}\b"
                                 r"|\b(?:SP|STT|CIW|ATW|CCB|ANS)\d{2,6}\b", r["subject"] or ""))),
    ("2+ ALL-CAPS words  [rejected]",
     lambda r, h: len(re.findall(r"\b[A-Z]{3,}\b", r["subject"] or "")) >= 2),
    ("an unsubscribe block (body)",
     lambda r, h: bool(promotional.OPT_OUT_RE.search(h or ""))),
    ("more <img> than <p> (body)",
     lambda r, h: len(re.findall(r"<img", h or "", re.I))
                  > max(1, len(re.findall(r"<p[\s>]", h or "", re.I)))),
    ("10+ outbound links (body)  [rejected]",
     lambda r, h: len(re.findall(r"<a\s[^>]*href=[\"']https?://", h or "", re.I)) >= 10),
    ("a tracking pixel (body)  [rejected]",
     lambda r, h: bool(re.search(r"<img[^>]+(?:width|height)=[\"']?1[\"']?[^>]*>", h or "", re.I))),
)


def _domain(address) -> str:
    return (address or "").split("@")[-1].lower().strip()


def gather(conn) -> dict:
    conn.row_factory = sqlite3.Row
    bodies = {r["email_id"]: r["body_html"]
              for r in conn.execute("SELECT email_id, body_html FROM mail_body")}

    # Attachments that would actually stop the rule, from the ledger rather than from
    # `email_log.attachment_count`. That column counts every part including the signature logos a
    # marketing email drags along, which `stage1_triage._attachment_kinds` ignores because they are
    # dropped as decorative at ingest. Counting them makes every advertisement look like it carries
    # a document, and the report then disagrees with the pipeline it is describing.
    real_attachments = {row[0] for row in conn.execute(
        "SELECT DISTINCT email_id FROM attachment_ledger WHERE COALESCE(is_inline, 0) = 0")}

    cols = ("email_id, subject, sender, email_date, matched_rule, category, po_hints, "
            "attachment_count")
    candidates = conn.execute(
        f"SELECT {cols} FROM email_log WHERE matched_rule IN "
        f"('rule_7_unknown', 'rule_5e_bulk_mail_noise', 'rule_5f_possible_advertising')").fetchall()
    delivery = conn.execute(
        f"SELECT {cols} FROM email_log WHERE category IN ('surface','hold') "
        f"OR email_id IN (SELECT source_email_id FROM extracted_records)").fetchall()

    # Genuine delivery bodies. Advertising is never accumulated, so this set cannot be contaminated.
    delivery_bodies = []
    # Both homes for the payload — see `stage2_accumulate._bundle_for_key`. Reading only
    # `accumulation.payload_json` would silently drop every message stored since payloads moved to
    # their own table, and this report would quietly narrow rather than fail.
    for (payload,) in conn.execute(
            "SELECT COALESCE(NULLIF(a.payload_json, ''), p.payload_json) AS payload_json "
            "  FROM accumulation a "
            "  LEFT JOIN accumulation_payload p ON p.email_id = a.email_id "
            " WHERE LENGTH(COALESCE(NULLIF(a.payload_json, ''), p.payload_json)) > 50"):
        try:
            body = (json.loads(payload).get("email") or {}).get("body_html")
        except Exception:                                   # noqa: BLE001 — a bad row is not fatal
            continue
        if body:
            delivery_bodies.append(body)

    def verdict(row):
        return promotional.classify(promotional.Message(
            subject=row["subject"] or "", body_html=bodies.get(row["email_id"]) or "",
            sender=row["sender"] or "", has_po=bool(row["po_hints"]),
            has_attachment=row["email_id"] in real_attachments))

    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "store": str(settings.PIPELINE_STATE_DB_PATH),
        "total": conn.execute("SELECT COUNT(*) FROM email_log").fetchone()[0],
        "bodies_on_disk": len(bodies),
        "candidates": candidates,
        "delivery": delivery,
        "delivery_bodies": delivery_bodies,
        "bodies": bodies,
        "bands": {row["email_id"]: verdict(row) for row in candidates},
        "delivery_bands": {row["email_id"]: verdict(row) for row in delivery},
    }


def _rate(rows, bodies, test) -> float:
    if not rows:
        return 0.0
    hit = sum(1 for r in rows if test(r, bodies.get(r["email_id"])))
    return 100.0 * hit / len(rows)


def _esc(value) -> str:
    return html.escape(str(value or ""))


def build(data: dict, out_path: Path) -> Path:
    cand, deliv = data["candidates"], data["delivery"]
    bands = Counter(v.band for v in data["bands"].values())
    wrong = [r for r in deliv
             if data["delivery_bands"][r["email_id"]].band == promotional.ADVERTISING]

    parts = [f"""<!doctype html><html><head><meta charset="utf-8">
<title>Advertising lexicon — evidence</title><style>
body {{ font: 13px/1.55 "Segoe UI", system-ui, sans-serif; color:#1b1b1b; max-width:1080px;
        margin:28px auto; padding:0 18px; }}
h1 {{ font-size:21px; border-bottom:3px solid #A6192E; padding-bottom:6px; }}
h2 {{ font-size:15px; margin-top:30px; color:#A6192E; }}
table {{ border-collapse:collapse; width:100%; margin:10px 0 18px; font-size:12px; }}
th, td {{ border:1px solid #d9d9d9; padding:5px 8px; text-align:left; vertical-align:top; }}
th {{ background:#f4f4f4; }}
td.n {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
.sub {{ color:#666; font-size:12px; }}
.ok {{ color:#136f3c; font-weight:600; }} .bad {{ color:#A6192E; font-weight:600; }}
.tag {{ font-size:11px; padding:1px 6px; border-radius:9px; background:#eee; }}
tr {{ page-break-inside:avoid; }}
</style></head><body>
<h1>Advertising lexicon — the evidence</h1>
<p class="sub">Generated {_esc(data['generated'])} from {_esc(data['store'])}.
Read-only. Sender domains only; no body text is reproduced.</p>

<h2>Corpora</h2>
<table><tr><th>corpus</th><th class="n">n</th><th>what it is</th></tr>
<tr><td>every message</td><td class="n">{data['total']}</td><td>rows in <code>email_log</code></td></tr>
<tr><td>candidates</td><td class="n">{len(cand)}</td><td>the catch-all, plus whatever the lexicon already classified</td></tr>
<tr><td>known delivery mail</td><td class="n">{len(deliv)}</td><td>surface/hold, or produced a record — <b>none of these may be called advertising</b></td></tr>
<tr><td>delivery bodies</td><td class="n">{len(data['delivery_bodies'])}</td><td>from <code>accumulation.payload_json</code>; advertising is never accumulated</td></tr>
<tr><td>candidate bodies</td><td class="n">{data['bodies_on_disk']}</td><td><code>mail_body</code> — cached on demand, not kept at ingest</td></tr>
</table>

<h2>Which features actually separate the two</h2>
<p class="sub">Rates over the two corpora. A feature marked <span class="tag">rejected</span> reads
like a marketing tell and measurably is not — it is excluded from the lexicon, and this table is why.</p>
<table><tr><th>feature</th><th class="n">candidates</th><th class="n">delivery mail</th><th>reads as</th></tr>"""]

    for name, test in SUBJECT_FEATURES:
        left = _rate(cand, data["bodies"], test)
        if "(body)" in name:
            # Body features are measured against the accumulated bodies, not against `email_log`
            # rows whose body is usually absent — comparing a 36% hit rate to a corpus that is 94%
            # empty would say nothing about either.
            blank = {"subject": "", "sender": "", "po_hints": "", "attachment_count": 0}
            right = 100.0 * sum(1 for h in data["delivery_bodies"] if test(blank, h))                     / max(1, len(data["delivery_bodies"]))
        else:
            right = _rate(deliv, data["bodies"], test)
        lift = (left + 1) / (right + 1)
        verdict = ("strong ad tell" if lift > 8 else "ad tell" if lift > 3 else
                   "strong business tell" if lift < 0.125 else "business tell" if lift < 0.34
                   else "does not discriminate")
        parts.append(f'<tr><td>{_esc(name)}</td><td class="n">{left:.1f}%</td>'
                     f'<td class="n">{right:.1f}%</td><td>{verdict}</td></tr>')
    parts.append("</table>")

    parts.append(f"""<h2>The lexicon as it stands</h2>
<p class="sub">Weights are applied by <code>pipeline/parsing/promotional.py</code>. A message is
called advertising only on a promotional score of {promotional.ADVERTISING_AT}+ <b>and a business
score of exactly zero</b> — one business signal is an absolute veto, never a subtraction.</p>
<table><tr><th class="n">weight</th><th>promotional signal</th></tr>""")
    for signal in promotional.PROMOTIONAL:
        parts.append(f'<tr><td class="n">{signal.weight}</td><td>{_esc(signal.why)}</td></tr>')
    parts.append('</table><table><tr><th class="n">weight</th><th>business signal (a veto)</th></tr>')
    for signal in promotional.BUSINESS:
        parts.append(f'<tr><td class="n">{signal.weight}</td><td>{_esc(signal.why)}</td></tr>')
    parts.append("</table>")

    fp = (f'<span class="ok">0 — none of the {len(deliv)} known delivery mails is called '
          f'advertising</span>' if not wrong else
          f'<span class="bad">{len(wrong)} — INVESTIGATE</span>')
    parts.append(f"""<h2>Where the {len(cand)} candidates land</h2>
<table><tr><th>band</th><th class="n">n</th><th>what happens to it</th></tr>
<tr><td>advertising</td><td class="n">{bands[promotional.ADVERTISING]}</td>
    <td>hidden — <code>rule_5e_bulk_mail_noise</code>, listed on Not deliveries, reversible in two clicks</td></tr>
<tr><td>maybe</td><td class="n">{bands[promotional.MAYBE]}</td>
    <td>queued and badged — <code>rule_5f_possible_advertising</code>, for a person to settle</td></tr>
<tr><td>neither</td><td class="n">{bands[promotional.NEITHER]}</td>
    <td>routed exactly as before; the lexicon claims nothing</td></tr>
</table>
<p><b>False positives:</b> {fp}</p>""")

    if wrong:
        parts.append('<table><tr><th>sender domain</th><th>subject</th></tr>')
        for r in wrong[:40]:
            parts.append(f'<tr><td>{_esc(_domain(r["sender"]))}</td>'
                         f'<td>{_esc((r["subject"] or "")[:90])}</td></tr>')
        parts.append("</table>")

    maybes = [r for r in cand if data["bands"][r["email_id"]].band == promotional.MAYBE]
    parts.append(f"<h2>The {len(maybes)} a person has to settle</h2>"
                 '<p class="sub">Every one of these is on Needs a human under the '
                 '<b>Maybe advertising</b> chip, carrying the reason below.</p>'
                 '<table><tr><th>sender domain</th><th>subject</th><th>why it is unclear</th></tr>')
    for r in maybes:
        why = ", ".join(data["bands"][r["email_id"]].matched[:3])
        parts.append(f'<tr><td>{_esc(_domain(r["sender"]))}</td>'
                     f'<td>{_esc((r["subject"] or "")[:70])}</td><td>{_esc(why)}</td></tr>')
    parts.append("</table>")

    hidden = Counter(_domain(r["sender"]) for r in cand
                     if data["bands"][r["email_id"]].band == promotional.ADVERTISING)
    parts.append("<h2>Hidden as advertising, by sender domain</h2>"
                 '<table><tr><th>sender domain</th><th class="n">n</th></tr>')
    for dom, n in hidden.most_common():
        parts.append(f'<tr><td>{_esc(dom)}</td><td class="n">{n}</td></tr>')
    parts.append("</table></body></html>")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("".join(parts), encoding="utf-8")
    return out_path


def run(*, out: str = "", db_path=None) -> int:
    conn = state_db.get_connection(db_path or settings.PIPELINE_STATE_DB_PATH)
    try:
        data = gather(conn)
    finally:
        conn.close()

    bands = Counter(v.band for v in data["bands"].values())
    wrong = sum(1 for r in data["delivery"]
                if data["delivery_bands"][r["email_id"]].band == promotional.ADVERTISING)

    print(f"messages in the store        {data['total']}")
    print(f"candidates examined          {len(data['candidates'])}")
    print(f"known delivery mail          {len(data['delivery'])}")
    print(f"delivery bodies recovered    {len(data['delivery_bodies'])}")
    print()
    print(f"  advertising (hidden)       {bands[promotional.ADVERTISING]}")
    print(f"  maybe (queued, badged)     {bands[promotional.MAYBE]}")
    print(f"  neither (unchanged)        {bands[promotional.NEITHER]}")
    print()
    print(f"  delivery mail called advertising: {wrong}"
          + ("   <- INVESTIGATE" if wrong else "   (none, as it must be)"))

    target = Path(out or DEFAULT_OUT) / "mail_lexicon_analysis.html"
    print(f"\nwrote {build(data, target)}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="", help=f"directory for the report (default {DEFAULT_OUT})")
    args = parser.parse_args(argv)
    return run(out=args.out)


if __name__ == "__main__":
    sys.exit(main())
