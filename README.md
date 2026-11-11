# 医院月报数据核查 API

本服务面向医院数仓工程师，提供两个核查接口：

1. **计数矩阵遮盖核查**：发布分年龄组（列）、乡镇（行）的病例计数前，
   检查 `null` 遮盖格能否被**公开的行/列合计精确反推**；
2. **阳性记录更正核算**：阳性检验记录入仓后被更正（新增 / 整条替换 /
   撤销）时，按 14 天窗口规则重算病例数，返回各「采样月 × 乡镇 ×
   年龄组」月报单元需要**补计（正差额）或扣减（负差额）**的非零差额。

## 接口一：遮盖核查

对每个隐藏格，在所有合计同时成立的前提下返回：

- 可行最小值 `min`、最大值 `max`（隐藏值可取任意非负整数）；
- 分别达到两界的完整可行矩阵 `minimizer_matrix` / `maximizer_matrix`；
- `min == max` 时标记 `recoverable: true`，表示该格可被唯一反推，
  工程师应调整遮盖位置后重新核查；
- 约束无解时返回 HTTP 409 与结构化冲突说明，**不输出任何区间**。

## 接口二：阳性记录更正核算

`POST /api/v1/case-count-correction`

提交受影响人员**更正前的全部阳性记录**与一个**更正批次**（按检验记录号
新增 `add`、整条替换 `replace`、撤销 `revoke`）。每条记录含检验记录号、
脱敏人员编号、采样日、乡镇、年龄组。

病例窗口规则：

- 按人独立开窗：从最早一条尚未被覆盖的记录开窗，采样日为 14 天窗口的
  第 1 天（窗口覆盖采样日起共 14 个自然日）；
- 每窗只计首条记录为 1 例；同日记录按检验记录号字典序升序排列；
- 病例按**首条记录**的采样月、乡镇、年龄组归因。

响应只含更正后 − 更正前的**非零差额**（正数补计、负数扣减），
按（采样月, 乡镇, 年龄组）升序：

```json
{
  "status": "ok",
  "window_days": 14,
  "delta_units": 2,
  "deltas": [
    {"month": "2026-01", "township": "城东街道", "age_group": "18-59岁", "delta": -1},
    {"month": "2026-02", "township": "城东街道", "age_group": "18-59岁", "delta": 1}
  ]
}
```

整批原子校验：原记录或更正批次内编号重复、新增编号已存在、找不到要
替换或撤销的记录，任一发生即**整批拒绝**（HTTP 409），一次性返回全部
原因，不应用任何更正：

```json
{
  "status": "conflict",
  "reasons": [
    "新增的检验记录号已存在：JC2026040101",
    "找不到要撤销的记录：JC2026049999"
  ]
}
```

Swagger 内置可直接提交的**跨月撤销样例**（撤销 1 月 31 日首诊记录后，
2 月 10 日记录自立窗口，病例从 1 月移到 2 月），以及新增+整条替换、
整批报错样例。

## 目录结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI 应用与路由（含 Swagger 示例）
│   ├── schemas.py     # Pydantic 请求/响应模型、内置示例
│   ├── solver.py      # Dinic 最大流可行性判定 + HiGHS 线性规划求界
│   └── correction.py  # 14 天窗口病例计数 + 更正批次校验与差额核算
├── tests/             # pytest 测试（含暴力枚举交叉验证）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── requirements-dev.txt
```

## 启动方式（Docker Compose）

```bash
docker compose up --build
```

启动后：

- Swagger UI：<http://localhost:8000/docs> （可直接选用内置示例提交）
- ReDoc：<http://localhost:8000/redoc>
- 健康检查：<http://localhost:8000/health>

停止：`docker compose down`。

## 请求与响应

`POST /api/v1/suppression-check`

请求体（矩阵最多 12×12；`null` 为遮盖格，其余为固定非负整数）：

```json
{
  "matrix": [
    [5, null, null],
    [null, 3, null],
    [null, null, 2]
  ],
  "row_totals": [12, 10, 8],
  "col_totals": [9, 9, 12]
}
```

可行响应（HTTP 200，节选）：

```json
{
  "status": "feasible",
  "grand_total": 30,
  "hidden_count": 6,
  "recoverable_count": 0,
  "cells": [
    {"row": 0, "col": 1, "min": 0, "max": 4, "recoverable": false}
  ],
  "minimizer_matrix": [[5, 0, 7], [4, 3, 3], [0, 6, 2]],
  "maximizer_matrix": [[5, 4, 3], [0, 3, 7], [4, 2, 2]]
}
```

无解响应（HTTP 409，不含任何区间信息）：

```json
{
  "status": "conflict",
  "reasons": [
    "不存在满足全部公开合计的非负整数矩阵：最大流可行性检验未通过",
    "行子集 [0] 的隐藏格需求合计为 6，但这些行的隐藏格只能进入列 [0]，其可承接供给仅为 2（Hall 条件被破坏：需求 > 邻接供给）",
    "最大流 6 < 隐藏格所需总流量 10"
  ],
  "witness": {
    "max_flow": 6,
    "required_flow": 10,
    "row_subset": [0],
    "neighbor_cols": [0],
    "subset_demand": 6,
    "neighbor_supply": 2
  }
}
```

状态码约定：

| 场景 | 状态码 |
| --- | --- |
| 存在可行矩阵 | 200，返回区间与两张完整矩阵 |
| 合计约束无解（总量不符 / 固定格超限 / Hall 条件破坏） | 409，返回冲突原因与见证 |
| 请求结构或类型错误（超 12×12、负数、布尔、长度不符） | 422，Pydantic 校验信息 |

更正核算接口 `POST /api/v1/case-count-correction` 的状态码约定：

| 场景 | 状态码 |
| --- | --- |
| 核算完成 | 200，返回非零差额列表（可能为空） |
| 批次校验失败（编号重复 / 新增已存在 / 替换或撤销目标不存在） | 409，整批拒绝并返回全部原因 |
| 请求结构或类型错误（空编号、非法日期、缺字段、超上限） | 422，Pydantic 校验信息 |

## 算法说明

1. **建模**：每个遮盖格对应一个非负整数变量，行/列合计构成等式约束，
   即整数运输问题。
2. **可行性**：运输约束矩阵是全幺模矩阵，右侧为整数，线性规划松弛的
   顶点天然为整数解。服务先用自实现的 Dinic 最大流判定可行性：
   source→行（剩余行需求）、隐藏格所在行列之间建容量无限边、列→sink
   （剩余列供给）。最大流达不到隐藏格总流量即无解，并由最小割给出
   Hall 型冲突见证（哪些行的需求超出其邻接列供给）。
3. **求界**：可行时对每个隐藏格分别以该变量系数 +1 / −1 为目标做
   `scipy.optimize.linprog(method="highs")`，得到 `min` / `max`，
   并输出取扫描序第一个隐藏格的两张极值完整矩阵。
4. **复核**：输出矩阵在返回前重新逐行逐列核对公开合计、非负性与整数性。
5. **可还原判定**：`min == max` 即该格取值唯一，可被公开合计反推。

复杂度：设隐藏格数为 h（h ≤ 144），共 2h+1 次小规模 LP 与一次
O(V²E) 最大流；12×12 实测在百毫秒内完成。

## 本地开发（无 Docker 时）

```bash
python3 -m venv --without-pip .venv
.venv/bin/python get-pip.py            # 若 ensurepip 不可用
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload --port 8000
.venv/bin/python -m pytest tests -q
```

测试集包含 70 组随机小规模问题的**暴力枚举交叉验证**：LP 求出的每个
区间与枚举全部可行赋值的 min/max 完全一致，并覆盖无解、可还原、
零矩阵、1×1、12×12 与各类 422/409 场景。
