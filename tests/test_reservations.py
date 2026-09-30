"""原料预留端到端测试：预留、可用量、核销、取消、过期、隔离、撤销恢复、幂等。"""
from __future__ import annotations

import datetime
from datetime import timezone

from sqlalchemy import update

from app.db import session_scope
from app.models import MaterialReservation


def _iso(hours: float) -> str:
    return (
        datetime.datetime.now(timezone.utc) + datetime.timedelta(hours=hours)
    ).isoformat()


def _raw(client, batch_no="RM-1", qty="100", unit="kg", key=None):
    return client.post(
        "/raw-batches",
        json={"batch_no": batch_no, "unit": unit, "quantity": qty, "note": None},
        headers={"Idempotency-Key": key or f"raw-{batch_no}"},
    )


def _reserve(client, no, source="RM-1", qty="10", unit="kg", expires=None, key=None):
    return client.post(
        "/reservations",
        json={
            "reservation_no": no,
            "source_batch_no": source,
            "unit": unit,
            "quantity": qty,
            "expires_at": expires or _iso(1),
        },
        headers={"Idempotency-Key": key or f"rsv-{no}"},
    )


def _produce(
    client,
    out_no,
    inputs,
    out_qty=None,
    unit="kg",
    key=None,
    reservation_no=None,
):
    from decimal import Decimal

    total = sum((Decimal(str(q)) for _, q in inputs), Decimal(0))
    body = {
        "output_batch_no": out_no,
        "unit": unit,
        "output_quantity": str(out_qty if out_qty is not None else total),
        "inputs": [{"batch_no": b, "quantity": str(q)} for b, q in inputs],
    }
    if reservation_no is not None:
        body["reservation_no"] = reservation_no
    return client.post(
        "/productions", json=body, headers={"Idempotency-Key": key or f"prod-{out_no}"}
    )


def _set_expired(no: str) -> None:
    with session_scope() as s:
        s.execute(
            update(MaterialReservation)
            .where(MaterialReservation.reservation_no == no)
            .values(
                expires_at=datetime.datetime.now(timezone.utc)
                - datetime.timedelta(seconds=5)
            )
        )


# ------------------------------------------------------------- 预留与可用量


def test_reservation_does_not_change_remaining_but_holds_available(client):
    assert _raw(client).status_code == 201
    r = _reserve(client, "RSV-1", qty="30")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "active"
    assert body["source_remaining_qty"] == 100.0
    assert body["source_reserved_qty"] == 30.0
    assert body["source_available_qty"] == 70.0

    batch = client.get("/batches/RM-1").json()
    # 实存不变，可用量减少
    assert batch["remaining_qty"] == 100.0
    assert batch["reserved_qty"] == 30.0
    assert batch["available_qty"] == 70.0


def test_multiple_reservations_accumulate_on_available(client):
    _raw(client)
    assert _reserve(client, "R1", qty="20").status_code == 201
    r = _reserve(client, "R2", qty="30.5")
    assert r.status_code == 201
    batch = client.get("/batches/RM-1").json()
    assert batch["reserved_qty"] == 50.5
    assert batch["available_qty"] == 49.5


def test_reservation_over_available_entirely_rejected(client):
    _raw(client, qty="10")
    r = _reserve(client, "R1", qty="11")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "INSUFFICIENT_BALANCE"
    # 无部分预留：额度未被占用
    batch = client.get("/batches/RM-1").json()
    assert batch["reserved_qty"] == 0.0
    assert batch["available_qty"] == 10.0
    assert client.get("/reservations/R1").status_code == 404


def test_reservation_cannot_overcommit_when_other_active_holds(client):
    _raw(client, qty="10")
    assert _reserve(client, "R1", qty="6").status_code == 201
    r = _reserve(client, "R2", qty="5")  # 仅剩可用 4
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "INSUFFICIENT_BALANCE"
    assert client.get("/batches/RM-1").json()["available_qty"] == 4.0


def test_normal_production_can_only_consume_available(client):
    _raw(client, qty="100")
    _reserve(client, "R1", qty="30")
    # 实存 100 但可用仅 70：请求 80 被拒
    r = _produce(client, "FG-X", [("RM-1", "80")], key="p-bad")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "INSUFFICIENT_BALANCE"
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 100.0
    assert client.get("/batches/FG-X").status_code == 404
    # 恰好用满可用量 70：成功，预留仍占 30
    assert _produce(client, "FG-1", [("RM-1", "70")], key="p-ok").status_code == 201
    batch = client.get("/batches/RM-1").json()
    assert batch["remaining_qty"] == 30.0
    assert batch["reserved_qty"] == 30.0
    assert batch["available_qty"] == 0.0


def test_duplicate_reservation_no_rejected(client):
    _raw(client)
    assert _reserve(client, "R1", qty="1").status_code == 201
    r = _reserve(client, "R1", qty="1", key="other-key")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RESERVATION_NO_REUSED"


def test_reservation_unknown_source_rejected(client):
    r = _reserve(client, "R1", source="GHOST", qty="1")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SOURCE_NOT_FOUND"


def test_reservation_unit_mismatch_rejected(client):
    _raw(client, unit="kg")
    r = _reserve(client, "R1", qty="1", unit="L")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "UNIT_MISMATCH"


def test_reservation_deadline_must_be_future(client):
    _raw(client)
    r = _reserve(client, "R1", qty="1", expires=_iso(-0.01))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "RESERVATION_DEADLINE_NOT_FUTURE"
    assert client.get("/reservations/R1").status_code == 404


def test_reservation_requires_positive_quantity(client):
    _raw(client)
    r = _reserve(client, "R1", qty="0")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_reservation_requires_idempotency_key(client):
    _raw(client)
    r = client.post(
        "/reservations",
        json={
            "reservation_no": "R1",
            "source_batch_no": "RM-1",
            "unit": "kg",
            "quantity": "1",
            "expires_at": _iso(1),
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


# ------------------------------------------------------------- 预留核销


def test_reserved_production_consumes_in_one_transaction(client):
    _raw(client, qty="100")
    _reserve(client, "R1", qty="30")
    r = _produce(
        client,
        "FG-1",
        [("RM-1", "30")],
        key="p1",
        reservation_no="R1",
    )
    assert r.status_code == 201, r.text
    # 实存被扣减
    batch = client.get("/batches/RM-1").json()
    assert batch["remaining_qty"] == 70.0
    assert batch["reserved_qty"] == 0.0
    assert batch["available_qty"] == 70.0
    # 预留已核销并回填产出
    rsv = client.get("/reservations/R1").json()
    assert rsv["status"] == "consumed"
    assert rsv["consumed_by_output_no"] == "FG-1"


def test_reserved_production_mismatch_batch_rejected(client):
    _raw(client, "RM-1", "100")
    _raw(client, "RM-2", "100")
    _reserve(client, "R1", source="RM-1", qty="10")
    r = _produce(
        client, "FG-1", [("RM-2", "10")], key="p1", reservation_no="R1"
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "RESERVATION_MISMATCH"
    assert client.get("/batches/FG-1").status_code == 404
    assert client.get("/reservations/R1").json()["status"] == "active"
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 100.0


def test_reserved_production_mismatch_qty_rejected(client):
    _raw(client)
    _reserve(client, "R1", qty="30")
    r = _produce(
        client, "FG-1", [("RM-1", "29")], key="p1", reservation_no="R1"
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "RESERVATION_MISMATCH"
    assert client.get("/batches/FG-1").status_code == 404
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 100.0


def test_reserved_production_mismatch_unit_rejected(client):
    _raw(client)
    _reserve(client, "R1", qty="30", unit="kg")
    r = _produce(
        client, "FG-1", [("RM-1", "30")], unit="L", key="p1", reservation_no="R1"
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "UNIT_MISMATCH"


def test_reserved_production_must_have_single_input(client):
    _raw(client, "RM-1", "100")
    _raw(client, "RM-2", "100")
    _reserve(client, "R1", source="RM-1", qty="10")
    r = _produce(
        client,
        "FG-1",
        [("RM-1", "10"), ("RM-2", "1")],
        key="p1",
        reservation_no="R1",
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "RESERVATION_MISMATCH"
    assert client.get("/batches/FG-1").status_code == 404


def test_consume_unknown_reservation_rejected(client):
    _raw(client)
    r = _produce(
        client, "FG-1", [("RM-1", "1")], key="p1", reservation_no="GHOST"
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RESERVATION_NOT_FOUND"
    assert client.get("/batches/FG-1").status_code == 404


def test_consume_cancelled_reservation_rejected(client):
    _raw(client)
    _reserve(client, "R1", qty="10")
    assert client.post(
        "/reservations/R1/cancellation",
        json={"cancel_reason": "计划变更"},
        headers={"Idempotency-Key": "cx1"},
    ).status_code == 200
    r = _produce(client, "FG-1", [("RM-1", "10")], key="p1", reservation_no="R1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RESERVATION_CANCELLED"
    assert client.get("/batches/FG-1").status_code == 404


def test_consume_consumed_reservation_rejected(client):
    _raw(client)
    _reserve(client, "R1", qty="10")
    assert _produce(
        client, "FG-1", [("RM-1", "10")], key="p1", reservation_no="R1"
    ).status_code == 201
    r = _produce(
        client, "FG-2", [("RM-1", "10")], key="p2", reservation_no="R1"
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RESERVATION_CONSUMED"
    assert client.get("/batches/FG-2").status_code == 404


def test_consume_expired_reservation_rejected_without_output(client):
    _raw(client)
    _reserve(client, "R1", qty="10")
    _set_expired("R1")
    assert client.get("/reservations/R1").json()["status"] == "expired"
    r = _produce(client, "FG-1", [("RM-1", "10")], key="p1", reservation_no="R1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RESERVATION_EXPIRED"
    assert client.get("/batches/FG-1").status_code == 404
    # 过期已释放：可用量恢复
    assert client.get("/batches/RM-1").json()["available_qty"] == 100.0


def test_quarantine_blocks_consume_but_holds_quota(client):
    _raw(client, qty="100")
    _reserve(client, "R1", qty="20")
    # 标记来源疑似：预留仍占额度
    r = client.post(
        "/contamination",
        json={"batch_no": "RM-1", "note": None},
        headers={"Idempotency-Key": "q1"},
    )
    assert r.status_code == 200
    batch = client.get("/batches/RM-1").json()
    assert batch["reserved_qty"] == 20.0
    assert batch["available_qty"] == 80.0
    # 隔离期间不得核销
    r = _produce(client, "FG-1", [("RM-1", "20")], key="p1", reservation_no="R1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_QUARANTINED"
    assert client.get("/batches/FG-1").status_code == 404
    # 复核放行后，未到期预留可核销
    r = client.post(
        "/reviews",
        json={"batch_no": "RM-1", "review_note": "复检合格"},
        headers={"Idempotency-Key": "rv1"},
    )
    assert r.status_code == 200
    r = _produce(client, "FG-1", [("RM-1", "20")], key="p2", reservation_no="R1")
    assert r.status_code == 201, r.text
    assert client.get("/reservations/R1").json()["status"] == "consumed"


def test_expired_reservation_cannot_be_consumed_after_release(client):
    _raw(client, qty="100")
    _reserve(client, "R1", qty="60")
    _set_expired("R1")
    # 释放后普通生产可使用全部实存
    r = _produce(client, "FG-1", [("RM-1", "90")], key="p1")
    assert r.status_code == 201


# ------------------------------------------------------------- 预留取消


def test_cancel_reservation_releases_quota(client):
    _raw(client, qty="100")
    _reserve(client, "R1", qty="40")
    r = client.post(
        "/reservations/R1/cancellation",
        json={"cancel_reason": "计划变更，不再预留"},
        headers={"Idempotency-Key": "cx1"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "cancelled"
    assert body["cancel_reason"] == "计划变更，不再预留"
    assert body["cancelled_at"] is not None
    assert body["source_available_qty"] == 100.0
    assert body["source_reserved_qty"] == 0.0
    assert client.get("/batches/RM-1").json()["available_qty"] == 100.0


def test_cancel_unknown_reservation_404(client):
    r = client.post(
        "/reservations/NOPE/cancellation",
        json={"cancel_reason": "x"},
        headers={"Idempotency-Key": "cx1"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RESERVATION_NOT_FOUND"


def test_cancel_consumed_reservation_rejected(client):
    _raw(client)
    _reserve(client, "R1", qty="10")
    _produce(client, "FG-1", [("RM-1", "10")], key="p1", reservation_no="R1")
    r = client.post(
        "/reservations/R1/cancellation",
        json={"cancel_reason": "x"},
        headers={"Idempotency-Key": "cx1"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RESERVATION_CONSUMED"


def test_cancel_expired_reservation_rejected(client):
    _raw(client)
    _reserve(client, "R1", qty="10")
    _set_expired("R1")
    r = client.post(
        "/reservations/R1/cancellation",
        json={"cancel_reason": "x"},
        headers={"Idempotency-Key": "cx1"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RESERVATION_EXPIRED"


def test_cancel_blank_reason_validation(client):
    _raw(client)
    _reserve(client, "R1", qty="10")
    r = client.post(
        "/reservations/R1/cancellation",
        json={"cancel_reason": "   "},
        headers={"Idempotency-Key": "cx1"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


# ------------------------------------------------------------- 查询


def test_get_unknown_reservation_404(client):
    r = client.get("/reservations/NOPE")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RESERVATION_NOT_FOUND"


def test_list_reservations_filter_by_source(client):
    _raw(client, "RM-1", "100")
    _raw(client, "RM-2", "100")
    _reserve(client, "R1", source="RM-1", qty="5")
    _reserve(client, "R2", source="RM-2", qty="7")
    all_items = client.get("/reservations").json()["items"]
    assert {i["reservation_no"] for i in all_items} == {"R1", "R2"}
    filtered = client.get("/reservations", params={"source_batch_no": "RM-2"}).json()
    assert filtered["source_batch_no"] == "RM-2"
    assert [i["reservation_no"] for i in filtered["items"]] == ["R2"]
    assert filtered["items"][0]["status"] == "active"


def test_query_reservation_status_lifecycle(client):
    _raw(client)
    _reserve(client, "R1", qty="10")
    assert client.get("/reservations/R1").json()["status"] == "active"
    client.post(
        "/reservations/R1/cancellation",
        json={"cancel_reason": "x"},
        headers={"Idempotency-Key": "cx1"},
    )
    assert client.get("/reservations/R1").json()["status"] == "cancelled"


# ------------------------------------------------------------- 撤销生产单恢复预留


def test_cancel_production_restores_consumed_reservation(client):
    _raw(client, qty="100")
    _reserve(client, "R1", qty="40")
    assert _produce(
        client, "FG-1", [("RM-1", "40")], out_qty=40, key="p1", reservation_no="R1"
    ).status_code == 201
    assert client.get("/reservations/R1").json()["status"] == "consumed"

    r = client.post(
        "/productions/FG-1/cancellation",
        json={"cancel_reason": "录入错误，整单作废"},
        headers={"Idempotency-Key": "cx1"},
    )
    assert r.status_code == 200, r.text
    # 同事务返还实投、恢复预留
    batch = client.get("/batches/RM-1").json()
    assert batch["remaining_qty"] == 100.0
    assert batch["reserved_qty"] == 40.0
    assert batch["available_qty"] == 60.0
    rsv = client.get("/reservations/R1").json()
    assert rsv["status"] == "active"
    assert rsv["consumed_by_output_no"] is None
    # 恢复后可再次凭同一预留单号投料
    r = _produce(
        client, "FG-2", [("RM-1", "40")], key="p2", reservation_no="R1"
    )
    assert r.status_code == 201
    assert client.get("/reservations/R1").json()["consumed_by_output_no"] == "FG-2"


def test_cancel_production_expired_restored_reservation_shows_expired(client):
    _raw(client, qty="100")
    _reserve(client, "R1", qty="40")
    _produce(client, "FG-1", [("RM-1", "40")], out_qty=40, key="p1", reservation_no="R1")
    _set_expired("R1")
    # 撤销生产单始终允许返还（隔离/到期都不阻止错误单据回退）
    r = client.post(
        "/productions/FG-1/cancellation",
        json={"cancel_reason": "录入错误"},
        headers={"Idempotency-Key": "cx1"},
    )
    assert r.status_code == 200
    rsv = client.get("/reservations/R1").json()
    # 落库恢复 active，但已到期 -> 动态呈现 expired、不再占用额度
    assert rsv["status"] == "expired"
    batch = client.get("/batches/RM-1").json()
    assert batch["remaining_qty"] == 100.0
    assert batch["reserved_qty"] == 0.0
    assert batch["available_qty"] == 100.0


# ------------------------------------------------------------- 幂等


def test_reservation_idempotent_replay(client):
    _raw(client)
    body = {
        "reservation_no": "R1",
        "source_batch_no": "RM-1",
        "unit": "kg",
        "quantity": "10",
        "expires_at": _iso(1),
    }
    r1 = client.post(
        "/reservations", json=body, headers={"Idempotency-Key": "same-key"}
    )
    r2 = client.post(
        "/reservations", json=body, headers={"Idempotency-Key": "same-key"}
    )
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json() == r2.json()
    assert client.get("/batches/RM-1").json()["reserved_qty"] == 10.0


def test_reservation_idempotency_conflict(client):
    _raw(client)
    hdr = {"Idempotency-Key": "same-key"}
    r1 = client.post(
        "/reservations",
        json={
            "reservation_no": "R1",
            "source_batch_no": "RM-1",
            "unit": "kg",
            "quantity": "10",
            "expires_at": _iso(1),
        },
        headers=hdr,
    )
    assert r1.status_code == 201
    r2 = client.post(
        "/reservations",
        json={
            "reservation_no": "R2",
            "source_batch_no": "RM-1",
            "unit": "kg",
            "quantity": "20",
            "expires_at": _iso(2),
        },
        headers=hdr,
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    # 异体冲突未留副作用
    assert client.get("/batches/RM-1").json()["reserved_qty"] == 10.0


def test_cancel_reservation_idempotent_replay(client):
    _raw(client)
    _reserve(client, "R1", qty="10")
    body = {"cancel_reason": "计划变更"}
    hdr = {"Idempotency-Key": "cx-key"}
    r1 = client.post("/reservations/R1/cancellation", json=body, headers=hdr)
    r2 = client.post("/reservations/R1/cancellation", json=body, headers=hdr)
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json()


def test_reserved_production_idempotent_replay_consumes_once(client):
    _raw(client, qty="100")
    _reserve(client, "R1", qty="10")
    body = {
        "output_batch_no": "FG-1",
        "unit": "kg",
        "output_quantity": "10",
        "reservation_no": "R1",
        "inputs": [{"batch_no": "RM-1", "quantity": "10"}],
    }
    hdr = {"Idempotency-Key": "prod-key"}
    r1 = client.post("/productions", json=body, headers=hdr)
    r2 = client.post("/productions", json=body, headers=hdr)
    assert r1.status_code == r2.status_code == 201
    assert r1.json() == r2.json()
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 90.0
    assert client.get("/reservations/R1").json()["status"] == "consumed"


# ------------------------------------------------------------- 兼容：既有响应字段保留


def test_batch_response_keeps_existing_fields(client):
    _raw(client)
    body = client.get("/batches/RM-1").json()
    for field in (
        "batch_no",
        "unit",
        "initial_qty",
        "remaining_qty",
        "is_suspect",
        "is_quarantined",
        "is_cancelled",
        "note",
    ):
        assert field in body


def test_production_without_reservation_unchanged_shape(client):
    _raw(client)
    r = _produce(client, "FG-1", [("RM-1", "10")])
    assert r.status_code == 201
    assert set(r.json().keys()) == {
        "output_batch_no",
        "unit",
        "output_quantity",
        "inputs",
    }
