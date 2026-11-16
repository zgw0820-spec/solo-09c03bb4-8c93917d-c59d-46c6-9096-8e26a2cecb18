"""Pure validation of the initial state and the change batches.

These checks are independent of any SQL execution and guarantee the
invariants required by the task:

* the initial source data is legal (unique keys, refunds reference orders);
* each batch starts from the state produced by the previous batch;
* every update/delete targets a row that exists at that point;
* primary keys stay unique and foreign keys stay valid after every batch;
* batch ids keep the order supplied, so the "first difference" refers to the
  original batch id even after reduction.

`before` is an *identifier/snapshot*: only the primary key of ``before`` is
required.  Extra fields may be supplied and are validated for type, but they
are not required to match the current row.  This implements the stated rule
"改删对象须存在" without imposing an unstated equality requirement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import Batch, ChangeOp, InitialData

ORDER_FIELDS = ("order_id", "amount")
REFUND_FIELDS = ("refund_id", "order_id", "amount")

TABLE_FIELDS = {"orders": ORDER_FIELDS, "refunds": REFUND_FIELDS}
TABLE_PK = {"orders": "order_id", "refunds": "refund_id"}


class InputError(ValueError):
    """Initial data or a batch is illegal."""


@dataclass
class CdcRecord:
    """Before/after image of one change, exposed to the incremental SQL."""

    table: str
    op: str
    old: dict[str, Any] | None
    new: dict[str, Any] | None
    seq: int


@dataclass
class SourceState:
    orders: dict[int, dict[str, Any]] = field(default_factory=dict)
    refunds: dict[int, dict[str, Any]] = field(default_factory=dict)

    def clone(self) -> "SourceState":
        return SourceState(
            orders={k: dict(v) for k, v in self.orders.items()},
            refunds={k: dict(v) for k, v in self.refunds.items()},
        )

    def table(self, name: str) -> dict[int, dict[str, Any]]:
        return self.orders if name == "orders" else self.refunds

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "orders": [dict(self.orders[k]) for k in sorted(self.orders)],
            "refunds": [dict(self.refunds[k]) for k in sorted(self.refunds)],
        }


#: SQLite integers are signed 64-bit; amounts outside this range would make a
#: trial fail with an overflow instead of a meaningful verdict.
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


def _check_int64(value: int, *, name: str, where: str) -> None:
    if value < INT64_MIN or value > INT64_MAX:
        raise InputError(f"{where}: {name} 超出 64 位整数范围")


def _coerce_row(
    table: str, raw: dict[str, Any], *, where: str, partial: bool
) -> dict[str, Any]:
    fields = TABLE_FIELDS.get(table)
    if fields is None:
        raise InputError(f"{where}: 未知表 {table!r}")
    if not isinstance(raw, dict):
        raise InputError(f"{where}: 行必须是 JSON 对象")
    extra = set(raw) - set(fields)
    if extra:
        raise InputError(f"{where}: 未知字段 {sorted(extra)}")
    pk = TABLE_PK[table]
    if pk not in raw:
        raise InputError(f"{where}: 缺少主键 {pk}")
    if not partial:
        missing = set(fields) - set(raw)
        if missing:
            raise InputError(f"{where}: 缺少字段 {sorted(missing)}")
    row: dict[str, Any] = {}
    for name in fields:
        if name not in raw:
            continue
        value = raw[name]
        if not isinstance(value, int) or isinstance(value, bool):
            raise InputError(f"{where}: {name} 必须是整数")
        _check_int64(value, name=name, where=where)
        row[name] = value
    if row[pk] < 1:
        raise InputError(f"{where}: {pk} 必须为正整数")
    return row


def _check_fk(state: SourceState, *, where: str) -> None:
    for refund in state.refunds.values():
        if refund["order_id"] not in state.orders:
            raise InputError(
                f"{where}: refunds.refund_id={refund['refund_id']} 引用了不存在的 "
                f"order_id={refund['order_id']}"
            )


def build_initial_state(initial: InitialData) -> SourceState:
    state = SourceState()
    for row in initial.orders:
        if row.order_id in state.orders:
            raise InputError(f"初始 orders 中 order_id={row.order_id} 重复")
        _check_int64(row.amount, name="amount", where="初始 orders")
        state.orders[row.order_id] = {"order_id": row.order_id, "amount": row.amount}
    for row in initial.refunds:
        if row.refund_id in state.refunds:
            raise InputError(f"初始 refunds 中 refund_id={row.refund_id} 重复")
        _check_int64(row.amount, name="amount", where="初始 refunds")
        state.refunds[row.refund_id] = {
            "refund_id": row.refund_id,
            "order_id": row.order_id,
            "amount": row.amount,
        }
    _check_fk(state, where="初始数据")
    return state


def apply_batch_recording(
    state: SourceState, batch: Batch, *, where: str
) -> tuple[SourceState, list[CdcRecord]]:
    """Apply ``batch`` to ``state``, returning the new state and CDC images.

    ``state`` itself is not mutated.  Raises :class:`InputError` for any
    illegal change (missing target, duplicate key, dangling FK).
    """
    new_state = state.clone()
    records: list[CdcRecord] = []
    for index, op in enumerate(batch.changes):
        loc = f"{where} 第 {index + 1} 个变更"
        records.append(_apply_op(new_state, op, where=loc, seq=index))
    _check_fk(new_state, where=where)
    return new_state, records


def apply_batch(state: SourceState, batch: Batch, *, where: str) -> SourceState:
    return apply_batch_recording(state, batch, where=where)[0]


def _apply_op(
    state: SourceState, op: ChangeOp, *, where: str, seq: int
) -> CdcRecord:
    table = op.table
    fields = TABLE_FIELDS[table]
    pk = TABLE_PK[table]
    rows = state.table(table)

    if op.op == "insert":
        row = _coerce_row(table, op.after or {}, where=f"{where} after", partial=False)
        if row[pk] in rows:
            raise InputError(f"{where}: insert 的 {pk}={row[pk]} 已存在")
        rows[row[pk]] = row
        return CdcRecord(table, "insert", None, dict(row), seq)

    before = _coerce_row(
        table, op.before or {}, where=f"{where} before", partial=True
    )
    existing = rows.get(before[pk])
    if existing is None:
        raise InputError(f"{where}: {op.op} 的目标 {pk}={before[pk]} 不存在")

    if op.op == "delete":
        old = dict(existing)
        del rows[before[pk]]
        return CdcRecord(table, "delete", old, None, seq)

    after = _coerce_row(table, op.after or {}, where=f"{where} after", partial=False)
    if after[pk] != before[pk] and after[pk] in rows:
        raise InputError(f"{where}: update 目标主键 {pk}={after[pk]} 已存在")
    old = dict(existing)
    del rows[before[pk]]
    rows[after[pk]] = after
    return CdcRecord(table, "update", old, dict(after), seq)


def replay(
    initial: InitialData, batches: list[Batch]
) -> tuple[SourceState, list[SourceState]]:
    """Return the initial state and the state after every batch.

    Raises :class:`InputError` if any batch is illegal.
    """
    state = build_initial_state(initial)
    states = [state]
    for batch in batches:
        state = apply_batch(state, batch, where=f"批次 {batch.batch_id}")
        states.append(state)
    return states[0], states[1:]


def validate_all(initial: InitialData, batches: list[Batch]) -> None:
    replay(initial, batches)
