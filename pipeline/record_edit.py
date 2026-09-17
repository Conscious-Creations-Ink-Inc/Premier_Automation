"""Correct a record a person is looking at, from what they can see that the machine could not.

`record_completion` fills what the *proof of delivery* asserts — the date it arrived, who signed —
and refuses to take those from a form, because a form where somebody types a delivery date is a
form where somebody can type the wrong one. That rule is right and is not weakened here.

This is for the fields no delivery note contains. Measured on the live queue, the twenty-one
incomplete records are missing a spec code seventeen times, a description sixteen, a quantity
sixteen. No POD carries any of them; a person reads them off the purchase order. Until this
existed, the only controls on those rows were `Fill`, which cannot supply them, and `Verify`, which
compares against an order and writes nothing unless the spec matches exactly.

**The whitelist is the safety boundary.** `extracted_store.EDITABLE_FIELDS` names the fields this
module may write and ignores anything else rather than raising, so a stale form cannot write
somewhere it should not. Two omissions are load-bearing and the reasoning for both lives beside
that tuple: `po_number`, because changing which purchase order a delivery is against would silently
move a receipt onto another budget line; and `po_line_number`, because a free-text line number does
not merely risk a typo — it *satisfies* the `post_decision` gate that exists to catch one.

Every field this does write is recorded in `record_edits`, one row per field, in the same
transaction as the change itself. `extracted_records.updated_at` and `created_by` say that somebody
touched the row and who; they have never said which field, and never what it used to hold.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple

from api.stores.extracted_store import EDITABLE_FIELDS
from pipeline import completeness

CONFLICT_MARKER = "quantity_conflict"
"""What `reconcile_cross_source_duplicates` appends when two sources state different quantities.

`read_views._READY_CLAUSE` excludes any record carrying it, and **nothing ever cleared it**. So a
reviewer could settle all forty-four conflicts by typing the right number and every record would
stay off the Records page for ever, blocked by a flag describing a disagreement they had just
resolved. Settling the quantity is what the row is for, so settling it has to clear the flag.
"""

_NUMERIC = {
    "quantity_received": "a delivered quantity cannot be negative",
    "package_quantity": "a package count cannot be negative",
}
"""The fields stored as REAL, with the sentence each one's refusal needs.

A mapping rather than a tuple because the message is per field. Membership tests read the same,
and `_validate` has to say "a package count" where it used to say "a delivered quantity" — the
shared sentence was already wrong the moment a second number joined the form.
"""


@dataclass
class Edited:
    ok: bool
    message: str
    applied: List[str] = field(default_factory=list)
    remaining: List[str] = field(default_factory=list)
    is_complete: bool = False
    conflict_resolved: bool = False
    errors: List[str] = field(default_factory=list)


def _clean(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _unchanged(name: str, old: str, new: str) -> bool:
    """Whether a submitted value says the same thing as the one already stored.

    String equality is the wrong test for a number. A REAL column reads back as `41.0`, and the
    edit form strips that trailing `.0` before putting it in a box somebody is about to retype —
    so re-saving a record nobody actually changed compared `"41"` against `"41.0"`, called it a
    correction, wrote the column and stamped `updated_at`. Harmless while nothing was watching;
    the moment `record_edits` existed it meant every save logged edits that never happened, which
    is worse than no log at all.
    """
    if old == new:
        return True
    if name not in _NUMERIC or not old or not new:
        return False
    try:
        return float(old) == float(new)
    except ValueError:
        return False


def _validate(fields: Mapping[str, Any]) -> Dict[str, str]:
    """Refuse what would be stored wrong rather than storing it and finding out later.

    Two checks, both from values that reached the live store through the manual form: a quantity of
    `"6"` in the *unit* box, and a delivery date of `"17/08/2026"`. Everything downstream compares
    dates as ISO strings — the date filter, the sort, `receipt_log` — so a `DD/MM/YYYY` value sorts
    and filters wrongly and does it silently.
    """
    errors: Dict[str, str] = {}
    for name, complaint in _NUMERIC.items():
        raw = _clean(fields.get(name))
        if not raw:
            continue
        try:
            if float(raw) < 0:
                errors[name] = complaint
        except ValueError:
            errors[name] = f"{raw!r} is not a number"

    day = _clean(fields.get("pod_stated_date"))
    if day:
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            errors["pod_stated_date"] = (
                f"{day!r} is not a date in YYYY-MM-DD form — 2025-09-15, not 15/09/2025")
    return errors


def apply(conn: sqlite3.Connection, row: Any, fields: Mapping[str, Any], *,
          edited_by: str = "", now: Optional[str] = None) -> Edited:
    """Write a reviewer's corrections onto one record. Returns what changed and what still blocks it."""
    edited_by = _clean(edited_by)
    if not edited_by:
        return Edited(ok=False, message="say who is making this correction — it is recorded "
                                        "against the record and printed on the report")

    problems = _validate(fields)
    if problems:
        return Edited(ok=False, message="nothing was saved — " + "; ".join(problems.values()),
                      errors=sorted(problems))

    record_id = int(row["id"])
    updates: Dict[str, Any] = {}
    applied: List[str] = []
    # What to write to `record_edits`, gathered here because this is the only place that still holds
    # the old value — a moment later the UPDATE has replaced it and nothing can recover it.
    changes: List[Tuple[str, str, str]] = []
    for name in EDITABLE_FIELDS:
        if name not in fields:
            continue                      # absent from the form is not the same as cleared
        new = _clean(fields.get(name))
        old = _clean(row[name] if name in row.keys() else None)
        if _unchanged(name, old, new):
            continue
        updates[name] = float(new) if name in _NUMERIC and new else (new or None)
        changes.append((name, old, new))
        label = completeness.LABELS.get(name, name)
        applied.append(f"{label} = {new or '(cleared)'}" + (f" (was {old})" if old else ""))

    source = _clean(row["extraction_source"] if "extraction_source" in row.keys() else "")

    # Confirming the number already there settles the conflict just as much as changing it does.
    # A conflict says two sources disagreed and nobody chose; a reviewer submitting a quantity is
    # the choosing, and on 44 of the 50 live conflicts the stored quantity was already the right
    # one. Under the `new == old` rule above, `quantity_received` never reached `updates`, so the
    # clause below never fired and the form answered "nothing was changed" — leaving the reviewer
    # no way at all to clear a flag they had just resolved. Checked against the submitted fields
    # rather than against `updates` for exactly that reason.
    settles_conflict = (
        CONFLICT_MARKER in source
        and "quantity_received" in fields
        and _clean(fields.get("quantity_received")) != ""
    )

    if not updates and not settles_conflict:
        gaps = completeness.gaps(row)
        return Edited(ok=False, is_complete=gaps.is_complete,
                      message="nothing was changed",
                      remaining=[completeness.LABELS.get(f, f) for f in gaps.missing_required])

    stamp = now or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    resolved = False
    if settles_conflict:
        # Leaving the marker would keep the record off the Records page permanently — see
        # CONFLICT_MARKER.
        updates["extraction_source"] = source.replace(f"+{CONFLICT_MARKER}", "").replace(
            CONFLICT_MARKER, "").strip("+ ") or "manual"
        # Recorded like any other change. Rewriting a provenance column on a person's behalf is
        # precisely the kind of write an auditor asks about, and it is the one field on this row
        # that nobody typed.
        changes.append(("extraction_source", source, updates["extraction_source"]))
        applied.append(f"quantity conflict settled by {edited_by}")
        resolved = True

    # Column names come from `EDITABLE_FIELDS` and this module's own literals, never from a
    # request; only values bind.
    assignments = ", ".join(f"{name} = ?" for name in updates)
    conn.execute(
        f"UPDATE extracted_records SET {assignments}, updated_at = ?, created_by = "
        f"COALESCE(NULLIF(created_by, ''), ?) WHERE id = ?",
        (*updates.values(), stamp, edited_by, record_id))
    # In the same transaction as the UPDATE above, deliberately. `sqlite3` opens one implicitly on
    # the first write and the `commit()` below is the only one, so a failure here rolls the column
    # change back with it. There is no state in which the value moved and nothing recorded that it
    # did — which is the whole reason this is not a separate call afterwards.
    conn.executemany(
        "INSERT INTO record_edits (record_id, field, old_value, new_value, edited_by, edited_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(record_id, name, before or None, after or None, edited_by, stamp)
         for name, before, after in changes])
    conn.commit()

    after = dict(row)
    after.update({k: v for k, v in updates.items()})
    gaps = completeness.gaps(after)
    return Edited(
        ok=True, applied=applied, is_complete=gaps.is_complete, conflict_resolved=resolved,
        remaining=[completeness.LABELS.get(f, f) for f in gaps.missing_required],
        message=("this record is complete and has moved to Records" if gaps.is_complete
                 else f"saved — still {gaps.describe()}"))


def history(conn: sqlite3.Connection, record_id: int, limit: int = 12) -> List[sqlite3.Row]:
    """What has already been corrected on this record, newest first.

    Read back on the edit form so a reviewer opening a record can see it has been through hands
    before — the single question `updated_at` could never answer. `limit` is a screenful: this is
    context beside a form, not an audit export, and a record with forty edits has a bigger problem
    than a truncated list.
    """
    prior = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT field, old_value, new_value, edited_by, edited_at FROM record_edits "
            "WHERE record_id = ? ORDER BY id DESC LIMIT ?", (record_id, limit)).fetchall()
    finally:
        conn.row_factory = prior
