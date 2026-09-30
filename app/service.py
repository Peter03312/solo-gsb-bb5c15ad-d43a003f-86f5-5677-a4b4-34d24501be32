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


def utcnow() -> datetime:
    """服务端当前时间（UTC，带时区）。预留到期与否一律以此为准。"""
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """PG 读回为带时区时间；SQLite 读回丢失时区（按 UTC 落库），统一补成 UTC。"""
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


def _active_reserved_qty(
    session: Session, source_nos: list[str], now: datetime | None = None
) -> dict[str, Decimal]:
    """各来源批次当前“有效预留”（未取消、未核销、未到期）的预留量之和。

    预留不改变实存；本聚合即实存之外的占用额度。到期按调用方传入的
    服务端时间（默认当前 UTC）动态判定，无需后台清理。
    """
    if not source_nos:
        return {}
    moment = now or utcnow()
    rows = session.execute(
        select(
            MaterialReservation.source_no,
            func.coalesce(func.sum(MaterialReservation.qty), 0),
        )
        .where(MaterialReservation.source_no.in_(source_nos))
        .where(MaterialReservation.status == "active")
        .where(MaterialReservation.expires_at > moment)
        .group_by(MaterialReservation.source_no)
    ).all()
    return {source: Decimal(qty or 0) for source, qty in rows}


def _available_map(
    session: Session, sources: dict[str, Batch], now: datetime | None = None
) -> dict[str, Decimal]:
    """可用量 = 实存 - 有效预留量；不低于 0（不变量：有效预留总量不超过实存）。"""
    moment = now or utcnow()
    reserved = _active_reserved_qty(session, list(sources), moment)
    return {
        no: max(Decimal("0"), b.remaining_qty - reserved.get(no, Decimal("0")))
        for no, b in sources.items()
    }


def _lock_reservation(session: Session, reservation_no: str) -> MaterialReservation | None:
    """按预留单号读取并加行锁（PG FOR UPDATE；SQLite 由写事务锁覆盖）。"""
    stmt = select(MaterialReservation).where(
        MaterialReservation.reservation_no == reservation_no
    )
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    return session.execute(stmt).scalar_one_or_none()


def submit_production(session: Session, data: ProductionIn) -> Production:
    """提交一次生产：校验全部通过后才扣减余量并创建产出批次。

    普通生产（不填 reservation_no）只能消耗各来源的**可用量**
    （实存 - 未取消且未到期预留）；预留生产（填 reservation_no）必须
    恰好只有一条投料，且投料批次、数量、单位与预留完全一致，随后在同一
    事务内核销预留、扣减实存并生成产出。

    校验顺序（任一失败整笔拒绝、不扣余量、不占产出编号）：
    1. 投入明细编号不重复；
    2. 每个来源批次存在、单位一致、未隔离、未作废；
    3. 产出编号未被占用；
    4. 预留单号（若填）存在、未取消、未核销、未到期、来源未隔离/作废，
       且投料批次、数量、单位与预留完全一致；
    5. 用量为正且不超过可用量（预留生产为自占额度，天然满足）；
    6. 产出总量不超过投入总量。
    """
    now = utcnow()
    source_nos = [item.batch_no for item in data.inputs]
    if len(set(source_nos)) != len(source_nos):
        raise errors.AppError(
            "DUPLICATE_INPUT",
            "同一生产单中来源批次不得重复",
            422,
        )

    sources: dict[str, Batch] = {}
    # 先按编号排序一次性锁定所有来源批次，避免并发事务交叉加锁死锁
    # （PostgreSQL 行锁；SQLite 为立即写锁）
    locked = _lock_batches_in_order(session, source_nos)
    for no in source_nos:
        batch = locked.get(no)
        if batch is None:
            raise errors.batch_not_found(no)
        sources[no] = batch

    for item in data.inputs:
        batch = sources[item.batch_no]
        if batch.unit != data.unit:
            raise errors.unit_mismatch(item.batch_no, batch.unit, data.unit)
        if batch.is_quarantined:
            raise errors.batch_quarantined(item.batch_no)

    # 已撤销生产单的产出编号永久保留但不得再投料（与 QC 锁串行化，
    # 撤销事务取 QC 表锁，因此这里读到的“有效/已撤销”状态与最终一致）
    cancelled_nos = cancelled_output_nos(session, source_nos)
    for item in data.inputs:
        if item.batch_no in cancelled_nos:
            raise errors.batch_cancelled(item.batch_no)

    if session.get(Batch, data.output_batch_no) is not None:
        raise errors.batch_exists(data.output_batch_no)

    # 预留生产：预留行在批次锁之后按固定顺序加锁（先批次后预留，全局一致，
    # 杜绝与预留创建/取消事务之间的死锁环）；SQLite 由写锁整体串行。
    reservation: MaterialReservation | None = None
    if data.reservation_no is not None:
        reservation = _lock_reservation(session, data.reservation_no)
        if reservation is None:
            raise errors.reservation_not_found(data.reservation_no)
        if reservation.status == "cancelled":
            raise errors.reservation_cancelled(data.reservation_no)
        if reservation.status == "consumed":
            raise errors.reservation_consumed(data.reservation_no)
        if _as_utc(reservation.expires_at) <= now:
            raise errors.reservation_expired(
                data.reservation_no, _as_utc(reservation.expires_at)
            )
        # 隔离期间预留仍占额度但不得核销；来源已隔离/作废整笔拒绝
        source_batch = sources.get(reservation.source_no)
        if source_batch is None:
            # 预留来源必须正是本笔投料的来源
            raise errors.reservation_mismatch(
                data.reservation_no,
                "source_batch_no",
                reservation.source_no,
                data.inputs[0].batch_no,
            )
        if source_batch.is_quarantined:
            raise errors.batch_quarantined(reservation.source_no)
        if reservation.source_no in cancelled_nos:
            raise errors.batch_cancelled(reservation.source_no)
        if len(data.inputs) != 1:
            raise errors.reservation_mismatch(
                data.reservation_no,
                "inputs",
                f"[{reservation.source_no}]",
                f"[{', '.join(source_nos)}]",
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
                data.reservation_no,
                "quantity",
                normalize_decimal(reservation.qty),
                normalize_decimal(item.quantity),
            )
        if data.unit != reservation.unit:
            raise errors.reservation_mismatch(
                data.reservation_no,
                "unit",
                reservation.unit,
                data.unit,
            )

    total_input = Decimal("0")
    if reservation is not None:
        # 预留额度恰为本笔用量：在行锁内自占自销，不会超用，也不会被并发抢占。
        # 仍以“实存 >= 本笔用量”兜底（有效预留成立时该不等式恒成立）。
        item = data.inputs[0]
        batch = sources[item.batch_no]
        if item.quantity > batch.remaining_qty:
            raise errors.insufficient(
                item.batch_no, item.quantity, batch.remaining_qty
            )
        total_input = item.quantity
    else:
        available = _available_map(session, sources, now)
        for item in data.inputs:
            if item.quantity > available[item.batch_no]:
                raise errors.available_insufficient(
                    item.batch_no, item.quantity, available[item.batch_no]
                )
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

    # 预留与扣减、产出在同一事务内核销：任一前置失败都不会走到这里
    if reservation is not None:
        reservation.status = "consumed"
        reservation.consumed_by_output_no = output.batch_no

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

    # 该单若核销过原料预留：整单作废后把预留恢复为有效（额度状态回到
    # “已预留但未投料”）。已到期者恢复后在读取点动态呈现 expired、不再占额；
    # 未到期者继续占用，可再次凭单号投料或主动取消。
    consumed_reservations = session.execute(
        select(MaterialReservation).where(
            MaterialReservation.consumed_by_output_no == output_no,
            MaterialReservation.status == "consumed",
        )
    ).scalars().all()
    for reservation in consumed_reservations:
        reservation.status = "active"
        reservation.consumed_by_output_no = None

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


# ---------------------------------------------------------------- 原料预留


def create_reservation(session: Session, data: ReservationIn) -> dict[str, Any]:
    """登记一笔原料预留。

    - 预留编号全局唯一（与批次编号空间无关，但同样不得重复）；
    - 来源批次必须存在；预留不改变实存，仅占用“可用量”；
    - 截止时间必须在服务端当前时间之后；
    - 有效预留总量（含本笔）不得超过来源实存，否则整笔拒绝、无部分预留。

    并发：先按编号升序锁定来源批次行（与生产提交同一全局加锁顺序），再在
    锁内汇总有效预留并插入，杜绝并发预留/投料使可用量为负。隔离批次允许
    预留（隔离期间仍占额度），已作废产出批次不得预留。
    """
    now = utcnow()
    if data.expires_at <= now:
        raise errors.reservation_deadline_past(data.expires_at)

    existing = session.execute(
        select(MaterialReservation.id).where(
            MaterialReservation.reservation_no == data.reservation_no
        )
    ).first()
    if existing is not None:
        raise errors.reservation_exists(data.reservation_no)

    source = _lock_batch(session, data.source_batch_no)
    if source is None:
        raise errors.batch_not_found(data.source_batch_no)
    if source.unit != data.unit:
        raise errors.unit_mismatch(data.source_batch_no, source.unit, data.unit)
    if data.source_batch_no in cancelled_output_nos(session, [data.source_batch_no]):
        raise errors.batch_cancelled(data.source_batch_no)

    reserved = _active_reserved_qty(session, [data.source_batch_no], now).get(
        data.source_batch_no, Decimal("0")
    )
    available = max(Decimal("0"), source.remaining_qty - reserved)
    if data.quantity > available:
        raise errors.available_insufficient(
            data.source_batch_no, data.quantity, available
        )

    reservation = MaterialReservation(
        reservation_no=data.reservation_no,
        source_no=data.source_batch_no,
        unit=data.unit,
        qty=data.quantity,
        expires_at=data.expires_at,
        status="active",
    )
    session.add(reservation)
    try:
        session.flush()
    except IntegrityError:
        raise errors.reservation_exists(data.reservation_no) from None
    return get_reservation(session, data.reservation_no, now=now)


def cancel_reservation(
    session: Session, reservation_no: str, data: ReservationCancelIn
) -> dict[str, Any]:
    """主动取消一笔预留，立即释放额度。

    过期、已取消、已核销均整笔拒绝；并发上先锁来源批次行、再锁预留行，
    与生产提交/预留创建共用“先批次后预留”的加锁顺序。
    """
    now = utcnow()
    reservation = session.execute(
        select(MaterialReservation).where(
            MaterialReservation.reservation_no == reservation_no
        )
    ).scalar_one_or_none()
    if reservation is None:
        raise errors.reservation_not_found(reservation_no)

    # 先锁来源批次（全局统一加锁顺序：批次 -> 预留），再锁定预留行
    _lock_batch(session, reservation.source_no)
    reservation = _lock_reservation(session, reservation_no)
    assert reservation is not None

    if reservation.status == "cancelled":
        raise errors.reservation_cancelled(reservation_no)
    if reservation.status == "consumed":
        raise errors.reservation_consumed(reservation_no)
    if _as_utc(reservation.expires_at) <= now:
        raise errors.reservation_expired(
            reservation_no, _as_utc(reservation.expires_at)
        )

    reservation.status = "cancelled"
    reservation.cancel_reason = data.cancel_reason.strip()
    reservation.cancelled_at = now
    session.flush()
    return get_reservation(session, reservation_no, now=now)


def _reservation_status(reservation: MaterialReservation, now: datetime) -> str:
    """对外状态：落库 active 但已过截止时间时动态呈现 expired（额度已释放）。"""
    if reservation.status == "active" and _as_utc(reservation.expires_at) <= now:
        return "expired"
    return reservation.status


def get_reservation(
    session: Session, reservation_no: str, now: datetime | None = None
) -> dict[str, Any]:
    """查询预留状态与来源批次当前实存/预留/可用量。"""
    moment = now or utcnow()
    reservation = session.execute(
        select(MaterialReservation)
        .where(MaterialReservation.reservation_no == reservation_no)
    ).scalar_one_or_none()
    if reservation is None:
        raise errors.reservation_not_found(reservation_no)
    source = session.get(Batch, reservation.source_no)
    reserved = _active_reserved_qty(session, [reservation.source_no], moment).get(
        reservation.source_no, Decimal("0")
    )
    return {
        "reservation_no": reservation.reservation_no,
        "source_batch_no": reservation.source_no,
        "unit": reservation.unit,
        "quantity": float(reservation.qty),
        "expires_at": _as_utc(reservation.expires_at),
        "status": _reservation_status(reservation, moment),
        "cancel_reason": reservation.cancel_reason,
        "cancelled_at": (
            _as_utc(reservation.cancelled_at)
            if reservation.cancelled_at is not None
            else None
        ),
        "consumed_by_output_no": reservation.consumed_by_output_no,
        "created_at": _as_utc(reservation.created_at),
        "source_remaining_qty": float(source.remaining_qty),
        "source_reserved_qty": float(reserved),
        "source_available_qty": float(max(Decimal("0"), source.remaining_qty - reserved)),
    }


def list_reservations(
    session: Session, source_batch_no: str | None = None
) -> dict[str, Any]:
    """列出预留（可按来源批次过滤）；按创建先后返回，状态按当前时间动态计算。"""
    moment = utcnow()
    stmt = select(MaterialReservation).order_by(MaterialReservation.id)
    if source_batch_no is not None:
        stmt = stmt.where(MaterialReservation.source_no == source_batch_no)
    rows = session.execute(stmt).scalars().all()
    return {
        "source_batch_no": source_batch_no,
        "items": [
            {
                "reservation_no": r.reservation_no,
                "source_batch_no": r.source_no,
                "unit": r.unit,
                "quantity": float(r.qty),
                "expires_at": _as_utc(r.expires_at),
                "status": _reservation_status(r, moment),
                "cancel_reason": r.cancel_reason,
                "cancelled_at": (
                    _as_utc(r.cancelled_at) if r.cancelled_at is not None else None
                ),
                "consumed_by_output_no": r.consumed_by_output_no,
                "created_at": _as_utc(r.created_at),
            }
            for r in rows
        ],
    }


def batch_availability(session: Session, batch: Batch) -> tuple[Decimal, Decimal]:
    """返回某批次当前的 (有效预留量, 可用量)，供批次查询响应使用。"""
    reserved = _active_reserved_qty(session, [batch.batch_no]).get(
        batch.batch_no, Decimal("0")
    )
    available = max(Decimal("0"), batch.remaining_qty - reserved)
    return reserved, available


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
