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
    # 可选：整笔生产必须恰好核销该预留（批次、数量、单位完全一致）；
    # 不填则沿用“只消耗可用量”的普通生产路径
    reservation_no: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("reservation_no")
    @classmethod
    def _reservation_no_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("预留单号不得为空白")
        return value


class ReservationIn(BaseModel):
    """车间预留原料：预留单号、来源批次、计量单位、正数数量、未来截止时间。"""

    reservation_no: BatchNo
    source_batch_no: BatchNo
    unit: Unit
    quantity: PositiveQty
    # 必须是服务端当前时间之后的时刻；接受 "2026-10-01T08:00:00Z" /
    # "+08:00" 偏移等 ISO8601，朴素时间一律按 UTC 解释
    expires_at: datetime

    @field_validator("expires_at", mode="after")
    @classmethod
    def _aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class ReservationCancelIn(BaseModel):
    """主动取消预留：必须填写非空白取消原因。"""

    cancel_reason: str = Field(
        min_length=1, max_length=1000, examples=["生产计划变更，取消该笔原料预留"]
    )

    @field_validator("cancel_reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("取消原因不得为空白")
        return value


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
    # 未取消且未到期预留占用的总量（实存不变，仅占可用额度）
    reserved_qty: float = 0.0
    # 可用量 = 实存 - 有效预留量；普通生产只能消耗可用量
    available_qty: float = 0.0
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


# ---------- 原料预留 ----------


ReservationStatus = Literal["active", "consumed", "cancelled", "expired"]


class ReservationOut(BaseModel):
    """预留单：登记/查询/取消共用同一响应形态。

    status 为查询时刻按服务端时间动态计算的对外状态：落库 active 但已过
    expires_at 时对外呈现 expired（额度已释放，不改变落库行）。
    """

    reservation_no: str
    source_batch_no: str
    unit: str
    quantity: float
    expires_at: datetime
    status: ReservationStatus
    cancel_reason: str | None = None
    cancelled_at: datetime | None = None
    consumed_by_output_no: str | None = None
    created_at: datetime
    # 查询时刻来源批次的实存/有效预留/可用量快照
    source_remaining_qty: float
    source_reserved_qty: float
    source_available_qty: float


class ReservationListItem(BaseModel):
    """预留列表项：预留状态本身，不带来源批次余量快照。"""

    reservation_no: str
    source_batch_no: str
    unit: str
    quantity: float
    expires_at: datetime
    status: ReservationStatus
    cancel_reason: str | None = None
    cancelled_at: datetime | None = None
    consumed_by_output_no: str | None = None
    created_at: datetime


class ReservationListOut(BaseModel):
    source_batch_no: str | None = None
    items: list[ReservationListItem]
