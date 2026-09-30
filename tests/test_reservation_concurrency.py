"""预留并发测试：并发预留与投料绝不能使可用量为负，且无死锁/锁错误。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from app import service
from app.db import session_scope
from app.errors import AppError
from app.models import Batch
from app.schemas import ProductionIn, RawBatchIn, ReservationIn

import datetime
from datetime import timezone


def _future(hours: float = 1) -> datetime.datetime:
    return datetime.datetime.now(timezone.utc) + datetime.timedelta(hours=hours)


def _seed(qty: str = "100") -> None:
    with session_scope() as s:
        service.register_raw_batch(
            s, RawBatchIn(batch_no="RM-1", unit="kg", quantity=qty)
        )


def test_concurrent_reservations_never_overcommit(engine):
    """实存 100，6 笔并发预留各 20（共 120）：恰 5 成功 1 拒绝，有效预留恰为 100。"""
    _seed("100")
    results: list[str] = []
    barrier = threading.Barrier(6)

    def worker(idx: int) -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.create_reservation(
                    s,
                    ReservationIn(
                        reservation_no=f"R-{idx}",
                        source_batch_no="RM-1",
                        unit="kg",
                        quantity=Decimal("20"),
                        expires_at=_future(),
                    ),
                )
            results.append("ok")
        except AppError as exc:
            results.append(exc.code)
        except Exception as exc:  # 死锁等必须暴露
            results.append(f"ERROR:{type(exc).__name__}:{exc}")

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(worker, range(6)))

    assert results.count("ok") == 5, results
    assert results.count("INSUFFICIENT_BALANCE") == 1, results
    assert not [r for r in results if r.startswith("ERROR")], results

    with session_scope() as s:
        batch = s.get(Batch, "RM-1")
        reserved, available = service.batch_availability(s, batch)
        assert batch.remaining_qty == Decimal("100.0000")
        assert reserved == Decimal("100.0000")
        assert available == 0


def test_concurrent_reservation_and_plain_production_never_negative(engine):
    """实存 50：5 笔预留各 20 与 5 笔普通生产各 20 并发。
    成功的预留 + 成功的投料所占额度之和必须恰为 50，可用量恒非负。"""
    _seed("50")
    results: list[tuple[str, str]] = []
    barrier = threading.Barrier(10)

    def reserve(idx: int) -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.create_reservation(
                    s,
                    ReservationIn(
                        reservation_no=f"R-{idx}",
                        source_batch_no="RM-1",
                        unit="kg",
                        quantity=Decimal("20"),
                        expires_at=_future(),
                    ),
                )
            results.append(("reserve", "ok"))
        except AppError as exc:
            results.append(("reserve", exc.code))
        except Exception as exc:
            results.append(("reserve", f"ERROR:{type(exc).__name__}:{exc}"))

    def produce(idx: int) -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_production(
                    s,
                    ProductionIn(
                        output_batch_no=f"FG-{idx}",
                        unit="kg",
                        output_quantity=Decimal("20"),
                        inputs=[{"batch_no": "RM-1", "quantity": Decimal("20")}],  # type: ignore[list-item]
                    ),
                )
            results.append(("produce", "ok"))
        except AppError as exc:
            results.append(("produce", exc.code))
        except Exception as exc:
            results.append(("produce", f"ERROR:{type(exc).__name__}:{exc}"))

    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = [pool.submit(reserve, i) for i in range(5)]
        futs += [pool.submit(produce, i) for i in range(5)]
        list(futs)

    errors = [r for _, r in results if r.startswith("ERROR")]
    assert not errors, results

    reserve_ok = sum(1 for kind, code in results if kind == "reserve" and code == "ok")
    produce_ok = sum(1 for kind, code in results if kind == "produce" and code == "ok")
    # 每笔成功占 20，总额度 50
    assert reserve_ok + produce_ok in (2, 3)
    assert reserve_ok * 20 + produce_ok * 20 <= 50
    # 且不会出现“额度没用完却被拒”以外的超用；最紧的成功组合应满足账本守恒
    with session_scope() as s:
        batch = s.get(Batch, "RM-1")
        reserved, available = service.batch_availability(s, batch)
        # 实存 = 已投料产出扣减后的余量；可用量 = 余量 - 有效预留
        assert available >= 0
        consumed = Decimal("50") - batch.remaining_qty
        assert consumed + reserved + available == Decimal("50")


def test_concurrent_consume_same_reservation_only_one_wins(engine):
    """两笔并发生产引用同一预留：恰一笔成功核销，另一笔以已核销拒绝；
    实存只扣一次，产出只生成一个。"""
    _seed("100")
    with session_scope() as s:
        service.create_reservation(
            s,
            ReservationIn(
                reservation_no="R-1",
                source_batch_no="RM-1",
                unit="kg",
                quantity=Decimal("30"),
                expires_at=_future(),
            ),
        )

    results: list[str] = []
    barrier = threading.Barrier(2)

    def worker(out_no: str) -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_production(
                    s,
                    ProductionIn(
                        output_batch_no=out_no,
                        unit="kg",
                        output_quantity=Decimal("30"),
                        reservation_no="R-1",
                        inputs=[{"batch_no": "RM-1", "quantity": Decimal("30")}],  # type: ignore[list-item]
                    ),
                )
            results.append("ok")
        except AppError as exc:
            results.append(exc.code)
        except Exception as exc:
            results.append(f"ERROR:{type(exc).__name__}:{exc}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(worker, ["FG-1", "FG-2"]))

    assert results.count("ok") == 1, results
    assert results.count("RESERVATION_CONSUMED") == 1, results
    assert not [r for r in results if r.startswith("ERROR")], results

    with session_scope() as s:
        assert s.get(Batch, "RM-1").remaining_qty == Decimal("70.0000")
        assert s.get(Batch, "FG-1") is not None or s.get(Batch, "FG-2") is not None
        assert not (s.get(Batch, "FG-1") is not None and s.get(Batch, "FG-2") is not None)


def test_concurrent_cancel_and_consume_equivalent_to_serial(engine):
    """取消预留与凭预留投料并发：要么取消先（投料以 RESERVATION_CANCELLED
    拒绝、额度释放），要么投料先（取消以 RESERVATION_CONSUMED 拒绝、实存已扣）。"""
    _seed("100")
    with session_scope() as s:
        service.create_reservation(
            s,
            ReservationIn(
                reservation_no="R-1",
                source_batch_no="RM-1",
                unit="kg",
                quantity=Decimal("30"),
                expires_at=_future(),
            ),
        )

    from app.schemas import ReservationCancelIn

    results: dict[str, str] = {}
    barrier = threading.Barrier(2)

    def consume() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_production(
                    s,
                    ProductionIn(
                        output_batch_no="FG-1",
                        unit="kg",
                        output_quantity=Decimal("30"),
                        reservation_no="R-1",
                        inputs=[{"batch_no": "RM-1", "quantity": Decimal("30")}],  # type: ignore[list-item]
                    ),
                )
            results["consume"] = "ok"
        except AppError as exc:
            results["consume"] = exc.code
        except Exception as exc:
            results["consume"] = f"ERROR:{type(exc).__name__}:{exc}"

    def cancel() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.cancel_reservation(
                    s, "R-1", ReservationCancelIn(cancel_reason="计划变更")
                )
            results["cancel"] = "ok"
        except AppError as exc:
            results["cancel"] = exc.code
        except Exception as exc:
            results["cancel"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [consume, cancel]))

    assert not any(str(v).startswith("ERROR") for v in results.values()), results

    with session_scope() as s:
        batch = s.get(Batch, "RM-1")
        reserved, available = service.batch_availability(s, batch)
        fg1 = s.get(Batch, "FG-1")
        if results["cancel"] == "ok":
            assert results["consume"] in ("RESERVATION_CANCELLED", "RESERVATION_EXPIRED"), results
            assert fg1 is None
            assert reserved == 0
            assert batch.remaining_qty == Decimal("100.0000")
            assert available == Decimal("100.0000")
        else:
            assert results["cancel"] == "RESERVATION_CONSUMED", results
            assert results["consume"] == "ok", results
            assert fg1 is not None
            assert batch.remaining_qty == Decimal("70.0000")
            assert reserved == 0
            assert available == Decimal("70.0000")
