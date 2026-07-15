# Premier Receiver Automation — Code

The actual implementation. Design docs live one level up: `../BuildPlan/` (the 7 stage specs + `PLAN.md` + `ORCHESTRATOR_DESIGN.md`) and `../ourDocs/` (business background, checklists, edge-case log). Read those before changing anything here — every file in this repo maps directly to a section of one of those docs.

## Setup

```
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

## Running Tests

```
pytest
```

## Status

Scaffolding only. `config/settings.py` and `pipeline/models.py` are implemented; everything else is a stub pointing at the `BuildPlan/` file that specifies it. Build order: see `../BuildPlan/PLAN.md`'s "Build Sequence Roadmap."
