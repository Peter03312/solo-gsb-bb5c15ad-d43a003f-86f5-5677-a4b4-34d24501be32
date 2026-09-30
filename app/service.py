"""业务逻辑层。所有写操作都在单个事务内完成，校验失败整体回滚。"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from . import errors
from .models import (
    Batch,
    BatchReview,
    IdempotencyKey,
    MaterialReservation,
    Production,
    ProductionCancellation,
    ProductionInput,
)
from .schemas import (
    ProductionCancellationIn,
    ProductionIn,
    RawBatchIn,
    ReservationCancelIn,
    ReservationIn,
    ReviewIn,
    SuspectIn,
)


# ---------------------------------------------------------------- 时间支持


def utcnow() -> datetime:
    """服务端当前时间（UTC，aware）。预留到期一律以此为准。"""
    return datetime.now(timezone.utc)


def _is_sqlite(session: Session) -> bool:
    return session.get_bind().dialect.name == "sqlite"


def _store_dt(session: Session, value: datetime) -> datetime:
    """落库时间归一化：PostgreSQL 保留 aware UTC；SQLite 无原生 tz，
    统一存 naive UTC，读取时再补 tzinfo，保证两侧比较都在同一时区基准上。"""
    value = value.astimezone(timezone.utc)
    return value.replace(tzinfo=None) if _is_sqlite(session) else value


def _read_dt(session: Session, value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

# ---------------------------------------------------------------- 幂等支持


def _canonical_hash(payload: dict[str, Any]) -> str:
    """对请求体做规范序列化后取 sha256；Decimal 用原始标量文本表示。"""

    def default(obj: object) -> object:
        if isinstance(obj, Decimal):
            return f"decimal:{normalize_decimal(obj)}"
        raise TypeError(f"不可序列化的类型 {type(obj)!r}")

    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=default)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def normalize_decimal(value: Decimal) -> str:
    """1 与 1.00 视为同一内容；去掉多余尾零。"""
    return format(value.normalize(), "f")


def idempotent(
    session: Session,
    *,
    method: str,
    path: str,
    key: str | None,
    payload: dict[str, Any],
    action: Callable[[], tuple[int, Any]],
) -> tuple[int, Any]:
    """以幂等键执行动作。

    - 无 key：直接执行（查询类接口不需要）；
    - 同 key + 同内容（含重试）：返回首次固化的状态码与响应；
    - 同 key + 异内容：409 冲突；
    - 失败请求不落幂等记录，允许客户端修正后以同 key 重试。
    """
    if not key:
        status, body = action()
        return status, body

    request_hash = _canonical_hash(payload)
    stmt = select(IdempotencyKey).where(
        IdempotencyKey.method == method,
        IdempotencyKey.path == path,
        IdempotencyKey.key == key,
    )
    existing = session.execute(stmt).scalar_one_or_none()
    if existing is not None:
        if existing.request_hash != request_hash:
            raise AppError_conflict(key)
        return existing.status_code, json.loads(existing.response_body)

    status, body = action()

    record = IdempotencyKey(
        method=method,
        path=path,
        key=key,
        request_hash=request_hash,
        status_code=status,
        response_body=json.dumps(body, ensure_ascii=False, default=str),
    )
    session.add(record)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        # 并发下另一请求已抢先登记该 key
        existing = session.execute(stmt).scalar_one_or_none()
        if existing is not None:
            if existing.request_hash != request_hash:
                raise AppError_conflict(key)
            return existing.status_code, json.loads(existing.response_body)
        raise
    return status, body


def AppError_conflict(key: str) -> errors.AppError:
    return errors.AppError(
        "IDEMPOTENCY_CONFLICT",
        f"幂等键 {key!r} 已用于不同的请求内容，异内容冲突",
        409,
    )


# ---------------------------------------------------------------- 批次操作


def register_raw_batch(session: Session, data: RawBatchIn) -> Batch:
    """登记一个原料批次。编号不得复用。"""
    existing = session.get(Batch, data.batch_no)
    if existing is not None:
        raise errors.batch_exists(data.batch_no)

    batch = Batch(
        batch_no=data.batch_no,
        unit=data.unit,
        initial_qty=data.quantity,
        remaining_qty=data.quantity,
        note=data.note,
    )
    session.add(batch)
    try:
        session.flush()
    except IntegrityError:
        raise errors.batch_exists(data.batch_no) from None
    return batch


def _lock_batch(session: Session, batch_no: str) -> Batch | None:
    """读取批次并加行锁（PostgreSQL FOR UPDATE；SQLite 被写事务锁覆盖）。"""
    stmt = select(Batch).where(Batch.batch_no == batch_no)
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    return session.execute(stmt).scalar_one_or_none()


def _lock_batches_in_order(
    session: Session, batch_nos: list[str]
) -> dict[str, Batch]:
    """按编号排序后逐个锁定批次。

    全局统一的加锁顺序（编号升序）保证并发生产事务之间不会出现
    “互相持有对方等待的行锁”式死锁（PostgreSQL FOR UPDATE；
    SQLite 写事务已被 BEGIN IMMEDIATE 全局串行化，FOR UPDATE 为空操作）。
    """
    ordered = sorted(set(batch_nos))
    locked: dict[str, Batch] = {}
    for no in ordered:
        stmt = select(Batch).where(Batch.batch_no == no)
        if session.get_bind().dialect.name == "postgresql":
            stmt = stmt.with_for_update()
        batch = session.execute(stmt).scalar_one_or_none()
        if batch is not None:
            locked[no] = batch
    return locked


def _acquire_qc_lock(session: Session) -> None:
    """污染标记/复核等 QC 写事务的串行化锁。

    PostgreSQL：对 batches 取 SHARE ROW EXCLUSIVE 表锁。它与生产事务的
    ROW SHARE（SELECT ... FOR UPDATE）/ ROW EXCLUSIVE（写入）冲突，也与
    其它 QC 事务互斥——使“标记/复核”与“投料”严格串行，既杜绝隔离批次被
    投料，也避免“先锁 root、再锁后代”与生产的排序行锁之间形成死锁环。
    QC 操作低频，粗粒度表锁代价可接受。
    SQLite：BEGIN IMMEDIATE 已提供全库写串行化，无需额外处理。
    """
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("LOCK TABLE batches IN SHARE ROW EXCLUSIVE MODE"))


# ---------------------------------------------------------------- 预留额度


def _active_reserved_totals(
    session: Session, batch_nos: list[str], *, now: datetime | None = None
) -> dict[str, Decimal]:
    """各批次“仍占额度”的预留合计：未取消、未核销且未到期。

    到期按服务端当前时间（UTC）即时判定，不做后台扫描，到期即释放额度。
    """
    if not batch_nos:
        return {}
    current = _store_dt(session, now or utcnow())
    rows = session.execute(
        select(
            MaterialReservation.source_no,
            func.coalesce(func.sum(MaterialReservation.qty), 0),
        )
        .where(MaterialReservation.source_no.in_(batch_nos))
        .where(MaterialReservation.status == "active")
        .where(MaterialReservation.expires_at > current)
        .group_by(MaterialReservation.source_no)
    ).all()
    return {source: Decimal(total) for source, total in rows}


def _active_reserved_total(
    session: Session, batch_no: str, *, now: datetime | None = None
) -> Decimal:
    return _active_reserved_totals(session, [batch_no], now=now).get(batch_no, Decimal(0))


def batch_availability(
    session: Session,
    batch: Batch,
    *,
    reserved: Decimal | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """组装某批次实存/预留/可用额度；可用量绝不为负（写事务内强制约束）。"""
    if reserved is None:
        reserved = _active_reserved_total(session, batch.batch_no, now=now)
    available = batch.remaining_qty - reserved
    return {
        "batch_no": batch.batch_no,
        "unit": batch.unit,
        "remaining_qty": batch.remaining_qty,
        "reserved_qty": reserved,
        "available_qty": available,
        "is_quarantined": batch.is_quarantined,
    }


def _lock_reservation(
    session: Session, reservation_no: str
) -> MaterialReservation | None:
    stmt = select(MaterialReservation).where(
        MaterialReservation.reservation_no == reservation_no
    )
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    return session.execute(stmt).scalar_one_or_none()


def reservation_status(
    reservation: MaterialReservation, *, session: Session, now: datetime
) -> str:
    """展示/校验状态：active 记录在 expires_at 到期后即时呈现为 expired。"""
    if reservation.status == "active":
        expires = _read_dt(session, reservation.expires_at)
        if expires is not None and expires <= now:
            return "expired"
    return reservation.status


def _reservation_dict(
    session: Session,
    reservation: MaterialReservation,
    *,
    now: datetime,
    source: Batch | None = None,
    availability: dict[str, Any] | None = None,
    output_map: dict[int, str] | None = None,
) -> dict[str, Any]:
    source_batch = source or session.get(Batch, reservation.source_no)
    if availability is None:
        availability = batch_availability(session, source_batch, now=now)
    consumed_output_no: str | None = None
    if reservation.consumed_production_id is not None:
        if output_map is not None:
            consumed_output_no = output_map.get(reservation.consumed_production_id)
        if consumed_output_no is None:
            prod = session.get(Production, reservation.consumed_production_id)
            consumed_output_no = prod.output_no if prod is not None else None
    return {
        "reservation_no": reservation.reservation_no,
        "source_batch_no": reservation.source_no,
        "unit": reservation.unit,
        "quantity": reservation.qty,
        "expires_at": _read_dt(session, reservation.expires_at),
        "status": reservation_status(reservation, session=session, now=now),
        "created_at": _read_dt(session, reservation.created_at),
        "cancelled_at": _read_dt(session, reservation.cancelled_at),
        "cancel_reason": reservation.cancel_reason,
        "consumed_at": _read_dt(session, reservation.consumed_at),
        "consumed_by_output_no": consumed_output_no,
        "source": availability,
    }


def create_reservation(session: Session, data: ReservationIn) -> dict[str, Any]:
    """登记一笔原料预留。

    预留不改变实存余量；同一事务内先锁来源批次行（与生产投料同一加锁层级，
    全局按编号升序，杜绝死锁），再核对：
    1. 截止时间严格晚于服务端当前时间；
    2. 来源批次存在且未作废（隔离批次可预留，隔离只禁核销、不禁占额度）；
    3. 申报单位与来源批次一致；
    4. 实存 - 未取消/未核销/未到期预留 >= 本次预留量（可用量不足整笔拒绝）；
    5. 预留单号唯一（唯一约束兜底并发首请求竞争）。
    任一不符整笔拒绝，无部分预留。
    """
    now = utcnow()
    if data.expires_at <= now:
        raise errors.deadline_not_future(data.expires_at)

    batch = _lock_batch(session, data.source_batch_no)
    if batch is None:
        raise errors.batch_not_found(data.source_batch_no)

    if data.source_batch_no in cancelled_output_nos(session, [data.source_batch_no]):
        raise errors.batch_cancelled(data.source_batch_no)
    if batch.unit != data.unit:
        raise errors.unit_mismatch(data.source_batch_no, batch.unit, data.unit)

    reserved = _active_reserved_total(session, data.source_batch_no, now=now)
    available = batch.remaining_qty - reserved
    if data.quantity > available:
        raise errors.insufficient_available(
            data.source_batch_no, data.quantity, available
        )

    reservation = MaterialReservation(
        reservation_no=data.reservation_no,
        source_no=data.source_batch_no,
        unit=data.unit,
        qty=data.quantity,
        expires_at=_store_dt(session, data.expires_at),
        status="active",
    )
    session.add(reservation)
    try:
        session.flush()
    except IntegrityError:
        raise errors.reservation_exists(data.reservation_no) from None

    # 本笔预留已计入“仍占额度”合计
    availability = batch_availability(
        session, batch, reserved=reserved + data.quantity, now=now
    )
    return _reservation_dict(
        session, reservation, now=now, source=batch, availability=availability
    )


def get_reservation(session: Session, reservation_no: str) -> dict[str, Any]:
    reservation = session.get(MaterialReservation, reservation_no)
    if reservation is None:
        raise errors.reservation_not_found(reservation_no)
    source = session.get(Batch, reservation.source_no)
    return _reservation_dict(session, reservation, now=utcnow(), source=source)


def list_reservations(session: Session, batch_no: str) -> dict[str, Any]:
    """查询某来源批次的全部预留（含已取消/已核销/已到期）与各状态及当前可用量。"""
    batch = session.get(Batch, batch_no)
    if batch is None:
        raise errors.batch_not_found(batch_no)
    now = utcnow()
    rows = session.execute(
        select(MaterialReservation)
        .where(MaterialReservation.source_no == batch_no)
        .order_by(MaterialReservation.created_at, MaterialReservation.reservation_no)
    ).scalars().all()
    availability = batch_availability(session, batch, now=now)
    return {
        "source_batch_no": batch_no,
        "availability": availability,
        "reservations": [
            _reservation_dict(
                session,
                r,
                now=now,
                source=batch,
                availability=availability,
            )
            for r in rows
        ],
    }


def cancel_reservation(
    session: Session, reservation_no: str, data: ReservationCancelIn | None = None
) -> dict[str, Any]:
    """主动取消预留：仅“未取消、未核销且未到期”的预留可取消。

    取消即时释放额度（不再计入可用量的扣减项）。取消与预留登记、按预留投料
    共用同一加锁顺序（先锁来源批次行，再锁预留行），并发结局等价于某一串行
    顺序。过期/已取消/已核销整笔拒绝。
    """
    now = utcnow()
    reservation = session.get(MaterialReservation, reservation_no)
    if reservation is None:
        raise errors.reservation_not_found(reservation_no)

    # 先锁来源批次，再锁预留行——与 create_reservation / submit_production
    # 的加锁方向一致，避免“预留锁→批次锁”与“批次锁→预留锁”形成死锁环
    source = _lock_batch(session, reservation.source_no)
    reservation = _lock_reservation(session, reservation_no)
    assert reservation is not None

    status = reservation_status(reservation, session=session, now=now)
    if status != "active":
        raise errors.reservation_not_active(reservation_no, status)

    reason = (data.reason.strip() if data and data.reason else "操作员主动取消")
    reservation.status = "cancelled"
    reservation.cancel_reason = reason
    reservation.cancelled_at = _store_dt(session, now)
    session.flush()

    return _reservation_dict(session, reservation, now=now, source=source)


def submit_production(session: Session, data: ProductionIn) -> Production:
    """提交一次生产：校验全部通过后才扣减余量并创建产出批次。

    校验顺序（任一失败整笔拒绝、不改变余量、不占用产出编号）：
    1. 投入明细编号不重复；
    2. 每个来源批次存在、单位一致、未隔离、未作废；
    3. 若携带 reservation_no：预留存在且为 active（未取消/未核销/未到期、
       来源未隔离未作废），且投料批次、数量、单位与预留**完全一致**（仅一笔投料）；
    4. 产出编号未被占用；
    5. 普通生产（不带预留）每笔用量不超过该批次**可用量**
       （可用量 = 实存 - 未取消/未核销/未到期预留）；
    6. 产出总量不超过投入总量。

    通过后在同一事务内核销预留（active -> consumed）、扣减实存、生成产出。
    """
    source_nos = [item.batch_no for item in data.inputs]
    if len(set(source_nos)) != len(source_nos):
        raise errors.AppError(
            "DUPLICATE_INPUT",
            "同一生产单中来源批次不得重复",
            422,
        )

    now = utcnow()
    # 先（无锁）读取预留以确定其来源批次，随后把“投料来源 + 预留来源”一并
    # 按编号升序加锁，最后再锁预留行——保持全局统一的“批次锁→预留锁”顺序。
    reservation: MaterialReservation | None = None
    if data.reservation_no is not None:
        reservation = session.get(MaterialReservation, data.reservation_no)
        if reservation is None:
            raise errors.reservation_not_found(data.reservation_no)
        lock_nos = sorted(set(source_nos) | {reservation.source_no})
    else:
        lock_nos = source_nos

    sources: dict[str, Batch] = {}
    total_input = Decimal("0")
    # 先按编号排序一次性锁定所有来源批次，避免并发事务交叉加锁死锁
    # （PostgreSQL 行锁；SQLite 为立即写锁）
    locked = _lock_batches_in_order(session, lock_nos)
    for no in source_nos:
        batch = locked.get(no)
        if batch is None:
            raise errors.batch_not_found(no)
        sources[no] = batch

    # 预留行在全部批次锁之后再加锁
    if reservation is not None:
        reservation = _lock_reservation(session, data.reservation_no)
        assert reservation is not None

    for item in data.inputs:
        batch = sources[item.batch_no]
        if batch.unit != data.unit:
            raise errors.unit_mismatch(item.batch_no, batch.unit, data.unit)
        if batch.is_quarantined:
            raise errors.batch_quarantined(item.batch_no)

    # 已撤销生产单的产出编号永久保留但不得再投料（与 QC 锁串行化，
    # 撤销事务取 QC 表锁，因此这里读到的“有效/已撤销”状态与最终一致）
    cancelled_set = cancelled_output_nos(session, lock_nos)
    for item in data.inputs:
        if item.batch_no in cancelled_set:
            raise errors.batch_cancelled(item.batch_no)

    # 按预留投料校验：未取消/未核销/未到期；来源未隔离未作废；
    # 投料批次、数量、单位与预留完全一致（仅一笔投料）
    if reservation is not None:
        status = reservation_status(reservation, session=session, now=now)
        if status != "active":
            raise errors.reservation_not_active(data.reservation_no, status)
        reserved_source = locked.get(reservation.source_no)
        if reserved_source is None:
            raise errors.batch_not_found(reservation.source_no)
        if reservation.source_no in cancelled_set:
            raise errors.batch_cancelled(reservation.source_no)
        if reserved_source.is_quarantined:
            raise errors.batch_quarantined(reservation.source_no)
        # 投料批次、数量和单位须与预留完全一致：按预留核销只允许单笔投料
        if len(data.inputs) != 1:
            raise errors.reservation_mismatch(
                data.reservation_no, "投料笔数", 1, len(data.inputs)
            )
        item = data.inputs[0]
        if item.batch_no != reservation.source_no:
            raise errors.reservation_mismatch(
                data.reservation_no,
                "source_batch_no",
                reservation.source_no,
                item.batch_no,
            )
        if item.quantity != reservation.qty:
            raise errors.reservation_mismatch(
                data.reservation_no, "quantity", reservation.qty, item.quantity
            )
        if data.unit != reservation.unit:
            raise errors.reservation_mismatch(
                data.reservation_no, "unit", reservation.unit, data.unit
            )

    if session.get(Batch, data.output_batch_no) is not None:
        raise errors.batch_exists(data.output_batch_no)

    # 实存/可用量校验：普通生产只能消耗可用量（预留占用的额度不得被普通生产超用）；
    # 按预留核销时数量与预留严格相等，预留有效即保证 available >= qty，
    # 仍以实存兜底核对，绝不让余量为负。
    if reservation is None:
        reserved_totals = _active_reserved_totals(session, source_nos, now=now)
    for item in data.inputs:
        batch = sources[item.batch_no]
        if reservation is not None:
            if item.quantity > batch.remaining_qty:
                raise errors.insufficient(
                    item.batch_no, item.quantity, batch.remaining_qty
                )
        else:
            available = batch.remaining_qty - reserved_totals.get(item.batch_no, Decimal(0))
            # 兼容既有错误码：普通生产超用（此时先受可用量约束）仍报
            # INSUFFICIENT_BALANCE，仅在消息中体现可用量口径
            if item.quantity > available:
                exc = errors.insufficient(item.batch_no, item.quantity, available)
                exc.message = (
                    f"批次 {item.batch_no!r} 可用量不足：请求 {item.quantity}，"
                    f"当前可用 {available}（可用量 = 实存余量 - 未取消且未到期的预留）"
                )
                raise exc
        total_input += item.quantity

    if data.output_quantity > total_input:
        raise errors.AppError(
            "OUTPUT_EXCEEDS_INPUT",
            f"产出量 {normalize_decimal(data.output_quantity)} 超过投入总量 "
            f"{normalize_decimal(total_input)}",
            422,
            details={
                "output_quantity": normalize_decimal(data.output_quantity),
                "total_input": normalize_decimal(total_input),
            },
        )

    output = Batch(
        batch_no=data.output_batch_no,
        unit=data.unit,
        initial_qty=data.output_quantity,
        remaining_qty=data.output_quantity,
    )
    session.add(output)
    try:
        session.flush()  # 让产出批次主键可用，并尽早暴露编号冲突
    except IntegrityError:
        raise errors.batch_exists(data.output_batch_no) from None

    production = Production(
        output_no=output.batch_no, output_qty=data.output_quantity
    )
    session.add(production)
    session.flush()

    for item in data.inputs:
        batch = sources[item.batch_no]
        batch.remaining_qty -= item.quantity
        session.add(
            ProductionInput(
                production_id=production.id,
                source_no=batch.batch_no,
                qty=item.quantity,
            )
        )

    # 同事务核销预留：占用额度随实存扣减一并解除（consumed 不再计入预留合计）
    if reservation is not None:
        reservation.status = "consumed"
        reservation.consumed_production_id = production.id
        reservation.consumed_at = _store_dt(session, now)

    session.flush()
    # 重新加载聚合，便于响应组装
    return session.execute(
        select(Production)
        .options(selectinload(Production.inputs))
        .where(Production.id == production.id)
    ).scalar_one()


def cancelled_output_nos(session: Session, batch_nos: list[str]) -> set[str]:
    """给定批次编号中，属于“已撤销生产单产出”的子集。"""
    if not batch_nos:
        return set()
    rows = session.execute(
        select(Production.output_no)
        .join(ProductionCancellation, ProductionCancellation.production_id == Production.id)
        .where(Production.output_no.in_(batch_nos))
    ).all()
    return {r[0] for r in rows}


def _load_production(session: Session, output_no: str) -> Production | None:
    """按产出编号加载生产单（含投料、来源批次与撤销记录）。"""
    return session.execute(
        select(Production)
        .options(
            selectinload(Production.inputs).selectinload(ProductionInput.source_batch),
            selectinload(Production.cancellation),
        )
        .where(Production.output_no == output_no)
    ).scalar_one_or_none()


def _require_production(session: Session, output_no: str) -> Production:
    """产出编号必须存在且确为某生产单的产出，否则按约定拒绝（不改任何状态）。"""
    if session.get(Batch, output_no) is None:
        raise errors.batch_not_found(output_no)
    prod = _load_production(session, output_no)
    if prod is None:
        raise errors.production_not_found(output_no)
    return prod


def get_production_detail(session: Session, output_no: str) -> dict[str, Any]:
    """查询生产单：原单据（投料明细）+ 撤销状态/撤销原因（若已撤销）。"""
    prod = _require_production(session, output_no)
    unit = session.get(Batch, output_no).unit
    return _production_detail_dict(prod, unit)


def get_cancellation_detail(session: Session, output_no: str) -> dict[str, Any]:
    """查询某产出的撤销记录（未撤销 -> CANCELLATION_NOT_FOUND 404）。"""
    prod = _require_production(session, output_no)
    if prod.cancellation is None:
        raise errors.cancellation_not_found(output_no)
    return _cancellation_result_dict(prod)


def _production_detail_dict(prod: Production, unit: str) -> dict[str, Any]:
    return {
        "output_batch_no": prod.output_no,
        "unit": unit,
        "output_quantity": prod.output_qty,
        "inputs": [
            {"batch_no": pi.source_no, "quantity": pi.qty}
            for pi in sorted(prod.inputs, key=lambda x: x.id)
        ],
        "status": "cancelled" if prod.cancellation is not None else "active",
        "cancellation": (
            {
                "cancel_reason": prod.cancellation.cancel_reason,
                "cancelled_at": prod.cancellation.cancelled_at,
            }
            if prod.cancellation is not None
            else None
        ),
    }


def _cancellation_result_dict(prod: Production) -> dict[str, Any]:
    """撤销结果/撤销记录：产出编号、状态、返还明细、撤销原因与时间。

    返还量直接取原生产单投料明细（撤销后这些行不再变化）；remaining_qty 取
    来源批次的当前余量（撤销事务内即“返还后余量”，历史查询时可能已被后续
    生产再次消耗，展示的是查询时刻余量）。
    """
    assert prod.cancellation is not None
    returns = []
    for pi in sorted(prod.inputs, key=lambda x: x.id):
        source = pi.source_batch
        # 撤销事务已把原投料量加回；若之后该来源又被其它生产消耗，
        # 这里展示的是“当前余量”，返还量本身恒为原投料量。
        remaining = source.remaining_qty
        returns.append(
            {
                "batch_no": pi.source_no,
                "returned_quantity": pi.qty,
                "remaining_qty": remaining,
            }
        )
    return {
        "output_batch_no": prod.output_no,
        "status": "cancelled",
        "returns": returns,
        "cancellation": {
            "cancel_reason": prod.cancellation.cancel_reason,
            "cancelled_at": prod.cancellation.cancelled_at,
        },
    }


def cancel_production(
    session: Session, output_no: str, data: ProductionCancellationIn
) -> dict[str, Any]:
    """撤销一张录入错误的生产单。

    仅当产出满足全部条件时才允许撤销，任一不符整笔拒绝、不改变任何库存：
    1. 产出编号存在且确为生产单产出；
    2. 该生产单尚未撤销（重复撤销拒绝）；
    3. 产出当前非疑似；
    4. 产出无质检复核记录；
    5. 产出从未被后续生产投料（仅统计仍有效、未撤销的下游单）。

    通过后在同一事务内：
    - 向各来源批次返还原投料量（即使来源批次已隔离也照常返还）；
    - 产出批次余量清零并标记作废（编号保留，不再允许投料）；
    - 写入撤销记录（原因 + 时间）；
    - 撤销的投料边即时退出有效谱系与疑似隔离传播，并按有效谱系重算
      产出自身的隔离标志。

    并发：先取与污染标记/复核相同的 QC 串行化锁（PG 表锁，SQLite 由
    BEGIN IMMEDIATE 全库串行），再按编号升序锁定产出与各来源批次——
    与生产投料的排序行锁共用同一全局加锁顺序，杜绝死锁；撤销与投料、
    污染标记、复核并发时等价于某一串行顺序，不会超用余量或误放行。
    """
    _acquire_qc_lock(session)

    # 先在固定点上确认生产单存在，再确定需要锁定的来源集合
    prod = _load_production(session, output_no)
    if session.get(Batch, output_no) is None:
        raise errors.batch_not_found(output_no)
    if prod is None:
        raise errors.production_not_found(output_no)
    if prod.cancellation is not None:
        raise errors.production_already_cancelled(output_no)

    source_nos = sorted({pi.source_no for pi in prod.inputs})
    # 产出与来源统一按编号升序加锁，与 submit_production 的加锁顺序一致
    locked = _lock_batches_in_order(session, [output_no, *source_nos])
    output = locked[output_no]

    # 条件 3/4：当前疑似、或已有复核记录 -> 拒绝
    if output.is_suspect:
        raise errors.output_is_suspect(output_no)
    reviewed = session.execute(
        select(BatchReview.id).where(BatchReview.batch_no == output_no).limit(1)
    ).first()
    if reviewed is not None:
        raise errors.output_has_review(output_no)

    # 条件 5：产出已被“仍有效”的后续生产投料 -> 拒绝（下游单自身已撤销的不算）
    downstream = session.execute(
        select(ProductionInput.id)
        .join(Production, Production.id == ProductionInput.production_id)
        .outerjoin(
            ProductionCancellation,
            ProductionCancellation.production_id == Production.id,
        )
        .where(ProductionInput.source_no == output_no)
        .where(ProductionCancellation.id.is_(None))
        .limit(1)
    ).first()
    if downstream is not None:
        raise errors.output_already_consumed(output_no)

    # 全部条件满足：同事务返还各来源原投料量（来源是否隔离不影响返还）
    for pi in prod.inputs:
        source = locked[pi.source_no]
        source.remaining_qty += pi.qty

    # 产出逻辑作废：编号/原单据保留，余量清零，永不再投料
    output.remaining_qty = 0

    # 产出作废旧仍占额度的预留（active 且未到期）随之在同一事务内自动取消，
    # 避免“作废批次余量 0 却仍有预留占额”导致可用量为负；已核销/已取消的不动。
    now = utcnow()
    active_reservations = session.execute(
        select(MaterialReservation).where(
            MaterialReservation.source_no == output_no,
            MaterialReservation.status == "active",
            MaterialReservation.expires_at > _store_dt(session, now),
        )
    ).scalars().all()
    for rv in active_reservations:
        rv.status = "cancelled"
        rv.cancel_reason = f"来源批次 {output_no} 随生产单撤销作废，预留自动取消"
        rv.cancelled_at = _store_dt(session, now)

    record = ProductionCancellation(
        production_id=prod.id, cancel_reason=data.cancel_reason.strip()
    )
    session.add(record)
    # 显式建立关系，避免 selectinload 的身份映射缓存仍把 cancellation 视作 None
    prod.cancellation = record
    session.flush()  # 让撤销边对下面的有效谱系谓词立即不可见

    # 撤销边退出传播后，按“自身疑似或任一有效祖先疑似”重算产出隔离标志。
    # 上面已拒绝“自身疑似”，且其全部祖先边刚被本单撤销，故必为 False；
    # 仍以统一谓词落库，避免任何标志漂移。
    should_quarantine = bool(
        session.execute(text(_ANCESTOR_SUSPECT_SQL), {"root": output_no}).scalar_one()
    )
    output.is_quarantined = should_quarantine

    session.flush()
    return _cancellation_result_dict(prod)


# ---------------------------------------------------------------- 谱系与污染

# 边方向：source -> output（原料指向产出）。
# 已撤销生产单的投料边不进入“有效谱系”：以 LEFT JOIN ... IS NULL 排除
# 存在撤销记录的生产单（撤销是逻辑作废，原行保留可查但不再参与谱系/疑似传播）。
_EDGE_CTE = """
SELECT pi.source_no AS parent_no, p.output_no AS child_no
FROM production_inputs pi
JOIN productions p ON p.id = pi.production_id
LEFT JOIN production_cancellations pc ON pc.production_id = p.id
WHERE pc.id IS NULL
"""


def _descendant_rows(session: Session, batch_no: str) -> list[dict[str, Any]]:
    """递归求全部后代（直接/间接使用该批次的产出），带层数。"""
    sql = f"""
    WITH RECURSIVE walk(batch_no, depth, path) AS (
        SELECT :root, 0, :root
        UNION
        SELECT e.child_no, w.depth + 1, w.path || '>' || e.child_no
        FROM walk w JOIN ({_EDGE_CTE}) e ON e.parent_no = w.batch_no
    )
    SELECT batch_no, depth FROM walk WHERE depth > 0 ORDER BY depth, batch_no
    """
    return [dict(r._mapping) for r in session.execute(text(sql), {"root": batch_no})]


def _ancestor_rows(session: Session, batch_no: str) -> list[dict[str, Any]]:
    """递归求全部祖先（直接/间接被其消耗的原料），带层数。"""
    sql = f"""
    WITH RECURSIVE walk(batch_no, depth) AS (
        SELECT :root, 0
        UNION
        SELECT e.parent_no, w.depth + 1
        FROM walk w JOIN ({_EDGE_CTE}) e ON e.child_no = w.batch_no
    )
    SELECT batch_no, depth FROM walk WHERE depth > 0 ORDER BY depth, batch_no
    """
    return [dict(r._mapping) for r in session.execute(text(sql), {"root": batch_no})]


def _load_batches_map(session: Session, nos: list[str]) -> dict[str, Batch]:
    if not nos:
        return {}
    rows = session.execute(select(Batch).where(Batch.batch_no.in_(nos))).scalars().all()
    return {b.batch_no: b for b in rows}


def _lock_descendant_closure(
    session: Session, root_no: str
) -> tuple[Batch | None, list[str], dict[str, Batch]]:
    """在固定点谱系上加载 root 及其全部后代。

    调用方必须先经 ``_acquire_qc_lock`` 与并发生产事务串行化，因此此处读到的
    谱系边与批次状态在本事务内稳定（PG 上表锁排除了并发投料，SQLite 上
    BEGIN IMMEDIATE 全库串行），不存在边被并发追加的问题。

    返回 (root 批次[可能不存在], 后代编号[depth 序], 批次映射)。
    """
    root = _lock_batch(session, root_no)
    if root is None:
        return None, [], {}

    rows = _descendant_rows(session, root_no)
    descendant_nos = [r["batch_no"] for r in rows]
    targets = _load_batches_map(session, descendant_nos)
    return root, descendant_nos, {root_no: root, **targets}


# 判断给定批次是否应隔离（自身疑似或任一祖先疑似）：
# 祖先部分以目标批次为根沿 source -> output 反向递归（严格祖先），
# 再并上目标自身；谱系边以同事务内可见数据为准，谱系为无环 DAG。
_ANCESTOR_SUSPECT_SQL = f"""
WITH RECURSIVE anc(batch_no) AS (
    SELECT e.parent_no
    FROM ({_EDGE_CTE}) e
    WHERE e.child_no = :root
    UNION
    SELECT e.parent_no FROM anc a
    JOIN ({_EDGE_CTE}) e ON e.child_no = a.batch_no
)
SELECT EXISTS(
    SELECT 1 FROM batches s
    WHERE s.is_suspect AND (
        s.batch_no = :root OR s.batch_no IN (SELECT batch_no FROM anc)
    )
)
"""


def _suspect_closure_map(
    session: Session, batch_nos: list[str]
) -> dict[str, bool]:
    """对每个目标批次求：自身疑似，或任一祖先疑似（是否应隔离）。"""
    return {
        no: bool(session.execute(text(_ANCESTOR_SUSPECT_SQL), {"root": no}).scalar_one())
        for no in batch_nos
    }


def lineage(session: Session, batch_no: str) -> dict[str, Any]:
    root = session.get(Batch, batch_no)
    if root is None:
        raise errors.batch_not_found(batch_no)

    up_rows = _ancestor_rows(session, batch_no)
    down_rows = _descendant_rows(session, batch_no)

    up_map = _load_batches_map(session, [r["batch_no"] for r in up_rows])
    down_map = _load_batches_map(session, [r["batch_no"] for r in down_rows])

    def node(r: dict[str, Any], b: Batch) -> dict[str, Any]:
        return {
            "batch_no": b.batch_no,
            "unit": b.unit,
            "remaining_qty": b.remaining_qty,
            "is_suspect": b.is_suspect,
            "is_quarantined": b.is_quarantined,
            "depth": r["depth"],
        }

    upstream = [node(r, up_map[r["batch_no"]]) for r in up_rows if r["batch_no"] in up_map]
    downstream = [
        node(r, down_map[r["batch_no"]]) for r in down_rows if r["batch_no"] in down_map
    ]

    involved = set(up_map) | set(down_map) | {batch_no}
    # 有效谱系子图仅包含未撤销生产单的投料边
    edges = session.execute(
        select(ProductionInput.source_no, Production.output_no)
        .join(Production, Production.id == ProductionInput.production_id)
        .outerjoin(
            ProductionCancellation,
            ProductionCancellation.production_id == Production.id,
        )
        .where(ProductionCancellation.id.is_(None))
        .where(
            ProductionInput.source_no.in_(involved),
            Production.output_no.in_(involved),
        )
    ).all()
    return {
        "batch_no": batch_no,
        "upstream": upstream,
        "downstream": downstream,
        "edges": [{"from": s, "to": o} for s, o in edges],
    }


def mark_suspect(session: Session, data: SuspectIn) -> dict[str, Any]:
    """标记批次疑似污染，并隔离其全部后代产出批次。

    混料规则：一个产出若混有受污染来源（直接或间接），该产出整体隔离；
    隔离批次不得再投料（提交生产时拦截）。

    并发：先取 QC 串行化锁（PG 表锁 / SQLite 全库写锁），再读取后代闭包
    并写状态，与生产、复核事务整体等价于某一串行顺序。
    """
    _acquire_qc_lock(session)
    batch, descendant_nos, locked = _lock_descendant_closure(session, data.batch_no)
    if batch is None:
        raise errors.batch_not_found(data.batch_no)

    previously_quarantined = {no for no, b in locked.items() if b.is_quarantined}

    batch.is_suspect = True
    batch.is_quarantined = True
    if data.note:
        batch.note = data.note

    for no in descendant_nos:
        target = locked[no]
        target.is_quarantined = True

    session.flush()
    newly = sorted(set(descendant_nos) - previously_quarantined)
    return {
        "source": data.batch_no,
        "is_suspect": True,
        "affected_batch_nos": descendant_nos,
        "newly_quarantined": newly,
    }


def review_suspect(session: Session, data: ReviewIn) -> dict[str, Any]:
    """质检复核确认误报：撤销批次疑似标记，并重算自身及全部后代的隔离状态。

    规则：
    - 只接受当前仍疑似的批次；编号不存在 -> SOURCE_NOT_FOUND(404)，
      已撤销或从未疑似 -> BATCH_NOT_SUSPECT(409)，均不改变任何状态；
    - 重算范围为该批次及其全部后代：自身或任一祖先仍疑似则继续隔离，否则解除；
    - 混料后代若还有另一条疑似来源（祖先），不得因本来源撤销而解除隔离。

    并发：先取 QC 串行化锁，再按与 mark_suspect 相同的固定点闭包读取，
    与生产事务的来源行锁互斥，保证复核 / 污染标记 / 投料并发时等价于
    某一串行顺序；隔离批次在任一串行结局下都不会被投料。
    """
    _acquire_qc_lock(session)
    batch, descendant_nos, locked = _lock_descendant_closure(session, data.batch_no)
    if batch is None:
        raise errors.batch_not_found(data.batch_no)
    if not batch.is_suspect:
        raise errors.batch_not_suspect(data.batch_no)

    targets = [data.batch_no, *descendant_nos]

    # 撤销自身疑似标记并固化复核说明（同一批次可能经历 标记→撤销→再标记→再撤销，
    # 复用已有复核行更新说明，避免唯一约束冲突）
    batch.is_suspect = False
    review = session.execute(
        select(BatchReview).where(BatchReview.batch_no == data.batch_no)
    ).scalar_one_or_none()
    if review is None:
        session.add(
            BatchReview(batch_no=data.batch_no, review_note=data.review_note)
        )
    else:
        review.review_note = data.review_note
        review.reviewed_at = func.now()

    # 重算：自身疑似或任一祖先疑似 -> 隔离；否则解除。
    # 注意：session.execute(text()) 不会触发 ORM autoflush，必须显式 flush，
    # 让下面的原始 SQL 谓词读到本事务刚撤销的 is_suspect 状态。
    session.flush()
    should_quarantine = _suspect_closure_map(session, targets)

    released: list[str] = []
    still: list[str] = []
    for no in targets:
        target = locked[no]
        quarantined = should_quarantine.get(no, False)
        if target.is_quarantined and not quarantined:
            released.append(no)
        target.is_quarantined = quarantined
        if quarantined:
            still.append(no)

    session.flush()
    released.sort()
    # still 中的第一个即复核批次自身（撤销后通常已解除；仍疑似祖先时会保留）
    still.sort()
    return {
        "reviewed_batch_no": data.batch_no,
        "is_suspect": False,
        "review_note": data.review_note,
        "affected_batch_nos": descendant_nos,
        "released_batch_nos": released,
        "still_quarantined": still,
    }
