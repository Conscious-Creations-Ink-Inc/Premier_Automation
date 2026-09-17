"""Run the live Microsoft 365 receiving mailbox through the real orchestrator.

The Graph sibling of `tools/ingest_corpus.py`. Same pipeline, same summary — the only difference
is where the mail comes from, which is the entire point of the `Mailbox` seam.

Different database, though, and deliberately so: this writes `state/pipeline_state.sqlite3`, the
live store, while the corpus tool writes `state/sample_state.sqlite3`. Test data and Premier's
real mail are separated by file rather than by a column, so nothing that queries one can see the
other.

    python -m tools.ingest_mailbox --preflight        # auth + folders + per-folder counts, reads no mail
    python -m tools.ingest_mailbox --dry-run-ocr      # what a real run would bill
    python -m tools.ingest_mailbox                    # read-only shadow, mock OCR  (the default)
    python -m tools.ingest_mailbox --ocr azure --yes  # real OCR, still moves nothing
    python -m tools.ingest_mailbox --move-mail        # production: files mail into the 4 folders

**Read-only is the default and `--move-mail` is deliberately awkward to type.** This reads
Premier's real mailbox. Without the guard, one run moves every message out of their Inbox and
creates four folders in it — visible to them within seconds and tedious to undo. The shadow run
produces every verdict, every ledger row and every staged record; the only thing it does not do
is touch their mail.

Two traps carried over from the corpus runner, both worse here:

1. **OCR is not gated by triage.** `evidence.gather()` reads every attachment *before* triage
   decides anything, so no verdict can save a photograph from an OCR call. Against a live mailbox
   that is real money on mail we may well discard. Mock by default, budget always.
2. **Mail is only skipped once it has been processed.** `seen_message_ids` is now written at the
   end of each email rather than while fetching (`state_db.mark_seen`), so an interrupted run
   resumes correctly. But in read-only mode nothing leaves the Inbox, which makes that marker the
   *only* thing preventing a full reprocess — do not clear it casually.
"""

import argparse
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Optional

import requests

from config import settings
from connectors.mailbox import GRAPH_BASE_URL, GraphMailbox
from pipeline import attachment_ledger, email_log, ingest_orchestrator, read_views, state_db
from pipeline.stage3_extract.ocr_adapter import build_client
from tools.ingest_corpus import (
    DEFAULT_MAX_OCR_PAGES,
    BudgetedOcrClient,
    count_ocr_exposure,
    folder_counts,
)

# The live store. The corpus tool's own DEFAULT_DB_PATH is the sample one; deliberately not
# imported above, so the two names can never be confused for each other at a call site.
DEFAULT_DB_PATH = settings.PIPELINE_STATE_DB_PATH


@dataclass
class MailboxRunSummary:
    mailbox_address: str
    read_only: bool
    emails_processed: int = 0
    records_staged: int = 0
    ocr_client: str = ""
    ocr_calls: int = 0
    ocr_refused: int = 0
    ocr_quota_exhausted: bool = False
    """Azure reported the account out of call volume during this run.

    Carried up so the run records it once, as a run-level fact, instead of leaving it to be
    inferred from a wall of identical `service_unavailable` ledger rows."""
    email_log_rows: int = 0
    orphans: int = 0
    folders: Dict[str, int] = field(default_factory=dict)
    messages_moved: int = 0
    would_have_moved: Dict[str, str] = field(default_factory=dict)
    elapsed_seconds: float = 0.0


def preflight(mailbox: Optional[GraphMailbox] = None) -> int:
    """Prove the credentials, the policy scoping and the address before touching any mail.

    Reads no message bodies and no attachments, so it costs nothing and cannot trip the OCR path.
    A 403 here is the Application Access Policy talking; a 404 means the address is wrong.
    """
    mailbox = mailbox or GraphMailbox(read_only=True)
    print(f"mailbox:   {mailbox.mailbox_address}")

    headers = mailbox._headers()          # raises with Graph's own error text if auth fails
    print("token:     acquired")

    folders_url = f"{GRAPH_BASE_URL}/users/{mailbox.mailbox_address}/mailFolders"
    resp = requests.get(folders_url, headers=headers, params={"$top": 100}, timeout=30)
    resp.raise_for_status()
    folders = resp.json().get("value", [])
    print(f"folders:   {len(folders)} — " + ", ".join(
        f"{f.get('displayName')} ({f.get('totalItemCount', '?')})" for f in folders
    ))

    routing = {
        settings.MAILBOX_FOLDER_HIDDEN, settings.MAILBOX_FOLDER_ROUTED,
        settings.MAILBOX_FOLDER_PROCESSED, settings.MAILBOX_FOLDER_ERRORS,
        settings.MAILBOX_FOLDER_QUARANTINE,
    }
    existing = {f.get("displayName") for f in folders}
    missing = sorted(routing - existing)
    print("routing:   " + (f"{', '.join(missing)} do not exist yet "
                           f"(created on the first --move-mail run)" if missing
                           else "all present"))

    # One line per source folder, so a run says how much Junk it is about to read rather than
    # leaving that to be inferred from the record count afterwards.
    for source in mailbox.folders:
        count_url = (f"{GRAPH_BASE_URL}/users/{mailbox.mailbox_address}"
                     f"/mailFolders/{source}/messages/$count")
        count_resp = requests.get(count_url, headers={**headers, "ConsistencyLevel": "eventual"},
                                  timeout=30)
        if count_resp.ok:
            print(f"{source + ':':11}{count_resp.text.strip()} message(s)")
        else:
            print(f"{source + ':':11}count unavailable (HTTP {count_resp.status_code}); "
                  f"the ingest run will still page through it")

    print("\nNothing was read and nothing was moved.")
    return 0


def run_once(
    *,
    ocr: str = "mock",
    max_ocr_pages: int = DEFAULT_MAX_OCR_PAGES,
    read_only: bool = True,
    db_path=DEFAULT_DB_PATH,
    should_stop=None,
    on_progress=None,
) -> MailboxRunSummary:
    started = time.monotonic()
    mailbox = GraphMailbox(read_only=read_only)
    # Handed to the connector as well as to the orchestrator. The orchestrator only asks between
    # emails, and the whole mailbox read happens before the first email — so without this a stop
    # could not reach a run until every message and attachment had downloaded.
    mailbox.should_stop = should_stop
    if on_progress is not None:
        read = {"calls": 0}

        def _reading(note: str) -> None:
            read["calls"] += 1
            on_progress("reading", read["calls"], 0, note)

        mailbox.on_activity = _reading
    summary = MailboxRunSummary(
        mailbox_address=mailbox.mailbox_address, read_only=read_only, ocr_client=ocr,
    )

    conn = state_db.get_connection(db_path)
    try:
        # Every state table accumulates across runs, and a live poll is incremental — it sees
        # only what arrived since last time. Reporting table totals as this run's result (which
        # the corpus runner can get away with, because `--reset` empties them first) would claim
        # a poll processed every email the pipeline has ever seen.
        log_rows_before = email_log.count(conn)

        client = BudgetedOcrClient(build_client(ocr), max_pages=max_ocr_pages)
        summary.records_staged = ingest_orchestrator.process_new_mail(
            mailbox, ocr_client=client, conn=conn, should_stop=should_stop,
            on_progress=on_progress,
        )
        summary.ocr_calls = client.calls
        summary.ocr_refused = client.refused
        summary.ocr_quota_exhausted = client.quota_exhausted
        summary.email_log_rows = email_log.count(conn) - log_rows_before
        summary.orphans = len(attachment_ledger.orphans(conn))

        # `routed_to` is this run's own record: one entry per email settled, whether or not the
        # move was actually performed. In read-only mode that makes it the shadow answer to
        # "what would production have done with this mail".
        summary.emails_processed = len(mailbox.routed_to)
        summary.folders = dict(Counter(mailbox.routed_to.values()))
        if read_only:
            summary.would_have_moved = dict(mailbox.routed_to)
        else:
            summary.messages_moved = len(mailbox.routed_to)
    finally:
        conn.close()

    summary.elapsed_seconds = time.monotonic() - started
    return summary


def _print_summary(summary: MailboxRunSummary, db_path) -> None:
    mode = "READ-ONLY shadow" if summary.read_only else "LIVE — mail is being moved"
    print(f"mailbox:   {summary.mailbox_address}   [{mode}]")
    print(f"database:  {db_path}")
    print(f"emails:    {summary.emails_processed} processed this run")
    print("folders:   " + (" · ".join(f"{k} {v}" for k, v in sorted(summary.folders.items()))
                           or "none"))
    print(f"records:   {summary.records_staged} staged")
    print(f"ocr:       {summary.ocr_calls} page(s) sent (client={summary.ocr_client}, "
          f"budget={DEFAULT_MAX_OCR_PAGES}, refused={summary.ocr_refused})")
    print(f"orphans:   {summary.orphans}          <- must be 0")
    print(f"email_log: +{summary.email_log_rows} rows  <- must equal emails processed")
    if summary.read_only:
        print(f"moved:     0 message(s)  <- must be 0 (read-only); "
              f"{len(summary.would_have_moved)} would have moved in production")
    else:
        print(f"moved:     {summary.messages_moved} message(s) filed into Premier's mailbox")

    if summary.emails_processed == 0:
        print("\n0 emails processed — either the Inbox is empty or every message id is already in "
              "seen_message_ids. In read-only mode nothing ever leaves the Inbox, so a second run "
              "processing nothing is the expected result, not a fault.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--preflight", action="store_true",
                        help="check auth, folders and Inbox count, then exit. Reads no mail.")
    parser.add_argument("--ocr", default="mock", choices=["mock", "azure", "tesseract", "auto"],
                        help="OCR client. Defaults to mock, which never spends an Azure page.")
    parser.add_argument("--max-ocr-pages", type=int, default=DEFAULT_MAX_OCR_PAGES,
                        help="hard ceiling on billed pages for this run")
    parser.add_argument("--dry-run-ocr", action="store_true",
                        help="report how many pages a real run would send, then exit")
    parser.add_argument("--move-mail", action="store_true",
                        help="actually file mail into Hidden/Routed/Processed/Errors in Premier's "
                             "mailbox, creating those folders if absent. Off by default.")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation for --ocr azure and for --move-mail")
    args = parser.parse_args()

    try:
        if args.preflight:
            return preflight()

        if args.dry_run_ocr:
            # read_only so that merely pricing the run cannot move anything.
            exposure = count_ocr_exposure(GraphMailbox(read_only=True))
            billed = min(exposure, args.max_ocr_pages)
            print(f"mailbox:  {settings.GRAPH_MAILBOX_ADDRESS}")
            print(f"images that would reach the OCR client: {exposure}")
            print(f"pages that would actually be sent:      {billed} (budget {args.max_ocr_pages})")
            print("\nNothing was sent to OCR and no mail was moved.")
            return 0

        if args.move_mail and not args.yes:
            print(f"This run will MOVE mail in {settings.GRAPH_MAILBOX_ADDRESS} out of the Inbox "
                  f"into {settings.MAILBOX_FOLDER_HIDDEN}/{settings.MAILBOX_FOLDER_ROUTED}/"
                  f"{settings.MAILBOX_FOLDER_PROCESSED}/{settings.MAILBOX_FOLDER_ERRORS}, "
                  f"creating those folders if they do not exist.")
            if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Aborted. Nothing was moved.")
                return 1

        if args.ocr in ("azure", "auto") and not args.yes:
            exposure = min(count_ocr_exposure(GraphMailbox(read_only=True)), args.max_ocr_pages)
            endpoint = settings.AZURE_DOC_INTELLIGENCE_ENDPOINT or "(not configured)"
            print(f"This run will send up to {exposure} page(s) to Azure Document Intelligence "
                  f"at {endpoint}.")
            if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Aborted. Nothing was sent.")
                return 1

        summary = run_once(
            ocr=args.ocr, max_ocr_pages=args.max_ocr_pages, read_only=not args.move_mail,
        )
    except ValueError as e:            # GraphMailbox's own missing-config message
        print(f"{e}", file=sys.stderr)
        return 1
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        print(f"Graph returned HTTP {status}: {e}", file=sys.stderr)
        if status == 403:
            print("403 usually means the Application Access Policy does not cover this mailbox.",
                  file=sys.stderr)
        elif status == 404:
            print(f"404 usually means GRAPH_MAILBOX_ADDRESS is wrong "
                  f"({settings.GRAPH_MAILBOX_ADDRESS}).", file=sys.stderr)
        return 1

    _print_summary(summary, DEFAULT_DB_PATH)

    conn = state_db.get_connection(DEFAULT_DB_PATH)
    try:
        view = read_views.summary(conn)
        print(f"\nready to process: {view.records_ready} of {view.records_total} record(s)")
        print(f"needs a human:    {view.needs_human} item(s)")
        print("\nPages:  http://127.0.0.1:8000/ui/mails   (start with `python run_api.py`)")
    finally:
        conn.close()

    return 0 if summary.orphans == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
