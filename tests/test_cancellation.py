"""生产单撤销：返还库存、作废产出、谱系退出、拒绝规则、查询与幂等。"""
from __future__ import annotations


def _raw(client, batch_no, qty, unit="kg", key=None, note=None):
    return client.post(
        "/raw-batches",
        json={"batch_no": batch_no, "unit": unit, "quantity": qty, "note": note},
        headers={"Idempotency-Key": key or f"key-{batch_no}"},
    )


def _produce(client, out_no, inputs, out_qty=None, unit="kg", key=None):
    from decimal import Decimal

    total = sum((Decimal(str(q)) for _, q in inputs), Decimal(0))
    return client.post(
        "/productions",
        json={
            "output_batch_no": out_no,
            "unit": unit,
            "output_quantity": str(out_qty if out_qty is not None else total),
            "inputs": [{"batch_no": b, "quantity": str(q)} for b, q in inputs],
        },
        headers={"Idempotency-Key": key or f"key-{out_no}"},
    )


def _flag(client, batch_no, key, note=None):
    return client.post(
        "/contamination",
        json={"batch_no": batch_no, "note": note},
        headers={"Idempotency-Key": key},
    )


def _review(client, batch_no, note, key):
    return client.post(
        "/reviews",
        json={"batch_no": batch_no, "review_note": note},
        headers={"Idempotency-Key": key},
    )


def _cancel(client, out_no, reason, key):
    return client.post(
        f"/productions/{out_no}/cancellation",
        json={"cancel_reason": reason},
        headers={"Idempotency-Key": key},
    )


def _qty(client, batch_no):
    return client.get(f"/batches/{batch_no}").json()["remaining_qty"]


# ------------------------------------------------------------- 正常撤销


def test_cancel_returns_inputs_and_voids_output(client):
    _raw(client, "RM-A", "100")
    _raw(client, "RM-B", "50")
    r = _produce(client, "MIX-1", [("RM-A", "40"), ("RM-B", "20")], out_qty=55, key="p1")
    assert r.status_code == 201
    assert _qty(client, "RM-A") == 60.0 and _qty(client, "RM-B") == 30.0

    r = _cancel(client, "MIX-1", "配料录入错误，多投 20kg", "c1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["output_batch_no"] == "MIX-1"
    assert body["status"] == "cancelled"
    assert body["cancellation"]["cancel_reason"] == "配料录入错误，多投 20kg"
    assert "cancelled_at" in body["cancellation"]
    # 返还明细：各来源原投料量 + 返还后余量
    returns = {(x["batch_no"]): x for x in body["returns"]}
    assert returns["RM-A"]["returned_quantity"] == 40.0
    assert returns["RM-A"]["remaining_qty"] == 100.0
    assert returns["RM-B"]["returned_quantity"] == 20.0
    assert returns["RM-B"]["remaining_qty"] == 50.0

    # 各来源余量已在同一事务内返还
    assert _qty(client, "RM-A") == 100.0
    assert _qty(client, "RM-B") == 50.0
    # 产出作废：余量清零，编号保留并标记 is_cancelled
    fg = client.get("/batches/MIX-1").json()
    assert fg["remaining_qty"] == 0.0
    assert fg["is_cancelled"] is True


def test_cancel_restores_even_when_sources_quarantined(client):
    # 来源批次已隔离（疑似）不影响同事务返还原投料量
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    assert _flag(client, "RM-A", "c-flag").status_code == 200
    assert client.get("/batches/RM-A").json()["is_quarantined"] is True

    r = _cancel(client, "MIX-1", "录入错误", "c1")
    assert r.status_code == 200, r.text
    assert _qty(client, "RM-A") == 100.0  # 60 + 40 返还，隔离状态不阻断返还
    # 来源仍是疑似/隔离；产出因撤销边退出传播而解除隔离且未被误放任何疑似批次
    rm = client.get("/batches/RM-A").json()
    assert rm["is_suspect"] is True and rm["is_quarantined"] is True
    mix = client.get("/batches/MIX-1").json()
    assert mix["is_quarantined"] is False and mix["is_suspect"] is False


def test_cancelled_edge_leaves_effective_lineage(client):
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    _produce(client, "FG-1", [("MIX-1", "10")], key="p2")
    # FG-1 是叶子（无后续投料）可撤销；撤销后 MIX-1 -> FG-1 边退出有效谱系
    r = _cancel(client, "FG-1", "录入错误", "c1")
    assert r.status_code == 200, r.text

    # RM-A 的有效下游里不再出现 FG-1（FG-1 的边 FG->无；MIX-1->FG-1 边被撤销）
    down = {n["batch_no"] for n in client.get("/batches/RM-A/lineage").json()["downstream"]}
    assert down == {"MIX-1"}
    # MIX-1 谱系里不再有 FG-1；有效上游边 RM-A -> MIX-1 保留，
    # 被撤销的 MIX-1 -> FG-1 边不再出现
    lin = client.get("/batches/MIX-1/lineage").json()
    assert {n["batch_no"] for n in lin["downstream"]} == set()
    assert lin["edges"] == [{"from": "RM-A", "to": "MIX-1"}]
    # FG-1 自身谱系为孤立点：无上下游、无边
    fg_lin = client.get("/batches/FG-1/lineage").json()
    assert fg_lin["upstream"] == [] and fg_lin["edges"] == []


def test_cancelled_edge_excluded_from_suspect_propagation(client):
    # 撤销后再标记来源疑似，不得沿已撤销边传播到作废产出
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    assert _cancel(client, "MIX-1", "录入错误", "c1").status_code == 200

    assert _flag(client, "RM-A", "f1").status_code == 200
    mix = client.get("/batches/MIX-1").json()
    assert mix["is_quarantined"] is False  # 撤销边不参与疑似隔离传播
    # 但来源自身仍隔离
    assert client.get("/batches/RM-A").json()["is_quarantined"] is True


# ------------------------------------------------------------- 条件不符拒绝


def test_cancel_rejected_when_output_consumed_by_later_production(client):
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    _produce(client, "FG-1", [("MIX-1", "10")], key="p2")  # MIX-1 已被后续投料

    r = _cancel(client, "MIX-1", "录入错误", "c1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "OUTPUT_ALREADY_CONSUMED"
    # 不改库存
    assert _qty(client, "RM-A") == 60.0
    assert _qty(client, "MIX-1") == 30.0
    assert client.get("/batches/MIX-1").json()["is_cancelled"] is False


def test_consuming_production_that_is_later_cancelled_does_not_block(client):
    # 下游单自身已撤销时，不视为“被后续生产投料”，上游产出可撤销
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    _produce(client, "FG-1", [("MIX-1", "10")], key="p2")
    # 先撤销叶子 FG-1（无下游）
    assert _cancel(client, "FG-1", "下游录错", "c1").status_code == 200
    # FG-1 已撤销：MIX-1 不再有“有效”的后续投料，可撤销
    r = _cancel(client, "MIX-1", "上游录错", "c2")
    assert r.status_code == 200, r.text
    assert _qty(client, "RM-A") == 100.0
    assert _qty(client, "MIX-1") == 0.0


def test_cancel_rejected_when_output_suspect(client):
    _raw(client, "RM-A", "100")
    _raw(client, "RM-B", "100")
    _produce(client, "MIX-1", [("RM-A", "40"), ("RM-B", "55")], key="p1")
    # 因另一来源疑似，MIX-1 被隔离但其自身非疑似 —— 允许撤销
    assert _flag(client, "RM-B", "f-b").status_code == 200
    assert client.get("/batches/MIX-1").json()["is_suspect"] is False
    r = _cancel(client, "MIX-1", "录入错误", "c1")
    assert r.status_code == 200, r.text
    assert _qty(client, "RM-A") == 100.0 and _qty(client, "RM-B") == 100.0


def test_cancel_rejected_when_output_itself_suspect(client):
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    assert _flag(client, "MIX-1", "f1").status_code == 200  # 直接标记产出疑似

    r = _cancel(client, "MIX-1", "录入错误", "c1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "OUTPUT_SUSPECT"
    assert _qty(client, "RM-A") == 60.0  # 未返还
    assert client.get("/batches/MIX-1").json()["is_cancelled"] is False

    # 经复核排除嫌疑后：当前虽不再疑似，但已留下复核记录，仍不得撤销
    assert _review(client, "MIX-1", "复检合格", "r1").status_code == 200
    r = _cancel(client, "MIX-1", "录入错误", "c2")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "OUTPUT_HAS_REVIEW"
    assert _qty(client, "RM-A") == 60.0


def test_cancel_rejected_when_output_has_review_record(client):
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    # 标记产出疑似再复核：留下复核记录，此后即便非疑似也不得撤销
    assert _flag(client, "MIX-1", "f1").status_code == 200
    assert _review(client, "MIX-1", "误报", "r1").status_code == 200

    r = _cancel(client, "MIX-1", "录入错误", "c1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "OUTPUT_HAS_REVIEW"
    assert _qty(client, "RM-A") == 60.0


def test_duplicate_cancel_rejected(client):
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    assert _cancel(client, "MIX-1", "第一次撤销", "c1").status_code == 200

    r = _cancel(client, "MIX-1", "重复撤销", "c2")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PRODUCTION_ALREADY_CANCELLED"
    assert _qty(client, "RM-A") == 100.0  # 没有重复返还


def test_cancelled_output_cannot_be_consumed(client):
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    assert _cancel(client, "MIX-1", "录入错误", "c1").status_code == 200

    r = _produce(client, "FG-1", [("MIX-1", "5")], key="p2")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_CANCELLED"
    assert client.get("/batches/FG-1").status_code == 404


def test_cancel_unknown_number_rejected(client):
    r = _cancel(client, "GHOST", "x", "c1")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SOURCE_NOT_FOUND"


def test_cancel_raw_batch_rejected(client):
    _raw(client, "RM-A", "100")
    r = _cancel(client, "RM-A", "原料单不能撤销", "c1")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PRODUCTION_NOT_FOUND"
    assert _qty(client, "RM-A") == 100.0


def test_cancel_blank_reason_rejected(client):
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    r = client.post(
        "/productions/MIX-1/cancellation",
        json={"cancel_reason": "   "},
        headers={"Idempotency-Key": "c1"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _qty(client, "RM-A") == 60.0
    assert client.get("/batches/MIX-1").json()["is_cancelled"] is False


def test_cancel_requires_idempotency_key(client):
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    r = client.post(
        "/productions/MIX-1/cancellation", json={"cancel_reason": "无键"}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert _qty(client, "RM-A") == 60.0


# ------------------------------------------------------------- 幂等


def test_cancel_idempotent_replay_returns_first_result(client):
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    payload = {"cancel_reason": "录入错误"}
    r1 = client.post(
        "/productions/MIX-1/cancellation",
        json=payload,
        headers={"Idempotency-Key": "K1"},
    )
    assert r1.status_code == 200
    # 同键同体重放：返回首次结果，库存只返还一次
    r2 = client.post(
        "/productions/MIX-1/cancellation",
        json=payload,
        headers={"Idempotency-Key": "K1"},
    )
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    assert _qty(client, "RM-A") == 100.0


def test_cancel_same_key_different_content_conflicts(client):
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    assert (
        client.post(
            "/productions/MIX-1/cancellation",
            json={"cancel_reason": "原因一"},
            headers={"Idempotency-Key": "K"},
        ).status_code
        == 200
    )
    r = client.post(
        "/productions/MIX-1/cancellation",
        json={"cancel_reason": "原因二"},
        headers={"Idempotency-Key": "K"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    # 首笔撤销仍生效且只返还一次
    detail = client.get("/productions/MIX-1").json()
    assert detail["status"] == "cancelled"
    assert detail["cancellation"]["cancel_reason"] == "原因一"
    assert _qty(client, "RM-A") == 100.0


# ------------------------------------------------------------- 查询保留


def test_production_detail_keeps_original_document(client):
    _raw(client, "RM-A", "100")
    _raw(client, "RM-B", "50")
    _produce(client, "MIX-1", [("RM-A", "40"), ("RM-B", "20")], out_qty=55, key="p1")

    detail = client.get("/productions/MIX-1").json()
    assert detail["status"] == "active"
    assert detail["cancellation"] is None
    assert detail["output_batch_no"] == "MIX-1"
    assert detail["output_quantity"] == 55.0
    assert detail["inputs"] == [
        {"batch_no": "RM-A", "quantity": 40.0},
        {"batch_no": "RM-B", "quantity": 20.0},
    ]

    _cancel(client, "MIX-1", "录入错误", "c1")
    detail = client.get("/productions/MIX-1").json()
    assert detail["status"] == "cancelled"
    # 原单据及投料明细仍保留
    assert detail["inputs"] == [
        {"batch_no": "RM-A", "quantity": 40.0},
        {"batch_no": "RM-B", "quantity": 20.0},
    ]
    assert detail["cancellation"]["cancel_reason"] == "录入错误"
    assert "cancelled_at" in detail["cancellation"]


def test_get_production_not_found_codes(client):
    assert client.get("/productions/GHOST").status_code == 404
    assert (
        client.get("/productions/GHOST").json()["error"]["code"] == "SOURCE_NOT_FOUND"
    )
    _raw(client, "RM-A", "100")
    r = client.get("/productions/RM-A")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PRODUCTION_NOT_FOUND"


def test_cancellation_record_query(client):
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")

    # 未撤销 -> 404
    r = client.get("/productions/MIX-1/cancellation")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "CANCELLATION_NOT_FOUND"

    _cancel(client, "MIX-1", "录入错误", "c1")
    r = client.get("/productions/MIX-1/cancellation")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "cancelled"
    assert body["cancellation"]["cancel_reason"] == "录入错误"
    assert {x["batch_no"]: x["returned_quantity"] for x in body["returns"]} == {
        "RM-A": 40.0
    }


def test_returned_source_can_be_consumed_again(client):
    # 撤销返还后，来源余量可再次用于新的生产单（编号不与作废产出冲突）
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    assert _cancel(client, "MIX-1", "录入错误", "c1").status_code == 200

    r = _produce(client, "MIX-2", [("RM-A", "100")], out_qty=98, key="p2")
    assert r.status_code == 201, r.text
    assert _qty(client, "RM-A") == 0.0
    assert _qty(client, "MIX-2") == 98.0


def test_cancelled_output_number_not_reusable(client):
    # 作废产出编号保留，不得用同编号再登记/生产
    _raw(client, "RM-A", "100")
    _produce(client, "MIX-1", [("RM-A", "40")], key="p1")
    assert _cancel(client, "MIX-1", "录入错误", "c1").status_code == 200

    r = _produce(client, "MIX-1", [("RM-A", "5")], key="p2")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_NO_REUSED"
