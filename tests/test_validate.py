"""Unit tests for input validation and replay legality."""

from __future__ import annotations

import pytest

from app.models import Batch, ChangeOp, InitialData
from app.validate import InputError, apply_batch, build_initial_state, replay


def _init() -> InitialData:
    return InitialData.model_validate(
        {
            "orders": [{"order_id": 1, "amount": 1000}],
            "refunds": [{"refund_id": 10, "order_id": 1, "amount": 100}],
        }
    )


def test_insert_requires_new_key():
    state = build_initial_state(_init())
    batch = Batch(
        batch_id=1,
        changes=[ChangeOp(op="insert", table="orders", after={"order_id": 1, "amount": 1})],
    )
    with pytest.raises(InputError, match="已存在"):
        apply_batch(state, batch, where="t")


def test_update_delete_target_must_exist():
    state = build_initial_state(_init())
    with pytest.raises(InputError, match="不存在"):
        apply_batch(
            state,
            Batch(
                batch_id=1,
                changes=[
                    ChangeOp(
                        op="update",
                        table="orders",
                        before={"order_id": 99},
                        after={"order_id": 99, "amount": 1},
                    )
                ],
            ),
            where="t",
        )
    with pytest.raises(InputError, match="不存在"):
        apply_batch(
            state,
            Batch(
                batch_id=1,
                changes=[ChangeOp(op="delete", table="refunds", before={"refund_id": 99})],
            ),
            where="t",
        )


def test_before_only_needs_primary_key():
    state = build_initial_state(_init())
    new_state = apply_batch(
        state,
        Batch(
            batch_id=1,
            changes=[ChangeOp(op="delete", table="refunds", before={"refund_id": 10})],
        ),
        where="t",
    )
    assert new_state.refunds == {}


def test_fk_must_hold_after_batch():
    state = build_initial_state(_init())
    with pytest.raises(InputError, match="不存在的 order_id"):
        apply_batch(
            state,
            Batch(
                batch_id=1,
                changes=[ChangeOp(op="delete", table="orders", before={"order_id": 1})],
            ),
            where="t",
        )


def test_update_can_rename_key():
    state = build_initial_state(_init())
    new_state = apply_batch(
        state,
        Batch(
            batch_id=1,
            changes=[
                ChangeOp(
                    op="update",
                    table="orders",
                    before={"order_id": 1},
                    after={"order_id": 2, "amount": 1000},
                ),
                ChangeOp(
                    op="update",
                    table="refunds",
                    before={"refund_id": 10},
                    after={"refund_id": 10, "order_id": 2, "amount": 100},
                ),
            ],
        ),
        where="t",
    )
    assert set(new_state.orders) == {2}
    assert new_state.refunds[10]["order_id"] == 2


def test_duplicate_initial_keys_rejected():
    with pytest.raises(InputError, match="重复"):
        build_initial_state(
            InitialData.model_validate(
                {
                    "orders": [
                        {"order_id": 1, "amount": 1},
                        {"order_id": 1, "amount": 2},
                    ],
                    "refunds": [],
                }
            )
        )


def test_dangling_initial_refund_rejected():
    with pytest.raises(InputError, match="不存在的 order_id"):
        build_initial_state(
            InitialData.model_validate(
                {
                    "orders": [],
                    "refunds": [{"refund_id": 1, "order_id": 5, "amount": 1}],
                }
            )
        )


def test_initial_row_limit_is_100_total():
    orders = [{"order_id": i, "amount": 1} for i in range(1, 101)]
    refunds = [{"refund_id": 1, "order_id": 1, "amount": 1}]
    with pytest.raises(Exception):
        InitialData.model_validate({"orders": orders, "refunds": refunds})


def test_replay_is_deterministic():
    initial = _init()
    batches = [
        Batch(
            batch_id=4,
            changes=[
                ChangeOp(
                    op="update",
                    table="orders",
                    before={"order_id": 1},
                    after={"order_id": 1, "amount": 1200},
                )
            ],
        )
    ]
    _, first = replay(initial, batches)
    _, second = replay(initial, batches)
    assert first[0].snapshot() == second[0].snapshot()
