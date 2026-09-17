"""Fetch the attachments of already-ingested mail, once, so the store holds them too.

Needed because of how the pipeline avoids re-reading mail. `seen_message_ids` means a settled
email is never fetched again, and `skip_ids` now stops it being fetched even to be skipped — so
the backlog will not heal itself by waiting. Every attachment ingested before
`pipeline.attachment_store` existed has exactly one remaining copy, in Premier's Outlook mailbox,
and this is the run that copies it to disk while that is still true.

**Read-only against the mailbox.** GETs only: no move, no flag, no mark-as-read. It reuses
`GraphMailbox._fetch_attachments`, which is what knows how to pull all three attachment kinds —
including the `itemAttachment` `$value` stream that carries a forwarded Delivered Notification,
and which a plain `contentBytes` read silently returns nothing for.

    python -m tools.backfill_attachments --dry-run
    python -m tools.backfill_attachments
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone

from config import settings
from connectors.mailbox import GRAPH_BASE_URL, _SESSION, GraphMailbox
from pipeline import attachment_store, state_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emails_needing_backfill(conn) -> list:
    # `blob_sha256 IS NULL` means "no bytes here" -- which is true of a gap *and* of an attachment
    # whose bytes were released on purpose. Without the two exclusions below this would re-fetch
    # every reclaimed signature logo from Outlook and `attachment_store.put()` it back, silently
    # undoing `tools/reclaim_decorative_blobs.py` and spending ~24,000 Graph calls to do it.
    return [row["email_id"] for row in conn.execute(
        "SELECT email_id, COUNT(*) AS gaps FROM attachment_ledger "
        " WHERE blob_sha256 IS NULL AND sha256 <> '' AND size_bytes > 0 "
        "   AND blob_reclaimed_at IS NULL "
        "   AND disposition <> 'dropped_decorative' "
        " GROUP BY email_id ORDER BY MAX(first_seen_at) DESC"
    ).fetchall()]


def _graph_message_id(box: GraphMailbox, headers: dict, internet_message_id: str):
    """Graph's own folder-scoped id, which is the only handle its attachment routes accept."""
    quoted = internet_message_id.replace("'", "''")
    response = _SESSION.get(
        f"{GRAPH_BASE_URL}/users/{box.mailbox_address}/messages",
        headers=headers,
        params={"$filter": f"internetMessageId eq '{quoted}'",
                "$select": "id,body,hasAttachments", "$top": 1},
        timeout=30,
    )
    response.raise_for_status()
    items = response.json().get("value") or []
    return items[0] if items else None


def _link_stored_rows(conn, email_id: str, now: str) -> int:
    """Point ledger rows at blobs the fetch has just written.

    Matched on the ledger's own `sha256`, not on filename or ordinal: the content hash is the one
    identifier that survives a re-fetch, and `_fetch_attachments` stores by that same hash.
    """
    linked = 0
    for row in conn.execute(
        "SELECT id, sha256 FROM attachment_ledger WHERE email_id = ? AND blob_sha256 IS NULL",
        (email_id,),
    ).fetchall():
        if row["sha256"] and attachment_store.exists(row["sha256"]):
            conn.execute(
                "UPDATE attachment_ledger SET blob_sha256 = ?, blob_stored_at = ? WHERE id = ?",
                (row["sha256"], now, row["id"]),
            )
            linked += 1
    conn.commit()
    return linked


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would be fetched and touch neither Graph nor the disk")
    parser.add_argument("--limit", type=int, default=0, help="stop after this many emails")
    args = parser.parse_args(argv)

    conn = state_db.get_connection(settings.PIPELINE_STATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        pending = _emails_needing_backfill(conn)
        if args.limit:
            pending = pending[:args.limit]
        print(f"{len(pending)} email(s) with attachments that are not in the store")
        if args.dry_run or not pending:
            for email_id in pending:
                print(f"  would fetch {email_id[:70]}")
            return 0

        box = GraphMailbox(read_only=True)
        headers = box._headers()
        linked_total = 0
        for index, email_id in enumerate(pending, start=1):
            try:
                item = _graph_message_id(box, headers, email_id)
                if item is None:
                    # The mail has been deleted or archived out of reach. This is exactly the loss
                    # the store exists to prevent, and for these rows it has already happened.
                    print(f"  [{index}/{len(pending)}] GONE from the mailbox: {email_id[:60]}")
                    continue
                body = (item.get("body") or {}).get("content")
                fetched = box._fetch_attachments(item["id"], headers, body)
                # Store explicitly rather than leaning on the connector's own call. That one runs
                # only on the drop path — it exists to catch bytes just before they are released —
                # so depending on it here silently missed every attachment that was *kept*: the
                # `empty` and `corrupt` rows, which includes the embedded Delivered Notification.
                # A dropped attachment arrives with no bytes and is already in the store.
                for attachment in fetched:
                    attachment_store.put(attachment.content_bytes)
                linked = _link_stored_rows(conn, email_id, _now())
                linked_total += linked
                print(f"  [{index}/{len(pending)}] stored {linked} attachment(s) for {email_id[:52]}")
            except Exception as exc:                               # noqa: BLE001
                print(f"  [{index}/{len(pending)}] FAILED {email_id[:52]}: {type(exc).__name__}: {exc}")
        print(f"\nlinked {linked_total} ledger row(s) to stored bytes")
        print("run `python -m tools.verify_attachments` to confirm")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
