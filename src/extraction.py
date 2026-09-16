# -*- coding: utf-8 -*-
"""抽取服务层（对应 DEVELOPMENT_SPEC.md §9）

extract_facts(document_id, extractor_version) -> ExtractionReport
- 默认 mock 抽取器（规则，无 LLM）
- model 抽取器为占位（P1 不调用外部模型）
"""
from __future__ import annotations

from typing import List

from .providers.mock_extractor import MockExtractor
from .schemas import ExtractionReport, Page


def extract_facts(
    document_id: str,
    pages: List[Page],
    extractor_version: str = "mock",
) -> ExtractionReport:
    """从分页文本抽取岗位事实与任职区间。"""
    if extractor_version == "mock":
        return MockExtractor().extract(document_id, pages)
    if extractor_version.startswith("model"):
        # P1 占位：不调用外部模型
        report = ExtractionReport(extractor_version=extractor_version)
        report.warnings.append("ModelExtractor 未启用，P1 阶段默认使用 mock 抽取器")
        return report
    raise ValueError(f"未知抽取器版本: {extractor_version}")
