"""Filling a record's gaps from its own proof of delivery.

The rule these tests exist to hold is a data-integrity one, not a UI one: the delivery date and
the signature that reach Premier's ERP must come off the POD, and nothing a person already
reviewed may be silently replaced. Everything is driven against an in-memory store with a real
PDF built for the test, so the parsing is exercised rather than mocked.
"""

import io
import sqlite3

import pytest

from pipeline import completeness, record_completion


def _pdf(text_lines):
    """A minimal single-page PDF with a real text layer, so `pdfplumber` reads it as one would."""
    reportlab = pytest.importorskip("reportlab", reason="needed to build a PDF with a text layer")
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer)
    y = 800
    for line in text_lines:
        c.drawString(40, y, line)
        y -= 16
    c.save()
    return buffer.getvalue()


POD_TEXT = [
    "The following is the proof-of-delivery for tracking number: 7497809572",
    "Delivery Information:",
    "Status: Delivered Delivery date: Sep 10, 2025 10:18",
    "Signed for by: U ALI",
    "Shipping Information:",
    "Tracking number: 7497809572 Ship Date: Sep 5, 2025",
    "Purchase Order 910634 : 1",
]


@pytest.fixture
def store(monkeypatch):
    """A store holding one incomplete record, its PO lines, and a POD the record can be joined to."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE extracted_records (
            id INTEGER PRIMARY KEY, source_email_id TEXT, po_number TEXT, spec_code TEXT,
            item_description TEXT, vendor_name TEXT, quantity_received REAL, unit_of_measure TEXT,
            po_line_number INTEGER, pod_stated_date TEXT, received_by TEXT,
            carrier_name TEXT, tracking_number TEXT, updated_at TEXT);
        CREATE TABLE spitfire_po_lines (po_number TEXT, line_number INTEGER);
        -- Empty on purpose. These tests hand the POD straight to the chooser, so there is no
        -- stored verdict and `_pod_facts` must fall back to reading the file — the path a row
        -- ingested before `is_pod` existed takes.
        CREATE TABLE attachment_ledger (
            id INTEGER PRIMARY KEY, email_id TEXT, depth INTEGER DEFAULT 0, ordinal INTEGER,
            filename TEXT, is_pod INTEGER DEFAULT 0, pod_po_numbers TEXT DEFAULT '',
            pod_delivery_date TEXT, pod_signed_by TEXT);
    """)
    conn.execute("""INSERT INTO extracted_records
        (id, source_email_id, po_number, spec_code, item_description, vendor_name,
         quantity_received, unit_of_measure)
        VALUES (1, 'mail-1', '910634', 'GR-350a-WTF', 'Main Drapery Fabric', 'P. Kaufmann',
                196.0, 'YD')""")
    conn.executemany("INSERT INTO spitfire_po_lines VALUES (?, ?)",
                     [("910634", 1), ("910634", 3), ("910634", 4)])
    conn.commit()
    return conn


def _serve_pod(monkeypatch, content, *, kind="pdf", filename="POD.pdf"):
    """Stand in for the attachment chooser so these tests never depend on a stored blob."""
    from pipeline import spitfire_post
    from pipeline.attachment_bytes import ResolvedAttachment
    monkeypatch.setattr(
        spitfire_post, "_pod_for",
        lambda conn, row: ResolvedAttachment(content=content, filename=filename,
                                             content_type="application/pdf", kind=kind,
                                             source="test"))


def _row(conn, record_id=1):
    return conn.execute("SELECT * FROM extracted_records WHERE id = ?", (record_id,)).fetchone()


def test_the_delivery_date_and_signature_come_off_the_pod(store, monkeypatch):
    """The whole point of the module. These two values end up on a receipt in Premier's ERP, so
    they are read from the proof rather than typed by whoever happens to be looking at the screen."""
    _serve_pod(monkeypatch, _pdf(POD_TEXT))

    outcome = record_completion.complete(store, _row(store), line=1)

    assert outcome.ok and outcome.is_complete, outcome.message
    after = _row(store)
    assert after["pod_stated_date"] == "2025-09-10"
    assert after["received_by"] == "U ALI"
    assert after["po_line_number"] == 1
    assert completeness.is_complete(after)


def test_a_value_already_present_is_never_overwritten(store, monkeypatch):
    """Stage 3 may have extracted a date a reviewer has been reading. Replacing it here would
    change what they thought they were approving, without saying so."""
    store.execute("UPDATE extracted_records SET pod_stated_date = '2025-01-01' WHERE id = 1")
    store.commit()
    _serve_pod(monkeypatch, _pdf(POD_TEXT))

    record_completion.complete(store, _row(store), line=1)

    assert _row(store)["pod_stated_date"] == "2025-01-01", "the reviewer's value was replaced"
    assert _row(store)["received_by"] == "U ALI", "the empty field should still have been filled"


def test_a_pod_naming_a_different_po_is_refused(store, monkeypatch):
    """A delivery note for someone else's purchase order is not evidence for this line, and its
    date would be wrong on the receipt in a way nobody could see afterwards."""
    _serve_pod(monkeypatch, _pdf([l.replace("910634", "999999") for l in POD_TEXT]))

    outcome = record_completion.complete(store, _row(store), line=1)

    assert not outcome.ok
    assert "999999" in outcome.message and "not 910634" in outcome.message
    assert _row(store)["pod_stated_date"] is None, "a refused completion still wrote"


def test_a_line_the_purchase_order_does_not_have_is_refused(store, monkeypatch):
    """The one field a reviewer supplies is the one field checked against Spitfire's own lines."""
    _serve_pod(monkeypatch, _pdf(POD_TEXT))

    outcome = record_completion.complete(store, _row(store), line=99)

    assert not outcome.ok
    assert "no line 99" in outcome.message and "1, 3, 4" in outcome.message
    assert _row(store)["po_line_number"] is None
    assert _row(store)["received_by"] is None, "nothing may be written when the line is rejected"


def test_a_record_with_no_stored_pod_is_told_why(store, monkeypatch):
    from pipeline import spitfire_post
    monkeypatch.setattr(spitfire_post, "_pod_for", lambda conn, row: None)

    outcome = record_completion.complete(store, _row(store), line=1)

    assert not outcome.ok
    assert "no proof of delivery" in outcome.message
    assert outcome.remaining, "the reviewer is still told which fields are open"


def test_a_photographed_pod_read_at_ingest_fills_the_record(store, monkeypatch):
    """A POD is not a file type. This one is a photograph — nothing here can read its pixels — and
    it works because OCR read it once on the way in and the ledger kept the answer."""
    _serve_pod(monkeypatch, b"\x89PNG\r\n", kind="image", filename="POD.png")
    store.execute("""INSERT INTO attachment_ledger
                     (email_id, ordinal, filename, is_pod, pod_po_numbers,
                      pod_delivery_date, pod_signed_by)
                     VALUES ('mail-1', 0, 'POD.png', 1, '910634', '2025-09-10', 'U ALI')""")
    store.commit()

    outcome = record_completion.complete(store, _row(store), line=1)

    assert outcome.ok and outcome.is_complete, outcome.message
    after = _row(store)
    assert after["pod_stated_date"] == "2025-09-10"
    assert after["received_by"] == "U ALI"


def test_an_unread_image_pod_says_so_rather_than_failing_silently(store, monkeypatch):
    """No stored verdict and no text we can read for free. Naming the limit — and the way out —
    beats a blank refusal that leaves a reviewer wondering what they did wrong."""
    _serve_pod(monkeypatch, b"\x89PNG\r\n", kind="image", filename="POD.png")

    outcome = record_completion.complete(store, _row(store), line=1)

    assert not outcome.ok
    assert "image" in outcome.message and "reprocess" in outcome.message


def test_a_pdf_that_is_not_a_pod_is_refused(store, monkeypatch):
    _serve_pod(monkeypatch, _pdf(["Invoice 12345", "Net 30", "Thank you for your business"]))

    outcome = record_completion.complete(store, _row(store), line=1)

    assert not outcome.ok
    assert "does not read as a proof of delivery" in outcome.message


def test_completing_an_already_complete_record_changes_nothing(store, monkeypatch):
    store.execute("""UPDATE extracted_records
                        SET pod_stated_date='2025-09-10', received_by='U ALI', po_line_number=1
                      WHERE id = 1""")
    store.commit()
    _serve_pod(monkeypatch, _pdf(POD_TEXT))

    outcome = record_completion.complete(store, _row(store), line=1)

    assert not outcome.ok, "there was nothing to do"
    assert outcome.is_complete
    assert "already complete" in outcome.message


# --- keeping what a verification worked out ----------------------------------------------------
#
# `po_verify` resolved the line and threw it away, so every record stayed missing `PO line #` and
# `post_decision` refused it. What is kept, and what is deliberately not, is a decision about which
# budget line gets charged — so each rule has its own test.


class _Line:
    def __init__(self, line_number=1, spec_resolved=True, qty_agrees=True,
                 matched_on_parent=False, spec_code="GR-350a-WTF"):
        self.line_number = line_number
        self.spec_resolved = spec_resolved
        self.qty_agrees = qty_agrees
        self.matched_on_parent = matched_on_parent
        self.spec_code = spec_code


class _Verification:
    def __init__(self, matched):
        self.matched = matched


def test_an_exact_match_is_kept(store):
    outcome = record_completion.apply_verification(store, _row(store), _Verification(_Line()))

    assert outcome.ok, outcome.message
    assert _row(store)["po_line_number"] == 1
    assert "exact spec match" in outcome.applied[0]


def test_a_description_only_match_is_not_kept(store):
    """The weaker claim stays on screen and goes no further. Recording it would charge a budget
    line nobody confirmed."""
    outcome = record_completion.apply_verification(
        store, _row(store), _Verification(_Line(spec_resolved=False)))

    assert not outcome.ok
    assert "description alone" in outcome.message
    assert _row(store)["po_line_number"] is None


def test_a_match_that_disagrees_on_quantity_is_not_kept(store):
    outcome = record_completion.apply_verification(
        store, _row(store), _Verification(_Line(qty_agrees=False)))

    assert not outcome.ok
    assert "quantity" in outcome.message
    assert _row(store)["po_line_number"] is None


def test_a_line_already_on_the_record_is_never_overwritten(store):
    """A reviewer's own choice outranks the scorer, and so does anything already stored."""
    store.execute("UPDATE extracted_records SET po_line_number = 7 WHERE id = 1")
    store.commit()

    outcome = record_completion.apply_verification(
        store, _row(store), _Verification(_Line(line_number=3)))

    assert not outcome.ok
    assert _row(store)["po_line_number"] == 7


def test_a_parent_spec_match_still_counts_as_exact(store):
    """The mail said `STE-402-LT-B`, the purchase order carries `STE-402-LT`. Still an exact match
    on a real code, not a guess from prose."""
    outcome = record_completion.apply_verification(
        store, _row(store),
        _Verification(_Line(spec_resolved=False, matched_on_parent=True)))

    assert outcome.ok, outcome.message
    assert _row(store)["po_line_number"] == 1


def test_no_resolved_line_is_not_an_error(store):
    assert not record_completion.apply_verification(
        store, _row(store), _Verification(None)).ok


# --- One spec, many lines -----------------------------------------------------------------------
#
# PO 907514 carries 29 Spitfire lines that all read `LOB-900-SI`, a signage package whose lines
# differ only by description. There the spec cannot say which line a delivery is, so every one of
# the 23 delivered items identified itself only as "PO 907514, LOB-900-SI" — true of all 23.

@pytest.fixture
def signage(monkeypatch):
    """A purchase order whose spec names many lines, as PO 907514's really does."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE extracted_records (
            id INTEGER PRIMARY KEY, source_email_id TEXT, po_number TEXT, spec_code TEXT,
            item_description TEXT, vendor_name TEXT, quantity_received REAL, unit_of_measure TEXT,
            po_line_number INTEGER, pod_stated_date TEXT, received_by TEXT,
            carrier_name TEXT, tracking_number TEXT, updated_at TEXT);
        CREATE TABLE spitfire_po_lines (
            po_number TEXT, line_number INTEGER, spec_code TEXT, description TEXT,
            unit_of_measure TEXT, qty_ordered REAL);
        CREATE TABLE attachment_ledger (
            id INTEGER PRIMARY KEY, email_id TEXT, depth INTEGER DEFAULT 0, ordinal INTEGER,
            filename TEXT, is_pod INTEGER DEFAULT 0, pod_po_numbers TEXT DEFAULT '',
            pod_delivery_date TEXT, pod_signed_by TEXT);
    """)
    # Spitfire keeps the catalogue code inside the description; the delivery paperwork does not.
    conn.executemany(
        "INSERT INTO spitfire_po_lines VALUES ('907514', ?, 'LOB-900-SI', ?, 'EA', ?)",
        [(11, "Common Room ID Item: ST-6", 15.0),
         (13, "P.S. Small Directional Item: ST-3", 3.0),
         (14, "Restroom Door Placard Item: ST-11", 8.0),
         (18, "Exit Item: ST-14", 4.0),
         (24, "Exit Route Item: ST-15", 4.0)])
    conn.commit()
    return conn


def _signage_record(conn, description, quantity, *, record_id=1, spec="LOB-900-SI"):
    conn.execute("""INSERT INTO extracted_records
        (id, source_email_id, po_number, spec_code, item_description, quantity_received,
         unit_of_measure) VALUES (?, 'mail-sig', '907514', ?, ?, ?, 'EA')""",
        (record_id, spec, description, quantity))
    conn.commit()
    return conn.execute("SELECT * FROM extracted_records WHERE id = ?", (record_id,)).fetchone()


def test_the_description_names_the_line_when_the_spec_cannot(signage):
    """22 of PO 907514's 23 delivered items resolve this way; nothing else can tell them apart."""
    row = _signage_record(signage, "Common Room ID", 15.0)

    line, note = record_completion.find_line_by_description(signage, row)

    assert line == 11, note
    assert "Common Room ID" in note and "lines share spec" in note


def test_a_catalogue_code_on_the_purchase_order_line_does_not_prevent_the_match():
    """Spitfire stores `P.S. Small Directional Item: ST-3`; the email says `P.S. Small Directional`.
    The code carries digits the email never mentions, and the digit rule — rightly strict about
    numbers — rejected every candidate outright until the code was stripped. Measured then: 0/23."""
    from pipeline.parsing import items
    assert items.strip_catalogue_code("P.S. Small Directional Item: ST-3") == "P.S. Small Directional"
    assert items.strip_catalogue_code("Medicine Ball 4 Kg CODE: A0001002") == "Medicine Ball 4 Kg"
    assert items.strip_catalogue_code("Item Description Only") == "Item Description Only"


def test_a_subset_description_never_picks_the_longer_line(signage):
    """"Exit" must find line 18, not line 24's "Exit Route" — `token_set_ratio` scores them equal."""
    row = _signage_record(signage, "Exit", 4.0)

    line, note = record_completion.find_line_by_description(signage, row)

    assert line == 18, note


def test_a_wording_no_line_matches_is_left_to_a_person(signage):
    """One of PO 907514's 23 is exactly this: `Main Elevator Lobby Directory`, best score 53."""
    row = _signage_record(signage, "Main Elevator Lobby Directory", 3.0)

    line, note = record_completion.find_line_by_description(signage, row)

    assert line is None
    assert "choose the line yourself" in note


def test_a_line_that_disagrees_on_quantity_is_refused(signage):
    """The name matching is only half of it — the figure has to agree, or a reviewer looks."""
    row = _signage_record(signage, "Common Room ID", 9.0)   # the line is ordered 15

    line, note = record_completion.find_line_by_description(signage, row)

    assert line is None
    assert "ordered 15.0" in note and "9.0 received" in note


def test_an_unambiguous_spec_is_left_to_the_spec(signage):
    """Where a spec names one line, the spec decides — `apply_verification`'s refusal to accept a
    description alone is deliberate and stays exactly as it is."""
    signage.execute("INSERT INTO spitfire_po_lines VALUES "
                    "('907514', 40, 'LOB-901-XX', 'Lonely Sign Item: ST-99', 'EA', 2.0)")
    signage.commit()
    row = _signage_record(signage, "Lonely Sign", 2.0, spec="LOB-901-XX")

    line, note = record_completion.find_line_by_description(signage, row)

    assert line is None
    assert "spec names a single line" in note


def test_a_cold_mirror_says_so_rather_than_guessing(signage):
    """The resolution reads the local cache. A PO nobody has verified is not in it."""
    signage.execute("DELETE FROM spitfire_po_lines")
    signage.commit()
    row = _signage_record(signage, "Common Room ID", 15.0)

    line, note = record_completion.find_line_by_description(signage, row)

    assert line is None
    assert "not in the local mirror" in note


def test_a_reviewers_line_is_never_replaced(signage):
    """A person's choice outranks the scorer, as it does everywhere else in this module."""
    row = _signage_record(signage, "Common Room ID", 15.0)
    signage.execute("UPDATE extracted_records SET po_line_number = 99 WHERE id = 1")
    signage.commit()
    row = signage.execute("SELECT * FROM extracted_records WHERE id = 1").fetchone()

    line, note = record_completion.find_line_by_description(signage, row)

    assert line is None
    assert "already carries a line" in note
