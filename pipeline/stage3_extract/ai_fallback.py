from pipeline.stage3_extract.base import PartialFields


def propose_fields(text: str) -> PartialFields:
    """Calls Claude with a strict, narrow prompt (see BuildPlan/STAGE_3_EXTRACT.md):
      - Extract ONLY fields explicitly stated in the text.
      - Return null for anything not explicitly present — never infer or guess.
    This is the only function in the whole pipeline allowed to call an LLM, and even then it
    only ever proposes field values — the match decision stays deterministic in Stage 4.

    Not implemented yet: the `anthropic` SDK isn't installed and `ENABLE_AI_FALLBACK` defaults
    to False (checklist #1b is still open — Premier hasn't confirmed confidential content may
    go to Claude). Tests exercise the call site by monkeypatching this function directly.
    """
    raise NotImplementedError(
        "Real Claude fallback not yet wired up — requires the anthropic SDK and Premier's "
        "explicit confirmation on checklist #1b. Do not enable until both exist."
    )
