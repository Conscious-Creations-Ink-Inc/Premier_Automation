import base64
import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Set

import msal
import requests

from config import settings
from pipeline import attachment_ledger, attachment_store
from pipeline.mail_arrivals import ID_MATCH_LEN
from pipeline.models import Attachment, RawEmail

_logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
MAX_PAGES_PER_POLL = 20   # 20 x $top=50 = 1000 messages; a guard, not a real ceiling
GRAPH_WELL_KNOWN_FOLDERS = {"inbox", "drafts", "sentitems", "deleteditems", "archive", "junkemail", "outbox"}


def _capped_retry_class():
    """`urllib3.Retry` with `Retry-After` bounded by `settings.GRAPH_RETRY_AFTER_CAP_SECONDS`.

    Built lazily, like the session below, so importing this module costs no urllib3 import. The
    stock class obeys whatever the server says: one throttle response reading `Retry-After: 3600`
    held a run inside a single call for an hour, where no stop and no ceiling could reach it and
    the console showed a spinner with nothing to say.
    """
    from urllib3.util.retry import Retry

    class CappedRetry(Retry):
        def get_retry_after(self, response):
            wait = super().get_retry_after(response)
            if wait is None:
                return None
            return min(wait, float(settings.GRAPH_RETRY_AFTER_CAP_SECONDS))

    return CappedRetry


class StopRequested(Exception):
    """Raised inside a Graph read when the run it belongs to has been asked to stop.

    Carries nothing and is caught by `GraphMailbox.fetch_new`, which ends the listing and returns
    what it has already built. Nothing is marked seen during a read, so the mail it did not reach is
    simply read next time.
    """


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

    session = requests.Session()
    retry = _capped_retry_class()(
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


class _TimeboxedHttp:
    """A `requests` session for MSAL that always carries a timeout.

    MSAL builds its own session when it is not handed one, and calls it with no `timeout` — so a
    token request against an unresponsive `login.microsoftonline.com` blocks the calling thread
    indefinitely. Every Graph data call below has had `timeout=` since the connector was written;
    this is the one call that did not, and it is the one that ran the automation into the ground:
    two runs held the runner's lock for twelve and thirty-six hours respectively, blocked here,
    while the console reported them as still running.

    MSAL only needs `get` and `post`, so this stays a two-method shim over the pooled, retrying
    session rather than a session subclass. `timeout` is `setdefault`, not forced, so MSAL may
    still ask for something shorter.
    """

    def __init__(self, timeout: int):
        self._timeout = timeout

    def get(self, *args, **kwargs):
        kwargs.setdefault("timeout", self._timeout)
        return _SESSION.get(*args, **kwargs)

    def post(self, *args, **kwargs):
        kwargs.setdefault("timeout", self._timeout)
        return _SESSION.post(*args, **kwargs)


_SESSION = _graph_session()


@dataclass
class RecoveredMail:
    """What a by-id recovery found, and what it proved was not there.

    Two lists rather than one, because "we did not get it" has two meanings that must never be
    conflated. `absent` is only for a message the server *answered about* and did not have; a
    request that failed — auth, throttling, a dropped connection — leaves the id out of **both**
    lists, so the caller retries it next run.

    This started as a bare `List[RawEmail]`, and the caller inferred absence from whatever was
    missing out of what it asked for. That is wrong in the one direction that matters: a transient
    failure would have been recorded as "gone from the mailbox" and the message suppressed for
    good. On a mailbox where the point of the exercise is that a purchase-order email must not be
    lost, silence has to mean "ask again", never "it is gone".
    """

    emails: List[RawEmail] = field(default_factory=list)
    absent: List[str] = field(default_factory=list)

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

    def fetch_by_ids(self, email_ids: Sequence[str]) -> "RecoveredMail":
        """Specific messages by `internetMessageId`, ignoring any time window.

        The escape hatch from `fetch_new`'s watermark. That listing asks the server only for mail
        newer than the last clean run, which is a cost optimisation that quietly became a
        correctness hole: a message the pipeline never settled but whose `receivedDateTime` now
        sits behind the window can never appear in a listing again. On 2026-09-03 that was 24
        messages reaching back to 2026-08-31, one of them an urgent purchase-order email, all of
        them recorded in `mail_arrivals` and none in `email_log`.

        A window cannot fix that, whatever it is widened to — the miss is by id, so the recovery
        has to be by id.

        Concrete, not abstract, and returning nothing by default: a connector reading a fixed
        local folder has no such window and therefore no such gap, and must not be forced to
        implement a method it cannot need. Nothing found and nothing proved absent.
        """
        return RecoveredMail()

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
        folders: Optional[Sequence[str]] = None,
    ):
        self.tenant_id = tenant_id or settings.GRAPH_TENANT_ID
        self.client_id = client_id or settings.GRAPH_CLIENT_ID
        self.client_secret = client_secret or settings.GRAPH_CLIENT_SECRET
        self.mailbox_address = mailbox_address or settings.GRAPH_MAILBOX_ADDRESS
        self.folders = tuple(folders or settings.MAILBOX_SOURCE_FOLDERS)
        """Which folders `fetch_new` lists, as Graph well-known names. Injectable so a test can
        pin one folder without reaching into settings, and so a mailbox with a different shape can
        be read without a code change."""
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
            http_client=_TimeboxedHttp(settings.GRAPH_AUTH_TIMEOUT_SECONDS),
        )
        self._folder_id_cache: dict = {}  # display name -> resolved folder id, so repeated moves don't re-lookup

        # Set by the run that owns this mailbox, never by the connector itself. Both default to
        # None so the arrival watch, the tools and the tests behave exactly as before.
        #
        # `should_stop` is asked before every Graph call made while reading. Reading used to be one
        # uninterruptible stretch — every page listed and every body and attachment downloaded
        # into a list before the run's own stop check was ever reached — so a run sat on "Reading
        # the mailbox" for as long as that took, with Stop, the kill switch and the ceiling all
        # unable to reach it. Asked here, a stop lands within one call.
        #
        # `on_activity` is told after every call that returned, which is what the console's stall
        # watchdog measures. A read that is slow keeps it moving; a read that is hung does not.
        self.should_stop = None
        self.on_activity = None

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

    def _checkpoint(self, note: str = "") -> None:
        """Report activity, then honour a stop. Called around every Graph call made while reading.

        Activity first: a call that just came back *is* progress, even if the next thing that
        happens is stopping, and the watchdog should not read the gap before a stop as a stall.
        """
        if self.on_activity is not None:
            try:
                self.on_activity(note)
            except Exception:                                    # noqa: BLE001
                _logger.debug("activity callback failed; continuing", exc_info=True)
        if self.should_stop is not None and self.should_stop():
            raise StopRequested()

    def fetch_new(self, skip_ids: Optional[Set[str]] = None,
                  since: Optional[str] = None) -> List[RawEmail]:
        """Every message in the source folders we have not already settled, oldest first per folder.

        **Folders, plural.** This read `/mailFolders/Inbox/messages` until 2026-08-24, when
        Exchange junked an Authority Inbound Notification for PO 912614 and the pipeline was
        structurally incapable of noticing: no row in `mail_arrivals`, none in `email_log`, none
        anywhere. `settings.MAILBOX_SOURCE_FOLDERS` names what to read instead.

        Merging folders is safe without any extra bookkeeping because `skip_ids` matches on
        `internetMessageId`, which is stable across folders — unlike Graph's own `id`, which is
        folder-scoped. A message somehow listed from two folders costs one set lookup.

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
        emails: List[RawEmail] = []
        try:
            self._list_folders(skip, since, headers, emails)
        except StopRequested:
            # Not an error and not a partial failure: the run was asked to stop. What was already
            # built is returned so the caller's own stop check ends the run cleanly, and nothing
            # was marked seen, so the mail this never reached is read next time.
            _logger.info("read stopped on request after %s message(s)", len(emails))
            return emails
        return emails

    def _list_folders(self, skip, since, headers, emails: List[RawEmail]) -> None:
        """The body of `fetch_new`, split out so a stop can unwind every nested loop in one raise."""
        skipped = 0
        for folder in self.folders:
            url = f"{GRAPH_BASE_URL}/users/{self.mailbox_address}/mailFolders/{folder}/messages"
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

            # Per folder, not shared across them. `MAX_PAGES_PER_POLL` is "a guard, not a real
            # ceiling", and one budget spent by a busy Inbox would starve every folder after it —
            # which for Junk means never reading it at all on exactly the mailboxes where the
            # guard matters.
            pages = 0
            while url and pages < MAX_PAGES_PER_POLL:
                self._checkpoint(f"listing {folder}")
                resp = _SESSION.get(url, headers=headers, params=params if pages == 0 else None, timeout=30)
                resp.raise_for_status()
                payload = resp.json()
                for msg in payload.get("value", []):
                    # Before the try: a message we have already settled costs one set lookup, not a
                    # body and a round of attachment downloads.
                    if msg.get("internetMessageId") in skip:
                        skipped += 1
                        continue
                    self._checkpoint((msg.get("subject") or "")[:60])
                    try:
                        emails.append(self._to_raw_email(msg, headers, source_folder=folder))
                    except StopRequested:
                        # Ahead of the catch-all below, which exists so one malformed message
                        # cannot abort a poll. A stop is not a malformed message, and swallowed
                        # there it would be logged as a skip and the read would carry straight on.
                        raise
                    except Exception as e:
                        _logger.warning("skipping message %s: %s: %s",
                                        msg.get("id"), type(e).__name__, e, exc_info=True)
                url = payload.get("@odata.nextLink")
                pages += 1

            if url:
                _logger.warning("poll truncated after %s pages; more mail remains in '%s'",
                                pages, folder)

        if skipped:
            _logger.info("skipped %s already-processed message(s) without fetching them", skipped)

    def fetch_by_ids(self, email_ids: Sequence[str]) -> List[RawEmail]:
        """Fetch these messages by `internetMessageId`, whatever the watermark says.

        One `$filter` request per id rather than a wider listing. That looks wasteful and is the
        cheaper option: the ids come from `mail_arrivals`, so the count is the size of the actual
        gap — usually nothing, occasionally a couple of dozen — whereas re-listing far enough back
        to cover them means paging the whole Inbox on every run for ever.

        `internetMessageId` is the id used throughout the pipeline because it is stable across
        folders, unlike Graph's own message id. It is not folder-scoped, so this searches the
        mailbox rather than the configured source folders: a message that has since been moved is
        still the message we failed to read, and refusing to find it would leave exactly the
        permanent gap this method exists to close.

        Failures are per message and logged, never raised. A run must not die because one id of
        twenty-four has been deleted from the mailbox since the watch saw it.

        **A truncated id is matched with `startswith`, not `eq`.** The ids here come from
        `mail_arrivals`, which the metadata-only watch fills — and Graph cuts `internetMessageId` at
        255 characters unless `$select` asks for the body (see `mail_arrivals.ID_MATCH_LEN`). An
        `eq` on a cut id matches nothing, so this method reported 26 messages sitting in the Inbox
        as absent, and `mark_missing` recorded them as deleted. Verified against the live mailbox:
        `eq` on such an id returns 0 results and `startswith` returns exactly 1.
        """
        if not email_ids:
            return RecoveredMail()

        headers = self._headers()
        found = RecoveredMail()

        for email_id in email_ids:
            try:
                self._checkpoint("recovering missed mail")
            except StopRequested:
                _logger.info("recovery stopped on request after %s message(s)", len(found.emails))
                break
            # Graph string literals escape a single quote by doubling it. An unescaped apostrophe
            # in a Message-ID would otherwise make a malformed filter and a 400 for that message.
            quoted = str(email_id).replace("'", "''")
            # A complete Message-ID is `<…@…>`; one cut at exactly the truncation length has lost
            # its closing bracket. Both conditions are required — a genuine id that happens to be
            # 255 characters long is still complete, and `eq` is the right, indexed query for it.
            truncated = len(str(email_id)) == ID_MATCH_LEN and not str(email_id).endswith(">")
            where = (f"startswith(internetMessageId, '{quoted}')" if truncated
                     else f"internetMessageId eq '{quoted}'")
            try:
                resp = _SESSION.get(
                    f"{GRAPH_BASE_URL}/users/{self.mailbox_address}/messages",
                    headers=headers,
                    params={
                        "$filter": where,
                        "$select": ("id,internetMessageId,receivedDateTime,subject,from,body,"
                                    "hasAttachments"),
                        # Two, not one, so an ambiguous prefix can be *detected*. With `$top=1` a
                        # prefix matching several messages would silently return whichever the
                        # server listed first.
                        "$top": 2,
                    },
                    timeout=30,
                )
                resp.raise_for_status()
                items = resp.json().get("value") or []
                if not items:
                    # A 2xx with an empty result set: the server looked and does not have it.
                    # **Only this counts as absent.** The id goes on `absent` so the caller can
                    # stop asking for ever.
                    found.absent.append(str(email_id))
                    continue
                if len(items) > 1:
                    # Neither list. Not `emails`, because we cannot tell which of them was meant;
                    # and emphatically not `absent`, because the message is plainly there — calling
                    # a present message gone is the exact fault this whole change exists to undo.
                    # Left on the work list, so it is asked about again rather than condemned.
                    _logger.warning(
                        "ambiguous truncated id, %s messages share this prefix, will retry: %s",
                        len(items), email_id)
                    continue
                found.emails.append(
                    self._to_raw_email(items[0], headers, source_folder="recovered"))
            except StopRequested:
                # Asked mid-message, while its attachments were downloading. The message is not
                # half-added — `append` never ran — so ending here loses nothing.
                _logger.info("recovery stopped on request after %s message(s)", len(found.emails))
                break
            except Exception as e:                                 # noqa: BLE001
                # Deliberately NOT recorded as absent. Auth failures, throttling and dropped
                # connections all land here, and treating them as "gone from the mailbox" would
                # permanently suppress a message that is sitting in the Inbox. Left out of both
                # lists, so the next run asks again.
                _logger.warning("could not recover message %s, will retry: %s: %s",
                                email_id, type(e).__name__, e)

        if found.absent:
            _logger.info("%s of %s message(s) to recover are no longer in the mailbox",
                         len(found.absent), len(email_ids))
        if found.emails:
            _logger.info("recovered %s message(s) the listing window could not reach",
                         len(found.emails))
        return found

    def _to_raw_email(self, msg: dict, headers: dict, source_folder: str = "") -> RawEmail:
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
            source_folder=source_folder,
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
        seen_digests: dict = {}
        pages = 0

        while url and pages < MAX_PAGES_PER_POLL:
            self._checkpoint("attachments")
            resp = _SESSION.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
            for att in payload.get("value", []):
                try:
                    parsed = self._to_attachment(att, base, headers, cids, seen_digests)
                    if parsed is not None:
                        attachments.append(parsed)
                except StopRequested:
                    raise                       # not a bad attachment — see `_list_folders`
                except Exception as e:
                    _logger.warning("skipping attachment %s on %s: %s: %s",
                                    att.get("name"), message_id, type(e).__name__, e, exc_info=True)
            url = payload.get("@odata.nextLink")
            pages += 1
        return attachments

    def _to_attachment(
        self, att: dict, base_url: str, headers: dict, cids: set, seen_digests: Optional[dict] = None
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
            self._checkpoint(name[:60])
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
                # Compared by name before it is dropped. The drop is right either way; the
                # question is whether anyone hears about it. See
                # `attachment_ledger.duplicate_drop_hint`.
                attachment.drop_hint = attachment_ledger.duplicate_drop_hint(
                    name, seen_digests[result.sha256], result.sha256)
            if attachment.drop_hint is None:
                seen_digests[result.sha256] = name

        if attachment.drop_hint is not None:
            # Stored *before* the bytes go, and deliberately so for most drops. `mail_view` says in
            # as many words that "a pasted photograph is often the proof of delivery itself", so a
            # misclassified attachment whose bytes were released is evidence destroyed silently and
            # for ever. Keeping it costs one content-addressed file, which a duplicate shares.
            #
            # `keeps_bytes` is the exception, and the only one: a signature logo. That used to be
            # decided in `attachment_ledger._insert`, one call *later* than this line — so
            # `PREMIER_STORE_DECORATIVE=0` saved nothing and the store grew by every logo Premier
            # was ever sent. Setting it to 1 restores the old unconditional keep, here and there
            # together.
            if attachment_ledger.keeps_bytes(attachment.drop_hint):
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
