"""Read-only Spitfire (sfPMS) REST connector.

Premier's ERP is Spitfire sfPMS 2023.0.9692.36214, reachable at
`https://spitfire-host.test/instance`. The v23 OpenAPI document describes 288 paths /
405 operations across 16 tags; `_ALLOWED` below lists the ones this connector may call, all reads.

**This module cannot write to Spitfire.** Every request passes through `_request`, which checks
the (method, path) pair against `_ALLOWED` and raises before a socket is opened if it is not on
the list. The write surface is real and well understood — see the plan's "Where we would post" —
but Premier has not authorised a write, so none exists here. Adding one means editing `_ALLOWED`,
which is exactly the visible, reviewable act it should be.

Two things the 8 August 2026 browser probe established, and one it got wrong:

* The field mapping below is that probe's §2, re-expressed as code. PO 912456 is the fixture.
* `DocItem.ItemQuantity` reads 0.0 on lines that genuinely order 2 units, and
  `DocItem.Specification` is null while the spec code sits in `SourceItemNumber`. Both are
  handled in `_to_po_line`.
* It concluded we could not authenticate without a browser. `POST /api/Account` takes a
  `SiteLogin` body and mints the same FormsAuthenticationTicket cookie the browser gets, so a
  `requests.Session` *is* the session. That is what `authenticate()` does.
"""

import html
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import requests

from config import settings
from connectors import spitfire_auth, spitfire_cassette
from pipeline.models import POLine

_logger = logging.getLogger(__name__)

# Spitfire's document-address vocabulary, from `GET /api/document/{id}/dialog/address`.
ADDR_FROM, ADDR_REMIT_TO, ADDR_SHIP_TO, ADDR_TO = "F", "R", "S", "T"

# A tax line is a real DocItem: it has a number, an amount, and it sorts in with the others.
# On PO 912456 line 0002 is Tax — ContractUnits 0.0, null UOM, null ItemStatus — and nothing
# about its shape distinguishes it from an under-populated goods line. The account category is
# the only reliable discriminator.
#
# Freight was added 10 Aug after pulling corpus PO 908491, which carries "Air Freight - FF&E"
# (0023) and "Freight - FF&E" (0024) as FRT-FP0 alongside seven TAX-FP0 lines: **10 of its 25
# lines are non-receivable, 40%**. Tax alone would have offered ten phantom candidates to the
# matcher, several with the same project words as the goods they relate to.
NON_RECEIVABLE_ACCOUNT_PREFIXES = ("TAX-", "FRT-")
TAX_ACCOUNT_CATEGORY_PREFIX = "TAX-"   # retained: referenced by tests/test_spitfire_readonly.py

_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")

# "Item Number: STE-400-LT" — a label plus the spec code, which `spec_code` already carries.
_ITEM_NUMBER_LABEL = re.compile(r"\bItem\s*Number\s*:\s*\S*", re.I)
# The boundary between the item name and the manufacturing spec sheet. Non-capturing, so
# `re.split` yields clean segments rather than interleaving the group.
_SPEC_SHEET_SPLIT = re.compile(r"\b(?:Item\s*)?Descriptions?\s*:\s*", re.I)


class SpitfireReadOnlyViolation(RuntimeError):
    """Raised when something asks this connector to issue a request that is not a known read.

    Deliberately not a subclass of anything the pipeline catches broadly: a write attempt is a
    programming error to be fixed, never a condition to be handled and continued past.
    """


# --- The read-only allowlist -------------------------------------------------
# sfPMS uses POST for reads — the document search (`POST /api/catalog/search/.../contents`), the
# project doc list, the ETag diffs and the suggestion lookups all take a body and change nothing.
# So "GET only" would be both wrong and useless: it would block PO discovery, the one thing this
# phase exists to do. The rule is an explicit allowlist instead, and `{}` matches one path segment.

_ALLOWED: Tuple[Tuple[str, str], ...] = (
    ("POST", "/api/Account"),                       # login: creates a session, not data
    ("GET", "/api/Account"),
    ("GET", "/api/account/session"),
    ("GET", "/api/session/who"),                    # whose session this is; audit, reads no data
    ("GET", "/api/Account/PasswordOptions"),
    ("GET", "/api/system/version"),
    ("GET", "/api/system/branding"),
    ("GET", "/api/projects"),
    ("POST", "/api/projects"),                      # QueryFilters search
    ("GET", "/api/project/{}/docs"),
    ("POST", "/api/project/{}/docs"),               # QueryFilters search
    ("POST", "/api/project/{}/docs/changes"),       # ETag diff
    ("POST", "/api/catalog/search/{}/contents"),    # site-wide document search
    ("GET", "/api/document/{}"),
    ("GET", "/api/document/{}/items"),
    ("GET", "/api/document/{}/addresses"),
    ("GET", "/api/document/{}/route"),
    ("GET", "/api/document/{}/dates"),
    ("GET", "/api/document/{}/attachments"),
    ("POST", "/api/document/{}/changes/items"),     # ETag diff — a read despite the verb
    ("GET", "/api/document/{}/dialog/link"),        # which child docs a PO can spawn: write-phase research
    ("GET", "/api/choices/{}/{}"),
    ("GET", "/api/suggestions/{}/{}"),
    ("GET", "/api/xts/map/{}/sfpms/{}"),

    # --- added for the warehouse sweep (pipeline/spitfire_warehouse.py) ---------
    # All measured 200 on build 2023.0.9728.39704. They are here rather than being called around
    # the allowlist because the credential is NOT read-only: `POST /api/account/allows` answers 31
    # (Read+Insert+Update+Delete+Blanket). This list is the only thing between a sweep and a write.
    ("POST", "/api/viewable/DocMasterAlt"),         # PO number -> DocMasterKey, no project needed
    ("GET", "/api/document/{}/comments"),
    ("GET", "/api/document/{}/dialog/{}"),          # 13 named dialogs, all read-only menus
    ("GET", "/api/catalog/{}/meta"),
    ("GET", "/api/catalog/{}/versions"),            # DataHash: the server's own MD5 of the bytes
    ("GET", "/api/catalog/{}/AccessHistory"),
    ("GET", "/api/catalog/{}/file/url"),
    ("GET", "/api/uicfg/live/{}"),                  # the field dictionary the Swagger omits
    ("GET", "/api/session/reports/{}"),             # names the SSRS reports; cannot render one
    ("GET", "/api/project/{}/TypeSummary"),
    ("GET", "/api/project/{}/cost/committed"),
    ("GET", "/api/project/{}/cost/transactions"),
    ("GET", "/api/history/{}/{}/{}"),               # per-field audit trail
    ("GET", "/api/contact/{}"),
)

_ALLOWED_SEGMENTS = frozenset(
    (method.upper(), tuple(path.strip("/").split("/"))) for method, path in _ALLOWED
)


def is_allowed(method: str, path: str) -> bool:
    """True if (method, path) is a known-read operation.

    Compared segment-wise so `{}` matches exactly one segment and cannot swallow a suffix —
    `/api/document/{}` must not authorise `/api/document/{id}/items` by accident, since the two
    have genuinely different permission implications on a document Premier considers sensitive.
    Path comparison is case-insensitive because the Swagger itself is inconsistent: the same
    controller serves `/api/document/{id}/items` and `/api/Document/{id}/dialog/link`.
    """
    got = tuple(path.split("?")[0].strip("/").split("/"))
    for allowed_method, template in _ALLOWED_SEGMENTS:
        if allowed_method != method.upper() or len(template) != len(got):
            continue
        if all(t == "{}" or t.lower() == g.lower() for t, g in zip(template, got)):
            return True
    return False


@dataclass
class RequestRecord:
    """One issued request. Collected so a run can prove what it touched.

    Premier is being asked to grant an ERP login on the strength of "it only reads". A list of
    every method+path the run actually issued, checkable against `_ALLOWED`, is the evidence for
    that claim; `SpitfireReadClient.audit_log` is dumped into the run report.
    """
    method: str
    path: str
    status: Optional[int]
    elapsed_ms: int


@dataclass
class PODocument:
    """A purchase order as Spitfire holds it: header, lines, addresses, route."""
    doc_master_key: str
    po_number: str
    project_code: str
    project_name: str
    doc_status: str
    doc_status_label: str
    source_date: Optional[str]
    """The header's `SourceDate`. **Not the order date** — measured and rejected: 11 of the 17
    mirrored POs carrying line due dates have a line due *before* it, and all three POs the corpus
    delivers against were received before it. Goods due before the order exists is incoherent.
    Kept because it is cheap and the comparison should stay reproducible; use `order_date`."""

    vendor_name: str
    vendor_email: Optional[str]
    ship_to: Optional[str]
    assigned_agent: Optional[str]
    pay_terms_prose: Optional[str]
    lines: List[POLine] = field(default_factory=list)
    tax_lines_skipped: int = 0

    order_date: Optional[str] = None
    """When the PO was raised — the header's `DocDate`. See `order_date_of` for the two tests that
    picked it over `SourceDate` and `Due`. None where Spitfire holds its null date."""


def order_date_of(header) -> Optional[str]:
    """When the purchase order was raised: the header's **`DocDate`**.

    Established 2026-08-11 by reading all 28 mirrored POs off the live training host and testing
    the three candidate header dates against two independent checks:

    | field        | line due date falls before it | PO numbers out of date order |
    |--------------|-------------------------------|------------------------------|
    | `DocDate`    | 1 of 17                       | **0 of 27**                  |
    | `Due`        | 1 of 17                       | — (mostly `0001-01-01`)      |
    | `SourceDate` | 11 of 17                      | 11 of 27                     |

    The second column is the decisive one. Spitfire issues PO numbers in sequence, so sorting by
    PO number must sort by the date the order was raised — `DocDate` does that perfectly across 27
    consecutive pairs, and `SourceDate` inverts on 11. Whatever `SourceDate` is, it is not this.

    Not from `/api/document/{id}/dates`, despite the name. That endpoint returns *schedule* rows
    keyed by a `DocDateTypeKey` GUID with no type name, and on PO 908491 it held a single
    2023-06-09 → 2024-12-01 span unrelated to the order. It is not called.

    The one remaining outlier is PO 912379, whose earliest line is due 2025-07-17 against a
    `DocDate` of 2025-12-15. That is a backdated line, not a bad `DocDate`, and it surfaces on the
    timeline as a conflict rather than being smoothed away.
    """
    if not isinstance(header, dict):
        return None
    value = str(header.get("DocDate") or "")[:10]
    # Spitfire's null date. Treated as absent, or the timeline would claim the order was raised
    # in the year 1.
    return value if value and not value.startswith("0001-") else None


def strip_html(value: Optional[str]) -> str:
    """`DocItem.Description` arrives as HTML — `<div>FIT-902-TV - TV Wall Mount&nbsp;</div>`.

    It has to be stripped before it reaches the description matcher, or RapidFuzz scores the
    markup: `DESC_MATCH_THRESHOLD` is 80, and two descriptions that share nothing but a `<div>`
    wrapper and a few `&nbsp;` can clear it.
    """
    if not value:
        return ""
    return _WHITESPACE.sub(" ", html.unescape(_HTML_TAG.sub(" ", value))).strip()


def clean_description(value: Optional[str]) -> str:
    """Strip HTML, then the label boilerplate Spitfire embeds in `Description`.

    On PO 912456 the description is plain prose, which is why this was not needed at first. Corpus
    PO 908491 shows the other shape — every one of its 25 lines begins:

        Item Number: STE-400-LT
        Item Description: <the actual text>

    Left in, `Item Number` / `Item Description` are literal text common to every line on the PO, so
    RapidFuzz scores lines against each other on boilerplate they all share. At
    `DESC_MATCH_THRESHOLD = 80` that is enough to promote the wrong line. The spec code is dropped
    with the label because it is already carried, exactly, in `spec_code` from `SourceItemNumber`.
    """
    text = strip_html(value)
    if not text:
        return ""
    text = _ITEM_NUMBER_LABEL.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()

    # Real Premier POs put the item *name* first and a full manufacturing specification after a
    # "Description:" label — 1,000-2,000 characters of dimensions, UL listings, cord colours and
    # finish notes. Corpus PO 908491 line 1 is 1,500 characters of which the first 30 are the only
    # part a delivery email could ever echo:
    #
    #   Table Lamp at Hospitality Unit | Description: Budget Code: 53-600-077 Custom lamp at ...
    #
    # RapidFuzz token_sort_ratio compares token *sets*, so a 12-word email phrase scored against a
    # 250-word spec sheet is diluted far below DESC_MATCH_THRESHOLD (80) no matter how good the
    # match — the description signal would silently never fire. Keeping the head restores it.
    # Split on every "Description:" boundary and take the first segment with content. A line can
    # carry two labels — "Item Description: <name> Description: <spec>" — which leaves the leading
    # segment empty; taking the first non-empty one lands on the name in both shapes.
    segments = [seg.strip(" -:|") for seg in _SPEC_SHEET_SPLIT.split(text)]
    return next((seg for seg in segments if seg), "")


PO_FOUND = "found"
PO_ABSENT = "absent"
"""Spitfire answered and holds no such purchase order. Nothing a person can do about it."""
PO_UNREACHABLE = "unreachable"
"""No strategy completed, so we know nothing either way. Must be retried, never recorded as absent."""


class SpitfireReadClient:
    """Authenticated, read-only sfPMS REST client.

    Holds a `requests.Session`; `POST /api/Account` puts the sfPMSAuth / sfSession / sfSettings
    cookies on it and every later call rides that. Sessions lapse, so `_ensure_session`
    re-authenticates on demand rather than assuming one login lasts a run.

    Which credential is used is decided by `connectors/spitfire_auth.py`: the account in
    `SPITFIRE_UID`/`SPITFIRE_PW` when set, else a borrowed `SPITFIRE_SESSION_COOKIE`. The ticket
    is shared process-wide, so a new client reuses a live login instead of opening another.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        uid: Optional[str] = None,
        pw: Optional[str] = None,
        tz_offset: Optional[float] = None,
        timeout: int = 60,
        session_cookie: Optional[str] = None,
    ):
        self.base_url = (base_url or settings.SPITFIRE_BASE_URL or "").rstrip("/")
        account = spitfire_auth.credentials()
        # Passed-in credentials are pinned; configured ones are re-read on every login so a changed
        # password in `.env` takes effect without touching code.
        self._pinned_credentials = bool(uid or pw)
        self.uid = uid or (account.uid if account else None)
        self.pw = pw or (account.pw if account else None)
        self.tz_offset = settings.SPITFIRE_TZ_OFFSET if tz_offset is None else tz_offset
        self.timeout = timeout
        self._session = requests.Session()
        # Records responses while on Premier's office IP and replays them when off it. A no-op
        # unless SPITFIRE_CASSETTE_MODE is set, and it sits *under* this client: everything below
        # — the allowlist, the retry rule, the audit log — runs unchanged either way.
        spitfire_cassette.mount(self._session)
        self._authenticated = False
        self.audit_log: List[RequestRecord] = []
        if not self.base_url:
            raise ValueError("SpitfireReadClient needs SPITFIRE_BASE_URL (check .env)")

        self._host = urlparse(self.base_url).hostname

        # Precedence: a cookie handed to this constructor, then the account, then a cookie from
        # config. The account beats a configured cookie so a stale ticket left in `.env` can never
        # shadow it — and it is the only mode that can renew itself.
        self.login_mode = not session_cookie and bool(self.uid and self.pw)
        self.session_cookie = None if self.login_mode else (session_cookie
                                                            or spitfire_auth.session_cookie())
        self.cookie_mode = bool(self.session_cookie)
        if self.cookie_mode:
            # `sfPMSAuth` alone is the FormsAuthentication ticket and is sufficient — the other
            # cookies the browser holds are session id, settings and a session GUID, none of which
            # authenticate. Set on the jar rather than as a header so redirects and any later
            # Set-Cookie from the server merge normally.
            self._session.cookies.set("sfPMSAuth", self.session_cookie, domain=self._host, path="/")
            self._authenticated = True
            _logger.info("using a supplied sfPMSAuth session cookie for %s", self.base_url)
        elif self.login_mode:
            ticket = spitfire_auth.cached_ticket(self.base_url, self.uid)
            if ticket:
                self._load_ticket(ticket)

    # --- transport ----------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """The single chokepoint. Nothing in this module talks to Spitfire except through here.

        Retry policy is deliberately narrow. An unknown or invented document GUID returns **500**
        with a body byte-identical to a genuine server fault — sfPMS does not 404 for a missing
        document — so 500 cannot be treated as "not found", and retrying it forever would turn a
        bad key into a hang. One retry absorbs a transient fault; the second failure raises.
        """
        if not is_allowed(method, path):
            raise SpitfireReadOnlyViolation(
                f"{method.upper()} {path} is not on the read-only allowlist. "
                "This connector is read-only by design; see connectors/spitfire.py::_ALLOWED."
            )
        url = f"{self.base_url}{path}"
        last_error: Optional[Exception] = None
        for attempt in range(2):
            started = time.monotonic()
            try:
                response = self._session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as e:
                last_error = e
                self.audit_log.append(RequestRecord(method.upper(), path, None,
                                                    int((time.monotonic() - started) * 1000)))
                _logger.warning("spitfire %s %s failed (%s); attempt %s of 2",
                                method, path, type(e).__name__, attempt + 1)
                continue
            self.audit_log.append(RequestRecord(method.upper(), path, response.status_code,
                                                int((time.monotonic() - started) * 1000)))
            if response.status_code >= 500 and attempt == 0:
                _logger.warning("spitfire %s %s returned %s; retrying once",
                                method, path, response.status_code)
                continue
            return response
        raise RuntimeError(f"spitfire {method} {path} failed twice") from last_error

    def _get_json(self, path: str, **kwargs) -> Any:
        response = self._request("GET", path, **kwargs)
        response.raise_for_status()
        return response.json()

    def _post_json(self, path: str, payload: Any, **kwargs) -> Any:
        response = self._request("POST", path, json=payload, **kwargs)
        response.raise_for_status()
        return response.json()

    def read(self, path: str, payload: Any = None) -> requests.Response:
        """One allowlisted read, returned unraised so the caller can see what happened.

        `_get_json` is right for the pipeline, which wants a PO or an exception. A sweep wants the
        opposite: a 404 on one of a document's thirteen dialogs is data about that document, not a
        reason to abandon it, and the status and raw bytes both have to be recorded. Public
        because `tools/spitfire_warehouse_sync.py` must go through the allowlist, not around it —
        reaching for `_get_json` would have been the easy way to end up bypassing `_request`.
        """
        self._ensure_session()
        if payload is None:
            return self._request("GET", path)
        return self._request("POST", path, json=payload)

    def read_json(self, path: str, payload: Any = None, default: Any = None) -> Any:
        """`read`, decoded, with `default` for any non-2xx or unparseable body."""
        try:
            response = self.read(path, payload)
        except (requests.RequestException, RuntimeError):
            return default
        if not response.ok:
            return default
        try:
            return response.json()
        except ValueError:
            return default

    # --- session ------------------------------------------------------------

    def server_version(self) -> str:
        """Anonymous. Answers "can this machine reach Spitfire at all" without credentials."""
        return self._get_json("/api/system/version")

    def has_session(self) -> bool:
        """`GET /api/account/session` returns a bare boolean and is safe unauthenticated —
        it answers `false` rather than 401ing, which makes it a free liveness probe and avoids
        having to distinguish "session lapsed" from "endpoint refused" by parsing error bodies."""
        try:
            return bool(self._get_json("/api/account/session"))
        except (requests.HTTPError, ValueError):
            return False

    def whoami(self) -> dict:
        """Who the current session belongs to. Matters in cookie mode, where the identity is
        borrowed and every read is attributed to somebody else."""
        try:
            payload = self._get_json("/api/session/who")
            return payload if isinstance(payload, dict) else {}
        except (requests.HTTPError, ValueError):
            return {}

    def authenticate(self) -> None:
        """`POST /api/Account` with a `SiteLogin` body; cookies land on the session.

        `IsHashed` stays false — it is for the rare caller that already holds the hash, and
        sending a plaintext password against `IsHashed: true` fails in a way that reads like bad
        credentials. `tzOffset` is the client's offset from UT and is what Spitfire stamps
        server-side dates against, so a wrong value shifts received dates by hours.
        """
        if not self._pinned_credentials:
            # `.env` as it is now, not as it was at startup: this is what makes a password change
            # a one-file edit. A lapsed ticket re-logs in with whatever the file says today.
            account = spitfire_auth.credentials()
            if account:
                self.uid, self.pw = account.uid, account.pw
        missing = [n for n, v in (("SPITFIRE_UID", self.uid), ("SPITFIRE_PW", self.pw)) if not v]
        if missing:
            raise ValueError(f"SpitfireReadClient is missing required config: {', '.join(missing)} (check .env)")
        self._session.cookies.clear()      # never send a lapsed ticket alongside the login
        response = self._request("POST", "/api/Account", json={
            "UID": self.uid, "PW": self.pw, "IsHashed": False, "tzOffset": self.tz_offset,
        })
        if response.status_code != 200:
            raise RuntimeError(
                f"Spitfire login failed for {self.uid}: HTTP {response.status_code} "
                f"{response.text[:200]} (check SPITFIRE_UID / SPITFIRE_PW in .env)"
            )
        if not self._ticket_value():
            # A 200 without a ticket authenticates nothing, and every later call would 401 with a
            # message about permissions. Say what actually happened.
            raise RuntimeError(f"Spitfire login for {self.uid} answered 200 but issued no "
                               f"sfPMSAuth ticket")
        self._authenticated = True
        if self.login_mode:
            spitfire_auth.store_ticket(self.base_url, self.uid, self.ticket_cookies())
        _logger.info("authenticated to %s as %s", self.base_url, self.uid)

    def ticket_cookies(self) -> Dict[str, str]:
        """The cookies Spitfire issued this session, by name. Handed to the write client."""
        return {c.name: c.value for c in self._session.cookies if c.value is not None}

    def _ticket_value(self) -> Optional[str]:
        return next((c.value for c in self._session.cookies if c.name == "sfPMSAuth"), None)

    def _load_ticket(self, cookies: Dict[str, str]) -> None:
        self._session.cookies.clear()
        for name, value in cookies.items():
            self._session.cookies.set(name, value, domain=self._host, path="/")
        self._authenticated = True

    def ensure_session(self) -> None:
        """Public form of `_ensure_session`: a live session on return, or an exception."""
        self._ensure_session()

    def _ensure_session(self) -> None:
        if self.login_mode and spitfire_cassette.mode() == spitfire_cassette.REPLAY:
            # Offline: there is no server to log in to, and the recorded responses answer anyway.
            return
        if self._authenticated and self.has_session():
            return
        if self.login_mode:
            with spitfire_auth.lock():
                # Another client may have renewed the shared ticket while this one waited.
                shared = spitfire_auth.cached_ticket(self.base_url, self.uid)
                if shared and shared.get("sfPMSAuth") != self._ticket_value():
                    self._load_ticket(shared)
                    if self.has_session():
                        return
                self.authenticate()
            return
        if self.cookie_mode:
            # Deliberately not falling through to authenticate(): in cookie mode there is no
            # password to re-login with, and letting it try produces a confusing "missing
            # SPITFIRE_UID" rather than the truth, which is that the borrowed ticket has lapsed.
            raise RuntimeError(
                "the supplied sfPMSAuth cookie has expired or was rejected. Capture a fresh one "
                "from the browser (F12 -> Application -> Cookies -> sfPMSAuth) and set "
                "SPITFIRE_SESSION_COOKIE, or set SPITFIRE_UID/SPITFIRE_PW to log in directly."
            )
        self.authenticate()

    # --- PO discovery -------------------------------------------------------

    def resolve_po(self, po_number: str, po_doc_type_key: Optional[str] = None) -> Optional[str]:
        """PO number -> `DocMasterKey`, the gap nothing on disk crosses.

        Emails give us `912456`. Every read endpoint worth calling is keyed by GUID, and the
        August probe was handed that GUID by a human reading it out of a browser. Two strategies,
        in order of how little they assume:

        1. The site-wide document search, filtered by `DocNoLike`. No project needed.
        2. Enumerate projects and ask each for its doc list. Slow and chatty, but it depends on
           nothing being configured a particular way, so it is the floor.

        Returns None rather than raising when the PO genuinely is not there — an unmatched PO is
        an ordinary outcome that belongs in the report, not an exception.

        **Order reversed 10 Aug, against evidence.** The site-wide catalog search was the primary
        strategy on the assumption that it needed no project. It does not work for documents: it
        answers 200 with zero rows even for a title known to exist, because that endpoint searches
        the *file* catalog, not the document store. Project-scoped search is the one that returns
        the PO, so it goes first and the catalog search is kept only as a long shot.
        """
        return self.resolve_po_with_outcome(po_number, po_doc_type_key)[0]

    def resolve_po_with_outcome(self, po_number: str,
                                po_doc_type_key: Optional[str] = None) -> "tuple":
        """`(DocMasterKey | None, outcome)` — the same search, saying *why* it came back empty.

        `resolve_po` cannot tell "Spitfire answered, and has no such purchase order" apart from
        "not one of the three strategies got an answer at all": every one of them swallows its
        HTTP failure and moves on, and the caller sees `None` either way. Downstream that becomes
        "purchase order X was not found", which is a statement about *their* data made on the
        strength of *our* connection.

        The distinction matters because a record whose PO genuinely is not in Spitfire is nobody's
        work — nothing can be done about it — while one we simply could not look up must be tried
        again. Marking a whole backlog "not in Spitfire" during an outage would quietly close every
        one of them.

        `PO_ABSENT` is only returned when at least one strategy completed and reported nothing.
        """
        self._ensure_session()
        doc_type = po_doc_type_key or settings.SPITFIRE_PO_DOC_TYPE_KEY
        answered = False
        filters: Dict[str, Any] = {
            "DocNoLike": po_number,
            "IncludeDocs": True,
            "IncludeFiles": False,
            "IncludeClosed": True,
            "ResultLimit": 25,
        }
        if doc_type:
            filters["ForDocType"] = doc_type

        for project_id in self.list_project_ids():
            try:
                docs = self._post_json(f"/api/project/{project_id}/docs", filters)
            except (requests.HTTPError, ValueError):
                continue
            answered = True
            key = self._first_matching_key(docs, po_number)
            if key:
                return key, PO_FOUND

        key, alt_answered = self._resolve_po_alt_with_outcome(po_number, doc_type)
        answered = answered or alt_answered
        if key:
            return key, PO_FOUND

        try:
            results = self._post_json(
                f"/api/catalog/search/{settings.SPITFIRE_SEARCH_SCOPE}/contents", filters)
        except (requests.HTTPError, ValueError) as e:
            _logger.warning("catalog search for PO %s failed (%s)", po_number, type(e).__name__)
            return None, (PO_ABSENT if answered else PO_UNREACHABLE)
        key = self._first_matching_key(results, po_number)
        return (key, PO_FOUND) if key else (None, PO_ABSENT)

    def resolve_po_alt(self, po_number: str, po_doc_type_key: Optional[str] = None) -> Optional[str]:
        """PO number -> `DocMasterKey` in one call, with no project id at all.

        `POST /api/viewable/DocMasterAlt` returns a bare quoted GUID, or `""` for no match — a
        clean negative, unlike the 500-is-not-404 trap everywhere else on this API. It reaches
        purchase orders outside `SPITFIRE_PROJECT_IDS`, which the project-scoped search above
        structurally cannot: that search is the reason POs like 912456 read as unresolvable.

        Kept as a fallback rather than promoted to first strategy because the project search is
        what every recorded cassette and every test replays; changing the primary path is a
        separate, reviewable change.
        """
        return self._resolve_po_alt_with_outcome(po_number, po_doc_type_key)[0]

    def _resolve_po_alt_with_outcome(self, po_number: str,
                                     po_doc_type_key: Optional[str] = None) -> "tuple":
        """`(key | None, answered)` — `answered` is False when the call itself did not complete.

        `DocMasterAlt` returns a bare quoted GUID or `""` for no match, so it is the one endpoint
        here that gives a clean negative. That is only worth something if a transport failure is
        told apart from it, which is what the second element carries.
        """
        doc_type = po_doc_type_key or settings.SPITFIRE_PO_DOC_TYPE_KEY
        try:
            found = self._post_json("/api/viewable/DocMasterAlt", {
                "RequestID": "1",
                "DVName": "DocMasterAlt",
                "MatchingValue": str(po_number).strip(),
                "DependsOn": [doc_type, "empty", "empty"],
            })
        except (requests.HTTPError, ValueError, RuntimeError) as e:
            _logger.warning("DocMasterAlt for PO %s failed (%s)", po_number, type(e).__name__)
            return None, False
        key = str(found or "").strip()
        return (key or None), True

    @staticmethod
    def _first_matching_key(payload: Any, po_number: str) -> Optional[str]:
        """`DocNoLike` is a *like* filter, so `2124` would return 912456 alongside 912457.

        Every candidate is therefore re-checked for an exact hit on `DocNo` or `SubContract`
        before its key is accepted. On PO 912456 both fields carry the number; which one is
        authoritative depends on the doc type, so both are compared.
        """
        rows = payload if isinstance(payload, list) else (payload or {}).get("Rows") or []
        wanted = str(po_number).strip().upper()
        for row in rows:
            if not isinstance(row, dict):
                continue
            for field_name in ("DocNo", "SubContract", "SourceDocNo"):
                if str(row.get(field_name) or "").strip().upper() == wanted:
                    key = row.get("DocMasterKey") or row.get("DocKey") or row.get("Key")
                    if key:
                        return str(key)
        return None

    def list_project_ids(self) -> List[str]:
        """Project IDs to search, live if the account can see any, configured otherwise.

        `GET /api/projects` is **405** on this server despite being in the Swagger, and the POST
        form answers 200 with **zero rows** — the account has no project list of its own. Until
        Premier grants it project membership, the IDs have to come from configuration; ours were
        read out of `xsfDocHeader` and are where the June corpus POs live.

        The live call is still attempted first so this corrects itself the moment access is
        granted, rather than silently continuing to use a stale hard-coded list.
        """
        self._ensure_session()
        try:
            payload = self._post_json("/api/projects", {"IncludeHidden": True, "IncludeClosed": True})
            rows = payload if isinstance(payload, list) else (payload or {}).get("Rows") or []
            live = [str(r.get("ProjectID") or r.get("Project")) for r in rows
                    if isinstance(r, dict) and (r.get("ProjectID") or r.get("Project"))]
            if live:
                return live
        except (requests.HTTPError, ValueError) as e:
            _logger.warning("project list unavailable (%s); using SPITFIRE_PROJECT_IDS",
                            type(e).__name__)
        return list(settings.SPITFIRE_PROJECT_IDS)

    # --- PO read ------------------------------------------------------------

    def read_po(self, doc_master_key: str) -> PODocument:
        """The four reads, assembled by `build_po_document`."""
        self._ensure_session()
        header = self._get_json(f"/api/document/{doc_master_key}")
        items = self._get_json(f"/api/document/{doc_master_key}/items") or []
        addresses = self._get_json(f"/api/document/{doc_master_key}/addresses") or []
        route = self._get_json(f"/api/document/{doc_master_key}/route") or []
        # `/dates` is deliberately NOT called: it returns schedule rows keyed by an unnamed
        # `DocDateTypeKey` GUID and carries no order date — see `order_date_of`. One request per
        # PO for nothing.
        return build_po_document(doc_master_key, header, items, addresses, route)

    def read_po_lines(self, po_number: str) -> List[POLine]:
        """Convenience for the matcher: PO number straight to lines, or [] if unresolvable."""
        key = self.resolve_po(po_number)
        return self.read_po(key).lines if key else []

    # --- write-phase research (still reads) ---------------------------------

    def linkable_children(self, doc_master_key: str) -> Any:
        """What child documents this PO can spawn.

        Settles, without writing anything, whether the receiver should be created via
        `POST /api/document/0/{receiptTypeKey}?forParent=...` or via `POST /api/document/{id}/next`
        (the "Create Next rules" route, which inherits Premier's configured defaults). Recorded in
        the plan; acted on only once Premier authorises a write.
        """
        self._ensure_session()
        return self._get_json(f"/api/document/{doc_master_key}/dialog/link")

    def code_choices(self, set_name: str, for_doc_type: str = "1") -> Any:
        """Code lists — UOM, statuses, account categories.

        The August probe recorded these as unreachable, having tried `/api/config/*` and
        `/api/lookup`. Neither path exists in v23. `GET /api/choices/{setName}/{forDocType}` is the
        real endpoint and answers 200. The `/api/configuration/*` admin endpoints in the Swagger
        do exist but return 500 "Not Implemented" on this build — do not reach for them.
        """
        self._ensure_session()
        return self._get_json(f"/api/choices/{set_name}/{for_doc_type}")

    def xts_key_for(self, set_name: str, spitfire_key: str) -> Any:
        """Read side of Spitfire's own external-system key map.

        XTS is sfPMS's peer-sync bus. Its key map is the natural idempotency store for "has this
        shipment already been posted" — registering the pair is a `PUT`, and therefore write-phase
        work, but reading it is free and tells us what a previous run did.
        """
        self._ensure_session()
        return self._get_json(f"/api/xts/map/{set_name}/sfpms/{spitfire_key}")


# --- item mapping -------------------------------------------------------------


def _address_of_type(addresses: Sequence[Any], addr_type: str) -> Optional[dict]:
    return next((a for a in addresses
                 if isinstance(a, dict) and str(a.get("AddrType") or "").upper() == addr_type), None)


def is_tax_line(item: dict) -> bool:
    """Tax and freight lines are ordinary DocItems and must not be received against.

    Grouping them in inflates the line count and, worse, offers the matcher a line whose
    description often contains the same project words as the goods it taxes.

    `AccountCategory` appears in two places — on each `DocItemTask` and again on
    `RelatedLineDetails` — and both are checked. A line whose task collection is empty (the
    schema only promises "often 1-1") would otherwise slip the check and be offered as
    receivable.

    Name kept for its callers; it now screens freight as well — see
    `NON_RECEIVABLE_ACCOUNT_PREFIXES`.
    """
    categories = [t.get("AccountCategory") for t in (item.get("DocItemTask") or [])
                  if isinstance(t, dict)]
    categories.append(_related_details(item).get("AccountCategory"))
    return any(str(c or "").upper().startswith(NON_RECEIVABLE_ACCOUNT_PREFIXES)
               for c in categories)


def _first_task(item: dict) -> dict:
    """`DocItemTask` is *"often 1-1"* — often, not always. Index 0 is the primary extension."""
    tasks = item.get("DocItemTask") or []
    return tasks[0] if tasks and isinstance(tasks[0], dict) else {}


def _related_details(item: dict) -> dict:
    """`RelatedLineDetails` -> `RelatedItemDetail`: *"often zero, max 1"*.

    The Swagger declares it `oneOf` a single ref rather than as a plain `$ref`, and real payloads
    have been seen carrying it as a bare object. Both shapes, and absence, are handled here so no
    caller has to.
    """
    related = item.get("RelatedLineDetails") or {}
    if isinstance(related, list):
        related = related[0] if related and isinstance(related[0], dict) else {}
    return related if isinstance(related, dict) else {}


def _to_float(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _to_po_line(item: dict, doc: PODocument) -> POLine:
    """One `DocItem` -> one `POLine`, with the three field traps handled.

    * **Quantity.** `DocItem.ItemQuantity` reads 0.0 on PO 912456 line 0001, which orders 2 units.
      `RelatedLineDetails.ContractUnits` is the ordered quantity; `DocItemTask[0].Quantity` is the
      fallback. `ItemQuantity` is never trusted on its own.
    * **Spec code.** `DocItem.Specification` is null. The spec lives in `SourceItemNumber` — which
      Premier's own `czx_TPICreate_ReceiptDoc` independently confirms by joining
      `TPI.ItemNumber = di.SourceItemNumber`.
    * **Description.** HTML, so it is stripped here rather than at every call site.
    """
    task = _first_task(item)
    related = _related_details(item)

    qty_ordered = _to_float(related.get("ContractUnits"))
    if qty_ordered is None:
        qty_ordered = _to_float(task.get("Quantity"))
    if qty_ordered is None:
        qty_ordered = _to_float(item.get("ItemQuantity")) or 0.0

    # `DocItemNumber` is a display string and is NOT safe to treat as an integer:
    #   * PO 908491 carries a line whose number is the literal text "Remitted Tax";
    #   * receipt documents number their lines "0001-001", i.e. {DocNo}-{seq};
    #   * the lines come back unordered (0011, 0023, 0020, 0018 ...), so position means nothing.
    # Leading digits are taken where they exist and 0 stands for "unnumbered" — never raising,
    # because one oddly-numbered line must not cost us the other twenty-four.
    #
    # **This is not a match key.** The Authority Inbound cites "908491 : 300" while the same spec
    # (STE-402-LT) is DocItemNumber 0003 in Spitfire — 300 and 0003 are different numbering
    # schemes and their relationship is not established. `models.py:150` calls po_line_number
    # "the single most valuable field on the record"; until that mapping is proven, match on
    # `spec_code` (SourceItemNumber), which does line up exactly.
    # Anything not cleanly numeric becomes 0, meaning "unnumbered". Deliberately not a partial
    # parse: reading "0001-A" as 1 would fabricate an exact line number the document never
    # asserted, and an invented match key is worse than an absent one.
    raw_number = str(item.get("DocItemNumber") or "").strip()
    try:
        line_number = int(raw_number)
    except ValueError:
        line_number = 0

    return POLine(
        po_number=doc.po_number,
        line_number=line_number,
        line_key=str(item.get("DocItemKey") or ""),
        spec_code=str(item.get("SourceItemNumber") or "").strip(),
        description=clean_description(item.get("Description")),
        vendor_name=doc.vendor_name,
        # UOM is carried on both the task and the related-line details; the task is authoritative,
        # the other is the fallback for a line with no task extension.
        unit_of_measure=str(task.get("UOM") or related.get("UOM") or "").strip(),
        qty_ordered=qty_ordered,
        qty_received=_to_float(related.get("ReceivedUnits")) or 0.0,
        # An unapproved receipt sits here, not in ReceivedUnits. Ignoring it makes a delivery
        # whose receipt is still in someone's approval queue look entirely un-received.
        qty_in_transit=_to_float(related.get("ReceiptInProgressUnits")) or 0.0,
        cost_code=str(task.get("ProjEntity") or "").strip(),
        project_code=doc.project_code,
        project_name=doc.project_name,
        # `ItemStatus` is a site-defined code ('N' on the probed PO) and its code list is not
        # published, so it is carried through verbatim rather than guessed at. Anything that needs
        # open/closed should compare qty_ordered against qty_received, which is unambiguous.
        line_status=str(item.get("ItemStatus") or "").strip(),
        expected_date=item.get("Due") or item.get("Requested"),
        ship_to=doc.ship_to,
        assigned_agent=doc.assigned_agent,
        pay_terms=doc.pay_terms_prose,
    )


def build_po_document(doc_master_key: str, header: Any, items: Sequence[Any],
                      addresses: Sequence[Any], route: Sequence[Any]) -> PODocument:
    """The four payloads of a purchase order, assembled into one `PODocument`. No I/O.

    Split out of `SpitfireReadClient.read_po` so the warehouse can rebuild the same object from
    mirrored rows — `pipeline.spitfire_mirror.refresh_from_warehouse` — without a second parser.
    Every field trap lives on this path exactly once (`ItemQuantity` reading 0.0, the spec code in
    `SourceItemNumber`, HTML descriptions, `TAX-`/`FRT-` lines), so the offline projection and the
    live read cannot drift apart. `read_po` passes API responses; the mirror passes rows rebuilt
    into the same shape, which is why `sf_document` keeps Spitfire's own PascalCase field names.
    """
    header = header if isinstance(header, dict) else {}
    items = items or []
    addresses = addresses or []
    route = route or []

    vendor = _address_of_type(addresses, ADDR_TO)
    ship_to = _address_of_type(addresses, ADDR_SHIP_TO)
    po_number = str(header.get("DocNo") or header.get("SubContract") or "").strip()
    project_code = str(header.get("Project") or "").strip()
    project_name = str(header.get("Project_dv") or "").strip()

    # `ResponsibleParty_dv` reads empty on real POs, so the purchasing agent comes from the
    # route, with the F (From/Author) address as the fallback the probe recommended.
    agent = next((str(r.get("UserName")) for r in route
                  if isinstance(r, dict) and r.get("UserName")), None)
    if not agent:
        from_addr = _address_of_type(addresses, ADDR_FROM)
        agent = (from_addr or {}).get("Contact") or (from_addr or {}).get("Company")

    # Payment terms exist only as prose. There is no structured pay-terms field anywhere on
    # the document, which is why Stage 5's CBD/ADR rule cannot yet be driven from Spitfire.
    pay_terms = header.get("Notes") or header.get("NoteEML")

    doc = PODocument(
        doc_master_key=str(doc_master_key),
        po_number=po_number,
        project_code=project_code,
        project_name=project_name,
        doc_status=str(header.get("Status") or ""),
        doc_status_label=str(header.get("Status_dv") or ""),
        source_date=header.get("SourceDate"),
        order_date=order_date_of(header),
        vendor_name=str((vendor or {}).get("Company") or ""),
        vendor_email=(vendor or {}).get("Email"),
        ship_to=(ship_to or {}).get("Company") or (ship_to or {}).get("Address1"),
        pay_terms_prose=strip_html(pay_terms) or None,
        assigned_agent=agent,
    )

    for item in items:
        if not isinstance(item, dict):
            continue
        if is_tax_line(item):
            doc.tax_lines_skipped += 1
            continue
        doc.lines.append(_to_po_line(item, doc))
    return doc
