"""原料预留：登记、可用量、按预留核销、取消、到期释放、隔离交互、整笔拒绝、幂等。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import service


def _raw(client, batch_no, qty, unit="kg", key=None):
    return client.post(
        "/raw-batches",
        json={"batch_no": batch_no, "unit": unit, "quantity": str(qty)},
        headers={"Idempotency-Key": key or f"key-{batch_no}"},
    )


def _iso(hours=1):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _reserve(
    client,
    no,
    source,
    qty,
    *,
    unit="kg",
    expires_at=None,
    key=None,
):
    return client.post(
        "/reservations",
        json={
            "reservation_no": no,
            "source_batch_no": source,
            "unit": unit,
            "quantity": str(qty),
            "expires_at": expires_at or _iso(1),
        },
        headers={"Idempotency-Key": key or f"key-{no}"},
    )


def _produce(client, out_no, inputs, out_qty=None, unit="kg", key=None, reservation_no=None):
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
        "/productions",
        json=body,
        headers={"Idempotency-Key": key or f"key-{out_no}"},
    )


def _cancel_reservation(client, no, key, reason=None):
    body = {"reason": reason} if reason is not None else None
    return client.post(
        f"/reservations/{no}/cancellation", json=body, headers={"Idempotency-Key": key}
    )


# ------------------------------------------------------------- 登记与可用量


def test_create_reservation_does_not_change_remaining(client):
    _raw(client, "RM-1", 100)
    r = _reserve(client, "RSV-1", "RM-1", 30)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["reservation_no"] == "RSV-1"
    assert body["source_batch_no"] == "RM-1"
    assert body["quantity"] == 30.0
    assert body["status"] == "active"
    # 预留不改变实存余量，只扣减可用量
    assert body["source"]["remaining_qty"] == 100.0
    assert body["source"]["reserved_qty"] == 30.0
    assert body["source"]["available_qty"] == 70.0
    d = client.get("/batches/RM-1").json()
    assert d["remaining_qty"] == 100.0
    assert d["reserved_qty"] == 30.0
    assert d["available_qty"] == 70.0


def test_reservation_cumulative_reserved_totals(client):
    _raw(client, "RM-1", 100)
    assert _reserve(client, "R1", "RM-1", 30, key="1").status_code == 201
    assert _reserve(client, "R2", "RM-1", 25, key="2").status_code == 201
    d = client.get("/batches/RM-1").json()
    assert d["remaining_qty"] == 100.0
    assert d["reserved_qty"] == 55.0
    assert d["available_qty"] == 45.0
    # 恰好用尽可用量：允许
    assert _reserve(client, "R3", "RM-1", 45, key="3").status_code == 201
    d = client.get("/batches/RM-1").json()
    assert d["available_qty"] == 0.0
    # 再多 1 即拒绝，无部分预留
    r = _reserve(client, "R4", "RM-1", 0.0001, key="4")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "INSUFFICIENT_AVAILABLE"
    assert client.get("/batches/RM-1").json()["reserved_qty"] == 100.0


def test_duplicate_reservation_no_rejected(client):
    _raw(client, "RM-1", 100)
    assert _reserve(client, "R1", "RM-1", 10, key="a").status_code == 201
    r = _reserve(client, "R1", "RM-1", 5, key="b")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RESERVATION_NO_REUSED"
    # 第二笔未生效
    assert client.get("/batches/RM-1").json()["reserved_qty"] == 10.0


def test_reservation_unknown_source_rejected(client):
    r = _reserve(client, "R1", "GHOST", 10)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SOURCE_NOT_FOUND"


def test_reservation_unit_mismatch_rejected(client):
    _raw(client, "RM-1", 100, unit="kg")
    r = _reserve(client, "R1", "RM-1", 10, unit="L", key="a")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "UNIT_MISMATCH"
    assert client.get("/batches/RM-1").json()["reserved_qty"] == 0.0


def test_reservation_deadline_must_be_future(client):
    _raw(client, "RM-1", 100)
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    r = _reserve(client, "R1", "RM-1", 10, expires_at=past, key="a")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "DEADLINE_NOT_FUTURE"
    now = datetime.now(timezone.utc).isoformat()
    r = _reserve(client, "R1", "RM-1", 10, expires_at=now, key="b")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "DEADLINE_NOT_FUTURE"


def test_reservation_non_positive_qty_rejected(client):
    _raw(client, "RM-1", 100)
    r = client.post(
        "/reservations",
        json={
            "reservation_no": "R1",
            "source_batch_no": "RM-1",
            "unit": "kg",
            "quantity": "0",
            "expires_at": _iso(1),
        },
        headers={"Idempotency-Key": "a"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_reservation_naive_deadline_interpreted_as_utc(client):
    _raw(client, "RM-1", 100)
    naive = (datetime.utcnow() + timedelta(hours=1)).isoformat()
    r = _reserve(client, "R1", "RM-1", 10, expires_at=naive, key="a")
    assert r.status_code == 201, r.text


def test_reservation_requires_idempotency_key(client):
    _raw(client, "RM-1", 100)
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


# ------------------------------------------------------------- 普通生产受可用量约束


def test_normal_production_consumes_only_available(client):
    _raw(client, "RM-1", 100)
    _reserve(client, "R1", "RM-1", 30, key="r")
    # 实存 100、可用 70：普通生产请求 71 被拒绝，不动余量/预留
    r = _produce(client, "FG-1", [("RM-1", 71)], key="p1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "INSUFFICIENT_BALANCE"
    d = client.get("/batches/RM-1").json()
    assert d["remaining_qty"] == 100.0
    assert d["available_qty"] == 70.0
    assert client.get("/batches/FG-1").status_code == 404
    # 70 可用，可以投料
    assert _produce(client, "FG-1", [("RM-1", 70)], key="p2").status_code == 201
    d = client.get("/batches/RM-1").json()
    assert d["remaining_qty"] == 30.0
    assert d["reserved_qty"] == 30.0
    assert d["available_qty"] == 0.0


# ------------------------------------------------------------- 按预留投料核销


def test_consume_reservation_exact_match(client):
    _raw(client, "RM-1", 100)
    _reserve(client, "R1", "RM-1", 30, key="r")
    r = _produce(
        client,
        "FG-1",
        [("RM-1", 30)],
        key="p",
        reservation_no="R1",
    )
    assert r.status_code == 201, r.text
    # 同事务扣减实存、核销预留
    d = client.get("/batches/RM-1").json()
    assert d["remaining_qty"] == 70.0
    assert d["reserved_qty"] == 0.0
    assert d["available_qty"] == 70.0
    g = client.get("/reservations/R1").json()
    assert g["status"] == "consumed"
    assert g["consumed_by_output_no"] == "FG-1"
    assert g["consumed_at"] is not None


def test_consume_reservation_mismatch_entirely_rejected(client):
    _raw(client, "RM-1", 100)
    _raw(client, "RM-2", 100)
    _reserve(client, "R1", "RM-1", 30, key="r")

    # 数量不一致
    r = _produce(client, "FG-1", [("RM-1", 20)], key="p1", reservation_no="R1")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "RESERVATION_MISMATCH"
    # 批次不一致
    r = _produce(client, "FG-1", [("RM-2", 30)], key="p2", reservation_no="R1")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "RESERVATION_MISMATCH"
    # 单位不一致
    r = _produce(client, "FG-1", [("RM-1", 30)], unit="L", key="p3", reservation_no="R1")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "UNIT_MISMATCH"
    # 多笔投料
    r = _produce(
        client,
        "FG-1",
        [("RM-1", 30), ("RM-2", 1)],
        out_qty=31,
        key="p4",
        reservation_no="R1",
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "RESERVATION_MISMATCH"
    # 全部整笔拒绝：产出未创建，余量与预留不变
    assert client.get("/batches/FG-1").status_code == 404
    d = client.get("/batches/RM-1").json()
    assert d["remaining_qty"] == 100.0 and d["reserved_qty"] == 30.0
    assert client.get("/reservations/R1").json()["status"] == "active"


def test_consume_unknown_reservation_rejected(client):
    _raw(client, "RM-1", 100)
    r = _produce(client, "FG-1", [("RM-1", 10)], key="p", reservation_no="NOPE")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RESERVATION_NOT_FOUND"
    assert client.get("/batches/FG-1").status_code == 404


@pytest.mark.parametrize("terminal_state", ["cancelled", "consumed", "expired"])
def test_consume_non_active_reservation_rejected(client, terminal_state, monkeypatch):
    _raw(client, "RM-1", 100)
    _reserve(client, "R1", "RM-1", 10, key="r")
    if terminal_state == "cancelled":
        assert _cancel_reservation(client, "R1", "c").status_code == 200
    elif terminal_state == "consumed":
        assert _produce(client, "FG-0", [("RM-1", 10)], key="pc", reservation_no="R1").status_code == 201
    else:
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        monkeypatch.setattr(service, "utcnow", lambda: future)
    r = _produce(client, "FG-1", [("RM-1", 10)], key="p", reservation_no="R1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RESERVATION_NOT_ACTIVE"
    if terminal_state != "consumed":
        assert client.get("/batches/FG-1").status_code == 404


# ------------------------------------------------------------- 取消预留


def test_cancel_reservation_releases_availability(client):
    _raw(client, "RM-1", 100)
    _reserve(client, "R1", "RM-1", 30, key="r")
    r = _cancel_reservation(client, "R1", "c", reason="计划取消")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "cancelled"
    assert body["cancel_reason"] == "计划取消"
    assert body["cancelled_at"] is not None
    d = client.get("/batches/RM-1").json()
    assert d["reserved_qty"] == 0.0
    assert d["available_qty"] == 100.0


def test_cancel_reservation_without_body_uses_default_reason(client):
    _raw(client, "RM-1", 100)
    _reserve(client, "R1", "RM-1", 10, key="r")
    r = client.post(
        "/reservations/R1/cancellation", headers={"Idempotency-Key": "c"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["cancel_reason"]


def test_cancel_unknown_reservation_404(client):
    r = client.post(
        "/reservations/GHOST/cancellation", headers={"Idempotency-Key": "c"}
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RESERVATION_NOT_FOUND"


def test_cancel_consumed_reservation_rejected(client):
    _raw(client, "RM-1", 100)
    _reserve(client, "R1", "RM-1", 10, key="r")
    _produce(client, "FG-1", [("RM-1", 10)], key="p", reservation_no="R1")
    r = _cancel_reservation(client, "R1", "c")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RESERVATION_NOT_ACTIVE"
    # 实存未被取消动作影响
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 90.0


def test_cancel_expired_reservation_rejected(client, monkeypatch):
    _raw(client, "RM-1", 100)
    _reserve(client, "R1", "RM-1", 10, key="r")
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    monkeypatch.setattr(service, "utcnow", lambda: future)
    r = _cancel_reservation(client, "R1", "c")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RESERVATION_NOT_ACTIVE"
    assert client.get("/reservations/R1").json()["status"] == "expired"


# ------------------------------------------------------------- 到期释放（服务端时间）


def test_expired_reservation_releases_quota_and_allows_production(client, monkeypatch):
    _raw(client, "RM-1", 100)
    _reserve(client, "R1", "RM-1", 40, key="r")
    assert client.get("/batches/RM-1").json()["available_qty"] == 60.0
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    monkeypatch.setattr(service, "utcnow", lambda: future)
    # 到期按服务端时间即时释放，无需后台任务
    d = client.get("/batches/RM-1").json()
    assert d["reserved_qty"] == 0.0
    assert d["available_qty"] == 100.0
    g = client.get("/reservations/R1").json()
    assert g["status"] == "expired"
    # 到期额度可被普通生产使用
    assert _produce(client, "FG-1", [("RM-1", 100)], key="p").status_code == 201


def test_unexpired_reservation_released_only_after_deadline(client, monkeypatch):
    _raw(client, "RM-1", 100)
    _reserve(client, "R1", "RM-1", 40, key="r")
    before = datetime.now(timezone.utc) + timedelta(minutes=30)
    monkeypatch.setattr(service, "utcnow", lambda: before)
    assert client.get("/batches/RM-1").json()["available_qty"] == 60.0
    assert client.get("/reservations/R1").json()["status"] == "active"


# ------------------------------------------------------------- 隔离 / 复核交互


def test_quarantined_source_can_still_be_reserved(client):
    _raw(client, "RM-1", 100)
    client.post(
        "/contamination", json={"batch_no": "RM-1"}, headers={"Idempotency-Key": "f"}
    )
    # 隔离期间仍可预留，预留仍占额度
    r = _reserve(client, "R1", "RM-1", 10, key="r")
    assert r.status_code == 201, r.text
    d = client.get("/batches/RM-1").json()
    assert d["reserved_qty"] == 10.0 and d["available_qty"] == 90.0


def test_quarantined_reservation_cannot_be_consumed(client):
    _raw(client, "RM-1", 100)
    _reserve(client, "R1", "RM-1", 10, key="r")
    client.post(
        "/contamination", json={"batch_no": "RM-1"}, headers={"Idempotency-Key": "f"}
    )
    # 隔离期间占额度但不得核销
    r = _produce(client, "FG-1", [("RM-1", 10)], key="p", reservation_no="R1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_QUARANTINED"
    assert client.get("/batches/FG-1").status_code == 404
    assert client.get("/reservations/R1").json()["status"] == "active"
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 100.0


def test_consume_after_review_release_if_not_expired(client):
    _raw(client, "RM-1", 100)
    _reserve(client, "R1", "RM-1", 10, key="r")
    client.post(
        "/contamination", json={"batch_no": "RM-1"}, headers={"Idempotency-Key": "f"}
    )
    assert (
        client.post(
            "/reviews",
            json={"batch_no": "RM-1", "review_note": "误报"},
            headers={"Idempotency-Key": "v"},
        ).status_code
        == 200
    )
    # 复核放行后未到期预留可核销
    r = _produce(client, "FG-1", [("RM-1", 10)], key="p", reservation_no="R1")
    assert r.status_code == 201, r.text
    assert client.get("/reservations/R1").json()["status"] == "consumed"


def test_review_release_but_reservation_expired_still_rejected(client, monkeypatch):
    _raw(client, "RM-1", 100)
    _reserve(client, "R1", "RM-1", 10, key="r")
    client.post(
        "/contamination", json={"batch_no": "RM-1"}, headers={"Idempotency-Key": "f"}
    )
    client.post(
        "/reviews",
        json={"batch_no": "RM-1", "review_note": "误报"},
        headers={"Idempotency-Key": "v"},
    )
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    monkeypatch.setattr(service, "utcnow", lambda: future)
    # 复核放行但预留已到期：核销拒绝，额度已释放
    r = _produce(client, "FG-1", [("RM-1", 10)], key="p", reservation_no="R1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RESERVATION_NOT_ACTIVE"


# ------------------------------------------------------------- 来源作废交互


def test_reserve_cancelled_output_rejected(client):
    _raw(client, "RM-1", 100)
    _produce(client, "MIX-1", [("RM-1", 40)], key="p")
    client.post(
        "/productions/MIX-1/cancellation",
        json={"cancel_reason": "录入错误"},
        headers={"Idempotency-Key": "c"},
    )
    r = _reserve(client, "R1", "MIX-1", 1, key="r")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_CANCELLED"


def test_cancel_production_auto_cancels_active_reservations(client):
    _raw(client, "RM-1", 100)
    _produce(client, "MIX-1", [("RM-1", 40)], key="p")
    _reserve(client, "R1", "MIX-1", 5, key="r")
    assert client.get("/batches/MIX-1").json()["available_qty"] == 35.0
    # 撤销产出：同事务自动取消其未到期预留，可用量不为负
    r = client.post(
        "/productions/MIX-1/cancellation",
        json={"cancel_reason": "录入错误"},
        headers={"Idempotency-Key": "c"},
    )
    assert r.status_code == 200, r.text
    g = client.get("/reservations/R1").json()
    assert g["status"] == "cancelled"
    assert "作废" in g["cancel_reason"]
    d = client.get("/batches/MIX-1").json()
    assert d["remaining_qty"] == 0.0
    assert d["reserved_qty"] == 0.0
    assert d["available_qty"] == 0.0


# ------------------------------------------------------------- 查询


def test_list_reservations_and_statuses(client):
    _raw(client, "RM-1", 100)
    _reserve(client, "R1", "RM-1", 10, key="1")
    _reserve(client, "R2", "RM-1", 20, key="2")
    _cancel_reservation(client, "R2", "c2")
    r = client.get("/batches/RM-1/reservations")
    assert r.status_code == 200
    body = r.json()
    assert body["source_batch_no"] == "RM-1"
    assert body["availability"]["available_qty"] == 90.0
    statuses = {x["reservation_no"]: x["status"] for x in body["reservations"]}
    assert statuses == {"R1": "active", "R2": "cancelled"}


def test_list_reservations_unknown_batch_404(client):
    r = client.get("/batches/GHOST/reservations")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SOURCE_NOT_FOUND"


def test_get_unknown_reservation_404(client):
    r = client.get("/reservations/GHOST")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RESERVATION_NOT_FOUND"


# ------------------------------------------------------------- 幂等


def test_reservation_idempotent_replay(client):
    _raw(client, "RM-1", 100)
    payload = {
        "reservation_no": "R1",
        "source_batch_no": "RM-1",
        "unit": "kg",
        "quantity": "10",
        "expires_at": _iso(1),
    }
    r1 = client.post("/reservations", json=payload, headers={"Idempotency-Key": "K"})
    r2 = client.post("/reservations", json=payload, headers={"Idempotency-Key": "K"})
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json() == r2.json()
    # 预留只登记一次
    body = client.get("/batches/RM-1/reservations").json()
    assert len(body["reservations"]) == 1


def test_reservation_same_key_different_body_conflict(client):
    _raw(client, "RM-1", 100)
    p1 = {
        "reservation_no": "R1",
        "source_batch_no": "RM-1",
        "unit": "kg",
        "quantity": "10",
        "expires_at": _iso(1),
    }
    p2 = {**p1, "quantity": "11"}
    client.post("/reservations", json=p1, headers={"Idempotency-Key": "K"})
    r = client.post("/reservations", json=p2, headers={"Idempotency-Key": "K"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert client.get("/batches/RM-1").json()["reserved_qty"] == 10.0


def test_cancel_reservation_idempotent_replay(client):
    _raw(client, "RM-1", 100)
    _reserve(client, "R1", "RM-1", 10, key="r")
    r1 = _cancel_reservation(client, "R1", "K", reason="原因")
    r2 = _cancel_reservation(client, "R1", "K", reason="原因")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()
    assert client.get("/batches/RM-1").json()["reserved_qty"] == 0.0


def test_failed_reservation_same_key_retryable(client):
    # 首次来源不存在失败，失败不固化幂等记录；登记来源后同键同体重试成功
    payload = {
        "reservation_no": "R1",
        "source_batch_no": "RM-LATE",
        "unit": "kg",
        "quantity": "5",
        "expires_at": _iso(1),
    }
    r1 = client.post("/reservations", json=payload, headers={"Idempotency-Key": "K"})
    assert r1.status_code == 404
    _raw(client, "RM-LATE", 100, key="raw")
    r2 = client.post("/reservations", json=payload, headers={"Idempotency-Key": "K"})
    assert r2.status_code == 201, r2.text


def test_reserved_production_idempotent_replay_consumes_once(client):
    _raw(client, "RM-1", 100)
    _reserve(client, "R1", "RM-1", 10, key="r")
    body = {
        "output_batch_no": "FG-1",
        "unit": "kg",
        "output_quantity": 10,
        "inputs": [{"batch_no": "RM-1", "quantity": 10}],
        "reservation_no": "R1",
    }
    r1 = client.post("/productions", json=body, headers={"Idempotency-Key": "P"})
    r2 = client.post("/productions", json=body, headers={"Idempotency-Key": "P"})
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json() == r2.json()
    assert client.get("/batches/RM-1").json()["remaining_qty"] == 90.0
    assert client.get("/reservations/R1").json()["status"] == "consumed"
