"""Run the real corpus through the real orchestrator, into the real database.

Distinct from `tools/run_corpus.py`, which scores the pipeline against ground truth in memory
and never persists anything. This one calls `ingest_orchestrator.process_new_mail` and writes to
`state/sample_state.sqlite3` — the testing store the `/ui` pages read. Usage:

    python -m tools.ingest_corpus --reset                 # mock OCR, spends nothing
    python -m tools.ingest_corpus --dry-run-ocr           # what a real run would cost
    python -m tools.ingest_corpus --ocr azure --yes --reset
    python -m tools.ingest_corpus --purge-all --reset     # empty every table first (confirms)

Two facts about the pipeline shape this tool, and both are easy to be bitten by:

1. **OCR is not gated by triage.** `evidence.gather()` reads every attachment *before* triage
   decides anything, and `process_new_mail` never passes `allow_ocr=False`. So there is no
   verdict that can save a photograph from an OCR call — the page budget here is the only real
   backstop. Hence mock-by-default and an explicit confirmation for `--ocr azure`.
2. **Seen ids are recorded permanently.** A second run over the same corpus therefore processes
   zero emails and looks broken. `--reset` is the answer, and the absence of it is reported rather
   than left to be inferred.

This tool cannot reach Premier's live mail. It writes only to the sample store; the live mailbox
has its own file, filled by `tools/ingest_mailbox.py`. That separation is physical rather than a
filter, so no query, reset or purge here can put sample rows on a real receiver report — which is
the mistake this layout exists to make impossible.

`--reset` forgets this corpus's own emails so it can be re-run (seen ids are permanent, so a
second run otherwise processes nothing and looks broken). `--purge-all` empties the sample store
outright; it is now safe by construction, and still asks first.
"""

import argparse
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from config import settings
from connectors.mailbox import Mailbox
from connectors.msg_file import MsgFileMailbox
from pipeline import attachment_ledger, email_log, ingest_orchestrator, read_views, state_db
from pipeline.parsing import sniff
from pipeline.stage3_extract import ocr_adapter
from pipeline.stage3_extract.ocr_adapter import (
    DocumentIntelligenceClient,
    OcrBudgetExhausted,
    OcrQuotaExhausted,
    OcrResult,
    build_client,
)

DEFAULT_CORPUS = Path(r"D:\Premier\Documents\Premier\5,8 june")
DEFAULT_MAX_OCR_PAGES = 15
# The sample store, never the live one. Hard-defaulted here so a bare `python -m
# tools.ingest_corpus` cannot write test data into Premier's real mail.
DEFAULT_DB_PATH = settings.SAMPLE_STATE_DB_PATH


class BudgetedOcrClient(DocumentIntelligenceClient):
    """Wraps the resolved OCR client, counts pages, and refuses past the budget.

    Passing an explicit client into `process_new_mail` is also what stops `build_default_adapters`
    from resolving Azure on its own — so this class is both the meter and the switch.

    It raises rather than returning an empty result on purpose: `dispatch` records the failure
    against the attachment's ledger row with the reason text, so a budget stop shows on the
    manual page as "we stopped, and here is why" instead of "the image was blank".
    """

    def __init__(self, inner: DocumentIntelligenceClient, max_pages: int):
        self.inner = inner
        self.max_pages = max_pages
        self.calls = 0
        self.refused = 0
        self.quota_exhausted = False
        """Latched once Azure says the account is out of call volume.

        A second meter beside `max_pages`, and a different kind of limit: the budget is ours and
        is about spending, this is the provider's and is about a subscription that has nothing
        left to spend. Neither can be recovered from inside the run.

        It latches because the failure is account-wide. Without it every remaining attachment was
        tried anyway — one HTTP round trip each, all refused identically — which is how 580 ledger
        rows came to read `HTTP 403: Out of call volume quota` for what was one fact about the
        subscription. Latched, the first one pays for the discovery and the rest are refused
        locally, in microseconds, with the same reason recorded.
        """

    def analyze(self, content_bytes: bytes) -> OcrResult:
        if self.quota_exhausted:
            self.refused += 1
            raise OcrQuotaExhausted(
                "OCR quota exhausted — the account has no call volume left this period, so this "
                "page was not sent. Raise the tier, then recover the backlog with tools.reextract"
            )
        if self.calls >= self.max_pages:
            self.refused += 1
            raise OcrBudgetExhausted(
                f"OCR page budget of {self.max_pages} exhausted — re-run with a higher "
                f"--max-ocr-pages if this document is worth the spend"
            )
        self.calls += 1
        try:
            result = self.inner.analyze(content_bytes)
            # Charged after the fact, because how many pages a document has is not knowable until
            # the service has read it. `calls` is pre-incremented so a document that raises still
            # costs one — the request went out and was billed either way — and the correction below
            # only ever adds the extra pages of a multi-page scan.
            self.calls += max(0, getattr(result, "pages", 1) - 1)
            return result
        except Exception as exc:                                   # noqa: BLE001
            # Latch and re-raise unchanged: `dispatch` still records this attempt against its own
            # ledger row with Azure's own wording, which is the row that carries the evidence.
            # Only what happens to the *next* page changes.
            if ocr_adapter.is_quota_exhausted(exc):
                self.quota_exhausted = True
            raise


@dataclass
class RunSummary:
    corpus: Path
    msg_files: int = 0
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
    source_files_moved: int = 0
    reset_deleted: Dict[str, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0


def count_ocr_exposure(mailbox: Mailbox) -> int:
    """How many attachments would reach the OCR client, counted without calling anything.

    Deliberately reads the mailbox rather than guessing from file extensions: an attachment's
    kind is decided by `sniff`, and the corpus photographs arrive with no declared content type
    at all. Takes a `Mailbox` rather than a folder so `tools/ingest_mailbox.py` can price a live
    Graph poll with the same counter — which matters more there, since those pages cost money.
    """
    exposure = 0
    for email in mailbox.fetch_new():
        for attachment in email.attachments:
            if attachment.drop_hint:
                continue
            kind = attachment.sniffed_kind or sniff.sniff(
                attachment.content_bytes, attachment.filename, attachment.content_type or ""
            ).kind
            if kind == sniff.KIND_IMAGE:
                exposure += 1
    return exposure


def run_once(
    corpus_dir: Path = DEFAULT_CORPUS,
    *,
    ocr: str = "mock",
    max_ocr_pages: int = DEFAULT_MAX_OCR_PAGES,
    reset: bool = False,
    purge_all: bool = False,
    db_path=DEFAULT_DB_PATH,
    should_stop=None,
    on_progress=None,
) -> RunSummary:
    corpus_dir = Path(corpus_dir)
    started = time.monotonic()
    summary = RunSummary(corpus=corpus_dir, ocr_client=ocr)
    summary.msg_files = len(list(corpus_dir.glob("*.msg")))

    conn = state_db.get_connection(db_path)
    try:
        # read_only=True is not optional. Without it, one run scatters Premier's original .msg
        # files into Processed/ Routed/ Hidden/ subfolders of the folder they were given to us in.
        mailbox = MsgFileMailbox(corpus_dir, read_only=True)

        if reset:
            # Scoped to this corpus's own emails rather than `DELETE FROM` every table. The
            # store is the corpus's alone now, so the distinction no longer protects live mail —
            # it protects anything else a future corpus run adds here. The ids are stable across
            # runs (Message-ID, else a hash of the file bytes), so this is exactly the set about
            # to be processed.
            summary.reset_deleted = state_db.forget_emails(
                conn, [email.email_id for email in mailbox.fetch_new()]
            )
        if purge_all:
            for table in ("seen_message_ids", "email_log", "extracted_records",
                          "attachment_ledger", "accumulation", "released_events"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()

        client = BudgetedOcrClient(build_client(ocr), max_pages=max_ocr_pages)

        # Scoped to what this pass did rather than read off table totals: `--reset` aside, the
        # store accumulates across runs, so a total would report history as though it were now.
        log_rows_before = email_log.count(conn)

        summary.records_staged = ingest_orchestrator.process_new_mail(
            mailbox, ocr_client=client, conn=conn, should_stop=should_stop,
            on_progress=on_progress,
        )
        summary.ocr_calls = client.calls
        summary.ocr_refused = client.refused
        summary.email_log_rows = email_log.count(conn) - log_rows_before
        summary.orphans = len(attachment_ledger.orphans(conn))
        summary.emails_processed = len(mailbox.routed_to)
        summary.folders = dict(Counter(mailbox.routed_to.values()))
        # `routed_to` records every settled email whether or not the file moved, so in read-only
        # mode nothing moved by definition; anything else means the corpus folder was touched.
        summary.source_files_moved = 0 if mailbox.read_only else len(mailbox.routed_to)
    finally:
        conn.close()

    summary.elapsed_seconds = time.monotonic() - started
    return summary


def folder_counts(conn) -> Dict[str, int]:
    """Public because `tools/ingest_mailbox.py` prints the same breakdown."""
    return {
        row[0]: row[1]
        for row in conn.execute("SELECT folder, COUNT(*) FROM email_log GROUP BY folder ORDER BY 1")
    }


def _print_summary(summary: RunSummary, db_path) -> None:
    print(f"corpus:    {summary.corpus}  ({summary.msg_files} .msg files)")
    print(f"database:  {db_path}")
    if summary.reset_deleted:
        print("forgot:    " + " · ".join(
            f"{table} {count}" for table, count in summary.reset_deleted.items() if count
        ) + "   <- this corpus's rows only")
    print(f"emails:    {summary.emails_processed} processed this run")
    print("folders:   " + (" · ".join(f"{k} {v}" for k, v in sorted(summary.folders.items()))
                           or "none"))
    print(f"records:   {summary.records_staged} staged")
    print(f"ocr:       {summary.ocr_calls} page(s) sent (client={summary.ocr_client}, "
          f"budget={DEFAULT_MAX_OCR_PAGES}, refused={summary.ocr_refused})")
    print(f"orphans:   {summary.orphans}          <- must be 0")
    print(f"email_log: +{summary.email_log_rows} rows  <- must equal emails processed")
    print(f"moved:     {summary.source_files_moved} source file(s)  <- must be 0 (read-only corpus)")
    print(f"elapsed:   {summary.elapsed_seconds:.1f}s")

    if summary.emails_processed == 0:
        print("\n0 emails processed — every message id is already in seen_message_ids. "
              "Re-run with --reset to process the corpus again.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--ocr", default="mock", choices=["mock", "azure", "tesseract", "auto"],
                        help="OCR client. Defaults to mock, which never spends an Azure page.")
    parser.add_argument("--max-ocr-pages", type=int, default=DEFAULT_MAX_OCR_PAGES,
                        help="hard ceiling on billed pages for this run")
    parser.add_argument("--reset", action="store_true",
                        help="forget this corpus's own emails first, so it is reprocessed. "
                             "Anything else in the sample store is untouched.")
    parser.add_argument("--purge-all", action="store_true",
                        help="empty every table in the sample store, not just this corpus's "
                             "rows. Cannot reach live mailbox data — that is a separate file.")
    parser.add_argument("--dry-run-ocr", action="store_true",
                        help="report how many pages a real OCR run would send, then exit")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation for --ocr azure")
    args = parser.parse_args()

    if not args.corpus.exists():
        print(f"corpus folder not found: {args.corpus}", file=sys.stderr)
        return 1

    if args.dry_run_ocr:
        exposure = count_ocr_exposure(MsgFileMailbox(args.corpus, read_only=True))
        billed = min(exposure, args.max_ocr_pages)
        print(f"corpus:   {args.corpus}")
        print(f"images that would reach the OCR client: {exposure}")
        print(f"pages that would actually be sent:      {billed} (budget {args.max_ocr_pages})")
        print("\nNothing was sent. This was a local count.")
        return 0

    if args.ocr in ("azure", "auto") and not args.yes:
        exposure = min(
            count_ocr_exposure(MsgFileMailbox(args.corpus, read_only=True)), args.max_ocr_pages
        )
        endpoint = settings.AZURE_DOC_INTELLIGENCE_ENDPOINT or "(not configured)"
        print(f"This run will send up to {exposure} page(s) to Azure Document Intelligence "
              f"at {endpoint}.")
        answer = input("Continue? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted. Nothing was sent.")
            return 1

    if args.purge_all and not args.yes:
        conn = state_db.get_connection(DEFAULT_DB_PATH)
        try:
            total = email_log.count(conn)
        finally:
            conn.close()
        print(f"--purge-all empties every table in the sample store {DEFAULT_DB_PATH}, "
              f"including {total} email(s) already recorded — live mailbox mail among them. "
              f"--reset alone re-runs this corpus without touching anything else.")
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Aborted. Nothing was deleted.")
            return 1

    summary = run_once(
        args.corpus, ocr=args.ocr, max_ocr_pages=args.max_ocr_pages, reset=args.reset,
        purge_all=args.purge_all,
    )
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
