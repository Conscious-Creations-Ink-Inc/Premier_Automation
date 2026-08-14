"""The reconciliation dashboard API.

Run it with:  uvicorn api.main:app --reload --port 8000   (or `python run_api.py`)
Interactive docs at /docs — the whole approve/cancel flow is exercisable there, which means the
backend can be demonstrated before any of the React app exists.
"""
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api import config
from api.routers import admin, dashboard, delivery, extracted, inbox, po, reconciliation, vendor
from api.ui import routes as ui_routes
from operations import killswitch, scheduler
from operations import store as ops_store

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
    # The synthetic demo dataset is NOT seeded on boot any more. It used to be, so a fresh checkout
    # had something to show; now that every `/ui` page reads Premier's real mailbox, inventing rows
    # in a neighbouring database on startup is a way to end up demonstrating fabricated deliveries
    # by accident. `POST /api/admin/seed` still does it on request, for the `/api/*` surface only.

    # The operations side, inherited from the standalone console when the three UIs were collapsed
    # into one. Two things must happen before any request is served:
    #
    #  * `ensure_anchor` fixes the schedule's starting point, so a schedule enabled earlier is due
    #    an interval from then rather than immediately on this boot.
    #  * `killswitch.load` restores a stop from the database. A stop that forgets when the process
    #    bounces is not a stop.
    #
    # `scheduler.start()` is what makes the automation able to run unattended, so it is deliberately
    # last and deliberately visible: nothing it starts can run while the kill switch is engaged,
    # and the schedule itself is off until someone enables it on /ui/automation.
    ops_conn = ops_store.get_connection()
    try:
        ops_store.ensure_anchor(ops_conn, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        killswitch.load(ops_conn)
    finally:
        ops_conn.close()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.stop()


app = FastAPI(
    title="Premier Receiver Automation — Reconciliation Dashboard",
    description=DESCRIPTION,
    version="0.1.0",
    lifespan=lifespan,
)

# The UI is served by this process at /ui, so the browser is always same-origin and the default
# origin list is empty. Kept wired up for the case where something separate has to call /api/* —
# see the note in api/config.py before adding one.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in (admin, dashboard, reconciliation, extracted, delivery, po, inbox, vendor):
    app.include_router(module.router)

# The one interface Premier sees: server-rendered HTML at /ui. Registered outside the loop above on
# purpose — that loop is the /api/* demo surface on synthetic data, and the two read different
# databases. Excluded from the OpenAPI schema so /docs stays the API's own contract.
app.include_router(ui_routes.router)
