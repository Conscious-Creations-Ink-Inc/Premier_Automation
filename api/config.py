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

# Empty by default, and that is the safe answer now: the UI is served by this same process at /ui,
# so every browser request is already same-origin and needs no grant. The list used to name the
# Vite dev server on :5173 for the React dashboard, which no longer exists.
#
# Set CORS_ORIGINS only if something genuinely separate has to call /api/*, and name that origin
# exactly — the middleware is configured with allow_credentials=True, so a wildcard here would let
# any site read authenticated responses once this app has any authentication at all.
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "").split(",")
    if origin.strip()
]

# --- Review ------------------------------------------------------------------

# Who an approve/cancel is attributed to when the request doesn't name an operator.
DEFAULT_OPERATOR = os.getenv("DEFAULT_OPERATOR", "ashford@consciouscreations.ai")

# --- Demo data ---------------------------------------------------------------

# Fixed seed: every reseed produces the identical dataset, so the UI, the screenshots and the
# tests all describe the same records.
DEMO_RANDOM_SEED = 42
