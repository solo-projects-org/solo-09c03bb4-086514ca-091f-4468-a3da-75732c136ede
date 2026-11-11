"""阳性检验记录更正的月报差额核算。

业务背景
--------
阳性检验记录入仓后仍会被更正（按检验记录号新增、整条替换、撤销）。
医院数仓工程师需要知道哪些"采样月 × 乡镇 × 年龄组"月报单元需要
补计（正差额）或扣减（负差额）。本模块对更正前记录与更正批次分别
核算病例数，只输出更正后 − 更正前的非零差额。

病例窗口规则
------------
按人（脱敏人员编号）独立开窗：

1. 将该人员的记录按（采样日，检验记录号字典序升序）排列；
2. 取最早一条尚未被既有窗口覆盖的记录开窗，其采样日为 14 天窗口的
   第 1 天，即窗口覆盖 [采样日, 采样日 + 13 天]（含两端）；
3. 每个窗口只计首条记录为 1 例，归因到首条记录的采样月、乡镇与
   年龄组；窗口内其余记录不计数；
4. 遇到下一条未被覆盖的记录时重复第 2 步。

更正批次校验（整批原子）
------------------------
以下任一情况整批拒绝，一次性返回全部冲突原因，不做任何部分应用：

* 原记录或更正批次内部检验记录号重复；
* 新增的检验记录号已存在；
* 找不到要替换或撤销的记录。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# 14 天窗口：采样日为第 1 天，窗口覆盖采样日起共 14 个自然日。
WINDOW_DAYS = 14


class CorrectionConflict(ValueError):
    """更正批次校验失败（整批拒绝）时抛出，携带全部冲突原因。"""

    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons) if reasons else "invalid correction batch")
        self.reasons = reasons


@dataclass(frozen=True)
class CaseRecord:
    """一条阳性检验记录（更正前原始记录或更正批次中的完整记录）。"""

    record_id: str  # 检验记录号
    person_id: str  # 脱敏人员编号
    sample_date: date  # 采样日
    township: str  # 乡镇
    age_group: str  # 年龄组


def count_cases(records: list[CaseRecord]) -> dict[tuple[str, str, str], int]:
    """按人开 14 天窗口计数。

    返回 ``{(采样月 YYYY-MM, 乡镇, 年龄组): 病例数}``，病例归因到每个
    窗口首条记录（该窗口内采样日最早、同日取记录号字典序最小者）。
    """
    by_person: dict[str, list[CaseRecord]] = {}
    for rec in records:
        by_person.setdefault(rec.person_id, []).append(rec)

    counts: dict[tuple[str, str, str], int] = {}
    for person_records in by_person.values():
        # 同日按检验记录号字典序升序
        ordered = sorted(person_records, key=lambda r: (r.sample_date, r.record_id))
        window_end: date | None = None
        for rec in ordered:
            if window_end is not None and rec.sample_date <= window_end:
                continue  # 仍被上一窗口覆盖，不计数
            # 开新窗：采样日为 14 天窗口的第 1 天
            window_end = rec.sample_date + timedelta(days=WINDOW_DAYS - 1)
            key = (rec.sample_date.strftime("%Y-%m"), rec.township, rec.age_group)
            counts[key] = counts.get(key, 0) + 1
    return counts


def apply_corrections(
    records: list[CaseRecord],
    adds: list[CaseRecord],
    replaces: list[CaseRecord],
    revokes: list[str],
) -> list[CaseRecord]:
    """校验并应用更正批次，返回更正后的完整记录集合。

    任一校验失败即整批拒绝：抛出 :class:`CorrectionConflict` 并携带全部
    冲突原因，不应用任何更正。
    """
    reasons: list[str] = []

    # 1) 原记录内部重复编号
    original_counts: dict[str, int] = {}
    for rec in records:
        original_counts[rec.record_id] = original_counts.get(rec.record_id, 0) + 1
    for rid in sorted(rid for rid, n in original_counts.items() if n > 1):
        reasons.append(
            f"原记录中检验记录号重复：{rid}（出现 {original_counts[rid]} 次）"
        )

    # 2) 更正批次内部重复编号（add / replace / revoke 三个操作流合并检查）
    batch_ops: dict[str, list[str]] = {}
    for rec in adds:
        batch_ops.setdefault(rec.record_id, []).append("add")
    for rec in replaces:
        batch_ops.setdefault(rec.record_id, []).append("replace")
    for rid in revokes:
        batch_ops.setdefault(rid, []).append("revoke")
    for rid in sorted(rid for rid, ops in batch_ops.items() if len(ops) > 1):
        reasons.append(
            f"更正批次内检验记录号重复：{rid}（出现在 "
            f"{'、'.join(batch_ops[rid])}）"
        )

    original_ids = set(original_counts)

    # 3) 新增编号已存在
    for rec in adds:
        if rec.record_id in original_ids:
            reasons.append(f"新增的检验记录号已存在：{rec.record_id}")

    # 4) 找不到要替换或撤销的记录
    for rec in replaces:
        if rec.record_id not in original_ids:
            reasons.append(f"找不到要替换的记录：{rec.record_id}")
    for rid in revokes:
        if rid not in original_ids:
            reasons.append(f"找不到要撤销的记录：{rid}")

    if reasons:
        raise CorrectionConflict(reasons)

    corrected = {rec.record_id: rec for rec in records}
    for rid in revokes:
        del corrected[rid]
    for rec in replaces:  # 整条替换：记录号不变，其余字段整体覆盖
        corrected[rec.record_id] = rec
    for rec in adds:
        corrected[rec.record_id] = rec
    return list(corrected.values())


def compute_impact(
    records: list[CaseRecord],
    adds: list[CaseRecord],
    replaces: list[CaseRecord],
    revokes: list[str],
) -> dict:
    """核算更正影响，返回可直接序列化为 API 响应的字典。

    分别对更正前、更正后的记录集按（采样月, 乡镇, 年龄组）统计病例数，
    只保留更正后 − 更正前的非零差额。批次校验失败时抛出
    :class:`CorrectionConflict`。
    """
    corrected = apply_corrections(records, adds, replaces, revokes)
    before = count_cases(records)
    after = count_cases(corrected)

    deltas: list[dict] = []
    for key in sorted(set(before) | set(after)):
        diff = after.get(key, 0) - before.get(key, 0)
        if diff == 0:
            continue  # 只返回非零差额
        month, township, age_group = key
        deltas.append(
            {
                "month": month,
                "township": township,
                "age_group": age_group,
                "delta": diff,
            }
        )

    return {
        "status": "ok",
        "window_days": WINDOW_DAYS,
        "delta_units": len(deltas),
        "deltas": deltas,
    }
