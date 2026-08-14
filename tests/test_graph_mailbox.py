"""Tests for the Graph connector — the layer below the `Mailbox` seam.

Everything above that seam is already covered against `FakeMailbox` and `MsgFileMailbox`. What was
never exercised is the HTTP/JSON translation itself, which is the part that decides what a live
Premier mailbox actually looks like to the pipeline.

The HTTP layer is monkeypatched rather than mocked with a library: `connectors.mailbox` routes
every Graph call through one module-level `_SESSION`, so replacing that attribute with a fake
exposing `.get`/`.post` is enough and adds no dependency to requirements-dev.txt.

It is a pooled `requests.Session` (retrying on 429 and honouring `Retry-After`) rather than the
bare `requests` module, which is why the fake stands in for the session and not for `requests`.
"""
import base64
import json

import pytest

from connectors import mailbox as mailbox_module
from connectors.mailbox import GRAPH_BASE_URL, MAX_PAGES_PER_POLL, GraphMailbox
from pipeline import attachment_ledger
from pipeline.parsing import sniff

PNG_1PX = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"


class FakeResponse:
    def __init__(self, payload=None, *, content=b"", status_code=200, text=""):
        self._payload = payload
        self.content = content
        self.status_code = status_code
        self.text = text
        self.ok = status_code < 400

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected HTTP {self.status_code}")


class FakeRequests:
    """Serves canned payloads by URL substring and records every call made."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(("GET", url, params))
        for fragment, response in self.routes.items():
            if fragment in url:
                return response(url) if callable(response) else response
        return FakeResponse({"value": []})

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(("POST", url, json))
        return FakeResponse({"id": "created-folder-id"})


class FakeConfidentialClientApplication:
    """`msal.ConfidentialClientApplication` performs tenant discovery over the network in its
    constructor, so merely building a GraphMailbox would otherwise need a live authority."""

    def __init__(self, client_id, authority=None, client_credential=None):
        self.authority = authority

    def acquire_token_silent(self, scopes, account=None):
        return None

    def acquire_token_for_client(self, scopes):
        return {"access_token": "token"}


@pytest.fixture
def graph(monkeypatch):
    """A GraphMailbox with credentials stubbed, so no test can reach Microsoft."""
    monkeypatch.setattr(mailbox_module.msal, "ConfidentialClientApplication",
                        FakeConfidentialClientApplication)

    def _build(**kwargs):
        return GraphMailbox(
            tenant_id="tenant", client_id="client", client_secret="secret",
            mailbox_address="receiver@example.com", **kwargs,
        )
    return _build


def message(**overrides):
    defaults = dict(
        id="AAMkAD-folder-scoped-id",
        internetMessageId="<abc123@premierpm.com>",
        receivedDateTime="2026-06-08T14:00:00Z",
        subject="Inbound Notification 239475",
        body={"contentType": "html", "content": "<p>PO 208491</p>"},
        hasAttachments=False,
    )
    defaults["from"] = {"emailAddress": {"address": "warehousing@authoritylogistics.com"}}
    defaults.update(overrides)
    return defaults


def file_attachment(name, data, **overrides):
    payload = {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "id": f"att-{name}",
        "name": name,
        "contentType": "application/pdf",
        "contentBytes": base64.b64encode(data).decode(),
    }
    payload.update(overrides)
    return payload


# --- _to_raw_email ----------------------------------------------------------


def test_internet_message_id_is_the_dedupe_key_and_provider_id_is_kept_separately(graph):
    box = graph()
    email = box._to_raw_email(message(), {})

    # The whole point of finding C5: the id that survives a folder move is the dedupe key, and
    # the mutable one is carried alongside purely so /move has something to address.
    assert email.email_id == "<abc123@premierpm.com>"
    assert email.provider_message_id == "AAMkAD-folder-scoped-id"


def test_message_without_an_internet_message_id_falls_back_to_the_graph_id(graph):
    box = graph()
    email = box._to_raw_email(message(internetMessageId=None), {})
    assert email.email_id == "graph:AAMkAD-folder-scoped-id"


def test_sender_domain_is_split_out_for_triage(graph):
    box = graph()
    email = box._to_raw_email(message(), {})
    assert email.sender_address == "warehousing@authoritylogistics.com"
    assert email.sender_domain == "authoritylogistics.com"


def test_a_message_with_no_sender_does_not_explode(graph):
    box = graph()
    email = box._to_raw_email(message(**{"from": {}}), {})
    assert email.sender_address == ""
    assert email.sender_domain == ""


def test_html_and_text_bodies_land_in_the_right_field(graph):
    box = graph()
    html = box._to_raw_email(message(), {})
    assert html.body_html == "<p>PO 208491</p>" and html.body_text is None

    plain = box._to_raw_email(
        message(body={"contentType": "text", "content": "PO 208491"}), {}
    )
    assert plain.body_text == "PO 208491" and plain.body_html is None


def test_inline_images_are_fetched_even_when_graph_says_there_are_no_attachments(graph, monkeypatch):
    """Graph reports hasAttachments=false when the only images are pasted inline — which is
    exactly how a property manager sends a photographed POD."""
    fetched = []
    box = graph()
    monkeypatch.setattr(box, "_fetch_attachments", lambda *a, **k: fetched.append(a) or [])

    box._to_raw_email(message(
        hasAttachments=False,
        body={"contentType": "html", "content": '<img src="cid:photo1@x">'},
    ), {})
    assert fetched, "an inline-image-only message must still have its attachments fetched"


# --- fetch_new --------------------------------------------------------------


def test_fetch_new_follows_odata_nextlink(graph, monkeypatch):
    page_two = f"{GRAPH_BASE_URL}/page2"
    fake = FakeRequests({
        "mailFolders/Inbox/messages": lambda url: (
            FakeResponse({"value": [message(id="m2", internetMessageId="<two@x>")]})
            if "page2" in url else
            FakeResponse({
                "value": [message(id="m1", internetMessageId="<one@x>")],
                "@odata.nextLink": page_two,
            })
        ),
        "/page2": FakeResponse({"value": [message(id="m2", internetMessageId="<two@x>")]}),
    })
    monkeypatch.setattr(mailbox_module, "_SESSION", fake)

    emails = graph().fetch_new()
    assert [e.email_id for e in emails] == ["<one@x>", "<two@x>"]


def test_fetch_new_requests_oldest_first(graph, monkeypatch):
    fake = FakeRequests()
    monkeypatch.setattr(mailbox_module, "_SESSION", fake)
    graph().fetch_new()

    _, _, params = fake.calls[0]
    assert params["$orderby"] == "receivedDateTime asc", (
        "newest-first plus a page cap means a large backlog never drains"
    )


def test_fetch_new_stops_at_the_page_cap(graph, monkeypatch):
    # Always returns a nextLink, so only MAX_PAGES_PER_POLL keeps it from looping forever.
    fake = FakeRequests({
        "": FakeResponse({"value": [message()], "@odata.nextLink": f"{GRAPH_BASE_URL}/next"}),
    })
    monkeypatch.setattr(mailbox_module, "_SESSION", fake)

    emails = graph().fetch_new()
    assert len(emails) == MAX_PAGES_PER_POLL


def test_one_malformed_message_does_not_cost_us_the_rest_of_the_poll(graph, monkeypatch):
    broken = message(id="broken")
    del broken["receivedDateTime"]        # KeyError inside _to_raw_email
    fake = FakeRequests({
        "": FakeResponse({"value": [broken, message(id="ok", internetMessageId="<ok@x>")]}),
    })
    monkeypatch.setattr(mailbox_module, "_SESSION", fake)

    emails = graph().fetch_new()
    assert [e.email_id for e in emails] == ["<ok@x>"]


# --- attachments ------------------------------------------------------------


def test_item_attachment_is_downloaded_as_msg_bytes(graph, monkeypatch):
    """A forwarded notification is the most common shape in Premier's mail, and it arrives as an
    itemAttachment whose bytes only exist behind /$value."""
    fake = FakeRequests({"/$value": FakeResponse(content=b"\xd0\xcf\x11\xe0msg-bytes")})
    monkeypatch.setattr(mailbox_module, "_SESSION", fake)

    att = graph()._to_attachment(
        {"@odata.type": "#microsoft.graph.itemAttachment", "id": "i1",
         "name": "Delivered Notification"},
        f"{GRAPH_BASE_URL}/attachments", {}, set(), set(),
    )
    assert att.filename == "Delivered Notification.msg"
    assert att.content_bytes.startswith(b"\xd0\xcf\x11\xe0")


def test_reference_attachment_is_recorded_not_treated_as_a_coverage_gap(graph):
    """A OneDrive link carries no bytes. It must not read as `no_adapter`, which means "our
    readers have a hole" and is asserted to be empty by the corpus regression."""
    att = graph()._to_attachment(
        {"@odata.type": "#microsoft.graph.referenceAttachment", "id": "r1",
         "name": "POD.pdf", "sourceUrl": "https://premierpm.sharepoint.com/POD.pdf"},
        "", {}, set(), set(),
    )
    assert att.drop_hint.startswith("reference:")
    assert "sharepoint.com" in att.drop_hint

    disposition, detail = attachment_ledger._disposition_for_hint(att.drop_hint)
    assert disposition == attachment_ledger.DROPPED_REFERENCE
    assert disposition != attachment_ledger.NO_ADAPTER
    assert disposition in attachment_ledger.NEEDS_ATTENTION, "a person has to open that link"


def test_unknown_attachment_type_is_still_recorded(graph):
    att = graph()._to_attachment(
        {"@odata.type": "#microsoft.graph.somethingNew", "id": "x1", "name": "mystery.bin"},
        "", {}, set(), set(),
    )
    assert att.filename == "mystery.bin"
    assert "unhandled Graph type" in att.drop_hint


def test_zero_byte_attachment_is_marked_empty_like_the_msg_connector(graph):
    att = graph()._to_attachment(
        file_attachment("blank.pdf", b""), "", {}, set(), set(),
    )
    assert att.drop_hint == "empty:zero bytes"
    assert attachment_ledger._disposition_for_hint(att.drop_hint)[0] == attachment_ledger.DROPPED_EMPTY


def test_byte_identical_attachments_are_deduped_within_one_message(graph, monkeypatch):
    """Premier really does send the same POD twice under different filenames — the 5-Star thread
    in the corpus does exactly this. `MsgFileMailbox` collapses them; Graph must too, or the
    second copy is extracted a second time."""
    fake = FakeRequests({
        "/attachments": FakeResponse({"value": [
            file_attachment("POD.pdf", PDF_BYTES),
            file_attachment("POD_signed.pdf", PDF_BYTES),
        ]}),
    })
    monkeypatch.setattr(mailbox_module, "_SESSION", fake)

    first, second = graph()._fetch_attachments("m1", {})
    assert first.drop_hint is None and first.content_bytes == PDF_BYTES
    assert second.drop_hint == f"duplicate:{sniff.sniff(PDF_BYTES, 'x.pdf', '').sha256[:12]}"
    assert second.content_bytes == b"", "a dropped attachment keeps its metadata, not its bytes"
    # Both slots are still reported — the ledger shows what arrived and which copy was read.
    assert attachment_ledger._disposition_for_hint(second.drop_hint)[0] == \
        attachment_ledger.DROPPED_DUPLICATE


def test_dedupe_does_not_reach_across_messages(graph, monkeypatch):
    """Two separate deliveries can legitimately carry the same document."""
    fake = FakeRequests({
        "/attachments": FakeResponse({"value": [file_attachment("POD.pdf", PDF_BYTES)]}),
    })
    monkeypatch.setattr(mailbox_module, "_SESSION", fake)
    box = graph()

    assert box._fetch_attachments("m1", {})[0].drop_hint is None
    assert box._fetch_attachments("m2", {})[0].drop_hint is None


# --- read_only --------------------------------------------------------------


def test_read_only_mark_processed_makes_no_http_call_at_all(graph, monkeypatch):
    """The guard has to sit before _resolve_folder_id, not just before the move: resolving a
    folder name *creates* it, so a half-guarded read-only run still leaves four new folders in
    Premier's mailbox."""
    fake = FakeRequests()
    monkeypatch.setattr(mailbox_module, "_SESSION", fake)

    box = graph(read_only=True)
    box.mark_processed("AAMkAD-folder-scoped-id", "Processed")

    assert fake.calls == []
    assert box.routed_to == {"AAMkAD-folder-scoped-id": "Processed"}


def test_a_normal_run_does_move_the_message(graph, monkeypatch):
    fake = FakeRequests({
        "/mailFolders": FakeResponse({"value": [{"id": "folder-1", "displayName": "Processed"}]}),
    })
    monkeypatch.setattr(mailbox_module, "_SESSION", fake)

    box = graph()
    box.mark_processed("AAMkAD-folder-scoped-id", "Processed")

    posts = [c for c in fake.calls if c[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][1].endswith("/messages/AAMkAD-folder-scoped-id/move")
    assert posts[0][2] == {"destinationId": "folder-1"}
    # Recorded in both modes: it is the runner's only per-run count, since the state tables
    # accumulate across polls.
    assert box.routed_to == {"AAMkAD-folder-scoped-id": "Processed"}


def test_missing_config_names_what_is_missing(monkeypatch):
    monkeypatch.setattr(mailbox_module.settings, "GRAPH_MAILBOX_ADDRESS", "")
    with pytest.raises(ValueError) as excinfo:
        GraphMailbox(tenant_id="t", client_id="c", client_secret="s", mailbox_address=None)
    assert "mailbox_address" in str(excinfo.value)


# --- Server-side window -------------------------------------------------------
# `skip_ids` makes an already-seen message cheap, but it still has to be *listed* to be skipped.
# Nothing is ever moved out of Premier's Inbox, so that listing grows for ever while
# MAX_PAGES_PER_POLL truncates it at a thousand messages oldest-first. Past a thousand, new mail
# would never be reached at all.

def test_a_since_instant_narrows_the_listing_server_side(graph, monkeypatch):
    fake = FakeRequests()
    monkeypatch.setattr(mailbox_module, "_SESSION", fake)

    graph().fetch_new(since="2026-08-12T09:00:00Z")

    _, _, params = fake.calls[0]
    assert params["$filter"] == "receivedDateTime ge 2026-08-12T09:00:00Z"


def test_no_since_asks_for_everything(graph, monkeypatch):
    """The first run of a fresh store has no watermark and must not silently read a partial inbox."""
    fake = FakeRequests()
    monkeypatch.setattr(mailbox_module, "_SESSION", fake)

    graph().fetch_new()

    assert "$filter" not in fake.calls[0][2]


def test_the_window_is_inclusive(graph, monkeypatch):
    """`ge`, not `gt`. The caller backdates this instant deliberately and `skip_ids` rejects the
    overlap for the price of a set lookup; excluding a boundary message loses it for good."""
    fake = FakeRequests()
    monkeypatch.setattr(mailbox_module, "_SESSION", fake)

    graph().fetch_new(since="2026-08-12T09:00:00Z")

    assert " ge " in fake.calls[0][2]["$filter"]
