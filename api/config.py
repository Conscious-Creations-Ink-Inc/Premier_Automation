"""Settings for the dashboard API layer.

Deliberately thin. Everything the pipeline already decides — match thresholds, mailbox folder
names, the staging user — is read from `config.settings`, so the dashboard can never drift
from the pipeline's own rules. Only server/UI concerns live here.
"""
import os

from config import settings

# --- Storage -----------------------------------------------------------------

# The dashboard owns its own SQLite file. Seeding or resetting the demo therefore can never
# touch pipeline_state.sqlite3, which the real ingest orchestrator writes to.
DEMO_DB_PATH = settings.STATE_DIR / "demo_dashboard.sqlite3"

# --- Server ------------------------------------------------------------------

API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "8000"))

# The Vite dev server proxies /api, so the browser normally talks same-origin. CORS is still
# enabled so hitting :8000 directly (and /docs "Try it out") works during development.
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
    if origin.strip()
]

# --- Review ------------------------------------------------------------------

# Who an approve/cancel is attributed to when the request doesn't name an operator.
DEFAULT_OPERATOR = os.getenv("DEFAULT_OPERATOR", "ashford@consciouscreations.ai")

# --- Demo data ---------------------------------------------------------------

# Fixed seed: every reseed produces the identical dataset, so the UI, the screenshots and the
# tests all describe the same records.
DEMO_RANDOM_SEED = 42
