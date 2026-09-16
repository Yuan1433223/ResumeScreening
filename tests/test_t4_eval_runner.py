# -*- coding: utf-8 -*-
"""T4 评测 runner 的指标计算测试（合成数据，不碰真实简历/云端）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from eval.run_eval import (
    DocResult,
    EvalLabel,
    aggregate_stats,
    compute_fact_metrics,
    employment_metrics,
    evidence_quality,
    normalize_fact_value,
    review_triggered,
    truncated_quote,
    year_of,
)
from src.schemas import Evidence, ExtractionReport, Fact, FactStatus, Page


def page(text, n=1):
    return Page(document_id="d", page_number=n, text=text, text_hash="", extraction_method="test")


# ---------------------------------------------------------------------------
# 归一化与事实键
# ---------------------------------------------------------------------------

def test_normalize_fact_value():
    assert normalize_fact_value("total_years", "19") == 19
    assert normalize_fact_value("total_years", 19) == 19
    assert normalize_fact_value("total_years", "18.6") == 19
    assert normalize_fact_value("overseas", "responsible") == "responsible"
    assert normalize_fact_value("overseas", 0) == "0"  # 非年限类一律 str


# ---------------------------------------------------------------------------
# micro 指标
# ---------------------------------------------------------------------------

def _dr(doc_id, gt, extracted):
    return DocResult(
        doc_id=doc_id,
        extractor_version="x",
        gt_facts=gt,
        extracted_keys=extracted,
        matched=extracted & gt,
    )


def test_fact_metrics_micro_math():
    r = _dr("d1",
            gt={("overseas", "responsible"), ("english", "usage"), ("mobility", "travel_ok")},
            extracted={("overseas", "responsible"), ("english", "usage"), ("management", "managed")})
    m = compute_fact_metrics([r])
    assert m["gt_facts"] == 3
    assert m["extracted_facts"] == 3
    assert m["matched"] == 2
    assert m["precision"] == pytest.approx(2 / 3)
    assert m["recall"] == pytest.approx(2 / 3)
    assert m["f1"] == pytest.approx(2 / 3)  # 2*(2/3)*(2/3)/(4/3) = 2/3


def test_fact_metrics_pooled_across_docs():
    """micro 是 pooled：多文档合计后计算，不是各文档平均。"""
    r1 = _dr("a", gt={("overseas", "delivered")}, extracted={("overseas", "delivered")})
    r2 = _dr("b", gt={("english", "usage"), ("mobility", "travel_ok")},
             extracted={("english", "usage")})
    m = compute_fact_metrics([r1, r2])
    assert m["gt_facts"] == 3
    assert m["extracted_facts"] == 2
    assert m["matched"] == 2
    assert m["precision"] == pytest.approx(1.0)
    assert m["recall"] == pytest.approx(2 / 3)


def test_fact_metrics_empty_extracted():
    m = compute_fact_metrics([_dr("a", gt={("overseas", "delivered")}, extracted=set())])
    assert m["precision"] == 0.0
    assert m["recall"] == 0.0
    assert m["f1"] == 0.0


# ---------------------------------------------------------------------------
# 引用完整度启发式
# ---------------------------------------------------------------------------

def test_truncated_quote_heuristic():
    # 中文完整词 + 后接标点 → 不截断
    text = "负责海外钢结构项目交付。英语流利"
    assert truncated_quote(0, 11, text) is False  # "负责海外钢结构项目交付" 后接 "。"
    # 中文词后接汉字（无词边界）→ 不截断
    assert truncated_quote(0, 9, text) is False   # "负责海外钢结构项目交" 后接 "付"
    # ASCII 词被切断 → 截断（真实缺陷：模型输出被截断的引用）
    en = "Support business achievement and delivery"
    assert truncated_quote(0, 27, en) is True     # "Support business achieveme" 后接 "t"
    assert truncated_quote(0, 28, en) is False    # "Support business achievement" 后接 " "
    # 数字被切断 → 截断
    sal = "期望薪资：40-45K"
    assert truncated_quote(0, 6, sal) is True     # "期望薪资：4" 后接 "0"
    # 起点切断在 ASCII 词中间 → 截断
    assert truncated_quote(9, 28, en) is True     # 从 "usiness" 起，前字符 "b"
    # 引文到文末（无后文）→ 不截断
    assert truncated_quote(12, 16, text) is False  # "英语流利" 到文末


def test_evidence_quality_counts():
    p = page("负责海外项目交付，英语流利。")
    good = Evidence(id="e1", document_id="d", page_number=1, start=0, end=8,
                    quote=p.text[0:8])
    bad = Evidence(id="e2", document_id="d", page_number=1, start=0, end=6,
                   quote="不存在的引文")
    f1 = Fact(id="f1", kind="overseas", value="delivered", evidences=[good])
    f2 = Fact(id="f2", kind="english", value="usage", evidences=[bad])
    report = ExtractionReport(facts=[f1, f2], extractor_version="x")
    eq = evidence_quality(report, [p])
    assert eq["fact_count"] == 2
    assert eq["evidence_ok"] == 1
    assert eq["evidence_ok_rate"] == pytest.approx(0.5)
    assert eq["truncated_quotes"] == 0
    assert eq["avg_quote_len"] == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# 转人工 / 任职 / 成本
# ---------------------------------------------------------------------------

def test_review_triggered():
    assert review_triggered(ExtractionReport(warnings=["模型抽取失败，转人工复核：HTTP 500"]))
    assert review_triggered(ExtractionReport(warnings=["引用定位失败：页码越界"]))
    assert not review_triggered(ExtractionReport(warnings=["提示词版本 p1"]))


def test_year_of_and_employment_match():
    assert year_of("2016-03") == 2016
    assert year_of("2020") == 2020
    assert year_of("") is None
    assert year_of(None) is None

    report = ExtractionReport(
        employments=[],
        extractor_version="x",
    )
    # 空任职 → 无匹配
    label = EvalLabel(document_id="d", expected_employments=[
        {"start": "2016", "end": "2020"}])
    assert employment_metrics(report, label)["matched"] == 0


def test_aggregate_stats_percentiles():
    reports = [
        DocResult(doc_id=f"d{i}", extractor_version="model-p1",
                  report=ExtractionReport(
                      extractor_version="model-p1",
                      stats={"calls": 1, "input_tokens": 10, "output_tokens": 5,
                             "duration_s": float(i + 1), "cost_cents": 1.0}))
        for i in range(10)
    ]
    s = aggregate_stats(reports)
    assert s["docs"] == 10
    assert s["duration_p50_s"] == 5.0   # 1..10 按索引百分位
    assert s["duration_p95_s"] == 9.0
    assert s["input_tokens_total"] == 100
    assert s["cost_cents_total"] == 10.0
