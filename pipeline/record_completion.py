"""Fill a record's gaps from its own proof of delivery, plus the line a reviewer chose.

Three of the nine fields `completeness.REQUIRED` demands are routinely missing on records read
from an email body rather than from the POD itself: `pod_stated_date`, `received_by` and
`po_line_number`. Until they are filled the record cannot be posted — `post_decision` refuses it,
correctly, because a receiver with no delivery date is a claim with nothing behind it.

**Two of the three are facts the POD already asserts**, and this module takes them from there
rather than from a person's memory:

    Delivery date: Sep 10, 2025 10:18   -> pod_stated_date
    Signed for by: U ALI                -> received_by

That distinction is the whole point of the module. A form where somebody types a delivery date is
a form where somebody can type the wrong delivery date, and the receipt that reaches Premier's ERP
would carry it as fact. Reading it off the proof means the record and the file attached beside it
say the same thing, and `parsing/pod.py` already does the reading.

The third, `po_line_number`, is genuinely not in the POD — a FedEx delivery note knows nothing
about Spitfire's line numbering — so it comes from the reviewer, and is checked against the lines
the purchase order actually has.

**Nothing already filled is overwritten.** If Stage 3 extracted a delivery date, that is the one
the reviewer has been looking at; silently replacing it here would change what they thought they
were approving.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List, Optional

from pipeline import completeness


def _labels(gaps) -> List[str]:
    """The missing fields in the words `completeness` already uses, so one vocabulary reaches the
    reviewer rather than this module inventing a second set of names for the same cells."""
    return [completeness.LABELS.get(f, f) for f in gaps.missing_required]


@dataclass
class Completion:
    """What was filled, what is still missing, and whether the record can now be posted."""
    ok: bool
    message: str
    applied: List[str] = field(default_factory=list)
    """Human-readable "field = value" for each gap closed, shown back to the reviewer."""
    remaining: List[str] = field(default_factory=list)
    is_complete: bool = False


def complete(conn: sqlite3.Connection, row: Any, *, line: Optional[int] = None,
             now: Optional[str] = None) -> Completion:
    """Fill what can be filled on one record. Returns what changed without hiding what did not."""
    record_id = int(row["id"])
    po_number = str(row["po_number"] or "").strip()

    pod_doc, pod_error = _pod_facts(conn, row)
    if pod_error:
        return Completion(ok=False, message=pod_error,
                          remaining=_labels(completeness.gaps(row)))

    # The POD must be this record's POD. A delivery note naming other purchase orders is either
    # the wrong attachment or a multi-PO shipment we cannot split, and either way its date and
    # signature are not evidence for *this* line.
    if pod_doc is not None and pod_doc.po_numbers and po_number not in pod_doc.po_numbers:
        return Completion(
            ok=False,
            message=(f"the proof of delivery names purchase order(s) "
                     f"{', '.join(pod_doc.po_numbers)}, not {po_number} — it is not evidence for "
                     f"this record"))

    updates = {}
    applied = []

    if pod_doc is not None:
        if not str(row["pod_stated_date"] or "").strip() and pod_doc.delivery_date:
            updates["pod_stated_date"] = pod_doc.delivery_date
            applied.append(f"POD date = {pod_doc.delivery_date} (from the proof of delivery)")
        if not str(row["received_by"] or "").strip() and pod_doc.signed_for_by:
            updates["received_by"] = pod_doc.signed_for_by
            applied.append(f"received-by = {pod_doc.signed_for_by} (signed for, on the proof)")

    if line is not None and row["po_line_number"] is None:
        known = _lines_for(conn, po_number)
        if known and line not in known:
            return Completion(
                ok=False,
                message=(f"purchase order {po_number} has no line {line} — its lines are "
                         f"{', '.join(str(n) for n in known)}"))
        updates["po_line_number"] = line
        applied.append(f"PO line # = {line} (chosen by the reviewer)")

    if not updates:
        gaps = completeness.gaps(row)
        return Completion(
            ok=False, is_complete=gaps.is_complete,
            message=("nothing to fill — this record is already complete" if gaps.is_complete else
                     "nothing here could be filled from the proof of delivery; the remaining "
                     "fields have to come from Premier"),
            remaining=_labels(gaps))

    _write(conn, record_id, updates, now or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    after = dict(row)
    after.update(updates)
    gaps = completeness.gaps(after)
    return Completion(
        ok=True, is_complete=gaps.is_complete, applied=applied,
        remaining=_labels(gaps),
        message=("this record is now complete and can be posted" if gaps.is_complete else
                 f"still {gaps.describe()}"))


@dataclass
class _PodFacts:
    """What the proof of delivery asserts, however its text was obtained."""
    po_numbers: List[str]
    delivery_date: Optional[str]
    signed_for_by: Optional[str]


def apply_verification(conn: sqlite3.Connection, row: Any, verification: Any, *,
                       now: Optional[str] = None) -> Completion:
    """Keep what a verification worked out, when it is certain enough to keep.

    `po_verify` resolves the purchase-order line and then throws it away — it renders the number
    into the popup and the request ends. Nothing wrote it, so `completeness` went on reporting
    `PO line #` missing and `post_decision` went on refusing the record, for every record in the
    store but one.

    **Only an exact match is kept**, because this decides which budget line gets charged.
    `LineCheck.spec_resolved` is True when the record's own spec code selected the line; False
    means it was reached "by description alone, which is a weaker claim and is said so on screen".
    A weaker claim stays on screen and goes no further — those records keep their place in the
    manual queue, where a person picks the line.

    The quantity must agree too. A line matched by spec but disagreeing on quantity is exactly the
    case a reviewer needs to look at, not one to settle automatically.

    A line a reviewer chose is never overwritten: `reviewer_chose` outranks the scorer, and so does
    anything already stored.
    """
    matched = getattr(verification, "matched", None)
    if matched is None or matched.line_number is None:
        return Completion(ok=False, message="no line was resolved")
    if row["po_line_number"] is not None:
        return Completion(ok=False, message="this record already carries a line")
    if not (matched.spec_resolved or getattr(matched, "matched_on_parent", False)):
        return Completion(
            ok=False,
            message=("the line was matched on description alone, which is not certain enough to "
                     "record — choose it yourself if it is right"))
    if matched.qty_agrees is not True:
        return Completion(
            ok=False,
            message=("the matched line disagrees on quantity, so it is left for a person to "
                     "settle"))

    _write(conn, int(row["id"]), {"po_line_number": int(matched.line_number)},
           now or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    after = dict(row)
    after["po_line_number"] = int(matched.line_number)
    gaps = completeness.gaps(after)
    return Completion(
        ok=True, is_complete=gaps.is_complete, remaining=_labels(gaps),
        applied=[f"PO line # = {matched.line_number} (exact spec match on {matched.spec_code}, "
                 f"quantity agrees)"],
        message=("this record is now complete and can be posted" if gaps.is_complete
                 else f"still {gaps.describe()}"))


def _pod_facts(conn: sqlite3.Connection, row: Any):
    """What this record's proof of delivery says, or a sentence saying why there isn't one.

    Reads the verdict `stage3_extract` already recorded on the ledger row — `is_pod` and the
    parsed facts beside it. That is what lets a photographed POD work here at all: its text came
    from OCR, a paid call made once on the way in, and re-running it to answer a question a column
    already answers would charge Premier for every press of a button.

    Only when there is no stored verdict does it fall back to reading the file, and then only a
    PDF, whose text layer is free. A row with no verdict is either older than the column or was
    never dispatched.

    `spitfire_post._pod_for` picks *which* attachment, reused rather than reimplemented — it
    already excludes inline signature logos and matches the POD's own purchase order. Imported
    inside the function because that module pulls in the write client, and nothing about reading a
    POD should depend on being able to write to Spitfire.
    """
    from pipeline import spitfire_post
    from pipeline.parsing import pod as pod_parser
    from pipeline.stage3_extract.pdf_adapter import _page_texts

    pod = spitfire_post._pod_for(conn, row)
    if pod is None:
        return None, ("this record has no proof of delivery with stored bytes, so there is "
                      "nothing to read the delivery date and receiver from")

    stored = conn.execute(
        """SELECT COALESCE(pod_po_numbers, '') AS po_numbers, pod_delivery_date, pod_signed_by
             FROM attachment_ledger
            WHERE email_id = ? AND filename = ? AND COALESCE(is_pod, 0) = 1
            ORDER BY depth, ordinal LIMIT 1""",
        (str(row["source_email_id"] or ""), pod.filename)).fetchone()
    if stored is not None:
        return _PodFacts(
            po_numbers=[p for p in str(stored["po_numbers"] or "").split(",") if p],
            delivery_date=stored["pod_delivery_date"],
            signed_for_by=stored["pod_signed_by"]), ""

    if pod.kind != "pdf":
        return None, (f"the proof of delivery is a {pod.kind or 'file'} and was not read at "
                      f"ingest, so its delivery date and receiver are not available here — "
                      f"reprocess the email to have it read")

    document = pod_parser.parse_pod("\n".join(_page_texts(pod.content)))
    if document is None:
        return None, ("the attached file does not read as a proof of delivery — no delivery "
                      "information was found in it")
    return _PodFacts(po_numbers=document.po_numbers or [],
                     delivery_date=document.delivery_date,
                     signed_for_by=document.signed_for_by), ""


def _lines_for(conn: sqlite3.Connection, po_number: str) -> List[int]:
    """The line numbers this purchase order actually has, from the mirror. Empty if unmirrored."""
    rows = conn.execute(
        "SELECT line_number FROM spitfire_po_lines WHERE po_number = ? ORDER BY line_number",
        (po_number,)).fetchall()
    return [int(r[0]) for r in rows if r[0] is not None]


def _write(conn: sqlite3.Connection, record_id: int, updates: dict, now: str) -> None:
    # Column names come from this module's own literals, never from a request; only values bind.
    assignments = ", ".join(f"{name} = ?" for name in updates)
    conn.execute(
        f"UPDATE extracted_records SET {assignments}, updated_at = ? WHERE id = ?",
        tuple(updates.values()) + (now, record_id))
    conn.commit()
