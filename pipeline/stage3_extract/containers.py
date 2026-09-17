"""Containers — attachments that hold other attachments.

Three of them arrive in real mail: a forwarded `.msg`, an `.eml`, and a `.zip`. None was ever
opened. `KIND_MSG` is the sharpest case, because `stage1_triage._READABLE_KINDS` already lists
it as readable — so an email whose only evidence is an attached `.msg` was held as if we could
read it, and then nothing read it.

A container does not produce records itself. It produces **child sources**, which the dispatcher
runs back through the same adapter cascade, so a POD three levels down is parsed by exactly the
code that would have parsed it at the top level.

Every guard is checked against metadata *before* any member is decompressed, and the byte budget
is shared across the whole recursion tree, so a nested archive cannot reset it. That is what
makes a zip bomb a refused attachment rather than an out-of-memory kill.
"""

import io
import zipfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

from config import settings
from pipeline import attachment_ledger
from pipeline.models import Attachment
from pipeline.parsing import integrity, sniff
from pipeline.stage3_extract.base import ExtractionSource


@dataclass
class Budget:
    """Shared across one email's entire container tree — never reset by a nested container."""
    bytes_remaining: int
    members_remaining: int

    @classmethod
    def fresh(cls) -> "Budget":
        return cls(
            bytes_remaining=settings.MAX_EXPANDED_BYTES,
            members_remaining=settings.MAX_CONTAINER_MEMBERS_TOTAL,
        )

    def take(self, size: int) -> bool:
        if size > self.bytes_remaining or self.members_remaining <= 0:
            return False
        self.bytes_remaining -= size
        self.members_remaining -= 1
        return True


class ContainerAdapter(ABC):
    """Expands one source into child sources. Distinct from `ExtractionAdapter`, which produces
    records — a container yields more things to read, not readings."""

    @abstractmethod
    def can_expand(self, source: ExtractionSource) -> bool: ...

    @abstractmethod
    def expand(self, source: ExtractionSource, budget: Budget) -> List[Attachment]: ...


def _child(filename: str, data: bytes, parent_path: str, drop_hint: Optional[str] = None) -> Attachment:
    result = sniff.sniff(data, filename)
    return Attachment(
        filename=filename,
        content_type="application/octet-stream",
        content_bytes=data if drop_hint is None else b"",
        sha256=result.sha256,
        size_bytes=len(data),
        drop_hint=drop_hint,
        container_path=f"{parent_path}!/{filename}" if parent_path else filename,
    )


class ZipContainerAdapter(ContainerAdapter):
    """`.zip`. OOXML files are zips too, so they are excluded explicitly — a `.xlsx` must reach
    `ExcelAdapter`, not be exploded into its XML parts."""

    EXPANDABLE_KINDS = {sniff.KIND_ZIP_OFFICE}

    def can_expand(self, source: ExtractionSource) -> bool:
        if source.source_type != "attachment" or not source.content_bytes:
            return False
        return sniff.sniff(source.content_bytes, source.filename or "",
                           source.content_type or "").kind in self.EXPANDABLE_KINDS

    def expand(self, source: ExtractionSource, budget: Budget) -> List[Attachment]:
        state, detail = integrity.zip_state(source.content_bytes)
        if state == integrity.ENCRYPTED:
            raise PermissionError(f"encrypted archive: {detail}")
        if state == integrity.CORRUPT:
            raise ValueError(f"corrupt archive: {detail}")

        parent_path = source.container_path or source.filename or "archive.zip"
        children: List[Attachment] = []

        with zipfile.ZipFile(io.BytesIO(source.content_bytes)) as archive:
            members = [info for info in archive.infolist() if not info.is_dir()]

            if len(members) > settings.MAX_CONTAINER_MEMBERS:
                return [_child(parent_path, b"", "",
                               f"count:{len(members)} members exceeds {settings.MAX_CONTAINER_MEMBERS}")]

            for info in members:
                # Guards use the declared metadata, so nothing is decompressed to find out it
                # was too big — which is the whole point of a compression-ratio check.
                if info.file_size > settings.MAX_ATTACHMENT_BYTES:
                    children.append(_child(info.filename, b"", parent_path,
                                           f"oversize:{info.file_size} bytes"))
                    continue
                ratio = info.file_size / max(info.compress_size, 1)
                if ratio > settings.MAX_ZIP_COMPRESSION_RATIO:
                    children.append(_child(info.filename, b"", parent_path,
                                           f"oversize:compression ratio {ratio:.0f}:1"))
                    continue
                if not budget.take(info.file_size):
                    children.append(_child(info.filename, b"", parent_path,
                                           "oversize:expansion budget exhausted"))
                    continue
                try:
                    children.append(_child(info.filename, archive.read(info), parent_path))
                except Exception as e:
                    children.append(_child(info.filename, b"", parent_path,
                                           f"corrupt:{type(e).__name__}"))
        return children


class MsgContainerAdapter(ContainerAdapter):
    """A `.msg` or `.eml` arriving as an attachment.

    `MsgFileMailbox` already unwraps Outlook items it reads from disk, but a `.msg` delivered as
    raw bytes — which is how Graph hands over an `itemAttachment` — reached nothing at all.
    """

    def can_expand(self, source: ExtractionSource) -> bool:
        if source.source_type != "attachment" or not source.content_bytes:
            return False
        return sniff.sniff(source.content_bytes, source.filename or "",
                           source.content_type or "").kind == sniff.KIND_MSG

    def expand(self, source: ExtractionSource, budget: Budget) -> List[Attachment]:
        if sniff.is_eml(source.content_bytes):
            return self._expand_eml(source, budget)
        return self._expand_msg(source, budget)

    def _expand_msg(self, source: ExtractionSource, budget: Budget) -> List[Attachment]:
        import extract_msg

        from connectors.msg_file import MsgFileMailbox, _html_of

        parent_path = source.container_path or source.filename or "message.msg"
        message = extract_msg.openMsg(io.BytesIO(source.content_bytes))
        try:
            children: List[Attachment] = []
            html = _html_of(message)
            subject = (getattr(message, "subject", "") or "nested message").replace("\x00", "").strip()
            if html:
                children.append(_child(f"{subject[:80]}.html", html.encode("utf-8"), parent_path))
            elif getattr(message, "body", None):
                children.append(_child(f"{subject[:80]}.txt",
                                       message.body.encode("utf-8", "replace"), parent_path))

            # Reuse the connector's own traversal so an attached message is unwrapped exactly
            # like one read from disk — same decorative rule, same dedupe, same drop hints.
            reader = MsgFileMailbox(folder=".", read_only=True)
            for attachment in reader._collect_attachments(message, depth=0, container_path=parent_path):
                if attachment.drop_hint is None and not budget.take(attachment.size_bytes or 0):
                    attachment.drop_hint = "oversize:expansion budget exhausted"
                    attachment.content_bytes = b""
                children.append(attachment)
            return children
        finally:
            message.close()

    def _expand_eml(self, source: ExtractionSource, budget: Budget) -> List[Attachment]:
        """A MIME message's parts, with the same decorative rule the other two paths apply.

        This path is not the rare one it looks like. A Graph `itemAttachment` arrives as **MIME,
        not as a `.msg`** whatever it is named, so a forwarded email — the commonest shape in this
        corpus — is expanded here rather than by `_expand_msg`.

        It used to hand every part straight to `_child`, which classifies nothing. So a signature
        logo nested one level down was never marked decorative, went to OCR, and came back
        `400 InvalidContentDimensions` or `InvalidContent` — the whole
        `pollackweitznerlogo_emailsignature*.png` family, and a 207-byte 32x32 icon that Azure will
        not even look at. `_expand_msg` reuses `MsgFileMailbox._collect_attachments` and gets the
        rule for free; here it has to be applied explicitly, and it is applied with the same inputs
        so a nested logo is judged exactly as a top-level one is.
        """
        import email as email_lib
        from email import policy

        parent_path = source.container_path or source.filename or "message.eml"
        message = email_lib.message_from_bytes(source.content_bytes, policy=policy.default)
        children: List[Attachment] = []

        # The cids the body actually draws, which is what separates a rendered signature logo from
        # a photograph someone pasted in. Collected before the walk so rule 2 of `classify_image`
        # has it for every part, whatever order the parts arrive in.
        body_cids = set()
        for part in message.walk():
            if part.get_content_type() in ("text/html", "text/plain"):
                payload = part.get_payload(decode=True)
                if payload:
                    body_cids |= sniff.referenced_cids(
                        payload.decode(part.get_content_charset() or "utf-8", "replace"))

        seen_digests: dict = {}
        for index, part in enumerate(message.walk()):
            if part.get_content_maintype() == "multipart":
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            name = part.get_filename() or f"part{index}.{_extension_for(part.get_content_type())}"
            if not budget.take(len(payload)):
                children.append(_child(name, b"", parent_path, "oversize:expansion budget exhausted"))
                continue

            content_id = (part.get("Content-ID") or "").strip()
            result = sniff.sniff(payload, name, part.get_content_type() or "")
            drop_hint = None
            verdict = sniff.classify_image(payload, name, result, body_cids, content_id)
            if verdict.decorative:
                drop_hint = f"decorative:{verdict.certainty} — {verdict.reason}"
            elif result.sha256 in seen_digests:
                # Same content-hash dedupe both other connectors apply, so a nested message whose
                # logo appears in every quoted hop is read once rather than once per hop.
                drop_hint = attachment_ledger.duplicate_drop_hint(
                    name, seen_digests[result.sha256], result.sha256)
            else:
                seen_digests[result.sha256] = name

            child = _child(name, payload, parent_path, drop_hint)
            child.content_id = content_id or None
            child.is_inline = bool(content_id and content_id.strip("<>") in body_cids)
            children.append(child)
        return children


def _extension_for(content_type: str) -> str:
    return {
        "text/html": "html", "text/plain": "txt", "application/pdf": "pdf",
    }.get(content_type, "bin")


def build_default_containers() -> List[ContainerAdapter]:
    return [MsgContainerAdapter(), ZipContainerAdapter()]
