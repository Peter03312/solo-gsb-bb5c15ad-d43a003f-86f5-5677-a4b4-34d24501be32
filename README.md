# 批次追溯服务（batch-trace）

面向小型食品加工厂的**本地批次追溯服务**：登记原料批次、记录生产投料与产出、
查询批次余量与上下游谱系、在发现疑似污染批次时找出并隔离全部后代产出，并支持在
生产单录入错误时**撤销单据、同事务返还各来源原投料量**（产出作废、原单据留存可查）；
车间还可按**预留单号、计量单位、来源批次、正数数量与截止时间预留原料**，查询预留状态
与各批次**可用量**，并主动取消预留——预留不动实存，普通生产只能消耗可用量，
提交生产时可凭预留单号在同一事务内**核销预留、扣减实存、生成产出**。

- 语言/框架：Python 3.12 + FastAPI + SQLAlchemy 2
- 关系数据库：PostgreSQL 16（Docker Compose 一键启动）；无数据库时自动回退本地 SQLite 文件库
- 计量：`NUMERIC(18,4)` 定点数，数量必须为**正数**，同一笔单据内**单位一致**
- 部署：Dockerfile 构建镜像，`docker compose up` 安装依赖、建库建表并启动

---

## 一、快速启动

### 方式 A：Docker Compose（推荐，含 PostgreSQL）

```bash
docker compose up --build -d
```

启动后：

- API 服务地址：`http://localhost:8080`
- 健康检查：<http://localhost:8080/health>
- 交互式接口文档（Swagger UI）：<http://localhost:8080/docs>

API 容器会等待 PostgreSQL 健康后自动**建库建表**（`app/entrypoint.py` 调用 `CREATE TABLE IF NOT EXISTS ...`），
数据持久化在 compose 卷 `trace_pgdata` 中。

停止与清理：

```bash
docker compose down          # 停止，保留数据
docker compose down -v       # 停止并删除数据库卷
```

### 方式 B：本地直接运行（自动使用 SQLite）

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app.entrypoint
```

不设置 `DATABASE_URL` 时会在项目根目录创建 `trace.db`，服务同样监听 `http://0.0.0.0:8080`。

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest
```

---

## 二、配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | 见下 | SQLAlchemy 数据库连接串。compose 中为 `postgresql+psycopg2://trace:trace@db:5432/trace`；未设置时回退 `sqlite:///./trace.db` |
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | `db` / `5432` / `trace` / `trace` / `trace` | 未设置 `DATABASE_URL` 时用于拼装 PostgreSQL 连接串 |
| `HOST` | `0.0.0.0` | HTTP 监听地址 |
| `PORT` | `8080` | HTTP 监听端口 |
| `DB_POOL_SIZE` | `5` | PostgreSQL 连接池大小 |

compose 的端口映射：宿主机 `8080` → 容器 `8080`，可在 `docker-compose.yml` 的
`ports` 处修改宿主机端口。

---

## 三、HTTP 接口

所有写操作（`POST`）都必须携带请求头 `Idempotency-Key`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/raw-batches` | 登记一个原料批次及初始数量 |
| POST | `/productions` | 提交一次生产：多批次投入 → 一个产出批次（可填 `reservation_no` 按预留核销投料） |
| GET | `/productions/{output_batch_no}` | 查询生产单原单据、投料明细与撤销状态 |
| POST | `/productions/{output_batch_no}/cancellation` | 撤销录入错误的生产单，返还原投料量并作废产出 |
| GET | `/productions/{output_batch_no}/cancellation` | 查询撤销结果（返还明细、撤销原因与时间） |
| GET | `/batches/{batch_no}` | 查询批次实存余量、预留合计与可用量及状态 |
| GET | `/batches/{batch_no}/lineage` | 查询上下游谱系（递归，仅含未撤销边） |
| POST | `/contamination` | 标记疑似污染批次，返回并隔离全部后代 |
| POST | `/reviews` | 质检复核确认误报：撤销疑似标记并重算隔离 |
| POST | `/reservations` | 预留原料（预留单号、来源批次、单位、正数数量、截止时间） |
| GET | `/reservations/{reservation_no}` | 查询单笔预留状态与来源批次当前可用量 |
| GET | `/batches/{batch_no}/reservations` | 查询某来源批次的全部预留（各状态）与额度汇总 |
| POST | `/reservations/{reservation_no}/cancellation` | 主动取消预留（无请求体亦可），立即释放额度 |

### 业务规则

1. 数量一律为正数，使用定点数计算；同一生产单内所有批次单位必须一致。
2. 产出量 **不得超过** 投入总量（允许加工损耗，即产出 < 投入）。
3. 批次编号全局唯一、**不得复用**（原料批次与产出批次共用编号空间）。
4. 来源不存在、用量无效（非正数）、单位不一致或余量不足时，**整笔拒绝，不改变任何余量**。
5. 污染规则：标记某批次疑似污染后，其全部直接/间接产出（混料后代）**整体隔离**；
   隔离批次（含疑似批次本身）**不得再投料**，提交生产时整笔拒绝。
6. 复核规则：质检复核确认误报时，`POST /reviews` 提交**当前疑似批次编号**与
   **复核说明**（非空白），撤销其疑似标记并重算该批次及全部后代的隔离状态——
   自身或任一祖先仍疑似则继续隔离，否则解除。混料后代不会因一个来源解除就忽略
   另一疑似来源；编号不存在报 `SOURCE_NOT_FOUND`(404)，批次当前非疑似（从未标记
   或已撤销）报 `BATCH_NOT_SUSPECT`(409)，二者均不改变任何状态。批次可经历
   “标记 → 复核撤销 → 再次标记 → 再次复核”。
7. 幂等：同一 `Idempotency-Key` + 同一请求体重试，返回首次的状态码与响应体、不重复扣减、不重复撤销；
   同一 key + 不同请求体返回 `409 IDEMPOTENCY_CONFLICT`。
8. 撤销生产单（录入错误更正）：凭**产出批次编号**与**非空（非空白）撤销原因**调用
   `POST /productions/{output_batch_no}/cancellation`。仅当产出同时满足以下条件时才允许撤销，
   任一不符整笔拒绝且**不改变任何库存**：产出编号存在且确为生产单产出；该单**尚未撤销**；
   产出**当前非疑似**；产出**无质检复核记录**；产出**从未被后续生产投料**（引用它的下游
   生产单自身已撤销的不算）。撤销在同一事务内：① 向各来源批次**返还原投料量**（即使来源
   批次当时已隔离也照常返还，隔离只禁投料、不禁返还）；② 产出批次余量清零并逻辑作废，
   **编号永久保留**、不得再投料，也不得用同编号重新登记/生产；③ 记录撤销原因与时间。
   撤销的投料边**不再进入有效谱系查询**，也**不再参与疑似隔离传播**；撤销后按有效谱系
   重算产出隔离标志（仅被撤销的产出随之解除隔离，疑似来源本身不受影响）。撤销结果返回
   产出编号、各来源返还明细（原投料量与返还后余量）与撤销状态；原生产单、投料明细与
   撤销原因可随时经 `GET /productions/{no}` 与 `GET /productions/{no}/cancellation` 查询。
   已撤销产出再投料报 `BATCH_CANCELLED`(409)。
9. 并发：生产提交在单事务内对来源批次加行锁（PostgreSQL `SELECT ... FOR UPDATE`，
   按编号排序加锁避免死锁；SQLite 以 `BEGIN IMMEDIATE` 串行化写事务），并发提交**不会超用**同一批次余量。
   污染标记/复核/撤销均为低频更正操作：事务一开始先取 QC 串行化锁
   （PostgreSQL 为 `LOCK TABLE batches IN SHARE ROW EXCLUSIVE`，与生产事务的行锁/写入互斥；
   SQLite 由 `BEGIN IMMEDIATE` 全库写锁覆盖），随后在固定点上读取/锁定相关批次；撤销另对
   产出与全部来源沿用编号升序的同一加锁顺序。因此撤销与投料、污染标记、复核任意并发，
   结果都等价于某一串行顺序——不会超用余量、不会误放行隔离批次，也不会出现“既撤销又投料”；
   隔离批次绝不会被投料。
10. **原料预留（本轮新增）**：
    - `POST /reservations` 按**预留单号、计量单位、来源批次、正数数量和截止时间**预留原料。
      **预留不改变实存余量**；可用量 = 实存余量 − **未取消、未核销且未到期**的预留合计。
      **普通生产只能消耗可用量**；预留可叠加，用尽可用量后的新预留整笔拒绝，**无部分预留**。
    - 预留编号重复报 `RESERVATION_NO_REUSED`(409)；来源不存在报 `SOURCE_NOT_FOUND`(404)；
      单位不符报 `UNIT_MISMATCH`(422)；可用量不足报 `INSUFFICIENT_AVAILABLE`(409)；
      截止时间不晚于服务端当前时间报 `DEADLINE_NOT_FUTURE`(422)；对已作废产出预留报
      `BATCH_CANCELLED`(409)。**隔离批次仍可预留**（隔离只禁核销、不禁占额度）。
    - 提交生产时可选填 `reservation_no`：**投料批次、数量和单位须与预留完全一致，且只能有
      一笔投料**；在**同一事务内核销预留（active→consumed）、扣减实存、生成产出**。
      预留不存在报 `RESERVATION_NOT_FOUND`(404)；预留已过期/已取消/已核销报
      `RESERVATION_NOT_ACTIVE`(409)；来源已隔离报 `BATCH_QUARANTINED`(409)、来源已作废报
      `BATCH_CANCELLED`(409)；批次/数量/单位/投料笔数与预留不符报 `RESERVATION_MISMATCH`/
      `UNIT_MISMATCH`(422)。任一不符**整笔拒绝、不占用产出编号**。
    - **隔离期间预留仍占额度但不得核销**；经 `POST /reviews` 复核放行后，**仅未到期**预留可
      继续核销，已到期的按过期拒绝（额度已释放）。
    - **到期按服务端时间（UTC）即时判定并释放额度**：到期不做后台扫描、不改写记录，
      查询与核销时按当前时间与 `expires_at` 比较，状态呈现为 `expired`；到期预留不能再取消
      或核销，其额度已可被普通生产使用。
    - `POST /reservations/{no}/cancellation` 主动取消未到期预留并立即释放额度（请求体可空，
      可带 `{"reason": "..."}`）；过期/已取消/已核销报 `RESERVATION_NOT_ACTIVE`(409)。
    - 撤销生产单作废其产出时，该产出上**仍占额度的预留**在同一事务内**自动取消**
      （已核销/已取消的不动），保证作废批次可用量不为负。
    - 并发预留与投料沿用“先按编号升序锁批次行、再锁预留行”的统一加锁顺序：并发预留、
      普通投料与按预留核销任意并发，**可用量绝不会为负**；同一预留的“核销”与“取消/再次核销”
      并发时恰一方成功，结局等价于某一串行顺序。
    - 预留写操作同样要求 `Idempotency-Key`，沿用同键同体重放、异体 `409` 冲突约定。

### 错误响应格式

```json
{ "error": { "code": "INSUFFICIENT_BALANCE", "message": "批次 'RM-1' 余量不足：请求 200，剩余 100", "details": { } } }
```

常见错误码：`SOURCE_NOT_FOUND`(404)、`BATCH_NO_REUSED`(409)、`INSUFFICIENT_BALANCE`(409)、
`UNIT_MISMATCH`(422)、`OUTPUT_EXCEEDS_INPUT`(422)、`VALIDATION_ERROR`(422)、
`BATCH_QUARANTINED`(409)、`BATCH_NOT_SUSPECT`(409)、`IDEMPOTENCY_CONFLICT`(409)、
`PRODUCTION_NOT_FOUND`(404)、`PRODUCTION_ALREADY_CANCELLED`(409)、`OUTPUT_SUSPECT`(409)、
`OUTPUT_HAS_REVIEW`(409)、`OUTPUT_ALREADY_CONSUMED`(409)、`BATCH_CANCELLED`(409)、
`CANCELLATION_NOT_FOUND`(404)。

预留相关错误码：`RESERVATION_NO_REUSED`(409)、`RESERVATION_NOT_FOUND`(404)、
`INSUFFICIENT_AVAILABLE`(409)、`DEADLINE_NOT_FUTURE`(422)、
`RESERVATION_NOT_ACTIVE`(409)、`RESERVATION_MISMATCH`(422)。

---

## 四、HTTP 调用样例

### 1. 登记原料批次

```bash
curl -X POST http://localhost:8080/raw-batches \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: raw-rm-a-001' \
  -d '{"batch_no":"RM-A","unit":"kg","quantity":"100","note":"高筋面粉"}'
```

```json
{"batch_no":"RM-A","unit":"kg","initial_qty":100.0,"remaining_qty":100.0,
 "is_suspect":false,"is_quarantined":false,"note":"高筋面粉"}
```

### 2. 提交一次混料生产（多批次投入 → 一个产出）

```bash
curl -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: prod-mix1-001' \
  -d '{
        "output_batch_no": "MIX-1",
        "unit": "kg",
        "output_quantity": 90,
        "inputs": [
          {"batch_no": "RM-A", "quantity": 40},
          {"batch_no": "RM-B", "quantity": 55}
        ]
      }'
```

### 3. 安全重试（同一 key + 同一内容）

```bash
# 与上一条完全相同的请求：返回原结果，余量只扣一次
curl -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: prod-mix1-001' \
  -d '{ ...同上... }'

# 同一 key 换不同内容：
# 409 {"error":{"code":"IDEMPOTENCY_CONFLICT", ...}}
```

### 4. 查询批次余量

```bash
curl http://localhost:8080/batches/RM-A
# {"batch_no":"RM-A","unit":"kg","initial_qty":100.0,"remaining_qty":60.0, ...}
```

### 5. 查询上下游谱系

```bash
curl http://localhost:8080/batches/RM-A/lineage
```

```json
{
  "batch_no": "RM-A",
  "upstream": [],
  "downstream": [
    {"batch_no": "MIX-1", "depth": 1, "remaining_qty": 40.0,
     "unit": "kg", "is_suspect": false, "is_quarantined": false},
    {"batch_no": "FG-1",  "depth": 2, "remaining_qty": 50.0,
     "unit": "kg", "is_suspect": false, "is_quarantined": false}
  ],
  "edges": [
    {"from": "RM-A", "to": "MIX-1"},
    {"from": "MIX-1", "to": "FG-1"}
  ]
}
```

`depth` 表示与查询批次相隔的生产层数；`edges` 给出当前谱系子图中的“原料 → 产出”有向边。

### 6. 标记疑似污染并找出全部后代

```bash
curl -X POST http://localhost:8080/contamination \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: cont-20260930-01' \
  -d '{"batch_no":"RM-A","note":"抽检农残超标"}'
```

```json
{
  "source": "RM-A",
  "is_suspect": true,
  "affected_batch_nos": ["MIX-1", "FG-1"],
  "newly_quarantined": ["MIX-1", "FG-1"]
}
```

`MIX-1`（混料时混入 RM-A）与 `FG-1`（间接后代）被**整体隔离**；与 RM-A 无关的批次不受影响。
此后再用隔离批次投料会被拒绝：

```bash
curl -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: prod-fg2-001' \
  -d '{"output_batch_no":"FG-2","unit":"kg","output_quantity":5,
       "inputs":[{"batch_no":"MIX-1","quantity":5}]}'
# 409 {"error":{"code":"BATCH_QUARANTINED","message":"批次 'MIX-1' 已隔离，隔离批次不得再投料"}}
```

### 7. 质检复核：确认误报，撤销疑似标记并重算隔离

沿用上例：`RM-A` 已被标记疑似，混料后代 `MIX-1 = RM-A + RM-B`、`MIX-1 -> FG-1`
均被隔离。质检复检确认 RM-A 为误报：

```bash
curl -X POST http://localhost:8080/reviews \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: review-rm-a-20260930-01' \
  -d '{"batch_no":"RM-A","review_note":"留样复测未检出农残，确认误报"}'
```

```json
{
  "reviewed_batch_no": "RM-A",
  "is_suspect": false,
  "review_note": "留样复测未检出农残，确认误报",
  "affected_batch_nos": ["MIX-1", "FG-1"],
  "released_batch_nos": ["FG-1", "MIX-1", "RM-A"],
  "still_quarantined": []
}
```

- `affected_batch_nos`：本次重算涉及的全部后代；
- `released_batch_nos`：本次**解除隔离**的批次（复核批次自身 + 不再有任何疑似
  祖先的后代）；`still_quarantined`：重算范围内仍须隔离的批次。
- 此后 `GET /batches/{no}` 与 `/batches/{no}/lineage` 同步反映 `is_suspect=false`、
  `is_quarantined=false`；解除隔离的批次可重新投料。

混料来源不会被“连带误放”：若 RM-B 当时也处于疑似状态，撤销 RM-A 只会放行
RM-A 自身，`MIX-1`/`FG-1` 因仍有疑似祖先 RM-B 而继续隔离：

```json
{ "released_batch_nos": ["RM-A"], "still_quarantined": ["FG-1", "MIX-1"] }
```

非法复核明确报错且不改变状态：

```bash
# 编号不存在
curl -i -X POST http://localhost:8080/reviews \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: rv-x' \
  -d '{"batch_no":"GHOST","review_note":"x"}'
# 404 SOURCE_NOT_FOUND

# 已撤销 / 从未疑似
curl -i -X POST http://localhost:8080/reviews \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: rv-a2' \
  -d '{"batch_no":"RM-A","review_note":"再次撤销"}'
# 409 BATCH_NOT_SUSPECT
```

### 8. 撤销录入错误的生产单（返还原投料量、作废产出）

操作员发现生产单录入错误时，凭**产出批次编号**与**非空撤销原因**撤销。沿用第 2 节的
混料单（`MIX-1` 消耗 `RM-A` 40kg、`RM-B` 20kg，产出 55kg）：

```bash
curl -X POST http://localhost:8080/productions/MIX-1/cancellation \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: cancel-mix1-20260930-01' \
  -d '{"cancel_reason":"配料单录入错误，RM-B 实际应为 5kg，整单作废重录"}'
```

```json
{
  "output_batch_no": "MIX-1",
  "status": "cancelled",
  "returns": [
    {"batch_no": "RM-A", "returned_quantity": 40.0, "remaining_qty": 100.0},
    {"batch_no": "RM-B", "returned_quantity": 20.0, "remaining_qty": 50.0}
  ],
  "cancellation": {
    "cancel_reason": "配料单录入错误，RM-B 实际应为 5kg，整单作废重录",
    "cancelled_at": "2026-09-30T11:04:33"
  }
}
```

- `returns`：同一事务内逐笔返还的**原投料量**与返还后来源余量；即使来源批次当时已隔离
  （疑似）也照常返还——隔离只禁止再投料，不阻止错误单据的库存回退。
- 产出 `MIX-1` 余量清零、逻辑作废：`GET /batches/MIX-1` 返回 `"is_cancelled": true`、
  `"remaining_qty": 0.0`；编号永久保留，**不得再投料**（报 `BATCH_CANCELLED`），
  也不能用同编号重新登记/生产（报 `BATCH_NO_REUSED`）。
- 被撤销的投料边**退出有效谱系**：`/batches/RM-A/lineage` 中不再出现 `MIX-1` 这条链；
  也**不再传播疑似隔离**——撤销后才标记 `RM-A` 疑似，不会隔离到 `MIX-1`。

原单据与撤销原因保留可查：

```bash
# 生产单原单据 + 撤销状态/原因
curl http://localhost:8080/productions/MIX-1
# {"output_batch_no":"MIX-1", ..., "status":"cancelled",
#  "cancellation":{"cancel_reason":"...", "cancelled_at":"..."}}

# 仅取撤销结果（产出编号、返还明细、撤销原因与时间）
curl http://localhost:8080/productions/MIX-1/cancellation
# 未撤销时：404 CANCELLATION_NOT_FOUND
```

安全重试与异体冲突沿用既有约定：同一 `Idempotency-Key` + 同一请求体重放，原样返回首次
结果且只返还一次；同键换原因等不同请求体返回 `409 IDEMPOTENCY_CONFLICT`。

以下情形一律明确拒绝、**不改变任何库存**：

```bash
# 编号不存在              -> 404 SOURCE_NOT_FOUND
# 编号是原料批次而非产出  -> 404 PRODUCTION_NOT_FOUND
# 已被后续生产投料        -> 409 OUTPUT_ALREADY_CONSUMED
# 产出当前疑似            -> 409 OUTPUT_SUSPECT（须先经质检复核排除）
# 产出已有复核记录        -> 409 OUTPUT_HAS_REVIEW（经复核排除后同样永久不可撤销）
# 重复撤销                -> 409 PRODUCTION_ALREADY_CANCELLED
# 撤销原因为空/纯空白      -> 422 VALIDATION_ERROR
# 缺少 Idempotency-Key    -> 400 IDEMPOTENCY_KEY_REQUIRED
```

> 撤销只能沿谱系自底向上进行：若 `MIX-1` 已被投入 `FG-1`，须先撤销叶子 `FG-1`
> （`FG-1` 自身满足全部条件），`MIX-1` 才不再“被后续有效生产投料”，方可继续撤销。

### 9. 整笔拒绝示例（余量不足时不产生副作用）

```bash
# RM-A 当前余量 60，却请求消耗 200
curl -i -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: prod-bad-001' \
  -d '{"output_batch_no":"MIX-X","unit":"kg","output_quantity":200,
       "inputs":[{"batch_no":"RM-A","quantity":200}]}'
# HTTP/1.1 409 ... INSUFFICIENT_BALANCE；MIX-X 未创建，RM-A 余量仍为 60
```

### 10. 预留原料（不动实存，只占可用量）

为明天上午的一批生产预先锁定 `RM-A` 的 30kg：

```bash
curl -X POST http://localhost:8080/reservations \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: rsv-rm-a-20261001-01' \
  -d '{
        "reservation_no": "RSV-20261001-01",
        "source_batch_no": "RM-A",
        "unit": "kg",
        "quantity": 30,
        "expires_at": "2026-10-01T12:00:00+08:00"
      }'
```

```json
{
  "reservation_no": "RSV-20261001-01",
  "source_batch_no": "RM-A",
  "unit": "kg",
  "quantity": 30.0,
  "expires_at": "2026-10-01T04:00:00Z",
  "status": "active",
  "created_at": "2026-09-30T22:10:00Z",
  "cancelled_at": null,
  "cancel_reason": null,
  "consumed_at": null,
  "consumed_by_output_no": null,
  "source": {
    "batch_no": "RM-A", "unit": "kg",
    "remaining_qty": 100.0, "reserved_qty": 30.0, "available_qty": 70.0,
    "is_quarantined": false
  }
}
```

- `expires_at` 接受 ISO 8601 时间（建议带时区；不带时区按 UTC 解释），必须**严格晚于
  服务端当前时间**，否则 `422 DEADLINE_NOT_FUTURE`。
- `GET /batches/RM-A` 现同时返回 `remaining_qty`（实存）、`reserved_qty`（未取消/未核销/
  未到期预留合计）与 `available_qty`（可用量）；既有字段保持不变。

```bash
# 查询单笔预留状态
curl http://localhost:8080/reservations/RSV-20261001-01

# 查询某来源批次的全部预留（active/expired/cancelled/consumed）与额度汇总
curl http://localhost:8080/batches/RM-A/reservations
```

预留之后**普通生产只能消耗可用量**（70kg），不影响实存：

```bash
# 请求 71kg：409 INSUFFICIENT_BALANCE（消息中给出可用量口径），实存与预留都不变
curl -i -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: p-71' \
  -d '{"output_batch_no":"MIX-9","unit":"kg","output_quantity":71,
       "inputs":[{"batch_no":"RM-A","quantity":71}]}'
```

登记预留的拒绝情形（整笔拒绝、无部分预留）：

```bash
# 预留编号重复      -> 409 RESERVATION_NO_REUSED
# 来源批次不存在    -> 404 SOURCE_NOT_FOUND
# 单位与批次不符    -> 422 UNIT_MISMATCH
# 数量非正          -> 422 VALIDATION_ERROR
# 截止时间不在未来  -> 422 DEADLINE_NOT_FUTURE
# 可用量不足        -> 409 INSUFFICIENT_AVAILABLE
# 来源是已作废产出  -> 409 BATCH_CANCELLED
```

### 11. 按预留提交生产（同事务核销预留、扣减实存、生成产出）

```bash
curl -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: prod-rsv-20261001-01' \
  -d '{
        "output_batch_no": "MIX-7",
        "unit": "kg",
        "output_quantity": 30,
        "inputs": [{"batch_no": "RM-A", "quantity": 30}],
        "reservation_no": "RSV-20261001-01"
      }'
```

成功后同一事务内：`RM-A` 实存 100 → 70、预留 `active` → `consumed`
（`consumed_by_output_no = MIX-7`）、产出 `MIX-7` 已创建；预留不再占用额度。
投料**批次、数量、单位**必须与预留**完全一致**且只能有一笔投料：

```bash
# 预留不存在          -> 404 RESERVATION_NOT_FOUND（产出编号不被占用）
# 预留已取消/已核销   -> 409 RESERVATION_NOT_ACTIVE
# 预留已到期          -> 409 RESERVATION_NOT_ACTIVE（额度已按服务端时间释放）
# 来源批次已隔离      -> 409 BATCH_QUARANTINED（隔离期间占额度但不得核销）
# 来源批次已作废      -> 409 BATCH_CANCELLED
# 投料批次/数量/笔数
#   与预留不一致      -> 422 RESERVATION_MISMATCH
# 投料单位不一致      -> 422 UNIT_MISMATCH
```

隔离/复核与到期的交互：`RM-A` 在预留后被标记疑似时，预留**仍占额度**（普通生产同样不能
动用这 30kg），但按预留核投料会被 `BATCH_QUARANTINED` 拒绝；经复核放行后，**仍未到期**
的预留可正常核销；若放行时已过 `expires_at`，则该预留已自动失去额度（状态 `expired`），
核销以 `RESERVATION_NOT_ACTIVE` 拒绝，这 30kg 已回到可用量中可供普通生产使用。

### 12. 主动取消预留与到期释放

```bash
# 请求体可省略；也可带 {"reason": "排产调整，本批改投 RM-B"}
curl -X POST http://localhost:8080/reservations/RSV-20261001-01/cancellation \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: cancel-rsv-20261001-01' \
  -d '{"reason":"排产调整，本批改投 RM-B"}'
```

成功返回预留最新状态（`cancelled` + `cancelled_at` + `cancel_reason`），额度立即释放，
`GET /batches/RM-A` 的 `available_qty` 回升；对已取消预留再次操作返回
`409 RESERVATION_NOT_ACTIVE`，对已核销/已到期预留取消同样如此。取消接口与其它写操作一样
要求 `Idempotency-Key`，同键同体重放返回首次结果、异体冲突返回 `409 IDEMPOTENCY_CONFLICT`。

**到期不需要任何操作**：到期判定一律以**服务端当前时间（UTC）**为准，查询时状态显示为
`expired`，`reserved_qty` 即时不再计入该笔，额度自动释放给普通生产；到期记录保留可查。

---

## 五、项目结构

```
app/
  config.py       # 环境变量配置
  db.py           # 引擎/会话、SQLite 写锁与 PG 行锁支撑、建表
  models.py       # batches / productions / production_inputs / production_cancellations /
                  # batch_reviews / material_reservations / idempotency_keys
  schemas.py      # Pydantic 请求响应模型（正数、定点数、截止时间校验）
  errors.py       # 业务错误码
  service.py      # 核心逻辑：登记、预留与可用量、生产事务（含预留核销）、撤销返还、幂等、
                  # 递归有效谱系、污染传播、复核重算
  routes.py       # HTTP 路由
  main.py         # FastAPI 应用与异常处理
  entrypoint.py   # 容器入口：等待数据库 → 建表 → 启动
tests/            # 端到端 + 并发测试（100 个用例，含预留专项 41 个）
Dockerfile
docker-compose.yml
```

## 六、并发与一致性说明

- 每次生产提交是一个数据库事务：先按编号升序逐个锁定全部来源批次
  （全局统一加锁顺序，杜绝生产事务间死锁），再依次校验单位、隔离/撤销状态、
  余量与产出总量，全部通过后才扣减并写入产出；任一校验失败立即回滚。
- PostgreSQL 依赖 `SELECT ... FOR UPDATE` 行锁串行化同一批次的扣减；
  SQLite 写事务以 `BEGIN IMMEDIATE` 开始并设置 `busy_timeout`，写操作全局串行。
- 污染标记、复核与生产单撤销是低频 QC/更正操作：事务一开始先取 QC 串行化锁
  （PostgreSQL 为 `LOCK TABLE batches IN SHARE ROW EXCLUSIVE`，与生产事务的
  行锁/写入互斥；SQLite 由 `BEGIN IMMEDIATE` 全库写锁覆盖），随后在固定点上读取
  并写入。撤销事务在取到 QC 锁后，再对“产出 + 全部来源批次”沿用生产提交相同的
  编号升序加锁，因此
  - 撤销 / 投料 / 污染标记 / 复核任意并发，结果都等价于四者的某一串行顺序；
  - 撤销与“把该产出投入下游”并发时，要么投料先（撤销以 `OUTPUT_ALREADY_CONSUMED`
    拒绝），要么撤销先（投料以 `BATCH_CANCELLED` 拒绝），不可能既撤销又投料；
  - 撤销返还只增加来源余量且在锁内完成，不会超用余量；隔离批次不可能被误投料；
  - 撤销边即时退出有效谱系，标记/复核重算隔离时不会沿已撤销边传播。
- 幂等记录与业务数据在同一事务内落库，唯一约束 `(method, path, key)` 兜底并发首请求竞争。
- 预留并发沿用同一套加锁纪律：预留登记 / 普通投料 / 按预留核销都先按编号升序锁批次行，
  再锁预留行，因此
  - 并发预留同一批次时以行锁串行，合计占用绝不超过实存（可用量不为负）；
  - 预留登记与普通投料并发时，占用额度与扣减实存在同一锁上裁决，
    `20×预留成功数 + 20×投料成功数 ≤ 实存`；
  - 同一预留的“核销”与“取消”、以及两笔“核销”并发时，恰一笔成功，另一笔以
    `RESERVATION_NOT_ACTIVE` 拒绝，不会既核销又取消、也不会重复扣减；
  - 到期额度按服务端时间在加锁后的读取点上即时计算，无需后台扫描或状态迁移。

> 注：新表 `batch_reviews`、`production_cancellations` 与 `material_reservations`
> 随启动/入口的 `CREATE TABLE IF NOT EXISTS` 自动创建，既有数据库升级无需手工迁移；
> 现有登记、生产、污染标记、复核、批次/谱系查询接口的请求、响应与启动方式保持不变。
> 兼容性补充：`GET /batches/{batch_no}` 响应新增 `is_cancelled` 字段（原料批次与
> 未撤销产出恒为 `false`）、`available_qty` 与 `reserved_qty` 字段（无预留时分别等于
> `remaining_qty` 与 `0`），谱系查询仅过滤已撤销边，其余既有字段取值不变；
> `POST /productions` 请求新增可选字段 `reservation_no`（不带时行为与此前完全一致）。
> 既有不带预留的生产、撤销返还、批次与谱系查询请求/响应保持兼容。
