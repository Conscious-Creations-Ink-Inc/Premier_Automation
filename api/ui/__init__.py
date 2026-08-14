"""Server-rendered operations pages over the real pipeline database.

Separate from `api/routers/`, which serves the synthetic demo dashboard the React app consumes.
These pages read `state/pipeline_state.sqlite3` — what the orchestrator actually wrote.
"""
