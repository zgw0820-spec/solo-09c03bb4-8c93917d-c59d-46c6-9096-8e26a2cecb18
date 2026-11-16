"""Unit tests for the SQL safety policy."""

from __future__ import annotations

import pytest

from app import sqlguard
from app.sqlguard import SqlPolicyError


def test_full_allows_select_and_with():
    sqlguard.check_full("SELECT order_id, amount FROM orders")
    sqlguard.check_full(
        "WITH t AS (SELECT order_id FROM refunds) SELECT * FROM t"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE result SET net_amount = 0",
        "INSERT INTO orders VALUES (1, 1)",
        "DELETE FROM refunds",
        "SELECT * FROM orders; DROP TABLE orders",
        "SELECT * FROM sqlite_master",
        "SELECT * FROM result",
        "ATTACH DATABASE 'x' AS y",
        "PRAGMA writable_schema = 1",
    ],
)
def test_full_rejects_dangerous_sql(sql: str):
    with pytest.raises(SqlPolicyError):
        sqlguard.check_full(sql)


def test_incremental_allows_writes_to_result_only():
    sqlguard.check_incremental("UPDATE result SET net_amount = 1 WHERE 0")
    sqlguard.check_incremental(
        "INSERT INTO result(order_id, net_amount) SELECT 1, 2"
    )
    sqlguard.check_incremental("DELETE FROM result WHERE order_id = 1")


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE orders SET amount = 0",
        "DELETE FROM refunds",
        "INSERT INTO batch_changes(seq) VALUES (1)",
        "DROP TABLE result",
        "SELECT 1",
        "UPDATE result SET net_amount = 0; DROP TABLE result",
        "ATTACH DATABASE 'x' AS y",
        "BEGIN EXCLUSIVE",
    ],
)
def test_incremental_rejects_writes_to_other_tables(sql: str):
    with pytest.raises(SqlPolicyError):
        sqlguard.check_incremental(sql)


def test_incremental_allows_reads_of_sources():
    sqlguard.check_incremental(
        "UPDATE result SET net_amount = (SELECT SUM(amount) FROM refunds) WHERE 0"
    )


def test_split_statements_ignores_semicolons_in_literals():
    sql = "INSERT INTO result(order_id, net_amount) SELECT 1, 2;\n-- ;\nUPDATE result SET net_amount = 3 WHERE 0;"
    parts = sqlguard.split_statements(sql)
    assert len(parts) == 2
    parts = sqlguard.split_statements(
        "INSERT INTO result(order_id, net_amount) SELECT 1, 2 -- ;\n;"
    )
    assert len(parts) == 1


def test_authorizer_blocks_writes_from_full_sql():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE orders(order_id INTEGER, amount INTEGER)")
    conn.execute("CREATE TABLE result(order_id INTEGER, net_amount INTEGER)")
    conn.execute("INSERT INTO orders VALUES (1, 10)")
    with pytest.raises(Exception):
        with sqlguard.AuthorizerContext(conn, sqlguard.FULL):
            conn.execute("UPDATE orders SET amount = 0")


def test_authorizer_allows_result_write_in_incremental():
    import sqlite3

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE result(order_id INTEGER, net_amount INTEGER)")
    # The executor always opens the batch transaction before installing the
    # authorizer; implicit transactions are denied on purpose so a conflict
    # clause can never silently roll back the batch.
    conn.execute("BEGIN IMMEDIATE")
    with sqlguard.AuthorizerContext(conn, sqlguard.INCREMENTAL):
        conn.execute("INSERT INTO result VALUES (1, 5)")
    conn.execute("COMMIT")
    assert conn.execute("SELECT * FROM result").fetchall() == [(1, 5)]


def test_authorizer_blocks_read_of_result_in_full_mode():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE result(order_id INTEGER, net_amount INTEGER)")
    with pytest.raises(Exception):
        with sqlguard.AuthorizerContext(conn, sqlguard.FULL):
            conn.execute("SELECT * FROM result")


def test_authorizer_blocks_non_deterministic_functions():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE orders(order_id INTEGER)")
    for sql in ("SELECT random(), 1 FROM orders", "SELECT current_timestamp, 1"):
        with pytest.raises(sqlguard.SqlPolicyError, match="非确定性"):
            with sqlguard.AuthorizerContext(conn, sqlguard.FULL):
                conn.execute(sql)


def test_authorizer_allows_deterministic_functions():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE orders(order_id INTEGER)")
    with sqlguard.AuthorizerContext(conn, sqlguard.FULL):
        conn.execute("SELECT COALESCE(SUM(order_id), 0), ABS(-1) FROM orders")
