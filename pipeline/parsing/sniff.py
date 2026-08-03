"""Content sniffing — decide what an attachment *is* from its bytes, never its filename.

Two facts from the real corpus force this:

1. `.msg` attachments frequently carry `mimetype=None` (e.g. `Cameo Receivers.xlsx` and the
   3 MB `IMG_2479.jpeg` phone photos both arrive with no content type at all), so a
   content-type-only dispatch — what stage3 did before — silently drops them.
2. Filenames lie. `5star Fabric Verification Response.msg` carries two PDFs,
   `210634 - P. Kaufmann FedEx POD.pdf` and
   `FedEx 884603885067  GR-350d-WTF 78 yards from Daniel Stuart.pdf`, that are byte-identical
   (20535 bytes each) — one of the two names is simply wrong.

Also here: the inline-image filter. Every Premier email drags along the same 15 948-byte
signature logo (`image.png` / `image00N.png` / `Outlook-https___ww.png`), sometimes eight
copies in one message. Sending those to OCR is pure cost with zero signal.
"""

import hashlib
import re
from dataclasses import dataclass
from typing import Optional

# --- Kinds -----------------------------------------------------------------
# Deliberately coarse: it answers "which adapter", not "which exact format".

KIND_PDF = "pdf"
KIND_ZIP_OFFICE = "zip_office"   # xlsx/docx/pptx — all OOXML zips, disambiguated below
KIND_XLSX = "xlsx"
KIND_DOCX = "docx"
KIND_LEGACY_OFFICE = "legacy_office"   # OLE2: .doc/.xls/.msg
KIND_MSG = "msg"
KIND_IMAGE = "image"
KIND_HTML = "html"
KIND_TEXT = "text"
KIND_UNKNOWN = "unknown"

_MAGIC = [
    (b"%PDF-", KIND_PDF),
    (b"\x89PNG\r\n\x1a\n", KIND_IMAGE),
    (b"\xff\xd8\xff", KIND_IMAGE),          # JPEG (all APPn variants)
    (b"GIF87a", KIND_IMAGE),
    (b"GIF89a", KIND_IMAGE),
    (b"BM", KIND_IMAGE),                    # BMP
    (b"II*\x00", KIND_IMAGE),               # TIFF little-endian
    (b"MM\x00*", KIND_IMAGE),               # TIFF big-endian
    (b"PK\x03\x04", KIND_ZIP_OFFICE),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", KIND_LEGACY_OFFICE),
]

# HEIC/HEIF from iPhones: ftyp box at offset 4. Property staff photograph pallets with phones,
# so this is a real inbound format even though the June corpus happened to hold only JPEGs.
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1", b"avif"}

_OOXML_MARKERS = [
    (b"xl/workbook.xml", KIND_XLSX),
    (b"xl/", KIND_XLSX),
    (b"word/document.xml", KIND_DOCX),
    (b"word/", KIND_DOCX),
]

_HTML_RE = re.compile(rb"<\s*(html|body|table|div|p|meta)\b", re.IGNORECASE)

# The recurring Premier/Authority signature logo. Matching on size alone would be brittle, so we
# key on the content hash and only fall back to the heuristics below for unseen logos.
SIGNATURE_IMAGE_SHA256 = {
    # The Premier email-signature logo: 15,948 bytes, 30 copies across the 14 corpus messages,
    # arriving under three different names (image.png, image001.png, Outlook-https___ww.png).
    "284d570e7fe52f58f521bf27ac2cf2c81d6d5d6b48913ac6f2a7e19e581ceb4c",
}

# An image smaller than this is a logo, bullet, social icon or divider — never a photographed
# POD. The smallest genuine POD photo in the corpus is 2.6 MB; the largest decoration is 511 KB
# (an embedded screenshot in a signature block), so the gap is wide and the threshold is safe.
MIN_OCR_IMAGE_BYTES = 600 * 1024

_INLINE_NAME_RE = re.compile(
    r"^(image\d*\.(png|jpg|jpeg|gif|bmp)|outlook-[a-z0-9_]*\.(png|jpg|jpeg)|.*_?logo.*\.(png|jpg|jpeg))$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SniffResult:
    kind: str
    reason: str
    sha256: str

    @property
    def is_image(self) -> bool:
        return self.kind == KIND_IMAGE


def sniff(content_bytes: Optional[bytes], filename: str = "", content_type: str = "") -> SniffResult:
    """Classify by bytes. `filename`/`content_type` are used only to break ties the bytes
    genuinely cannot (an OOXML zip that is neither xl/ nor word/), never to override them."""
    if not content_bytes:
        return SniffResult(KIND_UNKNOWN, "empty content", "")

    digest = hashlib.sha256(content_bytes).hexdigest()
    head = content_bytes[:512]

    if len(content_bytes) >= 12 and content_bytes[4:8] == b"ftyp" and content_bytes[8:12] in _HEIF_BRANDS:
        return SniffResult(KIND_IMAGE, "heif/heic ftyp box", digest)

    for magic, kind in _MAGIC:
        if head.startswith(magic):
            if kind == KIND_ZIP_OFFICE:
                return SniffResult(_disambiguate_ooxml(content_bytes, filename), "ooxml zip", digest)
            if kind == KIND_LEGACY_OFFICE:
                return SniffResult(_disambiguate_ole2(content_bytes, filename), "ole2 compound file", digest)
            return SniffResult(kind, f"magic {magic[:8]!r}", digest)

    if _HTML_RE.search(head):
        return SniffResult(KIND_HTML, "html tag in first 512 bytes", digest)

    try:
        content_bytes[:4096].decode("utf-8")
        return SniffResult(KIND_TEXT, "decodes as utf-8", digest)
    except UnicodeDecodeError:
        return SniffResult(KIND_UNKNOWN, "no magic match, not utf-8 decodable", digest)


def _disambiguate_ooxml(content_bytes: bytes, filename: str) -> str:
    """xlsx/docx/pptx share the `PK\\x03\\x04` header. The zip central directory (last ~64 KB)
    names the parts, so scanning the tail tells us which one it is without unzipping."""
    tail = content_bytes[-65536:]
    for marker, kind in _OOXML_MARKERS:
        if marker in tail or marker in content_bytes[:65536]:
            return kind
    lowered = filename.lower()
    if lowered.endswith(".xlsx") or lowered.endswith(".xlsm"):
        return KIND_XLSX
    if lowered.endswith(".docx"):
        return KIND_DOCX
    return KIND_ZIP_OFFICE


def _disambiguate_ole2(content_bytes: bytes, filename: str) -> str:
    """OLE2 covers .msg, .doc and .xls. `__substg1.0_` streams are unique to Outlook items."""
    if b"__substg1.0_" in content_bytes[:65536] or filename.lower().endswith(".msg"):
        return KIND_MSG
    return KIND_LEGACY_OFFICE


def is_decorative_image(
    content_bytes: bytes,
    filename: str = "",
    sniffed: Optional[SniffResult] = None,
    referenced_cids: Optional[set] = None,
    content_id: Optional[str] = None,
) -> bool:
    """True for signature logos, social icons, bullets and other chrome that must never reach
    OCR. Errs toward keeping the image: a false 'decorative' would discard a photographed POD,
    which is the one artifact the property-delivery path cannot do without."""
    result = sniffed or sniff(content_bytes, filename)
    if not result.is_image:
        return False
    if result.sha256 in SIGNATURE_IMAGE_SHA256:
        return True
    if len(content_bytes) >= MIN_OCR_IMAGE_BYTES:
        return False   # big enough to be a real photo, whatever it is named
    if _INLINE_NAME_RE.match(filename.strip()):
        return True
    if referenced_cids and content_id and content_id.strip("<>") in referenced_cids:
        # cid-referenced images are rendered inside the body — decoration, not an enclosure
        return True
    return len(content_bytes) < 64 * 1024
