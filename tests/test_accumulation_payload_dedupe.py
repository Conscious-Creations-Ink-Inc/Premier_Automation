"""One payload per message, not one per purchase order — and read back either way.

`accumulation` is keyed per (PO, shipment, email), and it carried the serialized message on every
one of those rows. A message naming N purchase orders stored its whole body N times. Measured on
Premier's live store: one expediting report naming 74 POs held 173 MB by itself, and across the
store 322 MB of payload holds 119 MB of distinct content once the attachment bytes are out of it.

The rule these tests hold is the same shape as the one in `test_accumulation_payload_bytes.py`:
**write it once, and read it back whichever version wrote it.** A row written before
`accumulation_payload` existed keeps its payload inline; a row written after keeps it in the new
table. Both must produce the same bundle, because the migration that moves the backlog is a space
reclaim that a human runs on their own schedule — nothing may depend on whether it has happened.
"""
from datetime import datetime, timezone

import pytest

from pipeline import stage2_accumulate, state_db
from pipeline.stage1_triage import triage
from tests import corpus_fixtures as fx


@pytest.fixture
def conn():
    c = state_db.get_connection(":memory:")
    yield c
    c.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _accumulate(conn, email):
    return stage2_accumulate.process_triaged_email(conn, triage(email), now_iso())


def test_one_message_on_many_pos_stores_its_payload_once(conn):
    """The amplification itself. Seven purchase orders on one email is seven accumulation rows and
    exactly one payload — not seven copies of the same body."""
    pos = ("906725", "907665", "908491")
    email = fx.inbound_email(
        email_id="msg-many-po", notice="939261", po_numbers=pos, shipment="70010 : 1",
        lines=[{"po": po, "line": "1", "part": f"EXT-{i}-AC", "item": f"2 EACH - EXT-{i}-AC Planter"}
               for i, po in enumerate(pos, start=900)],
    )
    _accumulate(conn, email)

    rows = conn.execute("SELECT COUNT(*) FROM accumulation").fetchone()[0]
    payloads = conn.execute("SELECT COUNT(*) FROM accumulation_payload").fetchone()[0]
    assert rows == len(pos)
    assert payloads == 1
    # And the per-row column is left empty rather than duplicated.
    assert conn.execute(
        "SELECT COUNT(*) FROM accumulation WHERE payload_json <> ''").fetchone()[0] == 0


def test_a_bundle_reads_a_payload_written_the_new_way(conn):
    email = fx.inbound_email(email_id="msg-new", notice="939336",
                             po_numbers=("908491",), shipment="90052 : 1")
    events = _accumulate(conn, email)

    assert len(events) == 1
    assert [e.email.email_id for e in events[0].emails] == ["msg-new"]


def test_a_bundle_still_reads_a_payload_written_the_old_way(conn):
    """A row from before the split, with the payload inline and no `accumulation_payload` row.

    This is every row in Premier's live store today. If the coalesce were dropped, or written the
    other way round, this bundle would come back empty and the delivery would carry no message —
    silently, because an empty bundle is a legal state.
    """
    email = fx.inbound_email(email_id="msg-old", notice="939337",
                             po_numbers=("908491",), shipment="90052 : 1")
    _accumulate(conn, email)

    # Rewrite it the way the previous version did: payload inline, nothing in the new table.
    payload = conn.execute(
        "SELECT payload_json FROM accumulation_payload WHERE email_id = 'msg-old'").fetchone()[0]
    conn.execute("UPDATE accumulation SET payload_json = ? WHERE email_id = 'msg-old'", (payload,))
    conn.execute("DELETE FROM accumulation_payload")
    conn.commit()

    key = stage2_accumulate._key_for(conn, triage(email), "908491")
    bundle = stage2_accumulate._bundle_for_key(conn, key)
    assert [e.email.email_id for e in bundle] == ["msg-old"]


def test_a_row_whose_payload_is_in_neither_place_is_skipped_not_raised(conn):
    """Missing evidence, not an empty message.

    Handing `None` to the deserializer would raise from inside `json.loads` with nothing naming the
    cause; dropping the row leaves the rest of the bundle readable and the gap visible as a shorter
    bundle.
    """
    email = fx.inbound_email(email_id="msg-orphan", notice="939338",
                             po_numbers=("908491",), shipment="90052 : 1")
    _accumulate(conn, email)
    conn.execute("DELETE FROM accumulation_payload")
    conn.commit()

    key = stage2_accumulate._key_for(conn, triage(email), "908491")
    assert stage2_accumulate._bundle_for_key(conn, key) == []
