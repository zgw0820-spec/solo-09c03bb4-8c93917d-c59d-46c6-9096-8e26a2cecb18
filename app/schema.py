"""Schema creation and deterministic serialization helpers.

All validation runs against a private, in-memory SQLite database.  Every
trial rebuilds the database from the untouched initial state, so a failed,
slow or malicious trial can never leak into another trial or request.

Tables visible to submitted SQL
-------------------------------

``orders``          order_id INTEGER, amount INTEGER      (source, read-only)
``refunds``         refund_id INTEGER, order_id INTEGER,  (source, read-only)
                    amount INTEGER
``batch_changes``   before/after images of the current batch (incremental only)
``result``          order_id, net_amount                 (incremental writes)

``result`` deliberately has **no** primary key or unique constraint: the
comparison is a multiset comparison, so a buggy incremental SQL that inserts
a duplicate row must be detectable as an extra row.  ``full_select`` may also
return duplicate rows; those are loaded verbatim into ``result`` and counted.

``batch_changes`` columns (all NULLable; per-row only the relevant ones are
populated)::

    seq, tbl, op,
    old_order_id, old_amount,
    old_refund_id, old_refund_order_id, old_refund_amount,
    new_order_id, new_amount,
    new_refund_id, new_refund_order_id, new_refund_amount

    tbl = 'orders'  -> use old/new_order_id and old/new_amount
    tbl = 'refunds' -> use old/new_refund_id, old/new_refund_order_id,
                       old/new_refund_amount
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable, Sequence

from .validate import CdcRecord

SOURCE_DDL = (
    """
    CREATE TABLE orders (
        order_id INTEGER PRIMARY KEY,
        amount   INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE refunds (
        refund_id INTEGER PRIMARY KEY,
        order_id  INTEGER NOT NULL REFERENCES orders(order_id),
        amount    INTEGER NOT NULL
    )
    """,
)

RESULT_DDL = """
CREATE TABLE result (
    order_id   INTEGER,
    net_amount INTEGER
)
"""

CDC_DDL = """
CREATE TABLE batch_changes (
    seq                  INTEGER NOT NULL,
    tbl                  TEXT    NOT NULL,
    op                   TEXT    NOT NULL,
    old_order_id         INTEGER,
    old_amount           INTEGER,
    old_refund_id        INTEGER,
    old_refund_order_id  INTEGER,
    old_refund_amount    INTEGER,
    new_order_id         INTEGER,
    new_amount           INTEGER,
    new_refund_id        INTEGER,
    new_refund_order_id  INTEGER,
    new_refund_amount    INTEGER
)
"""

ALL_TABLES = ("orders", "refunds", "result", "batch_changes")
SOURCE_TABLES = ("orders", "refunds")
RESULT_TABLE = "result"
CDC_TABLE = "batch_changes"
RESULT_COLUMNS = ("order_id", "net_amount")


def create_schema(conn: sqlite3.Connection, *, with_result: bool = True) -> None:
    for stmt in SOURCE_DDL:
        conn.execute(stmt)
    if with_result:
        conn.execute(RESULT_DDL)
        conn.execute(CDC_DDL)


def reset_result(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM result")


def set_initial_source_rows(
    conn: sqlite3.Connection,
    orders: Iterable[dict[str, Any]],
    refunds: Iterable[dict[str, Any]],
) -> None:
    conn.execute("DELETE FROM batch_changes")
    conn.execute("DELETE FROM refunds")
    conn.execute("DELETE FROM orders")
    conn.executemany(
        "INSERT INTO orders(order_id, amount) VALUES (?, ?)",
        [(r["order_id"], r["amount"]) for r in orders],
    )
    conn.executemany(
        "INSERT INTO refunds(refund_id, order_id, amount) VALUES (?, ?, ?)",
        [(r["refund_id"], r["order_id"], r["amount"]) for r in refunds],
    )


_CDC_COLUMNS = (
    "seq", "tbl", "op",
    "old_order_id", "old_amount",
    "old_refund_id", "old_refund_order_id", "old_refund_amount",
    "new_order_id", "new_amount",
    "new_refund_id", "new_refund_order_id", "new_refund_amount",
)


def _cdc_row(record: CdcRecord) -> tuple[Any, ...]:
    values: dict[str, Any] = {name: None for name in _CDC_COLUMNS}
    values["seq"] = record.seq
    values["tbl"] = record.table
    values["op"] = record.op
    if record.table == "orders":
        if record.old:
            values["old_order_id"] = record.old["order_id"]
            values["old_amount"] = record.old["amount"]
        if record.new:
            values["new_order_id"] = record.new["order_id"]
            values["new_amount"] = record.new["amount"]
    else:
        if record.old:
            values["old_refund_id"] = record.old["refund_id"]
            values["old_refund_order_id"] = record.old["order_id"]
            values["old_refund_amount"] = record.old["amount"]
        if record.new:
            values["new_refund_id"] = record.new["refund_id"]
            values["new_refund_order_id"] = record.new["order_id"]
            values["new_refund_amount"] = record.new["amount"]
    return tuple(values[name] for name in _CDC_COLUMNS)


def set_batch_changes(conn: sqlite3.Connection, records: Sequence[CdcRecord]) -> None:
    conn.execute("DELETE FROM batch_changes")
    conn.executemany(
        f"INSERT INTO batch_changes({', '.join(_CDC_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in _CDC_COLUMNS)})",
        [_cdc_row(r) for r in records],
    )


def normalize_value(value: Any) -> Any:
    """Make result values comparable across SQL/column-affinity round trips.

    ``result.net_amount`` has INTEGER affinity, so a full SQL that returns
    ``3800.0`` is stored (and read back) as ``3800``.  Integral floats are
    therefore canonicalised to ints so that such rows compare equal; other
    floats are kept as-is (NaN/Inf have no JSON representation, so they are
    rendered as tagged strings).
    """
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        if value != value:  # NaN
            return "NaN"
        if value in (float("inf"), float("-inf")):
            return "Infinity" if value > 0 else "-Infinity"
        if value.is_integer() and abs(value) < 2**63:
            return int(value)
        return value
    return value


def canonical_row(row: Iterable[Any]) -> tuple[Any, ...]:
    return tuple(normalize_value(v) for v in row)


def rows_to_json(rows: Iterable[Sequence[Any]]) -> list[list[Any]]:
    return [[normalize_value(v) for v in row] for row in rows]
