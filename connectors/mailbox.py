import base64
import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional

import msal
import requests

from config import settings
from pipeline.models import Attachment, RawEmail

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"


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
        headers = self._headers()
        url = f"{GRAPH_BASE_URL}/users/{self.mailbox_address}/mailFolders/Inbox/messages"
        params = {"$top": 50, "$select": "id,receivedDateTime,subject,from,body,hasAttachments"}
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        return [self._to_raw_email(msg, headers) for msg in resp.json().get("value", [])]

    def _to_raw_email(self, msg: dict, headers: dict) -> RawEmail:
        sender_address = msg.get("from", {}).get("emailAddress", {}).get("address", "") or ""
        body = msg.get("body", {})
        body_content = body.get("content")
        is_html = body.get("contentType") == "html"
        attachments = self._fetch_attachments(msg["id"], headers) if msg.get("hasAttachments") else []
        return RawEmail(
            email_id=msg["id"],
            received_at=msg["receivedDateTime"],
            sender_address=sender_address,
            sender_domain=sender_address.split("@")[-1] if "@" in sender_address else "",
            subject=msg.get("subject", ""),
            body_html=body_content if is_html else None,
            body_text=body_content if not is_html else None,
            attachments=attachments,
        )

    def _fetch_attachments(self, message_id: str, headers: dict) -> List[Attachment]:
        url = f"{GRAPH_BASE_URL}/users/{self.mailbox_address}/messages/{message_id}/attachments"
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        attachments = []
        for att in resp.json().get("value", []):
            if att.get("@odata.type") == "#microsoft.graph.fileAttachment":
                attachments.append(Attachment(
                    filename=att["name"],
                    content_type=att.get("contentType", "application/octet-stream"),
                    content_bytes=base64.b64decode(att["contentBytes"]),
                ))
        return attachments

    def mark_processed(self, email_id: str, folder: str) -> None:
        # `folder` is a Graph well-known folder name (e.g. "archive", "deleteditems") or a real
        # folder id. Resolving Premier's actual processed-mail folder structure is still pending
        # real mailbox access — see JOE_FOLLOWUPS_CHECKLIST.md item #4.
        url = f"{GRAPH_BASE_URL}/users/{self.mailbox_address}/messages/{email_id}/move"
        resp = requests.post(url, headers=self._headers(), json={"destinationId": folder}, timeout=30)
        resp.raise_for_status()
