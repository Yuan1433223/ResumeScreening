# -*- coding: utf-8 -*-
"""P0 数据契约：领域模型（对应 DEVELOPMENT_SPEC.md §5）。

枚举与未知值不能由自由文本替代；证据位置引用 Page.text 的字符偏移，左闭右开。
P0 只实现分析闭环所需模型；排期/审批动作模型（Slot/Booking/ExternalAction 等）在 P2 引入。
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, SecretStr, model_validator


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------
class ParseStatus(str, Enum):
    PENDING = "pending"
    PARSED = "parsed"
    NEEDS_REVIEW = "needs_review"      # 解析失败/正文过少，进入人工复核
    FAILED = "failed"


class FactStatus(str, Enum):
    SUPPORTED = "supported"            # 有证据支持
    CONTRADICTED = "contradicted"      # 有反向证据
    UNKNOWN = "unknown"                # 无证据，不代表不具备


class HardResult(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


class Recommendation(str, Enum):
    RECOMMEND = "recommend"            # >=75
    NEEDS_REVIEW = "needs_review"      # 60-74 或关键信息缺失
    HOLD = "hold"                      # 硬条件 fail 或 <60


class EvidenceSource(str, Enum):
    PAGE_TEXT = "page_text"
    OCR = "ocr"
    FILENAME = "filename"              # 仅展示用元数据，不进入评分
    MANUAL = "manual"                  # HR 补充证据


# ---------------------------------------------------------------------------
# 领域模型
# ---------------------------------------------------------------------------
class JobProfile(BaseModel):
    """岗位配置：硬条件 + 8 维评分表 + 阈值。"""

    id: str
    version: str                        # 规则版本，评分缓存与审批绑定依赖它
    title: str
    analysis_date: str                  # YYYY-MM-DD，作为当前任职截止日期
    hard_rules: Dict[str, object]
    rubric: Dict[str, object]           # 8 维评分表配置（结构见 config.py）
    thresholds: Dict[str, float]        # {"recommend": 75, "needs_review": 60}


class Document(BaseModel):
    """简历文件（一份文件一个 Document；同名不自动合并候选人）。"""

    id: str
    sha256: str
    storage_path: str
    mime: str
    parse_status: ParseStatus = ParseStatus.PENDING
    parser_version: str = ""
    # P2 多租户预留：数据归属 org；P2a 恒为 "local"，查询不按 org_id 隔离。
    org_id: str = "local"


class Page(BaseModel):
    """单页文本。"""

    document_id: str
    page_number: int = Field(ge=1)      # 从 1 起
    text: str
    text_hash: str = ""
    extraction_method: str = "pdfplumber"  # pdfplumber / tesseract / easyocr
    quality_flags: List[str] = Field(default_factory=list)  # 如 "text_too_short", "ocr_low_confidence"


class Evidence(BaseModel):
    """引用：quote 必须等于 Page.text[start:end]。"""

    id: str
    document_id: str
    page_number: int = Field(ge=1)
    start: int = Field(ge=0)            # 字符偏移，左闭
    end: int = Field(ge=0)              # 右开
    quote: str
    source_type: EvidenceSource = EvidenceSource.PAGE_TEXT

    @model_validator(mode="after")
    def _check_range(self) -> "Evidence":
        if self.end <= self.start:
            raise ValueError("end 必须大于 start")
        return self


class Fact(BaseModel):
    """从简历抽取出的岗位事实，绑定证据。"""

    id: str
    document_id: str = ""
    kind: str                           # 枚举见 config.FACT_KINDS
    value: object
    status: FactStatus = FactStatus.SUPPORTED
    evidence_ids: List[str] = Field(default_factory=list)   # 展示用引用 ID
    evidences: List[Evidence] = Field(default_factory=list)  # 完整证据对象（校验用）
    uncertainty_reason: Optional[str] = None


class Employment(BaseModel):
    """任职区间（用于年限合并与相关经验判定）。"""

    document_id: str = ""
    start_month: Optional[str] = None   # "2018-03"；只有年份时可写 "2018-00" 表示月份未知
    end_month: Optional[str] = None
    is_current: bool = False
    domain: Optional[str] = None        # 如 "钢结构/MiC"
    relevant: Optional[bool] = None     # 是否直接相关；None=未知
    responsibilities: List[str] = Field(default_factory=list)
    evidence_ids: List[str] = Field(default_factory=list)


class DimensionScore(BaseModel):
    """单维评分结果。"""

    score: Optional[int] = None         # 未提及为 None
    max_score: int
    status: FactStatus = FactStatus.UNKNOWN
    level: Optional[str] = None         # 命中的档位描述
    matched_facts: List[str] = Field(default_factory=list)


class EvidenceValidation(BaseModel):
    """单条证据校验结果。"""

    evidence_id: str
    valid: bool
    reason: str = ""


class ValidationResult(BaseModel):
    """证据校验汇总：通过数、失败明细。校验失败的路由为 needs_review。"""

    total: int = 0
    valid_count: int = 0
    invalid: List[EvidenceValidation] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.total > 0 and self.valid_count == self.total


class ExtractionReport(BaseModel):
    """抽取报告：事实 + 任职区间 + 结构化缺失/警告。"""

    facts: List[Fact] = Field(default_factory=list)
    employments: List[Employment] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)   # 抽取层面的缺失与冲突
    extractor_version: str = ""
    stats: Dict[str, float] = Field(default_factory=dict)  # 模型调用统计：calls/tokens/duration_s/cost_cents


class ModelConfig(BaseModel):
    """云端大模型配置（T3）：由 .env 装配，密钥用 SecretStr 防日志泄露。

    约定：以"类型化配置对象"整体注入 ModelExtractor 构造器一次（即你问的
    "以键传递"——集中在装配边界）；后续层不接触 api_key，只传递 extractor_version。
    """

    enabled: bool = False
    provider: str = ""                       # 如 openai-compatible / deepseek / volc
    base_url: str = ""                       # 兼容 OpenAI 的 /chat/completions 地址
    api_key: SecretStr = SecretStr("")
    model: str = ""
    timeout_seconds: float = 60.0
    max_retries: int = 2                     # 额外重试次数（含首次共 3 次调用，规格 §7）
    max_cost_cents: float = 0.0              # 单份预算上限；0 表示不限制
    prompt_version: str = "p1"
    temperature: float = 0.0
    cost_per_1k_input_cents: float = 0.0     # 计费价目（可选，填了才估算成本）
    cost_per_1k_output_cents: float = 0.0


class Assessment(BaseModel):
    """评估结果：带证据的三态硬条件 + 已确认分数 + 机器建议。"""

    id: str
    document_id: str
    job_version: str
    extractor_version: str = ""
    rubric_version: str = ""
    hard_results: Dict[str, HardResult] = Field(default_factory=dict)
    hard_summary: HardResult = HardResult.UNKNOWN
    dimensions: Dict[str, DimensionScore] = Field(default_factory=dict)
    known_score: int = 0                # 仅累加已确认项
    score_complete: bool = False        # False 时禁止显示完整适配率
    missing_items: List[str] = Field(default_factory=list)
    recommendation: Recommendation = Recommendation.NEEDS_REVIEW
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    org_id: str = "local"  # P2 多租户预留；P2a 不做隔离


class ReviewDecision(str, Enum):
    APPROVE = "approve"                  # 批准进入排期池
    HOLD = "hold"                        # 暂缓/不通过
    NEEDS_REVIEW = "needs_review"        # 需补充事实后重评


class Review(BaseModel):
    """人工审批记录。审批先幂等入库（request_id），再恢复同一条任务。"""

    id: str
    assessment_id: str
    assessment_version: str              # 审批绑定的评估版本；旧版本审批不能自动批准新版本
    decision: ReviewDecision
    reason: str = ""
    reviewer_id: str = "hr"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    request_id: str = ""                 # 幂等键：同一 request 重复提交返回同一结果


class Batch(BaseModel):
    """一次导入的一批简历文件（用于批次冻结与全局排期）。"""

    id: str
    job_version: str
    status: str = "open"                 # open / frozen / scheduled
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    frozen_at: Optional[datetime] = None
    org_id: str = "local"                 # P2 多租户预留；P2a 不做隔离


class Slot(BaseModel):
    """面试时段（每轮 4 个 45 分钟，带时区）。"""

    id: str
    round_id: str
    start_at: datetime
    end_at: datetime
    timezone: str = "Asia/Shanghai"
    capacity: int = 1                    # 每时段仅 1 人


class BookingStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    WAITLIST = "waitlist"
    CANCELLED = "cancelled"


class Booking(BaseModel):
    """预约：同 round+candidate 仅一条有效；slot 仅一个有效占用。"""

    id: str
    round_id: str
    candidate_id: str
    slot_id: Optional[str] = None
    status: BookingStatus = BookingStatus.PENDING
    idempotency_key: str = ""


class ExternalActionStatus(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ExternalAction(BaseModel):
    """外部动作（建会/通知）：幂等键防重；状态可查询。"""

    id: str
    booking_id: str
    type: str                            # "meeting" / "notify"
    idempotency_key: str = ""
    status: ExternalActionStatus = ExternalActionStatus.PENDING
    provider_id: Optional[str] = None    # 外部返回的稳定标识（会议 ID / 通知 ID）
    attempt: int = 1
    last_error: Optional[str] = None


class SchedulingRound(BaseModel):
    """排期轮次：批次审批截止后冻结名单，统一排序分配时段。"""

    id: str
    batch_id: str
    round_no: int = 1
    status: str = "open"                 # open / frozen / scheduled
    expected_job_version: str = ""
    frozen_at: Optional[datetime] = None
    slots: List[Slot] = Field(default_factory=list)
    bookings: List[Booking] = Field(default_factory=list)
