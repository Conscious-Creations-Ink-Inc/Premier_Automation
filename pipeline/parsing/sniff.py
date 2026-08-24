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
KIND_ZIP_OFFICE = "zip_office"   # a plain zip, or an OOXML we couldn't narrow further
KIND_XLSX = "xlsx"
KIND_DOCX = "docx"
KIND_PPTX = "pptx"
KIND_LEGACY_OFFICE = "legacy_office"   # OLE2 we couldn't narrow further
KIND_XLS = "xls"
KIND_DOC = "doc"
KIND_MSG = "msg"
KIND_IMAGE = "image"
KIND_HTML = "html"
KIND_TEXT = "text"
KIND_ARCHIVE = "archive"                           # tar/gz — expandable with the stdlib
KIND_ARCHIVE_UNSUPPORTED = "archive_unsupported"   # 7z/rar — recognised, no reader
KIND_UNKNOWN = "unknown"

ALL_KINDS = frozenset({
    KIND_PDF, KIND_ZIP_OFFICE, KIND_XLSX, KIND_DOCX, KIND_PPTX, KIND_LEGACY_OFFICE,
    KIND_XLS, KIND_DOC, KIND_MSG, KIND_IMAGE, KIND_HTML, KIND_TEXT, KIND_ARCHIVE,
    KIND_ARCHIVE_UNSUPPORTED, KIND_UNKNOWN,
})
"""Every kind the sniffer can return. `tests/test_attachment_universality.py` asserts each one
has a fixture and reaches a terminal disposition, so adding a kind without deciding what reads
it fails the suite rather than silently dropping mail."""

_MAGIC = [
    (b"%PDF-", KIND_PDF),
    (b"\x89PNG\r\n\x1a\n", KIND_IMAGE),
    (b"\xff\xd8\xff", KIND_IMAGE),          # JPEG (all APPn variants)
    (b"GIF87a", KIND_IMAGE),
    (b"GIF89a", KIND_IMAGE),
    (b"II*\x00", KIND_IMAGE),               # TIFF little-endian
    (b"MM\x00*", KIND_IMAGE),               # TIFF big-endian
    (b"PK\x03\x04", KIND_ZIP_OFFICE),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", KIND_LEGACY_OFFICE),
    (b"7z\xbc\xaf\x27\x1c", KIND_ARCHIVE_UNSUPPORTED),
    (b"Rar!\x1a\x07", KIND_ARCHIVE_UNSUPPORTED),
    (b"\x1f\x8b", KIND_ARCHIVE),            # gzip, including .tar.gz
]


def _is_bmp(content_bytes: bytes) -> bool:
    """BMP's magic is the two ASCII letters "BM" — which any text beginning "BMW invoice…" also
    matches, and which would route that text to OCR as an image.

    Three structural cross-checks make the header unambiguous: bytes 6-10 are reserved and must
    be zero (in "BMW invoice for…" they read "voic"), the pixel-data offset must clear the
    14-byte file header, and the declared file size must match the bytes we actually hold.
    """
    if not content_bytes.startswith(b"BM") or len(content_bytes) < 14:
        return False
    if content_bytes[6:10] != b"\x00\x00\x00\x00":
        return False
    declared_size = int.from_bytes(content_bytes[2:6], "little")
    pixel_offset = int.from_bytes(content_bytes[10:14], "little")
    if not 14 <= pixel_offset <= max(declared_size, len(content_bytes)):
        return False
    return abs(declared_size - len(content_bytes)) <= 4 or declared_size == 0


# HEIC/HEIF from iPhones: ftyp box at offset 4. Property staff photograph pallets with phones,
# so this is a real inbound format even though the June corpus happened to hold only JPEGs.
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1", b"avif"}

_OOXML_MARKERS = [
    (b"xl/workbook.xml", KIND_XLSX),
    (b"xl/", KIND_XLSX),
    (b"word/document.xml", KIND_DOCX),
    (b"word/", KIND_DOCX),
    (b"ppt/presentation.xml", KIND_PPTX),
    (b"ppt/", KIND_PPTX),
]

# SVG is XML, so it must be tested before the HTML pattern — an `<svg>` containing a `<p>` would
# otherwise read as an HTML body and be searched for tables that cannot be there.
_SVG_RE = re.compile(rb"<\s*svg[\s>]", re.IGNORECASE)
_HTML_RE = re.compile(rb"<\s*(html|body|table|div|p|meta)\b", re.IGNORECASE)

# RFC-822 headers, for telling raw .eml bytes apart from ordinary text.
_EML_RE = re.compile(
    rb"^(From|To|Subject|Date|Message-ID|MIME-Version|Received|Return-Path):",
    re.IGNORECASE | re.MULTILINE,
)

_CID_SRC_RE = re.compile(r"""src\s*=\s*['"]?cid:([^'"\s>]+)""", re.IGNORECASE)


def referenced_cids(html: Optional[str]) -> set:
    """Content-IDs the body actually renders inline, via `src="cid:..."`.

    This is the signal that separates a signature logo from a photograph someone attached: the
    logo is referenced by the HTML and drawn in place, the photograph is not. `is_decorative_image`
    has always accepted this argument and never once been passed it, which is why a filename
    heuristic had to stand in for it — and why it discarded real PODs.
    """
    if not html:
        return set()
    return {match.group(1).strip("<>") for match in _CID_SRC_RE.finditer(html)}


# A header block ends at the first blank line, and nothing after it is a header. Scanning only
# that far is what keeps a body quoting "From: someone" from reading as an RFC-822 envelope.
MAX_HEADER_BLOCK_BYTES = 64 * 1024


def _header_block(content_bytes: bytes) -> bytes:
    """The candidate header block: everything up to the first blank line, capped.

    The cap matters as much as the terminator. Graph hands over an `itemAttachment` as MIME, and
    Exchange prepends a `Received:` chain to it — on the real Delivered Notification the block runs
    to 8,087 bytes and the first 2,255 are *nothing but* `Received:` continuation lines, so
    `Content-Type`, `Date`, `From`, `Message-ID` and `Subject` all sit past the 2 KB this used to
    look at. That is what made the canonical POD carrier in Premier's mail read as plain text.
    """
    window = content_bytes[:MAX_HEADER_BLOCK_BYTES]
    for terminator in (b"\r\n\r\n", b"\n\n"):
        end = window.find(terminator)
        if end != -1:
            return window[:end]
    return window


def is_eml(content_bytes: bytes) -> bool:
    """True for raw RFC-822 message bytes — two or more distinct headers in the header block.

    A lone `Received:` also counts, because that is a header no ordinary text file carries and
    Exchange can emit thousands of bytes of them before anything else appears.
    """
    names = {m.group(1).lower() for m in _EML_RE.finditer(_header_block(content_bytes))}
    return len(names) >= 2 or names == {b"received"}

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

    # WEBP is a RIFF container; the form type at offset 8 is what distinguishes it from WAV.
    # Outlook on the web converts pasted images to WEBP, so this is a live inbound format.
    if head.startswith(b"RIFF") and len(content_bytes) >= 12 and content_bytes[8:12] == b"WEBP":
        return SniffResult(KIND_IMAGE, "riff/webp", digest)

    if _is_bmp(head):
        return SniffResult(KIND_IMAGE, "bmp header", digest)

    for magic, kind in _MAGIC:
        if head.startswith(magic):
            if kind == KIND_ZIP_OFFICE:
                return SniffResult(_disambiguate_ooxml(content_bytes, filename), "ooxml zip", digest)
            if kind == KIND_LEGACY_OFFICE:
                return SniffResult(_disambiguate_ole2(content_bytes, filename), "ole2 compound file", digest)
            return SniffResult(kind, f"magic {magic[:8]!r}", digest)

    # An RFC-822 message, before every text-shaped gate below it. Graph returns an
    # `itemAttachment` as MIME with `contentType: message/rfc822` — never as an OLE `.msg`, which
    # the magic table above would already have caught — and `connectors.mailbox` names those `.msg`
    # regardless. All three signals therefore have to be honoured here or a forwarded delivery
    # notification falls through to `TextAdapter` and reports itself empty.
    lowered_name = filename.lower()
    if (is_eml(content_bytes)
            or (content_type or "").lower().startswith("message/rfc822")
            or lowered_name.endswith((".eml", ".msg", ".mht", ".mhtml"))):
        return SniffResult(KIND_MSG, "rfc822 message", digest)

    # SVG before HTML: an <svg> containing a <p> would otherwise be read as an HTML body.
    if _SVG_RE.search(head):
        return SniffResult(KIND_IMAGE, "svg root element", digest)

    if _HTML_RE.search(head):
        return SniffResult(KIND_HTML, "html tag in first 512 bytes", digest)

    try:
        content_bytes[:4096].decode("utf-8")
    except UnicodeDecodeError:
        return SniffResult(KIND_UNKNOWN, "no magic match, not utf-8 decodable", digest)

    # The RFC-822 test that used to live here now runs above the HTML gate, because a message is
    # not merely a text shape — it is a container, and reaching this line at all means it is not.
    return SniffResult(KIND_TEXT, "decodes as utf-8", digest)


def _disambiguate_ooxml(content_bytes: bytes, filename: str) -> str:
    """xlsx/docx/pptx share the `PK\\x03\\x04` header. The zip central directory (last ~64 KB)
    names the parts, so scanning the tail tells us which one it is without unzipping."""
    tail = content_bytes[-65536:]
    for marker, kind in _OOXML_MARKERS:
        if marker in tail or marker in content_bytes[:65536]:
            return kind
    lowered = filename.lower()
    if lowered.endswith((".xlsx", ".xlsm")):
        return KIND_XLSX
    if lowered.endswith(".docx"):
        return KIND_DOCX
    if lowered.endswith((".pptx", ".ppsx")):
        return KIND_PPTX
    return KIND_ZIP_OFFICE


def _disambiguate_ole2(content_bytes: bytes, filename: str) -> str:
    """OLE2 covers .msg, .doc and .xls, and each is read by a different library — so guessing
    wrong here means the attachment reaches no adapter at all.

    Each format names its own primary stream, and those names appear as UTF-16 in the OLE
    directory: `__substg1.0_` for an Outlook item, `Workbook`/`Book` for Excel, `WordDocument`
    for Word. Reading the directory with `olefile` would be cleaner, but a substring test over
    the first 64 KB is enough, costs nothing, and cannot raise on a truncated file.
    """
    head = content_bytes[:65536]
    lowered = filename.lower()

    if b"__substg1.0_" in head or lowered.endswith(".msg"):
        return KIND_MSG
    if b"W\x00o\x00r\x00d\x00D\x00o\x00c\x00u\x00m\x00e\x00n\x00t" in head or lowered.endswith(".doc"):
        return KIND_DOC
    if (b"W\x00o\x00r\x00k\x00b\x00o\x00o\x00k" in head
            or b"B\x00o\x00o\x00k" in head
            or lowered.endswith((".xls", ".xlt"))):
        return KIND_XLS
    return KIND_LEGACY_OFFICE


# Below this, an image is a bullet, divider, social icon or spacer — never a photograph. The
# smallest image any phone or camera produces is comfortably above it, including after Outlook's
# "resize large images on send" downscale.
TINY_IMAGE_BYTES = 16 * 1024

# The ceiling for treating a body-referenced (`cid:`) image as decoration. Being referenced by
# the body is *not* sufficient on its own: Outlook assigns a cid to an image a person pastes
# straight into the message, which is exactly how someone sends a photo of a pallet or a BOL.
# Every signature logo in the corpus is under 40 KB, while the inline images that could be real
# evidence run from 135 KB to 511 KB — so the cut sits between them, and anything larger is kept
# even though it is inline.
MAX_DECORATIVE_CID_BYTES = 64 * 1024


@dataclass(frozen=True)
class ImageVerdict:
    decorative: bool
    reason: str      # human-readable; goes straight into the attachment ledger
    certainty: str   # "known_hash" | "cid_referenced" | "tiny" | "kept"


def classify_image(
    content_bytes: bytes,
    filename: str = "",
    sniffed: Optional[SniffResult] = None,
    referenced_cids: Optional[set] = None,
    content_id: Optional[str] = None,
) -> ImageVerdict:
    """Decide whether an image is chrome or evidence.

    The rules are narrow on purpose, because the two errors are not symmetrical: keeping a logo
    costs one wasted OCR call, while discarding a photograph loses the only proof of delivery
    that exists. The previous rule discarded *any* image under 64 KB, and any image under 600 KB
    whose name matched `image\\d+\\.jpg` — which is precisely how Outlook names an inline-pasted,
    auto-downscaled photo. A real POD could vanish with no record.

    What replaces it, in order:

    1. **Known signature hash** — certain, and the corpus's 30 logo copies all match it.
    2. **Referenced by the body via `cid:` and small.** Being body-referenced is not enough on
       its own — Outlook gives a cid to an image pasted straight into the message, which is how
       people send a photo of a pallet or a BOL. Every signature logo in the corpus is under
       40 KB; the inline images that could be evidence run 135-511 KB. Hence the size ceiling.
    3. **Tiny** — under 16 KB, below anything a camera emits.

    Filename is no longer a test on its own; it only breaks ties inside rule 2.
    """
    result = sniffed or sniff(content_bytes, filename)
    if not result.is_image:
        return ImageVerdict(False, "not an image", "kept")

    if result.sha256 in SIGNATURE_IMAGE_SHA256:
        return ImageVerdict(True, "known signature-logo hash", "known_hash")

    size = len(content_bytes)
    cid = (content_id or "").strip("<>")
    if cid and referenced_cids and cid in referenced_cids and size < MAX_DECORATIVE_CID_BYTES:
        looks_inline = bool(_INLINE_NAME_RE.match(filename.strip()))
        return ImageVerdict(
            True,
            f"rendered inline in the body via cid:{cid}" + (" with an Outlook inline name" if looks_inline else ""),
            "cid_referenced",
        )

    if size < TINY_IMAGE_BYTES:
        return ImageVerdict(True, f"{size} bytes — below the {TINY_IMAGE_BYTES}-byte floor", "tiny")

    return ImageVerdict(False, f"{size} bytes, not body-referenced — treated as evidence", "kept")


def is_decorative_image(
    content_bytes: bytes,
    filename: str = "",
    sniffed: Optional[SniffResult] = None,
    referenced_cids: Optional[set] = None,
    content_id: Optional[str] = None,
) -> bool:
    """Boolean shim over `classify_image`, kept so existing callers need no change."""
    return classify_image(content_bytes, filename, sniffed, referenced_cids, content_id).decorative
