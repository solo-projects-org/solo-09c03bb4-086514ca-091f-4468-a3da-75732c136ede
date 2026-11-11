"""更正核算测试：14 天窗口规则、更正批次校验、API 状态码与 Swagger 示例。"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import correction
from app.correction import CaseRecord
from app.main import app
from app.schemas import EX_ADD_REPLACE, EX_BATCH_CONFLICT, EX_CROSS_MONTH_REVOKE

client = TestClient(app)


def rec(
    record_id: str,
    person_id: str = "P1",
    day: str = "2026-01-01",
    township: str = "城东街道",
    age_group: str = "18-59岁",
) -> CaseRecord:
    y, m, d = (int(part) for part in day.split("-"))
    return CaseRecord(
        record_id=record_id,
        person_id=person_id,
        sample_date=date(y, m, d),
        township=township,
        age_group=age_group,
    )


# ---------------------------------------------------------------------------
# 窗口计数规则
# ---------------------------------------------------------------------------


def test_window_covers_14_days_inclusive():
    # 采样日为第 1 天：第 14 天（+13）仍被覆盖，第 15 天（+14）开新窗
    records = [
        rec("R1", day="2026-01-01"),
        rec("R2", day="2026-01-14"),  # 窗口内，不计
        rec("R3", day="2026-01-15"),  # 第 15 天，开新窗
    ]
    counts = correction.count_cases(records)
    assert counts == {("2026-01", "城东街道", "18-59岁"): 2}


def test_same_day_tie_break_by_record_id():
    # 同日两条记录，字典序较小的记录号成为首条，病例归因到它的乡镇
    records = [
        rec("R002", day="2026-05-10", township="甲镇"),
        rec("R001", day="2026-05-10", township="乙镇"),
    ]
    counts = correction.count_cases(records)
    assert counts == {("2026-05", "乙镇", "18-59岁"): 1}


def test_windows_are_per_person():
    records = [
        rec("R1", person_id="P1", day="2026-01-01"),
        rec("R2", person_id="P2", day="2026-01-05"),  # 不同人，互不影响
    ]
    counts = correction.count_cases(records)
    assert counts == {("2026-01", "城东街道", "18-59岁"): 2}


def test_month_attributed_by_first_record_of_window():
    # 窗口跨月：病例计入首条记录的采样月
    records = [
        rec("R1", day="2026-01-31"),
        rec("R2", day="2026-02-05"),  # 被 1 月 31 日的窗口覆盖
    ]
    counts = correction.count_cases(records)
    assert counts == {("2026-01", "城东街道", "18-59岁"): 1}


# ---------------------------------------------------------------------------
# 更正批次应用与差额
# ---------------------------------------------------------------------------


def test_cross_month_revoke_shifts_case_to_next_month():
    before = [
        rec("A1", day="2026-01-31"),
        rec("A2", day="2026-02-10"),
    ]
    result = correction.compute_impact(before, [], [], ["A1"])
    assert result["status"] == "ok"
    assert result["window_days"] == 14
    assert result["deltas"] == [
        {"month": "2026-01", "township": "城东街道", "age_group": "18-59岁", "delta": -1},
        {"month": "2026-02", "township": "城东街道", "age_group": "18-59岁", "delta": 1},
    ]


def test_replace_into_existing_window_deduplicates():
    before = [
        rec("A1", day="2026-03-01"),
        rec("A2", day="2026-03-20"),
    ]
    replacement = [rec("A2", day="2026-03-10")]  # 改回 3 月 10 日，落入首窗
    result = correction.compute_impact(before, [], replacement, [])
    assert result["deltas"] == [
        {"month": "2026-03", "township": "城东街道", "age_group": "18-59岁", "delta": -1}
    ]


def test_add_backdated_record_rewindows_later_records():
    before = [rec("A1", day="2026-06-20")]
    adds = [rec("A0", day="2026-06-01")]  # 补录更早记录，6 月 20 日自立新窗
    result = correction.compute_impact(before, adds, [], [])
    assert result["deltas"] == [
        {"month": "2026-06", "township": "城东街道", "age_group": "18-59岁", "delta": 1}
    ]


def test_replace_can_move_record_to_another_person_and_township():
    before = [rec("A1", person_id="P1", day="2026-01-05", township="甲镇")]
    replacement = [
        rec("A1", person_id="P2", day="2026-01-08", township="乙镇", age_group="0-17岁")
    ]
    result = correction.compute_impact(before, [], replacement, [])
    assert result["deltas"] == [
        {"month": "2026-01", "township": "乙镇", "age_group": "0-17岁", "delta": 1},
        {"month": "2026-01", "township": "甲镇", "age_group": "18-59岁", "delta": -1},
    ]


def test_identical_replace_yields_no_deltas():
    before = [rec("A1", day="2026-01-05")]
    result = correction.compute_impact(before, [], [rec("A1", day="2026-01-05")], [])
    assert result["deltas"] == []
    assert result["delta_units"] == 0


def test_empty_batch_yields_no_deltas():
    before = [rec("A1", day="2026-01-05")]
    result = correction.compute_impact(before, [], [], [])
    assert result["deltas"] == []


def test_zero_deltas_are_omitted():
    # 更正只影响 1 月单元；另一个人的 1 月单元不变，不得出现在结果中
    before = [
        rec("A1", person_id="P1", day="2026-01-31", township="甲镇"),
        rec("A2", person_id="P1", day="2026-02-10", township="甲镇"),
        rec("B1", person_id="P2", day="2026-01-15", township="乙镇"),
    ]
    result = correction.compute_impact(before, [], [], ["A1"])
    keys = {(d["month"], d["township"], d["age_group"]) for d in result["deltas"]}
    assert ("2026-01", "乙镇", "18-59岁") not in keys
    assert all(d["delta"] != 0 for d in result["deltas"])


# ---------------------------------------------------------------------------
# 批次校验：整批报错
# ---------------------------------------------------------------------------


def test_duplicate_in_original_records_rejected():
    before = [rec("A1", day="2026-01-01"), rec("A1", day="2026-01-02")]
    with pytest.raises(correction.CorrectionConflict) as exc_info:
        correction.compute_impact(before, [], [], [])
    assert any("原记录中检验记录号重复：A1" in r for r in exc_info.value.reasons)


def test_duplicate_within_batch_rejected():
    before = [rec("A1", day="2026-01-01")]
    adds = [rec("A2", day="2026-01-03")]
    with pytest.raises(correction.CorrectionConflict) as exc_info:
        correction.compute_impact(before, adds, [], ["A2"])  # add 与 revoke 同号
    assert any("更正批次内检验记录号重复：A2" in r for r in exc_info.value.reasons)


def test_add_existing_id_rejected():
    before = [rec("A1", day="2026-01-01")]
    with pytest.raises(correction.CorrectionConflict) as exc_info:
        correction.compute_impact(before, [rec("A1", day="2026-01-09")], [], [])
    assert any("新增的检验记录号已存在：A1" in r for r in exc_info.value.reasons)


def test_replace_or_revoke_missing_id_rejected():
    before = [rec("A1", day="2026-01-01")]
    with pytest.raises(correction.CorrectionConflict) as exc_info:
        correction.compute_impact(before, [], [rec("A9", day="2026-01-09")], ["A8"])
    reasons = exc_info.value.reasons
    assert any("找不到要替换的记录：A9" in r for r in reasons)
    assert any("找不到要撤销的记录：A8" in r for r in reasons)


def test_all_violations_reported_together():
    # 多种问题并存时一次性返回全部原因（整批拒绝，不部分应用）
    before = [rec("A1", day="2026-01-01"), rec("A1", day="2026-01-02")]
    with pytest.raises(correction.CorrectionConflict) as exc_info:
        correction.compute_impact(
            before,
            [rec("A1", day="2026-01-09")],  # 新增已存在
            [],
            ["A9"],  # 撤销目标不存在
        )
    reasons = exc_info.value.reasons
    assert len(reasons) == 3
    assert any("原记录中检验记录号重复" in r for r in reasons)
    assert any("新增的检验记录号已存在" in r for r in reasons)
    assert any("找不到要撤销的记录" in r for r in reasons)


# ---------------------------------------------------------------------------
# API 层
# ---------------------------------------------------------------------------


def test_api_cross_month_revoke_example():
    resp = client.post("/api/v1/case-count-correction", json=EX_CROSS_MONTH_REVOKE)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["window_days"] == 14
    assert body["delta_units"] == 2
    assert body["deltas"] == [
        {"month": "2026-01", "township": "城东街道", "age_group": "18-59岁", "delta": -1},
        {"month": "2026-02", "township": "城东街道", "age_group": "18-59岁", "delta": 1},
    ]


def test_api_add_and_replace_example():
    resp = client.post("/api/v1/case-count-correction", json=EX_ADD_REPLACE)
    assert resp.status_code == 200
    assert resp.json()["deltas"] == [
        {"month": "2026-03", "township": "河口镇", "age_group": "0-17岁", "delta": -1},
        {"month": "2026-03", "township": "河口镇", "age_group": "18-59岁", "delta": 1},
    ]


def test_api_batch_conflict_returns_409_with_all_reasons():
    resp = client.post("/api/v1/case-count-correction", json=EX_BATCH_CONFLICT)
    assert resp.status_code == 409
    body = resp.json()
    assert body["status"] == "conflict"
    assert len(body["reasons"]) == 2
    assert any("新增的检验记录号已存在：JC2026040101" in r for r in body["reasons"])
    assert any("找不到要撤销的记录：JC2026049999" in r for r in body["reasons"])
    # 整批报错：不返回任何差额
    assert "deltas" not in body


def test_api_validation_errors_422():
    base = {"corrections": {"add": [], "replace": [], "revoke": []}}

    # 空记录号 / 纯空白记录号（strip 后为空）
    for bad_id in ("", "   "):
        resp = client.post(
            "/api/v1/case-count-correction",
            json={
                **base,
                "records": [
                    {
                        "record_id": bad_id,
                        "person_id": "P1",
                        "sample_date": "2026-01-01",
                        "township": "甲镇",
                        "age_group": "18-59岁",
                    }
                ],
            },
        )
        assert resp.status_code == 422

    # 非法日期
    resp = client.post(
        "/api/v1/case-count-correction",
        json={
            **base,
            "records": [
                {
                    "record_id": "R1",
                    "person_id": "P1",
                    "sample_date": "2026-13-01",
                    "township": "甲镇",
                    "age_group": "18-59岁",
                }
            ],
        },
    )
    assert resp.status_code == 422

    # 缺少 corrections 字段
    resp = client.post("/api/v1/case-count-correction", json={"records": []})
    assert resp.status_code == 422


def test_openapi_contains_correction_endpoint_and_examples():
    schema = client.get("/openapi.json").json()
    post = schema["paths"]["/api/v1/case-count-correction"]["post"]
    examples = post["requestBody"]["content"]["application/json"]["examples"]
    summaries = {e["summary"] for e in examples.values()}
    # Swagger 中可直接提交跨月撤销样例
    assert any("跨月撤销" in s for s in summaries)
    assert "409" in post["responses"]
