import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List

from pipeline.models import Attachment, RawEmail


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
    """Real Microsoft Graph API connector — not implemented until Premier grants mailbox access.

    Needs: an Azure AD app registration scoped to the receiving mailbox, Mail.Read + Mail.ReadWrite
    application permissions with admin consent, and an Application Access Policy restricting this
    app to only the one mailbox. See BuildPlan/STAGE_1_INGEST_AND_TRIAGE.md and
    ourDocs/JOE_FOLLOWUPS_CHECKLIST.md item #4.
    """

    def __init__(self, tenant_id: str, client_id: str, client_secret: str, mailbox_address: str):
        raise NotImplementedError(
            "GraphMailbox requires real Graph API credentials and an Application Access Policy "
            "scoping this app to the receiving mailbox — see JOE_FOLLOWUPS_CHECKLIST.md #4."
        )

    def fetch_new(self) -> List[RawEmail]:
        raise NotImplementedError

    def mark_processed(self, email_id: str, folder: str) -> None:
        raise NotImplementedError
