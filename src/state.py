"""LangGraph 共享状态定义"""
from typing import TypedDict, List, Dict, Any, Optional


class ResumeState(TypedDict, total=False):
    # 输入
    file_path: str
    file_name: str
    job_title: str
    document_id: str
    run_id: str          # P2：任务跟踪（repository.runs）
    batch_id: str        # P2：批次归属
    skip_parse: bool     # 测试注入：跳过解析/抽取（facts 直接注入）

    # 解析结果（P1 分页）
    pages: List[Any]
    raw_text: str
    parsed: Dict[str, Any]

    # 抽取结果（P1）
    facts: List[Any]
    employments: List[Any]
    evidence_validation: Any
    extractor_version: str

    # 三态硬条件
    hard_results: Dict[str, str]
    hard_summary: str

    # 评分结果
    assessment: Any
    assessment_id: str   # P2：评估主键（关联审批/排期）
    known_score: int
    score_complete: bool
    missing_items: List[str]
    recommendation: str

    # 复核/审批（P2）
    review_comments: List[str]
    approval_decision: Any   # interrupt 恢复值（Command(resume) 传入）
    approval: Dict[str, Any] # 审批结果（decision / 状态）
    interview_scheduled: bool
    interview_time: Optional[str]
    notify_status: str

    # 输出
    final_report: Dict[str, Any]
    error: Optional[str]


class BatchState(TypedDict, total=False):
    resume_dir: str
    reports: List[Dict[str, Any]]
    summary: Dict[str, Any]
