"""The reconciliation dashboard API.

Run it with:  uvicorn api.main:app --reload --port 8000   (or `python run_api.py`)
Interactive docs at /docs — the whole approve/cancel flow is exercisable there, which means the
backend can be demonstrated before any of the React app exists.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api import config, db
from api.demo import seed as demo_seed
from api.routers import admin, dashboard, delivery, extracted, inbox, reconciliation, vendor

DESCRIPTION = """
Turns messy delivery-status email into PO-matched receipts, and surfaces the ones it cannot
resolve on its own.

Records that match a PO line on all three signals — PO number, spec, description — and are
missing nothing settle automatically. Everything else is flagged into the **exception queue**,
where a reviewer picks the correct line, fills whatever the vendor left out, and either
approves (staging a receipt) or cancels with a reason.

Stages 1-3 of the pipeline are real; stages 4-7 are simulated in this API layer against
synthetic data, so no live mailbox or ERP is needed.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Seed on first boot so a fresh checkout has something to show. Existing data is left
    # alone — reseeding is an explicit call to /api/admin/reset.
    conn = db.get_demo_connection()
    try:
        demo_seed.seed_if_empty(conn)
    finally:
        conn.close()
    yield


app = FastAPI(
    title="Premier Receiver Automation — Reconciliation Dashboard",
    description=DESCRIPTION,
    version="0.1.0",
    lifespan=lifespan,
)

# The Vite dev server proxies /api so the browser talks same-origin; CORS is here so hitting
# :8000 directly (and the /docs "Try it out" button) also works.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in (admin, dashboard, reconciliation, extracted, delivery, inbox, vendor):
    app.include_router(module.router)
