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
from pipeline import attachment_ledger, evidence, extracted_records_store, ingest_orchestrator, stage2_accumulate, state_db
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
    evidence_cache = evidence.EvidenceCache()
    adapters = ingest_orchestrator.build_default_adapters()
    try:
        released = []
        for email in emails:
            # Same order as process_new_mail: ledger every attachment, read them, and only then
            # decide whether the mail matters. Two corpus threads carry no PO in any body.
            attachment_ledger.observe(conn, email, None, _now())
            email_evidence = evidence.gather(conn, email, text.body_text_of(email), adapters, now=_now())
            evidence_cache.put(email_evidence)
            triaged = triage(email, evidence=email_evidence)
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
        records_by_file: Dict[str, list] = defaultdict(list)
        for event in released:
            raw = []
            for source in ingest_orchestrator._sources_for_delivery_event(event):
                if source.source_type == "attachment":
                    produced = evidence_cache.records_for(source.source_email_id, event.key.po_number)
                else:
                    produced = ingest_orchestrator.dispatch.dispatch_source(
                        conn, source, adapters,
                        budget=ingest_orchestrator.containers.Budget.fresh(), now=_now(),
                    )
                for record in produced:
                    if not ingest_orchestrator._belongs_to_event(record, event):
                        continue
                    record.shipment_number = record.shipment_number or event.key.shipment_number
                    raw.append(record)
            for record in ingest_orchestrator.reconcile_cross_source_duplicates(raw):
                records_by_file[file_by_id.get(record.source_email_id, record.source_email_id)].append(record)

        attachment_ledger.close_open_rows(conn, _now())
        ledger = _ledger_snapshot(conn, file_by_id)
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
        "attachments": ledger,
        "detail": per_email,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "corpus_regression.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out_dir / "corpus_regression.md").write_text(_markdown(report), encoding="utf-8")
    return report


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ledger_snapshot(conn, file_by_id: Dict[str, str]) -> dict:
    """Every attachment across the corpus, with the verdict it ended on.

    This is the headline evidence for "nothing is silently dropped": the 30 signature logos are
    visibly dropped *for a stated reason* rather than merely absent, and any attachment in
    Premier's real mail that ends at `no_adapter` is a genuine coverage gap — which is what
    `tests/test_corpus.py` asserts against.
    """
    rows = []
    for record in attachment_ledger._query(conn, "SELECT * FROM attachment_ledger ORDER BY email_id, depth, ordinal"):
        rows.append({
            "file": file_by_id.get(record.email_id, record.email_id),
            "filename": record.filename,
            "container_path": record.container_path,
            "depth": record.depth,
            "kind": record.sniffed_kind,
            "size_bytes": record.size_bytes,
            "disposition": record.disposition,
            "detail": record.disposition_detail,
            "claimed_by": record.claimed_by,
            "records_extracted": record.records_extracted,
        })
    return {
        "rows": rows,
        "by_disposition": attachment_ledger.counts_by_disposition(conn),
        "by_kind": attachment_ledger.counts_by_kind(conn),
        "orphans": len(attachment_ledger.orphans(conn)),
        "unclaimed": [r["filename"] for r in rows if r["disposition"] == attachment_ledger.NO_ADAPTER],
    }


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


def _attachment_section(ledger: dict) -> List[str]:
    if not ledger:
        return []

    rows = ledger["rows"]
    lines = [
        "## Attachments",
        "",
        f"**{len(rows)} attachment(s)** across the corpus, every one with a recorded verdict. "
        f"Orphans (attachments that escaped without one): **{ledger['orphans']}** — this must be zero.",
        "",
        "| Verdict | Count | Meaning |",
        "|---|---|---|",
    ]
    meanings = {
        "extracted": "read, and produced records",
        "empty": "read cleanly, held nothing extractable",
        "container_expanded": "an archive or message; its members have their own rows",
        "dropped_decorative": "signature logo or inline chrome",
        "dropped_duplicate": "byte-identical to one already read",
        "dropped_oversize": "breached an attachment limit",
        "not_dispatched": "the email was routed or hidden before extraction",
        "awaiting_release": "held pending the delivery event that will release it",
        "no_adapter": "**nothing claimed it — a real coverage gap**",
        "unsupported_format": "recognised, deliberately not read",
        "encrypted": "password-protected",
        "corrupt": "unreadable or truncated",
        "unreadable": "an adapter claimed it and failed",
    }
    for disposition, count in sorted(ledger["by_disposition"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| `{disposition}` | {count} | {meanings.get(disposition, '')} |")

    lines += ["", "| Detected kind | Count |", "|---|---|"]
    for kind, count in sorted(ledger["by_kind"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| `{kind}` | {count} |")

    if ledger["unclaimed"]:
        lines += ["", "### Unclaimed — a coverage gap", ""]
        lines += [f"- `{name}`" for name in ledger["unclaimed"]]
    else:
        lines += ["", "No attachment in Premier's real mail went unclaimed.", ""]

    read = [r for r in rows if r["disposition"] in ("extracted", "empty", "container_expanded")]
    if read:
        lines += ["", "### What was actually read", "",
                  "| File | Attachment | Kind | Size | Verdict | Adapter | Records |",
                  "|---|---|---|---|---|---|---|"]
        for r in sorted(read, key=lambda r: -r["records_extracted"]):
            lines.append(
                f"| {r['file'][:34]} | {(r['container_path'] or r['filename'])[:38]} | "
                f"`{r['kind']}` | {r['size_bytes']:,} | {r['disposition']} | "
                f"{r['claimed_by'] or ''} | {r['records_extracted']} |"
            )
    lines.append("")
    return lines


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

    lines += _attachment_section(report.get("attachments") or {})

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
