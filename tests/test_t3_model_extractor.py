# -*- coding: utf-8 -*-
"""T3 模型抽取器本地逻辑测试（不依赖真实云端 API）。

覆盖：引用定位（程序算偏移）、空白归一化匹配、定位失败转人工、
瞬态错误有限重试、格式纠正一次、成本/耗时统计、未配置转人工、
未知事实类型跳过、预算超限提示、extract_facts 分发。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.evidence import validate_evidence
from src.extraction import extract_facts
from src.providers.model_extractor import (
    ModelExtractor,
    ModelNotConfiguredError,
    ModelPermanentError,
    ModelResponse,
    ModelTransientError,
    ScriptedModelClient,
)
from src.schemas import ModelConfig, Page


def make_page(text, page_number=1, document_id="d"):
    return Page(
        document_id=document_id,
        page_number=page_number,
        text=text,
        text_hash="",
        extraction_method="test",
    )


def resp_json(facts, employments=None, input_tokens=100, output_tokens=50):
    return ModelResponse(
        text=json.dumps(
            {"facts": facts, "employments": employments or []}, ensure_ascii=False
        ),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_s=1.2,
    )


def make_config(**overrides):
    defaults = dict(
        enabled=True,
        max_retries=2,
        prompt_version="p1",
        cost_per_1k_input_cents=100,
        cost_per_1k_output_cents=200,
    )
    defaults.update(overrides)
    return ModelConfig(**defaults)


PAGE_TEXT = (
    "2016-2020 负责海外钢结构项目交付，管理 30 人团队。"
    "英语流利，参与国际客户会议。"
)


def test_quote_localization_program_computes_offsets():
    """模型只回页码+引用，程序计算偏移；证据校验通过。"""
    page = make_page(PAGE_TEXT)
    facts = [{"kind": "overseas", "value": "delivered", "status": "supported",
              "page": 1, "quote": "负责海外钢结构项目交付"}]
    extractor = ModelExtractor(make_config(), client=ScriptedModelClient([resp_json(facts)]))
    report = extractor.extract("d", [page])

    assert len(report.facts) == 1
    ev = report.facts[0].evidences[0]
    assert page.text[ev.start:ev.end] == "负责海外钢结构项目交付"
    assert ev.page_number == 1
    assert report.facts[0].status.value == "supported"

    # 证据校验：引用与原文一致
    vr = validate_evidence(report.facts, [page])
    assert vr.passed

    # 调用统计：1 次调用 + 成本估算
    assert report.stats["calls"] == 1
    assert report.stats["input_tokens"] == 100
    assert report.stats["cost_cents"] > 0


def test_flexible_whitespace_quote_matches_real_slice():
    """模型引文换行/多空格：归一化匹配，证据取原文真实切片。"""
    page = make_page("负责 海外\n钢结构项目交付。")
    facts = [{"kind": "steel_mic_experience", "value": "led", "status": "supported",
              "page": 1, "quote": "负责 海外\n钢结构项目"}]
    extractor = ModelExtractor(make_config(), client=ScriptedModelClient([resp_json(facts)]))
    report = extractor.extract("d", [page])
    assert len(report.facts) == 1
    ev = report.facts[0].evidences[0]
    assert page.text[ev.start:ev.end] == "负责 海外\n钢结构项目"


def test_quote_not_found_reports_ambiguity_and_skips_fact():
    """引用定位失败：明确报告歧义，该事实转人工（不进入评分）。"""
    page = make_page(PAGE_TEXT)
    facts = [{"kind": "overseas", "value": "delivered", "status": "supported",
              "page": 1, "quote": "这句原文并不存在"}]
    extractor = ModelExtractor(make_config(), client=ScriptedModelClient([resp_json(facts)]))
    report = extractor.extract("d", [page])
    assert report.facts == []
    assert any("定位失败" in w for w in report.warnings)


def test_out_of_range_page_flagged():
    page = make_page(PAGE_TEXT)
    facts = [{"kind": "overseas", "value": "delivered", "status": "supported",
              "page": 9, "quote": "负责海外钢结构项目交付"}]
    extractor = ModelExtractor(make_config(), client=ScriptedModelClient([resp_json(facts)]))
    report = extractor.extract("d", [page])
    assert report.facts == []
    assert any("页码" in w for w in report.warnings)


def test_unknown_kind_skipped_with_warning():
    page = make_page(PAGE_TEXT)
    facts = [{"kind": "random_kind", "value": "x", "status": "supported",
              "page": 1, "quote": "负责海外钢结构项目交付"}]
    extractor = ModelExtractor(make_config(), client=ScriptedModelClient([resp_json(facts)]))
    report = extractor.extract("d", [page])
    assert report.facts == []
    assert any("未知事实类型" in w for w in report.warnings)


def test_transient_error_retry_then_success():
    """限流/超时：有限重试后成功，调用次数=2。"""
    page = make_page(PAGE_TEXT)
    facts = [{"kind": "overseas", "value": "delivered", "status": "supported",
              "page": 1, "quote": "负责海外钢结构项目交付"}]
    client = ScriptedModelClient([
        ModelTransientError("rate limited"),
        resp_json(facts),
    ])
    extractor = ModelExtractor(make_config(max_retries=2), client=client,
                               sleep_fn=lambda s: None)
    report = extractor.extract("d", [page])
    assert len(report.facts) == 1
    assert report.stats["calls"] == 2


def test_transient_error_exhausted_falls_back_to_review():
    """重试耗尽：返回空报告 + 警告，不崩溃。"""
    page = make_page(PAGE_TEXT)
    client = ScriptedModelClient([
        ModelTransientError("timeout"),
        ModelTransientError("timeout"),
        ModelTransientError("timeout"),
    ])
    extractor = ModelExtractor(make_config(max_retries=2), client=client,
                               sleep_fn=lambda s: None)
    report = extractor.extract("d", [page])
    assert report.facts == []
    assert any("模型抽取失败" in w for w in report.warnings)
    assert report.stats["calls"] == 3


def test_format_correction_once_then_success():
    """格式错误：纠正一次后成功（共 2 次调用）。"""
    page = make_page(PAGE_TEXT)
    facts = [{"kind": "overseas", "value": "delivered", "status": "supported",
              "page": 1, "quote": "负责海外钢结构项目交付"}]
    client = ScriptedModelClient([
        ModelResponse(text="抱歉，我无法解析", input_tokens=10, output_tokens=5),
        resp_json(facts),
    ])
    extractor = ModelExtractor(make_config(), client=client)
    report = extractor.extract("d", [page])
    assert len(report.facts) == 1
    assert report.stats["calls"] == 2
    # 第二次提示应包含纠正提示
    assert "无法解析" in client.prompts[1]


def test_not_enabled_falls_back_to_review():
    """MODEL_ENABLED=0：空报告 + 警告，不调用任何客户端。"""
    page = make_page(PAGE_TEXT)
    extractor = ModelExtractor(make_config(enabled=False),
                               client=ScriptedModelClient([]))
    report = extractor.extract("d", [page])
    assert report.facts == []
    assert any("未启用" in w for w in report.warnings)


def test_extract_facts_model_dispatch_uses_model_extractor(tmp_path, monkeypatch):
    """extract_facts 的 model 分支不再走占位：未启用时返回 model 版本空报告。

    用固定配置注入，避免依赖真实 .env（启用后会走云端调用）。
    """
    import src.extraction as extraction_mod

    monkeypatch.setattr(
        extraction_mod, "MODEL_CONFIG",
        ModelConfig(enabled=False, prompt_version="p1"),
    )
    page = make_page(PAGE_TEXT)
    report = extract_facts("d", [page], extractor_version="model")
    assert report.extractor_version.startswith("model-")
    assert report.facts == []
    assert any("未启用" in w for w in report.warnings)


def test_budget_exceeded_reports_and_keeps_partial():
    """预算超限：提示转人工，已确认事实仍返回（评分完整性由 workflow 控制）。"""
    page = make_page(PAGE_TEXT)
    facts = [{"kind": "overseas", "value": "delivered", "status": "supported",
              "page": 1, "quote": "负责海外钢结构项目交付"}]
    # 价目极高，成本必然超预算
    config = make_config(max_cost_cents=1, cost_per_1k_input_cents=100000,
                         cost_per_1k_output_cents=200000)
    extractor = ModelExtractor(config, client=ScriptedModelClient([resp_json(facts)]))
    report = extractor.extract("d", [page])
    assert len(report.facts) == 1
    assert any("预算" in w for w in report.warnings)


def test_permanent_error_no_retry():
    """不可重试错误（鉴权/4xx）：立即失败转人工，不重试。"""
    page = make_page(PAGE_TEXT)
    client = ScriptedModelClient([ModelPermanentError("401 unauthorized")])
    extractor = ModelExtractor(make_config(max_retries=2), client=client,
                               sleep_fn=lambda s: None)
    report = extractor.extract("d", [page])
    assert report.facts == []
    assert report.stats["calls"] == 1
    assert any("模型抽取失败" in w for w in report.warnings)


def test_cloud_client_raises_when_not_configured():
    """CloudModelClient 未配置 key/model：抛 ModelNotConfiguredError。"""
    from src.providers.model_extractor import CloudModelClient

    client = CloudModelClient(ModelConfig(enabled=True))
    with pytest.raises(ModelNotConfiguredError):
        client.complete("prompt", timeout=10)
