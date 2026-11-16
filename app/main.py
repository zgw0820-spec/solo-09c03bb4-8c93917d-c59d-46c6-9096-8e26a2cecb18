"""FastAPI application: incremental-net SQL minimizer.

Every request is handled synchronously and uses its own in-memory SQLite
databases created by the :class:`~app.executor.Executor`.  No global mutable
state, no temp files, no network access: a trial cannot affect another request
(and FastAPI runs sync endpoints in a worker thread pool, so two requests can
run concurrently without sharing a connection).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException

from . import __version__, config
from .executor import Executor
from .models import (
    Batch,
    DifferenceDetail,
    MinimizeRequest,
    MinimizeResponse,
    ValidateRequest,
    ValidateResponse,
)
from .reducer import Reducer
from .sqlguard import SqlPolicyError
from .validate import InputError, validate_all

app = FastAPI(
    title="增量净额 SQL 反例精简器",
    version=__version__,
    description=(
        "给出 orders/refunds 初始数据（合计至多 100 行）、至多 20 批增删改变更、"
        "全量 SELECT 与增量 SQL，服务在隔离的内存 SQLite 中从全量初始成品出发逐批重放，"
        "每批事务提交后按**多重集**比较全量结果与增量 result 表（重复行计入），"
        "首次发现差异后自动删减批次，返回精简输入、首次差异的原批号以及缺失/多余行。\n\n"
        "**安全与判定边界**\n"
        "- 全量 SQL 只读，只能访问 orders/refunds；增量 SQL 只能写 result。"
        "两条 SQL 都由 SQLite authorizer 强制约束。\n"
        "- 每个候选输入都在全新内存库中从相同初始状态重建，试验之间、请求之间互不影响。\n"
        "- 任一语句报错、被策略拒绝、结果超过行数上限或单次执行超过 "
        f"{config.STATEMENT_TIMEOUT_SECONDS:g} 秒，即标记为**无法判定**，"
        "不能作为缩减完成的依据。\n"
        "- 只有删除任一剩余批次都会使输入非法或消除差异时，才返回 "
        "`reduced`/`already_minimal`。\n\n"
        "**增量 SQL 可用的表**\n"
        "`orders(order_id, amount)`、`refunds(refund_id, order_id, amount)`、"
        "`batch_changes(...)`（本批前后值）、`result(order_id, net_amount)`。"
        "`batch_changes` 的列定义见 README。"
    ),
    contact={"name": "结算数仓工具"},
)


@app.get("/health", summary="健康检查", tags=["运维"])
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": __version__,
        "statement_timeout_seconds": config.STATEMENT_TIMEOUT_SECONDS,
        "max_result_rows": config.MAX_RESULT_ROWS,
        "max_trials": config.MAX_TRIALS,
        "max_request_seconds": config.MAX_REQUEST_SECONDS,
    }


def _prepare(request: MinimizeRequest | ValidateRequest) -> Executor:
    try:
        validate_all(request.initial, request.batches)
    except InputError as exc:
        raise HTTPException(status_code=422, detail=f"输入非法: {exc}") from exc
    try:
        return Executor(request.sql.full_select, request.sql.incremental)
    except SqlPolicyError as exc:
        raise HTTPException(status_code=422, detail=f"SQL 不符合策略: {exc}") from exc


def _dump_batches(batches: list[Batch]) -> list[dict[str, Any]]:
    # ``exclude_none`` drops ``before`` from inserts and ``after`` from
    # deletes; the reduced input is still a valid request payload.
    return [b.model_dump(mode="json", exclude_none=True) for b in batches]


@app.post(
    "/v1/minimize",
    response_model=MinimizeResponse,
    summary="精简反例并返回首次差异",
    tags=["精简"],
    responses={
        422: {"description": "输入非法或 SQL 违反安全策略"},
    },
)
def minimize(request: MinimizeRequest) -> MinimizeResponse:
    executor = _prepare(request)
    try:
        outcome = Reducer(request.initial, request.batches, executor).run()
    except InputError as exc:  # pragma: no cover - validated above
        raise HTTPException(status_code=422, detail=f"输入非法: {exc}") from exc
    except SqlPolicyError as exc:
        raise HTTPException(status_code=422, detail=f"SQL 不符合策略: {exc}") from exc

    difference = None
    if outcome.difference is not None:
        missing, extra = outcome.difference
        difference = DifferenceDetail(missing=missing, extra=extra)

    reduced = {
        "initial": request.initial.model_dump(mode="json"),
        "batches": _dump_batches(outcome.reduced_batches),
        "sql": request.sql.model_dump(mode="json"),
    }
    return MinimizeResponse(
        status=outcome.status,  # type: ignore[arg-type]
        reduced_input=reduced,
        first_difference_batch_id=outcome.first_difference_batch_id,
        difference=difference,
        notes=outcome.notes,
        blocked_by=outcome.blocked_by,
        evaluations=outcome.evaluations,
    )


@app.post(
    "/v1/validate",
    response_model=ValidateResponse,
    summary="完整输入一次重放（不缩减）",
    tags=["校验"],
)
def validate(request: ValidateRequest) -> ValidateResponse:
    executor = _prepare(request)
    result = executor.run(request.initial, request.batches)
    difference = None
    batch_id = None
    if result.first_difference is not None:
        batch_id = result.first_difference.batch_id
        difference = DifferenceDetail(
            missing=result.first_difference.missing,
            extra=result.first_difference.extra,
        )
    return ValidateResponse(
        outcome=result.outcome,  # type: ignore[arg-type]
        first_difference_batch_id=batch_id,
        difference=difference,
        notes=result.notes,
    )
