"""请求/响应模型。

数量统一走 ``Decimal`` 精确计算，拒绝零、负数与 NaN/Inf；响应再序列化为数字。
"""
from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone
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
    # 可选：按预留单号投料。填写后投料批次、数量、单位须与预留完全一致，
    # 且只能有一笔投料；服务端在同一事务内核销预留、扣减实存、生成产出。
    reservation_no: str | None = Field(
        default=None, min_length=1, max_length=64, examples=["RSV-20260930-01"]
    )


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


# ---------- 原料预留 ----------


def _to_utc_aware(value: object) -> datetime:
    """截止时间统一为带时区的 UTC datetime；朴素时间按 UTC 解释。"""
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise ValueError("截止时间必须是 ISO 8601 日期时间") from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


FutureDateTime = Annotated[datetime, BeforeValidator(_to_utc_aware)]


class ReservationIn(BaseModel):
    """预留原料：预留单号、来源批次、单位、正数数量与截止时间。"""

    reservation_no: str = Field(min_length=1, max_length=64, examples=["RSV-20260930-01"])
    source_batch_no: BatchNo
    unit: Unit
    quantity: PositiveQty
    expires_at: FutureDateTime


class ReservationCancelIn(BaseModel):
    """主动取消预留：原因可选（默认“操作员主动取消”）。"""

    reason: str | None = Field(
        default=None, max_length=1000, examples=["计划变更，取消本笔预留"]
    )

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("取消原因不得为纯空白")
        return value


# ---------- 响应 ----------


class _ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class BatchOut(_ORMModel):
    batch_no: str
    unit: str
    initial_qty: float
    remaining_qty: float
    # 可用量 = 实存余量 - 未取消、未核销且未到期的预留量
    available_qty: float = 0.0
    # 上述未取消、未核销且未到期的预留合计
    reserved_qty: float = 0.0
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


# ---------- 预留响应 ----------


class ReservationOut(BaseModel):
    reservation_no: str
    source_batch_no: str
    unit: str
    quantity: float
    expires_at: datetime
    # 展示状态：active（未取消/未核销/未到期）、expired（已到期未核销）、
    # cancelled（主动取消或随来源产出作废自动取消）、consumed（已被生产核销）
    status: Literal["active", "expired", "cancelled", "consumed"]
    created_at: datetime
    cancelled_at: datetime | None = None
    cancel_reason: str | None = None
    consumed_at: datetime | None = None
    consumed_by_output_no: str | None = None
    # 响应组装时刻该来源批次的额度情况
    source: "BatchAvailabilityOut"


class BatchAvailabilityOut(BaseModel):
    """来源批次的实存/预留/可用额度。"""

    batch_no: str
    unit: str
    remaining_qty: float
    reserved_qty: float
    available_qty: float
    is_quarantined: bool


class ReservationListOut(BaseModel):
    source_batch_no: str
    availability: BatchAvailabilityOut
    reservations: list[ReservationOut]


ReservationOut.model_rebuild()
