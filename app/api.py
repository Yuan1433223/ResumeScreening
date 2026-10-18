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

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from src.auth import (
    COOKIE_NAME,
    SESSION_MAX_AGE,
    auth_enabled,
    create_session,
    ensure_auth_configured,
    get_current_user,
    read_session,
    verify_password,
)
from src.batch import list_resumes
from src import config
from src.config import JOB_PROFILE, JOB_TITLE, RESUME_DIR
from src.evidence import validate_evidence
from src.extraction import extract_facts
from src.observability import read_traces, record_trace
from src.parser import parse_document
from src.providers.mock_calendar import get_calendar
from src.providers.mock_notifier import get_notifier
from src.repository import get_repository
from src.review import submit_review
from src.schemas import Page, SchedulingRound
from src.scheduler import _list_rounds, close_round, create_round, run_external, schedule_round
from src.workflow import resume_run, run_single

app = FastAPI(title="ResumeScreening Agent API", version="0.1.0")
# 单用户认证 fail-fast：AUTH_ENABLED=1 但缺 AUTH_USERNAME/AUTH_PASSWORD 时
# 导入即抛错（不带着空密码弱配置起服务）。关闭态此调用为空操作。
ensure_auth_configured()


class BatchRequest(BaseModel):
    limit: Optional[int] = None
    extractor: str = "mock"


class ApproveRequest(BaseModel):
    decision: str = "approve"   # approve / hold / needs_review
    reason: str = ""


class LoginRequest(BaseModel):
    username: str
    password: str


def _repo():
    return get_repository()


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 单用户认证（契约冻结，前端已按此实现；开关 AUTH_ENABLED，默认关闭）
# - 关闭态：三端点存在但空操作，业务路由不拦截。
# - 开启态：login 校验凭据并 Set-Cookie 会话；logout 清 cookie；me 返回当前状态。
# - /api/auth/* 本身始终公开（不挂 get_current_user），/api/health 也始终公开。
# ---------------------------------------------------------------------------
@app.post("/api/auth/login")
def auth_login(req: LoginRequest, response: Response) -> dict:
    # 关闭态：不校验凭据，直接返回空操作成功（前端据此不跳登录页）。
    if not auth_enabled():
        return {"ok": True, "auth_enabled": False}
    if req.username == config.AUTH_USERNAME and verify_password(
        req.password, config.AUTH_PASSWORD
    ):
        response.set_cookie(
            key=COOKIE_NAME,
            value=create_session(req.username),
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return {"ok": True, "username": req.username}
    raise HTTPException(status_code=401, detail="用户名或密码错误")


@app.post("/api/auth/logout")
def auth_logout(response: Response) -> dict:
    # 关闭态也照清一遍（幂等），契约恒为 200 {"ok": true}。
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict:
    if not auth_enabled():
        return {"authenticated": True, "username": None, "auth_enabled": False}
    token = request.cookies.get(COOKIE_NAME, "")
    username = read_session(token) if token else None
    # 开启态未认证也返回 200，仅 authenticated=false（前端据此决定跳登录页）。
    return {
        "authenticated": bool(username),
        "username": username,
        "auth_enabled": True,
    }


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
            report = run_single(
                str(fp),
                JOB_TITLE,
                batch_id=batch_id,
                extractor_version=extractor,
                thread_id=f"{batch_id}__{fp.name}",
            )
            # T6 可观测性：每份简历落一条 trace（抽取统计/校验失败点/审批）
            reviews = repo.get_reviews(report.get("assessment_id", "")) \
                if report.get("assessment_id") else []
            record_trace({
                "batch_id": batch_id,
                "task_id": task["task_id"],
                "extractor": extractor,
                "document_id": report.get("document_id", ""),
                "file_name": report.get("文件名", fp.name),
                "parse_status": report.get("解析状态", ""),
                "facts": report.get("抽取事实数", 0),
                "employments": report.get("抽取任职数", 0),
                "stats": report.get("抽取统计") or {},
                "warnings": report.get("抽取警告") or [],
                "review_decision": reviews[-1].decision.value if reviews else "pending",
                "known_score": report.get("综合评分"),
                "recommendation": report.get("机器建议", "needs_review"),
            })
        except Exception as e:  # noqa: BLE001
            print(f"[api] 批次 {batch_id} 处理失败 {fp.name}: {e}")
            record_trace({
                "batch_id": batch_id,
                "task_id": task["task_id"],
                "extractor": extractor,
                "document_id": "",
                "file_name": fp.name,
                "error": f"批次处理失败: {e}",
            })
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


@app.get("/api/traces")
def list_traces(limit: int = 50, batch_id: str = "") -> dict:
    """T6 可观测性：最近处理 trace（JSONL 落盘，前端不直接暴露）。"""
    rows = read_traces(limit=min(limit, 200), batch_id=batch_id)
    return {"total": len(rows), "rows": rows}
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
# GET /review 的进程内展示缓存：
# facts 没有持久化表（repository 仅 documents/pages/assessments/reviews/runs/...），
# 复核端点每次都现场重抽。model 抽取走云端（OpenAI 兼容），每次打开复核页都重调
# 既慢又花钱且不确定，故按以下轻量键缓存一次抽取结果（dict + 锁，不引第三方、
# 不新建表）：
#   (document_id, extractor_version, assessment_id, assessment_created_at, page_count)
# - document_id：文档维度
# - extractor_version：与该文档最近一次 assessment 行一致（mock / model-p1 等）；
#   换抽取器重跑批次后，assessment 行的该字段变化 → 缓存键自然变化
# - assessment_id + created_at：批次重跑产生新 assessment 版本（save_assessment
#   以同 id INSERT OR REPLACE 覆盖写，但 created_at 刷新）→ 旧缓存自然失效；
#   无 assessment（边界：仅有 pages/doc）时用 ("", "")，回退 mock
# - page_count：页数变化（重新解析/补页）也视为新版本
# 仅服务于 GET 展示层；进程重启清空；不替代批次链路里落库的正式 assessment。
_REVIEW_EXTRACT_CACHE: Dict[tuple, Any] = {}
_REVIEW_CACHE_LOCK = threading.Lock()


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


@app.get("/api/documents/{document_id}/file")
def get_document_file(document_id: str) -> FileResponse:
    """返回原始简历文件（复核页直接呈现，替代不可读的 OCR 文本）。"""
    doc = _repo().get_document(document_id)
    if not doc or not doc.get("storage_path") or not Path(doc["storage_path"]).exists():
        raise HTTPException(status_code=404, detail=f"原始文件不存在: {document_id}")
    media_types = {
        ".pdf": "application/pdf",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".bmp": "image/bmp",
    }
    suffix = Path(doc["storage_path"]).suffix.lower()
    # 关键：可在浏览器内联呈现的类型必须用 Content-Disposition: inline。
    # 之前 FileResponse(filename=...) 默认给 attachment，PDF 在 <iframe> 中会被
    # 浏览器当作下载处理（面板空白、不渲染）；图片走 <img> 子资源请求不受影响，
    # 所以表现为"图片正常、PDF 空白"。未知类型保持 attachment 走下载。
    return FileResponse(
        doc["storage_path"],
        media_type=media_types.get(suffix, "application/octet-stream"),
        filename=Path(doc["storage_path"]).name,
        content_disposition_type=(
            "inline" if suffix in media_types else "attachment"
        ),
    )


@app.get("/api/documents/{document_id}/review")
def get_review_detail(document_id: str) -> dict:
    repo = _repo()
    doc = repo.get_document(document_id)
    pages = _ensure_pages(document_id)
    if not doc and not pages:
        raise HTTPException(status_code=404, detail=f"文档不存在: {document_id}")
    assessment = repo.get_assessment_by_document(document_id)

    # assessments 表只把 hard_results / dimensions / missing_items 序列化进 payload
    # JSON 列（save_assessment 写的是 assessment.model_dump(mode="json")），表本身没有
    # 这三列；直接 assessment.get(...) 永远拿到默认空值。这里防御性解析 payload，
    # 前端复核页"评估明细"卡片依赖这三个字段。known_score/score_complete/
    # recommendation/extractor_version/job_version 是独立列，与 payload 内容一致，
    # 以列值为准（列有 NOT NULL 默认值，即使 payload 损坏也能返回骨架）。
    assessment_payload: Dict[str, Any] = {}
    if assessment:
        raw_payload = assessment.get("payload") or "{}"
        try:
            parsed = json.loads(raw_payload)
            if isinstance(parsed, dict):
                assessment_payload = parsed
        except (ValueError, TypeError):
            assessment_payload = {}

    # 事实抽取必须与该文档最近一次实际批次所用抽取器一致：
    # assessment 行里的 extractor_version 是"该文档最近一次实际批次"的最权威来源；
    # 无 assessment（边界情况）时回退 mock，保持旧行为不回归。
    raw_extractor = (assessment or {}).get("extractor_version") or "mock"
    # assessment 行存的是抽取器自报版本（MockExtractor.version="mock-v1"，
    # ModelExtractor.version="model-p1"）；extract_facts 分派只认 "mock" 或
    # startswith("model")，需归一到分派键。
    if raw_extractor.startswith("mock"):
        dispatch_extractor = "mock"
    elif raw_extractor.startswith("model"):
        dispatch_extractor = raw_extractor
    else:
        dispatch_extractor = "mock"

    page_models = _pages_as_models(pages)

    # 进程内展示缓存：同一 (document_id, 抽取器自报版本, assessment 版本, 页数)
    # 重复打开不重复调用云端模型；批次重跑产生新 assessment 后键变化，自然失效。
    cache_key = (
        document_id,
        raw_extractor,
        (assessment or {}).get("id", ""),
        (assessment or {}).get("created_at", ""),
        len(pages),
    )
    with _REVIEW_CACHE_LOCK:
        report = _REVIEW_EXTRACT_CACHE.get(cache_key)
    if report is None:
        report = extract_facts(
            document_id,
            page_models,
            extractor_version=dispatch_extractor,
        )
        with _REVIEW_CACHE_LOCK:
            _REVIEW_EXTRACT_CACHE[cache_key] = report
    validation = validate_evidence(report.facts, page_models) if report.facts else None
    return {
        "document_id": document_id,
        "file_name": Path(doc["storage_path"]).name if doc else "",
        "ext": Path(doc["storage_path"]).suffix.lower().lstrip(".") if doc else "",
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
            "hard_results": assessment_payload.get("hard_results", {}),
            "dimensions": assessment_payload.get("dimensions", {}),
            "known_score": assessment.get("known_score"),
            "score_complete": bool(assessment.get("score_complete")),
            "missing_items": assessment_payload.get("missing_items", []),
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
