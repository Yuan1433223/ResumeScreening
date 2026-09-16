# -*- coding: utf-8 -*-
"""P0 合成回归测试：对应 DEVELOPMENT_SPEC.md §10/§11。

覆盖：空正文绝不推荐、正文仅表态、6年+5年年限、重叠日期、英语否定、
重复行业词、解析器异常，以及 59/60/74/75 路由边界。
所有样例与真实候选人姓名无关。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.config import JOB_PROFILE
from src.schemas import Employment, Fact, FactStatus, HardResult, Recommendation
from src.scorer import (
    assess,
    compare_years,
    evaluate_hard_rules,
    merge_employment_months,
)
from src.workflow import run_single


def F(kind, value, status=FactStatus.SUPPORTED, fid=None):
    return Fact(id=fid or f"f_{kind}_{abs(hash(kind + str(value)))}", kind=kind, value=value, status=status)


def E(start, end, relevant=None, is_current=False):
    return Employment(start_month=start, end_month=end, relevant=relevant, is_current=is_current)


# ---------------------------------------------------------------------------
# 1. 空白 PDF 但文件名含钢结构/MiC → 解析失败/空正文 → needs_review，绝不推荐
# ---------------------------------------------------------------------------
def test_blank_pdf_with_steel_filename_goes_review(tmp_path):
    p = tmp_path / "张三 钢结构10年以上.pdf"
    p.write_bytes(b"%PDF-1.4\n%%EOF")  # 无法解析的假 PDF
    report = run_single(str(p), thread_id="t_blank_pdf")
    assert report["解析状态"] in ("failed", "needs_review")
    assert report["机器建议"] == "needs_review"
    assert report["机器建议"] != "recommend"
    # 文件名年限不进入评分
    assert report.get("综合评分") is None


# ---------------------------------------------------------------------------
# 2. 正文仅写“希望从事海外项目” → 相关经验 unknown → needs_review
# ---------------------------------------------------------------------------
def test_only_aspiration_text_goes_review():
    a = assess([], [], JOB_PROFILE, assessment_id="t_aspire")
    assert a.hard_results["relevant_domain"] == HardResult.UNKNOWN
    assert a.hard_summary == HardResult.UNKNOWN
    assert a.recommendation == Recommendation.NEEDS_REVIEW


# ---------------------------------------------------------------------------
# 3. 总经验 6 年且直接相关 5 年并有管理经历 → 年限通过
#    2 年不相关 + 4.75 年相关(当前) = 6.75 年总；直接相关 4.75 年
# ---------------------------------------------------------------------------
def _six_and_five_emps():
    return [
        E("2019-01", "2020-12", relevant=False),              # 2 年不相关
        E("2021-01", "2026-09", relevant=True, is_current=True),  # 5.75 年相关
    ]


def test_six_years_total_five_relevant_passes_experience():
    facts = [F("relevant_domain", True), F("management", "managed")]
    hard = evaluate_hard_rules(facts, _six_and_five_emps(), JOB_PROFILE)
    # 总 7.75 年 < 8 → 单项 FAIL；直接相关 5.75 年 >= 5 → PASS；OR → PASS
    assert hard["experience"] == HardResult.PASS
    assert hard["summary"] == HardResult.PASS


def test_six_years_total_five_relevant_route():
    facts = [
        F("relevant_domain", True),
        F("management", "managed"),
        F("steel_mic_experience", "participated"),
    ]
    a = assess(facts, _six_and_five_emps(), JOB_PROFILE, assessment_id="t_6y5y")
    assert a.hard_results["experience"] == HardResult.PASS
    # 硬条件通过但评分不完整 → needs_review（保守复核）
    assert a.recommendation == Recommendation.NEEDS_REVIEW


# ---------------------------------------------------------------------------
# 4. 多个任职日期重叠 → 不重复计数
# ---------------------------------------------------------------------------
def test_overlapping_employments_not_double_counted():
    emps = [
        E("2018-01", "2020-12", relevant=True),   # 36 个月
        E("2020-06", "2022-12", relevant=True),   # 重叠 6 个月
    ]
    t_min, t_max, r_min, r_max = merge_employment_months(emps, "2026-09-14")
    # 并集：2018-01 ~ 2022-12 = 60 个月（两段相加 66，必须去重）
    assert (t_min, t_max, r_min, r_max) == (60, 60, 60, 60)

# ---------------------------------------------------------------------------
# 5. 英语明确写为不熟练 → 否定句不因关键词加分
# ---------------------------------------------------------------------------
def test_explicit_not_fluent_english_scores_zero():
    facts = [
        F("relevant_domain", True),
        F("management", "managed"),   # 5 分
        F("english", "not_fluent", FactStatus.CONTRADICTED),
    ]
    emps = [E("2015-01", "2026-09", relevant=True, is_current=True)]
    a = assess(facts, emps, JOB_PROFILE, assessment_id="t_eng_neg")
    eng = a.dimensions["english"]
    assert eng.status == FactStatus.CONTRADICTED
    assert eng.score == 0
    # 英语计 0 分，而非因“英语”关键词得 2/5 分
    assert a.known_score == 5


# ---------------------------------------------------------------------------
# 6. 出现重复行业词 → 每维取最高档位，不叠加
# ---------------------------------------------------------------------------
def test_repeated_keywords_take_max_level_only():
    facts = [
        F("relevant_domain", True),
        F("management", "managed"),
        F("steel_mic_experience", "participated"),
        F("steel_mic_experience", "responsible"),   # 最高档 20
        F("steel_mic_experience", "participated"),  # 重复出现
    ]
    emps = [E("2015-01", "2026-09", relevant=True, is_current=True)]
    a = assess(facts, emps, JOB_PROFILE, assessment_id="t_repeat")
    dim = a.dimensions["steel_mic_experience"]
    assert dim.score == 20
    assert len(dim.matched_facts) == 1  # 只计一次


# ---------------------------------------------------------------------------
# 7. 解析器异常 → 不崩溃，返回待复核报告
# ---------------------------------------------------------------------------
def test_parser_exception_returns_review(tmp_path):
    p = tmp_path / "weird.bin"
    p.write_bytes(b"\x00\x01\x02 not a resume")
    report = run_single(str(p), thread_id="t_exc")
    assert report["解析状态"] in ("failed", "needs_review")
    assert report["机器建议"] == "needs_review"
    assert report["机器建议"] != "recommend"


# ---------------------------------------------------------------------------
# 路由边界：59/60/74/75；分数不超过 100
# ---------------------------------------------------------------------------
def _full_emps():
    return [E("2010-01", "2026-09", relevant=True, is_current=True)]


def _facts_from_scores(s):
    """按维度分映射回事实值。s: dict[dim, score]"""
    mapping = {
        "steel_mic_experience": {10: "participated", 20: "responsible", 30: "led"},
        "steel_full_flow": {5: "one_phase", 10: "two_phases", 15: "full_flow"},
        "overseas": {5: "collab", 10: "responsible", 15: "delivered"},
        "management": {5: "managed", 10: "clear_scope", 15: "scope_and_results"},
        "quality_safety": {5: "one_duty", 10: "two_plus"},
        "english": {2: "info", 5: "usage"},
        "project_scale": {5: "quantified"},
        "mobility": {2: "location_ok", 5: "travel_ok"},
    }
    facts = [F("relevant_domain", True)]
    for dim, score in s.items():
        facts.append(F(dim, mapping[dim][score]))
    return facts


def test_max_score_recommend():
    s = {d: cfg["max"] for d, cfg in JOB_PROFILE.rubric.items()}
    a = assess(_facts_from_scores(s), _full_emps(), JOB_PROFILE, assessment_id="t_max")
    assert a.known_score == 100
    assert a.score_complete is True
    assert a.recommendation == Recommendation.RECOMMEND


def test_score_never_exceeds_100():
    assert sum(cfg["max"] for cfg in JOB_PROFILE.rubric.values()) == 100


@pytest.mark.parametrize(
    "scores,expected_rec",
    [
        # 59 分且完整 → hold
        ({"steel_mic_experience": 30, "steel_full_flow": 5, "overseas": 5,
          "management": 5, "quality_safety": 5, "english": 2,
          "project_scale": 5, "mobility": 2}, Recommendation.HOLD),
        # 60 分且完整 → needs_review
        ({"steel_mic_experience": 20, "steel_full_flow": 10, "overseas": 5,
          "management": 5, "quality_safety": 5, "english": 5,
          "project_scale": 5, "mobility": 5}, Recommendation.NEEDS_REVIEW),
        # 74 分且完整 → needs_review
        ({"steel_mic_experience": 20, "steel_full_flow": 15, "overseas": 15,
          "management": 10, "quality_safety": 5, "english": 2,
          "project_scale": 5, "mobility": 2}, Recommendation.NEEDS_REVIEW),
        # 75 分且完整 → recommend
        ({"steel_mic_experience": 20, "steel_full_flow": 10, "overseas": 15,
          "management": 10, "quality_safety": 5, "english": 5,
          "project_scale": 5, "mobility": 5}, Recommendation.RECOMMEND),
    ],
)
def test_route_boundaries(scores, expected_rec):
    facts = _facts_from_scores(scores)
    a = assess(facts, _full_emps(), JOB_PROFILE, assessment_id=f"t_{sum(scores.values())}")
    assert a.score_complete is True
    assert a.known_score == sum(scores.values())
    assert a.recommendation == expected_rec


def test_incomplete_score_never_recommends():
    # 分数高但缺英语维度 → 不完整 → needs_review（保守复核优先）
    facts = _facts_from_scores({
        "steel_mic_experience": 30, "steel_full_flow": 15, "overseas": 15,
        "management": 15, "quality_safety": 10,
        "project_scale": 5, "mobility": 5,
    })  # 缺 english
    a = assess(facts, _full_emps(), JOB_PROFILE, assessment_id="t_incomplete")
    assert a.known_score == 95
    assert a.score_complete is False
    assert "english" in a.missing_items
    assert a.recommendation == Recommendation.NEEDS_REVIEW


# ---------------------------------------------------------------------------
# 硬条件：任一 fail → fail → hold
# ---------------------------------------------------------------------------
def test_hard_fail_when_domain_missing():
    facts = [F("relevant_domain", False), F("management", "managed")]
    emps = [E("2010-01", "2026-09", relevant=True, is_current=True)]
    a = assess(facts, emps, JOB_PROFILE, assessment_id="t_fail")
    assert a.hard_summary == HardResult.FAIL
    assert a.recommendation == Recommendation.HOLD


# ---------------------------------------------------------------------------
# 年限三态：确定下界满足→pass；确定上界不足→fail；跨阈值→unknown
# ---------------------------------------------------------------------------
def test_compare_years_three_states():
    assert compare_years(8 * 12, 8 * 12, 8) == HardResult.PASS
    assert compare_years(0, 7 * 12, 8) == HardResult.FAIL
    assert compare_years(7 * 12, 9 * 12, 8) == HardResult.UNKNOWN


def test_unknown_start_month_bounds():
    # 只有年份"2018"（月份未知），analysis 2026-09
    emps = [E("2018-00", "2026-09", relevant=True, is_current=True)]
    t_min, t_max, _, _ = merge_employment_months(emps, "2026-09-14")
    # 最少：2018-12 底 ~ 2026-09 底 = 94 个月；最多：2018-01 初 ~ 2026-09 底 = 105 个月
    assert (t_min, t_max) == (94, 105)
    # 8 年 = 96 个月：下界不足、上界够 → 跨阈值 unknown
    assert compare_years(t_min, t_max, 8) == HardResult.UNKNOWN
