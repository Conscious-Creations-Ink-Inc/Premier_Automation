"""Vendor-specific parsers.

A vendor parser knows one sender's exact document grammar and reads it deterministically —
no fuzzy matching, no AI, no guessing. It is always tried before the generic adapters, because
a format we recognise outright yields strictly more than the generic path can: line numbers,
carrier, tracking, received-by, and the item-versus-package quantity distinction.

Authority Logistics is the whole of Phase 1's structured volume. Everything else (property
threads, vendor confirmations, PODs) falls through to the generic adapters in `stage3_extract/`.
"""

from pipeline.vendors.authority import (
    AuthorityNotice,
    NoticeKind,
    parse_authority_notice,
    records_from_notice,
)

__all__ = ["AuthorityNotice", "NoticeKind", "parse_authority_notice", "records_from_notice"]
