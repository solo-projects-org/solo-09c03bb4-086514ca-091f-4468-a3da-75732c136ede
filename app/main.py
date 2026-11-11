"""FastAPI 应用：遮盖值反推核查 + 阳性记录更正差额核算服务。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse

from . import correction, solver
from .schemas import (
    EX_ADD_REPLACE,
    EX_BATCH_CONFLICT,
    EX_CROSS_MONTH_REVOKE,
    EX_HALL_CONFLICT,
    EX_RANGE,
    EX_RECOVERABLE,
    EX_TOTAL_CONFLICT,
    ConflictResponse,
    CorrectionConflictResponse,
    CorrectionImpactRequest,
    CorrectionImpactResponse,
    FeasibleResponse,
    HealthResponse,
    SuppressionRequest,
)

app = FastAPI(
    title="医院月报数据核查 API",
    description=(
        "面向医院数仓工程师的两个核查接口：\n\n"
        "1. **遮盖核查**：发布分年龄组、乡镇的病例计数前，检查 null 遮盖格"
        "能否被公开的行/列合计精确反推；\n"
        "2. **更正核算**：阳性检验记录入仓后发生更正（新增 / 整条替换 / "
        "撤销）时，按 14 天窗口规则重算病例数，返回各「采样月 × 乡镇 × "
        "年龄组」月报单元需要补计或扣减的非零差额。"
    ),
    version="1.1.0",
)


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["运维"],
    summary="健康检查",
)
def health() -> dict:
    return {"status": "ok", "service": "suppression-check"}


@app.post(
    "/api/v1/suppression-check",
    response_model=FeasibleResponse,
    responses={409: {"model": ConflictResponse, "description": "约束无解"}},
    tags=["核查"],
    summary="提交计数矩阵与公开合计，核查隐藏格反推风险",
)
def suppression_check(
    payload: Annotated[
        SuppressionRequest,
        Body(
            openapi_examples={
                "feasible_range": {
                    "summary": "可行：隐藏格存在取值区间（不可还原）",
                    "description": "3x3 矩阵，6 个遮盖格只能确定到区间，工程师需调整遮盖位置",
                    "value": EX_RANGE,
                },
                "feasible_recoverable": {
                    "summary": "可行：隐藏格被合计唯一锁定（可还原）",
                    "description": "4 个遮盖格的 min == max，存在精确反推风险",
                    "value": EX_RECOVERABLE,
                },
                "conflict_totals": {
                    "summary": "冲突：行列总合计不相等",
                    "value": EX_TOTAL_CONFLICT,
                },
                "conflict_hall": {
                    "summary": "冲突：总量相等但结构无解（Hall 条件）",
                    "description": "最大流小于所需流量，返回最小割冲突见证",
                    "value": EX_HALL_CONFLICT,
                },
            }
        ),
    ]
):
    try:
        result = solver.analyze(
            matrix=payload.matrix,
            row_totals=payload.row_totals,
            col_totals=payload.col_totals,
        )
    except solver.SuppressionConflict as exc:
        return JSONResponse(
            status_code=409,
            content={
                "status": "conflict",
                "reasons": exc.reasons,
                "witness": exc.witness,
            },
        )
    return result


@app.get(
    "/method",
    tags=["核查"],
    summary="算法说明",
)
def method() -> dict:
    return solver.fraction_explanation()


def _to_case_records(records) -> list[correction.CaseRecord]:
    return [correction.CaseRecord(**rec.model_dump()) for rec in records]


@app.post(
    "/api/v1/case-count-correction",
    response_model=CorrectionImpactResponse,
    responses={
        409: {
            "model": CorrectionConflictResponse,
            "description": "更正批次校验失败，整批拒绝",
        }
    },
    tags=["更正核算"],
    summary="提交更正前阳性记录与更正批次，返回月报单元补计/扣减差额",
)
def case_count_correction(
    payload: Annotated[
        CorrectionImpactRequest,
        Body(
            openapi_examples={
                "cross_month_revoke": {
                    "summary": "跨月撤销：病例从 1 月移到 2 月",
                    "description": (
                        "撤销 1 月 31 日的首诊记录后，2 月 10 日的记录自立"
                        " 14 天窗口：1 月单元扣减 1，2 月单元补计 1"
                    ),
                    "value": EX_CROSS_MONTH_REVOKE,
                },
                "add_and_replace": {
                    "summary": "新增 + 整条替换",
                    "description": (
                        "替换把 3 月 20 日的记录改回 3 月 10 日，落入既有"
                        "窗口不再单独计例；同时为另一人新增一条记录"
                    ),
                    "value": EX_ADD_REPLACE,
                },
                "batch_conflict": {
                    "summary": "整批报错：新增编号已存在且撤销目标不存在",
                    "description": "任一校验失败即整批拒绝（HTTP 409），一次性返回全部原因",
                    "value": EX_BATCH_CONFLICT,
                },
            }
        ),
    ]
):
    try:
        return correction.compute_impact(
            records=_to_case_records(payload.records),
            adds=_to_case_records(payload.corrections.add),
            replaces=_to_case_records(payload.corrections.replace),
            revokes=list(payload.corrections.revoke),
        )
    except correction.CorrectionConflict as exc:
        return JSONResponse(
            status_code=409,
            content={"status": "conflict", "reasons": exc.reasons},
        )
