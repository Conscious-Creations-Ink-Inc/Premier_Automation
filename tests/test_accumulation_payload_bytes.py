"""The 1.87 GB of base64 that made the console slow, and the rule that keeps it out.

`accumulation.payload_json` carried every attachment's bytes base64'd inline. Measured on the live
store on 2026-09-03: **1,870 MB of a 1,930 MB database** — 98% of the payload, median 921 KB a row,
43 MB for the worst. Accumulation rows are keyed per (PO, shipment), so an email touching three POs
embedded its attachments three times; the top 40 rows alone held 520 MB of copies of 242 MB of
content, and one 9.3 MB file was written twelve times.

Every byte of it was already on disk. `attachment_store` is content-addressed and, verified across
the whole ledger, held 12,084 of the 12,088 rows carrying bytes — the four exceptions being three
zero-byte test fixtures and one file refused as oversize.

The rule these tests hold is not "never embed". It is **never embed what the store already has,
and never drop what it does not** — so no attachment can be lost by deploying this, in either
direction, against rows written by either version.
"""

import base64
import json

from pipeline import attachment_store, stage2_accumulate
from pipeline.models import Attachment


def _attachment(data: bytes, sha: str) -> Attachment:
    return Attachment(filename="pod.pdf", content_type="application/pdf",
                      content_bytes=data, sha256=sha, size_bytes=len(data))


def test_bytes_already_in_the_store_are_not_embedded_again(monkeypatch, tmp_path):
    """The whole point: the store has it, so the payload carries a reference instead of a copy."""
    monkeypatch.setattr(attachment_store, "ROOT", tmp_path)
    data = b"%PDF-1.4 proof of delivery" * 500
    sha = attachment_store.put(data, root=tmp_path)

    embedded = stage2_accumulate._embedded_bytes(_attachment(data, sha))

    assert embedded == "", "the store already holds these bytes; embedding them doubles them"


def test_bytes_the_store_does_not_have_are_still_embedded(monkeypatch, tmp_path):
    """The safety half, and the reason this is deployable.

    An attachment the store never took — one refused as oversize, say — must keep its inline copy.
    A byte is only ever dropped once its replacement is confirmed present.
    """
    monkeypatch.setattr(attachment_store, "ROOT", tmp_path)
    data = b"never stored"

    embedded = stage2_accumulate._embedded_bytes(
        _attachment(data, "0" * 64))          # a sha the store has never seen

    assert base64.b64decode(embedded) == data, (
        "bytes absent from the store must stay in the payload, or they are simply lost")


def test_bytes_come_back_out_of_the_store(monkeypatch, tmp_path):
    """A round trip through the store returns the original bytes exactly."""
    monkeypatch.setattr(attachment_store, "ROOT", tmp_path)
    data = b"signed for by U ALI" * 300
    sha = attachment_store.put(data, root=tmp_path)

    recovered = stage2_accumulate._attachment_bytes({"sha256": sha, "content_b64": ""})

    assert recovered == data


def test_rows_written_by_the_old_code_still_deserialize(monkeypatch, tmp_path):
    """1,217 rows already carry `content_b64`. The inline copy is read first, so they are
    unaffected by this change and need no migration to remain readable."""
    monkeypatch.setattr(attachment_store, "ROOT", tmp_path)
    data = b"an older payload that embedded its bytes"

    recovered = stage2_accumulate._attachment_bytes({
        "sha256": "",                                     # older rows may not even carry one
        "content_b64": base64.b64encode(data).decode("ascii"),
    })

    assert recovered == data


def test_a_missing_blob_degrades_one_attachment_rather_than_failing_the_delivery(
        monkeypatch, tmp_path):
    """`b""` is the state a connector-dropped attachment has always produced and the pipeline
    already handles it. Raising here would fail the whole delivery over one unreadable file."""
    monkeypatch.setattr(attachment_store, "ROOT", tmp_path)

    assert stage2_accumulate._attachment_bytes({"sha256": "f" * 64, "content_b64": ""}) == b""
    assert stage2_accumulate._attachment_bytes({}) == b""


def test_a_serialized_payload_carries_no_attachment_bytes(monkeypatch, tmp_path):
    """End to end, against the real serializer: the payload must be metadata-sized.

    This is the assertion that would have caught the regression in the first place — a payload
    whose size tracks its attachments is the defect, and it is invisible in any test that only
    checks field values.
    """
    monkeypatch.setattr(attachment_store, "ROOT", tmp_path)
    big = b"\x89PNG" + b"x" * 400_000
    sha = attachment_store.put(big, root=tmp_path)

    payload = stage2_accumulate._embedded_bytes(_attachment(big, sha))
    document = json.dumps({"content_b64": payload})

    assert len(document) < 1_000, (
        f"payload is {len(document)} bytes for a 400 KB attachment — the bytes are being "
        "embedded again")


# --- the one-time migration ------------------------------------------------
#
# Exercised as a function against synthetic payloads. It is deliberately never run against the
# live database from a test — CLAUDE.md §4: migrations are written, never run.

def test_the_migration_unlinks_only_what_the_store_can_vouch_for(monkeypatch, tmp_path):
    """Verified by content, not just by presence.

    `exists()` alone would be enough for a cache. This is not a cache: once the payload is
    rewritten the stored blob is the last copy, so a truncated or replaced file is exactly the
    case where unlinking loses an attachment permanently.
    """
    from tools import shrink_accumulation_payloads as migration

    monkeypatch.setattr(attachment_store, "ROOT", tmp_path)

    good = b"a real proof of delivery" * 100
    good_sha = attachment_store.put(good, root=tmp_path)

    # Present in the store, but its content no longer hashes to the name it is filed under.
    corrupt_sha = "c" * 64
    corrupt_path = tmp_path / corrupt_sha[:2] / corrupt_sha
    corrupt_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_path.write_bytes(b"truncated")

    assert migration._blob_is_trustworthy(good_sha) is True
    assert migration._blob_is_trustworthy(corrupt_sha) is False, (
        "a blob that does not hash to its own name must never license dropping the inline copy")
    assert migration._blob_is_trustworthy("") is False
    assert migration._blob_is_trustworthy("f" * 64) is False


def test_the_migration_keeps_bytes_it_cannot_replace(monkeypatch, tmp_path):
    """The mixed case, which is the one that matters: one attachment safely in the store and one
    that never made it. The first is unlinked; the second keeps its only copy."""
    from tools import shrink_accumulation_payloads as migration

    monkeypatch.setattr(attachment_store, "ROOT", tmp_path)
    stored = b"stored safely" * 200
    stored_sha = attachment_store.put(stored, root=tmp_path)
    orphan = b"never stored anywhere else"

    payload = json.dumps({"email": {"attachments": [
        {"sha256": stored_sha, "content_b64": base64.b64encode(stored).decode("ascii")},
        {"sha256": "0" * 64, "content_b64": base64.b64encode(orphan).decode("ascii")},
    ]}})

    rewritten, freed, dropped, kept = migration._rewrite(payload)

    assert (dropped, kept) == (1, 1)
    assert freed > 0
    attachments = json.loads(rewritten)["email"]["attachments"]
    assert attachments[0]["content_b64"] == "", "the stored one should be a reference now"
    assert base64.b64decode(attachments[1]["content_b64"]) == orphan, (
        "the attachment with no other copy must keep its bytes")


def test_the_migration_is_idempotent(monkeypatch, tmp_path):
    """A second pass must find nothing to do, so re-running it is never destructive."""
    from tools import shrink_accumulation_payloads as migration

    monkeypatch.setattr(attachment_store, "ROOT", tmp_path)
    data = b"already unlinked" * 50
    sha = attachment_store.put(data, root=tmp_path)

    payload = json.dumps({"email": {"attachments": [
        {"sha256": sha, "content_b64": base64.b64encode(data).decode("ascii")}]}})

    once, _, _, _ = migration._rewrite(payload)
    twice, freed, dropped, _ = migration._rewrite(once)

    assert twice is None and freed == 0 and dropped == 0


def test_the_migration_leaves_a_payload_it_cannot_parse_alone(monkeypatch, tmp_path):
    """Malformed JSON is skipped, not raised on. One bad row must not stop the migration."""
    from tools import shrink_accumulation_payloads as migration

    monkeypatch.setattr(attachment_store, "ROOT", tmp_path)
    assert migration._rewrite("{not json")[0] is None
    assert migration._rewrite('{"email": {}}')[0] is None
