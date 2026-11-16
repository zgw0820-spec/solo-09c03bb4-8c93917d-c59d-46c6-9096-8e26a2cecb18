"""Reducer tests: minimality, order preservation, undeterminable handling."""

from __future__ import annotations

import pytest

from app.executor import Executor
from app.models import Batch, InitialData
from app.reducer import (
    STATUS_ALREADY_MINIMAL,
    STATUS_NO_DIFFERENCE,
    STATUS_REDUCED,
    STATUS_UNDETERMINED,
    Reducer,
)
from app.validate import InputError

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
                {"order_id": 3, "amount": 300},
            ],
            "refunds": [{"refund_id": 10, "order_id": 1, "amount": 200}],
        }
    )


def _independent_only_batches() -> list[Batch]:
    """Three unrelated orders; only the order-3 refund deletion trips the bug.

    Batch 11 touches order 1, batch 22 touches order 2 and batch 33/44 touch
    order 3.  The buggy SQL forgets refund deletions, so only batch 44
    produces a difference; batches 11 and 22 are pure noise.
    """
    return [
        Batch.model_validate(
            {"batch_id": 11, "changes": [op_insert("refunds", refund_id=20, order_id=1, amount=50)]}
        ),
        Batch.model_validate(
            {"batch_id": 22, "changes": [op_insert("refunds", refund_id=30, order_id=2, amount=70)]}
        ),
        Batch.model_validate(
            {"batch_id": 33, "changes": [op_insert("refunds", refund_id=21, order_id=3, amount=90)]}
        ),
        Batch.model_validate(
            {"batch_id": 44, "changes": [op_delete("refunds", refund_id=21)]}
        ),
    ]


def test_reducer_returns_minimal_ordered_counterexample():
    executor = Executor(CORRECT_FULL, BUGGY_INCREMENTAL_DELETE_MISSING)
    outcome = Reducer(_initial(), _independent_only_batches(), executor).run()
    assert outcome.status == STATUS_REDUCED
    assert [b.batch_id for b in outcome.reduced_batches] == [33, 44]
    assert outcome.first_difference_batch_id == 44
    assert outcome.difference == ([[3, 300]], [[3, 210]])


def test_reducer_keeps_original_order_not_sorted_order():
    # Original order is 33, 11, 44, 22 (not ascending); the surviving pair
    # must keep relative order (33 before 44).
    batches = _independent_only_batches()
    reordered = [batches[2], batches[0], batches[3], batches[1]]
    executor = Executor(CORRECT_FULL, BUGGY_INCREMENTAL_DELETE_MISSING)
    outcome = Reducer(_initial(), reordered, executor).run()
    assert [b.batch_id for b in outcome.reduced_batches] == [33, 44]
    assert outcome.first_difference_batch_id == 44


def test_reducer_reports_no_difference():
    executor = Executor(CORRECT_FULL, CORRECT_INCREMENTAL)
    outcome = Reducer(_initial(), _independent_only_batches(), executor).run()
    assert outcome.status == STATUS_NO_DIFFERENCE
    assert outcome.first_difference_batch_id is None
    assert outcome.difference is None


def test_already_minimal_when_nothing_can_be_removed():
    batches = [
        Batch.model_validate(
            {"batch_id": 1, "changes": [op_insert("refunds", refund_id=30, order_id=1, amount=10)]}
        ),
        Batch.model_validate(
            {"batch_id": 2, "changes": [op_delete("refunds", refund_id=30)]}
        ),
    ]
    executor = Executor(CORRECT_FULL, BUGGY_INCREMENTAL_DELETE_MISSING)
    outcome = Reducer(_initial(), batches, executor).run()
    assert outcome.status == STATUS_ALREADY_MINIMAL
    assert [b.batch_id for b in outcome.reduced_batches] == [1, 2]
    assert outcome.first_difference_batch_id == 2


def test_single_batch_difference_is_reported():
    batches = [
        Batch.model_validate(
            {
                "batch_id": 7,
                "changes": [
                    op_insert("refunds", refund_id=30, order_id=1, amount=10),
                    op_delete("refunds", refund_id=30),
                ],
            }
        )
    ]
    executor = Executor(CORRECT_FULL, BUGGY_INCREMENTAL_DELETE_MISSING)
    outcome = Reducer(_initial(), batches, executor).run()
    assert outcome.status == STATUS_ALREADY_MINIMAL
    assert outcome.first_difference_batch_id == 7


def test_illegal_subset_is_never_used_as_evidence():
    # Deleting batch 2 would leave batch 3 referencing a missing order; the
    # reducer must not "remove the difference" that way.
    batches = [
        Batch.model_validate(
            {"batch_id": 1, "changes": [op_insert("orders", order_id=50, amount=100)]}
        ),
        Batch.model_validate(
            {"batch_id": 2, "changes": [op_insert("refunds", refund_id=40, order_id=50, amount=10)]}
        ),
        Batch.model_validate(
            {"batch_id": 3, "changes": [op_delete("refunds", refund_id=40)]}
        ),
    ]
    executor = Executor(CORRECT_FULL, BUGGY_INCREMENTAL_DELETE_MISSING)
    outcome = Reducer(_initial(), batches, executor).run()
    assert outcome.status in (STATUS_REDUCED, STATUS_ALREADY_MINIMAL)
    ids = [b.batch_id for b in outcome.reduced_batches]
    if 3 in ids:
        assert 2 in ids, "批次 3 依赖批次 2 创建的 refund 40"


def test_erroring_removal_blocks_completion_claim():
    # The buggy SQL errors on batch 1 (references a missing column only when
    # reached).  Removing batches cannot produce a certifiable result.
    bad_inc = BUGGY_INCREMENTAL_DELETE_MISSING + "\nDELETE FROM result WHERE nope = 1;"
    batches = _independent_only_batches()
    executor = Executor(CORRECT_FULL, bad_inc)
    outcome = Reducer(_initial(), batches, executor).run()
    assert outcome.status == STATUS_UNDETERMINED
    assert outcome.first_difference_batch_id is None
    assert any("无法判定" in note for note in outcome.notes)


def test_original_input_must_be_legal():
    batches = [
        Batch.model_validate(
            {"batch_id": 1, "changes": [op_delete("orders", order_id=1)]}
        )
    ]
    executor = Executor(CORRECT_FULL, BUGGY_INCREMENTAL_DELETE_MISSING)
    with pytest.raises(InputError):
        Reducer(_initial(), batches, executor).run()


def test_reduced_input_replays_identically():
    """A second reducer run on the reduced input yields the same outcome."""
    executor = Executor(CORRECT_FULL, BUGGY_INCREMENTAL_DELETE_MISSING)
    first = Reducer(_initial(), _independent_only_batches(), executor).run()
    second = Reducer(_initial(), first.reduced_batches, executor).run()
    assert second.status == STATUS_ALREADY_MINIMAL
    assert [b.batch_id for b in second.reduced_batches] == [
        b.batch_id for b in first.reduced_batches
    ]
    assert second.first_difference_batch_id == first.first_difference_batch_id
    assert second.difference == first.difference
