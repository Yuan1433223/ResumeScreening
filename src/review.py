# -*- coding: utf-8 -*-
"""审批服务（对应 DEVELOPMENT_SPEC.md §6.4/§7）。

- submit_review：expected_version 不匹配 → 冲突（旧审批不能自动批准新版本）；
  以 request_id 幂等入库，再恢复同一条任务。
- resume_run：读取未消费审批并恢复；进程在入库与恢复之间退出时，恢复仍可继续。
"""
from __future__ import annotations

import uuid
from typing import Optional

from .repository import Repository, get_repository
from .schemas import Review, ReviewDecision


class VersionConflictError(Exception):
    """评估版本已更新，旧审批无效。"""


class ReviewNotFoundError(Exception):
    """指定的评估或审批不存在。"""


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def submit_review(
    assessment_id: str,
    expected_version: str,
    decision: ReviewDecision | str,
    reason: str = "",
    reviewer_id: str = "hr",
    request_id: str = "",
    repo: Repository | None = None,
) -> Review:
    """提交审批：幂等 + 版本校验。重复 request_id 返回同一结果。

    流程：① 校验评估存在且版本匹配；② 审批幂等入库；③ 恢复 LangGraph 任务。
    入库与恢复之间进程退出时，resume_run 会读取未消费审批继续执行。
    """
    repo = repo or get_repository()
    assessment = repo.get_assessment(assessment_id)
    if assessment is None:
        raise ReviewNotFoundError(f"评估不存在: {assessment_id}")

    # 版本冲突：评分规则/事实更改后，旧审批不能自动批准新版本
    if assessment["job_version"] != expected_version:
        raise VersionConflictError(
            f"评估版本不匹配: 当前 {assessment['job_version']} != 期望 {expected_version}"
        )

    if isinstance(decision, str):
        decision = ReviewDecision(decision)

    review = Review(
        id=_new_id("rev"),
        assessment_id=assessment_id,
        assessment_version=assessment["job_version"],
        decision=decision,
        reason=reason,
        reviewer_id=reviewer_id,
        request_id=request_id or _new_id("req"),
    )
    created = repo.create_review(review)
    if created.request_id != review.request_id:
        # 重复提交：已有记录，直接返回（幂等）
        return created

    # 恢复 LangGraph 任务（审批已入库，进程崩溃后仍可恢复）
    _resume_assessment(assessment_id, repo)
    return created


def _resume_assessment(assessment_id: str, repo: Repository) -> None:
    """从评估反查 run/thread_id，恢复任务。"""
    runs = repo.list_runs(assessment_id=assessment_id)
    run_row = runs[0] if runs else None
    if run_row is None:
        return  # 无持久化任务（纯函数场景，如测试直接调用 assess）
    from .workflow import resume_run

    resume_run(run_row["id"])


def pending_review(assessment_id: str, repo: Repository | None = None) -> Optional[Review]:
    """查询该评估尚未消费的审批（供恢复程序使用）。"""
    repo = repo or get_repository()
    return repo.get_unconsumed_review(assessment_id)
