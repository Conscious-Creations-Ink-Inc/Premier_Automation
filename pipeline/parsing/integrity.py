"""Is this file readable, encrypted, or broken?

Three states that look identical from the outside currently collapse into one. `PdfAdapter`
opens a PDF inside a bare `except Exception: return []`, so a password-protected file reports
"no text layer"; `OcrAdapter` then claims it as a scan, OCR finds nothing, and the file
disappears. A locked PDF and a photographed POD are the same object to this pipeline.

They need different answers from a person: *"ask the sender for an unlocked copy"* versus
*"this is a photo, it needs OCR"* versus *"the file is truncated, re-request it"*. Telling them
apart costs a header probe.

Stdlib only, plus `msoffcrypto` where it is already installed — nothing here may raise, because
it runs on bytes that are by definition suspect.
"""

import io
import re
import zipfile
from typing import Tuple

READABLE = "readable"
ENCRYPTED = "encrypted"
CORRUPT = "corrupt"
SCANNED = "scanned"      # a PDF that opens cleanly and simply has no text layer

# The trailer of an encrypted PDF carries an /Encrypt reference. Checking the raw bytes is
# faster and far more robust than opening the document, which is what fails on these files.
_PDF_ENCRYPT_RE = re.compile(rb"/Encrypt\s+\d+\s+\d+\s+R")
_PDF_EOF_RE = re.compile(rb"%%EOF\s*$")

_ZIP_ENCRYPTED_FLAG = 0x1


def pdf_state(content_bytes: bytes) -> Tuple[str, str]:
    """`(state, detail)` for PDF bytes, without depending on a successful parse.

    Returns `READABLE` when a text layer is present, `SCANNED` when the document opens but holds
    no text, `ENCRYPTED` when it is password-protected, `CORRUPT` when it will not open at all.
    """
    if not content_bytes.startswith(b"%PDF-"):
        return CORRUPT, "missing %PDF- header"

    if _PDF_ENCRYPT_RE.search(content_bytes):
        return ENCRYPTED, "PDF trailer declares /Encrypt — password-protected; request an unlocked copy"

    if not _PDF_EOF_RE.search(content_bytes[-2048:]):
        # Not fatal on its own — some generators pad past %%EOF — so it only becomes the verdict
        # if the document also fails to open below.
        truncated_hint = "no %%EOF marker; the file looks truncated"
    else:
        truncated_hint = ""

    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(content_bytes)) as pdf:
            for page in pdf.pages:
                if (page.extract_text() or "").strip():
                    return READABLE, "text layer present"
        return SCANNED, "opens cleanly, no text layer — an image-only PDF"
    except Exception as e:
        name = type(e).__name__
        if "Password" in name or "password" in str(e).lower():
            return ENCRYPTED, f"{name}: password required"
        return CORRUPT, truncated_hint or f"{name}: {e}"


def zip_state(content_bytes: bytes) -> Tuple[str, str]:
    """`(state, detail)` for zip bytes, including OOXML (.xlsx/.docx/.pptx are zips)."""
    try:
        with zipfile.ZipFile(io.BytesIO(content_bytes)) as archive:
            members = archive.infolist()
            if any(info.flag_bits & _ZIP_ENCRYPTED_FLAG for info in members):
                return ENCRYPTED, "one or more members are password-protected"
            if not members:
                return CORRUPT, "archive contains no entries"
            return READABLE, f"{len(members)} member(s)"
    except zipfile.BadZipFile as e:
        return CORRUPT, f"BadZipFile: {e}"
    except Exception as e:
        return CORRUPT, f"{type(e).__name__}: {e}"


def office_state(content_bytes: bytes) -> Tuple[str, str]:
    """`(state, detail)` for an Office document, OLE2 or OOXML.

    An encrypted OOXML file is an OLE2 container holding an `EncryptedPackage` stream — so it
    does not even look like a zip, and `openpyxl` reports it as corrupt. `msoffcrypto` knows the
    difference; if it is unavailable the byte marker is a good enough fallback.
    """
    if b"EncryptedPackage" in content_bytes[:65536]:
        return ENCRYPTED, "OOXML EncryptedPackage stream — password-protected"

    try:
        import msoffcrypto
        handle = msoffcrypto.OfficeFile(io.BytesIO(content_bytes))
        if handle.is_encrypted():
            return ENCRYPTED, "msoffcrypto reports the document is encrypted"
        return READABLE, "not encrypted"
    except ImportError:
        pass
    except Exception:
        # msoffcrypto rejects plenty of perfectly readable files (it only understands the
        # formats it can decrypt), so a failure here says nothing either way.
        pass

    if content_bytes.startswith(b"PK\x03\x04"):
        return zip_state(content_bytes)
    return READABLE, "no encryption marker found"


def describe(state: str) -> str:
    """The sentence a human sees on the exception queue."""
    return {
        READABLE: "readable",
        SCANNED: "image-only — needs OCR",
        ENCRYPTED: "password-protected — ask the sender for an unlocked copy",
        CORRUPT: "unreadable or truncated — ask the sender to resend",
    }.get(state, state)
