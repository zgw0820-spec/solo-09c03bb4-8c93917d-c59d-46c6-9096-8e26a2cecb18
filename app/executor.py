"""Deterministic, isolated execution of the submitted SQL.

One :meth:`Executor.run` is one *trial*: a brand-new in-memory SQLite database
built from the canonical initial state.  Nothing is shared between trials, so
an erroring, slow or malicious trial can never affect another trial or another
request.

Trial protocol (mirrors the task statement):

1. Create ``orders``/``refunds`` and load the initial rows.
2. Run ``full_select`` on the untouched initial rows -> the reference
   multiset.  The full SQL must return exactly ``(order_id, net_amount)``.
3. Create ``result`` and load the reference rows into it: the incremental SQL
   starts from the finished full-product table ("全量初始成品").
4. For each retained batch, in the original order:
   a. legality of the batch in our in-memory source state
      (missing update/delete target, duplicate PK, broken FK);
   b. one transaction: apply the source-row edits, refresh
      ``batch_changes`` with this batch's before/after images, run the
      incremental SQL, COMMIT.  Foreign-key checks are deferred to COMMIT so
      a batch may move a primary key and its referencing rows together; the
      *final* state must still be legal;
   c. re-run ``full_select`` read-only on the post-batch source rows and
      compare its multiset with ``result`` (duplicate rows counted).
5. The first batch whose multisets differ is the *first difference*.

A statement that raises, is rejected by the authorizer, returns more than
``MAX_RESULT_ROWS`` rows, or exceeds ``STATEMENT_TIMEOUT_SECONDS`` is
*undeterminable*: the trial reports ``error`` / ``timeout`` and is never used
as evidence in the reduction.
"""

from __future__ import annotations

import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from . import config, sqlguard
from .models import Batch, InitialData
from .schema import (
    CDC_DDL,
    RESULT_DDL,
    SOURCE_DDL,
    canonical_row,
    rows_to_json,
    set_batch_changes,
    set_initial_source_rows,
)
from .validate import (
    CdcRecord,
    SourceState,
    apply_batch_recording,
    build_initial_state,
)

OUTCOME_OK = "ok"
OUTCOME_DIFFERENT = "different"
OUTCOME_ERROR = "error"
OUTCOME_TIMEOUT = "timeout"

# Progress handler resolution in VM instructions.  ~0.1 ms of ordinary SQL,
# which keeps the 1 s deadline accurate without measurable overhead.
PROGRESS_INSTRUCTION_STEP = 1000


class TrialError(Exception):
    def __init__(self, message: str, *, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class Difference:
    batch_id: int
    missing: list[list[Any]] = field(default_factory=list)
    extra: list[list[Any]] = field(default_factory=list)


@dataclass
class TrialResult:
    outcome: str
    first_difference: Difference | None = None
    notes: list[str] = field(default_factory=list)


class Executor:
    """Runs trials for one request.  Stateless apart from configuration."""

    def __init__(self, full_sql: str, incremental_sql: str) -> None:
        if len(full_sql) > config.MAX_SQL_CHARS or (
            len(incremental_sql) > config.MAX_SQL_CHARS
        ):
            raise sqlguard.SqlPolicyError(
                f"SQL 长度不得超过 {config.MAX_SQL_CHARS} 字符"
            )
        sqlguard.check_full(full_sql)
        sqlguard.check_incremental(incremental_sql)
        self.full_sql = full_sql
        self.incremental_statements = sqlguard.split_statements(incremental_sql)
        self.statement_timeout = config.STATEMENT_TIMEOUT_SECONDS
        self._deadline: float | None = None
        self._trial_deadline: float | None = None

    # -- time budget ----------------------------------------------------
    def _progress(self) -> int:
        if self._deadline is not None and time.monotonic() > self._deadline:
            return 1
        return 0

    # -- connection -----------------------------------------------------
    @staticmethod
    def _connect() -> sqlite3.Connection:
        conn = sqlite3.connect(
            ":memory:", isolation_level=None, check_same_thread=True
        )
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA max_page_count = {int(config.MAX_DB_PAGES)}")
        conn.execute("PRAGMA temp_store = MEMORY")
        try:
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, config.MAX_VALUE_BYTES)
        except (AttributeError, sqlite3.Error):  # pragma: no cover - old sqlite
            pass
        return conn

    # -- one statement with per-statement + per-trial budget ------------
    def _execute(
        self, conn: sqlite3.Connection, sql: str, mode: str
    ) -> list[tuple[Any, ...]]:
        timeout = self.statement_timeout
        now = time.monotonic()
        statement_deadline = now + timeout
        if self._trial_deadline is not None:
            statement_deadline = min(statement_deadline, self._trial_deadline)
        self._deadline = statement_deadline
        conn.set_progress_handler(self._progress, PROGRESS_INSTRUCTION_STEP)
        try:
            with sqlguard.AuthorizerContext(conn, mode):
                cursor = conn.execute(sql)
                return self._fetch_capped(cursor)
        except sqlguard.SqlPolicyError:
            raise
        except TrialError:
            raise
        except sqlite3.Error as exc:
            text = str(exc)
            lowered = text.lower()
            if "interrupted" in lowered:
                raise TrialError(
                    f"语句执行超过 {timeout:g} 秒，无法判定", kind="timeout"
                ) from exc
            if "not authorized" in lowered:
                raise TrialError(f"SQL 被安全策略拒绝: {text}", kind="error") from exc
            if "too many" in lowered or "too big" in lowered or "out of memory" in lowered:
                raise TrialError(f"SQL 资源超限，无法判定: {text}", kind="limit") from exc
            raise TrialError(f"SQL 执行失败: {text}", kind="error") from exc
        finally:
            self._deadline = None

    @staticmethod
    def _fetch_capped(cursor: sqlite3.Cursor) -> list[tuple[Any, ...]]:
        """Fetch with a hard row cap.  Exceeding it is undeterminable."""
        if cursor.description is None:
            return []
        rows = list(cursor.fetchmany(config.MAX_RESULT_ROWS + 1))
        if len(rows) > config.MAX_RESULT_ROWS:
            raise TrialError(
                f"结果超过 {config.MAX_RESULT_ROWS} 行上限，无法判定",
                kind="limit",
            )
        return rows

    def _full_rows(self, conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
        rows = self._execute(conn, self.full_sql, sqlguard.FULL)
        for row in rows:
            if len(row) != 2:
                raise TrialError(
                    "full_select 必须返回 2 列 (order_id, net_amount)，"
                    f"实际返回 {len(row)} 列",
                    kind="error",
                )
        return rows

    @staticmethod
    def _read_result(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
        """Read ``result`` for comparison (our own SQL, not submitted SQL)."""
        cursor = conn.execute("SELECT order_id, net_amount FROM result")
        rows = list(cursor.fetchmany(config.MAX_RESULT_ROWS + 1))
        if len(rows) > config.MAX_RESULT_ROWS:
            raise TrialError(
                f"result 超过 {config.MAX_RESULT_ROWS} 行，无法判定", kind="limit"
            )
        return rows

    # -- source-row edits ----------------------------------------------
    @staticmethod
    def _apply_source_changes(
        conn: sqlite3.Connection, records: Sequence[CdcRecord]
    ) -> None:
        for record in records:
            if record.table == "orders":
                if record.op == "insert":
                    assert record.new is not None
                    conn.execute(
                        "INSERT INTO orders(order_id, amount) VALUES (?, ?)",
                        (record.new["order_id"], record.new["amount"]),
                    )
                elif record.op == "delete":
                    assert record.old is not None
                    conn.execute(
                        "DELETE FROM orders WHERE order_id = ?",
                        (record.old["order_id"],),
                    )
                else:
                    assert record.old is not None and record.new is not None
                    conn.execute(
                        "UPDATE orders SET order_id = ?, amount = ? WHERE order_id = ?",
                        (
                            record.new["order_id"],
                            record.new["amount"],
                            record.old["order_id"],
                        ),
                    )
            else:
                if record.op == "insert":
                    assert record.new is not None
                    conn.execute(
                        "INSERT INTO refunds(refund_id, order_id, amount) "
                        "VALUES (?, ?, ?)",
                        (
                            record.new["refund_id"],
                            record.new["order_id"],
                            record.new["amount"],
                        ),
                    )
                elif record.op == "delete":
                    assert record.old is not None
                    conn.execute(
                        "DELETE FROM refunds WHERE refund_id = ?",
                        (record.old["refund_id"],),
                    )
                else:
                    assert record.old is not None and record.new is not None
                    conn.execute(
                        "UPDATE refunds SET refund_id = ?, order_id = ?, amount = ? "
                        "WHERE refund_id = ?",
                        (
                            record.new["refund_id"],
                            record.new["order_id"],
                            record.new["amount"],
                            record.old["refund_id"],
                        ),
                    )

    @staticmethod
    def _check_result_width(rows: Iterable[Sequence[Any]]) -> None:
        for row in rows:
            if len(row) != 2:
                raise TrialError(
                    f"结果行必须是 2 列 (order_id, net_amount)，实际 {len(row)} 列",
                    kind="error",
                )

    # -- a single complete trial ---------------------------------------
    def run(
        self,
        initial: InitialData,
        batches: list[Batch],
        *,
        deadline: float | None = None,
    ) -> TrialResult:
        try:
            state = build_initial_state(initial)
        except Exception as exc:  # initial data is validated at the API edge
            return TrialResult(OUTCOME_ERROR, notes=[f"初始数据非法: {exc}"])

        self._trial_deadline = deadline
        conn = self._connect()
        try:
            for ddl in SOURCE_DDL:
                conn.execute(ddl)
            conn.execute(RESULT_DDL)
            conn.execute(CDC_DDL)
            snapshot = state.snapshot()
            set_initial_source_rows(conn, snapshot["orders"], snapshot["refunds"])

            # Reference full result for the initial state.
            initial_rows = self._full_rows(conn)
            self._check_result_width(initial_rows)
            conn.executemany(
                "INSERT INTO result(order_id, net_amount) VALUES (?, ?)",
                [tuple(row) for row in initial_rows],
            )

            for batch in batches:
                try:
                    state, records = apply_batch_recording(
                        state, batch, where=f"批次 {batch.batch_id}"
                    )
                except Exception as exc:
                    return TrialResult(
                        OUTCOME_ERROR,
                        notes=[f"批次 {batch.batch_id} 无法应用: {exc}"],
                    )

                # --- begin batch transaction ---------------------------
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute("PRAGMA defer_foreign_keys = ON")
                    self._apply_source_changes(conn, records)
                    set_batch_changes(conn, records)
                    for statement in self.incremental_statements:
                        self._execute(conn, statement, sqlguard.INCREMENTAL)
                    conn.execute("COMMIT")
                except Exception as exc:
                    if conn.in_transaction:
                        try:
                            conn.execute("ROLLBACK")
                        except sqlite3.Error:
                            pass
                    if isinstance(exc, sqlguard.SqlPolicyError):
                        return TrialResult(
                            OUTCOME_ERROR,
                            notes=[f"批次 {batch.batch_id}: {exc}"],
                        )
                    if isinstance(exc, TrialError):
                        return TrialResult(
                            OUTCOME_TIMEOUT
                            if exc.kind == "timeout"
                            else OUTCOME_ERROR,
                            notes=[f"批次 {batch.batch_id}: {exc}"],
                        )
                    if isinstance(exc, sqlite3.Error):
                        return TrialResult(
                            OUTCOME_ERROR,
                            notes=[
                                f"批次 {batch.batch_id}: 源表变更与数据库约束冲突: {exc}"
                            ],
                        )
                    raise
                # --- end batch transaction -----------------------------

                full_rows = self._full_rows(conn)
                result_rows = self._read_result(conn)
                self._check_result_width(result_rows)
                missing, extra = _multiset_diff(full_rows, result_rows)
                if missing or extra:
                    return TrialResult(
                        OUTCOME_DIFFERENT,
                        first_difference=Difference(
                            batch_id=batch.batch_id, missing=missing, extra=extra
                        ),
                        notes=[
                            f"批次 {batch.batch_id} 后 result 与全量不一致"
                            f"（缺 {len(missing)} 行、多 {len(extra)} 行）"
                        ],
                    )
            return TrialResult(OUTCOME_OK)
        except sqlguard.SqlPolicyError as exc:
            return TrialResult(OUTCOME_ERROR, notes=[str(exc)])
        except TrialError as exc:
            return TrialResult(
                OUTCOME_TIMEOUT if exc.kind == "timeout" else OUTCOME_ERROR,
                notes=[str(exc)],
            )
        except sqlite3.Error as exc:
            return TrialResult(OUTCOME_ERROR, notes=[f"SQL 执行失败: {exc}"])
        finally:
            self._trial_deadline = None
            conn.close()


def _multiset_diff(
    full_rows: Iterable[Sequence[Any]], inc_rows: Iterable[Sequence[Any]]
) -> tuple[list[list[Any]], list[list[Any]]]:
    full_counter = Counter(canonical_row(r) for r in full_rows)
    inc_counter = Counter(canonical_row(r) for r in inc_rows)
    missing_counter = full_counter - inc_counter
    extra_counter = inc_counter - full_counter

    def ordered(counter: Counter) -> list[list[Any]]:
        items: list[tuple[Any, ...]] = []
        for row, count in counter.items():
            items.extend([row] * count)
        items.sort(key=lambda r: tuple((type(v).__name__, repr(v)) for v in r))
        return rows_to_json(items)

    return ordered(missing_counter), ordered(extra_counter)
