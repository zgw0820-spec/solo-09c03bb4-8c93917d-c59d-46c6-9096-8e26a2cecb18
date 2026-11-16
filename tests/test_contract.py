"""Additional contract/edge-case tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

from conftest import (
    BUGGY_INCREMENTAL_DELETE_MISSING,
    CORRECT_FULL,
    CORRECT_INCREMENTAL,
    op_delete,
    op_insert,
)


def _client() -> TestClient:
    return TestClient(app)


def _base_payload() -> dict:
    return {
        "initial": {
            "orders": [
                {"order_id": 1, "amount": 1000},
                {"order_id": 2, "amount": 500},
                {"order_id": 3, "amount": 300},
            ],
            "refunds": [{"refund_id": 10, "order_id": 1, "amount": 200}],
        },
        "batches": [
            {"batch_id": 11, "changes": [op_insert("refunds", refund_id=20, order_id=1, amount=50)]},
            {"batch_id": 22, "changes": [op_insert("refunds", refund_id=30, order_id=2, amount=70)]},
            {"batch_id": 33, "changes": [op_insert("refunds", refund_id=21, order_id=3, amount=90)]},
            {"batch_id": 44, "changes": [op_delete("refunds", refund_id=21)]},
        ],
        "sql": {"full_select": CORRECT_FULL, "incremental": BUGGY_INCREMENTAL_DELETE_MISSING},
    }


def test_more_than_20_batches_rejected():
    client = _client()
    payload = _base_payload()
    payload["batches"] = [
        {"batch_id": i, "changes": [op_insert("orders", order_id=100 + i, amount=1)]}
        for i in range(1, 22)
    ]
    response = client.post("/v1/minimize", json=payload)
    assert response.status_code == 422


def test_initial_over_100_rows_rejected():
    client = _client()
    payload = _base_payload()
    payload["initial"]["orders"] = [
        {"order_id": i, "amount": 1} for i in range(1, 101)
    ]
    payload["initial"]["refunds"] = [{"refund_id": 500, "order_id": 1, "amount": 1}]
    response = client.post("/v1/minimize", json=payload)
    assert response.status_code == 422


def test_duplicate_batch_ids_rejected():
    client = _client()
    payload = _base_payload()
    payload["batches"][1]["batch_id"] = payload["batches"][0]["batch_id"]
    assert client.post("/v1/minimize", json=payload).status_code == 422


def test_incremental_reads_result_allowed():
    # Incremental SQL may read its own target table.
    incremental = (
        "INSERT INTO result(order_id, net_amount) "
        "SELECT new_order_id, new_amount FROM batch_changes "
        "WHERE tbl='orders' AND op='insert'; "
        "DELETE FROM result WHERE order_id IN "
        "(SELECT order_id FROM result GROUP BY order_id HAVING COUNT(*) > 1);"
    )
    response = _client().post(
        "/v1/minimize", json={**_base_payload(), "sql": {"full_select": CORRECT_FULL, "incremental": incremental}}
    )
    assert response.status_code == 200


def test_incremental_with_comments_and_multiple_statements():
    incremental = (
        "-- create new orders\n"
        "INSERT INTO result(order_id, net_amount)\n"
        "SELECT new_order_id, new_amount FROM batch_changes\n"
        "WHERE tbl = 'orders' AND op = 'insert'; /* refund inserts */\n"
        "UPDATE result SET net_amount = result.net_amount - b.amt\n"
        "FROM (SELECT new_refund_order_id AS oid, SUM(new_refund_amount) AS amt\n"
        "      FROM batch_changes WHERE tbl='refunds' AND op='insert'\n"
        "      GROUP BY new_refund_order_id) AS b\n"
        "WHERE result.order_id = b.oid; -- done\n"
    )
    body = {
        **_base_payload(),
        "sql": {"full_select": CORRECT_FULL, "incremental": incremental},
    }
    # This SQL is correct for inserts but not deletions, so a difference is
    # still expected at batch 44 - what matters here is that it parses/runs.
    response = _client().post("/v1/minimize", json=body)
    assert response.status_code == 200
    assert response.json()["first_difference_batch_id"] == 44


def test_full_sql_returning_three_columns_is_an_error():
    payload = _base_payload()
    payload["sql"]["full_select"] = (
        "SELECT order_id, amount, 1 FROM orders"
    )
    body = _client().post("/v1/minimize", json=payload).json()
    assert body["status"] == "undetermined"


def test_full_sql_reading_result_rejected():
    payload = _base_payload()
    payload["sql"]["full_select"] = "SELECT order_id, net_amount FROM result"
    assert _client().post("/v1/minimize", json=payload).status_code == 422


def test_incremental_writing_source_rejected():
    payload = _base_payload()
    payload["sql"]["incremental"] = "DELETE FROM orders WHERE 0;"
    assert _client().post("/v1/minimize", json=payload).status_code == 422


def test_state_is_isolated_between_sequential_requests():
    client = _client()
    first = client.post("/v1/minimize", json=_base_payload()).json()
    second = client.post("/v1/minimize", json=_base_payload()).json()
    assert first["status"] == second["status"] == "reduced"
    assert first["reduced_input"] == second["reduced_input"]
    assert first["difference"] == second["difference"]


def test_response_shape_contains_full_reduced_input():
    body = _client().post("/v1/minimize", json=_base_payload()).json()
    reduced = body["reduced_input"]
    assert set(reduced) == {"initial", "batches", "sql"}
    assert reduced["initial"]["orders"]
    assert all("batch_id" in batch for batch in reduced["batches"])
    assert "full_select" in reduced["sql"] and "incremental" in reduced["sql"]
