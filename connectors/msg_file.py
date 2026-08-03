"""Outlook `.msg` intake.

Premier's real samples — and the way their staff hand work over internally — are `.msg` files.
Until this connector existed the pipeline could only be fed by the Graph API or by hand-written
JSON fixtures (docs/CODE-ANALYSIS-FINDINGS.md, "verdict"), which meant nothing had ever been run
against a real message.

It implements the same `Mailbox` interface as `GraphMailbox`, so the orchestrator cannot tell
them apart. That makes the June corpus a regression suite for the production code path rather
than for a parallel test-only path.

Three things the corpus forced:

* **Nested messages.** `5star Fabric Verification Response.msg` carries an attached Delivered
  Notification `.msg`, which carries the two FedEx POD PDFs. Losing the recursion loses the PODs.
* **Null-terminated filenames.** `extract_msg` hands back `"image.png\\x00"`; left alone, the
  null propagates into filenames, logs and JSON.
* **Missing MIME types.** `Cameo Receivers.xlsx` and the phone photos arrive with
  `mimetype=None`, so the content type is filled in from a byte sniff instead.
"""

import hashlib
from pathlib import Path
from typing import List, Optional

import extract_msg

from connectors.mailbox import Mailbox
from pipeline.models import Attachment, RawEmail
from pipeline.parsing import sniff

# Nesting is shallow in practice (a forward of a notification that has attachments). The cap is
# a loop guard against a malformed or maliciously self-referential item, not a real limit.
MAX_NESTING_DEPTH = 6

_CONTENT_TYPE_BY_KIND = {
    sniff.KIND_PDF: "application/pdf",
    sniff.KIND_XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    sniff.KIND_DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    sniff.KIND_MSG: "application/vnd.ms-outlook",
    sniff.KIND_HTML: "text/html",
    sniff.KIND_TEXT: "text/plain",
}


def _clean_name(raw: Optional[str]) -> str:
    """extract_msg returns MAPI strings with their trailing NUL still attached."""
    return (raw or "").replace("\x00", "").strip()


def _content_type_for(data: bytes, filename: str, declared: Optional[str]) -> str:
    result = sniff.sniff(data, filename)
    if result.kind == sniff.KIND_IMAGE:
        # Keep the declared subtype when we have one — image/png vs image/jpeg matters to OCR.
        return declared or ("image/jpeg" if data[:3] == b"\xff\xd8\xff" else "image/png")
    return _CONTENT_TYPE_BY_KIND.get(result.kind) or declared or "application/octet-stream"


class MsgFileMailbox(Mailbox):
    """Reads `*.msg` files from a folder as if they were an inbox.

    `email_id` is the message's RFC `Message-ID` when present, falling back to a hash of the
    file bytes. Both are stable across re-runs, which is what makes ingestion idempotent — and
    unlike the Graph connector's folder-scoped `id`, neither changes when the item is moved
    (finding C5).
    """

    def __init__(
        self,
        folder: Path,
        pattern: str = "*.msg",
        keep_decorative_images: bool = False,
        read_only: bool = False,
    ):
        self.folder = Path(folder)
        self.pattern = pattern
        self.keep_decorative_images = keep_decorative_images
        self.read_only = read_only
        """When set, `mark_processed` logs the folder it *would* have moved to and changes
        nothing on disk. Regression runs point this connector at Premier's original sample
        folder; without the guard, one run would scatter their source data across four
        subdirectories."""
        self._path_by_email_id: dict = {}

    # --- Mailbox interface ---------------------------------------------------

    def fetch_new(self) -> List[RawEmail]:
        """One `RawEmail` per file. A file that fails to parse is skipped with a log line and
        never aborts the batch — one corrupt item must not cost us the other thirteen
        (finding C8)."""
        emails: List[RawEmail] = []
        for path in sorted(self.folder.glob(self.pattern)):
            try:
                emails.append(self.read_file(path))
            except Exception as e:
                print(f"[msg_file] failed to parse {path.name}: {type(e).__name__}: {e}")
        return emails

    def mark_processed(self, email_id: str, folder: str) -> None:
        """Move the source file into a sibling folder, mirroring the Graph connector's folder
        routing. Looks the path up by the id we handed out, rather than assuming the filename
        equals the id — that assumption is why `LocalFolderMailbox` silently no-ops (finding C2)."""
        if self.read_only:
            self.routed_to = getattr(self, "routed_to", {})
            self.routed_to[email_id] = folder
            return
        source = self._path_by_email_id.get(email_id)
        if not source or not Path(source).exists():
            print(f"[msg_file] mark_processed: no source file tracked for {email_id}")
            return
        target_dir = self.folder / folder
        target_dir.mkdir(parents=True, exist_ok=True)
        Path(source).rename(target_dir / Path(source).name)
        self._path_by_email_id[email_id] = str(target_dir / Path(source).name)

    # --- Parsing -------------------------------------------------------------

    def read_file(self, path: Path) -> RawEmail:
        message = extract_msg.Message(str(path))
        try:
            email = self._to_raw_email(message, fallback_id_source=path.read_bytes())
        finally:
            message.close()
        self._path_by_email_id[email.email_id] = str(path)
        return email

    def _to_raw_email(self, message, fallback_id_source: bytes) -> RawEmail:
        sender = _clean_name(getattr(message, "sender", "")) or _clean_name(getattr(message, "senderEmailAddress", ""))
        sender_address = _extract_address(sender)
        message_id = _clean_name(getattr(message, "messageId", ""))
        email_id = message_id or "sha256:" + hashlib.sha256(fallback_id_source).hexdigest()[:32]

        html_body = getattr(message, "htmlBody", None)
        if isinstance(html_body, bytes):
            html_body = html_body.decode("utf-8", "replace")
        text_body = getattr(message, "body", None) or None

        received_at = ""
        date = getattr(message, "date", None)
        if date is not None:
            received_at = date.isoformat() if hasattr(date, "isoformat") else str(date)

        attachments = self._collect_attachments(message, depth=0)

        return RawEmail(
            email_id=email_id,
            received_at=received_at,
            sender_address=sender_address,
            sender_domain=sender_address.split("@")[-1].lower() if "@" in sender_address else "",
            subject=_clean_name(getattr(message, "subject", "")),
            body_html=html_body or None,
            body_text=text_body,
            attachments=attachments,
        )

    def _collect_attachments(self, message, depth: int) -> List[Attachment]:
        """Flatten the attachment tree into one list.

        A nested `.msg` contributes both its own body (as an `.html` pseudo-attachment, so the
        HTML adapter can read it exactly like any other body) and its own attachments. Flattening
        rather than nesting keeps `RawEmail` a flat record while losing nothing — the Delivered
        Notification quoted inside the 5-Star thread still reaches the Authority parser.
        """
        if depth > MAX_NESTING_DEPTH:
            print(f"[msg_file] nesting deeper than {MAX_NESTING_DEPTH} — stopping recursion")
            return []

        collected: List[Attachment] = []
        seen_digests = set()

        for raw_attachment in message.attachments:
            filename = _clean_name(getattr(raw_attachment, "longFilename", None)
                                   or getattr(raw_attachment, "shortFilename", None)) or "unnamed"
            declared_type = _clean_name(getattr(raw_attachment, "mimetype", None)) or None
            data = raw_attachment.data

            if hasattr(data, "attachments"):   # a nested Outlook item
                nested_subject = _clean_name(getattr(data, "subject", "")) or "nested message"
                nested_html = getattr(data, "htmlBody", None)
                if isinstance(nested_html, bytes):
                    nested_html = nested_html.decode("utf-8", "replace")
                if nested_html:
                    collected.append(Attachment(
                        filename=f"{nested_subject[:80]}.html",
                        content_type="text/html",
                        content_bytes=nested_html.encode("utf-8"),
                    ))
                collected.extend(self._collect_attachments(data, depth + 1))
                continue

            if not isinstance(data, bytes) or not data:
                continue

            result = sniff.sniff(data, filename, declared_type or "")
            if not self.keep_decorative_images and sniff.is_decorative_image(data, filename, result):
                continue
            # Content-hash dedupe. It collapses the corpus's two identically-named-differently
            # PODs into one — the right outcome, since they are the same document, and it also
            # stops the same logo being carried thirty times.
            if result.sha256 in seen_digests:
                continue
            seen_digests.add(result.sha256)

            collected.append(Attachment(
                filename=filename,
                content_type=_content_type_for(data, filename, declared_type),
                content_bytes=data,
            ))

        return collected


def _extract_address(raw: str) -> str:
    """`'"Gutierrez, Maria" <MariaGutierrez@premierpm.com>'` -> the address, lowercased."""
    if not raw:
        return ""
    if "<" in raw and ">" in raw:
        raw = raw[raw.rfind("<") + 1: raw.rfind(">")]
    return raw.strip().lower()
