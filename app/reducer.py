"""Delta-debugging style reduction of the change batches.

Semantics implemented (each rule mirrors the task statement):

* Start from a trial on the *complete* input.  If it shows no difference,
  there is nothing to reduce (``no_difference``).  If it is undeterminable
  (SQL error / >1 s / policy violation), we cannot conclude anything
  (``undetermined``).
* A trial is evidence only when it is **valid**:
  - the source-row changes are legal from the canonical initial state
    (update/delete targets exist, primary keys unique, foreign keys valid
    after the batch);
  - the full SQL is executed read-only and the incremental SQL goes through
    the authorizer;
  - no statement errors, is rejected, or exceeds the per-statement budget.
  Error/timeout trials are never used to justify keeping or dropping a batch.
* Reduction only *removes* batches and preserves the original order.  A batch
  (or chunk of batches) may be removed only if the reduced input stays legal
  **and** still shows a difference.
* Completion is certified by a final pass that removes each remaining batch
  individually: if every such removal makes the input illegal or eliminates
  the difference, the reduction is complete.  If any single removal is
  undeterminable, we do not claim completion (``undetermined``), because an
  error/timeout is explicitly not a valid reduction result.

Chunk removal (ddmin style) lets the reducer escape local minima that
single-batch greedy removal could get stuck in; a final single-batch
certification pass guarantees the returned set is 1-minimal.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import config
from .executor import OUTCOME_DIFFERENT, OUTCOME_TIMEOUT, Executor, TrialResult
from .models import Batch, InitialData
from .validate import InputError, replay

STATUS_REDUCED = "reduced"
STATUS_ALREADY_MINIMAL = "already_minimal"
STATUS_NO_DIFFERENCE = "no_difference"
STATUS_UNDETERMINED = "undetermined"


class BudgetExhausted(Exception):
    """The per-request budget is spent; stop without claiming completion."""


@dataclass
class ReductionOutcome:
    status: str
    reduced_batches: list[Batch]
    first_difference_batch_id: int | None
    difference: tuple[list[list], list[list]] | None
    notes: list[str] = field(default_factory=list)
    evaluations: int = 0
    #: Diagnoses that blocked a completion claim (timeouts / SQL errors).
    blocked_by: list[str] = field(default_factory=list)


class Reducer:
    def __init__(
        self,
        initial: InitialData,
        batches: list[Batch],
        executor: Executor,
        *,
        request_deadline: float | None = None,
    ) -> None:
        self.initial = initial
        self.batches = list(batches)
        self.executor = executor
        self.request_deadline = (
            request_deadline
            if request_deadline is not None
            else time.monotonic() + config.MAX_REQUEST_SECONDS
        )
        self.evaluations = 0
        self._cache: dict[tuple[int, ...], tuple[TrialResult, str | None]] = {}

    # -- trial cache -----------------------------------------------------
    @staticmethod
    def _key(batches: list[Batch]) -> tuple[int, ...]:
        return tuple(b.batch_id for b in batches)

    def _budget_ok(self) -> bool:
        return (
            time.monotonic() <= self.request_deadline
            and self.evaluations < config.MAX_TRIALS
        )

    def trial(self, batches: list[Batch]) -> tuple[TrialResult, str | None]:
        """Return ``(result, invalid_reason)`` for one candidate input.

        ``invalid_reason`` is set when the candidate is not a legal input
        (missing update/delete target, broken FK, ...); such a candidate is
        never usable as evidence either way.
        """
        key = self._key(batches)
        if key in self._cache:
            return self._cache[key]
        if not self._budget_ok():
            raise BudgetExhausted
        try:
            replay(self.initial, batches)
        except InputError as exc:
            result = TrialResult("invalid", notes=[str(exc)])
            self._cache[key] = (result, str(exc))
            self.evaluations += 1
            return self._cache[key]
        result = self.executor.run(
            self.initial, batches, deadline=self.request_deadline
        )
        self._cache[key] = (result, None)
        self.evaluations += 1
        return self._cache[key]

    @staticmethod
    def _usable_difference(result: TrialResult, invalid: str | None) -> bool:
        return invalid is None and result.outcome == OUTCOME_DIFFERENT

    # -- main loop -------------------------------------------------------
    def run(self) -> ReductionOutcome:
        base_result, base_invalid = self.trial(self.batches)
        if base_invalid is not None:
            raise InputError(base_invalid)

        if base_result.outcome == OUTCOME_DIFFERENT:
            assert base_result.first_difference is not None
            current = self.batches
            current_diff = _diff_tuple(base_result)
        elif base_result.outcome == OUTCOME_TIMEOUT:
            return self._undetermined_from_base(
                "完整输入无法判定（执行超过 "
                f"{config.STATEMENT_TIMEOUT_SECONDS:g} 秒），不能缩减",
                base_result,
            )
        elif base_result.outcome == "error":
            return self._undetermined_from_base(
                "完整输入无法判定（SQL 报错或被安全策略拒绝），不能缩减",
                base_result,
            )
        else:
            return ReductionOutcome(
                status=STATUS_NO_DIFFERENCE,
                reduced_batches=self.batches,
                first_difference_batch_id=None,
                difference=None,
                notes=["完整输入未发现差异，无法缩减"] + base_result.notes,
                evaluations=self.evaluations,
            )

        changed = False
        budget_exhausted = False
        try:
            current, current_diff, changed = self._reduce(current, current_diff)
        except BudgetExhausted:
            # Keep whatever reduction was already accepted: `_reduce` updates
            # its result only on success, so re-derive from the last accepted
            # state (tracked on the instance) rather than the original input.
            budget_exhausted = True
            current = getattr(self, "_accepted", current)
            current_diff = getattr(self, "_accepted_diff", current_diff)
            changed = bool(getattr(self, "_changed", False))

        # -- certification pass: remove each remaining batch individually --
        notes: list[str] = []
        undetermined: list[str] = []
        for index, batch in enumerate(current):
            candidate = current[:index] + current[index + 1 :]
            try:
                result, invalid = self.trial(candidate)
            except BudgetExhausted:
                budget_exhausted = True
                undetermined.append(
                    f"批次 {batch.batch_id}: 请求预算耗尽，无法确认删除后的行为"
                )
                continue
            if invalid is not None:
                notes.append(
                    f"批次 {batch.batch_id}: 删除会使输入非法（{invalid}），保留"
                )
            elif self._usable_difference(result, invalid):
                # Should be impossible after chunk reduction; never claim
                # minimality on this basis.
                undetermined.append(
                    f"批次 {batch.batch_id}: 删除后仍存在差异但未继续缩减"
                )
            elif result.outcome == OUTCOME_TIMEOUT:
                undetermined.append(
                    f"批次 {batch.batch_id}: 删除后执行超过 "
                    f"{config.STATEMENT_TIMEOUT_SECONDS:g} 秒，无法判定，"
                    "不能作为缩减完成依据"
                )
            elif result.outcome == "error":
                detail = result.notes[0] if result.notes else "SQL 报错"
                undetermined.append(
                    f"批次 {batch.batch_id}: 删除后 SQL 无法判定（{detail}），"
                    "不能作为缩减完成依据"
                )
            elif candidate:
                notes.append(f"批次 {batch.batch_id}: 删除后差异消失，保留")
            else:
                notes.append(
                    f"批次 {batch.batch_id}: 唯一剩余批次"
                    "（至少保留 1 批才存在可观察的输入），保留"
                )

        if budget_exhausted:
            undetermined.append("请求预算（时间或试验次数）耗尽，未完成认证")

        if undetermined:
            status = STATUS_UNDETERMINED
            notes = undetermined + notes + ["未能确认缩减完成"]
        else:
            status = STATUS_REDUCED if changed else STATUS_ALREADY_MINIMAL
            notes = [
                f"缩减完成：保留 {len(current)} 个批次；"
                "删除任一剩余批次都会使输入非法或消除差异"
            ] + notes

        return ReductionOutcome(
            status=status,
            reduced_batches=current,
            first_difference_batch_id=current_diff[0],
            difference=current_diff[1],
            notes=notes,
            evaluations=self.evaluations,
            blocked_by=undetermined,
        )

    def _undetermined_from_base(
        self, headline: str, base_result: TrialResult
    ) -> ReductionOutcome:
        return ReductionOutcome(
            status=STATUS_UNDETERMINED,
            reduced_batches=self.batches,
            first_difference_batch_id=None,
            difference=None,
            notes=[headline] + base_result.notes,
            evaluations=self.evaluations,
            blocked_by=base_result.notes or [headline],
        )

    # -- chunk reduction -------------------------------------------------
    def _accept(
        self,
        current: list[Batch],
        current_diff: tuple[int, tuple[list[list], list[list]]],
    ) -> None:
        """Remember the best accepted reduction, so budget exhaustion keeps it."""
        self._accepted = current
        self._accepted_diff = current_diff
        self._changed = True

    def _reduce(
        self,
        current: list[Batch],
        current_diff: tuple[int, tuple[list[list], list[list]]],
    ) -> tuple[list[Batch], tuple[int, tuple[list[list], list[list]]], bool]:
        """Chunked removal, then single-batch greedy cleanup."""
        self._accepted = current
        self._accepted_diff = current_diff
        self._changed = False
        changed = False

        # ddmin-like: try removing chunks of decreasing size.  Accepted chunks
        # reduce the input and the same chunk size is retried.
        size = max(1, len(current) // 2)
        while size >= 1 and len(current) > 1:
            index = 0
            progressed = False
            while index + size <= len(current):
                candidate = current[:index] + current[index + size :]
                if candidate:
                    result, invalid = self.trial(candidate)
                    if self._usable_difference(result, invalid):
                        current = candidate
                        current_diff = _diff_tuple(result)
                        changed = True
                        progressed = True
                        self._accept(current, current_diff)
                        continue
                index += 1
            if progressed:
                continue
            if size == 1:
                break
            size = max(1, size // 2)

        # Guarantee 1-minimality for the *returned* order level: a single
        # batch elsewhere in the list may have become removable after later
        # removals, so sweep until a full pass makes no change.
        while True:
            removed = False
            for index in range(len(current)):
                candidate = current[:index] + current[index + 1 :]
                if not candidate:
                    continue
                result, invalid = self.trial(candidate)
                if self._usable_difference(result, invalid):
                    current = candidate
                    current_diff = _diff_tuple(result)
                    changed = True
                    removed = True
                    self._accept(current, current_diff)
                    break
            if not removed:
                break
        return current, current_diff, changed


def _diff_tuple(
    result: TrialResult,
) -> tuple[int, tuple[list[list], list[list]]]:
    diff = result.first_difference
    assert diff is not None
    return diff.batch_id, (diff.missing, diff.extra)
