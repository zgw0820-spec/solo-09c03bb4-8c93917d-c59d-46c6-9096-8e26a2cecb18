"""API tests through FastAPI's TestClient."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

from conftest import (
    BUGGY_INCREMENTAL_DELETE_MISSING,
    CORRECT_FULL,
    CORRECT_INCREMENTAL,
    op_delete,
    op_insert,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def _payload(incremental: str) -> dict:
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
        "sql": {"full_select": CORRECT_FULL, "incremental": incremental},
    }


def test_health(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_minimize_reduces_and_reports_difference(client: TestClient):
    response = client.post("/v1/minimize", json=_payload(BUGGY_INCREMENTAL_DELETE_MISSING))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "reduced"
    assert [b["batch_id"] for b in body["reduced_input"]["batches"]] == [33, 44]
    assert body["first_difference_batch_id"] == 44
    assert body["difference"] == {"missing": [[3, 300]], "extra": [[3, 210]]}
    assert body["evaluations"] > 0
    assert body["reduced_input"]["sql"]["incremental"] == BUGGY_INCREMENTAL_DELETE_MISSING


def test_minimize_no_difference(client: TestClient):
    response = client.post("/v1/minimize", json=_payload(CORRECT_INCREMENTAL))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "no_difference"
    assert body["first_difference_batch_id"] is None
    assert body["difference"] is None
    # The reduced input is returned unchanged when nothing can be reduced.
    assert len(body["reduced_input"]["batches"]) == 4


def test_minimize_rejects_illegal_input(client: TestClient):
    payload = _payload(BUGGY_INCREMENTAL_DELETE_MISSING)
    payload["batches"][3]["changes"] = [
        {"op": "delete", "table": "refunds", "before": {"refund_id": 999}}
    ]
    response = client.post("/v1/minimize", json=payload)
    assert response.status_code == 422
    assert "不存在" in response.json()["detail"]


def test_minimize_rejects_unsafe_sql(client: TestClient):
    payload = _payload("DROP TABLE result")
    response = client.post("/v1/minimize", json=payload)
    assert response.status_code == 422
    assert "SQL" in response.json()["detail"]


def test_minimize_rejects_full_sql_writes(client: TestClient):
    payload = _payload(BUGGY_INCREMENTAL_DELETE_MISSING)
    payload["sql"]["full_select"] = "DELETE FROM orders"
    response = client.post("/v1/minimize", json=payload)
    assert response.status_code == 422


def test_validate_endpoint(client: TestClient):
    response = client.post("/v1/validate", json=_payload(BUGGY_INCREMENTAL_DELETE_MISSING))
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "different"
    assert body["first_difference_batch_id"] == 44
    assert body["difference"]["missing"] == [[3, 300]]


def test_timeout_is_undetermined_not_no_difference(client: TestClient):
    payload = _payload(BUGGY_INCREMENTAL_DELETE_MISSING)
    payload["sql"]["full_select"] = (
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c "
        "WHERE x < 100000000) SELECT count(*), 0 FROM c"
    )
    response = client.post("/v1/minimize", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "undetermined"
    assert body["first_difference_batch_id"] is None


def test_swagger_and_openapi_served(client: TestClient):
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/v1/minimize" in schema["paths"]
    assert "/v1/validate" in schema["paths"]


def test_example_leak_refund_delete(client: TestClient):
    payload = json.loads((EXAMPLES / "leak_refund_delete.json").read_text())
    response = client.post("/v1/minimize", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in ("reduced", "already_minimal")
    assert body["first_difference_batch_id"] == 103
    # Batches 101, 102, 104, 105 are irrelevant; 103 (refund delete) is the
    # bug.  It depends on 101 having created refund 9002.
    ids = [b["batch_id"] for b in body["reduced_input"]["batches"]]
    assert 103 in ids
    assert 101 in ids
    assert len(ids) < 6


def test_example_correct_incremental(client: TestClient):
    payload = json.loads((EXAMPLES / "correct_incremental.json").read_text())
    response = client.post("/v1/minimize", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "no_difference"


def test_concurrent_requests_are_isolated(client: TestClient):
    results: list[tuple[str, list[int]]] = []

    def call(incremental: str) -> None:
        response = client.post("/v1/minimize", json=_payload(incremental))
        body = response.json()
        results.append(
            (body["status"], [b["batch_id"] for b in body["reduced_input"]["batches"]])
        )

    threads = [
        threading.Thread(target=call, args=(BUGGY_INCREMENTAL_DELETE_MISSING,))
        for _ in range(3)
    ] + [
        threading.Thread(target=call, args=(CORRECT_INCREMENTAL,))
        for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 6
    assert sum(1 for status, _ in results if status == "reduced") == 3
    assert sum(1 for status, _ in results if status == "no_difference") == 3
    for status, ids in results:
        if status == "reduced":
            assert ids == [33, 44]
