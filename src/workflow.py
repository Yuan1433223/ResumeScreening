"""LangGraph 简历筛选工作流 - 规则节点，无 LLM

P1 变更（对应 DEVELOPMENT_SPEC.md §7/§9）：
- parse → extract → validate → screen/score 完整分析闭环
- extract 用 Mock 抽取器产出带引用的 Fact/Employment
- validate 校验引用一致性，校验未通过 → needs_review
- screen 采用三态硬条件；score 输出 known_score / missing_items / recommendation
- 保留 CLI 可用入口；排期/通知节点为占位（P2 重构为独立排期器）

P2 变更（对应 DEVELOPMENT_SPEC.md §7/§8/§9）：
- 持久化 checkpointer（SqliteSaver）+ 稳定 thread_id；审批节点 interrupt，Command(resume) 恢复
- review 节点 interrupt 等待人工审批；审批先以 request_id 幂等入库再恢复任务
- 排期/通知移出候选人图：批次审批截止后由 scheduler 全局排序原子占用
- 分析结果以 assessment 版本键幂等落库（repository），重启重放不重复计算
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from .state import ResumeState
from .parser import parse_document, parse_name_from_filename
from .extraction import extract_facts
from .evidence import validate_evidence
from .schemas import Assessment, Page, ReviewDecision, ValidationResult
from .scorer import assess
from .config import JOB_PROFILE, JOB_TITLE, DB_PATH, CHECKPOINT_DB_PATH
from .repository import get_repository
from .schemas import Assessment, Page, ReviewDecision


def _doc_id(file_path: str) -> str:
    """稳定 document_id：由文件路径哈希生成（重复文件复用同一 Document）。"""
    return "doc_" + hashlib.sha256(str(file_path).encode("utf-8")).hexdigest()[:16]


def _new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:12]


def node_parse(state: ResumeState) -> ResumeState:
    """解析节点：分页抽取文本。失败不抛异常，进入复核。"""
    if state.get("skip_parse"):
        return state
    try:
        document_id = state.get("document_id") or _doc_id(state["file_path"])
        pages, status, reason = parse_document(document_id, state["file_path"])
        raw_text = "\n".join(p.text for p in pages)
        file_path = state["file_path"]
        return {
            **state,
            "document_id": document_id,
            "pages": pages,
            "raw_text": raw_text,
            "parsed": {
                "name": parse_name_from_filename(file_path),
                "file_name": Path(file_path).name,
                "file_path": file_path,
                "parse_status": status.value,
                "parse_error": reason or None,
                "text_length": len(raw_text),
                "has_text": bool(raw_text.strip()),
            },
            "error": reason if status.value != "parsed" else None,
        }
    except Exception as e:
        return {
            **state,
            "pages": [],
            "raw_text": "",
            "parsed": {
                "name": state.get("file_name", ""),
                "file_name": state.get("file_name", ""),
                "file_path": state["file_path"],
                "parse_status": "failed",
                "parse_error": f"解析失败: {e}",
                "text_length": 0,
                "has_text": False,
            },
            "error": f"解析失败: {e}",
        }


def route_after_parse(state: ResumeState) -> str:
    """解析失败/正文空 → review（人工复核）；否则继续 extract。"""
    parsed = state.get("parsed") or {}
    if parsed.get("parse_status") in ("failed", "needs_review"):
        return "review"
    return "extract"


def node_extract(state: ResumeState) -> ResumeState:
    """抽取节点：Mock 抽取事实与任职区间（带精确引用）。"""
    if state.get("error") or state.get("skip_parse"):
        return state
    report = extract_facts(
        state["document_id"],
        state.get("pages", []),
        extractor_version="mock",
    )
    return {
        **state,
        "facts": report.facts,
        "employments": report.employments,
        "extractor_version": report.extractor_version,
    }


def node_validate(state: ResumeState) -> ResumeState:
    """证据校验节点：引用一致性。校验未通过 → needs_review。

    测试注入模式（skip_parse）跳过切片校验：facts 为直接注入的合成事实，
    无原文切片可比对，视为通过。
    """
    if state.get("error"):
        return state
    if state.get("skip_parse"):
        return {**state, "evidence_validation": ValidationResult(total=0, valid_count=0, invalid=[])}
    result = validate_evidence(state.get("facts", []), state.get("pages", []))
    return {
        **state,
        "evidence_validation": result,
        "error": None if result.passed else "证据校验未通过",
    }


def route_after_validate(state: ResumeState) -> str:
    if state.get("error"):
        return "review"
    return "screen"


def node_screen(state: ResumeState) -> ResumeState:
    """筛选节点：三态硬条件。"""
    if state.get("error"):
        return state
    hard_results = _evaluate_hard(state.get("facts", []), state.get("employments", []))
    return {
        **state,
        "hard_results": hard_results,
        "hard_summary": hard_results.get("summary"),
    }


def _evaluate_hard(facts, employments) -> Dict[str, str]:
    from .scorer import evaluate_hard_rules

    hr = evaluate_hard_rules(facts, employments, JOB_PROFILE)
    return {k: v.value for k, v in hr.items()}


def node_score(state: ResumeState) -> ResumeState:
    """评分节点：8 维评估；结果幂等落库（repository）。"""
    if state.get("error"):
        return state
    assessment = assess(
        state.get("facts", []),
        state.get("employments", []),
        JOB_PROFILE,
        document_id=state.get("document_id", "doc"),
        assessment_id=f"{state.get('document_id', 'doc')}_v1",
        extractor_version=state.get("extractor_version", "synthetic"),
    )
    # 评估结果落库（幂等：同 assessment id 覆盖为同一版本键）
    repo = get_repository()
    repo.save_assessment(assessment)
    # 关联 run
    run_id = state.get("run_id")
    if run_id:
        repo.update_run(run_id, assessment_id=assessment.id)
    return {
        **state,
        "assessment": assessment,
        "assessment_id": assessment.id,
        "known_score": assessment.known_score,
        "score_complete": assessment.score_complete,
        "missing_items": assessment.missing_items,
        "recommendation": assessment.recommendation.value,
    }


def node_review(state: ResumeState) -> ResumeState:
    """复核节点：汇总意见；随后 interrupt 等待人工审批。

    审批流程（P2）：submit_review 先以 request_id 幂等入库，再 Command(resume=decision)
    恢复本节点；进程在两步之间退出时，resume_run 读取未消费审批继续执行。
    """
    comments: List[str] = []
    parsed = state.get("parsed") or {}
    status = parsed.get("parse_status")

    if status in ("failed", "needs_review"):
        comments.append(f"解析不可用（{status}），需人工复核：{parsed.get('parse_error', '正文缺失')}")
    else:
        validation = state.get("evidence_validation")
        if validation is not None and not validation.passed:
            comments.append(f"证据校验未通过（{validation.valid_count}/{validation.total}），需人工复核")
            for inv in validation.invalid[:5]:
                comments.append(f"  - {inv.reason}")
        summary = state.get("hard_summary")
        if summary == "pass":
            comments.append("硬条件通过")
        elif summary == "fail":
            comments.append("硬条件未通过")
        else:
            comments.append("硬条件存在未知项，需人工确认")
        rec = state.get("recommendation", "needs_review")
        comments.append(f"机器建议：{rec}")

    # 若已通过 Command(resume=...) 恢复（resume 返回审批 decision），直接消费
    if state.get("approval_decision") is not None:
        return {**state, "review_comments": comments}

    # 首次到达：interrupt 等待人工审批（审批前不创建任何会议/通知）
    decision = interrupt({
        "assessment_id": state.get("assessment_id"),
        "candidate": parsed.get("name", "未知姓名"),
        "recommendation": state.get("recommendation", "needs_review"),
        "review_comments": comments,
    })
    return {**state, "review_comments": comments, "approval_decision": decision}


def node_apply_approval(state: ResumeState) -> ResumeState:
    """消费审批结果：更新审批状态；不创建会议（排期由 scheduler 独立完成）。"""
    decision = state.get("approval_decision")
    if isinstance(decision, dict):
        decision = decision.get("decision", decision.get("decision") or "needs_review")
    decision_str = str(decision or "needs_review").strip('"')
    try:
        ReviewDecision(decision_str)
    except ValueError:
        decision_str = "needs_review"
    return {
        **state,
        "approval": {
            "decision": decision_str,
            "interview_scheduled": False,  # 排期器负责，候选人图不建会
            "notify_status": "待批次排期",
        },
    }


def _build_score_detail(state: ResumeState) -> Dict[str, Any]:
    """构造评分明细（维度 → 分数/档位/证据原文引用）。"""
    assessment = state.get("assessment")
    score_detail: Dict[str, Any] = {}
    if assessment:
        for dim, ds in assessment.dimensions.items():
            score_detail[dim] = {
                "score": ds.score,
                "max": ds.max_score,
                "status": ds.status.value,
                "level": ds.level,
                "evidence": _evidence_texts(ds, state.get("facts", [])),
            }
    return score_detail


def node_report(state: ResumeState) -> ResumeState:
    """输出节点：汇总最终报告，含证据定位与缺失项、审批状态。"""
    parsed = state.get("parsed") or {}
    assessment = state.get("assessment")
    validation = state.get("evidence_validation")
    approval = state.get("approval")

    report = {
        "岗位": state.get("job_title", JOB_TITLE),
        "候选人": parsed.get("name", "未知姓名"),
        "文件名": parsed.get("file_name", ""),
        "解析状态": parsed.get("parse_status", "failed"),
        "解析错误": parsed.get("parse_error"),
        "抽取器": state.get("extractor_version", ""),
        "证据校验": None if validation is None else {
            "通过": validation.valid_count,
            "总数": validation.total,
            "失败原因": [i.reason for i in validation.invalid[:5]],
        },
        "硬条件结果": state.get("hard_results", {}),
        "综合评分": state.get("known_score", 0) if assessment else None,
        "评分完整": state.get("score_complete", False),
        "缺失项": state.get("missing_items", []),
        "评分明细": _build_score_detail(state),
        "机器建议": state.get("recommendation", "needs_review"),
        "复核意见": state.get("review_comments", []),
        "审批状态": (approval or {}).get("decision", "pending"),
        "面试安排": (approval or {}).get("interview_scheduled", False),
        "面试时间": None,
        "通知状态": (approval or {}).get("notify_status", "待审批"),
        "文本长度": parsed.get("text_length", 0),
        "是否可解析文本": parsed.get("has_text", False),
    }
    return {**state, "final_report": report}


def _evidence_texts(ds, facts: List[Any]) -> List[str]:
    """收集维度命中所用事实的证据原文（可定位）。"""
    texts = []
    matched_ids = set(ds.matched_facts)  # 命中档位对应的事实 ID
    for f in facts:
        if f.id in matched_ids:
            for ev in f.evidences:
                if ev.quote and ev.quote not in texts:
                    texts.append(ev.quote)
    return texts


_checkpointer_cache: Any = None


def _get_checkpointer() -> Any:
    """进程内复用 SqliteSaver（持久连接 + 编译图），符合规格"服务启动时复用图"。"""
    global _checkpointer_cache
    if _checkpointer_cache is None:
        import sqlite3
        from langgraph.checkpoint.sqlite import SqliteSaver

        conn = sqlite3.connect(CHECKPOINT_DB_PATH, check_same_thread=False)
        _checkpointer_cache = SqliteSaver(conn)
    return _checkpointer_cache


def build_resume_graph():
    """构建并返回 LangGraph 工作流（P2：持久化 checkpointer + 审批 interrupt）。

    图结构：parse → extract → validate → screen → score → review(interrupt) → apply → report
    解析失败/校验失败路由到 review（人工复核）。
    """
    workflow = StateGraph(ResumeState)

    workflow.add_node("parse", node_parse)
    workflow.add_node("extract", node_extract)
    workflow.add_node("validate", node_validate)
    workflow.add_node("screen", node_screen)
    workflow.add_node("score", node_score)
    workflow.add_node("review", node_review)
    workflow.add_node("apply", node_apply_approval)
    workflow.add_node("report", node_report)

    workflow.set_entry_point("parse")
    workflow.add_conditional_edges("parse", route_after_parse, {
        "extract": "extract",
        "review": "review",
    })
    workflow.add_edge("extract", "validate")
    workflow.add_conditional_edges("validate", route_after_validate, {
        "screen": "screen",
        "review": "review",
    })
    workflow.add_edge("screen", "score")
    workflow.add_edge("score", "review")
    workflow.add_edge("review", "apply")
    workflow.add_edge("apply", "report")
    workflow.add_edge("report", END)

    # P2：持久化 checkpointer（SqliteSaver，进程内复用）
    return workflow.compile(checkpointer=_get_checkpointer())


def run_single(
    file_path: str,
    job_title: str = JOB_TITLE,
    thread_id: Optional[str] = None,
    facts: List[Any] | None = None,
    employments: List[Any] | None = None,
    document_id: Optional[str] = None,
    batch_id: Optional[str] = None,
) -> Dict[str, Any]:
    """对单份简历运行完整工作流（分析闭环），停在审批节点。

    - thread_id 缺省时用 document_id（稳定：重复分析同一文件复用同一检查点）
    - 返回含审批状态的分析报告；审批由 submit_review 驱动恢复
    - facts/employments 参数仅供测试注入（跳过抽取）
    """
    doc_id = document_id or _doc_id(file_path)
    tid = thread_id or doc_id
    graph = build_resume_graph()
    repo = get_repository()

    # P2 幂等：同 sha256 复用 Document；记录 run（稳定 thread_id）
    sha = hashlib.sha256(Path(file_path).read_bytes()).hexdigest()
    existing = repo.get_document_by_sha(sha)
    if existing:
        doc_id = existing["id"]
        tid = thread_id or doc_id
    repo.upsert_document(
        doc_id, sha, str(file_path),
        mime=Path(file_path).suffix.lower().lstrip(".") or "unknown",
        parser_version="pdfplumber/rapidocr",
    )
    run_id = _new_run_id()
    repo.create_run(run_id, tid, doc_id, batch_id=batch_id)

    state: Dict[str, Any] = {
        "file_path": file_path,
        "job_title": job_title,
        "document_id": doc_id,
        "run_id": run_id,
    }
    if facts is not None:
        # 测试注入：跳过解析/抽取，直接从证据校验开始
        state["skip_parse"] = True
        state["facts"] = facts
        state["employments"] = employments or []
        state["pages"] = [
            Page(document_id=doc_id, page_number=1,
                 text=" ".join(f.value if isinstance(f.value, str) else str(f.value) for f in facts),
                 text_hash="", extraction_method="injected")
        ]
        state["parsed"] = {
            "name": Path(file_path).stem,
            "file_name": Path(file_path).name,
            "file_path": file_path,
            "parse_status": "parsed",
            "parse_error": None,
            "text_length": len(state["pages"][0].text),
            "has_text": True,
        }
        state["extractor_version"] = "injected"

    result = graph.invoke(state, config={"configurable": {"thread_id": tid}})
    report = result.get("final_report") or _report_from_state(result)
    return report


def _report_from_state(state: ResumeState) -> Dict[str, Any]:
    """interrupt 未到达 report 节点时，从当前 state 构造可展示报告。"""
    parsed = state.get("parsed") or {}
    assessment = state.get("assessment")
    approval = state.get("approval")
    validation = state.get("evidence_validation")
    return {
        "岗位": state.get("job_title", JOB_TITLE),
        "候选人": parsed.get("name", "未知姓名"),
        "文件名": parsed.get("file_name", ""),
        "解析状态": parsed.get("parse_status", "failed"),
        "解析错误": parsed.get("parse_error"),
        "抽取器": state.get("extractor_version", ""),
        "证据校验": None if validation is None else {
            "通过": validation.valid_count,
            "总数": validation.total,
            "失败原因": [i.reason for i in validation.invalid[:5]],
        },
        "硬条件结果": state.get("hard_results", {}),
        "综合评分": state.get("known_score", 0) if assessment else None,
        "评分完整": state.get("score_complete", False),
        "缺失项": state.get("missing_items", []),
        "评分明细": _build_score_detail(state),
        "机器建议": state.get("recommendation", "needs_review"),
        "复核意见": state.get("review_comments", []),
        "审批状态": (approval or {}).get("decision", "pending"),
        "面试安排": (approval or {}).get("interview_scheduled", False),
        "面试时间": None,
        "通知状态": (approval or {}).get("notify_status", "待审批"),
        "文本长度": parsed.get("text_length", 0),
        "是否可解析文本": parsed.get("has_text", False),
    }


def resume_run(run_id: str, decision: Optional[str] = None) -> Dict[str, Any]:
    """恢复一条被 interrupt 暂停的任务。

    - decision 缺省时：读取该评估未消费的审批（进程在"审批入库"与"恢复"之间崩溃的场景）
    - 有审批则 Command(resume=decision) 恢复；无审批返回当前状态（仍待审批）
    """
    repo = get_repository()
    run = repo.get_run(run_id)
    if run is None:
        raise ValueError(f"run 不存在: {run_id}")

    # 读取未消费审批（审批先入库再恢复）
    review = None
    assessment_id = run.get("assessment_id")
    if assessment_id:
        review = repo.get_unconsumed_review(assessment_id)
    if review is not None:
        decision = review.decision.value
        repo.mark_review_consumed(review.id)
        repo.update_run(run_id, status="resumed")

    graph = build_resume_graph()
    cfg = {"configurable": {"thread_id": run["thread_id"]}}

    if decision is not None:
        result = graph.invoke(Command(resume=decision), cfg)
        repo.update_run(run_id, status="finished", finished_at=__import__("datetime").datetime.now().isoformat())
        return result.get("final_report") or _report_from_state(result)

    # 无审批：返回当前状态（待审批）
    snapshot = graph.get_state(cfg)
    return _report_from_state(snapshot.values)
