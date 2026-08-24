"""The HTTP surface the React app talks to."""


def _queue(client):
    response = client.get("/api/reconciliation/exceptions")
    assert response.status_code == 200
    return response.json()


def _find(client, needle: str):
    return next(row for row in _queue(client) if needle in row["match"]["flag_reason"])


def test_read_endpoints_all_answer(client):
    for path in (
        "/api/health", "/api/dashboard/summary", "/api/reconciliation",
        "/api/reconciliation/exceptions", "/api/extracted-records", "/api/delivery-report",
        "/api/inbox/preview", "/api/vendor/template",
    ):
        assert client.get(path).status_code == 200, path


def test_exception_queue_only_returns_flagged_items_awaiting_review(client):
    rows = _queue(client)

    assert rows
    for row in rows:
        assert row["match"]["flagged"] is True
        assert row["match"]["review_status"] == "pending_review"


def test_detail_offers_ranked_candidates_and_the_reason(client):
    row = _find(client, "missing quantity")
    detail = client.get(f"/api/reconciliation/{row['match']['id']}").json()

    assert detail["candidates"], "a reviewer needs lines to choose between"
    assert detail["candidates"][0]["signals_matched"] >= detail["candidates"][-1]["signals_matched"]
    assert detail["match"]["flag_reason"]
    assert "quantity_received" in detail["editable_fields"]


def test_detail_offers_candidates_even_when_nothing_matched(client):
    """An unknown PO must not be a dead end — the reviewer still gets lines to pick from."""
    row = _find(client, "no PO line matched")
    detail = client.get(f"/api/reconciliation/{row['match']['id']}").json()

    assert detail["match"]["po_line_id"] is None
    assert detail["candidates"]


def test_approve_is_rejected_while_a_required_field_is_missing(client):
    row = _find(client, "missing quantity")

    response = client.post(f"/api/reconciliation/{row['match']['id']}/approve", json={})

    assert response.status_code == 400
    assert "Still missing" in response.json()["detail"]


def test_approve_settles_the_item_and_updates_the_dashboard(client):
    before = client.get("/api/dashboard/summary").json()
    row = _find(client, "missing quantity")
    match_id = row["match"]["id"]

    response = client.post(
        f"/api/reconciliation/{match_id}/approve",
        json={"filled_fields": {"quantity_received": 18}, "note": "confirmed by phone"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["review_status"] == "approved"
    assert body["extracted_status"] == "matched"
    assert body["receipt_id"] is not None

    after = client.get("/api/dashboard/summary").json()
    assert after["exception_queue_size"] == before["exception_queue_size"] - 1
    assert after["totals"]["approved"] == before["totals"]["approved"] + 1
    assert after["totals"]["staged_receipts"] == before["totals"]["staged_receipts"] + 1
    assert match_id not in [r["match"]["id"] for r in _queue(client)]


def test_cancel_requires_a_reason_and_then_fails_the_record(client):
    row = _queue(client)[0]
    match_id = row["match"]["id"]

    assert client.post(f"/api/reconciliation/{match_id}/cancel", json={"reason": ""}).status_code == 422

    response = client.post(
        f"/api/reconciliation/{match_id}/cancel", json={"reason": "duplicate receipt"}
    )
    assert response.status_code == 200
    assert response.json()["extracted_status"] == "failed"

    record = client.get(f"/api/extracted-records/{row['record']['id']}").json()
    assert "duplicate receipt" in record["record"]["record"]["comments"]


def test_decisions_are_attributed_to_the_operator_header(client):
    row = _find(client, "missing quantity")
    match_id = row["match"]["id"]

    client.post(
        f"/api/reconciliation/{match_id}/approve",
        json={"filled_fields": {"quantity_received": 18}},
        headers={"X-Operator": "reviewer@premier.example"},
    )

    detail = client.get(f"/api/reconciliation/{match_id}").json()
    assert detail["decisions"][0]["decided_by"] == "reviewer@premier.example"


def test_unknown_item_is_a_404(client):
    assert client.get("/api/reconciliation/999999").status_code == 404
    assert client.post("/api/reconciliation/999999/approve", json={}).status_code == 400


def test_request_info_renders_the_chase_template(client):
    row = _find(client, "missing quantity")

    body = client.post(f"/api/reconciliation/{row['match']['id']}/request-info").json()

    assert body["status"] == "mock_sent"
    assert "212547" in body["subject"] or "212547" in body["rendered_body"]
    assert "Quantity received" in body["rendered_body"]


def test_delivery_report_buckets_every_mail_by_status(client):
    report = client.get("/api/delivery-report").json()

    assert [bucket["key"] for bucket in report["buckets"]] == [
        "delivered_received", "in_transit", "cancelled",
    ]
    assert sum(bucket["count"] for bucket in report["buckets"]) == report["total"]


def test_organize_files_status_mail_and_leaves_the_rest(client):
    before = client.get("/api/inbox/preview").json()
    assert before["pending_move"] > 0

    result = client.post("/api/inbox/organize", json={}).json()

    assert result["moved"] == before["pending_move"]
    after = client.get("/api/inbox/preview").json()
    assert after["pending_move"] == 0
    # Confirmations, cancellations and unclear mail are deliberately left in the inbox.
    assert after["kept_in_inbox"] == before["kept_in_inbox"] > 0
