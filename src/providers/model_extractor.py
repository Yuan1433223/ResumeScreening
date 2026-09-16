# -*- coding: utf-8 -*-
"""模型抽取器：云端大模型事实抽取（T3，增量落地）。

作用域说明（与使用者约定）：
- 本地逻辑已就绪：提示词 v1、结构化解析、引用定位（模型只回页码+原文引用，
  由程序计算字符偏移，规格 §7"不要让模型直接计算偏移"）、有限重试、格式纠正
  （最多一次）、成本/耗时统计、unknown/失败转人工。
- 云端调用点预留：`CloudModelClient.complete()` 是唯一需要补充的云端作用域。
  在 `.env` 填好 MODEL_* 字段（api_key/base_url/model）后，在其中实现
  OpenAI 兼容的 /chat/completions 调用即可；若你的 provider 协议不同，也改这里。
- 未配置 / 未实现云端时，抽取返回空报告 + 警告，工作流路由到人工复核，
  不影响规则版流程与演示。

设计约定（"以键传递"）：配置以 ModelConfig 类型化对象整体注入构造器一次；
后续层不接触 api_key，只传递 extractor_version 字符串。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Sequence

from pydantic import BaseModel, Field

from ..config import FACT_KINDS
from ..schemas import (
    Employment,
    Evidence,
    EvidenceSource,
    ExtractionReport,
    Fact,
    FactStatus,
    ModelConfig,
    Page,
)


# ---------------------------------------------------------------------------
# 异常与响应
# ---------------------------------------------------------------------------
class ModelNotConfiguredError(Exception):
    """ModelConfig 未配置密钥/模型：调用方应转为人工复核。"""


class ModelTransientError(Exception):
    """可重试错误：超时 / 限流 / 5xx。"""


class ModelPermanentError(Exception):
    """不可重试错误：鉴权 / 参数 / 输出无法解析。"""


@dataclass
class ModelResponse:
    """一次模型调用的结果。"""

    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    duration_s: float = 0.0


class ModelClient(Protocol):
    """模型客户端接口：本地逻辑只依赖该协议。"""

    def complete(self, prompt: str, *, timeout: float) -> ModelResponse: ...


# ---------------------------------------------------------------------------
# 云端客户端（T3 作用域预留）
# ---------------------------------------------------------------------------
class CloudModelClient:
    """云端大模型客户端。

    - 配置（api_key/base_url/model 等）以 ModelConfig 整体注入（装配边界）
    - 未配置 key/model → ModelNotConfiguredError，外层转为人工复核
    - complete() 的云端 HTTP 调用点由使用者补充实现（见方法内标注）
    """

    def __init__(self, config: ModelConfig) -> None:
        self._config = config

    def complete(self, prompt: str, *, timeout: float) -> ModelResponse:
        cfg = self._config
        api_key = cfg.api_key.get_secret_value() if cfg.api_key else ""
        if not api_key or not cfg.model:
            raise ModelNotConfiguredError(
                "ModelConfig 未配置 MODEL_API_KEY / MODEL_NAME；请在 .env 中补充后启用"
            )
        # ==================================================================
        # 云端大模型调用点（T3 预留）：由你补充实现。
        # 约定（OpenAI 兼容 /chat/completions）：
        #   1) POST {cfg.base_url}
        #      headers: {"Authorization": f"Bearer {api_key}",
        #                "Content-Type": "application/json"}
        #      body:    {"model": cfg.model,
        #                "messages": [{"role": "user", "content": prompt}],
        #                "temperature": cfg.temperature}
        #      timeout: timeout 秒（用你选用的 HTTP 库传入）
        #   2) 成功：返回 ModelResponse(
        #        text=resp["choices"][0]["message"]["content"],
        #        input_tokens=resp.get("usage", {}).get("prompt_tokens", 0),
        #        output_tokens=resp.get("usage", {}).get("completion_tokens", 0),
        #        duration_s=实测耗时)
        #   3) 错误分类：超时/限流/5xx 抛 ModelTransientError（外层有限重试）；
        #      鉴权/参数等 4xx 抛 ModelPermanentError（不重试）。
        # 若你的 provider 不是 OpenAI 兼容协议，请在本方法内适配。
        # ==================================================================
        raise NotImplementedError(
            "请在 src/providers/model_extractor.py 的 CloudModelClient.complete() "
            "填入你的云端 API 调用（.env 的 MODEL_* 字段已就绪）"
        )


class ScriptedModelClient:
    """测试/离线脚本用：按顺序返回预设响应；可注入异常模拟故障。"""

    def __init__(self, responses: Sequence[Any]) -> None:
        self._responses = list(responses)
        self.prompts: List[str] = []

    def complete(self, prompt: str, *, timeout: float) -> ModelResponse:
        self.prompts.append(prompt)
        if not self._responses:
            raise ModelPermanentError("脚本响应耗尽")
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


# ---------------------------------------------------------------------------
# 提示词 v1
# ---------------------------------------------------------------------------
PROMPT_V1 = """你是简历事实抽取器。输入是简历的分页文本（用 <page N> ... </page N> 标记）。
任务：按下列事实类型抽取岗位事实，只依据原文，不得推断没有证据的信息。

事实类型（kind）与取值要求：
- relevant_domain: true/false
- total_years: 数值（年）
- relevant_years: 数值（年）
- management: managed / clear_scope / scope_and_results
- steel_mic_experience: participated / responsible / led
- steel_full_flow: one_phase / two_phases / full_flow
- overseas: collab / responsible / delivered
- quality_safety: one_duty / two_plus
- english: info / usage（若原文明确“英语不熟练/不流利”，输出 value="negated" 且 status="contradicted"）
- project_scale: quantified
- mobility: location_ok / travel_ok

规则：
1. 每条事实必须给出 kind、value、status（supported/contradicted/unknown）、
   页码 page、以及能支持它的原文引用 quote（从该页文本原样截取 10–80 字）。
2. 无法从原文确定的信息不输出，或 status="unknown"，绝不猜测、绝不补全。
3. 任职经历（employments）同样给出原文引用：start_month（如 "2018-03"）、
   end_month（仍在职则 null）、is_current、domain、relevant、responsibilities。
4. 只输出一个 JSON 对象，不要 markdown 代码块，不要任何额外说明。

输出格式：
{"facts": [{"kind": "...", "value": ..., "status": "supported", "page": 1, "quote": "..."}],
 "employments": [{"start_month": "2018-03", "end_month": null, "is_current": false,
                  "domain": "钢结构/MiC", "relevant": true,
                  "responsibilities": ["..."], "quote": "..."}]}"""

_CORRECTION_HINT = "\n\n上一次输出无法解析。请只输出严格 JSON，不要附加任何说明、注释或代码块。"


# ---------------------------------------------------------------------------
# 模型输出契约（本地校验）
# ---------------------------------------------------------------------------
class _ModelOutputFact(BaseModel):
    kind: str
    value: Any
    status: str = "supported"
    page: int = Field(default=1, ge=1)
    quote: str = ""


class _ModelOutputEmployment(BaseModel):
    start_month: Optional[str] = None
    end_month: Optional[str] = None
    is_current: bool = False
    domain: Optional[str] = None
    relevant: Optional[bool] = None
    responsibilities: List[str] = Field(default_factory=list)
    quote: str = ""


class _ModelOutput(BaseModel):
    facts: List[_ModelOutputFact] = Field(default_factory=list)
    employments: List[_ModelOutputEmployment] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 抽取器
# ---------------------------------------------------------------------------
def _find_page(pages: Sequence[Page], page_number: int) -> Optional[Page]:
    for p in pages:
        if p.page_number == page_number:
            return p
    return None


def _localize_quote(page: Page, quote: str) -> Optional[Evidence]:
    """在页面文本中定位引用，程序计算字符偏移（模型不直接计算偏移）。

    - 优先精确子串；允许空白归一化匹配（模型断行/多余空格），匹配结果取原文真实切片。
    - 找不到返回 None，由调用方记为歧义并转人工。
    """
    q = quote.strip()
    if not q:
        return None
    idx = page.text.find(q)
    if idx >= 0:
        start, end = idx, idx + len(q)
    else:
        pat = re.compile(r"\s+".join(re.escape(tok) for tok in q.split()), re.DOTALL)
        m = pat.search(page.text)
        if m is None:
            return None
        start, end = m.start(), m.end()
    return Evidence(
        id=f"ev_{uuid.uuid4().hex[:10]}",
        document_id=page.document_id,
        page_number=page.page_number,
        start=start,
        end=end,
        quote=page.text[start:end],
        source_type=EvidenceSource.PAGE_TEXT,
    )


class ModelExtractor:
    """模型抽取器：提示词 + 结构化解析 + 引用定位 + 重试/纠正/成本统计。

    构造器接收 ModelConfig（"以键传递"的装配边界）；client 缺省时使用
    CloudModelClient（云端作用域，未配置/未实现则自动转人工）。
    """

    def __init__(
        self,
        config: ModelConfig,
        client: Optional[ModelClient] = None,
        sleep_fn: Any = time.sleep,
    ) -> None:
        self._config = config
        self._client = client
        self._sleep = sleep_fn

    @property
    def version(self) -> str:
        return f"model-{self._config.prompt_version}"

    def extract(self, document_id: str, pages: Sequence[Page]) -> ExtractionReport:
        cfg = self._config
        if not cfg.enabled:
            return ExtractionReport(
                extractor_version=self.version,
                warnings=["ModelExtractor 未启用（MODEL_ENABLED=0），转人工复核"],
            )
        client = self._client or CloudModelClient(cfg)

        base_prompt = self._build_prompt(pages)
        stats: Dict[str, float] = {
            "calls": 0, "input_tokens": 0, "output_tokens": 0,
            "duration_s": 0.0, "cost_cents": 0.0,
        }
        warnings: List[str] = []
        output: Optional[_ModelOutput] = None
        last_error: Optional[Exception] = None
        corrected = False
        prompt = base_prompt

        # 尝试次数：瞬态重试 cfg.max_retries 次（含首次共 3 次，规格 §7）
        # + 格式纠正最多 1 次（纠正后仍失败转人工）
        for attempt in range(cfg.max_retries + 2):
            try:
                resp = client.complete(prompt, timeout=cfg.timeout_seconds)
            except ModelTransientError as e:
                stats["calls"] += 1
                last_error = e
                if attempt >= cfg.max_retries:
                    break
                self._sleep(min(0.5 * (2 ** attempt), 4.0))
                continue
            except ModelPermanentError as e:
                stats["calls"] += 1
                last_error = e
                break
            except ModelNotConfiguredError as e:
                warnings.append(f"模型未配置，转人工复核：{e}")
                return ExtractionReport(
                    extractor_version=self.version, warnings=warnings, stats=stats,
                )

            stats["calls"] += 1
            stats["input_tokens"] += resp.input_tokens
            stats["output_tokens"] += resp.output_tokens
            stats["duration_s"] += resp.duration_s
            stats["cost_cents"] = self._estimate_cost(stats)

            parsed = self._parse_output(resp.text)
            if parsed is not None:
                output = parsed
                break
            # 格式错误：最多纠正一次
            if not corrected:
                corrected = True
                prompt = base_prompt + _CORRECTION_HINT
                continue
            last_error = ModelPermanentError("模型输出两次均无法解析为结构化 JSON")
            break

        if output is None:
            warnings.append(f"模型抽取失败，转人工复核：{last_error}")
            return ExtractionReport(
                extractor_version=self.version, warnings=warnings, stats=stats,
            )

        # 预算超限检查（在解析成功且已计费后）：超限则只返回已确认事实并转人工复核提示
        if cfg.max_cost_cents > 0 and stats["cost_cents"] > cfg.max_cost_cents:
            warnings.append(
                f"模型调用成本 {stats['cost_cents']:.2f} 美分超过预算 "
                f"{cfg.max_cost_cents}，转人工复核"
            )

        facts, fact_warnings = self._to_facts(document_id, pages, output.facts)
        employments, emp_warnings = self._to_employments(document_id, pages, output.employments)
        warnings.extend(fact_warnings)
        warnings.extend(emp_warnings)

        return ExtractionReport(
            facts=facts,
            employments=employments,
            warnings=warnings,
            extractor_version=self.version,
            stats=stats,
        )

    # -- 内部方法 -----------------------------------------------------------
    def _build_prompt(self, pages: Sequence[Page]) -> str:
        blocks = []
        for p in pages:
            blocks.append(f"<page {p.page_number}>\n{p.text}\n</page {p.page_number}>")
        return PROMPT_V1 + "\n\n=== 简历分页文本 ===\n" + "\n".join(blocks)

    @staticmethod
    def _parse_output(text: str) -> Optional[_ModelOutput]:
        t = text.strip()
        if t.startswith("```"):
            t = re.sub(r"^```(?:json)?\s*", "", t)
            t = re.sub(r"\s*```$", "", t)
        try:
            data = json.loads(t)
            return _ModelOutput.model_validate(data)
        except (ValueError, json.JSONDecodeError):
            return None

    def _estimate_cost(self, stats: Dict[str, float]) -> float:
        cfg = self._config
        if cfg.cost_per_1k_input_cents <= 0 and cfg.cost_per_1k_output_cents <= 0:
            return 0.0
        return (
            stats["input_tokens"] / 1000.0 * cfg.cost_per_1k_input_cents
            + stats["output_tokens"] / 1000.0 * cfg.cost_per_1k_output_cents
        )

    def _to_facts(
        self, document_id: str, pages: Sequence[Page], items: Sequence[_ModelOutputFact],
    ) -> tuple[List[Fact], List[str]]:
        facts: List[Fact] = []
        warnings: List[str] = []
        for mf in items:
            if mf.kind not in FACT_KINDS:
                warnings.append(f"未知事实类型 {mf.kind}，已跳过")
                continue
            page = _find_page(pages, mf.page)
            if page is None:
                warnings.append(
                    f"引用页码 {mf.page} 超出范围（共 {len(pages)} 页），事实 {mf.kind} 转人工"
                )
                continue
            ev = _localize_quote(page, mf.quote)
            if ev is None:
                warnings.append(
                    f"引用定位失败（第 {mf.page} 页未找到原文）：{mf.quote[:40]}…；"
                    f"事实 {mf.kind} 转人工"
                )
                continue
            try:
                status = FactStatus(mf.status)
            except ValueError:
                warnings.append(f"事实 {mf.kind} 状态非法（{mf.status}），按 unknown 处理")
                status = FactStatus.UNKNOWN
            facts.append(Fact(
                id=f"f_model_{uuid.uuid4().hex[:10]}",
                document_id=document_id,
                kind=mf.kind,
                value=mf.value,
                status=status,
                evidences=[ev],
                evidence_ids=[ev.id],
            ))
        return facts, warnings

    def _to_employments(
        self, document_id: str, pages: Sequence[Page], items: Sequence[_ModelOutputEmployment],
    ) -> tuple[List[Employment], List[str]]:
        employments: List[Employment] = []
        warnings: List[str] = []
        for me in items:
            # 任职区间引用走全页定位：找不到记歧义、该段任职转人工
            ev = self._locate_on_any_page(pages, me.quote)
            if ev is None:
                warnings.append(
                    f"任职经历引用定位失败：{me.quote[:40]}…；该段任职转人工"
                )
                continue
            employments.append(Employment(
                document_id=document_id,
                start_month=me.start_month,
                end_month=me.end_month,
                is_current=me.is_current,
                domain=me.domain,
                relevant=me.relevant,
                responsibilities=me.responsibilities,
                evidence_ids=[ev.id] if ev else [],
            ))
        return employments, warnings

    def _locate_on_any_page(self, pages: Sequence[Page], quote: str) -> Optional[Evidence]:
        for p in pages:
            ev = _localize_quote(p, quote)
            if ev is not None:
                return ev
        return None
