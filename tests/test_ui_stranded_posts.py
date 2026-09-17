"""The queue entry for a post whose chain died mid-flight.

`CLAIMED` is not a resting state, but for a long time it was the only one with no way out and no
way to see it. Record 234 on PO 212560 sat there from 26 August 2026: the receipt, the POD upload
and the attach had all succeeded in Spitfire, and because `CLAIMED` is in `BLOCKING`, the Post
button was not drawn, "Post report" and "Verify POD" both answered that nothing had been posted,
and posting again reported a process still "in flight" that had been dead for a day.

It appeared in no queue at all — `blocked()` lists only `FLAGGED`, `awaiting_report()` only
`POD_POSTED` — so the sole trace anywhere in the product was the word "Posting…" in one table
cell. These are the two tests that keep it visible.
"""

import pytest
from fastapi.testclient import TestClient

from config import settings
from pipeline import post_ledger, state_db


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "pipeline_state.sqlite3"
    monkeypatch.setattr(settings, "PIPELINE_STATE_DB_PATH", path)
    conn = state_db.get_connection(path)
    conn.execute(
        """INSERT INTO extracted_records
           (id, source_email_id, po_number, spec_code, item_description, quantity_received,
            unit_of_measure, email_date, extraction_source, extraction_confidence, created_at)
           VALUES (234, 'mail-s', '212560', 'LRR-804-AC', 'Sculptural Accessory', 2.0, 'EA',
                   '2026-08-17', 'authority_inbound', 1.0, '2026-08-17')""")
    # Complete, so it reaches the Records page — `completeness.REQUIRED` wants a POD date, and the
    # cell under test is drawn there.
    conn.execute("UPDATE extracted_records SET pod_stated_date = '2026-08-17' WHERE id = 234")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def client(store):
    from api.main import app

    return TestClient(app)


def strand(store, *, receipt="7af9973d", doc_no="0001"):
    """Claim a delivery and walk away, exactly as a killed process leaves it."""
    conn = state_db.get_connection(store)
    attempt = post_ledger.claim(conn, record_id=234, po_number="212560", line_number=1,
                                pod_md5="E1CD03FF", quantity=2.0)
    if receipt:
        post_ledger.record_receipt(conn, attempt.idempotency_key,
                                   receipt_key=receipt, receipt_doc_no=doc_no)
    conn.commit()
    conn.close()


def test_a_stranded_claim_is_listed_on_the_queue(client, store):
    """Not merely present in the table — named, with the receipt that may be unfinished, because
    what makes this urgent is that Premier's ERP may hold a half-built document."""
    strand(store)

    page = client.get("/ui/manual").text

    assert "Posts that died mid-flight (1)" in page
    assert "stranded-posting" in page
    assert "212560" in page and "0001" in page
    assert "settle_stranded_posts" in page, "the queue must say how to repair it"


def test_nothing_is_listed_when_no_claim_is_stranded(client, store):
    """The section is absent, not empty-with-a-heading. `stranded()` returning nothing is the
    normal state its own docstring demands between runs, and a permanent empty box for it would
    read as an outstanding problem."""
    page = client.get("/ui/manual").text

    assert "died mid-flight" not in page
    assert "stranded-posting" not in page


def test_the_records_cell_says_how_long_it_has_been_posting(client, store):
    """"Posting…" on its own reads as live, and this state is usually anything but — `CLAIMED`
    never settles itself. The date is what separates a post running right now from one abandoned
    in August, and it is the difference between waiting and going to look."""
    strand(store)

    page = client.get("/ui/records").text

    assert "Posting… (since" in page
