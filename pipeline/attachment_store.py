"""Where the bytes actually live.

Until this existed, **ingest kept no copy of any attachment.** `attachment_ledger` recorded a
filename, a size and a sha256; `accumulation.payload_json` had a `content_bytes` field that was
empty on every row in Premier's live store; and `mail_attachment` was populated lazily, only for
messages a human happened to click. So the one durable copy of every POD, BOL and tracker
spreadsheet was Premier's Outlook mailbox, re-fetched on demand. Delete or archive the mail and the
evidence was gone — for a system whose product *is* delivery evidence.

**Content-addressed**, by the sha256 the ledger already computes. Three things follow from that,
all of them wanted here:

* Premier really does send byte-identical PODs under different filenames — the 5-Star thread
  carries two, 20,535 bytes each — so one file backs both ledger rows instead of two copies.
* Writing is idempotent. Re-ingesting the same mail rewrites nothing.
* A blob cannot be silently wrong: the name *is* the hash, so verification is a re-read.

On disk rather than in SQLite. The limits allow 25 MB per attachment and 100 MB per email, which
turns the state database into gigabytes of BLOB that every unrelated query then pages around — and
this data is write-once, read-rarely, which is what a filesystem is for. It also means Premier can
be handed the folder.

The two-character shard keeps directory listings usable; ext4 and NTFS both slow down measurably
with a hundred thousand entries in one directory.
"""

import hashlib
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from config import settings

_logger = logging.getLogger(__name__)

ROOT = settings.STATE_DIR / "attachments"


def _path_for(sha256: str, root: Optional[Path] = None) -> Path:
    return (root or ROOT) / sha256[:2] / sha256


def path_for(sha256: str, root: Optional[Path] = None) -> Optional[Path]:
    """Where this content is, or None if it was never stored."""
    if not sha256:
        return None
    candidate = _path_for(sha256, root)
    return candidate if candidate.exists() else None


def put(data: bytes, root: Optional[Path] = None) -> Optional[str]:
    """Store these bytes, returning their sha256. Idempotent; None for empty input.

    Written to a temporary file in the same directory and then renamed, so a crash mid-write
    cannot leave a truncated blob sitting under a name that claims to be its hash. `os.replace`
    is atomic on POSIX and on Windows.
    """
    if not data:
        return None
    digest = hashlib.sha256(data).hexdigest()
    target = _path_for(digest, root)
    if target.exists():
        return digest

    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".part")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(temp_name, target)
    except Exception:
        # A store that fails must not take the run with it: the ledger row, the triage verdict and
        # the extracted records are all still correct without the blob, and `verify_attachments`
        # reports the gap. Losing the email because the disk was full would be the worse trade.
        _logger.warning("could not store attachment %s", digest, exc_info=True)
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        return None
    return digest


def get(sha256: str, root: Optional[Path] = None) -> Optional[bytes]:
    """The stored bytes, or None if this content was never stored or has gone missing."""
    found = path_for(sha256, root)
    if found is None:
        return None
    try:
        return found.read_bytes()
    except OSError:
        _logger.warning("attachment %s is in the ledger but unreadable on disk", sha256)
        return None


def exists(sha256: str, root: Optional[Path] = None) -> bool:
    return path_for(sha256, root) is not None
