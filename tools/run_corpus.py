"""Corpus regression run — the real June `.msg` files through the real pipeline.

Runs Stages 1 -> 2 -> 3 over `Documents/Premier/5,8 june` and scores the result against the
ground truth Premier gave us for free: every file was forwarded by their expeditor with a
one-line annotation ("straightforward, WH rec'd", "Human at property confirmed delivery",
"anything else weird that happened on this one!!"), and the filenames name the case. Those
annotations are the labels.

Scored per corpus class (A-E) rather than per file format, because class is what predicts
production accuracy — the classes differ in *where* the three fields live, not in whether the
file is HTML or PDF.

Read-only: the connector is opened with `read_only=True`, so Premier's sample folder is never
modified. Usage:

    python -m tools.run_corpus [--corpus DIR] [--out DIR]
"""

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from connectors.msg_file import MsgFileMailbox
from pipeline import extracted_records_store, ingest_orchestrator, stage2_accumulate, state_db
from pipeline.models import TriageCategory
from pipeline.parsing import text, thread
from pipeline.vendors import authority
from pipeline.stage1_triage import triage

DEFAULT_CORPUS = Path(r"D:\Premier\Documents\Premier\5,8 june")
DEFAULT_OUT = Path(r"D:\Premier\dev_reports")

# --- Ground truth ------------------------------------------------------------
# `class`    — the corpus class from Documents/Premier_Delivery_Email_Corpus_Analysis.md
# `category` — what triage must decide
# `lines`    — receivable lines the *parser* must read out of the document, where the format
#              states them; None where the evidence is unreadable (photographed PODs) and the
#              correct behaviour is a routed exception
# `staged`   — records the *orchestrator* stages in a single pass, which is a different number.
#              A Delivered notice is held pending its Inbound partner, and the second copy of an
#              already-released notice is deduped, so both legitimately stage nothing.

EXPECTED: Dict[str, dict] = {
    "WH Inbound - 11 bases, 12 tops of STE-402.msg": {
        "class": "A", "category": TriageCategory.SURFACE, "notice": "239336",
        "pos": ["208491"], "shipment": "50052", "lines": 11, "staged": 11,
        "note": "direct from Authority; unequal parts (11 bases, 12 shades of STE-402)",
    },
    "WH Inbound with equal parts.msg": {
        "class": "A", "category": TriageCategory.SURFACE, "notice": "239336",
        "pos": ["208491"], "shipment": "50052", "lines": 11, "staged": 0,
        "note": "the same notice 239336, forwarded — must triage identically to the direct copy",
    },
    "WH Inbound - 1 base only (final part of STE-402).msg": {
        "class": "A", "category": TriageCategory.SURFACE, "notice": "239475",
        "pos": ["208491"], "shipment": None, "lines": 1, "staged": 1,
        "note": "blank ALS Shipment # — the key must be None, not the inbound number",
    },
    "WH Inbound with multiple POs.msg": {
        "class": "A", "category": TriageCategory.SURFACE, "notice": "239260",
        "pos": ["206725", "207665"], "shipment": "50009", "lines": 13, "staged": 13,
        "note": "13 lines across 2 POs; each PO releases its own event and must not duplicate",
    },
    "Delivered Notification - NOT A REC - related to the Inbound with multiple POs.msg": {
        "class": "B", "category": TriageCategory.HOLD, "notice": "50009",
        "pos": ["206725", "207665"], "shipment": "50009", "lines": 10, "staged": 0,
        "note": "pairs with Inbound 239260 on shipment 50009 — the double-receipt trap",
    },
    "WH Inbound - Straightforward.msg": {
        "class": "B", "category": TriageCategory.HOLD, "notice": "50033",
        "pos": ["206534"], "shipment": "50033", "lines": 2, "staged": 0,
        "note": "filename says Inbound but the content is Delivered 50033",
    },
    "WH Lost Item.msg": {
        "class": "E", "category": TriageCategory.ROUTE, "notice": None,
        "pos": None, "shipment": None, "lines": 0, "staged": 0,
        "note": "PO status report thread discussing a lost item and replacement PO — human only",
    },
    "5star Fabric Verification Response.msg": {
        "class": "D", "category": TriageCategory.HOLD, "notice": None,
        "pos": ["210634", "210635", "210636"], "shipment": None, "lines": None, "staged": None,
        "note": "partial confirmation naming only a spec; PO/qty live in the quoted request grid",
    },
    "5star Fabric Verification w attachments.msg": {
        "class": "D", "category": TriageCategory.HOLD, "notice": None,
        "pos": ["210634", "210635", "210636"], "shipment": None, "lines": None, "staged": None,
        "note": "same thread with the FedEx POD PDFs attached",
    },
    "Del to Property - Straightforward 1.msg": {
        "class": "C", "category": TriageCategory.HOLD, "notice": None,
        "pos": ["212448"], "shipment": None, "lines": None, "staged": None,
        "note": "short human confirmation from the property",
    },
    "Del to Property - used excel attachment to track 2.msg": {
        "class": "C2", "category": TriageCategory.HOLD, "notice": None,
        "pos": None, "shipment": None, "lines": None, "staged": None,
        "note": "no PO in any body — everything is in Cameo Receivers.xlsx",
    },
    "Del to Property - used excel attachment to track due to slow property responses.msg": {
        "class": "C2", "category": TriageCategory.HOLD, "notice": None,
        "pos": None, "shipment": None, "lines": None, "staged": None,
        "note": "7 POs in the subject plus a 92-row tracker",
    },
    "Del to Property - manual check against BOLs due to name missing on address.msg": {
        "class": "C3", "category": TriageCategory.ROUTE, "notice": None,
        "pos": None, "shipment": None, "lines": None, "staged": None,
        "note": "evidence is five 3 MB pallet photos; correct behaviour is a routed exception",
    },
    "OS&E Cintas - Property Confirmation.msg": {
        "class": "C", "category": TriageCategory.HOLD, "notice": None,
        "pos": ["211310", "212578"], "shipment": None, "lines": None, "staged": None,
        "note": "117 tables, 64 hops — the boilerplate/thread stress case",
    },
}


def run(corpus_dir: Path, out_dir: Path) -> dict:
    mailbox = MsgFileMailbox(corpus_dir, read_only=True)
    emails = mailbox.fetch_new()
    by_id = {email.email_id: email for email in emails}
    file_by_id = {email_id: Path(path).name for email_id, path in mailbox._path_by_email_id.items()}

    conn = state_db.get_connection(":memory:")
    per_email: List[dict] = []
    try:
        released = []
        for email in emails:
            triaged = triage(email)
            row = {
                "file": file_by_id.get(email.email_id, email.email_id),
                "subject": email.subject,
                "origin_sender": triaged.origin_sender_address,
                "category": triaged.category.value,
                "rule": triaged.matched_rule,
                "notification_number": triaged.notification_number,
                "po_hints": triaged.extracted_po_hints,
                "shipment_hint": triaged.extracted_shipment_hint,
                "attachments": [a.filename for a in email.attachments],
                "reason": triaged.reason,
            }
            row["parsed_lines"] = _parsed_line_count(email, triaged)
            per_email.append(row)
            if triaged.category in (TriageCategory.SURFACE, TriageCategory.HOLD):
                released.extend(stage2_accumulate.process_triaged_email(conn, triaged, _now()))

        released.extend(stage2_accumulate.sweep_stale_holds(conn, datetime.now(timezone.utc)))

        # Mirrors process_new_mail's extraction loop exactly, including the cross-source
        # reconcile — without it, an Inbound and the Delivered notice bundled with it both stage
        # their lines and the run reports twice the receipts the pipeline would really create.
        adapters = ingest_orchestrator.build_default_adapters()
        records_by_file: Dict[str, list] = defaultdict(list)
        for event in released:
            raw = []
            for source in ingest_orchestrator._sources_for_delivery_event(event):
                for record in ingest_orchestrator.run_adapters(source, adapters):
                    if not ingest_orchestrator._belongs_to_event(record, event):
                        continue
                    record.shipment_number = record.shipment_number or event.key.shipment_number
                    raw.append(record)
            for record in ingest_orchestrator.reconcile_cross_source_duplicates(raw):
                records_by_file[file_by_id.get(record.source_email_id, record.source_email_id)].append(record)
    finally:
        conn.close()

    for row in per_email:
        row["records"] = [asdict(r) for r in records_by_file.get(row["file"], [])]

    scored = _score(per_email)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "corpus": str(corpus_dir),
        "emails": len(emails),
        "score": scored,
        "detail": per_email,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "corpus_regression.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out_dir / "corpus_regression.md").write_text(_markdown(report), encoding="utf-8")
    return report


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parsed_line_count(email, triaged) -> Optional[int]:
    """Lines the vendor parser reads out of the document, independent of accumulation state.

    Kept separate from the staged count on purpose. A held Delivered notice and a deduped
    second copy of an already-released Inbound both stage nothing — correctly — but both are
    still fully parsed, and a parser regression in either would otherwise hide behind a zero
    that the pipeline was right to produce.
    """
    notice = authority.parse_authority_notice(
        triaged.origin_sender_address or email.sender_address,
        thread.strip_forward_prefixes(email.subject),
        email.body_html,
        text.body_text_of(email),
    )
    if notice is None:
        return None
    return len([line for line in notice.lines if line.quantity is not None])


def _score(per_email: List[dict]) -> dict:
    by_class = defaultdict(lambda: {"files": 0, "triage_ok": 0, "parsed_ok": 0, "parsed_scored": 0,
                                    "staged_ok": 0, "staged_scored": 0})
    failures: List[str] = []
    unlabelled: List[str] = []

    for row in per_email:
        expected = EXPECTED.get(row["file"])
        if expected is None:
            unlabelled.append(row["file"])
            continue
        bucket = by_class[expected["class"]]
        bucket["files"] += 1

        if row["category"] == expected["category"].value:
            bucket["triage_ok"] += 1
        else:
            failures.append(
                f"{row['file']}: triage expected {expected['category'].value}, got "
                f"{row['category']} via {row['rule']}"
            )

        if expected["pos"] is not None and sorted(row["po_hints"]) != sorted(expected["pos"]):
            missing = sorted(set(expected["pos"]) - set(row["po_hints"]))
            if missing:
                failures.append(f"{row['file']}: POs missing {missing} (got {row['po_hints']})")

        if expected["shipment"] != row["shipment_hint"] and expected["notice"]:
            failures.append(
                f"{row['file']}: shipment key expected {expected['shipment']!r}, got {row['shipment_hint']!r}"
            )

        if expected["lines"] is not None:
            bucket["parsed_scored"] += 1
            if row["parsed_lines"] == expected["lines"]:
                bucket["parsed_ok"] += 1
            else:
                failures.append(
                    f"{row['file']}: parser expected {expected['lines']} quantified line(s), "
                    f"got {row['parsed_lines']}"
                )

        if expected["staged"] is not None:
            bucket["staged_scored"] += 1
            actual = len([r for r in row["records"] if r.get("quantity_received") is not None])
            if actual == expected["staged"]:
                bucket["staged_ok"] += 1
            else:
                failures.append(
                    f"{row['file']}: expected {expected['staged']} staged record(s), got {actual}"
                )

    totals = {
        key: sum(b[key] for b in by_class.values())
        for key in ("files", "triage_ok", "parsed_ok", "parsed_scored", "staged_ok", "staged_scored")
    }
    return {
        "by_class": {k: dict(v) for k, v in sorted(by_class.items())},
        "totals": totals,
        "failures": failures,
        "unlabelled_files": unlabelled,
    }


def _markdown(report: dict) -> str:
    score = report["score"]
    lines = [
        "# Corpus regression run",
        "",
        f"**Generated** {report['generated_at']}  ",
        f"**Corpus** `{report['corpus']}` — {report['emails']} messages  ",
        "",
        "Real Premier `.msg` files run through Stages 1-3 of the real pipeline. Scored per corpus",
        "class, using the expeditor's forwarding annotations as ground truth.",
        "",
        "## Score",
        "",
        "| Class | Files | Triage correct | Lines parsed | Records staged |",
        "|---|---|---|---|---|",
    ]

    def ratio(ok: int, scored: int) -> str:
        return f"{ok}/{scored}" if scored else "n/a"

    for name, bucket in score["by_class"].items():
        lines.append(
            f"| {name} | {bucket['files']} | {bucket['triage_ok']}/{bucket['files']} | "
            f"{ratio(bucket['parsed_ok'], bucket['parsed_scored'])} | "
            f"{ratio(bucket['staged_ok'], bucket['staged_scored'])} |"
        )
    totals = score["totals"]
    lines += [
        f"| **All** | **{totals['files']}** | **{totals['triage_ok']}/{totals['files']}** | "
        f"**{ratio(totals['parsed_ok'], totals['parsed_scored'])}** | "
        f"**{ratio(totals['staged_ok'], totals['staged_scored'])}** |",
        "",
        "*Lines parsed* is what the vendor parser reads out of the document. *Records staged* is",
        "what a single pipeline pass writes, which is legitimately lower: a Delivered notice is",
        "held pending its Inbound partner, and a second copy of an already-released notice is",
        "deduped. Both numbers are scored so a parser regression cannot hide behind a zero the",
        "pipeline was right to produce.",
        "",
    ]

    if score["failures"]:
        lines += ["## Failures", ""] + [f"- {f}" for f in score["failures"]] + [""]
    else:
        lines += ["No failures.", ""]

    if score["unlabelled_files"]:
        lines += ["## Unlabelled files (no ground truth recorded)", ""]
        lines += [f"- {f}" for f in score["unlabelled_files"]] + [""]

    lines += ["## Per-message detail", ""]
    for row in report["detail"]:
        expected = EXPECTED.get(row["file"], {})
        lines += [
            f"### {row['file']}",
            "",
            f"- **Class** {expected.get('class', '?')} — {expected.get('note', '')}",
            f"- **Origin sender** `{row['origin_sender']}`",
            f"- **Triage** `{row['category']}` via `{row['rule']}`" + (f" — {row['reason']}" if row["reason"] else ""),
            f"- **POs** {row['po_hints']} · **Shipment** `{row['shipment_hint']}` · "
            f"**Notice** `{row['notification_number']}`",
            f"- **Attachments kept** {row['attachments'] or 'none'}",
            f"- **Lines parsed** {row['parsed_lines'] if row['parsed_lines'] is not None else 'n/a'} · "
            f"**Records staged** {len(row['records'])}",
        ]
        quantified = [r for r in row["records"] if r.get("quantity_received") is not None]
        if quantified:
            lines += ["", "| PO | Line | Spec | Qty | UOM | Pkg | Date | Carrier | Conf |", "|---|---|---|---|---|---|---|---|---|"]
            for r in quantified[:20]:
                lines.append(
                    f"| {r['po_number']} | {r.get('po_line_number') or ''} | {r.get('spec_code') or ''} | "
                    f"{r['quantity_received']:g} | {r.get('unit_of_measure') or ''} | "
                    f"{('%g %s' % (r['package_quantity'], r['package_uom'])) if r.get('package_quantity') else ''} | "
                    f"{r.get('pod_stated_date') or ''} | {r.get('carrier_name') or ''} | "
                    f"{r['extraction_confidence']:.2f} |"
                )
            if len(quantified) > 20:
                lines.append(f"| … | | | +{len(quantified) - 20} more | | | | | |")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    report = run(args.corpus, args.out)
    score = report["score"]
    totals = score["totals"]
    print(f"messages: {report['emails']}")
    print(f"triage:   {totals['triage_ok']}/{totals['files']}")
    print(f"parsed:   {totals['parsed_ok']}/{totals['parsed_scored']}")
    print(f"staged:   {totals['staged_ok']}/{totals['staged_scored']}")
    for failure in score["failures"]:
        print(f"  FAIL {failure}")
    print(f"report:   {args.out / 'corpus_regression.md'}")
    return 1 if score["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
