import os
from pathlib import Path
from typing import List

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

AUTH_DB_PATH = STATE_DIR / "auth.sqlite3"
"""Who may sign in to /ui. A file of its own, not tables in `pipeline_state.sqlite3`, for three
reasons: adding tables to the live store would be an automatic schema change to a 2.12 GB database
(CLAUDE.md s4); `state/` accumulates `.bak-*` copies of that store -- 36 of them, ~40 GB, as of
2026-09-16 -- and a password hash should not be copied into every one of them; and an auth store
that can be read, audited and rotated without opening the pipeline's data is easier to reason
about. Created by `tools/create_admin.py`, never by an import."""

SESSION_TTL_HOURS = int(os.getenv("PREMIER_SESSION_TTL_HOURS", "12"))
"""How long a signed-in session stays valid before the cookie must be reissued."""

SESSION_COOKIE_NAME = os.getenv("PREMIER_SESSION_COOKIE", "premier_session")

SPITFIRE_WAREHOUSE_DB_PATH = STATE_DIR / "spitfire_mirror.sqlite3"
"""Every readable Spitfire endpoint, landed in typed tables. A third file rather than more tables
in `pipeline_state.sqlite3`, because that one is already 2.12 GB — 2.03 GB of it `accumulation.
payload_json` — and every `*.bak-` copy the backfill tools take would grow by the warehouse too.
(Measured 2026-09-16. This figure read "100 MB / 78 MB" when it was written; the duplication
described in `state_db._ensure_schema` is what happened in between.)

It is a mirror, not state: nothing the pipeline decides lives here, so losing it costs a re-sweep
and nothing else. `pipeline/spitfire_mirror.py`'s two tables stay in the state DB and remain the
app's fast path; this is pure addition. See `pipeline/spitfire_warehouse.py`."""
# SPITFIRE_MOCK_DB_PATH was reserved for a local Spitfire simulator that was never written, and
# nothing referenced it. The real read connector (connectors/spitfire.py) supersedes the idea:
# PO lines are mirrored into the pipeline state DB from the live API instead of being faked.

# --- Stage 1: Ingest & Triage -------------------------------------------

# Observed in the real June corpus (Documents/Premier_Delivery_Email_Corpus_Analysis.md §5).
# Still partial — Premier owes us their full vendor/property lists (checklist item B7) — but
# these are verified against actual message headers rather than invented, which the previous
# placeholder lists were not (finding C6).

# These are ROUTING RULES, not test data. Triage decides what an email is by which list its
# sender's domain falls in, so the real values are operational configuration and belong in the
# environment — never in source, per Ashford Standards v1.5 §9 and §12.
#
# The defaults below are the synthetic `.test` domains the fixtures use (RFC 6761 — reserved,
# and can never resolve to a real host). That is deliberate and load-bearing: the test suite runs
# against the defaults and needs no credentials, while production supplies the real domains via
# `.env`. Changing a default therefore changes what the tests exercise, not what production
# routes.
#
# Premier still owes us their full vendor and property lists (checklist item B7).

def _domains(var: str, default: str) -> List[str]:
    """Comma-separated domain list from the environment, lowercased and de-blanked."""
    return [d.strip().lower() for d in os.getenv(var, default).split(",") if d.strip()]


INTERNAL_DOMAINS = _domains("PREMIER_INTERNAL_DOMAINS", "example-pm.test")
WAREHOUSE_SENDER_DOMAINS = _domains(
    "PREMIER_WAREHOUSE_SENDER_DOMAINS", "example-logistics.test,example-warehouse.test")
FREIGHT_SENDER_DOMAINS = _domains(
    "PREMIER_FREIGHT_SENDER_DOMAINS",
    "fedex.com,ups.com,dhl.com,rxo.com,oldominion.com,globaltranz.com")
VENDOR_CONFIRMATION_DOMAINS = _domains(
    "PREMIER_VENDOR_CONFIRMATION_DOMAINS",
    "example-interiors.test,example-tile.test,example-flooring.test,example-finishes.test")
PROPERTY_DOMAINS = _domains(
    "PREMIER_PROPERTY_DOMAINS", "example-hotels.test,example-property.test")

# Mailboxes that only ever send scheduled reports. Nothing from one is a delivery event.
#
# `Reports@example-pm.test` sends `4-Pending Pay Requests (includes In-Process) was executed
# at 8/27/2026 2:00:10 AM` every morning at 2am. It lists every open pay request, so it carries 130
# PO numbers — which is all `rule_4_property_reply` needs to hold it as a property confirmation.
# One arrival staged **126 records**, 36% of everything in the store, and 126 phantom deliveries
# with it. Reading the prose cannot help: the report says nothing about goods arriving, which is
# exactly why the intent rule below also catches it. This list is the cheaper, surer half.
REPORT_SENDER_ADDRESSES = [
    a.strip().lower()
    for a in os.getenv("PREMIER_REPORT_SENDER_ADDRESSES", "reports@example-pm.test").split(",")
    if a.strip()
]

# Boilerplate anchors — the contact fragments that mark a signature or footer block so
# `parsing/boilerplate.py` can strip it before extraction reads the message.
#
# Same reasoning as the domain lists above: a real street address and a real switchboard number
# are production data and may not sit in source (§12), but the stripping rules genuinely need
# them, so they are configuration with synthetic defaults. Get these wrong in production and
# signature text survives into extraction, where an address line reads as delivery content.
BOILERPLATE_INTERNAL_ADDRESS = os.getenv(
    "PREMIER_BOILERPLATE_INTERNAL_ADDRESS", "100 Example Plaza")
BOILERPLATE_PARTNER_ADDRESS = os.getenv(
    "PREMIER_BOILERPLATE_PARTNER_ADDRESS", "200 Example Center, Suite 400")
BOILERPLATE_PARTNER_PHONE = os.getenv(
    "PREMIER_BOILERPLATE_PARTNER_PHONE", "555.010.1234")

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

# The spec code is the one field vendors name most inconsistently. Premier's own PO calls it a
# spec; Atlas heads the column "Carton Markings (Item#/Mfg#/Serial#/Model#/Roll#)"; carriers write
# Model# or Serial#. Kept as one list, in one place, because it is the list that grows: adding the
# next alias should be a one-line edit here and nowhere else.
#
# `parsing/confirmation.SPEC_HEADERS` reads this too — it used to carry a second, separately
# maintained list, so an alias added to one was silently missing from the other.
SPEC_CODE_ALIASES = [
    "Spec", "Spec#", "Spec #", "SPEC # or Phase Code", "Phase Code",
    "Item Number", "Item#", "Item #", "Product Number",
    "Mfg#", "Mfg #", "Manufacturer#", "Manufacturer #",
    "Serial#", "Serial #", "Model#", "Model #", "Roll#", "Roll #",
    "Carton Markings",
]

COLUMN_SYNONYMS = {
    # A warehouse or vendor writes the purchase order under its own customer's name for it. Atlas
    # heads the column `Customer PO #`; Peerless writes `Customer Order Number`. Across the stored
    # parses these carry 33 real purchase orders — 213993, 215127, 215060, 215017 — and not one of
    # them mapped, because "PO"/"PO#" are too short for embedded matching and "Purchase Order"
    # scores far under the threshold against "customer po #".
    #
    # `Order Number` and `Order No` are in this list deliberately and are **only safe because of
    # the check in `build_record_from_row`**: on the Peerless packing slip attached to PO 212749,
    # `Customer Order Number` is 125609 and `Order Number` is SO127023. SO127023 fails the shape
    # test; 125609 passes it, is not a Spitfire purchase order at all, and would otherwise be
    # staged as the one these goods were ordered on. `ExtractionSource.known_po_numbers` is what
    # rejects it. Do not add a further order-number alias without checking that guard still holds.
    "po_number": ["PO", "Purchase Order", "PO#", "PO Number",
                  "Customer PO", "Customer PO #", "Customer PO#", "Customer P.O.",
                  "Customer PO Number", "Customer Order Number", "Order Number", "Order No"],
    "spec_code": SPEC_CODE_ALIASES,
    "item_description": ["Description", "Item Description", "Product"],
    # "Delivered Qty" is the vendor packing list's own column for what actually left the
    # dock, and it sits beside an "Ordered Qty" that must never be read as received —
    # that confusion is the whole of the records 131-133 defect. The two score 75 against
    # each other, under the 85 threshold, so the distinction holds; `test_parsing` proves
    # it rather than trusting it.
    "quantity_received": ["Qty", "Quantity", "Qty Shipped", "Qty Received", "Qty Rcvd",
                          "Delivered Qty", "Qty Delivered"],
    "unit_of_measure": ["UOM", "Unit", "Type"],
    "vendor_name": ["Vendor", "Supplier"],
}
HEADER_MATCH_THRESHOLD = 85   # RapidFuzz ratio, 0-100 — fuzzy header matching against COLUMN_SYNONYMS

# A header cell that *contains* a synonym as a whole word, rather than being one. Atlas writes
# "Description (such as Desk, Chair, Lamp)" and "Carton Markings (Item#/Mfg#/Serial#/Model#/Roll#)"
# — both unmistakable to a reader, both scoring 44-55 on whole-string ratio and so missed entirely,
# which sent the whole grid to the regex fallback and produced records with no PO at all.
#
# Only synonyms this long are searched for inside a larger header: "PO" and "Qty" are short enough
# to appear inside unrelated words, and both already match on their own as whole cells.
HEADER_EMBEDDED_MIN_LENGTH = 4

# Facts a delivery form states once, about the whole shipment, in a labelled band above the item
# grid — not per line. Atlas's receiving report carries `Carrier | Vendor / Shipper's Name | Date
# Received | Received by` with the values on the row beneath, then `BOL, PRO, Or Tracking #` the
# same way.
#
# Worth reading precisely because `pod_stated_date` is one of the five fields `completeness`
# demands and the one no other system holds: without it a record with a perfectly good PO, spec,
# description and quantity still cannot be posted, and a person retypes a date that was sitting in
# the document all along.
DOCUMENT_FIELD_LABELS = {
    "pod_stated_date": ["Date Received", "Received Date", "Date Delivered", "Delivery Date",
                        "Date of Delivery"],
    "received_by": ["Received by", "Received By", "Signed for by", "Signed By", "Receiver"],
    "carrier_name": ["Carrier", "Carrier Name", "Delivered By"],
    "tracking_number": ["BOL, PRO, Or Tracking #", "Tracking #", "Tracking Number", "Tracking",
                        "PRO #", "BOL #"],
    "vendor_name": ["Vendor / Shipper's Name", "Vendor/Shipper's Name", "Shipper's Name",
                    "Shipper", "Vendor Name"],
}
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

OCR_UPLOAD_FORMATS = frozenset({"JPEG", "PNG", "BMP", "TIFF"})
"""Image containers Document Intelligence accepts. Anything else is re-encoded before upload.

Not a guess — the service answers an unsupported container with
`400 InvalidContent: "The file is corrupted or format is unsupported."`, which reads as a broken
file and is not one. **GIF is the case that matters here**: Outlook writes an animated GIF for a
sender's signature banner, twenty-one of them reached OCR, and every one came back "corrupted".
Re-encoding the first frame to PNG makes the same bytes readable — checked against the live
resource on `image004.gif`, which failed as a GIF and returned text as a PNG.

`ocr_adapter.fit_for_upload` does the conversion, so every client path gets it.
"""

OCR_MIN_IMAGE_PIXELS = 50
OCR_MAX_IMAGE_PIXELS = 10000
"""Document Intelligence's own dimension range, per side, in pixels.

Outside it the service answers `400 InvalidContentDimensions`, which — like `InvalidContent` —
describes the request rather than the file, and left seven attachments sitting on the exception
queue under a heading that told nobody what to do. Both ends are correctable locally: a 32x32
icon scales up to 50x50 and is accepted (it reads as empty, which is the honest answer for an
icon), and an oversized scan comes down to fit.

Scaling up is not an attempt to invent detail. It exists so the *disposition* is decided by what
the image contains rather than by a request the service refused to look at.
"""

OCR_MAX_UPLOAD_BYTES = int(os.getenv("PREMIER_OCR_MAX_UPLOAD_BYTES", str(4 * 1024 * 1024)))
"""The largest body Document Intelligence will accept, which is not the same number as above.

`OCR_MAX_IMAGE_BYTES` is Azure *Vision*'s ceiling and was never re-derived when the project moved
to Document Intelligence. The pre-flight guard therefore passed a 4.67 MB photo that the service
then refused with `InvalidContentLength: "The input image is too large."` — five times, for one
forwarded picture, on a purchase order whose quantity conflict that picture is the evidence for.

The ledger draws the line exactly: the largest file ever read here is 3.09 MB and the smallest ever
refused is 4.67 MB. 4 MB is the free (F0) tier's cap; a paid tier allows 500 MB, so this is
env-overridable and a tier change is a config edit rather than a code one.

Oversized *images* are scaled to fit rather than refused — see `ocr_adapter.fit_for_upload`.
"""

RECOVER_PER_RUN = int(os.getenv("PREMIER_RECOVER_PER_RUN", "25"))
"""How many messages one run may re-fetch **by id** after the listing window has passed them.

`stage1_ingest.listing_window_start()` narrows the Graph listing to mail newer than the last clean
run. That saves paging the whole Inbox every time, and it cost 24 messages: anything never settled
whose `receivedDateTime` has fallen behind the window can never be listed again, however often the
run repeats. On 2026-09-03 those 24 reached back to 2026-08-31 and included an urgent
purchase-order email, all of them recorded in `mail_arrivals` with no `email_log` row.

Bounded because recovery is one Graph request per id. Unbounded, a mailbox with a long backlog
would turn a single run into a request storm — and the run ceiling
(`RUN_MAX_MINUTES`) would eventually stop it part way, which is a worse outcome than draining
steadily. Oldest first, so a backlog clears in the order it accumulated and the message in most
danger of never being read goes first.

Twenty-five per run against a sixty-minute schedule drains a backlog of a few hundred within a
day, while adding at most a couple of seconds to a healthy run that has nothing to recover.
"""

STORE_DECORATIVE_ATTACHMENTS = os.getenv("PREMIER_STORE_DECORATIVE", "0") == "1"
"""Whether a decorative attachment's bytes are kept in the content-addressed store.

Decorative means a sender's logo, an email-signature graphic, an inline screenshot — anything the
connector marks `dropped_decorative`. **Off by default**: these are never evidence, and the ledger
row recording that we saw one is kept either way.

Sized before deciding, because the intuition here is wrong in both directions. Content addressing
already collapses **9,954 decorative rows into 939 distinct blobs**, of which 907 are only ever
decorative and occupy **13 MB — 2% of a 654 MB store**; the other 635 MB is real attachments. So
this saves very little, and it is emphatically *not* the fix for a large `state/` directory.

What it does buy is that the store stays what it claims to be: the place delivery evidence lives.

The classifier is safe to trust here, and that was checked rather than assumed: the largest
attachment it has ever called decorative is **60 KB**, and all 10,051 of them are under 100 KB. A
photographed POD is megabytes, so nothing that could be evidence lands in this bucket.

**The cost, and the reason this is a setting rather than a deletion.** `mail_view` rewrites every
`cid:` reference in a message body to point at the attachment route, so these blobs are what render
a message's inline images. With this off, email bodies from new mail show a broken image where the
sender's logo was. Mail already ingested is unaffected. Set `PREMIER_STORE_DECORATIVE=1` to get the
old behaviour back without a code change.
"""

OCR_PAGES_PER_RUN = int(os.getenv("PREMIER_OCR_PAGES_PER_RUN", "150"))
"""How many pages one ingest run may send to OCR before it starts refusing.

`BudgetedOcrClient` is the only real spend stop in this project, and its ceiling used to come from
`tools.ingest_corpus.DEFAULT_MAX_OCR_PAGES` — a *tool* default of 15, written for driving a corpus
by hand from a terminal. `operations/runner.py` never overrode it, so every scheduled run against
the live mailbox silently stopped after fifteen pages and recorded the rest as
`service_unavailable`. That is how 104 attachments came to be sitting in the ledger reading "OCR
page budget of 15 exhausted" while the credentials were valid and Azure was answering.

The number a production run uses belongs here, next to the other spend caps, where it can be seen
and changed without editing a tool. It is still a ceiling and still refuses past it — the point is
that the refusal is now a decision somebody made about this mailbox, not a leaked default.
"""

ENABLE_AI_FALLBACK = False   # checklist #1b — off until Premier explicitly permits Claude on real content
ANTHROPIC_MODEL = "claude-sonnet-5"
AI_FALLBACK_CONFIDENCE_CAP = 0.5

AZURE_DOC_INTELLIGENCE_CONFIDENCE_CAP = 0.85

# Retry policy. Only *retryable* failures reach it — `ocr_adapter._is_retryable` sends 429/5xx and
# dropped connections here and fails a 400 immediately, because re-sending an oversized file to be
# refused a second time spends an attempt to learn nothing.
#
# One attempt at a flat 2s used to be the whole policy, and 77% of every OCR failure in the ledger
# was a 429: Azure's throttle window is routinely longer than two seconds, so the single retry
# landed inside the same window and gave up. Backoff is now exponential with jitter, and
# `Retry-After` wins over both when the service sends one.
AZURE_OCR_RETRY_COUNT = 4
AZURE_OCR_RETRY_BACKOFF_SECONDS = 2
AZURE_OCR_RETRY_MAX_SECONDS = 60         # a single wait; the deadline below bounds the total

OCR_MIN_SECONDS_BETWEEN_CALLS = float(os.getenv("PREMIER_OCR_MIN_CALL_GAP", "3"))
"""Floor on the gap between analyze submissions, across every caller.

The OCR path has never been concurrent and still overran the quota: a loop over one email's inline
images fires them back to back, and each analysis costs a POST plus several poll GETs against the
same allowance. Three seconds is ~20 calls a minute, which is what the free tier allows. Raise it
if 429s reappear; set it to 0 on a tier that does not need pacing.
"""

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

DESC_MATCH_THRESHOLD = 80   # RapidFuzz token_set_ratio, 0-100

# How far clear of the runner-up the best description score must be before it is allowed to
# resolve a line on its own. A score alone says "this line is plausible"; only the gap says "and
# no other line is". Measured on the 92-record corpus: 12 keeps every genuine resolution and
# refers the four real ties ("Exit" and "Accesible Lift" both score 100 against 8 and 23
# candidates on PO 907514 — the email genuinely does not say which sign arrived).
DESC_MATCH_GAP = 12

MATERIAL_COST_CODE_PREFIX = "1"   # shared with Stage 5

# --- Stage 5: Verify ---------------------------------------------------------

CBD_ADR_RECEIPT_WINDOW_DAYS = 30

# --- Stage 6: Build & Log -------------------------------------------------

STAGED_BY_USER = "CC-Automation"

# --- Mailbox folders we read from ---------------------------------------------
# Graph well-known folder names, used verbatim as URL segments by `connectors/mailbox.py` and
# `operations/arrivals.py`.
#
# Junk is here because Exchange put a real delivery notification in it. On 2026-08-24 an Authority
# Inbound Notification for PO 912614 was junked, and because both readers named `Inbox` in their
# URLs it left no row in `mail_arrivals`, `email_log` or `mail_body` — a receiver nobody creates,
# with nothing on any screen to say why. Reading Junk is the safety net; the cure is a safe-sender
# rule on Premier's tenant, and `email_log.source_folder` is what gives someone the evidence to
# ask for one.
#
# Order matters only for readability. Both readers dedupe on `internetMessageId`, which is stable
# across folders, so a message listed twice costs one set lookup.
MAILBOX_SOURCE_FOLDERS = ("inbox", "junkemail")

# --- Mailbox folder routing (post-processing organization, both connectors) --
# Every email process_new_mail finishes with gets moved out of its source folder into one of
# these, so the source folders only ever hold genuinely new/unprocessed mail and a human can audit
# any category by looking at the folder — see ORCHESTRATOR_DESIGN.md's "every decision traceable"
# principle.

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

GRAPH_AUTH_TIMEOUT_SECONDS = int(os.getenv("PREMIER_GRAPH_AUTH_TIMEOUT_SECONDS", "30"))
"""How long a token request to login.microsoftonline.com may hang before it is given up on.

Every Graph *data* call has carried a timeout since the connector was written. The token call did
not: MSAL builds its own `requests.Session` and, unless it is handed an HTTP client, posts without
a `timeout` at all. A socket to the login endpoint that stops answering — the usual cause is the
machine suspending mid-run — therefore blocks forever rather than raising.

That is not theoretical. Run 1064 sat inside this call from 02:15 to 14:16 (twelve hours) and run
923 from 02:11 to 14:10 the following day (thirty-six), each holding the runner's lock the whole
time, each ending in `ConnectionError` the moment the machine woke. The console read "Running
now..." for half a day at a stretch and every scheduled run behind it was refused with "a run is
already in progress".

Thirty seconds is the same ceiling the data calls use. A token request that has not answered in
that time is not going to.
"""

RUN_MAX_MINUTES = int(os.getenv("PREMIER_RUN_MAX_MINUTES", "60"))
"""Wall-clock ceiling on a single ingest run, enforced between emails.

A belt to the timeout's braces, and deliberately a *cooperative* stop rather than a thread kill:
the runner hands this to `should_stop`, which is polled at the same committed boundary the kill
switch is, so a run that overruns stops with the database consistent and the remaining mail simply
unseen. It bounds the run at this many minutes plus one email's work.

Sized against the measured distribution of runs that actually did work (`error IS NULL AND
emails > 0`, the hung ones excluded): n=73, minimum 4s, **median 68s, maximum 2,016s — 33.6
minutes**, that longest one a 103-email pass staging 199 records.

Sixty rather than thirty, because thirty is *below* that observed maximum and would have cut a
legitimate heavy pass off mid-mailbox. Sixty leaves ~1.8x headroom and equals the current schedule
interval, which is the useful principle here: a run that outlives its own interval is broken by
definition, whatever it is doing. It still stops the twelve- and thirty-six-hour hangs dead.

Do not lower this to fit a fast machine. The number that matters is the slowest *legitimate* run,
and mail volume after an outage is far spikier than the median suggests.
"""

RUN_STALL_MINUTES = int(os.getenv("PREMIER_RUN_STALL_MINUTES", "15"))
"""A run that reports no activity at all for this long is abandoned, whatever it is doing.

`RUN_MAX_MINUTES` cannot do this job on its own. It is *polled*, so it only fires when the run
reaches a point that asks — and a run blocked inside one call never asks. Run 1179 sat in "Reading
the mailbox" for over twelve minutes on 2026-09-15 with nothing able to reach it: not the ceiling,
not the kill switch, and it was holding the scheduler's own thread, so the new-mail watch froze
behind it too. This is measured from the outside, by `operations.scheduler`, off the heartbeat
every Graph call and every email refreshes.

**Fifteen, because it has to clear the slowest single step that is still alive.** One attachment
fetch is bounded at 60 s read, retried three times with `Retry-After` capped at
`GRAPH_RETRY_AFTER_CAP_SECONDS` — about six minutes worst case before the heartbeat moves — and
one photographed attachment through OCR is bounded at 180 s submit plus 120 s poll. A stall timer
shorter than a step that is merely slow would abandon healthy runs.
"""

RUN_STOP_GRACE_SECONDS = int(os.getenv("PREMIER_RUN_STOP_GRACE_SECONDS", "120"))
"""How long "Stop this run" waits for the run to stop itself before abandoning it.

A stop is cooperative first: it lands between Graph calls while reading, and between emails while
processing, so the database is consistent when it does. If the run has not let go after this long,
it is not going to soon, and the button is not allowed to be a request somebody watches fail — the
run is abandoned and the automation is free to run again.
"""

GRAPH_RETRY_AFTER_CAP_SECONDS = int(os.getenv("PREMIER_GRAPH_RETRY_AFTER_CAP_SECONDS", "30"))
"""The longest a throttled Graph call will sleep on the server's `Retry-After` before retrying.

urllib3 obeys `Retry-After` with no upper bound, so a single throttle response naming an hour held
the run for an hour inside one call — unreachable by any stop and invisible on screen. Capped, a
throttled call that is still throttled after its retries fails and is recorded, and the next run
picks the mail up; waiting longer than this has never been the difference between reading it and
not.
"""

# --- Spitfire sfPMS (ERP, read-only) ------------------------------------------
# Training instance, sfPMS 2023.0.9692.36214. `GET /api/system/version` answers anonymously from
# outside Premier's network, so the connector needs no VPN for training; production may differ.
# Credentials are blank until Premier provisions `svc-receiver-automation`. For this phase that
# account needs READ permission only — purchase orders and their lines. Nothing else.

# No default. A real host in source is production data (§12), and a wrong default is worse than
# an absent one — the connector fails with a clear message rather than pointing somewhere unknown.
SPITFIRE_BASE_URL = os.getenv("SPITFIRE_BASE_URL") or ""
# The account every Spitfire call authenticates as (`POST /api/Account`). Training and Production
# each have their own; the code is identical, only these values and SPITFIRE_BASE_URL differ.
# When both are set they win over SPITFIRE_SESSION_COOKIE, and `connectors/spitfire_auth.py`
# re-reads `.env` on every login, so changing a password means editing `.env` and nothing else.
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

# A borrowed browser session — development fallback only, used when SPITFIRE_UID/PW are unset.
# It cannot renew itself. Paste the value of the `sfPMSAuth` cookie (NOT `sfSession`, which is
# only a session id).
#
# Short-lived: forms tickets lapse and Spitfire enforces an idle timeout, so this is for one-off
# pulls, never the scheduled pipeline. Everything read is attributed to whoever owns the session.
SPITFIRE_SESSION_COOKIE = os.getenv("SPITFIRE_SESSION_COOKIE") or None

# PO discovery needs a project ID, and this account cannot enumerate projects — `POST /api/projects`
# returns 200 with zero rows and `GET /api/projects` is 405, so the searchable set is whatever is
# listed here. Real project codes are Premier's data and live in `.env`, never in source (§12).
#
# This is a real boundary, not a default worth guessing at: a PO in a project absent from this
# list does not resolve, and the UI says so plainly rather than claiming the PO does not exist.
# Remove the workaround entirely once Premier grants the automation account project membership,
# which would make /api/projects answer for itself.
# The default is synthetic, for the same reason the domain lists above are: a PO outside the list
# is refused *by name*, and a test proving that message needs a non-empty list to be outside of.
# Real project codes come from `.env`; these placeholders match no real project.
SPITFIRE_PROJECT_IDS = [
    p.strip() for p in os.getenv(
        "SPITFIRE_PROJECT_IDS", "PRJ001PB100003,PRJ001PB100002,PRJ002PB100002",
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
