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
import json
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from .state import ResumeState
from .parser import ParseStatus, parse_document, parse_name_from_filename
from .extraction import extract_facts
from .evidence import validate_evidence
from .schemas import Assessment, Page, ReviewDecision, ValidationResult
from .scorer import assess
from .config import JOB_PROFILE, JOB_TITLE, JOB_VERSION, DB_PATH, CHECKPOINT_DB_PATH
from .repository import get_repository
from .schemas import Assessment, Page, ReviewDecision


# T5c：图调用（检查点读写）全局串行化——后台批次线程与 API 线程共用同一
# SqliteSaver 连接（check_same_thread=False），需防并发游标竞争。
_GRAPH_LOCK = threading.RLock()


def _doc_id(file_path: str) -> str:
    """稳定 document_id：由文件路径哈希生成（重复文件复用同一 Document）。"""
    return "doc_" + hashlib.sha256(str(file_path).encode("utf-8")).hexdigest()[:16]


def _new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:12]


def _cached_pages(repo: Any, document_id: str) -> Optional[List[Page]]:
    """读取 pages 表缓存；无缓存或正文为空返回 None（调用方回退 OCR）。"""
    try:
        rows = repo.get_pages(document_id)
    except Exception:  # noqa: BLE001 缓存读取失败不阻断解析
        return None
    if not rows or not any((r.get("text") or "").strip() for r in rows):
        return None
    pages: List[Page] = []
    for r in rows:
        flags = r.get("quality_flags") or []
        if isinstance(flags, str):
            try:
                flags = json.loads(flags)
            except (ValueError, TypeError):
                flags = []
        pages.append(
            Page(
                document_id=document_id,
                page_number=int(r["page_number"]),
                text=r.get("text") or "",
                text_hash=r.get("text_hash") or "",
                extraction_method=r.get("extraction_method") or "cache",
                quality_flags=flags,
            )
        )
    return pages or None


def node_parse(state: ResumeState) -> ResumeState:
    """解析节点：分页抽取文本。失败不抛异常，进入复核。"""
    if state.get("skip_parse"):
        return state
    import time as _t
    _t0 = _t.monotonic()
    print(f"[parse] 开始 {Path(state['file_path']).name}")
    try:
        document_id = state.get("document_id") or _doc_id(state["file_path"])
        # 页面缓存复用：同一文档已解析过（pages 表有正文）则跳过 OCR，换抽取器重跑秒级
        cached = _cached_pages(get_repository(), document_id)
        if cached is not None:
            pages, status, reason = cached, ParseStatus.PARSED, ""
        else:
            pages, status, reason = parse_document(document_id, state.get("source_path") or state["file_path"])
            get_repository().save_pages(document_id, pages)
        print(f"[parse] 完成 {_t.monotonic()-_t0:.0f}s pages={len(pages)} chars={sum(len(p.text) for p in pages)} status={status.value}")
        raw_text = "\n".join(p.text for p in pages)
        file_path = state["file_path"]
        # PII 落库（尽力而为）：从解析正文提取姓名/手机/邮箱写入 documents.pii_*，
        # 失败仅记日志、留空，绝不阻断主解析/评分流程。
        try:
            from .pii import extract_pii
            pii = extract_pii(raw_text, file_path)
            get_repository().set_document_pii(
                document_id, name=pii["name"], phone=pii["phone"], email=pii["email"],
            )
        except Exception as _e:  # noqa: BLE001
            print(f"[parse] PII 提取/落库失败（忽略）: {_e}")
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
    """抽取节点：按 state 指定的抽取器（mock / model*）抽取事实与任职区间。"""
    if state.get("error") or state.get("skip_parse"):
        return state
    extractor_version = state.get("extractor_version", "mock")
    print(f"[extract] {state['document_id']} extractor={extractor_version} pages={len(state.get('pages', []))}")
    report = extract_facts(
        state["document_id"],
        state.get("pages", []),
        extractor_version=extractor_version,
    )
    print(f"[extract] {state['document_id']} 完成 facts={len(report.facts)} employments={len(report.employments)}")
    return {
        **state,
        "facts": report.facts,
        "employments": report.employments,
        "extractor_version": report.extractor_version,
        # T6 可观测性：保留抽取统计与校验失败点，随报告/trace 带出
        "extract_stats": report.stats,
        "extract_warnings": report.warnings,
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
    """保存一次执行的完整分析快照；失败路径保留人工可处理的未知评估。"""
    blocked = bool(state.get("error"))
    assessment = assess(
        [] if blocked else state.get("facts", []),
        [] if blocked else state.get("employments", []), JOB_PROFILE,
        document_id=state.get("document_id", "doc"),
        assessment_id=f"{state.get('run_id', state.get('document_id', 'doc'))}_{JOB_VERSION}",
        extractor_version=state.get("extractor_version", "synthetic"),
    )
    assessment.snapshot_version = 1
    assessment.analysis_date = JOB_PROFILE.analysis_date
    assessment.scoring_available = not blocked
    assessment.facts = state.get("facts", [])
    assessment.employments = state.get("employments", [])
    assessment.pages = state.get("pages", [])
    assessment.warnings = list(state.get("extract_warnings", []))
    assessment.stats = state.get("extract_stats", {})
    if blocked:
        assessment.warnings.append(str(state["error"]))
    repo = get_repository()
    repo.save_assessment(assessment)
    if state.get("run_id"):
        repo.update_run(state["run_id"], assessment_id=assessment.id)
    return {**state, "assessment": assessment, "assessment_id": assessment.id,
            "known_score": None if blocked else assessment.known_score, "score_complete": assessment.score_complete,
            "missing_items": assessment.missing_items,
            "recommendation": assessment.recommendation.value}


def node_review(state: ResumeState) -> ResumeState:
    """复核节点：汇总意见；随后 interrupt 等待人工审批。

    审批流程（P2）：submit_review 先以 request_id 幂等入库，再 Command(resume=decision)
    恢复本节点；进程在两步之间退出时，resume_run 读取未消费审批继续执行。
    """
    if not state.get("assessment"):
        state = node_score(state)
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
        "document_id": state.get("document_id", ""),
        "assessment_id": state.get("assessment_id", ""),
        "抽取事实数": len(state.get("facts", [])),
        "抽取任职数": len(state.get("employments", [])),
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
        # T6 可观测性：抽取统计（calls/耗时/token/费用）与校验失败点
        "抽取统计": state.get("extract_stats") or {},
        "抽取警告": state.get("extract_warnings") or [],
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
    with _GRAPH_LOCK:
        if _checkpointer_cache is None:
            import sqlite3
            from langgraph.checkpoint.sqlite import SqliteSaver
            from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
            import inspect
            from . import schemas

            conn = sqlite3.connect(CHECKPOINT_DB_PATH, check_same_thread=False)
            allowed = [(schemas.__name__, name) for name, value in vars(schemas).items()
                       if inspect.isclass(value) and value.__module__ == schemas.__name__]
            _checkpointer_cache = SqliteSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=allowed))
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
        "review": "score",
    })
    workflow.add_edge("extract", "validate")
    workflow.add_conditional_edges("validate", route_after_validate, {
        "screen": "screen",
        "review": "score",
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
    extractor_version: str = "mock",
) -> Dict[str, Any]:
    """对单份简历运行完整工作流（分析闭环），停在审批节点。

    - thread_id 缺省时用 document_id（稳定：重复分析同一文件复用同一检查点）
    - facts/employments 参数仅供测试注入（跳过抽取）
    - extractor_version：mock / model（T5c：工作台批次可切换）
    """
    repo = get_repository()
    content = Path(file_path).read_bytes()
    sha = hashlib.sha256(content).hexdigest()
    existing = repo.get_document_by_sha(sha)
    doc_id = existing["id"] if existing else (document_id or "doc_" + sha[:24])
    # 内容身份与原始路径分离；原文件更新不改变历史复核内容。
    source_dir = Path(repo.db_path).parent / "documents"
    source_dir.mkdir(parents=True, exist_ok=True)
    source_path = source_dir / (sha + Path(file_path).suffix.lower())
    if not source_path.exists():
        temporary = source_path.with_name(source_path.name + '.' + uuid.uuid4().hex + '.tmp')
        temporary.write_bytes(content)
        temporary.replace(source_path)
    repo.upsert_document(doc_id, sha, str(source_path),
                         mime=Path(file_path).suffix.lower().lstrip("."),
                         parser_version="pdfplumber/rapidocr", file_name=Path(file_path).name)
    tid = f"{thread_id or batch_id or 'cli'}__{doc_id}__{JOB_VERSION}__{extractor_version}"
    graph = build_resume_graph()
    run_row = repo.create_run(_new_run_id(), tid, doc_id, batch_id=batch_id)
    with _GRAPH_LOCK:
        previous = graph.get_state({"configurable": {"thread_id": tid}})
        if previous.values.get("assessment_id"):
            return previous.values.get("final_report") or _report_from_state(previous.values)
    state: Dict[str, Any] = {
        "file_path": file_path,
        "source_path": str(source_path),
        "batch_id": batch_id,
        "job_title": job_title,
        "document_id": doc_id,
        "run_id": run_row["id"],
        "extractor_version": extractor_version,
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

    with _GRAPH_LOCK:
        result = graph.invoke(state, config={"configurable": {"thread_id": tid}})
    print(f"[run] {Path(file_path).name} invoke 完成 run={run_row['id']}")
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
        "document_id": state.get("document_id", ""),
        "assessment_id": state.get("assessment_id", ""),
        "抽取事实数": len(state.get("facts", [])),
        "抽取任职数": len(state.get("employments", [])),
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
        "抽取统计": state.get("extract_stats") or {},
        "抽取警告": state.get("extract_warnings") or [],
    }


def resume_run(run_id: str, decision: Optional[str] = None) -> Dict[str, Any]:
    """恢复一条被 interrupt 暂停的任务。

    - decision 缺省时：读取该评估未消费的审批（进程在"审批入库"与"恢复"之间崩溃的场景）
    - 有审批则 Command(resume=decision) 恢复；无审批返回当前状态（仍待审批）
    - 审批消费顺序（T1 修复）：只在恢复成功后才标记已消费。恢复中途崩溃时审批保持
      未消费，下次重放可再次读取并继续；线程已恢复完成时重放只补收尾、不重复执行。
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

    graph = build_resume_graph()
    cfg = {"configurable": {"thread_id": run["thread_id"]}}

    with _GRAPH_LOCK:
        if decision is None:
            # 无待消费审批：返回当前状态（仍待审批）。
            # 线程若已跑完（上次恢复在收尾前崩溃），补齐 finished 状态。
            snapshot = graph.get_state(cfg)
            if not snapshot.next and snapshot.values.get("approval"):
                repo.update_run(run_id, status="finished",
                                finished_at=__import__("datetime").datetime.now().isoformat())
            return _report_from_state(snapshot.values)

        # 重放保护：线程已到达 END（上次恢复成功、检查点已保存，仅消费/收尾未完成）
        # → 不重复恢复，只补消费与收尾。
        snapshot = graph.get_state(cfg)
        if not snapshot.next:
            if review is not None:
                repo.mark_review_consumed(review.id)
            repo.update_run(run_id, status="finished",
                            finished_at=__import__("datetime").datetime.now().isoformat())
            return _report_from_state(snapshot.values)

        # 正常恢复：invoke 成功后才消费审批、标记 finished。
        # 崩溃点分析：invoke 前退出 → 审批未消费，下次重放正常恢复；
        # invoke 后退出 → 检查点已在 END，下次走重放保护分支，不重复执行。
        repo.update_run(run_id, status="resumed")
        result = graph.invoke(Command(resume=decision), cfg)
        if review is not None:
            repo.mark_review_consumed(review.id)
        repo.update_run(run_id, status="finished",
                        finished_at=__import__("datetime").datetime.now().isoformat())
        return result.get("final_report") or _report_from_state(result)
