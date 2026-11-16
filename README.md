# 增量净额 SQL 反例精简器

面向结算数仓开发者的定位工具：给定 `orders`/`refunds` 初始数据、至多 20 批增删改变更、
**全量 SELECT** 与**增量 SQL**，服务会自动找出增量维护净额表时的错误，并把导致错误的
变更批次缩减到最小（保留原批号与原顺序），返回精简输入、首次差异批号以及缺失/多余行。

## 快速开始

```bash
# 1) Docker Compose（推荐）
docker compose up --build
#    Swagger UI: http://localhost:8000/docs
#    OpenAPI   : http://localhost:8000/openapi.json
#    健康检查  : http://localhost:8000/health

# 2) 本地运行（Python 3.11+）
python3 -m pip install -r requirements.txt
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# 3) 测试
python3 -m pytest tests/ -q
```

示例请求：

```bash
curl -s -X POST http://localhost:8000/v1/minimize \
     -H 'content-type: application/json' \
     --data-binary @examples/leak_refund_delete.json | python3 -m json.tool
```

`examples/leak_refund_delete.json` 是一个**漏处理退款删除**的样例：增量 SQL 处理了
订单插入/更新、退款插入，却忘了退款删除，于是批次 103 删除退款后 `result` 没有回补，
净额少算。服务返回：

```json
{
  "status": "reduced",
  "first_difference_batch_id": 103,
  "difference": {"missing": [[1001, 3800]], "extra": [[1001, 3300]]},
  "reduced_input": {"initial": {...}, "batches": [{"batch_id": 101, ...}, {"batch_id": 103, ...}], "sql": {...}},
  "notes": ["缩减完成：保留 2 个批次；删除任一剩余批次都会使输入非法或消除差异", ...]
}
```

即：原本 6 个批次里只剩 2 个（101 插入退款、103 删除退款）；批次 101 无法删除，因为
删掉后批次 103 的删除目标不存在，输入非法。`examples/correct_incremental.json` 是正确
实现，返回 `no_difference`。

## 数据模型与执行契约

### 初始数据

```jsonc
{
  "orders":  [{"order_id": 1001, "amount": 5000}, ...],   // 订单号、金额（整数分）
  "refunds": [{"refund_id": 9001, "order_id": 1001, "amount": 1200}, ...]
}
```

`orders + refunds` 合计至多 **100 行**；金额必须是整数分，主键必须是正整数且唯一，
退款的 `order_id` 必须存在。

### 批次

```jsonc
{
  "batch_id": 103,                      // 原批号，输入内唯一；缩减后仍沿用
  "changes": [
    {"op": "insert", "table": "refunds", "after":  {"refund_id": 9002, "order_id": 1001, "amount": 500}},
    {"op": "update", "table": "orders",  "before": {"order_id": 1001},
                                          "after":  {"order_id": 1001, "amount": 6000}},
    {"op": "delete", "table": "refunds", "before": {"refund_id": 9002}}
  ]
}
```

- 至多 20 批；每批按给定顺序应用，**批号顺序即输入顺序**。
- `insert`：`after` 为完整行，主键不得已存在。
- `update`/`delete`：`before` 只需提供主键（用于定位目标，目标必须存在）；`update` 的
  `after` 为完整新行，允许改主键。
- 每批应用后必须满足主键唯一与 `refunds.order_id → orders.order_id` 外键；同一批内可以
  先改主键再同步改引用行（批内事务使用延迟外键检查）。

### SQL

`full_select`

- 必须是**单条** `SELECT`/`WITH`/`VALUES` 语句，只读；
- 只能访问 `orders`、`refunds`；
- 必须返回 **2 列** `(order_id, net_amount)`，均为整数；
- 结果按**多重集**处理：重复行会计数。`result` 表没有主键/唯一约束，就是为了让重复行
  也能被检出。

`incremental`

- 可包含多条语句，按顺序在**每批一个事务**内执行；
- 只能读 `orders`、`refunds`、`batch_changes`、`result`；
- 只能写 `result`（`INSERT`/`UPDATE`/`DELETE`/`REPLACE`）；
- 至少一条写 `result` 的语句；
- 禁止 DDL、`ATTACH`、`PRAGMA`、事务控制、`load_extension` 等。

`batch_changes` 提供**本批**变更的前后值（列名见
[app/schema.py](app/schema.py)）：

| 列 | 含义 |
| --- | --- |
| `seq` | 批内变更序号（从 0 开始） |
| `tbl` | `'orders'` / `'refunds'` |
| `op` | `'insert'` / `'update'` / `'delete'` |
| `old_order_id`, `old_amount` | 订单旧值（insert 时为 NULL） |
| `old_refund_id`, `old_refund_order_id`, `old_refund_amount` | 退款旧值 |
| `new_order_id`, `new_amount` | 订单新值（delete 时为 NULL） |
| `new_refund_id`, `new_refund_order_id`, `new_refund_amount` | 退款新值 |

## 判定流程

1. 校验初始数据与全部批次合法（主键、外键、改删对象存在）。
2. **一次试验** = 全新的进程内内存 SQLite 库，从相同初始状态重建：
   - 建源表并载入初始行；执行 `full_select` 得到初始全量成品；
   - 建 `result` 并把全量成品载入其中（"从全量初始成品开始"）；
   - 按原顺序逐批：开事务 → 应用源表变更 → 刷新 `batch_changes` → 执行增量 SQL →
     提交；随后只读重跑 `full_select`，与 `result` 做**多重集**比较；
   - 第一个不一致的批次即"首次差异"，记录缺行/多行后停止。
3. 若完整输入报错或超时（单条语句 > 1 秒、结果超行数上限、被安全策略拒绝），状态为
   `undetermined`，**不能**作为缩减完成的依据。
4. **缩减**：只删批次、不改顺序。候选输入必须仍然合法且仍存在差异才会被接受；采用
   ddmin 式分块删除 + 单批贪心清扫。
5. **认证**：对每个剩余批次单独删一次；只有每次都"输入非法"或"差异消失"才返回
   `reduced`/`already_minimal`。任一次删除出现报错/超时，返回 `undetermined` 并说明。

### 返回字段

| 字段 | 说明 |
| --- | --- |
| `status` | `reduced` / `already_minimal` / `no_difference` / `undetermined` |
| `reduced_input` | `initial`、`batches`（精简结果，原批号原顺序）、`sql` |
| `first_difference_batch_id` | 精简输入中首次差异的**原批号** |
| `difference.missing` / `difference.extra` | 缺失 / 多余的行（多重集，重复行重复列出） |
| `notes` | 每个保留批次的保留原因、无法判定的原因 |
| `blocked_by` | 阻止“缩减完成”结论的具体原因（SQL 报错/超时/预算耗尽）；为空表示认证完成 |
| `evaluations` | 本次请求评估过的候选输入数 |

> 注意：缩减可能改变"首次差异批号"（删除更早的批次后，首个差异可能提前出现），
> 返回的始终是**精简输入**中首次差异的原始批号，并在 `evaluations`/`notes` 中说明。

## 安全与隔离

- 每次 `Executor.run` 新建内存数据库，连接不共享；FastAPI 同步端点在工作线程中运行，
  并发请求之间没有共享的可变状态。测试 `test_concurrent_requests_are_isolated` 覆盖。
- 双层防护：文本预检（语句数、首关键字、禁止词、写目标表）+ SQLite
  `set_authorizer`（表级读写权限、禁止 DDL/ATTACH/PRAGMA/事务控制）。
- 资源限制：单语句 1 秒（progress handler 中断）、结果行数上限、数据库页上限、值长度
  上限、单请求总时限与最大试验次数。
- 容器：非 root 用户、`read_only` 根文件系统、`tmpfs /tmp`、`cap_drop: ALL`、
  `no-new-privileges`。

## 可调参数（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `STATEMENT_TIMEOUT_SECONDS` | `1.0` | 单条语句时限；超过即"无法判定"（任务要求 1 秒） |
| `MAX_RESULT_ROWS` | `10000` | 单次查询结果行数上限 |
| `MAX_TRIALS` | `400` | 单请求最多候选评估次数 |
| `MAX_REQUEST_SECONDS` | `120` | 单请求总时限 |
| `MAX_DB_PAGES` | `16384` | 每个试验库的页数上限 |
| `MAX_VALUE_BYTES` | `1000000` | 单个值长度上限 |

## 已知限制

- `full_select` 必须正好返回 2 列；不同宽度视为 SQL 错误（`undetermined`），不做列裁剪。
- 增量 SQL 的读权限按表控制，不限制读取整表或构造大中间结果；这类查询会因 1 秒预算或
  行数上限被判定为"无法判定"。
- 缩减只针对批次层面；单个批次内部的多个 `change` 不会被拆开或删减。
- SQLite 是单线程内存库；每次试验重建数据库，因此候选评估的成本与批次数、数据量成正比
  （数据上限 100 行、20 批，实测示例 9 次评估内完成）。
- 不处理跨请求的持久化或任务队列；请求是同步完成的（超时受 `MAX_REQUEST_SECONDS` 约束）。

## 目录结构

```
app/
  main.py       FastAPI 应用与端点
  models.py     Pydantic 请求/响应模型
  validate.py   初始数据与批次合法性、CDC 记录
  schema.py     SQLite 表结构与序列化
  sqlguard.py   SQL 策略（文本预检 + authorizer）
  executor.py   单次试验：重建库、逐批事务、多重集比较
  reducer.py    ddmin 缩减与最小性认证
  config.py     环境变量配置
tests/          92 个测试（校验、SQL 策略、SQL 形态、执行器、缩减器、API、契约）
examples/       正确实现与漏删退款的反例
```
