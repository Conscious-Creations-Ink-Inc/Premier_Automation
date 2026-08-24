"""Query layer for the reconciliation domain: the match verdict per extracted record, the
human decision audit trail, and the mock staged receipts an approval produces.

These three tables always move together inside one decision, so they live in one module.
`MatchRow` is defined here rather than in the service so the dependency runs one way only:
services import stores, never the reverse.
"""
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

# Review lifecycle. Records the automation resolved on its own are born `auto_approved`;
# everything flagged is born `pending_review` and waits for a person.
REVIEW_AUTO_APPROVED = "auto_approved"
REVIEW_PENDING = "pending_review"
REVIEW_APPROVED = "approved"
REVIEW_CANCELLED = "cancelled"

_MATCH_COLUMNS = (
    "extracted_record_id", "po_line_id", "po_signal", "spec_signal", "desc_signal",
    "desc_score", "signals_matched", "confidence", "missing_fields", "flagged",
    "flag_reason", "route_target", "review_status", "notes", "created_at", "updated_at",
)


@dataclass
class MatchRow:
    """One reconciliation verdict. `id` is None until it is written."""
    extracted_record_id: int
    confidence: str                       # high | medium | low | none
    route_target: str                     # pipeline.models.RouteTarget value
    review_status: str
    po_line_id: Optional[int] = None
    po_signal: bool = False
    spec_signal: bool = False
    desc_signal: bool = False
    desc_score: float = 0.0
    signals_matched: int = 0
    missing_fields: List[str] = field(default_factory=list)
    flagged: bool = False
    flag_reason: str = ""
    notes: str = ""
    created_at: str = ""
    updated_at: Optional[str] = None
    id: Optional[int] = None

    @property
    def is_open(self) -> bool:
        """Still sitting in the exception queue waiting on a person."""
        return self.review_status == REVIEW_PENDING


# --- match_results ------------------------------------------------------------


def write(conn: sqlite3.Connection, match: MatchRow, now: str) -> int:
    match.created_at = match.created_at or now
    placeholders = ", ".join(["?"] * len(_MATCH_COLUMNS))
    cursor = conn.execute(
        f"INSERT INTO match_results ({', '.join(_MATCH_COLUMNS)}) VALUES ({placeholders})",
        tuple(_to_sql(column, getattr(match, column)) for column in _MATCH_COLUMNS),
    )
    conn.commit()
    match.id = cursor.lastrowid
    return match.id


def get(conn: sqlite3.Connection, match_id: int) -> Optional[MatchRow]:
    rows = _query(conn, "SELECT * FROM match_results WHERE id = ?", (match_id,))
    return rows[0] if rows else None


def get_by_record(conn: sqlite3.Connection, extracted_record_id: int) -> Optional[MatchRow]:
    rows = _query(
        conn, "SELECT * FROM match_results WHERE extracted_record_id = ?", (extracted_record_id,)
    )
    return rows[0] if rows else None


def list_matches(
    conn: sqlite3.Connection,
    flagged: Optional[bool] = None,
    review_status: Optional[str] = None,
    confidence: Optional[str] = None,
) -> List[MatchRow]:
    clauses, params = [], []
    if flagged is not None:
        clauses.append("flagged = ?")
        params.append(int(flagged))
    if review_status is not None:
        clauses.append("review_status = ?")
        params.append(review_status)
    if confidence is not None:
        clauses.append("confidence = ?")
        params.append(confidence)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return _query(conn, f"SELECT * FROM match_results{where} ORDER BY id", tuple(params))


def list_exception_queue(conn: sqlite3.Connection) -> List[MatchRow]:
    """The flagged items still awaiting a human — what the Exception Queue screen shows."""
    return _query(
        conn,
        "SELECT * FROM match_results WHERE flagged = 1 AND review_status = ? ORDER BY id",
        (REVIEW_PENDING,),
    )


def apply_decision(
    conn: sqlite3.Connection,
    match_id: int,
    review_status: str,
    now: str,
    po_line_id: Optional[int] = None,
    route_target: Optional[str] = None,
    notes: Optional[str] = None,
) -> None:
    """Records the outcome of a human decision. `po_line_id` is passed when the reviewer picked
    a different candidate than the automation's best guess."""
    sets = ["review_status = ?", "updated_at = ?"]
    params: List = [review_status, now]
    if po_line_id is not None:
        sets.append("po_line_id = ?")
        params.append(po_line_id)
    if route_target is not None:
        sets.append("route_target = ?")
        params.append(route_target)
    if notes is not None:
        sets.append("notes = ?")
        params.append(notes)
    params.append(match_id)
    conn.execute(f"UPDATE match_results SET {', '.join(sets)} WHERE id = ?", tuple(params))
    conn.commit()


def counts_by(conn: sqlite3.Connection, column: str) -> Dict[str, int]:
    """Grouped tallies for the dashboard. `column` is restricted to a known allow-list because
    it is interpolated into SQL."""
    if column not in ("confidence", "review_status", "route_target"):
        raise ValueError(f"not a groupable column: {column}")
    rows = conn.execute(f"SELECT {column}, COUNT(*) FROM match_results GROUP BY {column}").fetchall()
    return {row[0]: row[1] for row in rows}


# --- review_decisions (audit trail) -------------------------------------------

_DECISION_COLUMNS = (
    "match_result_id", "extracted_record_id", "decision", "decided_by", "decided_at",
    "reason", "resulting_status", "po_line_id", "receipt_id",
)


@dataclass
class ReviewDecision:
    match_result_id: int
    extracted_record_id: int
    decision: str            # approve | cancel
    decided_by: str
    decided_at: str
    resulting_status: str
    reason: str = ""
    po_line_id: Optional[int] = None
    receipt_id: Optional[int] = None
    id: Optional[int] = None


def write_decision(conn: sqlite3.Connection, decision: ReviewDecision) -> int:
    placeholders = ", ".join(["?"] * len(_DECISION_COLUMNS))
    cursor = conn.execute(
        f"INSERT INTO review_decisions ({', '.join(_DECISION_COLUMNS)}) VALUES ({placeholders})",
        tuple(getattr(decision, column) for column in _DECISION_COLUMNS),
    )
    conn.commit()
    decision.id = cursor.lastrowid
    return decision.id


def decisions_for_match(conn: sqlite3.Connection, match_id: int) -> List[ReviewDecision]:
    return _query_decisions(
        conn, "SELECT * FROM review_decisions WHERE match_result_id = ? ORDER BY id", (match_id,)
    )


def recent_decisions(conn: sqlite3.Connection, limit: int = 20) -> List[ReviewDecision]:
    return _query_decisions(
        conn, "SELECT * FROM review_decisions ORDER BY id DESC LIMIT ?", (limit,)
    )


# --- staged_receipts (mock stage 6) -------------------------------------------

_RECEIPT_COLUMNS = (
    "extracted_record_id", "po_line_id", "shipment_number", "purchase_order", "item_number",
    "item_description", "vendor", "carrier_name", "pro_number", "quantity", "quantity_types",
    "act_delivery_date", "delivery_location", "comments", "created_by", "created_at",
)


def write_receipt(conn: sqlite3.Connection, values: Dict) -> int:
    placeholders = ", ".join(["?"] * len(_RECEIPT_COLUMNS))
    cursor = conn.execute(
        f"INSERT INTO staged_receipts ({', '.join(_RECEIPT_COLUMNS)}) VALUES ({placeholders})",
        tuple(values.get(column) for column in _RECEIPT_COLUMNS),
    )
    conn.commit()
    return cursor.lastrowid


def receipt_for_record(conn: sqlite3.Connection, extracted_record_id: int) -> Optional[Dict]:
    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM staged_receipts WHERE extracted_record_id = ? ORDER BY id LIMIT 1",
            (extracted_record_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.row_factory = prior_factory


def staged_po_line_ids(conn: sqlite3.Connection) -> set:
    """Every PO line that already has a staged receipt.

    Fetched for all lines at once rather than per line: the PO list view would otherwise issue one
    query per line, and the whole point of that page is to show every PO on one screen.
    """
    return {row[0] for row in conn.execute(
        "SELECT DISTINCT po_line_id FROM staged_receipts WHERE po_line_id IS NOT NULL"
    ).fetchall()}


def count_receipts(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM staged_receipts").fetchone()[0]


# --- row conversion -----------------------------------------------------------


def _to_sql(column: str, value):
    if column == "missing_fields":
        return ",".join(value or [])
    if column in ("flagged", "po_signal", "spec_signal", "desc_signal"):
        return int(bool(value))
    return value


def _from_sql(column: str, value):
    if column == "missing_fields":
        return [part for part in (value or "").split(",") if part]
    if column in ("flagged", "po_signal", "spec_signal", "desc_signal"):
        return bool(value)
    return value


def _query(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> List[MatchRow]:
    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return [
            MatchRow(id=row["id"], **{c: _from_sql(c, row[c]) for c in _MATCH_COLUMNS})
            for row in rows
        ]
    finally:
        conn.row_factory = prior_factory


def _query_decisions(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> List[ReviewDecision]:
    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return [
            ReviewDecision(id=row["id"], **{c: row[c] for c in _DECISION_COLUMNS}) for row in rows
        ]
    finally:
        conn.row_factory = prior_factory
