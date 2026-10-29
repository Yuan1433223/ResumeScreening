# -*- coding: utf-8 -*-
"""基于证据事实的规则评分器（无 LLM）

对应 DEVELOPMENT_SPEC.md §6：
- 三态硬条件：相关领域经验 AND（总>=8 OR 直接相关>=5）AND 项目管理
- 工作年限按正文工作区间的月份并集合并，重叠任职不重复计数
- 8 维评分：每维取最高满足档位；未提及维度 score=None、status=unknown
- known_score 仅累加已确认项；score_complete=False 时禁止显示完整适配率
- 建议路由：硬条件 fail→hold；>=75→recommend；60-74 或信息缺失→needs_review；<60→hold
"""
from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

from .config import SCORING, THRESHOLDS
from .schemas import (
    Assessment,
    DimensionScore,
    Employment,
    Fact,
    FactStatus,
    HardResult,
    JobProfile,
    Recommendation,
)

# ---------------------------------------------------------------------------
# 年限计算
# ---------------------------------------------------------------------------
def _parse_month(month: Optional[str]) -> Optional[Tuple[int, int]]:
    """'2018-03' -> (2018, 2)；'2018-00'（年份已知月份未知）-> (2018, None)。"""
    if not month:
        return None
    parts = str(month).split("-")
    try:
        y = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 and parts[1] else None
        if m == 0:
            m = None  # "00" 表示月份未知
        if m is not None and not (1 <= m <= 12):
            return None
        return (y, m)
    except (ValueError, IndexError):
        return None


def _month_index(year: int, month: int) -> int:
    """0-based 月份序号，便于求差。"""
    return year * 12 + (month - 1)


def employment_bounds(emp: Employment, analysis_date: str) -> Tuple[int, int, int, int]:
    """返回 (min_start, max_start, min_end, max_end) 月份序号。

    区间左闭右开：结束月份按"该月月底+1"计，保证跨度=实际月数。
    月份未知时：最早开始=当年1月，最晚开始=当年12月；结束同理。
    is_current 的任职以 analysis_date 为截止。
    """
    # analysis_date: "YYYY-MM-DD"
    try:
        a_y, a_m, _ = map(int, analysis_date.split("-"))
        a_idx = _month_index(a_y, a_m) + 1  # 当前月月底
    except ValueError:
        a_idx = _month_index(date.today().year, date.today().month) + 1

    s = _parse_month(emp.start_month)
    e = _parse_month(emp.end_month)

    if s is None:
        # 无起始信息：下界未知，上界给一个极端（无法确定）。用 0 表示"最早"，max 用很大
        s_min, s_max = 0, a_idx
    else:
        y, m = s
        s_min = _month_index(y, m) if m else _month_index(y, 1)
        s_max = _month_index(y, m) if m else _month_index(y, 12)

    if emp.is_current:
        e_min = e_max = a_idx
    elif e is None:
        e_min, e_max = 0, a_idx
    else:
        y, m = e
        e_min = _month_index(y, m) + 1 if m else _month_index(y, 2)
        e_max = _month_index(y, m) + 1 if m else _month_index(y, 13)

    return s_min, s_max, e_min, e_max


def _merge_span_months(spans: List[Tuple[int, int]]) -> int:
    """合并重叠区间并求总月数（重叠不重复计数）。"""
    if not spans:
        return 0
    ordered = sorted((a, b) for a, b in spans if b > a)
    total = 0
    cur_a, cur_b = ordered[0]
    for a, b in ordered[1:]:
        if a <= cur_b:
            cur_b = max(cur_b, b)
        else:
            total += cur_b - cur_a
            cur_a, cur_b = a, b
    total += cur_b - cur_a
    return total


def merge_employment_months(
    employments: Sequence[Employment], analysis_date: str
) -> Tuple[int, int, int, int]:
    """合并全部任职，返回 (总年限下界, 总年限上界, 直接相关下界, 直接相关上界)，单位：月。"""
    total_min_spans: List[Tuple[int, int]] = []
    total_max_spans: List[Tuple[int, int]] = []
    rel_min_spans: List[Tuple[int, int]] = []
    rel_max_spans: List[Tuple[int, int]] = []

    for emp in employments:
        s_min, s_max, e_min, e_max = employment_bounds(emp, analysis_date)
        # 下界场景：开始取最晚、结束取最早（工作年限最保守）
        lo = (s_max, e_min)
        # 上界场景：开始取最早、结束取最晚
        hi = (s_min, e_max)
        if lo[1] > lo[0]:
            total_min_spans.append(lo)
        if hi[1] > hi[0]:
            total_max_spans.append(hi)
        if emp.relevant:
            if lo[1] > lo[0]:
                rel_min_spans.append(lo)
            if hi[1] > hi[0]:
                rel_max_spans.append(hi)

    return (
        _merge_span_months(total_min_spans),
        _merge_span_months(total_max_spans),
        _merge_span_months(rel_min_spans),
        _merge_span_months(rel_max_spans),
    )


def compare_years(months_min: int, months_max: int, threshold_years: int) -> HardResult:
    """三态比较：确定下界已满足→pass；确定上界仍不足→fail；跨阈值→unknown。"""
    thr = threshold_years * 12
    if months_min >= thr:
        return HardResult.PASS
    if months_max < thr:
        return HardResult.FAIL
    return HardResult.UNKNOWN


# ---------------------------------------------------------------------------
# 三态组合
# ---------------------------------------------------------------------------
def and3(results: Sequence[HardResult]) -> HardResult:
    """AND：任一 fail 则 fail，全 pass 才 pass，其余 unknown。"""
    if any(r == HardResult.FAIL for r in results):
        return HardResult.FAIL
    if all(r == HardResult.PASS for r in results):
        return HardResult.PASS
    return HardResult.UNKNOWN


def or3(results: Sequence[HardResult]) -> HardResult:
    """OR：任一 pass 则 pass，全 fail 才 fail，其余 unknown。"""
    if any(r == HardResult.PASS for r in results):
        return HardResult.PASS
    if all(r == HardResult.FAIL for r in results):
        return HardResult.FAIL
    return HardResult.UNKNOWN


# ---------------------------------------------------------------------------
# 硬条件评估
# ---------------------------------------------------------------------------
def _fact_value(facts: Sequence[Fact], kind: str) -> List[object]:
    return [f.value for f in facts if f.kind == kind and f.status == FactStatus.SUPPORTED]


def evaluate_hard_rules(
    facts: Sequence[Fact], employments: Sequence[Employment], profile: JobProfile
) -> Dict[str, HardResult]:
    """按岗位配置评估三态硬条件。"""
    hard = profile.hard_rules

    # 1) 相关领域经验
    dom_vals = _fact_value(facts, hard["relevant_domain"]["fact_kind"])
    if dom_vals:
        relevant_domain = HardResult.PASS if bool(dom_vals[-1]) else HardResult.FAIL
    else:
        relevant_domain = HardResult.UNKNOWN

    # 2) 总经验 OR 直接相关经验
    if not employments:
        # 无任职记录 ≠ 确定不足：无证据视为 unknown（规格 6.1：确定上界仍不足才 fail）
        experience = HardResult.UNKNOWN
    else:
        total_min, total_max, rel_min, rel_max = merge_employment_months(
            employments, profile.analysis_date
        )
        total_ok = compare_years(total_min, total_max, hard["experience"]["total_years_ge"])
        rel_ok = compare_years(rel_min, rel_max, hard["experience"]["relevant_years_ge"])
        experience = or3([total_ok, rel_ok])

    # 3) 项目/团队管理
    mgmt_vals = _fact_value(facts, hard["management"]["fact_kind"])
    if mgmt_vals:
        management = HardResult.PASS if mgmt_vals[-1] else HardResult.FAIL
    else:
        management = HardResult.UNKNOWN

    results = {
        "relevant_domain": relevant_domain,
        "experience": experience,
        "management": management,
    }
    results["summary"] = and3(list(results.values()))
    return results


# ---------------------------------------------------------------------------
# 8 维评分
# ---------------------------------------------------------------------------
def score_dimension(facts: Sequence[Fact], dim_name: str, dim_cfg: Dict) -> DimensionScore:
    """对单维评分：取该维度（kind==dim_name）事实中最高命中档位；未提及→unknown；反向事实→contradicted。"""
    max_score = dim_cfg["max"]
    levels = dim_cfg["levels"]  # [(score, desc, fact_value), ...]

    dim_facts = [f for f in facts if f.kind == dim_name]
    # 按档位事实值匹配
    matched: List[Tuple[int, str, object, str]] = []
    for f in dim_facts:
        if f.status == FactStatus.SUPPORTED:
            for score, desc, val in levels:
                if f.value == val or (isinstance(f.value, str) and f.value.lower() == str(val).lower()):
                    matched.append((score, desc, f.value, f.id))
    if matched:
        # 取最高档；matched_facts 记录命中的事实 ID（可回溯证据原文）
        score, desc, val, fact_id = max(matched, key=lambda x: x[0])
        return DimensionScore(
            score=score,
            max_score=max_score,
            status=FactStatus.SUPPORTED,
            level=desc,
            matched_facts=[fact_id],
        )

    # 反向事实（如英语明确不熟练）
    contradicted = [f for f in dim_facts if f.status == FactStatus.CONTRADICTED]
    if contradicted:
        return DimensionScore(
            score=0,
            max_score=max_score,
            status=FactStatus.CONTRADICTED,
            level="有反向事实，计 0 分",
            matched_facts=[f.id for f in contradicted],
        )

    return DimensionScore(score=None, max_score=max_score, status=FactStatus.UNKNOWN)


def assess(
    facts: Sequence[Fact],
    employments: Sequence[Employment],
    profile: JobProfile,
    assessment_id: str = "a1",
    extractor_version: str = "synthetic",
    document_id: str = "",
) -> Assessment:
    """基于事实与任职记录生成评估。"""
    hard_results = evaluate_hard_rules(facts, employments, profile)

    dimensions: Dict[str, DimensionScore] = {}
    missing: List[str] = []
    for dim, cfg in SCORING.items():
        ds = score_dimension(facts, dim, cfg)
        dimensions[dim] = ds
        if ds.status == FactStatus.UNKNOWN:
            missing.append(dim)

    known_score = sum(d.score for d in dimensions.values() if d.score is not None)
    score_complete = len(missing) == 0

    # 建议路由（§6.4）
    summary = hard_results["summary"]
    if summary == HardResult.FAIL:
        recommendation = Recommendation.HOLD
    elif summary == HardResult.UNKNOWN:
        recommendation = Recommendation.NEEDS_REVIEW
    elif not score_complete:
        recommendation = Recommendation.NEEDS_REVIEW
    elif known_score >= THRESHOLDS["recommend"]:
        recommendation = Recommendation.RECOMMEND
    elif known_score >= THRESHOLDS["needs_review"]:
        recommendation = Recommendation.NEEDS_REVIEW
    else:
        recommendation = Recommendation.HOLD

    return Assessment(
        id=assessment_id,
        document_id=document_id,
        job_version=profile.version,
        extractor_version=extractor_version,
        rubric_version=profile.version,
        hard_results=hard_results,
        hard_summary=summary,
        dimensions=dimensions,
        known_score=known_score,
        score_complete=score_complete,
        missing_items=missing,
        recommendation=recommendation,
    )
