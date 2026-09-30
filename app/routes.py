"""批次追溯 HTTP 路由。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request, Response, status
from sqlalchemy.orm import Session

from . import service
from .deps import SessionDep
from .errors import AppError
from .models import Batch, Production
from .schemas import (
    BatchOut,
    DescendantsOut,
    LineageOut,
    ProductionCancellationIn,
    ProductionCancellationResult,
    ProductionDetailOut,
    ProductionIn,
    ProductionOut,
    RawBatchIn,
    ReservationCancelIn,
    ReservationIn,
    ReservationListOut,
    ReservationOut,
    ReviewIn,
    ReviewOut,
    SuspectIn,
)

router = APIRouter()


def _batch_out(
    batch: Batch,
    session: Session,
    cancelled: bool = False,
    availability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if availability is None:
        availability = service.batch_availability(session, batch)
    return {
        "batch_no": batch.batch_no,
        "unit": batch.unit,
        "initial_qty": float(batch.initial_qty),
        "remaining_qty": float(batch.remaining_qty),
        "available_qty": float(availability["available_qty"]),
        "reserved_qty": float(availability["reserved_qty"]),
        "is_suspect": batch.is_suspect,
        "is_quarantined": batch.is_quarantined,
        "is_cancelled": cancelled,
        "note": batch.note,
    }


def _production_out(prod: Production, unit: str) -> dict[str, Any]:
    return {
        "output_batch_no": prod.output_no,
        "unit": unit,
        "output_quantity": float(prod.output_qty),
        "inputs": [
            {"batch_no": pi.source_no, "quantity": float(pi.qty)}
            for pi in sorted(prod.inputs, key=lambda x: x.id)
        ],
    }


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/raw-batches", response_model=BatchOut, status_code=status.HTTP_201_CREATED)
def create_raw_batch(
    body: RawBatchIn,
    session: SessionDep,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    if not idempotency_key:
        raise AppError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "写操作必须携带 Idempotency-Key 请求头以支持安全重试",
            400,
        )

    payload = body.model_dump(mode="json")

    def action() -> tuple[int, dict[str, Any]]:
        batch = service.register_raw_batch(session, body)
        return status.HTTP_201_CREATED, _batch_out(batch, session)

    code, result = service.idempotent(
        session,
        method=request.method,
        path=request.url.path,
        key=idempotency_key,
        payload=payload,
        action=action,
    )
    response.status_code = code
    return result


@router.post("/productions", response_model=ProductionOut, status_code=status.HTTP_201_CREATED)
def create_production(
    body: ProductionIn,
    session: SessionDep,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    if not idempotency_key:
        raise AppError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "写操作必须携带 Idempotency-Key 请求头以支持安全重试",
            400,
        )

    payload = body.model_dump(mode="json")

    def action() -> tuple[int, dict[str, Any]]:
        prod = service.submit_production(session, body)
        return status.HTTP_201_CREATED, _production_out(prod, body.unit)

    code, result = service.idempotent(
        session,
        method=request.method,
        path=request.url.path,
        key=idempotency_key,
        payload=payload,
        action=action,
    )
    response.status_code = code
    return result


@router.get("/productions/{output_batch_no}", response_model=ProductionDetailOut)
def get_production(output_batch_no: str, session: SessionDep) -> dict[str, Any]:
    """查询生产单：原单据与投料明细，以及撤销状态/撤销原因（若已撤销）。"""
    return service.get_production_detail(session, output_batch_no)


@router.get(
    "/productions/{output_batch_no}/cancellation",
    response_model=ProductionCancellationResult,
)
def get_production_cancellation(
    output_batch_no: str, session: SessionDep
) -> dict[str, Any]:
    """查询撤销结果：产出编号、返还明细、撤销原因与时间。未撤销报 404。"""
    return service.get_cancellation_detail(session, output_batch_no)


@router.post(
    "/productions/{output_batch_no}/cancellation",
    response_model=ProductionCancellationResult,
    status_code=status.HTTP_200_OK,
)
def cancel_production(
    output_batch_no: str,
    body: ProductionCancellationIn,
    session: SessionDep,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """撤销录入错误的生产单：返还各来源原投料量、作废产出、保留单据可查。"""
    if not idempotency_key:
        raise AppError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "写操作必须携带 Idempotency-Key 请求头以支持安全重试",
            400,
        )

    payload = body.model_dump(mode="json")

    def action() -> tuple[int, dict[str, Any]]:
        return status.HTTP_200_OK, service.cancel_production(session, output_batch_no, body)

    code, result = service.idempotent(
        session,
        method=request.method,
        path=request.url.path,
        key=idempotency_key,
        payload=payload,
        action=action,
    )
    response.status_code = code
    return result


@router.get("/batches/{batch_no}", response_model=BatchOut)
def get_batch(batch_no: str, session: SessionDep) -> dict[str, Any]:
    batch = session.get(Batch, batch_no)
    if batch is None:
        from .errors import batch_not_found

        raise batch_not_found(batch_no)
    cancelled = batch_no in service.cancelled_output_nos(session, [batch_no])
    return _batch_out(batch, session, cancelled)


@router.get(
    "/batches/{batch_no}/reservations",
    response_model=ReservationListOut,
)
def list_batch_reservations(batch_no: str, session: SessionDep) -> dict[str, Any]:
    """查询某来源批次的全部预留（各状态）与当前实存/预留/可用量。"""
    return service.list_reservations(session, batch_no)


@router.get("/reservations/{reservation_no}", response_model=ReservationOut)
def get_reservation(reservation_no: str, session: SessionDep) -> dict[str, Any]:
    """查询单笔预留状态及其来源批次当前可用量。"""
    return service.get_reservation(session, reservation_no)


@router.post(
    "/reservations",
    response_model=ReservationOut,
    status_code=status.HTTP_201_CREATED,
)
def create_reservation(
    body: ReservationIn,
    session: SessionDep,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """按预留单号、计量单位、来源批次、正数数量和截止时间预留原料。"""
    if not idempotency_key:
        raise AppError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "写操作必须携带 Idempotency-Key 请求头以支持安全重试",
            400,
        )

    payload = body.model_dump(mode="json")

    def action() -> tuple[int, dict[str, Any]]:
        return status.HTTP_201_CREATED, service.create_reservation(session, body)

    code, result = service.idempotent(
        session,
        method=request.method,
        path=request.url.path,
        key=idempotency_key,
        payload=payload,
        action=action,
    )
    response.status_code = code
    return result


@router.post(
    "/reservations/{reservation_no}/cancellation",
    response_model=ReservationOut,
    status_code=status.HTTP_200_OK,
)
def cancel_reservation(
    reservation_no: str,
    session: SessionDep,
    request: Request,
    response: Response,
    body: ReservationCancelIn | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """主动取消预留并释放额度；过期/已取消/已核销整笔拒绝。"""
    if not idempotency_key:
        raise AppError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "写操作必须携带 Idempotency-Key 请求头以支持安全重试",
            400,
        )

    payload = body.model_dump(mode="json") if body is not None else {}

    def action() -> tuple[int, dict[str, Any]]:
        return status.HTTP_200_OK, service.cancel_reservation(
            session, reservation_no, body
        )

    code, result = service.idempotent(
        session,
        method=request.method,
        path=request.url.path,
        key=idempotency_key,
        payload=payload,
        action=action,
    )
    response.status_code = code
    return result


@router.get("/batches/{batch_no}/lineage", response_model=LineageOut)
def get_lineage(batch_no: str, session: SessionDep) -> dict[str, Any]:
    return service.lineage(session, batch_no)


@router.post("/contamination", response_model=DescendantsOut, status_code=status.HTTP_200_OK)
def flag_suspect(
    body: SuspectIn,
    session: SessionDep,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    # 污染标记本身是幂等动作：无键也允许，有键则走标准重放/冲突语义
    payload = body.model_dump(mode="json")

    def action() -> tuple[int, dict[str, Any]]:
        return status.HTTP_200_OK, service.mark_suspect(session, body)

    code, result = service.idempotent(
        session,
        method=request.method,
        path=request.url.path,
        key=idempotency_key,
        payload=payload,
        action=action,
    )
    response.status_code = code
    return result


@router.post("/reviews", response_model=ReviewOut, status_code=status.HTTP_200_OK)
def review_suspect(
    body: ReviewIn,
    session: SessionDep,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """质检复核：确认疑似批次为误报，撤销标记并重算隔离。必须携带幂等键。"""
    if not idempotency_key:
        raise AppError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "写操作必须携带 Idempotency-Key 请求头以支持安全重试",
            400,
        )

    payload = body.model_dump(mode="json")

    def action() -> tuple[int, dict[str, Any]]:
        return status.HTTP_200_OK, service.review_suspect(session, body)

    code, result = service.idempotent(
        session,
        method=request.method,
        path=request.url.path,
        key=idempotency_key,
        payload=payload,
        action=action,
    )
    response.status_code = code
    return result
