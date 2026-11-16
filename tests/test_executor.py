"""Executor tests: multiset comparison, first difference, budgets."""

from __future__ import annotations

import pytest

from app.executor import Executor, OUTCOME_DIFFERENT, OUTCOME_ERROR, OUTCOME_OK, OUTCOME_TIMEOUT
from app.models import Batch, ChangeOp, InitialData

from conftest import (
    BUGGY_INCREMENTAL_DELETE_MISSING,
    CORRECT_FULL,
    CORRECT_INCREMENTAL,
    op_delete,
    op_insert,
    op_update,
)


def _initial() -> InitialData:
    return InitialData.model_validate(
        {
            "orders": [
                {"order_id": 1, "amount": 1000},
                {"order_id": 2, "amount": 500},
            ],
            "refunds": [{"refund_id": 10, "order_id": 1, "amount": 200}],
        }
    )


def _batches() -> list[Batch]:
    return [
        Batch.model_validate(
            {"batch_id": 1, "changes": [op_insert("refunds", refund_id=11, order_id=1, amount=100)]}
        ),
        Batch.model_validate(
            {"batch_id": 2, "changes": [op_insert("orders", order_id=3, amount=700)]}
        ),
        Batch.model_validate(
            {"batch_id": 3, "changes": [op_update("orders", {"order_id": 3}, {"order_id": 3, "amount": 900})]}
        ),
        Batch.model_validate(
            {"batch_id": 4, "changes": [op_delete("refunds", refund_id=11)]}
        ),
    ]


def test_correct_incremental_has_no_difference():
    result = Executor(CORRECT_FULL, CORRECT_INCREMENTAL).run(_initial(), _batches())
    assert result.outcome == OUTCOME_OK


def test_buggy_incremental_reports_first_difference_multiset():
    result = Executor(CORRECT_FULL, BUGGY_INCREMENTAL_DELETE_MISSING).run(
        _initial(), _batches()
    )
    assert result.outcome == OUTCOME_DIFFERENT
    diff = result.first_difference
    assert diff is not None
    assert diff.batch_id == 4
    # order 1: full => 1000-200=800, result still 1000-200-100=700.
    assert diff.missing == [[1, 800]]
    assert diff.extra == [[1, 700]]


def test_duplicate_rows_are_counted():
    # Full SQL deliberately returns a duplicate pair; result is loaded with
    # both copies, so a correct incremental run stays equal.
    full = "SELECT order_id, amount AS net_amount FROM orders WHERE order_id = 1 UNION ALL SELECT order_id, amount FROM orders WHERE order_id = 1"
    inc = "UPDATE result SET net_amount = net_amount WHERE 0"
    result = Executor(full, inc).run(_initial(), _batches())
    assert result.outcome == OUTCOME_OK


def test_duplicate_extra_row_is_detected():
    full = "SELECT order_id, amount AS net_amount FROM orders WHERE order_id = 1"
    # Buggy incremental: inserts a spurious duplicate row on batch 1.
    inc = (
        "INSERT INTO result(order_id, net_amount) "
        "SELECT 1, amount FROM orders WHERE order_id = 1;"
    )
    initial = InitialData.model_validate(
        {"orders": [{"order_id": 1, "amount": 1000}], "refunds": []}
    )
    result = Executor(full, inc).run(
        initial,
        [
            Batch.model_validate(
                {"batch_id": 1, "changes": [op_update("orders", {"order_id": 1}, {"order_id": 1, "amount": 1000})]}
            )
        ],
    )
    assert result.outcome == OUTCOME_DIFFERENT
    assert result.first_difference is not None
    assert result.first_difference.extra == [[1, 1000]]


def test_full_sql_error_is_not_a_difference():
    result = Executor("SELECT nope FROM orders", "UPDATE result SET net_amount = 0 WHERE 0").run(
        _initial(), _batches()
    )
    assert result.outcome == OUTCOME_ERROR


def test_incremental_sql_error_is_undeterminable():
    result = Executor(
        CORRECT_FULL, "UPDATE result SET missing_column = 1 WHERE 0"
    ).run(_initial(), _batches())
    assert result.outcome == OUTCOME_ERROR
    assert "SQL" in result.notes[0]


def test_full_sql_cannot_write():
    with pytest.raises(Exception):
        Executor(
            "DELETE FROM orders", "UPDATE result SET net_amount = 0 WHERE 0"
        )


def test_incremental_cannot_touch_source_tables():
    with pytest.raises(Exception):
        Executor(
            CORRECT_FULL, "UPDATE orders SET amount = 0 WHERE 0"
        )


def test_statement_timeout_is_undeterminable():
    full = (
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c "
        "WHERE x < 100000000) SELECT count(*), 0 FROM c"
    )
    result = Executor(full, "UPDATE result SET net_amount = 0 WHERE 0").run(
        _initial(), _batches()
    )
    assert result.outcome == OUTCOME_TIMEOUT


def test_db_is_rebuilt_for_each_trial():
    # Running the same buggy trial twice must yield identical differences and
    # leave no state behind.
    executor = Executor(CORRECT_FULL, BUGGY_INCREMENTAL_DELETE_MISSING)
    first = executor.run(_initial(), _batches())
    second = executor.run(_initial(), _batches())
    assert first.first_difference == second.first_difference


def test_integral_float_from_full_sql_matches_integer_result():
    # A full SQL returning REAL net amounts is stored as INTEGER by result's
    # column affinity; the comparison must not report a spurious difference.
    full = (
        "SELECT o.order_id, "
        "CAST(o.amount - COALESCE(SUM(r.amount), 0) AS REAL) AS net_amount "
        "FROM orders o LEFT JOIN refunds r ON r.order_id = o.order_id "
        "GROUP BY o.order_id, o.amount"
    )
    inc = (
        "UPDATE result SET net_amount = 0 WHERE 0;"
    )
    initial = InitialData.model_validate(
        {"orders": [{"order_id": 1, "amount": 1000}], "refunds": []}
    )
    result = Executor(full, inc).run(
        initial,
        [
            Batch.model_validate(
                {"batch_id": 1, "changes": [op_update("orders", {"order_id": 1}, {"order_id": 1, "amount": 1000})]}
            )
        ],
    )
    assert result.outcome == OUTCOME_OK


def test_incremental_writing_wrong_table_is_rejected_before_execution():
    with pytest.raises(Exception):
        Executor(
            CORRECT_FULL,
            "INSERT INTO orders(order_id, amount) SELECT 1, 1;"
            "UPDATE result SET net_amount = 0 WHERE 0",
        )
