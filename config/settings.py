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
PIPELINE_STATE_DB_PATH = STATE_DIR / "pipeline_state.sqlite3"
SPITFIRE_MOCK_DB_PATH = STATE_DIR / "mock_spitfire.sqlite3"

# --- Stage 1: Ingest & Triage -------------------------------------------

WAREHOUSE_SENDER_DOMAINS = ["authoritylogistics.com", "hospitalitylogistics.com", "atlaslogistics.com"]
FREIGHT_SENDER_DOMAINS = ["fedex.com", "ups.com", "dhl.com", "rxo.com", "oldominion.com"]
VENDOR_CONFIRMATION_DOMAINS = ["pbhhospitality.com", "coraseal.com"]
# ^ realistic placeholders matching the vendors/carriers named in the discovery docs — swap for
# Premier's real domain lists once real samples arrive (checklist #5); nothing else needs to change.
PO_TOKEN_REGEX = r"\bPO\s?#?\s?(\d{5,7})\b"
SHIPMENT_TOKEN_REGEX = r"\bshipment\s?#?\s?(\d{4,10})\b"
CANCELLATION_KEYWORDS_REGEX = r"\b(cancel(?:led|lation)?|void(?:ed)?|terminat(?:e|ed)|no longer (?:needed|required))\b"
PROPERTY_REPLY_MAX_WORDS = 200   # heuristic: a short reply, not a structured table -> likely a property confirmation

# --- Stage 2: Accumulate -------------------------------------------------

HOLD_GRACE_PERIOD_HOURS = 48
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
SPEC_TOKEN_REGEX = r"\b([A-Z]{2,4}-\d{2,4}(?:-[A-Z]{1,4})?)\b"
QTY_TOKEN_REGEX = r"\b(\d+)\s*(?:of|/)\s*(\d+)\b"

ENABLE_AI_FALLBACK = False   # checklist #1b — off until Premier explicitly permits Claude on real content
ANTHROPIC_MODEL = "claude-sonnet-5"
AI_FALLBACK_CONFIDENCE_CAP = 0.5

AZURE_DOC_INTELLIGENCE_CONFIDENCE_CAP = 0.85
AZURE_OCR_RETRY_COUNT = 1
AZURE_OCR_RETRY_BACKOFF_SECONDS = 2
AZURE_DOC_INTELLIGENCE_ENDPOINT = None   # real value pending Azure resource provisioning
AZURE_DOC_INTELLIGENCE_KEY = None        # store in Key Vault once real infra exists — never hardcode

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

# --- Graph API (real mailbox connector) --------------------------------------

GRAPH_TENANT_ID = os.getenv("GRAPH_TENANT_ID")
GRAPH_CLIENT_ID = os.getenv("GRAPH_CLIENT_ID")
GRAPH_CLIENT_SECRET = os.getenv("GRAPH_CLIENT_SECRET")
GRAPH_MAILBOX_ADDRESS = os.getenv("GRAPH_MAILBOX_ADDRESS")  # still pending from Premier/test tenant setup
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]   # app-only permissions, consented on the app registration
GRAPH_AUTHORITY_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}"

# --- Orchestrators ------------------------------------------------------------

INGEST_ORCHESTRATOR_INTERVAL_MINUTES = 20   # our own default, not yet validated against real volume
MATCH_ORCHESTRATOR_SCHEDULE = "daily"        # or "on_demand"
