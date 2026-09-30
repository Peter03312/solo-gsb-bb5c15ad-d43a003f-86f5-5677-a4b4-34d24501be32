"""请求/响应模型。

数量统一走 ``Decimal`` 精确计算，拒绝零、负数与 NaN/Inf；响应再序列化为数字。
"""
from __future__ import annotations

from decimal import Decimal
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator


def _to_decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("数量不能为布尔值")
    try:
        dec = Decimal(str(value))
    except Exception as exc:  # invalid literal
        raise ValueError("数量必须是十进制数字") from exc
    if not dec.is_finite():
        raise ValueError("数量必须是有限数字")
    if dec <= 0:
        raise ValueError("数量必须为正数")
    return dec


# 正数数量：入参即完成 > 0 校验
PositiveQty = Annotated[Decimal, BeforeValidator(_to_decimal)]

BatchNo = Annotated[str, Field(min_length=1, max_length=64, examples=["RM-20260930-01"])]
Unit = Annotated[str, Field(min_length=1, max_length=16, examples=["kg"])]


class RawBatchIn(BaseModel):
    batch_no: BatchNo
    unit: Unit
    quantity: PositiveQty
    note: str | None = Field(default=None, max_length=255)


class ConsumptionIn(BaseModel):
    batch_no: BatchNo
    quantity: PositiveQty


class ProductionIn(BaseModel):
    output_batch_no: BatchNo
    unit: Unit
    output_quantity: PositiveQty
    inputs: list[ConsumptionIn] = Field(min_length=1)


class SuspectIn(BaseModel):
    batch_no: BatchNo
    note: str | None = Field(default=None, max_length=255)


class ReviewIn(BaseModel):
    batch_no: BatchNo
    review_note: str = Field(min_length=1, max_length=1000, examples=["复检合格：留样复测未检出农残"])

    @field_validator("review_note")
    @classmethod
    def _note_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("复核说明不得为空白")
        return value


class ProductionCancellationIn(BaseModel):
    """撤销生产单：必须填写非空白撤销原因。"""

    cancel_reason: str = Field(
        min_length=1, max_length=1000, examples=["配料录入错误，产出数量多录 20kg"]
    )

    @field_validator("cancel_reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("撤销原因不得为空白")
        return value


# ---------- 响应 ----------


class _ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class BatchOut(_ORMModel):
    batch_no: str
    unit: str
    initial_qty: float
    remaining_qty: float
    is_suspect: bool
    is_quarantined: bool
    # 是否为已撤销生产单的产出（撤销后保留编号但不得投料；原料批次恒为 false）
    is_cancelled: bool = False
    note: str | None = None


class ConsumptionOut(_ORMModel):
    batch_no: str
    quantity: float


class ProductionOut(_ORMModel):
    output_batch_no: str
    unit: str
    output_quantity: float
    inputs: list[ConsumptionOut]


class NodeOut(BaseModel):
    batch_no: str
    unit: str
    remaining_qty: float
    is_suspect: bool
    is_quarantined: bool
    depth: int


class LineageOut(BaseModel):
    batch_no: str
    upstream: list[NodeOut]
    downstream: list[NodeOut]
    edges: list[dict[str, str]]


class DescendantsOut(BaseModel):
    source: str
    is_suspect: bool
    affected_batch_nos: list[str]
    newly_quarantined: list[str]


class ReviewOut(BaseModel):
    reviewed_batch_no: str
    is_suspect: bool
    review_note: str
    affected_batch_nos: list[str]
    released_batch_nos: list[str]
    still_quarantined: list[str]


# ---------- 生产单查询 / 撤销 ----------


class ReturnLineOut(BaseModel):
    """撤销时返还给某来源批次的一笔原投料量。"""

    batch_no: str
    returned_quantity: float
    remaining_qty: float


class CancellationOut(BaseModel):
    """撤销记录：随生产单详情返回，也用于撤销结果与撤销记录查询。"""

    cancel_reason: str
    cancelled_at: datetime


class ProductionDetailOut(BaseModel):
    output_batch_no: str
    unit: str
    output_quantity: float
    inputs: list[ConsumptionOut]
    status: Literal["active", "cancelled"]
    cancellation: CancellationOut | None = None


class ProductionCancellationResult(BaseModel):
    """POST 撤销结果：产出编号、返还明细、撤销状态。"""

    output_batch_no: str
    status: Literal["cancelled"]
    returns: list[ReturnLineOut]
    cancellation: CancellationOut
