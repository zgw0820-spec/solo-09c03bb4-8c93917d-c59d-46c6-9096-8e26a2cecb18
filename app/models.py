"""Pydantic request/response models for the minimizer API.

The execution contract is documented here so it appears in Swagger:

* ``orders(order_id INTEGER, amount INTEGER)``
* ``refunds(refund_id INTEGER, order_id INTEGER REFERENCES orders, amount
  INTEGER)``
* ``result(order_id INTEGER, net_amount INTEGER)`` — maintained by the
  incremental SQL; ``full_select`` must return exactly these two columns.
* ``batch_changes`` is visible *only* to the incremental SQL and contains the
  before/after images of the current batch (see :mod:`app.schema`).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


class OrderRow(BaseModel):
    order_id: int = Field(..., ge=1, description="订单号（正整数）")
    amount: int = Field(
        ..., ge=INT64_MIN, le=INT64_MAX, description="订单金额，整数分（64 位整数）"
    )


class RefundRow(BaseModel):
    refund_id: int = Field(..., ge=1, description="退款号（正整数）")
    order_id: int = Field(..., ge=1, description="关联订单号")
    amount: int = Field(
        ..., ge=INT64_MIN, le=INT64_MAX, description="退款金额，整数分（64 位整数）"
    )


class InitialData(BaseModel):
    orders: list[OrderRow] = Field(default_factory=list)
    refunds: list[RefundRow] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_limits(self) -> "InitialData":
        if len(self.orders) + len(self.refunds) > 100:
            raise ValueError("orders + refunds 合计至多 100 行")
        return self


class ChangeOp(BaseModel):
    """One row change inside one batch.

    ``insert``  : ``after`` is the complete new row; the key must not exist.
    ``update``  : ``before`` only needs the primary key (it identifies the
                  existing row); ``after`` is the complete replacement row.
                  Primary keys may change.
    ``delete``  : ``before`` only needs the primary key.

    Every update/delete target must exist at that point in the replay.
    Foreign keys must be valid after each batch (a batch may move a key and
    its referencing rows together).
    """

    op: Literal["insert", "update", "delete"]
    table: Literal["orders", "refunds"]
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> "ChangeOp":
        if self.op == "insert":
            if self.after is None:
                raise ValueError("insert 必须提供 after")
            if self.before is not None:
                raise ValueError("insert 不应提供 before")
        if self.op in ("update", "delete") and self.before is None:
            raise ValueError(f"{self.op} 必须提供 before（至少包含主键）")
        if self.op == "update" and self.after is None:
            raise ValueError("update 必须提供 after")
        return self


class Batch(BaseModel):
    batch_id: int = Field(..., ge=1, description="原批号，输入内唯一；缩减后仍用原批号")
    changes: list[ChangeOp] = Field(..., min_length=1, max_length=200)


class SqlBundle(BaseModel):
    full_select: str = Field(
        ...,
        min_length=1,
        description=(
            "全量 SQL，必须是单条 SELECT/WITH，返回 2 列 "
            "(order_id, net_amount)，类型均为整数。只能读取 orders/refunds。"
        ),
    )
    incremental: str = Field(
        ...,
        min_length=1,
        description=(
            "增量 SQL，每批在一个事务中执行。只可读取 orders/refunds/"
            "batch_changes/result，只可写 result（INSERT/UPDATE/DELETE/REPLACE）。"
            "batch_changes 提供本批变更前后值。"
        ),
    )


class MinimizeRequest(BaseModel):
    initial: InitialData
    batches: list[Batch] = Field(..., min_length=1, max_length=20)
    sql: SqlBundle

    @model_validator(mode="after")
    def _unique_batches(self) -> "MinimizeRequest":
        ids = [b.batch_id for b in self.batches]
        if len(ids) != len(set(ids)):
            raise ValueError("batch_id 必须唯一")
        return self


class DifferenceDetail(BaseModel):
    missing: list[list[Any]] = Field(
        default_factory=list,
        description="增量结果缺失的行（全量结果有、result 没有），重复行重复列出",
    )
    extra: list[list[Any]] = Field(
        default_factory=list,
        description="增量结果多出的行（result 有、全量结果没有），重复行重复列出",
    )


Status = Literal["reduced", "already_minimal", "no_difference", "undetermined"]


class MinimizeResponse(BaseModel):
    status: Status = Field(
        ...,
        description=(
            "reduced=已缩减且认证完成；already_minimal=原始输入即已最小；"
            "no_difference=完整输入无差异；undetermined=存在报错/超时或预算耗尽，"
            "不能作为缩减完成结论"
        ),
    )
    reduced_input: dict[str, Any] = Field(
        ..., description="精简后的输入：initial / batches（原批号与原顺序）/ sql"
    )
    first_difference_batch_id: int | None = Field(
        None, description="精简输入中首次出现差异的原始批号；无差异时为 null"
    )
    difference: DifferenceDetail | None = Field(
        None, description="首次差异的缺行/多行（按多重集）"
    )
    notes: list[str] = Field(default_factory=list, description="判定依据说明")
    blocked_by: list[str] = Field(
        default_factory=list,
        description="阻止“缩减完成”结论的原因（SQL 报错/超时/预算耗尽）；为空表示认证完成",
    )
    evaluations: int = Field(..., description="本次请求执行的候选输入评估次数")


class ValidateRequest(BaseModel):
    initial: InitialData
    batches: list[Batch] = Field(..., min_length=1, max_length=20)
    sql: SqlBundle


class ValidateResponse(BaseModel):
    outcome: Literal["ok", "different", "error", "timeout"] = Field(
        ..., description="完整输入一次重放的结果"
    )
    first_difference_batch_id: int | None = None
    difference: DifferenceDetail | None = None
    notes: list[str] = Field(default_factory=list)
