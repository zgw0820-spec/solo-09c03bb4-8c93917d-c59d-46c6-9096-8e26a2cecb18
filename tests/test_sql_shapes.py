"""Regression tests for adversarial / tricky SQL shapes."""

from __future__ import annotations

import pytest

from app import sqlguard
from app.executor import Executor
from app.models import Batch, InitialData

from conftest import CORRECT_FULL


def _initial() -> InitialData:
    return InitialData.model_validate(
        {"orders": [{"order_id": 1, "amount": 1000}], "refunds": []}
    )


def _batches() -> list[Batch]:
    return [
        Batch.model_validate(
            {
                "batch_id": 1,
                "changes": [
                    {"op": "insert", "table": "orders",
                     "after": {"order_id": 2, "amount": 500}}
                ],
            }
        )
    ]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT order_id, replace(hex(zeroblob(2)), '0', 'a') FROM orders",
        "SELECT order_id, amount FROM orders WHERE order_id IN "
        "(SELECT order_id FROM refunds)",
        "WITH t AS (SELECT order_id FROM refunds) SELECT order_id, 0 FROM t",
    ],
)
def test_full_select_accepts_function_calls_and_ctes(sql: str):
    sqlguard.check_full(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "WITH t AS (SELECT order_id FROM orders) DELETE FROM orders",
        "SELECT order_id, amount FROM orders; DELETE FROM orders",
        "SELECT order_id, 0 FROM orders; SELECT 1, 0",
    ],
)
def test_full_select_rejects_hidden_writes(sql: str):
    with pytest.raises(sqlguard.SqlPolicyError):
        sqlguard.check_full(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT OR REPLACE INTO result(order_id, net_amount) VALUES (1, 2)",
        "INSERT INTO result(order_id, net_amount) VALUES (1, 2) "
        "ON CONFLICT(order_id) DO UPDATE SET net_amount = excluded.net_amount",
        "REPLACE INTO result(order_id, net_amount) VALUES (1, 2)",
        "WITH t AS (SELECT order_id FROM orders) INSERT INTO result "
        "SELECT order_id, 0 FROM t",
    ],
)
def test_incremental_accepts_valid_write_forms(sql: str):
    sqlguard.check_incremental(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO result(order_id, net_amount) SELECT 1, 0; DELETE FROM orders",
        "INSERT INTO result(order_id, net_amount) VALUES (1, 0); "
        "UPDATE orders SET amount = 0",
        "UPDATE result SET net_amount = 0 WHERE 0; ATTACH DATABASE 'x' AS y",
    ],
)
def test_incremental_rejects_smuggled_writes(sql: str):
    with pytest.raises(sqlguard.SqlPolicyError):
        sqlguard.check_incremental(sql)


def test_authorizer_denies_pragma_table_functions():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    with pytest.raises(sqlguard.SqlPolicyError):
        with sqlguard.AuthorizerContext(conn, sqlguard.FULL):
            conn.execute("SELECT * FROM pragma_table_info('orders')").fetchall()


def test_non_deterministic_sql_is_undeterminable_not_difference():
    result = Executor(
        "SELECT order_id, random() FROM orders",
        "UPDATE result SET net_amount = net_amount WHERE 0",
    ).run(_initial(), _batches())
    assert result.outcome == "error"
    assert result.first_difference is None
