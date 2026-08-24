"""The dispatcher: every source reaches an adapter, and every source gets a verdict.

`run_adapters` in the orchestrator answers "what records did this produce". This module answers
the question that was never asked: **what happened to this attachment?** Every path through it
ends in exactly one `attachment_ledger` disposition, which is what makes the guarantee
"nothing is silently dropped" checkable rather than aspirational — `attachment_ledger.orphans()`
returns anything that slipped through.

Containers are handled before leaf adapters, and their members recurse through the same code, so
a POD inside a zip inside a forwarded message is parsed by exactly the adapter that would have
read it at the top level.
"""

import logging
import sqlite3
from typing import Callable, List, Optional, Sequence

from config import settings
from pipeline import attachment_ledger
from pipeline.models import Attachment, ExtractedRecord
from pipeline.parsing import integrity, sniff
from pipeline.stage3_extract import containers as container_mod
from pipeline.stage3_extract.base import ExtractionAdapter, ExtractionSource
from pipeline.stage3_extract.unsupported_adapter import UnsupportedFormatError

_logger = logging.getLogger(__name__)


def dispatch_source(
    conn: Optional[sqlite3.Connection],
    source: ExtractionSource,
    adapters: Sequence[ExtractionAdapter],
    *,
    containers: Optional[Sequence[container_mod.ContainerAdapter]] = None,
    budget: Optional[container_mod.Budget] = None,
    depth: int = 0,
    now: str = "",
    triage_category: Optional[str] = None,
) -> List[ExtractedRecord]:
    """Read one source, recursing into it if it is a container. Always records an outcome."""
    containers = container_mod.build_default_containers() if containers is None else containers
    budget = budget or container_mod.Budget.fresh()

    if depth > settings.MAX_CONTAINER_DEPTH:
        _record(conn, source, attachment_ledger.DROPPED_DEPTH, now,
                detail=f"nesting deeper than {settings.MAX_CONTAINER_DEPTH}")
        return []

    for container in containers:
        try:
            if not container.can_expand(source):
                continue
        except Exception:
            continue
        return _expand(conn, source, container, adapters, containers, budget, depth, now, triage_category)

    return _read_leaf(conn, source, adapters, now)


def _expand(
    conn, source, container, adapters, containers, budget, depth, now, triage_category,
) -> List[ExtractedRecord]:
    name = container.__class__.__name__
    try:
        children = container.expand(source, budget)
    except PermissionError as e:
        _record(conn, source, attachment_ledger.ENCRYPTED, now, claimed_by=name, detail=str(e))
        return []
    except Exception as e:
        state, detail = integrity.zip_state(source.content_bytes or b"")
        disposition = attachment_ledger.CORRUPT if state == integrity.CORRUPT else attachment_ledger.UNREADABLE
        _record(conn, source, disposition, now, claimed_by=name,
                detail=detail or f"{type(e).__name__}: {e}", error_type=type(e).__name__)
        return []

    _record(conn, source, attachment_ledger.CONTAINER_EXPANDED, now,
            claimed_by=name, detail=f"{len(children)} member(s)")

    records: List[ExtractedRecord] = []
    for ordinal, child in enumerate(children):
        if conn is not None:
            attachment_ledger.add_child(
                conn, source.ledger_id, source.source_email_id, child,
                depth + 1, ordinal, triage_category, now,
            )
        if child.drop_hint:
            continue   # add_child already gave it a terminal disposition
        records.extend(dispatch_source(
            conn, _child_source(source, child), adapters,
            containers=containers, budget=budget, depth=depth + 1, now=now,
            triage_category=triage_category,
        ))
    return records


def _child_source(parent: ExtractionSource, child: Attachment) -> ExtractionSource:
    """A container member inherits the parent's email identity and PO filter, so
    `_belongs_to_event` and the `source_email_id` join keep working however deep it sits."""
    return ExtractionSource(
        source_email_id=parent.source_email_id,
        email_date=parent.email_date,
        source_type="attachment",
        filename=child.filename,
        content_type=child.content_type,
        content_bytes=child.content_bytes,
        sender_address=parent.sender_address,
        subject=parent.subject,
        only_po=parent.only_po,
        ledger_id=child.ledger_id,
        container_path=child.container_path,
    )


def _read_leaf(conn, source, adapters, now) -> List[ExtractedRecord]:
    """Walk the cascade. The first adapter to claim *and* return records wins; an adapter that
    claims and finds nothing falls through to the next one."""
    claimed_by = None
    last_error = None

    for adapter in adapters:
        name = adapter.__class__.__name__
        try:
            if not adapter.can_handle(source):
                continue
            claimed_by = name
            records = adapter.extract(source)
            if records:
                _record(conn, source, attachment_ledger.EXTRACTED, now,
                        claimed_by=name, records=len(records))
                return records
        except UnsupportedFormatError as e:
            _record(conn, source, attachment_ledger.UNSUPPORTED_FORMAT, now,
                    claimed_by=name, detail=e.guidance)
            return []
        except PermissionError as e:
            _record(conn, source, attachment_ledger.ENCRYPTED, now, claimed_by=name, detail=str(e))
            return []
        except Exception as e:
            last_error = (name, e)
            _logger.warning("%s failed on %s (%s): %s: %s", name, source.source_email_id,
                            source.filename, type(e).__name__, e, exc_info=True)
            continue

    if claimed_by is None:
        _record(conn, source, attachment_ledger.NO_ADAPTER, now,
                detail=_no_adapter_detail(source))
        return []

    if last_error is not None:
        name, error = last_error
        disposition, detail = _classify_failure(source, error)
        _record(conn, source, disposition, now, claimed_by=name, detail=detail,
                error_type=type(error).__name__)
        return []

    _record(conn, source, attachment_ledger.EMPTY, now, claimed_by=claimed_by,
            detail="read cleanly, nothing extractable")
    return []


def _classify_failure(source: ExtractionSource, error: Exception) -> tuple:
    """Distinguish "we could not open this" from "it is locked" from "it is broken", so the
    operator is told which one it is."""
    data = source.content_bytes or b""
    kind = sniff.sniff(data, source.filename or "", source.content_type or "").kind

    if kind == sniff.KIND_PDF:
        state, detail = integrity.pdf_state(data)
    elif kind in (sniff.KIND_XLSX, sniff.KIND_DOCX, sniff.KIND_PPTX, sniff.KIND_ZIP_OFFICE):
        state, detail = integrity.office_state(data)
    else:
        state, detail = integrity.CORRUPT, f"{type(error).__name__}: {error}"

    if state == integrity.ENCRYPTED:
        return attachment_ledger.ENCRYPTED, integrity.describe(state) + f" ({detail})"
    if state == integrity.CORRUPT:
        return attachment_ledger.CORRUPT, integrity.describe(state) + f" ({detail})"
    return attachment_ledger.UNREADABLE, f"{type(error).__name__}: {error}"


def _no_adapter_detail(source: ExtractionSource) -> str:
    data = source.content_bytes or b""
    result = sniff.sniff(data, source.filename or "", source.content_type or "")
    return (f"no adapter claimed kind={result.kind} ({result.reason}); "
            f"first bytes {data[:16].hex()}")


def _record(
    conn,
    source: ExtractionSource,
    disposition: str,
    now: str,
    *,
    claimed_by: Optional[str] = None,
    records: int = 0,
    detail: str = "",
    error_type: Optional[str] = None,
) -> None:
    if conn is None or source.ledger_id is None:
        return
    attachment_ledger.record_outcome(
        conn, source.ledger_id, disposition, now,
        claimed_by=claimed_by, records_extracted=records, detail=detail, error_type=error_type,
        pod_document=source.pod_document,
    )
