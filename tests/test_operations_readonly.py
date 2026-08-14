"""Nothing here may ever write to Premier's mailbox. These tests hold that guarantee in place.

The protection is behavioural, not permissioned: the Graph app registration carries
`Mail.ReadWrite`, so the capability to move, file or mark mail exists — nothing but this code
stands between it and Premier's live Inbox. A future edit could introduce a write without anyone
noticing, so the rule is asserted here rather than trusted.

Source-level assertions on purpose. A behavioural test would need a live mailbox, and "we did not
observe a write" is a much weaker statement than "no write verb exists in the package".
"""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OPERATIONS = ROOT / "operations"

# Two files are named outright rather than left to a glob, because both have already moved once and
# a glob would have stopped covering them without a single test failing.
#
# `mail_view` moved to `pipeline/` when the message viewer became shared with the /ui pages. It
# recovers mail from Graph and from Premier's own .msg folder and renders untrusted HTML, so it is
# the single most safety-critical file these sweeps cover.
#
# `api/ui/routes.py` is where the inbox and report screens landed when the standalone console was
# folded into the one UI. It is the only place that renders live mailbox data, so it inherits the
# console app's place in these sweeps — without this line the rename would have quietly narrowed
# the guarantee to the logic modules alone.
MAIL_VIEW = ROOT / "pipeline" / "mail_view.py"
UI_ROUTES = ROOT / "api" / "ui" / "routes.py"

PYTHON_FILES = sorted(OPERATIONS.glob("*.py")) + [MAIL_VIEW, UI_ROUTES]

# Anything that could mutate a mailbox over HTTP.
WRITE_VERBS = ("post", "put", "patch", "delete")


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_operations_package_is_present():
    """If the package moves, the rest of this file would silently pass over nothing."""
    assert sorted(OPERATIONS.glob("*.py")), f"no operations modules found under {OPERATIONS}"


@pytest.mark.parametrize("path", [MAIL_VIEW, UI_ROUTES], ids=lambda p: p.name)
def test_the_named_files_are_where_these_sweeps_expect_them(path: Path):
    """A guard on the guard. Every assertion below is parametrised over a file list; if either
    named file moves, the sweeps quietly stop covering it. This turns that into a failure instead
    of silence."""
    assert path.exists(), (
        f"{path} is missing — if it moved, point these sweeps at its new home rather than "
        f"deleting this test"
    )


@pytest.mark.parametrize("path", PYTHON_FILES, ids=lambda p: p.name)
def test_no_http_write_verbs(path: Path):
    """Only GET may leave this package. A POST to Graph is how mail gets moved or marked."""
    tree = ast.parse(_source(path), filename=str(path))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr.lower() not in WRITE_VERBS:
            continue
        owner = node.func.value
        name = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "")
        # `@router.post(...)` is our own inbound route, not an outbound call.
        if name in ("requests", "httpx", "session", "client"):
            offenders.append(f"{path.name}:{node.lineno} {name}.{node.func.attr}()")
    assert not offenders, "outbound write verb found: " + ", ".join(offenders)


@pytest.mark.parametrize("path", PYTHON_FILES, ids=lambda p: p.name)
def test_never_calls_mark_processed(path: Path):
    """`mark_processed` is the move. It must never be reached from here."""
    tree = ast.parse(_source(path), filename=str(path))
    calls = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "mark_processed"
    ]
    assert not calls, f"{path.name} calls mark_processed at line(s) {calls}"


@pytest.mark.parametrize("path", PYTHON_FILES, ids=lambda p: p.name)
def test_mailboxes_are_constructed_read_only(path: Path):
    """Any mailbox built here must be explicitly read-only.

    `MsgFileMailbox` defaults to read_only=False, so omitting it points a writer at Premier's own
    sample folder.
    """
    tree = ast.parse(_source(path), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name not in ("GraphMailbox", "MsgFileMailbox", "LocalFolderMailbox"):
            continue
        flag = next((kw for kw in node.keywords if kw.arg == "read_only"), None)
        assert flag is not None, f"{path.name}:{node.lineno} {name}() without read_only"
        assert getattr(flag.value, "value", None) is True, (
            f"{path.name}:{node.lineno} {name}(read_only=...) is not True"
        )


@pytest.mark.parametrize("path", PYTHON_FILES, ids=lambda p: p.name)
def test_ingest_helpers_called_read_only(path: Path):
    """`tools.ingest_mailbox.run_once` defaults to read_only=True; passing False would file mail."""
    for match in re.finditer(r"read_only\s*=\s*(\w+)", _source(path)):
        assert match.group(1) == "True", (
            f"{path.name} passes read_only={match.group(1)}"
        )


def test_message_frame_never_allows_scripts():
    """The frame renders mail from outside parties. `allow-scripts` alongside `allow-same-origin`
    would let a message run code against our own origin."""
    source = _source(MAIL_VIEW)
    sandboxes = re.findall(r'sandbox="([^"]*)"', source)
    assert sandboxes, "no sandbox attribute found — the frame must always be sandboxed"
    for value in sandboxes:
        assert "allow-scripts" not in value, f"sandbox grants allow-scripts: {value!r}"
        assert "allow-same-origin" in value, f"sandbox lost allow-same-origin: {value!r}"


def test_attachments_are_served_with_nosniff():
    """Attachments are attacker-controlled files; without nosniff the browser may execute one."""
    source = _source(UI_ROUTES)
    assert "X-Content-Type-Options" in source and "nosniff" in source
