"""预留并发：并发预留/投料绝不让可用量为负；预留核销与取消/普通投料串行等价。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app import service
from app.db import session_scope
from app.errors import AppError
from app.models import Batch
from app.schemas import (
    ProductionIn,
    RawBatchIn,
    ReservationIn,
)


def _reservation(no: str, qty: str, hours: int = 1) -> ReservationIn:
    return ReservationIn(
        reservation_no=no,
        source_batch_no="RM-1",
        unit="kg",
        quantity=Decimal(qty),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=hours),
    )


def _produce(idx: int, qty: str, reservation_no: str | None = None) -> ProductionIn:
    return ProductionIn(
        output_batch_no=f"FG-{idx}",
        unit="kg",
        output_quantity=Decimal(qty),
        inputs=[{"batch_no": "RM-1", "quantity": Decimal(qty)}],  # type: ignore[list-item]
        reservation_no=reservation_no,
    )


def test_concurrent_reservations_never_overcommit(engine):
    # 实存 100，8 个线程各预留 15（共 120）：恰 6 笔成功、2 笔可用量不足
    with session_scope() as s:
        service.register_raw_batch(s, RawBatchIn(batch_no="RM-1", unit="kg", quantity="100"))

    results: list[str] = []
    barrier = threading.Barrier(8)

    def worker(idx: int) -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.create_reservation(s, _reservation(f"R-{idx}", "15"))
            results.append("ok")
        except AppError as exc:
            results.append(exc.code)
        except Exception as exc:
            results.append(f"ERROR:{type(exc).__name__}:{exc}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(8)))

    assert results.count("ok") == 6, results
    assert results.count("INSUFFICIENT_AVAILABLE") == 2, results
    assert not [r for r in results if r.startswith("ERROR")], results

    with session_scope() as s:
        batch = s.get(Batch, "RM-1")
        reserved = service._active_reserved_total(s, "RM-1")
        assert batch.remaining_qty == Decimal("100.0000")  # 预留不动实存
        assert reserved == Decimal("90.0000")
        assert batch.remaining_qty - reserved == Decimal("10.0000")


def test_concurrent_reservation_and_normal_production_never_negative(engine):
    # 实存 100：4 个预留各 20 与 4 笔普通生产各 20 并发；
    # 预留成功 r 笔（占用 20r）+ 普通生产成功 p 笔（消耗 20p）必须满足 20r + 20p <= 100
    with session_scope() as s:
        service.register_raw_batch(s, RawBatchIn(batch_no="RM-1", unit="kg", quantity="100"))

    results: dict[str, list[str]] = {"reserve": [], "produce": []}
    barrier = threading.Barrier(8)

    def reserve_worker(idx: int) -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.create_reservation(s, _reservation(f"R-{idx}", "20"))
            results["reserve"].append("ok")
        except AppError as exc:
            results["reserve"].append(exc.code)
        except Exception as exc:
            results["reserve"].append(f"ERROR:{type(exc).__name__}:{exc}")

    def produce_worker(idx: int) -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_production(s, _produce(idx, "20"))
            results["produce"].append("ok")
        except AppError as exc:
            results["produce"].append(exc.code)
        except Exception as exc:
            results["produce"].append(f"ERROR:{type(exc).__name__}:{exc}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(reserve_worker, i) for i in range(4)]
        futures += [pool.submit(produce_worker, 100 + i) for i in range(4)]
        for f in futures:
            f.result()

    r_ok = results["reserve"].count("ok")
    p_ok = results["produce"].count("ok")
    assert not [x for x in results["reserve"] + results["produce"] if x.startswith("ERROR")], results
    assert 20 * r_ok + 20 * p_ok <= 100, (r_ok, p_ok, results)

    with session_scope() as s:
        batch = s.get(Batch, "RM-1")
        reserved = service._active_reserved_total(s, "RM-1")
        # 可用量恒非负
        assert batch.remaining_qty - reserved >= 0
        assert batch.remaining_qty == Decimal(100) - Decimal(20) * p_ok


def test_consume_reservation_concurrent_with_cancel_one_wins(engine):
    # 同一预留：按预留投料与主动取消并发，恰好一方成功，不能既核销又取消
    with session_scope() as s:
        service.register_raw_batch(s, RawBatchIn(batch_no="RM-1", unit="kg", quantity="100"))
        service.create_reservation(s, _reservation("R-1", "10"))

    out = {"consume": None, "cancel": None}
    barrier = threading.Barrier(2)

    def consume() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_production(s, _produce(1, "10", reservation_no="R-1"))
            out["consume"] = "ok"
        except AppError as exc:
            out["consume"] = exc.code
        except Exception as exc:
            out["consume"] = f"ERROR:{type(exc).__name__}:{exc}"

    def cancel() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                from app.schemas import ReservationCancelIn

                service.cancel_reservation(s, "R-1", ReservationCancelIn(reason="x"))
            out["cancel"] = "ok"
        except AppError as exc:
            out["cancel"] = exc.code
        except Exception as exc:
            out["cancel"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [consume, cancel]))

    assert not any(str(v).startswith("ERROR") for v in out.values()), out
    assert out["consume"] == "ok" or out["cancel"] == "ok", out
    assert not (out["consume"] == "ok" and out["cancel"] == "ok"), out

    with session_scope() as s:
        batch = s.get(Batch, "RM-1")
        if out["consume"] == "ok":
            # 核销成功：取消必被拒；实存扣 10，预留不再占额
            assert out["cancel"] == "RESERVATION_NOT_ACTIVE", out
            assert batch.remaining_qty == Decimal("90.0000")
            assert service._active_reserved_total(s, "RM-1") == 0
        else:
            # 取消成功：核销必被拒；实存不变，额度释放
            assert out["consume"] == "RESERVATION_NOT_ACTIVE", out
            assert batch.remaining_qty == Decimal("100.0000")
            assert service._active_reserved_total(s, "RM-1") == 0


def test_concurrent_consume_same_reservation_only_one_wins(engine):
    # 两个线程同时用同一预留投料：最多一笔成功，另一笔因预留已核销被拒
    with session_scope() as s:
        service.register_raw_batch(s, RawBatchIn(batch_no="RM-1", unit="kg", quantity="100"))
        service.create_reservation(s, _reservation("R-1", "10"))

    results: list[str] = []
    barrier = threading.Barrier(2)

    def worker(idx: int) -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_production(s, _produce(idx, "10", reservation_no="R-1"))
            results.append("ok")
        except AppError as exc:
            results.append(exc.code)
        except Exception as exc:
            results.append(f"ERROR:{type(exc).__name__}:{exc}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(worker, (1, 2)))

    assert results.count("ok") == 1, results
    assert results.count("RESERVATION_NOT_ACTIVE") == 1, results
    with session_scope() as s:
        assert s.get(Batch, "RM-1").remaining_qty == Decimal("90.0000")
