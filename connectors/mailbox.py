import base64
import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional

import msal
import requests

from config import settings
from pipeline.models import Attachment, RawEmail

_logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
MAX_PAGES_PER_POLL = 20   # 20 x $top=50 = 1000 messages; a guard, not a real ceiling
GRAPH_WELL_KNOWN_FOLDERS = {"inbox", "drafts", "sentitems", "deleteditems", "archive", "junkemail", "outbox"}


class Mailbox(ABC):
    @abstractmethod
    def fetch_new(self) -> List[RawEmail]: ...

    @abstractmethod
    def mark_processed(self, email_id: str, folder: str) -> None: ...


class LocalFolderMailbox(Mailbox):
    """Reads sample emails from a local folder so the pipeline runs without live mailbox access.

    Each email is one *.json file (RawEmail's fields) sitting directly in `folder`. Attachment
    binary content is loaded from `folder.parent / "attachments" / <file>` if a `file` key is
    given, or from inline text for tiny fixtures (see sample_data/emails/).
    """

    def __init__(self, folder: Path):
        self.folder = folder

    def fetch_new(self) -> List[RawEmail]:
        emails = []
        for path in sorted(self.folder.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            attachments = [self._load_attachment(a) for a in data.get("attachments", [])]
            emails.append(RawEmail(
                email_id=data["email_id"],
                received_at=data["received_at"],
                sender_address=data["sender_address"],
                sender_domain=data["sender_domain"],
                subject=data["subject"],
                body_html=data.get("body_html"),
                body_text=data.get("body_text"),
                attachments=attachments,
            ))
        return emails

    def _load_attachment(self, meta: dict) -> Attachment:
        if "file" in meta:
            content = (self.folder.parent / "attachments" / meta["file"]).read_bytes()
        else:
            content = meta.get("inline_text", "").encode("utf-8")
        return Attachment(filename=meta["filename"], content_type=meta["content_type"], content_bytes=content)

    def mark_processed(self, email_id: str, folder: str) -> None:
        target_dir = self.folder / folder
        target_dir.mkdir(parents=True, exist_ok=True)
        src = self.folder / f"{email_id}.json"
        if src.exists():
            src.rename(target_dir / f"{email_id}.json")


class GraphMailbox(Mailbox):
    """Real Microsoft Graph API connector (client-credentials / app-only auth).

    Needs: an Azure AD app registration scoped to the receiving mailbox, Mail.Read + Mail.ReadWrite
    application permissions with admin consent, and — before pointing this at Premier's real
    mailbox — an Application Access Policy restricting this app to only the one mailbox (not yet
    in place for the test tenant; see BuildPlan/STAGE_1_INGEST_AND_TRIAGE.md and
    ourDocs/JOE_FOLLOWUPS_CHECKLIST.md item #4).
    """

    def __init__(
        self,
        tenant_id: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        mailbox_address: Optional[str] = None,
    ):
        self.tenant_id = tenant_id or settings.GRAPH_TENANT_ID
        self.client_id = client_id or settings.GRAPH_CLIENT_ID
        self.client_secret = client_secret or settings.GRAPH_CLIENT_SECRET
        self.mailbox_address = mailbox_address or settings.GRAPH_MAILBOX_ADDRESS
        missing = [
            name for name, value in [
                ("tenant_id", self.tenant_id), ("client_id", self.client_id),
                ("client_secret", self.client_secret), ("mailbox_address", self.mailbox_address),
            ] if not value
        ]
        if missing:
            raise ValueError(f"GraphMailbox is missing required config: {', '.join(missing)} (check .env)")
        self._app = msal.ConfidentialClientApplication(
            self.client_id,
            authority=settings.GRAPH_AUTHORITY_TEMPLATE.format(tenant_id=self.tenant_id),
            client_credential=self.client_secret,
        )
        self._folder_id_cache: dict = {}  # display name -> resolved folder id, so repeated moves don't re-lookup

    def _access_token(self) -> str:
        result = self._app.acquire_token_silent(settings.GRAPH_SCOPE, account=None)
        if not result:
            result = self._app.acquire_token_for_client(scopes=settings.GRAPH_SCOPE)
        if "access_token" not in result:
            raise RuntimeError(
                f"Graph API auth failed: {result.get('error')}: {result.get('error_description')}"
            )
        return result["access_token"]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._access_token()}"}

    def fetch_new(self) -> List[RawEmail]:
        """Every message in the Inbox, following pagination, oldest first.

        Three fixes over the original single-page call:

        * **`@odata.nextLink` is followed.** `$top=50` with the link ignored silently lost every
          message beyond the fiftieth in a poll (finding C11).
        * **Oldest first**, so a poll truncated by `MAX_PAGES_PER_POLL` still makes forward
          progress instead of re-reading the same newest page forever.
        * **Per-message try/except.** One malformed message used to abort the whole poll, because
          the original built the list in a comprehension (finding C8).
        """
        headers = self._headers()
        url = f"{GRAPH_BASE_URL}/users/{self.mailbox_address}/mailFolders/Inbox/messages"
        params = {
            "$top": 50,
            "$orderby": "receivedDateTime asc",
            "$select": "id,internetMessageId,receivedDateTime,subject,from,body,hasAttachments",
        }

        emails: List[RawEmail] = []
        pages = 0
        while url and pages < MAX_PAGES_PER_POLL:
            resp = requests.get(url, headers=headers, params=params if pages == 0 else None, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
            for msg in payload.get("value", []):
                try:
                    emails.append(self._to_raw_email(msg, headers))
                except Exception as e:
                    _logger.warning("skipping message %s: %s: %s",
                                    msg.get("id"), type(e).__name__, e, exc_info=True)
            url = payload.get("@odata.nextLink")
            pages += 1

        if url:
            _logger.warning("poll truncated after %s pages; more mail remains in the Inbox", pages)
        return emails

    def _to_raw_email(self, msg: dict, headers: dict) -> RawEmail:
        sender_address = msg.get("from", {}).get("emailAddress", {}).get("address", "") or ""
        body = msg.get("body", {})
        body_content = body.get("content")
        is_html = body.get("contentType") == "html"

        # Graph reports hasAttachments=false for a message whose only images are inline, so
        # relying on it alone loses every pasted-in photograph.
        has_inline = bool(body_content and "cid:" in body_content)
        attachments = (self._fetch_attachments(msg["id"], headers, body_content)
                       if msg.get("hasAttachments") or has_inline else [])

        # internetMessageId is stable; the folder-scoped `id` changes the moment a message is
        # moved — which this pipeline does to every message it processes. Using `id` as the
        # dedupe key meant moved mail could be re-ingested (finding C5).
        return RawEmail(
            email_id=msg.get("internetMessageId") or f"graph:{msg['id']}",
            received_at=msg["receivedDateTime"],
            sender_address=sender_address,
            sender_domain=sender_address.split("@")[-1] if "@" in sender_address else "",
            subject=msg.get("subject", ""),
            body_html=body_content if is_html else None,
            body_text=body_content if not is_html else None,
            attachments=attachments,
            provider_message_id=msg["id"],
        )

    def _fetch_attachments(
        self, message_id: str, headers: dict, body_content: Optional[str] = None
    ) -> List[Attachment]:
        """All three attachment kinds Graph can return, paginated.

        `itemAttachment` — an attached Outlook message — was silently discarded, and it is how a
        forwarded notification arrives, the single most common shape in Premier's mail
        (finding C12). Its bytes come from the `/$value` endpoint and are handed on as a `.msg`,
        which the container adapter unwraps.

        `referenceAttachment` — a OneDrive/SharePoint link — carries no content and fetching it
        would need `Files.Read.All` and a separate consent conversation. It is recorded with its
        URL so a person can open it, rather than vanishing.
        """
        from pipeline.parsing import sniff

        base = f"{GRAPH_BASE_URL}/users/{self.mailbox_address}/messages/{message_id}/attachments"
        url = base
        cids = sniff.referenced_cids(body_content)
        attachments: List[Attachment] = []
        pages = 0

        while url and pages < MAX_PAGES_PER_POLL:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
            for att in payload.get("value", []):
                try:
                    parsed = self._to_attachment(att, base, headers, cids)
                    if parsed is not None:
                        attachments.append(parsed)
                except Exception as e:
                    _logger.warning("skipping attachment %s on %s: %s: %s",
                                    att.get("name"), message_id, type(e).__name__, e, exc_info=True)
            url = payload.get("@odata.nextLink")
            pages += 1
        return attachments

    def _to_attachment(
        self, att: dict, base_url: str, headers: dict, cids: set
    ) -> Optional[Attachment]:
        from pipeline.parsing import sniff

        odata_type = att.get("@odata.type", "")
        name = att.get("name") or "unnamed"
        content_id = att.get("contentId")
        is_inline = bool(att.get("isInline")) or bool(content_id and str(content_id).strip("<>") in cids)

        if odata_type == "#microsoft.graph.fileAttachment":
            data = base64.b64decode(att["contentBytes"])
        elif odata_type == "#microsoft.graph.itemAttachment":
            # The item's raw bytes; `$value` returns the .msg/.eml stream itself.
            resp = requests.get(f"{base_url}/{att['id']}/$value", headers=headers, timeout=60)
            resp.raise_for_status()
            data = resp.content
            if not name.lower().endswith((".msg", ".eml")):
                name = f"{name}.msg"
        elif odata_type == "#microsoft.graph.referenceAttachment":
            return Attachment(
                filename=name,
                content_type="application/x-reference",
                content_bytes=b"",
                content_id=content_id,
                is_inline=is_inline,
                drop_hint=f"reference:cloud link — {att.get('sourceUrl') or 'no URL supplied'}",
            )
        else:
            _logger.warning("unrecognised attachment type %s on %s", odata_type, name)
            return Attachment(
                filename=name, content_type=att.get("contentType") or "application/octet-stream",
                content_bytes=b"", drop_hint=f"reference:unhandled Graph type {odata_type}",
            )

        result = sniff.sniff(data, name, att.get("contentType") or "")
        attachment = Attachment(
            filename=name,
            content_type=att.get("contentType") or "application/octet-stream",
            content_bytes=data,
            content_id=content_id,
            is_inline=is_inline,
            sha256=result.sha256,
            size_bytes=len(data),
            sniffed_kind=result.kind,
        )
        verdict = sniff.classify_image(data, name, result, cids, content_id)
        if verdict.decorative:
            attachment.drop_hint = f"decorative:{verdict.certainty} — {verdict.reason}"
            attachment.content_bytes = b""
        return attachment

    def _resolve_folder_id(self, folder_name: str, headers: dict) -> str:
        """Graph's /move endpoint only accepts a well-known folder name (inbox, archive, ...) or
        a real folder id — never an arbitrary display name. Custom folders (Hidden, Routed,
        Processed, Errors) must be looked up by displayName, and created the first time if they
        don't exist yet. Cached per-instance so a batch of moves only looks each one up once."""
        if folder_name.lower() in GRAPH_WELL_KNOWN_FOLDERS:
            return folder_name
        if folder_name in self._folder_id_cache:
            return self._folder_id_cache[folder_name]

        url = f"{GRAPH_BASE_URL}/users/{self.mailbox_address}/mailFolders"
        resp = requests.get(url, headers=headers, params={"$filter": f"displayName eq '{folder_name}'"}, timeout=30)
        resp.raise_for_status()
        matches = resp.json().get("value", [])
        if matches:
            folder_id = matches[0]["id"]
        else:
            create_resp = requests.post(url, headers=headers, json={"displayName": folder_name}, timeout=30)
            create_resp.raise_for_status()
            folder_id = create_resp.json()["id"]

        self._folder_id_cache[folder_name] = folder_id
        return folder_id

    def mark_processed(self, email_id: str, folder: str) -> None:
        """`email_id` here is the provider id, not `RawEmail.email_id`.

        Those diverged when the dedupe key moved to `internetMessageId` (finding C5): Graph's
        /move endpoint only understands its own folder-scoped id, while the dedupe key has to be
        the one that survives the move. The orchestrator passes `provider_message_id`.
        """
        headers = self._headers()
        destination_id = self._resolve_folder_id(folder, headers)
        url = f"{GRAPH_BASE_URL}/users/{self.mailbox_address}/messages/{email_id}/move"
        resp = requests.post(url, headers=headers, json={"destinationId": destination_id}, timeout=30)
        resp.raise_for_status()
