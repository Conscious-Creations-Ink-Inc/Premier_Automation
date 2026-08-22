import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# --- Paths -------------------------------------------------------------

SAMPLE_DATA_DIR = BASE_DIR / "sample_data"
SAMPLE_EMAILS_DIR = SAMPLE_DATA_DIR / "emails"
SAMPLE_ATTACHMENTS_DIR = SAMPLE_DATA_DIR / "attachments"
PO_LINES_SEED_FILE = SAMPLE_DATA_DIR / "po_lines.json"

STATE_DIR = BASE_DIR / "state"
# Two stores, one schema. Premier's live mail and the .msg test corpus are kept in separate
# files rather than separated by a column, so no forgotten WHERE clause can ever show sample
# data on a receiver report. `pipeline/state_db.path_for()` is the only place that chooses.
PIPELINE_STATE_DB_PATH = STATE_DIR / "pipeline_state.sqlite3"   # LIVE mailbox. Never sample data.
SAMPLE_STATE_DB_PATH = STATE_DIR / "sample_state.sqlite3"       # the .msg corpus. Testing only.
# SPITFIRE_MOCK_DB_PATH was reserved for a local Spitfire simulator that was never written, and
# nothing referenced it. The real read connector (connectors/spitfire.py) supersedes the idea:
# PO lines are mirrored into the pipeline state DB from the live API instead of being faked.

# --- Stage 1: Ingest & Triage -------------------------------------------

# Observed in the real June corpus (Documents/Premier_Delivery_Email_Corpus_Analysis.md §5).
# Still partial — Premier owes us their full vendor/property lists (checklist item B7) — but
# these are verified against actual message headers rather than invented, which the previous
# placeholder lists were not (finding C6).

INTERNAL_DOMAINS = ["premierpm.com"]
WAREHOUSE_SENDER_DOMAINS = ["authoritylogistics.com", "crownwms.com"]
FREIGHT_SENDER_DOMAINS = ["fedex.com", "ups.com", "dhl.com", "rxo.com", "oldominion.com", "globaltranz.com"]
VENDOR_CONFIRMATION_DOMAINS = ["5starinterior.com", "knoxtile.com", "goarmstrong.com", "firstfinish.net"]
PROPERTY_DOMAINS = ["remingtonhotels.com", "cameobeverlyhills.com"]

# Kept for the generic adapters. Triage no longer uses these — `pipeline/parsing/tokens.py`
# owns the token grammar now, so the stages cannot disagree about what a PO is (finding C4).
PO_TOKEN_REGEX = r"\bPO\s?#?\s?(\d{5,7})\b"
SHIPMENT_TOKEN_REGEX = r"\bshipment\s?#?\s?(\d{4,10})\b"
CANCELLATION_KEYWORDS_REGEX = r"\b(cancel(?:led|lation)?|void(?:ed)?|terminat(?:e|ed)|no longer (?:needed|required))\b"
PROPERTY_REPLY_MAX_WORDS = 200   # heuristic: a short reply, not a structured table -> likely a property confirmation

# --- Stage 2: Accumulate -------------------------------------------------

# How long a hold-only delivery waits for a partner notice before `sweep_stale_holds` releases it.
#
# 48 hours is the real rule and the default. It exists so a warehouse notice arriving a day late
# still joins its property confirmation instead of producing a second receiver — the double-count
# that ended Premier's previous attempt. Do not lower it in code.
#
# Overridable by env so a demo can release held mail immediately (`PREMIER_HOLD_GRACE_HOURS=0`)
# without editing a business rule that then ships at 0 because somebody forgot to put it back.
# Deleting the line from `.env` is the whole revert.
HOLD_GRACE_PERIOD_HOURS = int(os.getenv("PREMIER_HOLD_GRACE_HOURS", "48"))
STALE_HOLD_THRESHOLD_DAYS = 14   # shared with Stage 7's stale-hold sweep

# --- Stage 3: Extract -----------------------------------------------------

COLUMN_SYNONYMS = {
    "po_number": ["PO", "Purchase Order", "PO#", "PO Number"],
    "spec_code": ["Spec", "Spec#", "Item Number", "Item#", "Product Number"],
    "item_description": ["Description", "Item Description", "Product"],
    "quantity_received": ["Qty", "Quantity", "Qty Shipped", "Qty Received", "Qty Rcvd"],
    "unit_of_measure": ["UOM", "Unit", "Type"],
    "vendor_name": ["Vendor", "Supplier"],
}
HEADER_MATCH_THRESHOLD = 85   # RapidFuzz ratio, 0-100 — fuzzy header matching against COLUMN_SYNONYMS
SPEC_TOKEN_REGEX = r"\b([A-Z]{2,4}-\d{2,4}[A-Za-z]?(?:-[A-Z]{1,4})?)\b"
# ^ trailing [A-Za-z]? added after real dummy test data: "GR-350a-WTF" has a letter fused onto
# the numeric segment — a real, plausible spec-code shape our original digits-only match missed.
QTY_TOKEN_REGEX = r"(?<!\d/)\b(\d+)\s*(?:of|/)\s*(\d+)\b(?!\s*/\s*\d)"
# ^ guarded against dates after real dummy test data: "7/18/26" was being misread as a quantity
# fraction "7/18". The guards reject a match that's part of a longer d/d/d chain (a date has
# three slash-separated numbers; a real quantity fraction like "11/12" only ever has two).

# --- Attachment limits ------------------------------------------------------
# There were no limits at all: a 200 MB attachment was read into memory, base64-encoded, and
# written into a SQLite TEXT column. Breaching any of these is never a silent drop — the
# attachment is recorded in the ledger with the reason and the mail is quarantined.

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024          # one file
MAX_EMAIL_ATTACHMENT_BYTES = 100 * 1024 * 1024   # all files on one email
MAX_ATTACHMENTS_PER_EMAIL = 100
MAX_CONTAINER_DEPTH = 4                          # zip inside msg inside zip …
MAX_CONTAINER_MEMBERS = 50                       # members of any one container
MAX_CONTAINER_MEMBERS_TOTAL = 200                # across an email's whole container tree
MAX_EXPANDED_BYTES = 100 * 1024 * 1024           # total uncompressed, shared across the tree
MAX_ZIP_COMPRESSION_RATIO = 200                  # file_size / compress_size — the bomb guard

# --- OCR --------------------------------------------------------------------
# Production OCR is Azure AI Vision's Read API, called over REST. Both values are read from the
# environment and are blank until Premier provisions the resource; while they are blank the
# client selection below resolves to the Mock and photographed PODs route to a person with a
# stated reason rather than silently yielding nothing.

AZURE_VISION_ENDPOINT = os.getenv("AZURE_VISION_ENDPOINT") or None
AZURE_VISION_KEY = os.getenv("AZURE_VISION_KEY") or None
AZURE_VISION_API_VERSION = "2024-02-01"
# 30s was not enough to *upload* a 2.7 MB phone photo on a normal office uplink — the POST died
# with a write timeout before Azure ever saw it, which reads as an OCR outage rather than a slow
# link. The corpus photos are 2.5-3 MB each, so this is the common case, not the tail.
AZURE_VISION_TIMEOUT_SECONDS = 180

OCR_CLIENT = os.getenv("PREMIER_OCR_CLIENT", "auto")   # auto | azure | tesseract | mock
OCR_MAX_IMAGE_BYTES = 20 * 1024 * 1024   # Azure Vision's own per-image ceiling
OCR_MAX_PAGES = 10                       # a scanned PDF is rasterised page by page; cap the spend

ENABLE_AI_FALLBACK = False   # checklist #1b — off until Premier explicitly permits Claude on real content
ANTHROPIC_MODEL = "claude-sonnet-5"
AI_FALLBACK_CONFIDENCE_CAP = 0.5

AZURE_DOC_INTELLIGENCE_CONFIDENCE_CAP = 0.85
AZURE_OCR_RETRY_COUNT = 1
AZURE_OCR_RETRY_BACKOFF_SECONDS = 2

# The provisioned resource ("premier", eastus) is a Document Intelligence resource, so it serves
# /documentintelligence/* but NOT Vision's /computervision/* — a Vision call against it 401s.
# DocInt is the better fit anyway: it returns real table structure (which Vision does not) and
# reads a PDF directly instead of rasterising it page by page. The endpoint/key fall back to the
# AZURE_VISION_* pair so one credential pair in .env drives both clients.
AZURE_DOC_INTELLIGENCE_ENDPOINT = os.getenv("AZURE_DOC_INTELLIGENCE_ENDPOINT") or AZURE_VISION_ENDPOINT
AZURE_DOC_INTELLIGENCE_KEY = os.getenv("AZURE_DOC_INTELLIGENCE_KEY") or AZURE_VISION_KEY
AZURE_DOC_INTELLIGENCE_API_VERSION = "2024-11-30"
# prebuilt-layout returns tables; prebuilt-read is text-only and ~6x cheaper per page on paid
# tiers (identical on the free tier, which meters pages not dollars). Switch via .env.
AZURE_DOC_INTELLIGENCE_MODEL = os.getenv("AZURE_DOC_INTELLIGENCE_MODEL", "prebuilt-layout")
AZURE_DOC_INTELLIGENCE_POLL_SECONDS = 2      # analyze is async: 202 + poll operation-location
AZURE_DOC_INTELLIGENCE_POLL_TIMEOUT_SECONDS = 120

TESSERACT_CMD_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"  # dev/test OCR only — see STAGE_3_EXTRACT.md

# --- Stage 4: Reconcile & Match --------------------------------------------

DESC_MATCH_THRESHOLD = 80   # RapidFuzz token_sort_ratio, 0-100
MATERIAL_COST_CODE_PREFIX = "1"   # shared with Stage 5

# --- Stage 5: Verify ---------------------------------------------------------

CBD_ADR_RECEIPT_WINDOW_DAYS = 30

# --- Stage 6: Build & Log -------------------------------------------------

STAGED_BY_USER = "CC-Automation"

# --- Mailbox folder routing (post-processing organization, both connectors) --
# Every email process_new_mail finishes with gets moved out of Inbox into one of these, so the
# Inbox only ever holds genuinely new/unprocessed mail and a human can audit any category by
# looking at the folder — see ORCHESTRATOR_DESIGN.md's "every decision traceable" principle.

MAILBOX_FOLDER_HIDDEN = "Hidden"        # TriageCategory.HIDE — pure noise, correctly discarded
MAILBOX_FOLDER_ROUTED = "Routed"        # TriageCategory.ROUTE — sent to the human exception queue
MAILBOX_FOLDER_PROCESSED = "Processed"  # SURFACE/HOLD — captured into our own state, safe to move
MAILBOX_FOLDER_ERRORS = "Errors"        # triage/accumulate raised — needs a human look, never retried silently forever
MAILBOX_FOLDER_QUARANTINE = "Quarantine"  # breached an attachment limit — held intact for a person

# --- Graph API (real mailbox connector) --------------------------------------

GRAPH_TENANT_ID = os.getenv("GRAPH_TENANT_ID")
GRAPH_CLIENT_ID = os.getenv("GRAPH_CLIENT_ID")
GRAPH_CLIENT_SECRET = os.getenv("GRAPH_CLIENT_SECRET")
GRAPH_MAILBOX_ADDRESS = os.getenv("GRAPH_MAILBOX_ADDRESS")  # still pending from Premier/test tenant setup
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]   # app-only permissions, consented on the app registration
GRAPH_AUTHORITY_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}"

# --- Spitfire sfPMS (ERP, read-only) ------------------------------------------
# Training instance, sfPMS 2023.0.9692.36214. `GET /api/system/version` answers anonymously from
# outside Premier's network, so the connector needs no VPN for training; production may differ.
# Credentials are blank until Premier provisions `svc-receiver-automation`. For this phase that
# account needs READ permission only — purchase orders and their lines. Nothing else.

SPITFIRE_BASE_URL = os.getenv("SPITFIRE_BASE_URL", "https://training.remingtonhotels.com/Training")
SPITFIRE_UID = os.getenv("SPITFIRE_UID") or None
SPITFIRE_PW = os.getenv("SPITFIRE_PW") or None
# Sent on login as `SiteLogin.tzOffset` and used by Spitfire to stamp server-side dates. Wrong
# value shifts received dates by hours, which matters because Stage 5 compares them to POD dates.
SPITFIRE_TZ_OFFSET = float(os.getenv("SPITFIRE_TZ_OFFSET", "-5"))

# `ForDocType` filter for PO discovery. Taken from Premier's own czx_TPICreate_ReceiptDoc.sql
# (@PODTK), which is a production value and unverified on training — hence overridable and
# optional: `resolve_po` still works without it, just with more candidates to sift.
# NB the master plan §3.5 transcribes this as 'ff197fd-...', one character short. The .sql is right.
SPITFIRE_PO_DOC_TYPE_KEY = os.getenv("SPITFIRE_PO_DOC_TYPE_KEY", "ff1975fd-76de-486c-888b-54e8fcd880e0")
SPITFIRE_SEARCH_SCOPE = os.getenv("SPITFIRE_SEARCH_SCOPE", "0")   # folderDesignation; 0 = site-wide

# Receipt document type, confirmed 10 Aug against TrainingsfDocSys: 76,414 documents carry it and
# `DocTypeKey_dv` reads "Receipt". Matches @RDTK in czx_TPICreate_ReceiptDoc.sql. Read-side use
# only for now — it identifies existing receipts so we can tell an already-received PO from a new
# delivery. The connector still has no method that creates one.
SPITFIRE_RECEIPT_DOC_TYPE_KEY = os.getenv(
    "SPITFIRE_RECEIPT_DOC_TYPE_KEY", "0c9a537a-3c41-4d16-ab9f-130ef69ea6c8")

# A borrowed browser session, as an alternative to SPITFIRE_UID/PW. Premier's Spitfire has Entra
# SSO enabled, so an interactive user may have no password to put in .env at all — in that case
# this is the only way in until `svc-receiver-automation` exists. Paste the value of the
# `sfPMSAuth` cookie (NOT `sfSession`, which is only a session id).
#
# Short-lived: forms tickets lapse and Spitfire enforces an idle timeout, so this is for one-off
# pulls, never the scheduled pipeline. Everything read is attributed to whoever owns the session.
SPITFIRE_SESSION_COOKIE = os.getenv("SPITFIRE_SESSION_COOKIE") or None

# PO discovery needs a project ID, and this account cannot enumerate projects — `POST /api/projects`
# returns 200 with zero rows and `GET /api/projects` is 405. The IDs below were read out of
# `TrainingsfDocSys.dbo.xsfDocHeader` and are where the June corpus POs actually live: 18 in
# ...100003, 8 in ...100002, 2 in MRC026PB100002. Remove this once Premier grants the automation
# account project membership, which would make /api/projects answer for itself.
SPITFIRE_PROJECT_IDS = [
    p.strip() for p in os.getenv(
        "SPITFIRE_PROJECT_IDS",
        "MRC024PB100003,MRC024PB100002,MRC026PB100002",
    ).split(",") if p.strip()
]

SPITFIRE_READ_ONLY = True
"""Not a runtime switch — a statement of scope. connectors/spitfire.py has no write methods at
all, and its allowlist rejects any non-read request before a socket opens. Premier has not
authorised a write to their ERP; when they do, that is a reviewed change to `_ALLOWED`, not a
flag someone flips."""

# --- Spitfire cassettes: developing away from the office IP --------------------
# Spitfire only answers from Premier's office network, and SPITFIRE_SESSION_COOKIE is a
# hand-captured browser ticket that lapses on idle — so the connected window is short even when
# you are there. `connectors/spitfire_cassette.py` records responses while connected and replays
# them when not, as a requests transport adapter mounted under both clients.
#
#   off     nothing is mounted; the code takes exactly the paths it takes today. THE DEFAULT.
#   record  call live, save every response, return the live one.
#   replay  never open a socket. A call nothing recorded raises, naming the call; the four
#           mutating write operations refuse outright (a replayed create_receipt would hand back
#           the same DocMasterKey every time and the ledger would record a key that looks real).
#   auto    replay on a hit, otherwise live-and-record. Office mode: warms the store as you work.
SPITFIRE_CASSETTE_MODE = os.getenv("SPITFIRE_CASSETTE_MODE", "off")
SPITFIRE_CASSETTE_DIR = Path(
    os.getenv("SPITFIRE_CASSETTE_DIR", "") or (STATE_DIR / "spitfire_cassettes"))
"""Gitignored: the bodies carry real vendor names, named Premier employees on approval routes, and
cost codes. A small scrubbed subset lives in `tests/fixtures/spitfire/` instead."""

# --- Orchestrators ------------------------------------------------------------

INGEST_ORCHESTRATOR_INTERVAL_MINUTES = 20   # our own default, not yet validated against real volume
MATCH_ORCHESTRATOR_SCHEDULE = "daily"        # or "on_demand"

INGEST_OVERLAP_MINUTES = int(os.getenv("PREMIER_INGEST_OVERLAP_MINUTES", "60"))
"""How far behind the watermark each poll starts listing. See `stage1_ingest.listing_window_start`.

Generous on purpose. Re-listing an hour of already-seen mail costs a set lookup per row, because
`skip_ids` rejects it before anything is fetched; missing a message because a clock disagreed by
two minutes costs a receiver nobody creates."""
