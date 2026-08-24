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
from connectors import spitfire_cassette

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
    ("POST",  "/api/document/{}/items"),       # array. PUT /items is an undiscoverable 500
    ("POST",  "/api/document/{}/attachments"), # array. file link OR doc link, see attach_*
)

# Document creation is `POST /api/document/{parent}/{typeKey}` and is NOT in the table above,
# because as a two-wildcard template it is far wider than it looks: `/api/document/{}/{}` also
# matches `POST /api/document/{id}/next` — which creates a document with an all-zero DocTypeKey —
# and `POST /api/document/from/{id}`, which silently copies one. Both were created by accident
# during the 13 Aug write probe from empty-body calls expected to be rejected. So creation is
# matched by shape instead: the parent must be the null GUID and the type must be a real GUID.
_GUID_LENGTH = 36


def _is_guid(value: str) -> bool:
    parts = value.split("-")
    return (len(value) == _GUID_LENGTH and len(parts) == 5
            and all(c in "0123456789abcdefABCDEF" for c in value.replace("-", "")))


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
)

_ALLOWED = _ALLOWED_WRITES + _ALLOWED_READBACKS

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
    if method.upper() == "DELETE":
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

    Cookie mode only, on purpose. `POST /api/Account` works and would let this class log in with
    a password, but there is no service account yet: the credential in `.env` belongs to a named
    human and carries AdminLevel 31 (Read+Insert+Update+Delete+Blanket). Silently re-authenticating
    a write client when a ticket lapses would turn an expiry into an unattended write, so an
    expired ticket raises `SpitfireSessionExpired` and stops.
    """

    def __init__(self, base_url: Optional[str] = None, session_cookie: Optional[str] = None,
                 timeout: int = 90):
        self.base_url = (base_url or settings.SPITFIRE_BASE_URL or "").rstrip("/")
        if not self.base_url:
            raise ValueError("SpitfireWriteClient needs SPITFIRE_BASE_URL (check .env)")
        self.session_cookie = session_cookie or settings.SPITFIRE_SESSION_COOKIE
        if not self.session_cookie:
            raise SpitfireSessionExpired(
                "no SPITFIRE_SESSION_COOKIE is set; capture one from the browser "
                "(F12 -> Application -> Cookies -> sfPMSAuth) before posting")
        self.timeout = timeout
        self.audit_log: List[WriteRecord] = []
        self._session = requests.Session()
        # See connectors/spitfire_cassette.py. A no-op unless SPITFIRE_CASSETTE_MODE is set. In
        # replay it answers the read-backs from disk and refuses the four mutating calls outright —
        # a replayed create_receipt would return one DocMasterKey for every delivery.
        spitfire_cassette.mount(self._session)
        host = urlparse(self.base_url).hostname
        # On the jar rather than as a header, so redirects and any later Set-Cookie merge normally.
        self._session.cookies.set("sfPMSAuth", self.session_cookie, domain=host, path="/")

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
        """
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
        """Add one receipt line. Body is an **array**; `PUT /items` is an undiscoverable 500.

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

    # --- 3. attachments -------------------------------------------------------------------------
    # One endpoint, two shapes. Premier's own receipt 209330 carries both in a single collection:
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
