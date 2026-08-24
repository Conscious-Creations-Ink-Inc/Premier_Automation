"""What the parser pulled out of each mail — step 1 of the workflow.

Every row here started as an email body or attachment; `extraction_source` and `raw_snippet`
record what was actually read, so a reviewer can always trace a value back to its evidence.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api import deps, schemas
from api.stores import emails_store, extracted_store, po_lines_store, reconciliation_store

router = APIRouter(prefix="/api/extracted-records", tags=["extracted"])


def _to_row(record_row, matches, lines, emails) -> schemas.ExtractedRow:
    match = matches.get(record_row.id)
    po_line = lines.get(match.po_line_id) if match and match.po_line_id is not None else None
    email = emails.get(record_row.record.source_email_id)
    return schemas.ExtractedRow(
        record=schemas.ExtractedRecordRowSchema.model_validate(record_row),
        match=schemas.MatchSchema.model_validate(match) if match else None,
        po_line=schemas.POLineSchema.from_row(po_line) if po_line else None,
        email=schemas.EmailContextSchema.model_validate(email) if email else None,
    )


def _caches(conn):
    return (
        {match.extracted_record_id: match for match in reconciliation_store.list_matches(conn)},
        {row.id: row for row in po_lines_store.list_all(conn)},
        {email.email_id: email for email in emails_store.list_all(conn)},
    )


@router.get("", response_model=List[schemas.ExtractedRow])
def list_records(
    status: Optional[str] = Query(default=None, description="pending | matched | failed | routed"),
    q: Optional[str] = Query(default=None, description="Search PO, spec or description."),
    conn=Depends(deps.get_conn),
) -> List[schemas.ExtractedRow]:
    matches, lines, emails = _caches(conn)
    return [
        _to_row(row, matches, lines, emails)
        for row in extracted_store.list_all(conn, status=status, search=q)
    ]


@router.get("/{record_id}", response_model=schemas.ExtractedRow)
def get_record(record_id: int, conn=Depends(deps.get_conn)) -> schemas.ExtractedRow:
    row = extracted_store.get(conn, record_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No such extracted record: {record_id}.")
    matches, lines, emails = _caches(conn)
    return _to_row(row, matches, lines, emails)
