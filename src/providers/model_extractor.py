# -*- coding: utf-8 -*-
"""Model 抽取器：LLM 抽取占位实现（P1 不调用外部模型）。

对应 DEVELOPMENT_SPEC.md §13：P0/P1 阶段不调用外部模型。
接口与 MockExtractor 对齐（extract(document_id, pages) -> ExtractionReport）；
接入真实模型时替换为提示词 + 结构化输出 + 退避重试（规格 §7）。
"""
from __future__ import annotations

from typing import List

from ..schemas import ExtractionReport, Page


class ModelExtractor:
    version = "model-v0-unavailable"

    def extract(self, document_id: str, pages: List[Page]) -> ExtractionReport:
        raise NotImplementedError(
            "ModelExtractor 为占位实现：P1 不调用外部模型。"
            "接入前需确认数据使用授权（规格 §12），并实现提示词/引用校验/退避重试。"
        )
