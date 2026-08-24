"""Render the receiver report to PDF, for attaching to a Spitfire receipt.

Chrome, because nothing else is available: WeasyPrint, Playwright, pdfkit and PyMuPDF are all
absent from `.venv` and none is worth adding as a dependency for one document a delivery.

Kept out of `receipt_log.py` on purpose. That module is pure — sqlite in, strings and bytes out,
no subprocesses and no temp files — and it is imported by the Receiver report page, which must
not be able to spawn a browser. This is the only file that shells out.

**The flags are not adjustable folklore.** Each one is here because its absence produced no file
at all, on this machine, more than once:

* `--user-data-dir` — without it Chrome exits silently, writes nothing, and returns 0.
* a **freshly named** profile per render — a stale or locked shared directory produced nothing on
  both passes of one run while a random name succeeded first time. A warm profile can also serve
  the HTML from cache and re-emit the *previous* PDF at an identical byte count with a new
  timestamp, which is the worst failure of the set because it looks like success.
* `--run-all-compositor-stages-before-draw` and `--virtual-time-budget` — otherwise the page is
  captured before layout settles and tables come out mid-render.
* `--no-pdf-header-footer` — Chrome's default footer stamps a `file:///` path across the bottom
  of a document that goes to Premier.

Chrome reports `NNNNN bytes written to file <path>` on **stderr** and exits 0, so stderr is
captured and quoted in the error when no file appears.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import List, Optional

_logger = logging.getLogger(__name__)

_CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)

RENDER_TIMEOUT_SECONDS = 90


class PdfRenderUnavailable(RuntimeError):
    """No Chrome to render with.

    Its own type so the post chain can say *"the report could not be built"* and stop before
    creating anything, rather than posting a receipt with no report and leaving somebody to
    notice later that half the evidence is missing.
    """


def chrome_path() -> Optional[str]:
    """The browser to render with, or None. `shutil.which` last so a PATH entry can override."""
    for candidate in _CHROME_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def is_available() -> bool:
    return chrome_path() is not None


def render(html: str, *, timeout: int = RENDER_TIMEOUT_SECONDS) -> bytes:
    """HTML string in, PDF bytes out.

    Everything lives in one temp directory that is removed afterwards, including the Chrome
    profile — which is what makes each render use a fresh one without accumulating directories
    under TEMP.
    """
    browser = chrome_path()
    if not browser:
        raise PdfRenderUnavailable(
            "no Chrome or Chromium was found, and no Python PDF library is installed in this "
            "environment — the receiver report cannot be rendered")

    workspace = Path(tempfile.mkdtemp(prefix="premier-report-"))
    source = workspace / "report.html"
    target = workspace / "report.pdf"
    # Unique per render. A shared profile is the documented way this silently produces nothing.
    profile = workspace / f"chrome-profile-{uuid.uuid4().hex[:8]}"
    source.write_text(html, encoding="utf-8")

    args: List[str] = [
        browser, "--headless", "--disable-gpu", "--no-sandbox",
        f"--user-data-dir={profile}",
        "--run-all-compositor-stages-before-draw",
        "--virtual-time-budget=6000",
        "--no-pdf-header-footer",
        f"--print-to-pdf={target}",
        source.as_uri(),
    ]
    try:
        completed = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
        if not target.exists() or target.stat().st_size == 0:
            # stderr is where Chrome says what happened, including on success.
            detail = (completed.stderr or b"").decode("utf-8", "replace").strip()[:400]
            raise RuntimeError(
                f"Chrome exited {completed.returncode} without writing a PDF"
                + (f": {detail}" if detail else " and said nothing on stderr"))
        return target.read_bytes()
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Chrome did not finish rendering within {timeout}s") from exc
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# --- The document ------------------------------------------------------------------------------
# The house style, copied rather than restyled: Premier's reports are meant to read as one family.
# Crimson #A6192E, A4 with 14mm/12mm margins, 8.6pt Segoe UI, and `page-break-inside: avoid` on
# rows so a delivery is never split across a page boundary.

_STYLE = """
@page { size: A4; margin: 14mm 12mm; }
* { box-sizing: border-box; }
body { font-family: "Segoe UI", Arial, sans-serif; font-size: 8.6pt; color: #1a1a1a; margin: 0; }
h1 { font-size: 15pt; color: #A6192E; margin: 0 0 2mm; }
h2 { font-size: 10pt; color: #A6192E; margin: 6mm 0 2mm; }
.sub { color: #555; font-size: 8pt; margin: 0 0 4mm; }
.callout { border-left: 3px solid #A6192E; background: #faf5f6; padding: 2.5mm 3mm;
           margin: 0 0 4mm; font-size: 8.2pt; }
table { width: 100%; border-collapse: collapse; margin: 0 0 4mm; }
th { background: #A6192E; color: #fff; text-align: left; padding: 1.6mm 2mm;
     font-size: 7.8pt; font-weight: 600; }
td { padding: 1.4mm 2mm; border-bottom: 1px solid #e3e3e3; vertical-align: top; }
tr { page-break-inside: avoid; }
.num { text-align: right; }
.foot { margin-top: 6mm; color: #777; font-size: 7.4pt; border-top: 1px solid #ddd;
        padding-top: 2mm; }
"""


def document(body_html: str, *, title: str, subtitle: str = "", note: str = "") -> str:
    """Wrap report HTML in the house style.

    `body_html` is `receipt_log.to_html()` output, which is already fully escaped — that module
    escapes everything it interpolates precisely so its callers can wrap it without re-escaping.
    Nothing untrusted is added here.
    """
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>{title}</title><style>{_STYLE}</style></head><body>",
        f"<h1>{title}</h1>",
    ]
    if subtitle:
        parts.append(f"<p class='sub'>{subtitle}</p>")
    if note:
        parts.append(f"<div class='callout'>{note}</div>")
    parts.append(body_html)
    parts.append("</body></html>")
    return "".join(parts)
