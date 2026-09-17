"""A forwarded email's signature logo is chrome one level down, too.

`MsgContainerAdapter` has two paths and they did not agree. `_expand_msg` reuses
`MsgFileMailbox._collect_attachments` and inherits its decorative rule for free; `_expand_eml`
handed every part straight to `_child`, which classifies nothing.

That is not the rare path it looks like. **A Graph `itemAttachment` arrives as MIME, not as a
`.msg`**, whatever it is named — so a forwarded email, the commonest shape in this corpus, is
expanded by the path with no rule. The result on 2026-09-09: seven attachments refused by Azure
with `400 InvalidContentDimensions`, every one of them a nested signature graphic, including a
207-byte 32x32 icon and the `pollackweitznerlogo_emailsignature*.png` family. All seven carried
`depth=1` and a `container_path` ending `.msg!/`, which is this path's fingerprint.
"""

import io
import os

from pipeline.stage3_extract.base import ExtractionSource
from pipeline.stage3_extract.containers import Budget, MsgContainerAdapter


def png(width: int, height: int) -> bytes:
    from PIL import Image

    image = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def build_mime(parts) -> bytes:
    """`parts` is a list of `(filename, bytes, content_id)`; content_id may be None."""
    import base64

    lines = [
        b"From: sender@example-vendor.test",
        b"Subject: FW: PO 212696 delivered",
        b'Content-Type: multipart/related; boundary="b"',
        b"",
        b"--b",
        b'Content-Type: text/html; charset="utf-8"',
        b"",
        b'<html><body>Delivered.<img src="cid:logo@vendor"></body></html>',
    ]
    for filename, data, content_id in parts:
        lines += [
            b"--b",
            b'Content-Type: image/png; name="' + filename.encode() + b'"',
            b"Content-Transfer-Encoding: base64",
            b'Content-Disposition: attachment; filename="' + filename.encode() + b'"',
        ]
        if content_id:
            lines.append(b"Content-ID: <" + content_id.encode() + b">")
        lines += [b"", base64.encodebytes(data).replace(b"\n", b"\r\n").strip()]
    lines += [b"--b--", b""]
    return b"\r\n".join(lines)


def expand(raw: bytes):
    source = ExtractionSource(
        source_email_id="e1", email_date="2026-09-08T12:00:00Z",
        source_type="attachment", filename="forwarded.msg",
        content_type="application/octet-stream", content_bytes=raw, container_path="forwarded.msg")
    return MsgContainerAdapter().expand(source, Budget.fresh())


def test_a_nested_signature_logo_never_reaches_ocr():
    """The 207-byte icon, in its real shape: tiny, nested, and previously sent to Azure anyway."""
    children = expand(build_mime([("image003.png", png(32, 32), None)]))

    logo = next(c for c in children if c.filename == "image003.png")
    assert logo.drop_hint, "a 207-byte icon is chrome, and must not cost an OCR call"
    assert "decorative" in logo.drop_hint


def test_a_body_referenced_banner_is_dropped_on_the_cid_rule_like_a_top_level_one():
    """Rule 2 of `classify_image` needs the body's `cid:` set, which this path never collected.

    Without it a 20 KB logo is over the tiny floor, unreferenced as far as the classifier can see,
    and therefore "evidence" — which is exactly how `pollackweitznerlogo_emailsignature*.png`
    reached the OCR queue.
    """
    children = expand(build_mime([("logo_emailsignature.png", png(90, 60), "logo@vendor")]))

    logo = next(c for c in children if c.filename == "logo_emailsignature.png")
    assert logo.drop_hint and "decorative" in logo.drop_hint
    assert logo.is_inline is True


def test_a_pasted_photograph_is_still_kept():
    """The asymmetry that governs this whole classifier: keeping a logo costs one wasted OCR call,
    discarding a photograph loses the only proof of delivery that exists.

    Large, and not drawn by the body — so it is evidence, and it survives.
    """
    children = expand(build_mime([("image001.png", png(900, 700), None)]))

    photo = next(c for c in children if c.filename == "image001.png")
    assert photo.drop_hint is None
    assert photo.content_bytes, "an evidence attachment keeps its bytes"


def test_the_same_logo_quoted_in_every_hop_is_read_once():
    """A thread quoted four deep carries four copies of the same banner. Content-hash dedupe is
    what both other connectors apply, and this path applied none."""
    banner = png(400, 300)
    children = expand(build_mime([
        ("image004.png", banner, None),
        ("image008.png", banner, None),
        ("image012.png", banner, None),
    ]))

    images = [c for c in children if c.filename.startswith("image")]
    kept = [c for c in images if c.drop_hint is None]
    assert len(kept) == 1, "one read, two recorded as duplicates"
    assert all("duplicate" in c.drop_hint for c in images if c.drop_hint)
