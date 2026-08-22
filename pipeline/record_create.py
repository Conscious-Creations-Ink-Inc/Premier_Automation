"""Build one record from a message a person read themselves.

Everything else in this pipeline stages records by parsing. This is the path for the mail it could
not finish: an image-only body, a portal link instead of an attachment, a property reply saying
"Yes ma'am, this was received!" and naming no purchase order. Premier's own staff complete those
through the review screens rather than the work being dropped, which is why the review UI is a
primary deliverable and not an exception queue.

**A manual record is not a second class of thing.** It lands in `extracted_records` beside every
other row, reaches the same Records page, is refused by the same eight gates in `post_decision`,
and produces a report identical to an automated one bar a single cell. What separates them is
`origin`, written once and never edited, so an audit six months from now can still say which.

Three rules this module exists to keep:

* **The five mandatory fields are refused here, before any INSERT.** The form's `required`
  attributes are courtesy; a browser is not a validator and a POST can arrive without one. Either
  all five are present and a record exists, or nothing was written — there is no draft row and no
  half-saved state to clean up.
* **Only what the email cannot supply is asked for.** Vendor, unit, line number and received-by are
  `completeness.DERIVED` — each has a source already holding the authoritative value, and a form
  where somebody types a vendor is a form where somebody can type the wrong vendor.
* **The delivery is checked against what is already staged.** Two records for one physical delivery
  become two receipts, which is the failure that ended Premier's previous attempt.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from pipeline import completeness, dedupe, extracted_records_store
from pipeline.models import ExtractedRecord
from pipeline.parsing import tokens

MANUAL = "manual"
"""`extracted_records.origin` for a record a person built. `auto` is everything Stage 3 staged."""

POD_ATTACHMENT = "attachment"
POD_EMAIL_BODY = "email_body"


@dataclass
class Created:
    """What happened, in the terms the form has to render back."""
    ok: bool
    message: str
    record_id: Optional[int] = None
    missing: List[str] = field(default_factory=list)
    """The mandatory fields left blank, in `completeness.LABELS`' words — so the reviewer reads one
    vocabulary rather than this module inventing a second set of names for the same cells."""
    duplicate_of: List[int] = field(default_factory=list)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _quantity(value: Any) -> Optional[float]:
    """A quantity, or None if it is not one.

    Zero is a real quantity — an empty shipment, or a line cancelled at the dock — and
    `completeness._is_present` treats it as present, so this must not fold it into None. Only a
    blank or an unparseable string is absent.
    """
    text = _clean(value).replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def prefill(conn: sqlite3.Connection, email_id: str) -> Dict[str, Any]:
    """What the pipeline already worked out about this message, for the form to open on.

    Never a blank form. Something read this mail and produced *something* — a PO number from the
    subject, a spec code from a quoted table — and making a person retype what is already on file
    is both slower and a fresh chance to get it wrong. Where nothing was extracted the fields are
    simply empty, which is the honest state.

    Values are taken from the most recent record staged from this email, then topped up from the
    triage hints on `email_log`, which survive even when extraction produced no record at all —
    and that is exactly the case this form is for.
    """
    conn.row_factory = sqlite3.Row
    values: Dict[str, Any] = {
        "po_number": "", "spec_code": "", "item_description": "",
        "quantity_received": "", "unit_of_measure": "", "pod_stated_date": "",
    }

    staged = conn.execute(
        """SELECT po_number, spec_code, item_description, quantity_received, unit_of_measure,
                  pod_stated_date
             FROM extracted_records WHERE source_email_id = ? ORDER BY id DESC LIMIT 1""",
        (email_id,)).fetchone()
    if staged is not None:
        for key in values:
            if staged[key] is not None and str(staged[key]).strip():
                values[key] = staged[key]

    if not values["po_number"]:
        hints = conn.execute("SELECT po_hints FROM email_log WHERE email_id = ?",
                             (email_id,)).fetchone()
        if hints is not None:
            # `po_hints` is a comma-separated list. One hint is an answer; several is a choice only
            # the reviewer can make, and pre-filling an arbitrary one of them would look like a
            # finding rather than a guess.
            found = [p for p in str(hints["po_hints"] or "").split(",") if p.strip()]
            if len(found) == 1:
                values["po_number"] = found[0].strip()

    # A delivery date the POD already asserts, so the commonest missing field arrives filled from
    # the proof rather than from somebody's memory — the same reasoning as `record_completion`.
    pod = conn.execute(
        """SELECT pod_delivery_date FROM attachment_ledger
            WHERE email_id = ? AND COALESCE(is_pod, 0) = 1
              AND COALESCE(pod_delivery_date, '') <> '' ORDER BY id LIMIT 1""",
        (email_id,)).fetchone()
    if pod is not None and not values["pod_stated_date"]:
        values["pod_stated_date"] = pod["pod_delivery_date"]

    return values


def choosable_attachments(conn: sqlite3.Connection, email_id: str) -> List[sqlite3.Row]:
    """The files on this message a reviewer may nominate as the proof of delivery.

    Inline attachments are excluded — signature logos and letterhead, not evidence — matching
    `spitfire_post._pod_for`. Everything else is offered whatever its type or disposition, because
    the three cases this chooser exists for are all files the automatic rules skip: a photographed
    delivery note, a POD naming another purchase order, and a proof that is not a carrier POD at
    all. A list filtered to what the machine already accepts would help nobody.

    Rows rather than `attachment_ledger.LedgerRow`, which carries neither `ordinal` — the handle
    the viewer and the byte resolver both address a file by — nor the stored POD verdict the
    chooser shows beside each candidate.

    A file whose bytes were never kept is listed but marked unusable, not hidden: "the POD is that
    one and we no longer have it" is a different problem from "there was no POD", and only the
    first is ours to fix.
    """
    prior = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, ordinal, depth, filename, sniffed_kind, size_bytes, disposition,
                      container_path, sha256, blob_sha256,
                      COALESCE(is_pod, 0) AS is_pod,
                      COALESCE(pod_po_numbers, '') AS pod_po_numbers,
                      pod_delivery_date, pod_signed_by
                 FROM attachment_ledger
                WHERE email_id = ? AND COALESCE(is_inline, 0) = 0
                ORDER BY depth, ordinal, id""",
            (email_id,)).fetchall()
    finally:
        conn.row_factory = prior
    return list(rows)


def has_bytes(conn: sqlite3.Connection, row: Any) -> bool:
    """Whether this attachment can actually be uploaded.

    Checked against the blob store by digest rather than by resolving the bytes: the chooser lists
    every attachment on a message and reading each one to find out would load megabytes of PDFs and
    phone photographs to render a list of filenames. `mail_cache` is consulted first because 18 of
    the 35 rows in Premier's live store had their bytes there and nowhere else.
    """
    from pipeline import attachment_store, mail_cache

    # The row factory is set explicitly rather than assumed of the caller. `cached_attachment_bytes`
    # reads its result by column name, so a connection left on the default factory hands back a
    # plain tuple and this raises `TypeError: tuple indices must be integers`. Every caller that
    # happened to run a `read_views` query first was fine; the one that re-renders the form after a
    # refusal opens its own connection and was not.
    prior = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        cached = mail_cache.cached_attachment_bytes(conn, str(_field(row, "email_id") or ""),
                                                    _field(row, "ordinal"))
    finally:
        conn.row_factory = prior
    if cached is not None and cached["content"]:
        return True
    for column in ("blob_sha256", "sha256"):
        digest = _field(row, column)
        if digest and attachment_store.exists(str(digest)):
            return True
    return False


def _field(row: Any, name: str) -> Any:
    try:
        return row[name]
    except (IndexError, KeyError, TypeError):
        return getattr(row, name, None)


def create(conn: sqlite3.Connection, *, email_id: str, created_by: str,
           values: Dict[str, Any], pod_ledger_id: Optional[int] = None,
           waive_pod: bool = False, note: str = "",
           now: Optional[str] = None) -> Created:
    """Validate and insert one manual record. Nothing is written unless everything checks out.

    `pod_ledger_id` names the attachment the reviewer chose. `waive_pod` is their explicit
    acceptance that this delivery has no proof document and may post without one — the two are
    mutually exclusive, and one of them must be true, because "nobody has decided yet" is not a
    state a record may be created in.
    """
    now = now or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    created_by = _clean(created_by)
    if not created_by:
        return Created(ok=False, message="a record has to say who created it")

    conn.row_factory = sqlite3.Row
    if conn.execute("SELECT 1 FROM email_log WHERE email_id = ?", (email_id,)).fetchone() is None:
        # Always from a specific message, so `source_email_id` is never null and the evidence,
        # the POD chooser and the mail popup all keep working on the row afterwards.
        return Created(ok=False, message=f"no message {email_id!r} has been through the pipeline")

    fields = {
        "po_number": _clean(values.get("po_number")),
        "spec_code": _clean(values.get("spec_code")),
        "item_description": _clean(values.get("item_description")),
        "quantity_received": _quantity(values.get("quantity_received")),
        "pod_stated_date": _clean(values.get("pod_stated_date")),
    }

    # --- the hard block ---------------------------------------------------------------------
    gaps = completeness.gaps(fields)
    if not gaps.is_complete:
        return Created(ok=False, missing=[completeness.LABELS.get(f, f)
                                          for f in gaps.missing_required],
                       message="this record cannot be created until " + gaps.describe())

    if pod_ledger_id is None and not waive_pod:
        return Created(
            ok=False,
            message=("choose which attachment is the proof of delivery, or say explicitly that "
                     "this delivery has none and may be posted without one"))
    if pod_ledger_id is not None and waive_pod:
        return Created(ok=False,
                       message="a record cannot both have a proof of delivery and waive one")

    if pod_ledger_id is not None:
        chosen = conn.execute(
            "SELECT id FROM attachment_ledger WHERE id = ? AND email_id = ?",
            (pod_ledger_id, email_id)).fetchone()
        if chosen is None:
            # Scoped to this message on purpose: a ledger id names a row in a table covering every
            # email, and an unscoped one could hang another delivery's proof on this receipt.
            return Created(ok=False,
                           message="that attachment is not on this message")

    # --- the duplicate check ----------------------------------------------------------------
    key = dedupe.delivery_key(
        po_number=fields["po_number"], spec_code=fields["spec_code"],
        quantity=fields["quantity_received"], pod_stated_date=fields["pod_stated_date"],
        pod_sha256=_sha_of(conn, pod_ledger_id))
    already = dedupe.find_by_delivery_key(conn, key)
    if already:
        ids = [int(r["id"]) for r in already]
        return Created(
            ok=False, duplicate_of=ids,
            message=(f"this looks like a delivery already recorded as "
                     f"{', '.join(f'record #{i}' for i in ids)} — {fields['quantity_received']:g} "
                     f"of {fields['spec_code']} against PO {fields['po_number']} on "
                     f"{fields['pod_stated_date']}. Open it rather than creating a second one."))

    record = ExtractedRecord(
        source_email_id=email_id,
        po_number=fields["po_number"],
        spec_code=fields["spec_code"],
        item_description=fields["item_description"],
        quantity_received=fields["quantity_received"],
        pod_stated_date=fields["pod_stated_date"],
        unit_of_measure=_clean(values.get("unit_of_measure")) or None,
        email_date=_email_date(conn, email_id),
        # Left to `completeness.DERIVED` and filled from the matched purchase order at report
        # time. A form where somebody types a vendor is a form where they can type a wrong one.
        vendor_name=None, carrier_name=None, tracking_number=None,
        shipment_number=None, delivery_location=None, comments=None,
        # Spec grammar, applied to what the reviewer typed rather than to what an adapter read.
        # `STE-402-LT-B` and `STE-402-LT-SH` are Spitfire lines 300 and 301, and a record that does
        # not carry the parent cannot be matched against either.
        **_spec_parts(fields["spec_code"]),
        extraction_source="manual",
        # A person read the message. That is a stronger claim than any parse, and `_READY_CLAUSE`
        # needs it above zero for the row to reach the Records page at all.
        extraction_confidence=1.0,
        raw_snippet=_clean(note)[:500] or None,
    )

    record_id = extracted_records_store.write_pending(conn, record, now, extra={
        "origin": MANUAL,
        "created_by": created_by,
        "manual_note": _clean(note) or None,
        "pod_ledger_id": pod_ledger_id,
        "pod_source": POD_ATTACHMENT if pod_ledger_id is not None else POD_EMAIL_BODY,
        "pod_waived_by": created_by if waive_pod else None,
        "pod_waived_at": now if waive_pod else None,
        "delivery_key": key,
    })

    _mark_handled(conn, email_id, now)

    return Created(
        ok=True, record_id=record_id,
        message=(f"record #{record_id} created" + (
            "" if pod_ledger_id is not None else
            " — with no proof of delivery, which you accepted; the receipt will carry none")))


def _spec_parts(spec_code: str) -> Dict[str, Any]:
    """`parent_spec_code` and `sub_spec_suffix` for a spec a person typed.

    Sub-parts are separate Spitfire lines, so a record carrying only the full code cannot be
    matched to either of them. `normalize_spec` is the same grammar the adapters use, so a typed
    spec and a parsed one decompose identically.
    """
    try:
        parsed = tokens.normalize_spec(spec_code)
    except Exception:                              # noqa: BLE001 — a typed spec may be anything
        return {"parent_spec_code": None, "sub_spec_suffix": None}
    if parsed is None:
        return {"parent_spec_code": None, "sub_spec_suffix": None}
    return {"parent_spec_code": getattr(parsed, "parent", None) or None,
            "sub_spec_suffix": getattr(parsed, "sub_part", None) or None}


def _sha_of(conn: sqlite3.Connection, pod_ledger_id: Optional[int]) -> str:
    """The content hash of the chosen POD, which is part of what makes a delivery identifiable."""
    if pod_ledger_id is None:
        return ""
    row = conn.execute("SELECT sha256 FROM attachment_ledger WHERE id = ?",
                       (pod_ledger_id,)).fetchone()
    return str(row["sha256"] or "") if row is not None else ""


def _email_date(conn: sqlite3.Connection, email_id: str) -> str:
    """The message's own date, for the record's `email_date` — a NOT NULL column.

    Deliberately not used as the delivery date: twelve of the fourteen corpus files were forwarded
    on one day, so the envelope date is that day for nearly all of them. The delivery date is a
    mandatory field on the form for exactly that reason.
    """
    row = conn.execute("SELECT email_date, processed_at FROM email_log WHERE email_id = ?",
                       (email_id,)).fetchone()
    if row is None:
        return datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(row["email_date"] or row["processed_at"] or "")


def _mark_handled(conn: sqlite3.Connection, email_id: str, now: str) -> None:
    """Record that a person completed this message, so extraction does not stage more from it.

    Scoped to this one message and never to its thread or its purchase order: the next mail on the
    same thread may be a genuinely separate delivery, and suppressing that would hide a real
    receipt. The stamp is visible on the mail row, because a suppression nobody can see is one
    nobody can trust.
    """
    conn.execute("UPDATE email_log SET handled_manually = 1 WHERE email_id = ?", (email_id,))
    conn.commit()


def records_from(conn: sqlite3.Connection, email_id: str) -> Sequence[sqlite3.Row]:
    """Records a person created from this message — what the mail popup reports back to them."""
    prior = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT id, po_number, spec_code, created_by, created_at FROM extracted_records "
            "WHERE source_email_id = ? AND origin = ? ORDER BY id",
            (email_id, MANUAL)).fetchall()
    finally:
        conn.row_factory = prior


def waive_pod(conn: sqlite3.Connection, record_id: int, *, by: str,
              now: Optional[str] = None) -> Created:
    """Accept that an *automatically* staged record has no proof of delivery and may post anyway.

    The manual form covers records a person builds. This covers the other half: a record extraction
    staged from a body-only notification, which `post_decision` refuses and which nothing else can
    unblock. Same rule, same stored fact, same single writer — and it stays a deliberate act by a
    named person rather than anything a run can do on its own.
    """
    now = now or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    by = _clean(by)
    if not by:
        return Created(ok=False, message="a waiver has to name who gave it")

    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM extracted_records WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        return Created(ok=False, message=f"there is no record #{record_id}")
    if _clean(row["pod_waived_by"]):
        return Created(ok=True, record_id=record_id,
                       message=f"already accepted without a proof of delivery by "
                               f"{row['pod_waived_by']}")

    conn.execute(
        "UPDATE extracted_records SET pod_waived_by = ?, pod_waived_at = ?, pod_source = ?, "
        "updated_at = ? WHERE id = ?",
        (by, now, POD_EMAIL_BODY, now, record_id))
    conn.commit()
    return Created(
        ok=True, record_id=record_id,
        message=(f"record #{record_id} may now be posted with no proof of delivery attached. "
                 f"The receipt will carry the receiver report and nothing else."))
