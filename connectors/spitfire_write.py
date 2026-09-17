"""The write half of the Spitfire connector. Deliberately a separate module.

`connectors/spitfire.py` states that it *cannot* write, and three things depend on that claim
staying literally true: its own `_ALLOWED` check, `tests/test_spitfire_readonly.py`, and the
promise made to Premier when they granted the login. Widening that allowlist to add a receipt
post would retire the guarantee for the read path as well, so the writes live here instead,
behind their own list and their own exception. The read connector is untouched and still cannot
write.

Every operation below was executed successfully against the training instance before it was
written down — the 8-call chain in `tools/spitfire_e2e_test.py` (PASS 2026-08-10 and -13), and
the upload/attach pair re-proven by hand on 2026-08-14 (fileKey `0a06c94b-…`, server `DataHash`
identical to the local MD5). Nothing here is derived from the OpenAPI document, which describes
none of the required shapes.

**Three refusals are structural, not policy toggles:**

* `route/apply` and `route/perform` dispatch the approval chain and *email real Premier staff*.
  Creating a receipt already auto-stages Cassie Breaux, Kendyl Shrogin and Tina Tran at sequence
  10 — reproducible across two runs — so the route exists and is populated; only the dispatch is
  withheld. Premier decides when that changes.
* `PATCH /api/document/{id}/Status` marks a receipt POD Confirmed with no approval at all,
  bypassing the FAA sign-off.
* `DELETE`, all 53 of them. None has ever been issued against this server.

The deny check runs *before* the allowlist and matches on the raw path, so a future mistake in
`_ALLOWED_WRITES` cannot authorise one of them by accident.

**A 200 from this API proves nothing.** Empty-body writes return 204 whether they did anything
or not, and an invented document GUID returns 500 rather than 404. Every method here that
changes something is paired with a read-back on the same client; callers are expected to use it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import requests

from config import settings
from connectors import spitfire_auth, spitfire_cassette

_logger = logging.getLogger(__name__)

EMPTY_GUID = "00000000-0000-0000-0000-000000000000"
"""The null parent slot in `POST /api/document/{parent}/{typeKey}`, and the `DocKey` of a
document link. Spitfire uses it as "no value" rather than accepting null."""


class SpitfireWriteViolation(RuntimeError):
    """Raised when something asks this client to issue a request that is not a sanctioned write.

    Like its read-side counterpart this is a programming error to be fixed, never a condition to
    catch and continue past — a caller that reaches it was about to do something to Premier's ERP
    that nobody authorised.
    """


# --- What may be written -----------------------------------------------------------------------
# `{}` matches exactly one path segment, so `/api/document/{}` cannot authorise
# `/api/document/{id}/items`. Query strings are stripped before matching; `?xm=catalog` and
# `?forProject=…` are part of the operation, not part of the permission.

_ALLOWED_WRITES: Tuple[Tuple[str, str], ...] = (
    ("POST",  "/api/catalog/upload"),          # multipart fileMeta + file -> {"key": fileKey}
    ("PATCH", "/api/document/{}/Title"),       # body is a bare JSON string, not an object
    ("POST",  "/api/document/{}/items"),       # array. PUT /items is 501, see set_line_quantity
    ("POST",  "/api/document/{}/attachments"), # array. file link OR doc link, see attach_*
    # The document-edit session — how sfPMS's own UI changes a field, and the only route that can
    # set a quantity on a receipt line. See `set_line_quantity`.
    #
    # `DELETE /session` is how a session is committed, and it is the *only* DELETE this module
    # permits — see the narrow exception in `is_allowed`. It destroys nothing: it releases an edit
    # lock and writes the staged changes. `POST /session/end` returns 200 and does **not** commit;
    # measured on training 2026-08-22, four lines patched and released that way all read back 0.0,
    # and the same four committed the moment the session was released with DELETE instead.
    ("PATCH",  "/api/document/{}/session/changes"),
    ("DELETE", "/api/document/{}/session"),
)

# Document creation is `POST /api/document/{parent}/{typeKey}` and is NOT in the table above,
# because as a two-wildcard template it is far wider than it looks: `/api/document/{}/{}` also
# matches `POST /api/document/{id}/next` — which creates a document with an all-zero DocTypeKey —
# and `POST /api/document/from/{id}`, which silently copies one. Both were created by accident
# during the 13 Aug write probe from empty-body calls expected to be rejected. So creation is
# matched by shape instead: the parent must be the null GUID and the type must be a real GUID.
_GUID_LENGTH = 36


def _fmt_quantity(value: float) -> str:
    """A quantity as `DocFieldChange.Data` wants it: a plain decimal string, no thousands
    separator and no trailing `.0` on a whole number, which is how sfPMS's own client sends it."""
    number = float(value)
    return str(int(number)) if number == int(number) else repr(number)


def _is_guid(value: str) -> bool:
    parts = value.split("-")
    return (len(value) == _GUID_LENGTH and len(parts) == 5
            and all(c in "0123456789abcdefABCDEF" for c in value.replace("-", "")))


def _is_session_release(path: str) -> bool:
    """`/api/document/{guid}/session` exactly — the commit-and-release call, and nothing else."""
    segments = tuple(path.strip("/").split("/"))
    return (len(segments) == 4 and segments[0].lower() == "api"
            and segments[1].lower() == "document" and _is_guid(segments[2])
            and segments[3].lower() == "session")


def _is_document_create(method: str, segments: Sequence[str]) -> bool:
    return (method == "POST" and len(segments) == 4
            and segments[0].lower() == "api" and segments[1].lower() == "document"
            and segments[2] == EMPTY_GUID and _is_guid(segments[3]))

_ALLOWED_READBACKS: Tuple[Tuple[str, str], ...] = (
    ("GET",  "/api/document/{}"),
    ("GET",  "/api/document/{}/items"),
    ("GET",  "/api/document/{}/attachments"),
    ("GET",  "/api/catalog/{}/versions"),      # DataHash — the server's own MD5
    ("GET",  "/api/session/who"),              # liveness; a lapsed cookie must be nameable
    ("POST", "/api/project/{}/docs"),          # read despite the verb: finds the PO and pay requests
    ("GET",  "/api/document/{}/session"),       # opens an edit session; returns a bare GUID string
    # The approval route, read so `sign_off_route_steps` can find *our own* stop on it. A read:
    # `route/apply` and `route/perform` — the two calls that email Premier staff — stay in
    # `_DENIED_SUBSTRINGS` and are not reachable from this module at all.
    ("GET",  "/api/document/{}/route"),
)

_ALLOWED = _ALLOWED_WRITES + _ALLOWED_READBACKS

RESPONDED = "A"
"""`DocRoute.Status` for a stop its routee has signed off.

`A` means two different things either side of this module: on `DocMasterDetail.Status` it is
"POD Confirmed", which bypasses the FAA approval gate and is refused by `_DENIED_SUBSTRINGS`.
On a `DocRoute` row it is "Responded", which is the thumbs-up on one person's own step.
"""

_ROUTE_SENTINEL = "0001-01-01"
"""Spitfire writes this for "never", and as a non-empty string it reads truthy.

Measured across 4,602 mirrored route rows: treating it as a real timestamp inverts the
`Reached` test, which is the gate for whether a stop may be responded to at all.
"""


def _route_reached(row: Any) -> bool:
    """Whether the route has actually arrived at this stop."""
    value = str((row or {}).get("Reached") or "").strip()
    return bool(value) and not value.startswith(_ROUTE_SENTINEL)

_ALLOWED_SEGMENTS = frozenset(
    (method.upper(), tuple(path.strip("/").split("/"))) for method, path in _ALLOWED
)

# Checked first, on the raw lowercased path, and independent of the allowlist above.
_DENIED_SUBSTRINGS: Tuple[str, ...] = (
    "route/apply",      # emails Premier staff
    "route/perform",    # ditto
    "/status",          # bypasses the FAA approval gate
    "/logout",          # destroys the hand-captured ticket the whole run depends on
)


def is_allowed(method: str, path: str) -> bool:
    """True if `(method, path)` is one of the sanctioned writes or its read-back.

    Case-insensitive on the path because sfPMS itself is inconsistent — the same controller
    serves `/api/document/{id}/items` and `/api/Document/{id}/dialog/link`.
    """
    bare = path.split("?")[0]
    lowered = bare.lower()
    if any(bad in lowered for bad in _DENIED_SUBSTRINGS):
        return False
    if method.upper() == "DELETE" and not _is_session_release(bare):
        # DELETE stays refused everywhere else. Releasing a document-edit session is the one
        # exception: it removes nothing, it is how a change is committed, and leaving sessions open
        # would strand edit locks on documents in Premier's system that nobody can see to clear.
        return False
    got = tuple(bare.strip("/").split("/"))
    if _is_document_create(method.upper(), got):
        return True
    for allowed_method, template in _ALLOWED_SEGMENTS:
        if allowed_method != method.upper() or len(template) != len(got):
            continue
        if all(t == "{}" or t.lower() == g.lower() for t, g in zip(template, got)):
            return True
    return False


def line_in(items: Sequence[Dict[str, Any]], po_line_key: str) -> Optional[Dict[str, Any]]:
    """The receipt line standing against `po_line_key`, out of items already read.

    The matching rule of `SpitfireWriteClient.find_prepopulated_line`, lifted out so it can be
    applied to one `read_items` result many times. Posting a twenty-line delivery through the method
    would read the whole document twenty times to answer twenty questions about one payload.

    Matched on `SCDocItemKey` — the purchase order line's own `DocItemKey` — because that is the
    identity Spitfire itself used to build the row. **Not on the spec code:** 82 of 179 lines share
    a spec with a sibling on the same order, so a spec match would pick an arbitrary one of them.
    """
    wanted = (po_line_key or "").strip().lower()
    if not wanted:
        return None
    for item in items:
        related = item.get("RelatedLineDetails") or {}
        if str(related.get("SCDocItemKey") or "").strip().lower() == wanted:
            return item
    return None


def task_key_of(item: Optional[Dict[str, Any]]) -> str:
    """The `ItemTaskKey` a quantity is written against, or "" if the row carries none.

    `DocItemTask[0]`, not `DocItemKey` — `set_line_quantity` patches `DocItemTask.Quantity` and
    `InstanceKey` is the task's key. Getting this wrong writes nothing and reports success.
    """
    tasks = (item or {}).get("DocItemTask") or [{}]
    first = tasks[0] if isinstance(tasks[0], dict) else {}
    return str(first.get("ItemTaskKey") or "")


@dataclass
class WriteRecord:
    """One issued request, kept so a post can prove exactly what it did.

    Spitfire records every write as `api@consciouscreations.ai` regardless of which person
    triggered it, so the ERP's own audit trail cannot distinguish one operator from another.
    This log, persisted alongside the post ledger, is the only record that can.
    """
    method: str
    path: str
    status: Optional[int]
    elapsed_ms: int
    note: str = ""


class SpitfireSessionExpired(RuntimeError):
    """The `sfPMSAuth` ticket was rejected.

    Its own type because the UI must say *"the Spitfire session has expired, recapture the
    cookie"* rather than a generic failure — auth here is a hand-copied browser ticket that
    lapses on idle, and a 401 sends people hunting for permission problems that do not exist.
    """


class SpitfireWriteClient:
    """Issues the receipt-posting chain, and nothing else.

    **Authentication.** With `SPITFIRE_UID`/`SPITFIRE_PW` configured this client rides the shared
    login ticket from `connectors/spitfire_auth.py`; otherwise a `SPITFIRE_SESSION_COOKIE`. It never
    logs in itself — `POST /api/Account` is not on `_ALLOWED_WRITES` — the read connector does.

    **A ticket is renewed only in `whoami()`**, which every posting chain calls before it creates
    anything. A 401 *during* a chain still raises `SpitfireSessionExpired` and stops: re-logging in
    and carrying on would resume a half-made receipt unattended, and the ledger already knows how to
    recover a `PARTIAL` safely.
    """

    def __init__(self, base_url: Optional[str] = None, session_cookie: Optional[str] = None,
                 timeout: int = 90):
        self.base_url = (base_url or settings.SPITFIRE_BASE_URL or "").rstrip("/")
        if not self.base_url:
            raise ValueError("SpitfireWriteClient needs SPITFIRE_BASE_URL (check .env)")
        self._host = urlparse(self.base_url).hostname
        # Same precedence as the read client: a cookie passed in, then the account, then a cookie
        # from config.
        account = None if session_cookie else spitfire_auth.credentials()
        self.login_mode = account is not None
        self.session_cookie = None if self.login_mode else (session_cookie
                                                            or spitfire_auth.session_cookie())
        if not self.login_mode and not self.session_cookie:
            raise SpitfireSessionExpired(
                "no Spitfire credentials are configured; set SPITFIRE_UID and SPITFIRE_PW in .env "
                "before posting")
        self.timeout = timeout
        self.audit_log: List[WriteRecord] = []
        self._session = requests.Session()
        # See connectors/spitfire_cassette.py. A no-op unless SPITFIRE_CASSETTE_MODE is set. In
        # replay it answers the read-backs from disk and refuses the four mutating calls outright —
        # a replayed create_receipt would return one DocMasterKey for every delivery.
        spitfire_cassette.mount(self._session)
        if self.login_mode:
            ticket = spitfire_auth.cached_ticket(self.base_url, account.uid)
            if ticket:
                self._load_ticket(ticket)
        else:
            # On the jar rather than as a header, so redirects and any later Set-Cookie merge.
            self._session.cookies.set("sfPMSAuth", self.session_cookie, domain=self._host, path="/")

    def _load_ticket(self, cookies: Dict[str, str]) -> None:
        self._session.cookies.clear()
        for name, value in cookies.items():
            self._session.cookies.set(name, value, domain=self._host, path="/")

    def _renew_ticket(self) -> None:
        """Take a live ticket from the shared login, logging in again if it lapsed. Pre-chain only."""
        if spitfire_cassette.mode() == spitfire_cassette.REPLAY:
            return
        try:
            self._load_ticket(spitfire_auth.login_ticket(self.base_url))
        except (RuntimeError, ValueError, requests.RequestException) as exc:
            raise SpitfireSessionExpired(f"could not log in to Spitfire: {exc}") from exc

    # --- transport ------------------------------------------------------------------------------

    def _request(self, method: str, path: str, note: str = "", **kwargs) -> requests.Response:
        """The single chokepoint. Nothing here reaches Spitfire except through this method.

        **Writes are never retried.** The read connector retries a 500 once, which is right for a
        GET and wrong for every operation in this module: this API has no idempotency anywhere —
        the catalog does not deduplicate identical bytes (proven 2026-08-14: the same 37,352-byte
        PDF uploaded twice produced two fileKeys), and re-sending an attach creates a second row.
        A retried timeout that actually succeeded server-side would post twice. So a failed write
        raises and the caller decides, with the ledger to tell it what already landed.
        """
        if not is_allowed(method, path):
            raise SpitfireWriteViolation(
                f"{method.upper()} {path} is not a sanctioned write. This client posts receipts "
                "and their attachments only; see connectors/spitfire_write.py::_ALLOWED_WRITES."
            )
        url = f"{self.base_url}{path}"
        started = time.monotonic()
        try:
            response = self._session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            self.audit_log.append(WriteRecord(method.upper(), path, None,
                                              int((time.monotonic() - started) * 1000), note))
            raise RuntimeError(f"spitfire {method} {path} failed: {type(exc).__name__}") from exc
        self.audit_log.append(WriteRecord(method.upper(), path, response.status_code,
                                          int((time.monotonic() - started) * 1000), note))
        if response.status_code == 401:
            if self.login_mode:
                raise SpitfireSessionExpired(
                    "the Spitfire session expired part-way through the post. Nothing was retried; "
                    "the ledger records what had already landed. The next post logs in afresh.")
            raise SpitfireSessionExpired(
                "the sfPMSAuth cookie has expired or was rejected. Capture a fresh one from the "
                "browser (F12 -> Application -> Cookies -> sfPMSAuth) and restart the post.")
        return response

    @staticmethod
    def _explain(response: requests.Response) -> str:
        """Turn a failure body into the most useful sentence available.

        The two error families are not interchangeable and mean different things to whoever reads
        the dialog: `{"ThisStatus","ThisReason"}` is Spitfire's own code rejecting the request,
        and the reason is usually the exact missing requirement. `{"Message"}` means the request
        never reached Spitfire — the ASP.NET layer stopped it, so the fault is in our shape.
        """
        try:
            body = response.json()
        except ValueError:
            return f"HTTP {response.status_code}: {response.text[:200]}"
        if isinstance(body, dict):
            if body.get("ThisReason"):
                return f"Spitfire refused it: {body['ThisReason']}"
            if body.get("Message"):
                return (f"the request never reached Spitfire ({body['Message']}) — "
                        f"HTTP {response.status_code}")
        return f"HTTP {response.status_code}: {str(body)[:200]}"

    def _json_or_raise(self, response: requests.Response, what: str) -> Any:
        if response.status_code >= 400:
            raise RuntimeError(f"{what} failed — {self._explain(response)}")
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(f"{what} returned a non-JSON body: "
                               f"{response.text[:200]}") from exc

    # --- session --------------------------------------------------------------------------------

    def whoami(self) -> str:
        """Whose ticket this is. Raises `SpitfireSessionExpired` if it has lapsed.

        Called before the chain starts so an expired cookie is reported *before* a receipt is
        created, not halfway through leaving a titled but empty document behind.

        In login mode this is also where a lapsed ticket is renewed — the one point in a chain
        where logging in again cannot resume anything half-made.
        """
        if self.login_mode:
            self._renew_ticket()
        payload = self._json_or_raise(self._request("GET", "/api/session/who", note="liveness"),
                                      "reading the session")
        if isinstance(payload, dict):
            # `EMail`, with that capital M, is what this endpoint actually returns — measured
            # 2026-08-17 against training: {"EMail": "api@…", "FullName": "…", "UserKey": "…"}.
            # The spellings below it were guesses and every one of them missed, so this returned
            # "" for a perfectly live session. Nothing decided on that — the lapsed-ticket check is
            # `_json_or_raise` on the response, not this value — but it is the identity the audit
            # trail attributes a receipt to, and an empty one names nobody.
            return str(payload.get("EMail") or payload.get("Email") or payload.get("UserName")
                       or payload.get("FullName") or payload.get("UID") or "")
        return ""

    # --- 1. the file ----------------------------------------------------------------------------

    def upload_file(self, content: bytes, filename: str, *, keywords: str = "",
                    when: Optional[str] = None) -> str:
        """Put bytes in the catalog and return the `fileKey`.

        `POST /api/catalog/upload?xm=catalog`, multipart, two parts. The failure ladder, learned
        by walking into each one:

        * no `file` part, or a hand-set `Content-Type` header -> **415 "File expected"**. The
          boundary is generated by the HTTP client; setting the header by hand loses it and the
          server sees no parts at all. `requests` builds it from `files=`, so it is never set here.
        * no `fileMeta` part -> **400 "Meta data missing"**.
        * `filemeta` in lower case -> the same 400. Part names are case-sensitive.
        * `fileMeta` without all four dates -> **500 "Nullable object must have a value"**, from a
          dereferenced nullable DateTime.

        `MD5` must be upper case; the server recomputes it and exposes it as `DataHash`, which is
        the only real proof the bytes arrived intact. Spitfire takes the stored filename from the
        `file` part, **not** from `fileMeta.Name` — measured 2026-08-14, when a file uploaded as
        `CC-TEST.ReceiverReport.pdf` in the metadata landed as `CC_TEST_Receiver_Report.pdf`. So
        `filename` here is what Premier will see.
        """
        stamp = when or date.today().isoformat()
        md5 = hashlib.md5(content).hexdigest().upper()
        suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        meta = {
            "value": filename, "Name": filename, "type": "file", "FileType": suffix,
            "size": len(content), "MD5": md5,
            # All four are mandatory. See the docstring; a missing one is a 500, not a 400.
            "date": f"{stamp}T00:00:00", "DocDate": f"{stamp}T00:00:00",
            "ReferenceDate": f"{stamp}T00:00:00", "Due": f"{stamp}T00:00:00",
            "Keywords": keywords,
        }
        response = self._request(
            "POST", "/api/catalog/upload?xm=catalog", note=f"upload {filename}",
            files={"fileMeta": (None, json.dumps(meta), "application/json"),
                   "file": (filename, content, "application/octet-stream")})
        payload = self._json_or_raise(response, f"uploading {filename}")
        key = (payload or {}).get("key")
        if not key:
            raise RuntimeError(f"uploading {filename} returned no key: {str(payload)[:200]}")
        return str(key)

    def verify_upload(self, file_key: str, expected_md5: str) -> bool:
        """Compare the server's `DataHash` against the MD5 we computed locally.

        The upload's own 200 does not establish this. `/versions` is used rather than `/meta` or
        `/object`, which return 500 on the very same key `/versions` accepts.
        """
        payload = self._json_or_raise(
            self._request("GET", f"/api/catalog/{file_key}/versions", note="verify hash"),
            "reading the catalog entry")
        rows = payload if isinstance(payload, list) else []
        server_hash = (rows[0] or {}).get("DataHash") if rows else None
        return bool(server_hash) and str(server_hash).upper() == expected_md5.upper()

    # --- 2. the receipt -------------------------------------------------------------------------

    def create_receipt(self, project_id: str, po_number: str,
                       receipt_type_key: Optional[str] = None) -> str:
        """Create a receipt document and return its `DocMasterKey`.

        The parent slot is the null GUID and the type key is the *second* path segment — this is
        `POST /api/document/{parent}/{typeKey}`, not a body-carrying create. There is no body.

        `forBatch` is the PO number and is what populates the header's `SubContract`, which is the
        only field tying the receipt to its purchase order; commitments, pay requests, change
        orders and receipts all carry the PO number there. If it does not come back set, the
        receipt is an orphan and the caller must not proceed — so `post_receipt` reads it back.

        Returns a bare quoted GUID, not an object.

        **This stages people.** The moment the document exists, Spitfire applies the configured
        approval chain: six routees, with three real Premier employees at sequence 10. Nothing is
        sent — that needs `route/apply`, which this module refuses by name — but they are on it.
        """
        type_key = receipt_type_key or settings.SPITFIRE_RECEIPT_DOC_TYPE_KEY
        path = (f"/api/document/{EMPTY_GUID}/{type_key}"
                f"?forProject={project_id}&forBatch={po_number}")
        payload = self._json_or_raise(
            self._request("POST", path, note=f"create receipt for PO {po_number}"),
            f"creating a receipt for PO {po_number}")
        if not (isinstance(payload, str) and len(payload) == 36):
            raise RuntimeError(f"creating a receipt returned {str(payload)[:120]!r}, "
                               "which is not a document key")
        return payload

    def set_title(self, doc_key: str, title: str) -> None:
        """`PATCH /api/document/{id}/Title`, body a **bare JSON string**.

        Called immediately after creation so a document is never sitting untitled in Premier's UI
        where somebody could open it and have no idea what it is or who made it.
        """
        response = self._request("PATCH", f"/api/document/{doc_key}/Title", json=title,
                                 note="title")
        if response.status_code >= 400:
            raise RuntimeError(f"titling the receipt failed — {self._explain(response)}")

    def add_line(self, doc_key: str, *, description: str, quantity: float,
                 source_item_number: Optional[str] = None, uom: Optional[str] = None,
                 proj_entity: Optional[str] = None) -> None:
        """Append a receipt line. **Almost certainly not what you want — see `set_line_quantity`.**

        A receipt created with `forBatch` is *already* built from the purchase order: one item per
        PO line, correctly linked, with only the quantity blank. This method adds a further row
        beside those, and Spitfire discards everything on it that would make it count —
        `SCDocItemKey`, `Subcontract`, `ProjEntity`, `AccountCategory`, `UOM` and the task quantity
        all read back null or zero. Every line this system posted before 2026-08-22 was one of
        those orphans, which is why no purchase order ever moved.

        Kept because appending a line is a real operation Spitfire supports and one may some day be
        wanted; it is no longer on the receipt path. Body is an **array**; `PUT /items` is 501.

        `UOM` and `ProjEntity` are copied from the matching PO line's `DocItemTask[0]` rather than
        invented: `ProjEntity` is the cost code the receipt posts against, and a wrong one books
        the receipt to the wrong budget line. Lines read back as `{DocNo}-{seq}`, never as the
        plain integer that was sent.
        """
        task: Dict[str, Any] = {"Quantity": quantity}
        if uom:
            task["UOM"] = uom
        if proj_entity:
            task["ProjEntity"] = proj_entity
        line: Dict[str, Any] = {
            "Description": description,
            "ItemQuantity": quantity,
            "DocItemTask": [task],
        }
        if source_item_number:
            line["SourceItemNumber"] = source_item_number
        response = self._request("POST", f"/api/document/{doc_key}/items", json=[line],
                                 note="add line")
        if response.status_code >= 400:
            raise RuntimeError(f"adding the receipt line failed — {self._explain(response)}")

    def find_prepopulated_line(self, doc_key: str, po_line_key: str) -> Optional[Dict[str, Any]]:
        """The receipt line already standing against this purchase order line, or None.

        Creating a receipt with `forBatch` does not produce an empty document. Spitfire builds it
        from the purchase order: one item per PO line, each already carrying `SCDocItemKey`,
        `SourceItemNumber`, `AccountCategory`, `ProjEntity`, `GLAcct`, `UOM` and `Rate`, numbered
        to match the order — gaps included. The only empty field is the quantity.

        Verified on training 2026-08-22 against PO 912559: `create_receipt` returned a document
        whose items 0001, 0002, 0004 and 0005 were already linked to that PO's four lines.

        Matched on `SCDocItemKey` — the PO line's own `DocItemKey` — because that is the identity
        Spitfire itself used to build the row. Matching on the spec would reintroduce exactly the
        ambiguity the matcher exists to resolve: 82 of 179 lines share a spec with a sibling.
        """
        return line_in(self.read_items(doc_key), po_line_key)

    def set_line_quantity(self, doc_key: str, quantities: Dict[str, float]) -> None:
        """Set the received quantity on lines Spitfire already built. `{ItemTaskKey: quantity}`.

        **Not `add_line`.** `POST /items` appends a *new* row, and everything that would make it
        count is discarded on insert — `SCDocItemKey`, `Subcontract`, `ProjEntity`,
        `AccountCategory`, `UOM` and the task quantity all read back null or zero, leaving an
        orphan attached to nothing. Every line this system posted before 2026-08-22 was one of
        those, which is why no purchase order ever moved. `POST /items` against a row that already
        exists answers `406 Column ItemNumber is constrained to be unique` — and still inserts.

        There is no item-level write route. `PUT /api/document/{id}/items` is documented in the v23
        schema as *"Updates the item"* but answers 501 on this server, and its own description names
        the alternative: *"Consider using PatchDocData (session/changes)"*. That is a document-edit
        session — the lock sfPMS's own UI takes when a person types in the box — and it is what
        this method drives:

            DELETE /api/document/{id}/session?sessionID=...       -> drop the inherited session
            GET    /api/document/{id}/session?freshenData=true    -> session id, a bare GUID
            PATCH  /api/document/{id}/session/changes             -> [DocFieldChange, ...]
            DELETE /api/document/{id}/session?sessionID=...       -> commits and releases

        **`DELETE` is what commits, not `POST /session/end`.** Both answer 200 and neither reports
        a difference; measured on training 2026-08-22, four lines patched and released with the
        POST read back 0.0, and the same four appeared the moment a session was released with
        DELETE. The POST leaves the changes staged in a session that stays open, which is worse
        than discarding them — the next writer inherits them.

        `InstanceKey` is the line's **`ItemTaskKey`**, out of `DocItemTask[0]` — not its
        `DocItemKey`. Every change goes in one PATCH: a whole delivery is one round trip, and a
        partial failure cannot leave half a receipt filled in.

        Two things learned the hard way and easy to reintroduce:

        * **Do not send `releaseSession=true`.** It answers
          `409 Violation of PRIMARY KEY constraint PK_xsfDocTopic`.
        * **`GET /session` returns the session already open**, not a new one, so a session left
          behind by an earlier failure is inherited rather than replaced. Ending it is therefore
          always safe, and always ours to do.

        The caller must read back *after* this returns, never inside it — see `verify_quantities`.
        """
        if not quantities:
            return

        # Release whatever session is already open on this document before taking one of our own.
        #
        # `GET /session` does not create a session, it returns the one in force — and creating a
        # receipt and titling it leaves one behind. A change staged into that inherited session is
        # **silently discarded**: measured on training 2026-08-22, two receipts built identically,
        # one patched through the inherited session and one through a session taken after
        # releasing it. Both answered 200 to every call. The first read back 0.0, the second 4.0.
        #
        # This is the whole difference between a quantity that lands and one that does not, and
        # nothing in any response distinguishes them — which is why the read-back in
        # `verify_quantities` is not optional.
        self._release_session(doc_key)

        session = self._json_or_raise(
            self._request("GET", f"/api/document/{doc_key}/session?freshenData=true",
                          note="open document session"),
            f"opening an edit session on {doc_key}")
        session_id = session if isinstance(session, str) else str(
            (session or {}).get("SessionID") or "")
        if not session_id:
            raise RuntimeError(
                f"opening an edit session on the receipt returned {str(session)[:120]!r}, "
                "which is not a session key — nothing was changed")

        changes = [{"DataMember": "DocItemTask", "DataField": "Quantity",
                    "InstanceKey": task_key, "Data": _fmt_quantity(quantity),
                    "IsURIEncoded": False}
                   for task_key, quantity in quantities.items()]
        self._commit_changes(doc_key, session_id, changes, note="set line quantities",
                             failure="setting the receipt line quantities failed")

    def _commit_changes(self, doc_key: str, session_id: str, changes: list, *,
                        note: str, failure: str) -> None:
        """PATCH staged field changes and always release the session afterwards.

        Shared by `set_line_quantity` and `sign_off_route_steps` because the rule that matters is
        the same for both and is not obvious: the release in the `finally` is what *commits*. A
        session left open holds an edit lock on a document in Premier's system that nobody can see
        in order to release it, and the changes staged in it stay staged, so the next writer
        inherits them.
        """
        try:
            response = self._request("PATCH", f"/api/document/{doc_key}/session/changes",
                                     json=changes, note=note)
            if response.status_code >= 400:
                raise RuntimeError(f"{failure} — {self._explain(response)}")
        finally:
            self._release_session(doc_key, session_id)

    def _release_session(self, doc_key: str, session_id: str = "") -> None:
        """Commit and release the document's edit session. Safe when there is nothing to release.

        `DELETE /session` is what commits staged changes; `POST /session/end` answers 200 and does
        not. Called both before taking a session (to drop an inherited one, whose staged changes
        would otherwise be discarded along with ours) and after patching (to commit).

        Never raises. On the way in, a document with no session is the normal case; on the way out
        this runs in a `finally` where the real error is the one already in flight.
        """
        if not session_id:
            try:
                current = self._request("GET", f"/api/document/{doc_key}/session",
                                        note="check for an open session")
                session_id = current.json() if current.status_code == 200 else ""
            except Exception:                                          # noqa: BLE001
                return
            if not isinstance(session_id, str) or not session_id.strip():
                return
        try:
            self._request("DELETE", f"/api/document/{doc_key}/session?sessionID={session_id}",
                          note="commit and release document session")
        except Exception:                                              # noqa: BLE001
            _logger.warning("could not release the edit session on document %s", doc_key)

    def session_user_key(self) -> str:
        """Our own `UserKey`. `whoami` returns the email, which route rows do not carry."""
        payload = self._json_or_raise(
            self._request("GET", "/api/session/who", note="identity"), "reading the session")
        return str((payload or {}).get("UserKey") or "") if isinstance(payload, dict) else ""

    def read_route(self, doc_key: str) -> list:
        """The document's approval route, one entry per routee."""
        payload = self._json_or_raise(
            self._request("GET", f"/api/document/{doc_key}/route", note="read route"),
            f"reading the route on {doc_key}")
        return payload if isinstance(payload, list) else []

    MAX_SIGN_PASSES = 4
    """How many times `sign_off_route_steps` will re-read the route.

    Our account sits at sequences 1, 5 and 15, so three signatures is the most any document has
    ever needed and the fourth pass is the one that finds nothing and stops. A bound rather than
    `while True` because the loop's exit depends on the *server* changing `Status` — if it ever
    reported a row as unacted after accepting the write, an unbounded loop would sign it for ever.
    """

    def sign_off_route_steps(self, doc_key: str) -> List[str]:
        """Sign off **our own** stops on this receipt's route. Returns one line per stop signed.

        Every receipt this system creates is staged with an approval route, and ours is the first
        stop on it. Until that stop is responded to the route never advances and Premier's
        reviewers at sequence 10 never see the receipt — so a POD posted and left unsigned is a
        proof nobody is asked to look at. On a fresh receipt our stops are sequences 1 and 5.

        **They cannot both be signed from one reading of the route, and that is the whole shape of
        this method.** Measured on receipt 0002 (2026-09-17): sequence 1 was `Reached` and offered
        `A,H,P`, while sequence 5 had no `Reached` timestamp at all and offered `C,D,P,G` — no `A`
        among them. Spitfire will not accept a response on a stop the route has not arrived at, and
        signing sequence 1 is what makes sequence 5 arrive. So this signs one stop, **reads the
        route again**, and signs whatever became reachable, until a pass finds nothing. A single
        pass looks like it worked and silently leaves sequence 5 Pending.

        Scope, deliberately narrow, and unchanged by the loop:

        * **Only rows that are ours and `Reached`.** Chosen by `UserKey` and timestamp, never by a
          hard-coded sequence: our account sits at 1, 5 and 15 and which one is live moves over
          time. `Reached` is the gate Spitfire itself applies — a stop the route has not arrived
          at cannot be responded to.
        * **Only where Spitfire says we may**, i.e. the row offers `CanEditRouteResponseCode`
          enabled and `A` among its choices. If the server does not offer the capability we do not
          invent it.
        * **`Status` only.** On Premier's own completed receipts every acted row reads
          `ResponseCode = None`; only `Status` moves to `A`. The `ResponseCode "A"` seen elsewhere
          was on a purchase-order route, a different document type with different conventions.

        Signing our step asserts "the proof of delivery is attached". It is **not** approving the
        receipt: sequence 10 is Premier's decision, and dispatching the route — `route/apply`,
        which emails three real people — is refused by name in `_DENIED_SUBSTRINGS`. Nothing here
        adds, removes or reorders a routee. Signing advances the route; it does not announce it.
        """
        user_key = self.session_user_key().lower()
        if not user_key:
            return ["could not sign the route: the session names no user key"]

        signed: List[str] = []
        refused: set = set()
        for _ in range(self.MAX_SIGN_PASSES):
            row = self._next_signable_step(doc_key, user_key, refused)
            if row is None:
                break
            sequence = row.get("Sequence")
            if not self._sign_one_step(doc_key, row, signed):
                # Recorded so the next pass does not pick the same row again and spend every
                # remaining pass on it.
                refused.add(str(row.get("RouteID")))
                continue
            signed.append(f"route step {sequence} signed off")

        return signed or ["no route step of ours is reached and unsigned"]

    def _next_signable_step(self, doc_key: str, user_key: str, refused: set):
        """The first stop of ours the route has reached and Spitfire will accept a response on.

        Re-reads the route every time it is called: a stop that was neither reached nor offering
        `A` a moment ago may be both now, because signing the stop before it is what advances the
        route onto it.
        """
        for row in self.read_route(doc_key):
            if str(row.get("UserKey") or "").lower() != user_key:
                continue
            if str(row.get("RouteID")) in refused:
                continue
            if not _route_reached(row) or str(row.get("Status") or "") == RESPONDED:
                continue
            allowed = any(command.get("CommandName") == "CanEditRouteResponseCode"
                          and command.get("Enabled")
                          for command in (row.get("MenuCommands") or []))
            if allowed and RESPONDED in str(row.get("Choices") or "").split(","):
                return row
        return None

    def _sign_one_step(self, doc_key: str, row, signed: List[str]) -> bool:
        """Set `DocRoute.Status = A` on one row. False when nothing was written, with the reason
        already appended to `signed` so the caller's report says what happened."""
        sequence = row.get("Sequence")
        self._release_session(doc_key)
        session = self._json_or_raise(
            self._request("GET", f"/api/document/{doc_key}/session?freshenData=true",
                          note="open document session"),
            f"opening an edit session on {doc_key}")
        session_id = session if isinstance(session, str) else str(
            (session or {}).get("SessionID") or "")
        if not session_id:
            signed.append(f"route step {sequence} not signed: no edit session")
            return False

        self._commit_changes(
            doc_key, session_id,
            [{"DataMember": "DocRoute", "DataField": "Status",
              "InstanceKey": row.get("RouteID"), "Data": RESPONDED, "IsURIEncoded": False}],
            note="sign off our route step",
            failure=f"signing route step {sequence} failed")
        return True

    def verify_quantities(self, doc_key: str, quantities: Dict[str, float]) -> Dict[str, float]:
        """Read the receipt back and return `{ItemTaskKey: quantity}` as Spitfire now holds it.

        Separate from `set_line_quantity` so that it is called *after* the session has been
        released. A read taken while the session is still open returns the pre-change value: on
        2026-08-22 a line read `0.0` immediately after its session was dropped and `4.0` moments
        later. That staleness made a correct write look like a failure twice during the
        investigation, and is exactly the trap a read-back is supposed to close.
        """
        found = {}
        for item in self.read_items(doc_key):
            for task in (item.get("DocItemTask") or []):
                if not isinstance(task, dict):
                    continue
                task_key = str(task.get("ItemTaskKey") or "")
                if task_key in quantities:
                    found[task_key] = float(task.get("Quantity") or 0.0)
        return found

    # --- 3. attachments -------------------------------------------------------------------------
    # One endpoint, two shapes. Premier's own receipt 909330 carries both in a single collection:
    # three document links (its PO and two pay requests) and one file link (the POD), told apart
    # only by which of `DocKey` / `AttachedDocMaster` is populated.

    def attach_file(self, doc_key: str, file_key: str, *, note: str = "",
                    cat_type: Optional[str] = None) -> None:
        """Hang an uploaded file on a document. `DocKey` = the catalog key.

        `CatType` is sent because Premier's own rows carry it, but do not depend on it: it came
        back all zeros on 2026-08-14 despite a valid receipt type GUID being sent, so Spitfire
        drops it on insert. Grouping attachments by category will not work.
        """
        body = [{"DocKey": file_key, "AttachedDocMaster": EMPTY_GUID, "Note": note,
                 "CatType": cat_type or settings.SPITFIRE_RECEIPT_DOC_TYPE_KEY,
                 "MailRoute": "P", "AccessLevel": "V"}]
        response = self._request("POST", f"/api/document/{doc_key}/attachments", json=body,
                                 note="attach file")
        if response.status_code >= 400:
            raise RuntimeError(f"attaching the file failed — {self._explain(response)}")

    def link_document(self, doc_key: str, target_doc_key: str, *, note: str = "",
                      cat_type: Optional[str] = None) -> None:
        """Link one document to another — the receipt to its PO, or to a pay request.

        Same endpoint as `attach_file`; the populated field is what differs. Deliberately **not**
        `PUT /api/document/{id}/link`, which *creates* child documents from type keys and returns
        500 when handed an existing key.
        """
        body = [{"DocKey": EMPTY_GUID, "AttachedDocMaster": target_doc_key, "Note": note,
                 "CatType": cat_type or settings.SPITFIRE_PO_DOC_TYPE_KEY,
                 "MailRoute": "P", "AccessLevel": "V"}]
        response = self._request("POST", f"/api/document/{doc_key}/attachments", json=body,
                                 note="link document")
        if response.status_code >= 400:
            raise RuntimeError(f"linking the document failed — {self._explain(response)}")

    # --- 4. read-backs --------------------------------------------------------------------------

    def read_header(self, doc_key: str) -> Dict[str, Any]:
        payload = self._json_or_raise(
            self._request("GET", f"/api/document/{doc_key}", note="read back header"),
            "reading the document")
        return payload if isinstance(payload, dict) else {}

    def read_items(self, doc_key: str) -> List[Dict[str, Any]]:
        payload = self._json_or_raise(
            self._request("GET", f"/api/document/{doc_key}/items", note="read back items"),
            "reading the document lines")
        return payload if isinstance(payload, list) else []

    def read_attachments(self, doc_key: str) -> List[Dict[str, Any]]:
        payload = self._json_or_raise(
            self._request("GET", f"/api/document/{doc_key}/attachments",
                          note="read back attachments"),
            "reading the attachments")
        return payload if isinstance(payload, list) else []

    def find_documents(self, project_id: str, doc_type_key: str, *,
                       doc_no_like: str = "", limit: int = 25) -> List[Dict[str, Any]]:
        """Search a project for documents of one type. A read, despite being a POST.

        Used to find the PO to link to and the pay requests that share its `SubContract`. An empty
        filter body returns 200 with **zero** rows — empty means "match nothing" here, not "match
        everything" — and `DocNoLike` is a *contains* match, so callers re-check equality.
        """
        body: Dict[str, Any] = {"ForDocType": doc_type_key, "IncludeDocs": True,
                                "IncludeFiles": False, "IncludeClosed": True, "ResultLimit": limit}
        if doc_no_like:
            body["DocNoLike"] = doc_no_like
        payload = self._json_or_raise(
            self._request("POST", f"/api/project/{project_id}/docs", json=body, note="find docs"),
            f"searching project {project_id}")
        return payload if isinstance(payload, list) else []

    def audit_rows(self) -> List[Dict[str, Any]]:
        """The audit log as plain dicts, for persisting beside the post ledger."""
        return [{"method": r.method, "path": r.path, "status": r.status,
                 "elapsed_ms": r.elapsed_ms, "note": r.note} for r in self.audit_log]
