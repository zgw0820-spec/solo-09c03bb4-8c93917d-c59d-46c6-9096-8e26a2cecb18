"""Shared fixtures and SQL helpers for the test-suite."""

from __future__ import annotations

import pytest

CORRECT_FULL = """
SELECT o.order_id,
       o.amount - COALESCE(SUM(r.amount), 0) AS net_amount
FROM orders o
LEFT JOIN refunds r ON r.order_id = o.order_id
GROUP BY o.order_id, o.amount
"""

# Correct incremental maintenance for the subset of changes exercised by the
# tests: order insert, order update (amount), refund insert, refund delete.
CORRECT_INCREMENTAL = """
INSERT INTO result(order_id, net_amount)
SELECT new_order_id, new_amount
FROM batch_changes
WHERE tbl = 'orders' AND op = 'insert';

UPDATE result
SET net_amount = net_amount + b.delta
FROM (
    SELECT old_order_id AS oid, new_amount - old_amount AS delta
    FROM batch_changes
    WHERE tbl = 'orders' AND op = 'update'
) AS b
WHERE result.order_id = b.oid;

DELETE FROM result
WHERE order_id IN (
    SELECT old_order_id FROM batch_changes
    WHERE tbl = 'orders' AND op = 'delete'
);

UPDATE result
SET net_amount = result.net_amount - b.amt
FROM (
    SELECT new_refund_order_id AS oid, SUM(new_refund_amount) AS amt
    FROM batch_changes
    WHERE tbl = 'refunds' AND op = 'insert'
    GROUP BY new_refund_order_id
) AS b
WHERE result.order_id = b.oid;

UPDATE result
SET net_amount = result.net_amount + b.amt
FROM (
    SELECT old_refund_order_id AS oid, SUM(old_refund_amount) AS amt
    FROM batch_changes
    WHERE tbl = 'refunds' AND op = 'delete'
    GROUP BY old_refund_order_id
) AS b
WHERE result.order_id = b.oid;
"""

# Bug: refund deletion is never reflected in result.
BUGGY_INCREMENTAL_DELETE_MISSING = """
INSERT INTO result(order_id, net_amount)
SELECT new_order_id, new_amount
FROM batch_changes
WHERE tbl = 'orders' AND op = 'insert';

UPDATE result
SET net_amount = net_amount + b.delta
FROM (
    SELECT old_order_id AS oid, new_amount - old_amount AS delta
    FROM batch_changes
    WHERE tbl = 'orders' AND op = 'update'
) AS b
WHERE result.order_id = b.oid;

UPDATE result
SET net_amount = result.net_amount - b.amt
FROM (
    SELECT new_refund_order_id AS oid, SUM(new_refund_amount) AS amt
    FROM batch_changes
    WHERE tbl = 'refunds' AND op = 'insert'
    GROUP BY new_refund_order_id
) AS b
WHERE result.order_id = b.oid;
"""


@pytest.fixture
def correct_sql() -> dict[str, str]:
    return {
        "full_select": CORRECT_FULL,
        "incremental": CORRECT_INCREMENTAL,
    }


@pytest.fixture
def buggy_sql() -> dict[str, str]:
    return {
        "full_select": CORRECT_FULL,
        "incremental": BUGGY_INCREMENTAL_DELETE_MISSING,
    }


def op_insert(table: str, **row) -> dict:
    return {"op": "insert", "table": table, "after": row}


def op_update(table: str, before: dict, after: dict) -> dict:
    return {"op": "update", "table": table, "before": before, "after": after}


def op_delete(table: str, **before) -> dict:
    return {"op": "delete", "table": table, "before": before}
