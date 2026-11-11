"""请求/响应 Pydantic 模型与 Swagger 示例。"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

# 示例数据（行列序号均从 0 开始，与请求中的矩阵下标一致）---------------

# 示例 1：3x3，4 个隐藏格构成自由块，取值为区间，不可还原。
EX_RANGE: dict = {
    "matrix": [
        [5, None, None],
        [None, 3, None],
        [None, None, 2],
    ],
    "row_totals": [12, 10, 8],
    "col_totals": [9, 9, 12],
}

# 示例 2：3x3，隐藏格被合计完全锁定，全部可还原。
EX_RECOVERABLE: dict = {
    "matrix": [
        [2, None, 3],
        [None, 4, None],
        [5, 1, None],
    ],
    "row_totals": [10, 10, 9],
    "col_totals": [9, 10, 10],
}

# 示例 3：行合计之和与列合计之和不相等，直接冲突。
EX_TOTAL_CONFLICT: dict = {
    "matrix": [
        [1, None],
        [None, 2],
    ],
    "row_totals": [5, 5],
    "col_totals": [6, 6],
}

# 示例 4：总量相等但运输结构无解（第 0 行只能把量送进第 0 列，容量不够）。
EX_HALL_CONFLICT: dict = {
    "matrix": [
        [None, 4],
        [3, None],
    ],
    "row_totals": [10, 7],
    "col_totals": [5, 12],
}

# ---------------------------------------------------------------------------
# 阳性检验记录更正核算示例
# ---------------------------------------------------------------------------

# 示例 5：跨月撤销。P0001 首条记录采样于 1 月 31 日，其 14 天窗口覆盖到
# 2 月 13 日，2 月 10 日的记录被覆盖不计；撤销首条后，2 月 10 日的记录
# 自立窗口，病例从 1 月月报单元移到 2 月（1 月扣减 1、2 月补计 1）。
EX_CROSS_MONTH_REVOKE: dict = {
    "records": [
        {
            "record_id": "JC2026013101",
            "person_id": "P0001",
            "sample_date": "2026-01-31",
            "township": "城东街道",
            "age_group": "18-59岁",
        },
        {
            "record_id": "JC2026021002",
            "person_id": "P0001",
            "sample_date": "2026-02-10",
            "township": "城东街道",
            "age_group": "18-59岁",
        },
        {
            "record_id": "JC2026011503",
            "person_id": "P0002",
            "sample_date": "2026-01-15",
            "township": "河西镇",
            "age_group": "60岁及以上",
        },
    ],
    "corrections": {
        "add": [],
        "replace": [],
        "revoke": ["JC2026013101"],
    },
}

# 示例 6：新增 + 整条替换。替换把 3 月 20 日的记录改回 3 月 10 日，
# 落入 3 月 1 日开窗的 14 天窗口内而不再单独计例；同时为 P1002 新增
# 一条 3 月 5 日的记录。
EX_ADD_REPLACE: dict = {
    "records": [
        {
            "record_id": "JC2026030110",
            "person_id": "P1001",
            "sample_date": "2026-03-01",
            "township": "河口镇",
            "age_group": "0-17岁",
        },
        {
            "record_id": "JC2026032011",
            "person_id": "P1001",
            "sample_date": "2026-03-20",
            "township": "河口镇",
            "age_group": "0-17岁",
        },
    ],
    "corrections": {
        "add": [
            {
                "record_id": "JC2026030501",
                "person_id": "P1002",
                "sample_date": "2026-03-05",
                "township": "河口镇",
                "age_group": "18-59岁",
            }
        ],
        "replace": [
            {
                "record_id": "JC2026032011",
                "person_id": "P1001",
                "sample_date": "2026-03-10",
                "township": "河口镇",
                "age_group": "0-17岁",
            }
        ],
        "revoke": [],
    },
}

# 示例 7：整批报错。新增的编号已存在，且要撤销的编号找不到，
# 整批拒绝并一次性返回全部原因（HTTP 409）。
EX_BATCH_CONFLICT: dict = {
    "records": [
        {
            "record_id": "JC2026040101",
            "person_id": "P2001",
            "sample_date": "2026-04-01",
            "township": "城北街道",
            "age_group": "18-59岁",
        }
    ],
    "corrections": {
        "add": [
            {
                "record_id": "JC2026040101",
                "person_id": "P2002",
                "sample_date": "2026-04-03",
                "township": "城北街道",
                "age_group": "0-17岁",
            }
        ],
        "replace": [],
        "revoke": ["JC2026049999"],
    },
}


class SuppressionRequest(BaseModel):
    """核查请求：计数矩阵（null 表示遮盖格）与公开行/列合计。"""

    matrix: list[list[StrictInt | None]] = Field(
        ...,
        description=(
            "最多 12x12 的非负整数计数矩阵；null 表示被遮盖的格子，"
            "其余数值视为已公开的固定格"
        ),
    )
    row_totals: list[StrictInt] = Field(
        ..., description="每一行的公开合计，长度须等于矩阵行数"
    )
    col_totals: list[StrictInt] = Field(
        ..., description="每一列的公开合计，长度须等于矩阵列数"
    )

    @field_validator("matrix")
    @classmethod
    def _validate_matrix_cells(cls, value):
        # StrictInt 在联合类型里仍可能放行 bool（bool 是 int 子类），显式拒绝；
        # 同时统一拦截负数
        for i, row in enumerate(value):
            for j, cell in enumerate(row):
                if isinstance(cell, bool):
                    raise ValueError(
                        f"matrix[{i}][{j}] 不能是布尔值，须为整数或 null"
                    )
                if cell is not None and cell < 0:
                    raise ValueError(f"matrix[{i}][{j}] 不能为负数：{cell}")
        return value

    @field_validator("row_totals", "col_totals")
    @classmethod
    def _validate_totals(cls, value):
        for i, cell in enumerate(value):
            if isinstance(cell, bool):
                raise ValueError(f"合计值不能是布尔值：位置 {i}")
            if cell < 0:
                raise ValueError(f"合计值不能为负数：位置 {i} = {cell}")
        return value

    @model_validator(mode="after")
    def _check_shape(self) -> "SuppressionRequest":
        if not self.matrix:
            raise ValueError("matrix 至少要包含一行")
        if len(self.matrix) > 12:
            raise ValueError("matrix 行数不能超过 12")
        width = len(self.matrix[0])
        if width == 0 or width > 12:
            raise ValueError("matrix 列数须在 1..12 之间")
        for i, row in enumerate(self.matrix):
            if len(row) != width:
                raise ValueError(f"第 {i} 行长度与首行不一致（应为 {width}）")
        if len(self.row_totals) != len(self.matrix):
            raise ValueError("row_totals 长度须等于矩阵行数")
        if len(self.col_totals) != width:
            raise ValueError("col_totals 长度须等于矩阵列数")
        if any(v < 0 for v in self.row_totals):
            raise ValueError("row_totals 不能包含负数")
        if any(v < 0 for v in self.col_totals):
            raise ValueError("col_totals 不能包含负数")
        return self

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"summary": "可行：隐藏格存在取值区间（不可还原）", "value": EX_RANGE},
                {"summary": "可行：隐藏格被合计唯一锁定（可还原）", "value": EX_RECOVERABLE},
                {"summary": "冲突：行列总合计不相等", "value": EX_TOTAL_CONFLICT},
                {"summary": "冲突：总量相等但结构无解（Hall 条件）", "value": EX_HALL_CONFLICT},
            ]
        }
    }


class Dimensions(BaseModel):
    rows: int
    cols: int


class CellRange(BaseModel):
    row: int = Field(..., description="隐藏格行下标（从 0 开始）")
    col: int = Field(..., description="隐藏格列下标（从 0 开始）")
    min: int = Field(..., description="在全部合计同时成立前提下该格的最小取值")
    max: int = Field(..., description="在全部合计同时成立前提下该格的最大取值")
    recoverable: bool = Field(
        ..., description="min == max 时为 true，表示可被公开合计精确反推"
    )


class FeasibleResponse(BaseModel):
    status: Literal["feasible"]
    dimensions: Dimensions
    grand_total: int = Field(..., description="行合计之和（等于列合计之和）")
    hidden_count: int
    recoverable_count: int = Field(..., description="可还原隐藏格数量")
    cells: list[CellRange] = Field(
        ..., description="每个隐藏格的取值区间与可还原标记，按行优先顺序"
    )
    minimizer_matrix: list[list[int]] = Field(
        ..., description="达到各隐藏格下界的完整可行矩阵（对首个隐藏格取 min）"
    )
    maximizer_matrix: list[list[int]] = Field(
        ..., description="达到各隐藏格上界的完整可行矩阵（对首个隐藏格取 max）"
    )


class ConflictWitness(BaseModel):
    max_flow: int
    required_flow: int
    row_subset: list[int] = Field(
        ..., description="最小割源侧的行下标集合 R（0 起）"
    )
    neighbor_cols: list[int] = Field(
        ..., description="与 R 中隐藏格相邻的列下标集合 N(R)"
    )
    subset_demand: int = Field(..., description="行子集 R 的隐藏格总需求")
    neighbor_supply: int = Field(
        ..., description="邻接列 N(R) 能承接的总供给，小于需求即无解"
    )


class ConflictResponse(BaseModel):
    status: Literal["conflict"]
    reasons: list[str] = Field(
        ..., description="冲突的文字说明；无解时不返回任何取值区间"
    )
    witness: ConflictWitness | None = Field(
        default=None, description="最大流最小割给出的 Hall 型冲突见证（若适用）"
    )


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str


# ---------------------------------------------------------------------------
# 阳性检验记录更正核算
# ---------------------------------------------------------------------------

# 防御性上限：单次提交的原始记录与各更正操作数量
MAX_RECORDS = 10000


class PositiveRecord(BaseModel):
    """一条阳性检验记录。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    record_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="检验记录号（全局唯一，同日记录按其字典序升序排列）",
        examples=["JC2026013101"],
    )
    person_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="脱敏人员编号（窗口按人独立计算）",
        examples=["P0001"],
    )
    sample_date: date = Field(
        ...,
        description="采样日（YYYY-MM-DD）；开窗时该日为 14 天窗口的第 1 天",
        examples=["2026-01-31"],
    )
    township: str = Field(
        ..., min_length=1, max_length=64, description="乡镇（街道）", examples=["城东街道"]
    )
    age_group: str = Field(
        ..., min_length=1, max_length=32, description="年龄组", examples=["18-59岁"]
    )


class CorrectionBatch(BaseModel):
    """更正批次：按检验记录号新增、整条替换或撤销。

    三类操作共享同一编号空间，批次内编号不得重复；任一校验失败
    （重复编号、新增编号已存在、替换/撤销目标不存在）即整批拒绝，
    不应用任何更正。
    """

    add: list[PositiveRecord] = Field(
        default_factory=list,
        max_length=MAX_RECORDS,
        description="新增的完整记录；检验记录号不得与现有记录重复",
    )
    replace: list[PositiveRecord] = Field(
        default_factory=list,
        max_length=MAX_RECORDS,
        description="整条替换：按检验记录号定位原记录，其余字段（人员、采样日、乡镇、年龄组）整体替换",
    )
    revoke: list[str] = Field(
        default_factory=list,
        max_length=MAX_RECORDS,
        description="要撤销的检验记录号",
    )


class CorrectionImpactRequest(BaseModel):
    """更正影响核算请求：更正前全部阳性记录 + 一个更正批次。"""

    records: list[PositiveRecord] = Field(
        ...,
        max_length=MAX_RECORDS,
        description="受影响人员更正前的全部阳性检验记录",
    )
    corrections: CorrectionBatch = Field(..., description="更正批次（新增 / 整条替换 / 撤销）")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"summary": "跨月撤销：病例从 1 月移到 2 月", "value": EX_CROSS_MONTH_REVOKE},
                {"summary": "新增 + 整条替换", "value": EX_ADD_REPLACE},
                {"summary": "整批报错：新增编号已存在且撤销目标不存在", "value": EX_BATCH_CONFLICT},
            ]
        }
    }


class CaseCountDelta(BaseModel):
    """一个月报单元（采样月 × 乡镇 × 年龄组）的病例数差额。"""

    month: str = Field(..., description="窗口首条记录的采样月，YYYY-MM", examples=["2026-02"])
    township: str = Field(..., description="乡镇（街道）", examples=["城东街道"])
    age_group: str = Field(..., description="年龄组", examples=["18-59岁"])
    delta: int = Field(
        ...,
        description="更正后 − 更正前；正数需补计，负数需扣减，恒为非零整数",
        examples=[1],
    )


class CorrectionImpactResponse(BaseModel):
    status: Literal["ok"]
    window_days: int = Field(..., description="窗口长度（天），采样日为第 1 天")
    delta_units: int = Field(..., description="发生变化的月报单元数量")
    deltas: list[CaseCountDelta] = Field(
        ...,
        description="仅含非零差额的月报单元，按（采样月, 乡镇, 年龄组）升序",
    )


class CorrectionConflictResponse(BaseModel):
    """更正批次校验失败（HTTP 409）：整批拒绝，不应用任何更正。"""

    status: Literal["conflict"]
    reasons: list[str] = Field(
        ..., description="整批拒绝的全部原因（重复编号 / 新增已存在 / 目标不存在）"
    )
