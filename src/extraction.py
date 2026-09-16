# -*- coding: utf-8 -*-
"""抽取服务层（对应 DEVELOPMENT_SPEC.md §9）

extract_facts(document_id, extractor_version) -> ExtractionReport
- 默认 mock 抽取器（规则，无 LLM）
- model 抽取器（T3）：配置见 config.MODEL_CONFIG；未启用/未配置 → 空报告+警告 → 人工复核
"""
from __future__ import annotations

from typing import List

from .config import MODEL_CONFIG
from .providers.mock_extractor import MockExtractor
from .providers.model_extractor import ModelExtractor
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
        # T3：模型抽取（配置见 config.MODEL_CONFIG；未启用/未配置 → 空报告 + 警告 → 人工复核）
        return ModelExtractor(MODEL_CONFIG).extract(document_id, pages)
    raise ValueError(f"未知抽取器版本: {extractor_version}")
