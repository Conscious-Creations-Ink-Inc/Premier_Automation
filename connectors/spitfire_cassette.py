"""Record Spitfire's responses while connected; replay them when not.

Spitfire is reachable only from Premier's office IP, and the credential in use is a hand-captured
browser cookie that lapses on idle — so even *at* the office the window is short. Without this,
every screen downstream of a live read (Verify, the PO mirror, the post gates) is undevelopable
from anywhere else.

**This mounts underneath the connectors, not inside them.** Both `SpitfireReadClient` and
`SpitfireWriteClient` build a `requests.Session` and issue every call through it, so a transport
adapter on that session intercepts below everything the clients do. The allowlist check, the
read connector's retry-a-500-once rule, the write connector's never-retry rule, the audit log and
the 401 -> `SpitfireSessionExpired` handling all keep running exactly as they do today, around a
response that happens to have come from disk. Not one line of either connector's logic changes.

**Default is `off`, and `off` mounts nothing.** With `SPITFIRE_CASSETTE_MODE` unset the session is
left with the adapters `requests` gave it, and the code takes the paths it takes today.

Writes are never replayed. Replaying `create_receipt` would hand back the same DocMasterKey on
every call, so two different deliveries would both "become" one receipt and the ledger would record
a key that looks real; and a recorded 200 says nothing about a body that has since changed. So in
replay the four mutating operations raise `SpitfireOffline`, while the read-backs beside them
(`/items`, `/attachments`, `catalog/{key}/versions`) replay like any other GET. That split falls
straight out of the allowlist `spitfire_write.py` already keeps.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlsplit

import requests
from requests.adapters import HTTPAdapter

from config import settings

_logger = logging.getLogger(__name__)

OFF = "off"
RECORD = "record"
REPLAY = "replay"
AUTO = "auto"
MODES = (OFF, RECORD, REPLAY, AUTO)


class SpitfireCassetteMiss(RuntimeError):
    """Replay was asked for a call nothing has recorded.

    The message names the method, path and body hash on purpose: it is a to-do list for the next
    time somebody is on the office network, not a mystery to debug offline.
    """


class SpitfireOffline(RuntimeError):
    """A write was attempted while replaying. Nothing was sent."""


# Mutating operations, by path shape. Matched on the *path*, so a GET read-back on the same
# document is unaffected — `POST /api/document/{k}/items` refuses while `GET /api/document/{k}/items`
# replays. Kept here rather than imported from `spitfire_write` so this module has no dependency on
# the write client, which cannot even be constructed without a session cookie.
_WRITE_PATHS = (
    ("POST", re.compile(r"^/api/catalog/upload$", re.I)),
    ("PATCH", re.compile(r"^/api/document/[^/]+/title$", re.I)),
    ("POST", re.compile(r"^/api/document/[^/]+/items$", re.I)),
    ("POST", re.compile(r"^/api/document/[^/]+/attachments$", re.I)),
    # Document creation: POST /api/document/{parent}/{typeKey}, two GUID segments and nothing after.
    ("POST", re.compile(r"^/api/document/[0-9a-f-]{36}/[0-9a-f-]{36}$", re.I)),
)


def mode() -> str:
    """The configured mode, normalised. Anything unrecognised is `off` — a typo in `.env` must not
    silently put a developer into replay and have them trust recorded figures as live ones."""
    value = str(getattr(settings, "SPITFIRE_CASSETTE_MODE", OFF) or OFF).strip().lower()
    if value not in MODES:
        _logger.warning("SPITFIRE_CASSETTE_MODE=%r is not one of %s; treating it as off",
                        value, ", ".join(MODES))
        return OFF
    return value


def writes_refused() -> bool:
    """True when a write cannot reach Spitfire because this session is replaying.

    Read by the UI so the Post control is not offered at all — the rule `_post_cell` already
    applies to "no POD" and "N gaps": a control whose only possible outcome is a refusal teaches
    people to ignore refusals.
    """
    return mode() == REPLAY


def is_write(method: str, path: str) -> bool:
    upper = method.upper()
    return any(upper == m and pattern.match(path) for m, pattern in _WRITE_PATHS)


# --- the store ----------------------------------------------------------------------------------

@dataclass
class Cassette:
    """One recorded exchange, as it is stored and as it is replayed."""
    method: str
    path: str
    query: str
    body_sha1: str
    status: int
    content_type: str
    body: bytes
    recorded_at: str
    base_url: str

    def to_meta(self) -> Dict[str, Any]:
        return {
            "method": self.method, "path": self.path, "query": self.query,
            "body_sha1": self.body_sha1, "status": self.status,
            "content_type": self.content_type, "bytes": len(self.body),
            "recorded_at": self.recorded_at, "base_url": self.base_url,
        }


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _canonical_query(query: str) -> str:
    """Query args sorted, so `?forProject=X&forBatch=Y` and the reverse are one cassette.

    `keep_blank_values` because Spitfire's own URLs carry empty args and dropping them would make
    two genuinely different calls collide.
    """
    if not query:
        return ""
    pairs = sorted(parse_qsl(query, keep_blank_values=True))
    return "&".join(f"{k}={v}" for k, v in pairs)


def _body_bytes(body: Any) -> bytes:
    if body is None:
        return b""
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode("utf-8")
    # A multipart upload body is a generator; it is a write and never recorded, but the key still
    # has to be computable without consuming it.
    return b"<stream>"


class CassetteStore:
    """Cassettes on disk: one JSON envelope per exchange, named by its key.

    JSON rather than the raw body alone, because the status code and content type are part of what
    is being replayed — a recorded 500 must replay as a 500, or the read connector's retry rule is
    exercised against a shape it never saw live.
    """

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root or settings.SPITFIRE_CASSETTE_DIR)

    def key(self, method: str, path: str, query: str = "", body: Any = None) -> str:
        parts = "|".join((
            method.upper(),
            path.rstrip("/") or "/",
            _canonical_query(query),
            _sha1(_body_bytes(body)),
        ))
        return _sha1(parts.encode("utf-8"))

    def _file(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def lookup(self, method: str, path: str, query: str = "", body: Any = None) -> Optional[Cassette]:
        key = self.key(method, path, query, body)
        on_disk = self._file(key)
        if not on_disk.exists():
            return None
        try:
            envelope = json.loads(on_disk.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # A corrupt cassette is a miss, not a crash: the caller's error names the call, which
            # is more useful than a JSONDecodeError naming a hash.
            _logger.warning("cassette %s could not be read (%s); treating as a miss", key, exc)
            return None
        return Cassette(
            method=envelope["method"], path=envelope["path"], query=envelope.get("query", ""),
            body_sha1=envelope.get("body_sha1", ""), status=int(envelope.get("status", 0)),
            content_type=envelope.get("content_type", ""),
            body=bytes.fromhex(envelope.get("body_hex", "")),
            recorded_at=envelope.get("recorded_at", ""), base_url=envelope.get("base_url", ""),
        )

    def record(self, method: str, path: str, response: requests.Response,
               query: str = "", body: Any = None) -> Cassette:
        cassette = Cassette(
            method=method.upper(), path=path, query=_canonical_query(query),
            body_sha1=_sha1(_body_bytes(body)), status=response.status_code,
            content_type=str(response.headers.get("Content-Type") or ""),
            body=response.content or b"",
            recorded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            base_url=str(getattr(settings, "SPITFIRE_BASE_URL", "") or ""),
        )
        self.write(self.key(method, path, query, body), cassette)
        return cassette

    def write(self, key: str, cassette: Cassette) -> Path:
        """Body as hex, not base64 or text.

        Most bodies are JSON and would survive as text, but `catalog` responses are not, and one
        store that holds both without a per-entry decision is worth the doubled size on disk — the
        whole capture is single-digit megabytes.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        envelope = cassette.to_meta()
        envelope["body_hex"] = cassette.body.hex()
        target = self._file(key)
        target.write_text(json.dumps(envelope, indent=1), encoding="utf-8")
        return target

    def replay(self, cassette: Cassette, request: Optional[requests.PreparedRequest] = None
               ) -> requests.Response:
        """A real `requests.Response`, so `.json()`, `.text` and `.raise_for_status()` all behave
        and nothing downstream can tell the difference."""
        response = requests.Response()
        response.status_code = cassette.status
        response._content = cassette.body                      # noqa: SLF001 — the documented seam
        response.encoding = "utf-8"
        response.reason = "OK" if cassette.status < 400 else "Error"
        if cassette.content_type:
            response.headers["Content-Type"] = cassette.content_type
        # A marker a human reading a captured audit trail can see. Nothing in the codebase reads it;
        # it exists so "where did this figure come from" is answerable after the fact.
        response.headers["X-Spitfire-Cassette"] = "replay"
        response.headers["X-Spitfire-Cassette-Recorded-At"] = cassette.recorded_at
        if request is not None:
            response.request = request
            response.url = request.url or ""
        return response

    def count(self) -> int:
        return len(list(self.root.glob("*.json"))) if self.root.exists() else 0


# --- the adapter --------------------------------------------------------------------------------

class CassetteAdapter(HTTPAdapter):
    """Sits under `requests` and answers from disk, records to it, or does neither.

    Subclassing `HTTPAdapter` rather than replacing the transport wholesale means `record` and a
    miss in `auto` fall through to the real implementation with every connection-pool and TLS
    setting the session already had.
    """

    def __init__(self, store: Optional[CassetteStore] = None, **kwargs):
        self.store = store or CassetteStore()
        super().__init__(**kwargs)

    def send(self, request: requests.PreparedRequest, **kwargs) -> requests.Response:
        current = mode()
        if current == OFF:
            return super().send(request, **kwargs)

        split = urlsplit(request.url or "")
        path = self._api_path(split.path)
        method = (request.method or "GET").upper()

        if method == "POST" and path.lower() == "/api/account":
            # The login is never recorded or replayed. Its body carries the password, and its whole
            # point is the `Set-Cookie` ticket, which a cassette does not store — a replayed login
            # would "succeed" and authenticate nothing.
            if current == REPLAY:
                raise SpitfireOffline("cannot log in to Spitfire while replaying recorded responses")
            return super().send(request, **kwargs)

        if current == REPLAY and is_write(method, path):
            raise SpitfireOffline(
                f"{method} {path} writes to Spitfire, and this session is replaying recorded "
                f"responses. Writes are only possible from Premier's office network."
            )

        if current in (REPLAY, AUTO):
            cassette = self.store.lookup(method, path, split.query, request.body)
            if cassette is not None:
                _logger.debug("cassette hit: %s %s", method, path)
                return self.store.replay(cassette, request)
            if current == REPLAY:
                raise SpitfireCassetteMiss(
                    f"nothing recorded for {method} {path}"
                    + (f"?{_canonical_query(split.query)}" if split.query else "")
                    + f" (body sha1 {_sha1(_body_bytes(request.body))[:12]}). "
                    f"Run tools/spitfire_record_cassettes.py from the office network."
                )

        response = super().send(request, **kwargs)
        if current in (RECORD, AUTO) and not is_write(method, path):
            try:
                self.store.record(method, path, response, split.query, request.body)
            except OSError as exc:
                # Never let a full disk turn a successful live read into a failure.
                _logger.warning("could not record %s %s: %s", method, path, exc)
        return response

    @staticmethod
    def _api_path(url_path: str) -> str:
        """The path as the connectors express it — `/api/...`, with the site prefix removed.

        `SPITFIRE_BASE_URL` ends in `/Training`, and hard-coding a recording to that prefix would
        make every cassette useless the day Premier moves us to production. Everything from `/api`
        onwards is the part that identifies the call.
        """
        marker = url_path.lower().find("/api/")
        return url_path[marker:] if marker >= 0 else url_path


def mount(session: requests.Session, store: Optional[CassetteStore] = None) -> bool:
    """Install the cassette adapter on a session. Returns whether it did.

    A no-op when the mode is `off`, which is the default — this is what makes the whole feature
    invisible until somebody asks for it.
    """
    if mode() == OFF:
        return False
    adapter = CassetteAdapter(store)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return True
