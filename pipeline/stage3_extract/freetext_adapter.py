from typing import List

from config import settings
from pipeline.models import ExtractedRecord
from pipeline.parsing import tokens
from pipeline.stage3_extract import ai_fallback
from pipeline.stage3_extract.base import (
    ExtractionAdapter,
    ExtractionSource,
    PartialFields,
    apply_confidence_floor,
    regex_extract_fields,
    split_sub_spec,
)


def states_a_delivery(fields: PartialFields) -> bool:
    """Whether a pass over running text found enough to claim goods were delivered.

    **Two of the three**: the purchase order the goods were ordered on, the item, and how many of
    it arrived. Any one of them alone is a mention, not a delivery.

    Each single-field case was a real population in the live store, and none of them could ever be
    completed by the person they were queued for:

    - **a spec alone** — this pass read pages 2-5 of a receiving report and returned the report's
      own number, `WRR-17`, as the spec of four separate deliveries. 102 records are a spec and
      nothing else.
    - **a purchase order alone** — 151 records carry a PO number and no item, no quantity and no
      description. Every one sits at confidence 0.0, because `apply_confidence_floor` already knew
      they said nothing; they were staged anyway. A body that names a PO is a body that mentions a
      PO.
    - **a quantity alone** — a number with no item against it names nothing that can be received.

    The whole class is unambiguous: **529 records came from a text-only pass and not one has ever
    been complete.** Only 13 name a purchase order in their own text; the rest were handed the
    delivery event's PO afterwards, which made a passing mention look like an attributable receipt.

    Evidence the pass did find — a purchase order mentioned, a date, a carrier — is not lost by
    returning nothing here. It is already stored whole against the attachment in
    `parsed_documents`, and the message still reaches a person through its own queue row rather
    than through a line item that claims goods arrived.
    """
    stated = (
        tokens.is_po_number(fields.po_number or ""),
        bool(fields.spec_code),
        fields.quantity_received is not None,
    )
    return sum(stated) >= 2

# Kept as a public alias — this is the name other modules/tests import for the regex-only pass.
extract_fields_from_text = regex_extract_fields


class FreetextAdapter(ExtractionAdapter):
    """The adapter of last resort for body content — always handles a body source, even if
    every field ends up None. Deliberately does NOT claim attachments: it never looks at
    content_bytes, so an unrecognized attachment type must fall through to nothing (logged
    by the Ingest Orchestrator) rather than being silently mislabeled as an empty 'freetext'
    extraction — see ORCHESTRATOR_DESIGN.md's "never guess when uncertain" principle."""

    def can_handle(self, source: ExtractionSource) -> bool:
        return source.source_type == "body"

    def extract(self, source: ExtractionSource) -> List[ExtractedRecord]:
        text = source.body_text or source.body_html or ""
        fields = regex_extract_fields(text)
        extraction_source = "freetext"

        found_count = sum(1 for f in (fields.po_number, fields.spec_code, fields.quantity_received) if f is not None)
        if found_count < 2 and settings.ENABLE_AI_FALLBACK:
            ai_fields = ai_fallback.propose_fields(text)
            fields = PartialFields(
                po_number=fields.po_number or ai_fields.po_number,
                spec_code=fields.spec_code or ai_fields.spec_code,
                quantity_received=fields.quantity_received if fields.quantity_received is not None else ai_fields.quantity_received,
            )
            extraction_source = "freetext+ai"

        if not states_a_delivery(fields):
            return []

        parent_spec, sub_spec = split_sub_spec(fields.spec_code)
        confidence = 0.3 if fields.po_number else 0.0
        if extraction_source == "freetext+ai":
            confidence = min(confidence + 0.1, settings.AI_FALLBACK_CONFIDENCE_CAP)

        record = ExtractedRecord(
            source_email_id=source.source_email_id,
            po_number=fields.po_number or "",
            shipment_number=None,
            spec_code=fields.spec_code,
            parent_spec_code=parent_spec,
            sub_spec_suffix=sub_spec,
            item_description=None,
            vendor_name=None,
            carrier_name=None,
            tracking_number=None,
            quantity_received=fields.quantity_received,
            unit_of_measure=None,
            pod_stated_date=None,
            email_date=source.email_date,
            delivery_location=None,
            comments=(text[:500] or None),
            extraction_source=extraction_source,
            extraction_confidence=confidence,
            raw_snippet=text[:500],
            source_ledger_id=source.ledger_id,
        )
        return [apply_confidence_floor(record)]
