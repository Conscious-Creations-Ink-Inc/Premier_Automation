"""Inbox organizer — clearing the repetitive status mail out of the way.

The point is what's *left*: after filing, the inbox holds only order confirmations,
cancellations and anything unclear — the mail a person actually has to read. Everything else is
the same delivery being announced three times, which belongs in a folder.

The proposed folder for each mail comes from the pipeline's own map in `config.settings`, so
this screen always agrees with what the real orchestrator would do.
"""
from typing import Dict, List

from fastapi import APIRouter, Depends

from api import deps, schemas, util
from api.stores import emails_store

router = APIRouter(prefix="/api/inbox", tags=["inbox"])


def _preview(conn) -> schemas.InboxPreview:
    emails = emails_store.list_all(conn)

    by_folder: Dict[str, int] = {}
    pending = kept = filed = 0
    for email in emails:
        if email.keep_in_inbox:
            kept += 1
        elif email.is_organized:
            filed += 1
        else:
            pending += 1
            by_folder[email.proposed_folder] = by_folder.get(email.proposed_folder, 0) + 1

    return schemas.InboxPreview(
        rows=[
            schemas.InboxRow(
                email_id=e.email_id, received_at=e.received_at, subject=e.subject,
                sender_address=e.sender_address, notification_type=e.notification_type,
                triage_category=e.triage_category, reason=e.reason,
                proposed_folder=e.proposed_folder, keep_in_inbox=e.keep_in_inbox,
                organized_folder=e.organized_folder, moved_at=e.moved_at,
            )
            for e in emails
        ],
        pending_move=pending, kept_in_inbox=kept, already_filed=filed, by_folder=by_folder,
    )


@router.get("/preview", response_model=schemas.InboxPreview)
def preview(conn=Depends(deps.get_conn)) -> schemas.InboxPreview:
    return _preview(conn)


@router.post("/organize", response_model=schemas.OrganizeResponse)
def organize(
    payload: schemas.OrganizeRequest, conn=Depends(deps.get_conn)
) -> schemas.OrganizeResponse:
    """Simulates the Graph folder move. Nothing touches a real mailbox — the app has no mail
    permissions in this phase — but the bookkeeping is exactly what the move would produce."""
    pending = emails_store.list_pending_organize(conn)
    targets = (
        [e.email_id for e in pending]
        if payload.email_ids is None
        else [e.email_id for e in pending if e.email_id in set(payload.email_ids)]
    )

    moved_folders: Dict[str, int] = {}
    for email in pending:
        if email.email_id in set(targets):
            moved_folders[email.proposed_folder] = moved_folders.get(email.proposed_folder, 0) + 1

    moved = emails_store.mark_organized(conn, targets, util.now_iso())
    return schemas.OrganizeResponse(
        moved=moved, remaining=_preview(conn).pending_move, by_folder=moved_folders
    )
