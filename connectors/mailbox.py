import base64
import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional, Set

import msal
import requests

from config import settings
from pipeline import attachment_store
from pipeline.models import Attachment, RawEmail

_logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
MAX_PAGES_PER_POLL = 20   # 20 x $top=50 = 1000 messages; a guard, not a real ceiling
GRAPH_WELL_KNOWN_FOLDERS = {"inbox", "drafts", "sentitems", "deleteditems", "archive", "junkemail", "outbox"}


def _graph_session() -> requests.Session:
    """One pooled session that honours Graph's own throttling.

    There was no 429 handling at all here: every call was a bare `requests.get` followed by
    `raise_for_status`, so a single throttle response failed the entire run and the next attempt
    was a whole poll interval away. Graph throttles per-mailbox and states `Retry-After`; obeying
    it is the difference between a two-second pause and a lost cycle.

    Pooling matters too. Without a Session every request opened a fresh TLS connection — and a
    poll makes one call per page plus one per message with attachments.
    """
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
    session.mount("https://", adapter)
    return session


_SESSION = _graph_session()


class Mailbox(ABC):
    @abstractmethod
    def fetch_new(self, skip_ids: Optional[Set[str]] = None,
                  since: Optional[str] = None) -> List[RawEmail]:
        """Mail the caller has not already settled.

        `skip_ids` is advisory — a connector that honours it avoids fetching those messages at all,
        and one that ignores it is still correct because the caller filters again. `since` is an
        ISO-8601 UTC instant a connector may use to narrow its listing server-side. Both default to
        None, so a connector reading a fixed local folder need do nothing with either.
        """

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

    def fetch_new(self, skip_ids: Optional[Set[str]] = None,
                  since: Optional[str] = None) -> List[RawEmail]:
        skip = skip_ids or frozenset()
        emails = []
        for path in sorted(self.folder.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("email_id") in skip:
                continue
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

    Needs: an Azure AD app registration scoped to the receiving mailbox, Mail.ReadWrite application
    permission with admin consent, and an Application Access Policy restricting this app to only
    the one mailbox — confirmed applied, which is what cleared this connector to read live mail.

    Prefer `read_only=True` for anything that is not a deliberate production run: without it every
    processed message is moved out of Premier's Inbox and the four routing folders are created.
    """

    def __init__(
        self,
        tenant_id: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        mailbox_address: Optional[str] = None,
        read_only: bool = False,
    ):
        self.tenant_id = tenant_id or settings.GRAPH_TENANT_ID
        self.client_id = client_id or settings.GRAPH_CLIENT_ID
        self.client_secret = client_secret or settings.GRAPH_CLIENT_SECRET
        self.mailbox_address = mailbox_address or settings.GRAPH_MAILBOX_ADDRESS
        self.read_only = read_only
        """When set, `mark_processed` records the folder it *would* have moved to and touches
        nothing — no move, and no folder created either, since `_resolve_folder_id` creates the
        four routing folders on first use. This is what makes a shadow run against Premier's live
        mailbox observably harmless: they see an unchanged Inbox, we still get every verdict.
        Mirrors `MsgFileMailbox.read_only`, which exists for the same reason."""
        self.routed_to: dict = {}
        """provider message id -> folder, for every email this instance settled."""
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

    def fetch_new(self, skip_ids: Optional[Set[str]] = None,
                  since: Optional[str] = None) -> List[RawEmail]:
        """Every message in the Inbox we have not already settled, following pagination, oldest first.

        Five things this gets right, four of them fixes over the original single-page call:

        * **`since` filters server-side.** Without it the listing returns the whole Inbox every
          poll, and because nothing is ever moved out (`read_only=True`) that list only grows —
          while `MAX_PAGES_PER_POLL` truncates it at a thousand messages, oldest first. Past a
          thousand, new mail would never be reached at all. `skip_ids` cannot fix that: it makes
          each message cheap, but the message still has to be listed to be skipped.

        * **`skip_ids` is applied before any per-message work.** Callers used to fetch everything
          and filter afterwards, which meant each poll downloaded every message *and every
          attachment byte* only to throw almost all of it away — 18 messages and 15 MB of
          attachments per run against this mailbox, about forty seconds, most of it re-reading the
          same photographs. The `internetMessageId` is in the listing response, so the decision can
          be made before `_to_raw_email` calls out for bodies and attachments.
        * **`@odata.nextLink` is followed.** `$top=50` with the link ignored silently lost every
          message beyond the fiftieth in a poll (finding C11).
        * **Oldest first**, so a poll truncated by `MAX_PAGES_PER_POLL` still makes forward
          progress instead of re-reading the same newest page forever.
        * **Per-message try/except.** One malformed message used to abort the whole poll, because
          the original built the list in a comprehension (finding C8).

        Skipping here is a *read* optimisation and nothing more. `seen_message_ids` is still only
        written once an email has a verdict (`ingest_orchestrator._settle`), so a poll that dies
        halfway still leaves the rest of its mail unseen and picked up next time.
        """
        skip = skip_ids or frozenset()
        headers = self._headers()
        url = f"{GRAPH_BASE_URL}/users/{self.mailbox_address}/mailFolders/Inbox/messages"
        params = {
            "$top": 50,
            "$orderby": "receivedDateTime asc",
            "$select": "id,internetMessageId,receivedDateTime,subject,from,body,hasAttachments",
        }
        if since:
            # `ge`, not `gt`: the caller already backdates this by an overlap window, and the
            # seen-set is what actually prevents re-processing. Erring towards listing a message
            # twice costs one skipped row; erring the other way loses it for good.
            params["$filter"] = f"receivedDateTime ge {since}"

        emails: List[RawEmail] = []
        pages = 0
        skipped = 0
        while url and pages < MAX_PAGES_PER_POLL:
            resp = _SESSION.get(url, headers=headers, params=params if pages == 0 else None, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
            for msg in payload.get("value", []):
                # Before the try: a message we have already settled costs one set lookup, not a
                # body and a round of attachment downloads.
                if msg.get("internetMessageId") in skip:
                    skipped += 1
                    continue
                try:
                    emails.append(self._to_raw_email(msg, headers))
                except Exception as e:
                    _logger.warning("skipping message %s: %s: %s",
                                    msg.get("id"), type(e).__name__, e, exc_info=True)
            url = payload.get("@odata.nextLink")
            pages += 1

        if skipped:
            _logger.info("skipped %s already-processed message(s) without fetching them", skipped)
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
        seen_digests: set = set()
        pages = 0

        while url and pages < MAX_PAGES_PER_POLL:
            resp = _SESSION.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
            for att in payload.get("value", []):
                try:
                    parsed = self._to_attachment(att, base, headers, cids, seen_digests)
                    if parsed is not None:
                        attachments.append(parsed)
                except Exception as e:
                    _logger.warning("skipping attachment %s on %s: %s: %s",
                                    att.get("name"), message_id, type(e).__name__, e, exc_info=True)
            url = payload.get("@odata.nextLink")
            pages += 1
        return attachments

    def _to_attachment(
        self, att: dict, base_url: str, headers: dict, cids: set, seen_digests: Optional[set] = None
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
            resp = _SESSION.get(f"{base_url}/{att['id']}/$value", headers=headers, timeout=60)
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

        if not data:
            return Attachment(
                filename=name,
                content_type=att.get("contentType") or "application/octet-stream",
                content_bytes=b"", content_id=content_id, is_inline=is_inline,
                drop_hint="empty:zero bytes",
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

        # Content-hash dedupe, per message — the same guard `MsgFileMailbox` applies, wording
        # included, so one physical delivery reads the same in the ledger whichever connector
        # brought it in. Premier really does send byte-identical PODs under different filenames.
        if seen_digests is not None:
            if attachment.drop_hint is None and result.sha256 in seen_digests:
                attachment.drop_hint = f"duplicate:{result.sha256[:12]}"
            if attachment.drop_hint is None:
                seen_digests.add(result.sha256)

        if attachment.drop_hint is not None:
            # Stored *before* the bytes go, and deliberately even for a drop. Thirty of the
            # forty-six rows in Premier's ledger are `dropped_decorative`, and `mail_view` says in
            # as many words that "a pasted photograph is often the proof of delivery itself" — so a
            # misclassified logo was destroying evidence, silently and for ever. Keeping it costs
            # one content-addressed file, which a duplicate shares.
            attachment_store.put(attachment.content_bytes)
            attachment.content_bytes = b""   # metadata is enough for a dropped attachment
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
        resp = _SESSION.get(url, headers=headers, params={"$filter": f"displayName eq '{folder_name}'"}, timeout=30)
        resp.raise_for_status()
        matches = resp.json().get("value", [])
        if matches:
            folder_id = matches[0]["id"]
        else:
            create_resp = _SESSION.post(url, headers=headers, json={"displayName": folder_name}, timeout=30)
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
        # Recorded whether or not the move happens, so `routed_to` is this run's own record of
        # what it decided — the only per-run answer available, since the state tables accumulate
        # across runs and a live poll is incremental by nature.
        self.routed_to[email_id] = folder
        if self.read_only:
            return
        headers = self._headers()
        destination_id = self._resolve_folder_id(folder, headers)
        url = f"{GRAPH_BASE_URL}/users/{self.mailbox_address}/messages/{email_id}/move"
        resp = _SESSION.post(url, headers=headers, json={"destinationId": destination_id}, timeout=30)
        resp.raise_for_status()
