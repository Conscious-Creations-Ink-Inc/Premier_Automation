from typing import List

from config import settings
from pipeline.models import ExtractedRecord
from pipeline.stage3_extract import ai_fallback
from pipeline.stage3_extract.base import (
    ExtractionAdapter,
    ExtractionSource,
    PartialFields,
    apply_confidence_floor,
    regex_extract_fields,
    split_sub_spec,
)

# Kept as a public alias — this is the name other modules/tests import for the regex-only pass.
extract_fields_from_text = regex_extract_fields


class FreetextAdapter(ExtractionAdapter):
    """The adapter of last resort — always handles something, even if every field ends up None."""

    def can_handle(self, source: ExtractionSource) -> bool:
        return True

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
        )
        return [apply_confidence_floor(record)]
