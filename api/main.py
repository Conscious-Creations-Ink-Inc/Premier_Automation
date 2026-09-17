"""The reconciliation dashboard API.

Run it with:  uvicorn api.main:app --reload --port 8000   (or `python run_api.py`)
Interactive docs at /docs — the whole approve/cancel flow is exercisable there, which means the
backend can be demonstrated before any of the React app exists.
"""
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.gzip import GZipMiddleware

from api import auth, config
from api import login as login_routes
from api.routers import admin, dashboard, delivery, extracted, inbox, po, reconciliation, vendor
from api.ui import routes as ui_routes
from operations import killswitch, scheduler
from operations import store as ops_store
from pipeline import read_views

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
    #  * `abandon_unfinished_runs` closes out rows left mid-run by a process that died. This
    #    process has only just started, so it holds no run and no lock — any row still open is a
    #    corpse, and left alone it renders as "running..." in history for ever.
    #
    # `scheduler.start()` is what makes the automation able to run unattended, so it is deliberately
    # last and deliberately visible: nothing it starts can run while the kill switch is engaged,
    # and the schedule itself is off until someone enables it on /ui/automation.
    ops_conn = ops_store.get_connection()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ops_store.ensure_anchor(ops_conn, now)
        killswitch.load(ops_conn)
        ops_store.abandon_unfinished_runs(ops_conn, at=now)
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

# Every page carries its rows inline — `/ui/mails` ships all 4,057 of them, 4.81 MB — on top of a
# fixed 154.5 KB of CSS and JS that is inlined into `<head>` and so can never be cached separately.
# Gzip takes that document to roughly a tenth of its size. Starlette's own middleware, so this
# costs no dependency (CLAUDE.md s1); `minimum_size` keeps it off the small JSON the pollers ask
# for every two seconds, where the compression would cost more than the bytes it saved.
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Signing in. Registered before everything else so the exemption is visible at the top of the
# guarded surface rather than buried under it: these are the only routes in the app that do not
# require a session, and a login page behind a login gate is a redirect loop.
app.include_router(login_routes.router)


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    """Liveness, deliberately outside the gate.

    So a supervisor can tell "the app is down" from "the app will not talk to you" without holding
    a credential. It reports nothing about the data — no counts, no queue depth, no mailbox — for
    the same reason the login page draws no stat strip.
    """
    return {"ok": True}


# The whole `/api/*` surface, behind the gate. Passed to `include_router` rather than written onto
# eight routers, and onto the router rather than onto each route function: a guard that has to be
# repeated is a guard that will be left off the route somebody adds next month. `admin` is in here
# too — it owns `seed` and `reset`, which are the last endpoints that should be open.
for module in (admin, dashboard, reconciliation, extracted, delivery, po, inbox, vendor):
    app.include_router(module.router, dependencies=[Depends(auth.require_session)])


@app.exception_handler(auth.NotAuthenticated)
async def _ask_them_to_sign_in(request: Request, _exc: auth.NotAuthenticated):
    """Turn the guard's refusal into the right kind of "no" for whoever asked.

    A browser navigating to a page wants to be taken to the login form, and to come back where it
    was going afterwards. A `fetch()` wants a status it can branch on: the `/ui/version` poller
    runs on every open tab every ten seconds, and answering it with a 303 to an HTML page would
    have it parsing a login form as JSON for ever, silently, with no sign anything was wrong.

    The test is what the request asked for, not what the path looks like — `/ui/version` and
    `/ui/run-progress` are JSON routes living under the HTML prefix.
    """
    accept = request.headers.get("accept", "")
    wants_html = "text/html" in accept or (accept in ("", "*/*") and request.method == "GET")
    if request.method == "GET" and wants_html:
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(f"/login?next={quote(target, safe='/?=&')}", status_code=303)
    return JSONResponse({"detail": "Not authenticated"}, status_code=401)

@app.middleware("http")
async def _drop_cached_summary_after_a_write(request, call_next):
    """The header summary is memoised (`read_views.summary_across_sources`). Anything that writes
    must drop it, or the redirect that follows a write draws the figures from before it.

    Here rather than in the twenty `/ui` POST handlers, because that list only grows and a call
    added to nineteen of them is a bug nobody sees for a month. A method check at the edge cannot be
    forgotten by the next route.

    After the response, not before: the handler is what does the writing. `read_views`' own TTL is
    what covers the writer this cannot see — the pipeline runner, which writes from a background
    thread and never passes through here.
    """
    response = await call_next(request)
    if request.method != "GET" and request.url.path.startswith("/ui"):
        read_views.invalidate_summary()
    return response


# The one interface Premier sees: server-rendered HTML at /ui. Registered outside the loop above on
# purpose — that loop is the /api/* demo surface on synthetic data, and the two read different
# databases. Excluded from the OpenAPI schema so /docs stays the API's own contract.
app.include_router(ui_routes.router, dependencies=[Depends(auth.require_session)])
