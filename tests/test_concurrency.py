"""并发提交测试：多线程同时消耗同一批次，绝不能超用余量。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from sqlalchemy import select

from app import service
from app.db import session_scope
from app.errors import AppError
from app.models import Batch, BatchReview
from app.schemas import (
    ProductionCancellationIn,
    ProductionIn,
    RawBatchIn,
    ReviewIn,
    SuspectIn,
)


def _produce_payload(idx: int, qty: str) -> ProductionIn:
    return ProductionIn(
        output_batch_no=f"FG-{idx}",
        unit="kg",
        output_quantity=Decimal(qty),
        inputs=[{"batch_no": "RM-1", "quantity": Decimal(qty)}],  # type: ignore[list-item]
    )


def test_concurrent_productions_never_overuse(engine):
    # 原料余量 100，6 个线程各消耗 15：6 个全部成功，最终余量恰为 10
    with session_scope() as s:
        service.register_raw_batch(s, RawBatchIn(batch_no="RM-1", unit="kg", quantity="100"))

    results: list[str] = []
    barrier = threading.Barrier(6)

    def worker(idx: int) -> None:
        barrier.wait()  # 尽量让所有线程同时进入事务
        try:
            with session_scope() as s:
                service.submit_production(s, _produce_payload(idx, "15"))
            results.append("ok")
        except AppError as exc:
            results.append(exc.code)
        except Exception as exc:  # 锁错误等也必须暴露，不能吞掉
            results.append(f"ERROR:{type(exc).__name__}:{exc}")

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(worker, range(6)))

    assert results == ["ok"] * 6, results
    with session_scope() as s:
        assert s.get(Batch, "RM-1").remaining_qty == Decimal("10.0000")


def test_concurrent_overallocation_rejects_excess(engine):
    # 余量 20，4 个线程各消耗 10（共 40）：恰好 2 成功、2 拒绝，余量归 0
    with session_scope() as s:
        service.register_raw_batch(s, RawBatchIn(batch_no="RM-1", unit="kg", quantity="20"))

    results: list[str] = []
    barrier = threading.Barrier(4)

    def worker(idx: int) -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_production(s, _produce_payload(idx, "10"))
            results.append("ok")
        except AppError as exc:
            results.append(exc.code)
        except Exception as exc:
            results.append(f"ERROR:{type(exc).__name__}:{exc}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(worker, range(4)))

    assert results.count("ok") == 2, results
    assert results.count("INSUFFICIENT_BALANCE") == 2, results
    assert not [r for r in results if r.startswith("ERROR")], results

    with session_scope() as s:
        assert s.get(Batch, "RM-1").remaining_qty == 0


def _seed_chain() -> None:
    with session_scope() as s:
        service.register_raw_batch(s, RawBatchIn(batch_no="RM-1", unit="kg", quantity="100"))
        service.submit_production(
            s,
            ProductionIn(
                output_batch_no="MIX-1",
                unit="kg",
                output_quantity=Decimal("100"),
                inputs=[{"batch_no": "RM-1", "quantity": Decimal("100")}],  # type: ignore[list-item]
            ),
        )
        service.mark_suspect(s, SuspectIn(batch_no="RM-1"))


def test_review_concurrent_with_production_equivalent_to_serial(engine):
    """复核与投料并发：要么投料先成功（复核后 MIX-1 解除），
    要么复核先执行（隔离中投料被拒绝）；两种结局都等价于某一串行顺序，
    绝不能出现“投料成功且最终状态不一致”的中间态。"""
    _seed_chain()
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def produce_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_production(
                    s,
                    ProductionIn(
                        output_batch_no="FG-1",
                        unit="kg",
                        output_quantity=Decimal("10"),
                        inputs=[{"batch_no": "MIX-1", "quantity": Decimal("10")}],  # type: ignore[list-item]
                    ),
                )
            results["produce"] = "ok"
        except AppError as exc:
            results["produce"] = exc.code
        except Exception as exc:  # 锁错误也必须暴露
            results["produce"] = f"ERROR:{type(exc).__name__}:{exc}"

    def review_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.review_suspect(
                    s, ReviewIn(batch_no="RM-1", review_note="复检合格，误报")
                )
            results["review"] = "ok"
        except AppError as exc:
            results["review"] = exc.code
        except Exception as exc:
            results["review"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [produce_worker, review_worker]))

    assert results["review"] == "ok", results
    assert not any(str(v).startswith("ERROR") for v in results.values()), results
    assert results["produce"] in ("ok", "BATCH_QUARANTINED"), results

    with session_scope() as s:
        rm1 = s.get(Batch, "RM-1")
        mix1 = s.get(Batch, "MIX-1")
        fg1 = s.get(Batch, "FG-1")
        # 无论串行顺序如何，复核后 RM-1 与 MIX-1 都已解除隔离
        assert rm1.is_suspect is False
        assert rm1.is_quarantined is False
        assert mix1.is_quarantined is False
        if results["produce"] == "ok":
            assert fg1 is not None
            assert mix1.remaining_qty == Decimal("90.0000")
        else:
            assert fg1 is None
            assert mix1.remaining_qty == Decimal("100.0000")


def test_flag_concurrent_with_production_equivalent_to_serial(engine):
    """污染标记与投料并发：要么投料先（FG-1 创建成功），要么标记先（被拒绝），
    最终 FG-1 一旦存在，其隔离状态必须与谱系一致。"""
    with session_scope() as s:
        service.register_raw_batch(
            s,
            RawBatchIn(batch_no="RM-1", unit="kg", quantity="100"),
        )
        service.submit_production(
            s,
            ProductionIn(
                output_batch_no="MIX-1",
                unit="kg",
                output_quantity=Decimal("100"),
                inputs=[{"batch_no": "RM-1", "quantity": Decimal("100")}],  # type: ignore[list-item]
            ),
        )

    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def produce_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.submit_production(
                    s,
                    ProductionIn(
                        output_batch_no="FG-1",
                        unit="kg",
                        output_quantity=Decimal("10"),
                        inputs=[{"batch_no": "MIX-1", "quantity": Decimal("10")}],  # type: ignore[list-item]
                    ),
                )
            results["produce"] = "ok"
        except AppError as exc:
            results["produce"] = exc.code
        except Exception as exc:
            results["produce"] = f"ERROR:{type(exc).__name__}:{exc}"

    def flag_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.mark_suspect(s, SuspectIn(batch_no="RM-1"))
            results["flag"] = "ok"
        except Exception as exc:
            results["flag"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [produce_worker, flag_worker]))

    assert results["flag"] == "ok", results
    assert not any(str(v).startswith("ERROR") for v in results.values()), results
    assert results["produce"] in ("ok", "BATCH_QUARANTINED"), results

    with session_scope() as s:
        rm1 = s.get(Batch, "RM-1")
        assert rm1.is_suspect is True and rm1.is_quarantined is True
        fg1 = s.get(Batch, "FG-1")
        if results["produce"] == "ok":
            # 投料先于标记：FG-1 存在，但标记传播后必须补上隔离
            assert fg1.is_quarantined is True
        else:
            assert fg1 is None


def test_flag_and_review_concurrent_equivalent_to_serial(engine):
    """同一批次上“污染标记”与“复核撤销”并发：最终状态必须等价于
    （标记→复核）或（复核→标记）两种串行顺序之一，不能有锁错误。"""
    with session_scope() as s:
        service.register_raw_batch(
            s, RawBatchIn(batch_no="RM-1", unit="kg", quantity="100")
        )

    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def flag_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.mark_suspect(s, SuspectIn(batch_no="RM-1"))
            results["flag"] = "ok"
        except Exception as exc:
            results["flag"] = f"ERROR:{type(exc).__name__}:{exc}"

    def review_worker() -> None:
        barrier.wait()
        try:
            with session_scope() as s:
                service.review_suspect(
                    s, ReviewIn(batch_no="RM-1", review_note="误报")
                )
            results["review"] = "ok"
        except AppError as exc:
            # 复核先于标记拿到锁时，批次还不是疑似，按规则拒绝是合理串行结局
            results["review"] = exc.code
        except Exception as exc:
            results["review"] = f"ERROR:{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [flag_worker, review_worker]))

    assert results["flag"] == "ok", results
    assert not any(str(v).startswith("ERROR") for v in results.values()), results
    assert results["review"] in ("ok", "BATCH_NOT_SUSPECT"), results

    with session_scope() as s:
        rm1 = s.get(Batch, "RM-1")
        # 串行结局 A：标记先、复核后 -> 不疑似、解除隔离
        # 串行结局 B：复核先（拒绝）、标记后 -> 疑似、隔离
        if results["review"] == "ok":
            assert rm1.is_suspect is False and rm1.is_quarantined is False
        else:
            assert rm1.is_suspect is True and rm1.is_quarantined is True


# ------------------------------------------------------------- 撤销并发


def _run_concurrent(fn_a, fn_b):  # type: ignore[no-untyped-def]
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def wrap(name, fn):  # type: ignore[no-untyped-def]
        def worker() -> None:
            barrier.wait()
            try:
                fn()
                results[name] = "ok"
            except AppError as exc:
                results[name] = exc.code
            except Exception as exc:  # 锁错误等也必须暴露
                results[name] = f"ERROR:{type(exc).__name__}:{exc}"

        return worker

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda f: f(), [wrap("a", fn_a), wrap("b", fn_b)]))
    return results


def test_cancel_concurrent_with_downstream_production_equivalent_to_serial(engine):
    """撤销 MIX-1 与“把 MIX-1 投入 FG-1”并发：
    结局 A 投料先 -> 撤销以 OUTPUT_ALREADY_CONSUMED 拒绝，余量不变，FG-1 存在；
    结局 B 撤销先 -> 投料以 BATCH_CANCELLED 拒绝，来源已返还，FG-1 不存在。
    两种结局都等价于某一串行顺序，不能既撤销又投料。"""
    with session_scope() as s:
        service.register_raw_batch(
            s, RawBatchIn(batch_no="RM-1", unit="kg", quantity="100")
        )
        service.submit_production(
            s,
            ProductionIn(
                output_batch_no="MIX-1",
                unit="kg",
                output_quantity=Decimal("40"),
                inputs=[{"batch_no": "RM-1", "quantity": Decimal("40")}],  # type: ignore[list-item]
            ),
        )

    def cancel() -> None:
        with session_scope() as s:
            service.cancel_production(
                s, "MIX-1", ProductionCancellationIn(cancel_reason="录入错误")
            )

    def consume() -> None:
        with session_scope() as s:
            service.submit_production(
                s,
                ProductionIn(
                    output_batch_no="FG-1",
                    unit="kg",
                    output_quantity=Decimal("10"),
                    inputs=[{"batch_no": "MIX-1", "quantity": Decimal("10")}],  # type: ignore[list-item]
                ),
            )

    results = _run_concurrent(consume, cancel)
    assert not any(str(v).startswith("ERROR") for v in results.values()), results

    with session_scope() as s:
        rm1 = s.get(Batch, "RM-1")
        mix1 = s.get(Batch, "MIX-1")
        fg1 = s.get(Batch, "FG-1")
        cancelled = service.cancelled_output_nos(s, ["MIX-1"]) == {"MIX-1"}

        if results["b"] == "ok":  # 撤销成功 -> 投料必被拒
            assert results["a"] == "BATCH_CANCELLED", results
            assert cancelled is True
            assert fg1 is None
            assert rm1.remaining_qty == Decimal("100.0000")  # 40 已返还
            assert mix1.remaining_qty == 0
        else:  # 投料成功 -> 撤销必因“已被后续投料”拒绝
            assert results["b"] == "OUTPUT_ALREADY_CONSUMED", results
            assert results["a"] == "ok", results
            assert cancelled is False
            assert fg1 is not None
            assert rm1.remaining_qty == Decimal("60.0000")  # 未返还
            assert mix1.remaining_qty == Decimal("30.0000")  # 40-10


def test_cancel_concurrent_with_flag_of_output_equivalent_to_serial(engine):
    """撤销 MIX-1 与“直接标记 MIX-1 疑似”并发：
    结局 A 撤销先 -> 标记仍成功（疑似标志独立于撤销边），撤销已生效；
    结局 B 标记先 -> 撤销以 OUTPUT_SUSPECT 拒绝，库存不变。
    任一结局都不得出现“撤销返还 + 误放行”。"""
    with session_scope() as s:
        service.register_raw_batch(
            s, RawBatchIn(batch_no="RM-1", unit="kg", quantity="100")
        )
        service.submit_production(
            s,
            ProductionIn(
                output_batch_no="MIX-1",
                unit="kg",
                output_quantity=Decimal("40"),
                inputs=[{"batch_no": "RM-1", "quantity": Decimal("40")}],  # type: ignore[list-item]
            ),
        )

    def cancel() -> None:
        with session_scope() as s:
            service.cancel_production(
                s, "MIX-1", ProductionCancellationIn(cancel_reason="录入错误")
            )

    def flag() -> None:
        with session_scope() as s:
            service.mark_suspect(s, SuspectIn(batch_no="MIX-1"))

    results = _run_concurrent(cancel, flag)
    assert results["b"] == "ok", results  # 标记总是成功
    assert not any(str(v).startswith("ERROR") for v in results.values()), results
    assert results["a"] in ("ok", "OUTPUT_SUSPECT"), results

    with session_scope() as s:
        rm1 = s.get(Batch, "RM-1")
        mix1 = s.get(Batch, "MIX-1")
        cancelled = service.cancelled_output_nos(s, ["MIX-1"]) == {"MIX-1"}
        if results["a"] == "ok":  # 撤销先：返还完成；标记后隔离，但不再进入谱系传播
            assert cancelled is True
            assert rm1.remaining_qty == Decimal("100.0000")
            assert mix1.is_suspect is True and mix1.is_quarantined is True
        else:  # 标记先：撤销拒绝，库存不变
            assert cancelled is False
            assert rm1.remaining_qty == Decimal("60.0000")
            assert mix1.is_suspect is True


def test_cancel_concurrent_with_flag_of_source_equivalent_to_serial(engine):
    """撤销 MIX-1 与“标记来源 RM-1 疑似”并发：来源是否隔离都允许返还，
    两种串行顺序下撤销都应成功，且撤销边不传播到作废产出之外；
    若标记先，MIX-1 仅因祖先疑似被隔离（自身非疑似），撤销仍成立并解除其隔离。"""
    with session_scope() as s:
        service.register_raw_batch(
            s, RawBatchIn(batch_no="RM-1", unit="kg", quantity="100")
        )
        service.submit_production(
            s,
            ProductionIn(
                output_batch_no="MIX-1",
                unit="kg",
                output_quantity=Decimal("40"),
                inputs=[{"batch_no": "RM-1", "quantity": Decimal("40")}],  # type: ignore[list-item]
            ),
        )

    def cancel() -> None:
        with session_scope() as s:
            service.cancel_production(
                s, "MIX-1", ProductionCancellationIn(cancel_reason="录入错误")
            )

    def flag() -> None:
        with session_scope() as s:
            service.mark_suspect(s, SuspectIn(batch_no="RM-1"))

    results = _run_concurrent(cancel, flag)
    assert results == {"a": "ok", "b": "ok"}, results

    with session_scope() as s:
        rm1 = s.get(Batch, "RM-1")
        mix1 = s.get(Batch, "MIX-1")
        # 无论顺序：来源保持疑似/隔离；撤销已返还，作废产出不再被隔离
        assert rm1.is_suspect is True and rm1.is_quarantined is True
        assert rm1.remaining_qty == Decimal("100.0000")
        assert mix1.is_quarantined is False
        assert service.cancelled_output_nos(s, ["MIX-1"]) == {"MIX-1"}


def test_cancel_concurrent_with_review_of_output_equivalent_to_serial(engine):
    """撤销 MIX-1 与“复核 MIX-1（撤销其疑似标记）”并发：
    复核成功必留下复核记录；若复核先提交，撤销随后须以 OUTPUT_HAS_REVIEW 拒绝；
    若撤销先提交，复核以 BATCH_NOT_SUSPECT 拒绝（产出从未疑似）。
    不能出现撤销返还与复核记录同时成立。"""
    with session_scope() as s:
        service.register_raw_batch(
            s, RawBatchIn(batch_no="RM-1", unit="kg", quantity="100")
        )
        service.submit_production(
            s,
            ProductionIn(
                output_batch_no="MIX-1",
                unit="kg",
                output_quantity=Decimal("40"),
                inputs=[{"batch_no": "RM-1", "quantity": Decimal("40")}],  # type: ignore[list-item]
            ),
        )
        service.mark_suspect(s, SuspectIn(batch_no="MIX-1"))

    def cancel() -> None:
        with session_scope() as s:
            service.cancel_production(
                s, "MIX-1", ProductionCancellationIn(cancel_reason="录入错误")
            )

    def review() -> None:
        with session_scope() as s:
            service.review_suspect(
                s, ReviewIn(batch_no="MIX-1", review_note="复检合格")
            )

    results = _run_concurrent(cancel, review)
    assert not any(str(v).startswith("ERROR") for v in results.values()), results

    with session_scope() as s:
        rm1 = s.get(Batch, "RM-1")
        cancelled = service.cancelled_output_nos(s, ["MIX-1"]) == {"MIX-1"}
        reviewed = (
            s.execute(select(BatchReview).where(BatchReview.batch_no == "MIX-1"))
            .scalar_one_or_none()
            is not None
        )
        # 互斥结局：撤销成功则无复核记录；复核成功则撤销必被拒
        if results["a"] == "ok":
            assert results["b"] == "BATCH_NOT_SUSPECT", results
            assert cancelled is True and reviewed is False
            assert rm1.remaining_qty == Decimal("100.0000")
        else:
            assert results["a"] in ("OUTPUT_SUSPECT", "OUTPUT_HAS_REVIEW"), results
            assert results["b"] == "ok", results
            assert cancelled is False and reviewed is True
            assert rm1.remaining_qty == Decimal("60.0000")
