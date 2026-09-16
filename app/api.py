# -*- coding: utf-8 -*-
"""T5a FastAPI 服务层：批次提交/查询、复核详情、审批。

- 复用现有 workflow/repository，不重写业务逻辑
- 批次同步执行（mock 抽取，秒级）；模型抽取走 eval runner（T5c 再接入）
- 数据以 repository（SQLite）为唯一事实来源
"""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel

from src.batch import list_resumes
from src.config import JOB_PROFILE, JOB_TITLE, RESUME_DIR
from src.evidence import validate_evidence
from src.extraction import extract_facts
from src.parser import parse_document
from src.providers.mock_calendar import get_calendar
from src.providers.mock_notifier import get_notifier
from src.repository import get_repository
from src.review import submit_review
from src.schemas import Page, SchedulingRound
from src.scheduler import _list_rounds, close_round, create_round, run_external, schedule_round
from src.workflow import resume_run, run_single

app = FastAPI(title="ResumeScreening Agent API", version="0.1.0")


class BatchRequest(BaseModel):
    limit: Optional[int] = None
    extractor: str = "mock"


class ApproveRequest(BaseModel):
    decision: str = "approve"   # approve / hold / needs_review
    reason: str = ""


def _repo():
    return get_repository()


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 批次：提交（后台任务）/ 查询
# ---------------------------------------------------------------------------
_BATCH_TASKS: Dict[str, Dict[str, Any]] = {}
_TASKS_LOCK = threading.Lock()


def _task_snapshot(task: Dict[str, Any]) -> dict:
    with _TASKS_LOCK:
        return {
            "task_id": task["task_id"],
            "batch_id": task["batch_id"],
            "extractor": task["extractor"],
            "status": task["status"],
            "total": task["total"],
            "done": task["done"],
            "current_file": task.get("current_file", ""),
            "rows": list(task["rows"]),
            "error": task["error"],
        }


def _batch_worker(task: Dict[str, Any], files: List[Path]) -> None:
    """后台线程串行跑批次；单份失败不阻断；进度逐步写入任务快照。

    线程身份 = {batch_id}__{file_name}：每次批次运行独立线程，避免复用
    旧文档线程的 END/中断检查点（T2 的"线程=文档"语义服务于 CLI 重放；
    工作台换抽取器重跑需要全新分析）。
    """
    batch_id = task["batch_id"]
    extractor = task["extractor"]
    repo = _repo()
    for fp in files:
        try:
            with _TASKS_LOCK:
                task["current_file"] = fp.name
            run_single(
                str(fp),
                JOB_TITLE,
                batch_id=batch_id,
                extractor_version=extractor,
                thread_id=f"{batch_id}__{fp.name}",
            )
        except Exception as e:  # noqa: BLE001
            print(f"[api] 批次 {batch_id} 处理失败 {fp.name}: {e}")
        with _TASKS_LOCK:
            task["done"] += 1
            task["rows"] = [_row_from_run(r) for r in repo.list_runs(batch_id=batch_id)]
    with _TASKS_LOCK:
        task["current_file"] = ""
        task["status"] = "done"


@app.post("/api/batches")
def create_batch(req: BatchRequest) -> dict:
    if req.extractor not in ("mock", "model"):
        raise HTTPException(status_code=400, detail="extractor 须为 mock / model")
    files = list_resumes(RESUME_DIR)
    if req.limit:
        files = files[: req.limit]
    if not files:
        raise HTTPException(status_code=400, detail="简历库为空")
    batch_id = f"batch_{uuid.uuid4().hex[:8]}"
    _repo().create_batch(batch_id, JOB_PROFILE.version)
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    task: Dict[str, Any] = {
        "task_id": task_id,
        "batch_id": batch_id,
        "extractor": req.extractor,
        "status": "running",
        "total": len(files),
        "done": 0,
        "current_file": "",
        "rows": [],
        "error": "",
    }
    with _TASKS_LOCK:
        _BATCH_TASKS[task_id] = task
    threading.Thread(target=_batch_worker, args=(task, files), daemon=True).start()
    return _task_snapshot(task)


@app.get("/api/batches/tasks/{task_id}")
def get_batch_task(task_id: str) -> dict:
    with _TASKS_LOCK:
        task = _BATCH_TASKS.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return _task_snapshot(task)
def _row_from_run(run: Dict[str, Any]) -> dict:
    repo = _repo()
    doc = repo.get_document(run["document_id"]) if run.get("document_id") else None
    assessment = repo.get_assessment(run["assessment_id"]) if run.get("assessment_id") else None
    reviews = repo.get_reviews(run["assessment_id"]) if run.get("assessment_id") else []
    decision = reviews[-1].decision.value if reviews else "pending"
    return {
        "run_id": run["id"],
        "document_id": run["document_id"],
        "candidate": Path(doc["storage_path"]).stem if doc else "",
        "file_name": Path(doc["storage_path"]).name if doc else "",
        "parse_status": run.get("status", ""),
        "extractor_version": (assessment or {}).get("extractor_version", ""),
        "hard_results": (assessment or {}).get("hard_results", {}),
        "known_score": (assessment or {}).get("known_score"),
        "score_complete": bool((assessment or {}).get("score_complete")),
        "recommendation": (assessment or {}).get("recommendation", "needs_review"),
        "missing_items": (assessment or {}).get("missing_items", []),
        "review_decision": decision,
    }


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: str) -> dict:
    rows = [_row_from_run(r) for r in _repo().list_runs(batch_id=batch_id)]
    if not rows:
        raise HTTPException(status_code=404, detail=f"批次不存在: {batch_id}")
    return {
        "batch_id": batch_id,
        "total": len(rows),
        "recommended": sum(1 for r in rows if r["recommendation"] == "recommend"),
        "review_pending": sum(1 for r in rows if r["review_decision"] == "pending"),
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# 复核：原文 + 事实/证据 + 评估
# ---------------------------------------------------------------------------
def _pages_as_models(pages: List[Dict[str, Any]]) -> List[Page]:
    """sqlite 行 → Page 模型（quality_flags 为 JSON 字符串，需解析）。"""
    models = []
    for p in pages:
        row = dict(p)
        if isinstance(row.get("quality_flags"), str):
            try:
                row["quality_flags"] = json.loads(row["quality_flags"])
            except (ValueError, TypeError):
                row["quality_flags"] = []
        models.append(Page(**row))
    return models


def _ensure_pages(document_id: str) -> List[Dict[str, Any]]:
    """取原文页；未落库则按需解析并缓存（save_pages 在此有了真实使用方）。"""
    repo = _repo()
    pages = repo.get_pages(document_id)
    if pages:
        return pages
    doc = repo.get_document(document_id)
    if not doc or not doc.get("storage_path"):
        return []
    parsed_pages, _, _ = parse_document(document_id, doc["storage_path"])
    if parsed_pages:
        repo.save_pages(document_id, parsed_pages)
        pages = repo.get_pages(document_id)
    return pages


@app.get("/api/documents/{document_id}/review")
def get_review_detail(document_id: str) -> dict:
    repo = _repo()
    doc = repo.get_document(document_id)
    pages = _ensure_pages(document_id)
    if not doc and not pages:
        raise HTTPException(status_code=404, detail=f"文档不存在: {document_id}")
    assessment = repo.get_assessment_by_document(document_id)

    page_models = _pages_as_models(pages)
    report = extract_facts(
        document_id,
        page_models,
        extractor_version="mock",
    )
    validation = validate_evidence(report.facts, page_models) if report.facts else None
    return {
        "document_id": document_id,
        "file_name": Path(doc["storage_path"]).name if doc else "",
        "pages": [{"page_number": p["page_number"], "text": p["text"]} for p in pages],
        "facts": [
            {"kind": f.kind, "value": f.value, "status": f.status.value,
             "quote": f.evidences[0].quote if f.evidences else "",
             "page_number": f.evidences[0].page_number if f.evidences else None}
            for f in report.facts
        ],
        "employments": [
            {"start_month": e.start_month, "end_month": e.end_month,
             "is_current": e.is_current, "relevant": e.relevant}
            for e in report.employments
        ],
        "evidence_valid": ({"valid_count": validation.valid_count, "total": validation.total}
                           if validation else None),
        "warnings": report.warnings,
        "assessment": None if not assessment else {
            "job_version": assessment.get("job_version"),
            "hard_results": assessment.get("hard_results", {}),
            "dimensions": assessment.get("dimensions", {}),
            "known_score": assessment.get("known_score"),
            "score_complete": bool(assessment.get("score_complete")),
            "missing_items": assessment.get("missing_items", []),
            "recommendation": assessment.get("recommendation"),
            "extractor_version": assessment.get("extractor_version", ""),
        },
    }


# ---------------------------------------------------------------------------
# 简历库：列表 / 上传
# ---------------------------------------------------------------------------
@app.get("/api/resumes")
def list_resume_files() -> dict:
    files = []
    for p in sorted(list_resumes(RESUME_DIR), key=lambda x: x.name):
        files.append({
            "file_name": p.name,
            "size_bytes": p.stat().st_size,
            "ext": p.suffix.lower().lstrip("."),
        })
    return {"total": len(files), "files": files}


@app.post("/api/upload")
async def upload_resume(file: UploadFile = File(...)) -> dict:
    name = Path(file.filename or "resume.pdf").name
    if Path(name).suffix.lower() not in {".pdf", ".jpg", ".jpeg", ".png", ".bmp"}:
        raise HTTPException(status_code=400, detail="仅支持 pdf/jpg/png/bmp")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="空文件")
    dest = Path(RESUME_DIR) / name
    dest.write_bytes(data)
    return {"file_name": name, "saved": str(dest)}


# ---------------------------------------------------------------------------
# 排期：生成 / 查询（幂等）
# ---------------------------------------------------------------------------
def _scheduling_view(batch_id: str) -> dict:
    repo = _repo()
    rounds = _list_rounds(batch_id, repo)
    if not rounds:
        return {"batch_id": batch_id, "round": None, "slots": [], "bookings": []}
    r = rounds[-1]

    def _cand_name(doc_id: str) -> str:
        doc = repo.get_document(doc_id)
        return Path(doc["storage_path"]).stem if doc else doc_id

    bookings = []
    for b in repo.get_bookings(r["id"]):
        act = repo.get_action_by_booking(b["id"], "notify")
        bookings.append({
            "booking_id": b["id"],
            "candidate": _cand_name(b["candidate_id"]),
            "document_id": b["candidate_id"],
            "slot_id": b["slot_id"],
            "status": b["status"],
            "notify_status": act["status"] if act else "",
        })
    slots = []
    for s in repo.get_slots(r["id"]):
        row = {"slot_id": s["id"], "start": s["start_at"], "end": s["end_at"],
               "status": "open"}
        booked = next(
            (b for b in bookings if b["slot_id"] == s["id"] and b["status"] == "confirmed"),
            None,
        )
        if booked:
            row["status"] = "taken"
            row["candidate"] = booked["candidate"]
            row["document_id"] = booked["document_id"]
        slots.append(row)
    return {
        "batch_id": batch_id,
        "round": {"id": r["id"], "round_no": r["round_no"], "status": r["status"]},
        "slots": slots,
        "bookings": bookings,
    }


@app.get("/api/scheduling")
def get_scheduling() -> dict:
    batch_id = _repo().latest_batch_id()
    if not batch_id:
        return {"batch_id": None, "round": None, "slots": [], "bookings": []}
    return _scheduling_view(batch_id)


@app.post("/api/scheduling/run")
def run_scheduling() -> dict:
    repo = _repo()
    batch_id = repo.latest_batch_id()
    if not batch_id:
        raise HTTPException(status_code=400, detail="尚无批次，请先创建批次")
    rounds = _list_rounds(batch_id, repo)
    if rounds and rounds[-1]["status"] != "open":
        raise HTTPException(status_code=409, detail="该批次已排期（幂等：直接查询即可）")
    if rounds and rounds[-1]["status"] == "open":
        sround = SchedulingRound(**rounds[-1])
    else:
        sround = create_round(batch_id, JOB_PROFILE.version)
    close_round(sround.id, JOB_PROFILE.version)
    bookings = schedule_round(sround.id)
    for b in bookings:
        if b.status.value == "confirmed":
            run_external(b, calendar=get_calendar(), notifier=get_notifier())
    return _scheduling_view(batch_id)


# ---------------------------------------------------------------------------
# 审批：提交决策并恢复任务
# ---------------------------------------------------------------------------
@app.post("/api/documents/{document_id}/approve")
def approve_document(document_id: str, req: ApproveRequest) -> dict:
    repo = _repo()
    assessment = repo.get_assessment_by_document(document_id)
    if not assessment:
        raise HTTPException(status_code=404, detail=f"该文档尚无评估: {document_id}")
    if req.decision not in ("approve", "hold", "needs_review"):
        raise HTTPException(status_code=400, detail="decision 须为 approve/hold/needs_review")

    review = submit_review(
        assessment["id"],
        assessment["job_version"],
        req.decision,
        reason=req.reason,
    )
    runs = repo.list_runs(assessment_id=assessment["id"])
    report: Dict[str, Any] = {}
    if runs:
        report = resume_run(runs[-1]["id"], decision=req.decision)
    return {
        "review_id": review.id,
        "decision": review.decision.value,
        "审批状态": report.get("审批状态", req.decision),
        "面试安排": report.get("面试安排", False),
        "通知状态": report.get("通知状态", "待审批"),
        "综合评分": report.get("综合评分"),
        "机器建议": report.get("机器建议"),
    }
