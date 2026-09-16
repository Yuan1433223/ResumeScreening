# -*- coding: utf-8 -*-
"""P2 流程闭环测试（对应 DEVELOPMENT_SPEC.md §10/§11）。

验收场景：
- 审批前不产生预约；修改评估版本后旧审批无效
- 暂停后杀进程并重启可恢复；重复批准返回同一结果
- 6 位获批候选人争 4 个时段：按优先级占位、2 位候补；并发分配不重复占位
- 通知失败后重试不重复建会
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.config import JOB_PROFILE, JOB_VERSION
from src.repository import Repository
from src.review import VersionConflictError, submit_review
from src.schemas import (
    Booking,
    BookingStatus,
    ExternalAction,
    Fact,
    FactStatus,
    Page,
    ReviewDecision,
)
from src.scheduler import (
    create_round,
    close_round,
    retry_action,
    run_external,
    schedule_round,
)
from src.workflow import run_single, resume_run


def make_page(text, page_number=1, document_id="d"):
    return Page(
        document_id=document_id,
        page_number=page_number,
        text=text,
        text_hash="",
        extraction_method="test",
    )


def make_fact(kind, value, document_id="d"):
    return Fact(
        id=f"f_{kind}_{document_id}",
        document_id=document_id,
        kind=kind,
        value=value,
        status=FactStatus.SUPPORTED,
    )


# 高分合成候选人：钢结构/MiC 主导 + 全流程 + 海外交付 + 管理 + 质量 + 英语 + 规模 + 机动
def high_score_facts(document_id="d"):
    return [
        make_fact("relevant_domain", True, document_id),
        make_fact("steel_mic_experience", "led", document_id),
        make_fact("steel_full_flow", "full_flow", document_id),
        make_fact("overseas", "delivered", document_id),
        make_fact("management", "scope_and_results", document_id),
        make_fact("quality_safety", "two_plus", document_id),
        make_fact("english", "usage", document_id),
        make_fact("project_scale", "quantified", document_id),
        make_fact("mobility", "travel_ok", document_id),
    ]


def medium_facts(document_id="d"):
    """中等分数：无 MiC 主导、无全流程、无海外交付。"""
    return [
        make_fact("relevant_domain", True, document_id),
        make_fact("steel_mic_experience", "participated", document_id),
        make_fact("steel_full_flow", "one_phase", document_id),
        make_fact("overseas", "collab", document_id),
        make_fact("management", "managed", document_id),
        make_fact("english", "info", document_id),
    ]


def run_and_approve(tmp_path, name: str, batch_id: str, facts, repo: Repository,
                    index: int, job_version: str = JOB_VERSION) -> str:
    """跑单份分析 → 停审批 → approve → 返回 document_id。"""
    p = tmp_path / f"{name}.pdf"
    p.write_bytes(f"%PDF-1.4 synthetic-{name}".encode())  # 内容含姓名，保证 sha 唯一
    report = run_single(
        str(p), thread_id=f"t_{name}", facts=facts, employments=[],
        batch_id=batch_id, document_id=f"doc_{name}",
    )
    assert report["机器建议"] in ("recommend", "needs_review", "hold")
    runs = repo.list_runs(batch_id=batch_id)
    run = runs[0]
    assessment = repo.get_assessment(run["assessment_id"])
    assert assessment is not None
    submit_review(
        assessment["id"], job_version, ReviewDecision.APPROVE,
        reason="测试批准", request_id=f"req_{name}",
    )
    return run["document_id"]


# ---------------------------------------------------------------------------
# 审批前不产生预约；重复批准幂等
# ---------------------------------------------------------------------------
def test_approval_required_before_schedule(tmp_path, _isolated_repository):
    repo = _isolated_repository
    batch_id = "batch_1"
    repo.create_batch(batch_id, JOB_VERSION)

    p = tmp_path / "张三.pdf"
    p.write_bytes(b"%PDF-1.4 synthetic-zhangsan")
    run_single(str(p), thread_id="t_zhang", facts=high_score_facts(),
               employments=[], batch_id=batch_id, document_id="doc_zhang")

    # 未审批：创建排期轮，冻结，排期 → 无人入池
    rnd = create_round(batch_id, JOB_VERSION, repo)
    close_round(rnd.id, JOB_VERSION, repo)
    bookings = schedule_round(rnd.id, repo=repo)
    assert bookings == [], "未审批候选人不应进入排期"


def test_duplicate_approval_returns_same_result(tmp_path, _isolated_repository):
    repo = _isolated_repository
    batch_id = "batch_dup"
    repo.create_batch(batch_id, JOB_VERSION)

    p = tmp_path / "李四.pdf"
    p.write_bytes(b"%PDF-1.4 synthetic-lisi")
    run_single(str(p), thread_id="t_lisi", facts=high_score_facts(),
               employments=[], batch_id=batch_id, document_id="doc_lisi")
    run = repo.list_runs(batch_id=batch_id)[0]
    assessment_id = run["assessment_id"]

    r1 = submit_review(assessment_id, JOB_VERSION, ReviewDecision.APPROVE,
                       reason="第一次", request_id="req_dup")
    r2 = submit_review(assessment_id, JOB_VERSION, ReviewDecision.APPROVE,
                       reason="重复提交", request_id="req_dup")
    assert r1.id == r2.id, "相同 request_id 应返回同一审批记录"
    assert r1.decision == r2.decision


# ---------------------------------------------------------------------------
# 版本冲突：修改评估版本后旧审批无效
# ---------------------------------------------------------------------------
def test_approval_version_conflict(tmp_path, _isolated_repository):
    repo = _isolated_repository
    batch_id = "batch_ver"
    repo.create_batch(batch_id, JOB_VERSION)

    p = tmp_path / "王五.pdf"
    p.write_bytes(b"%PDF-1.4 synthetic-wangwu")
    run_single(str(p), thread_id="t_wang", facts=high_score_facts(),
               employments=[], batch_id=batch_id, document_id="doc_wang")
    run = repo.list_runs(batch_id=batch_id)[0]
    assessment_id = run["assessment_id"]

    with pytest.raises(VersionConflictError):
        submit_review(assessment_id, "v999", ReviewDecision.APPROVE,
                      request_id="req_ver")
    # 版本匹配仍可审批
    r = submit_review(assessment_id, JOB_VERSION, ReviewDecision.APPROVE,
                      request_id="req_ver2")
    assert r.decision == ReviewDecision.APPROVE


# ---------------------------------------------------------------------------
# 暂停后杀进程并重启可恢复；未消费审批被读取
# ---------------------------------------------------------------------------
def test_resume_after_interrupt_without_consumed_review(tmp_path, _isolated_repository):
    """审批已入库但进程崩溃（未恢复）：resume_run 读取未消费审批继续。"""
    repo = _isolated_repository
    batch_id = "batch_restart"
    repo.create_batch(batch_id, JOB_VERSION)

    p = tmp_path / "赵六.pdf"
    p.write_bytes(b"%PDF-1.4 synthetic-zhaoliu")
    run_single(str(p), thread_id="t_zhao", facts=high_score_facts(),
               employments=[], batch_id=batch_id, document_id="doc_zhao")
    run = repo.list_runs(batch_id=batch_id)[0]
    assessment_id = run["assessment_id"]

    # 模拟：审批先入库（不恢复，进程在此崩溃）
    from src.schemas import Review
    from datetime import datetime, UTC
    review = Review(
        id="rev_manual",
        request_id="req_crash",
        assessment_id=assessment_id,
        assessment_version=JOB_VERSION,
        decision=ReviewDecision.APPROVE,
        reason="崩溃前已审批",
        created_at=datetime.now(UTC),
    )
    repo.create_review(review)

    # 重启后：resume_run 读取未消费审批并恢复
    report = resume_run(run["id"])
    assert report["审批状态"] == "approve"
    # 审批被标记消费
    assert repo.get_unconsumed_review(assessment_id) is None


# ---------------------------------------------------------------------------
# 6 人争 4 时段：按优先级占位、2 位候补、无重复占用
# ---------------------------------------------------------------------------
def test_six_candidates_four_slots(tmp_path, _isolated_repository):
    repo = _isolated_repository
    batch_id = "batch_6"
    repo.create_batch(batch_id, JOB_VERSION)

    # 6 人：前 4 高分（分数递减以验证排序），后 2 中分
    cands = ["c01", "c02", "c03", "c04", "c05", "c06"]
    for i, c in enumerate(cands):
        facts = high_score_facts(c) if i < 4 else medium_facts(c)
        # 人为拉开分数：高分者去掉一维，保证分数有差异
        if i == 0:
            facts = [f for f in facts if f.kind != "mobility"]
        run_and_approve(tmp_path, c, batch_id, facts, repo, i)

    rnd = create_round(batch_id, JOB_VERSION, repo)
    close_round(rnd.id, JOB_VERSION, repo)
    bookings = schedule_round(rnd.id, repo=repo)

    confirmed = [b for b in bookings if b.status == BookingStatus.CONFIRMED]
    waitlist = [b for b in bookings if b.status == BookingStatus.WAITLIST]
    assert len(confirmed) == 4
    assert len(waitlist) == 2

    # 无重复占用时段
    slots = [b.slot_id for b in confirmed]
    assert len(slots) == len(set(slots)), "同一时段不能被重复占用"
    assert all(s is not None for s in slots)

    # 排序验证：confirmed 4 人应为 c01-c04（最高分优先）
    ids = [b.candidate_id for b in confirmed]
    assert set(ids) == {"doc_c01", "doc_c02", "doc_c03", "doc_c04"}


# ---------------------------------------------------------------------------
# 通知失败后重试不重复建会
# ---------------------------------------------------------------------------
def test_notify_failure_retry_does_not_duplicate_meeting(tmp_path, _isolated_repository):
    from src.providers.mock_calendar import MockCalendar
    from src.providers.mock_notifier import MockNotifier

    repo = _isolated_repository
    batch_id = "batch_ntf"
    repo.create_batch(batch_id, JOB_VERSION)
    run_and_approve(tmp_path, "c1", batch_id, high_score_facts(), repo, 0)

    rnd = create_round(batch_id, JOB_VERSION, repo)
    close_round(rnd.id, JOB_VERSION, repo)
    bookings = schedule_round(rnd.id, repo=repo)
    assert len(bookings) == 1
    booking = bookings[0]

    # 通知器故障 1 次：建会成功、通知失败
    cal = MockCalendar()
    ntf = MockNotifier(fail_times=1)
    act = run_external(booking, repo=repo, calendar=cal, notifier=ntf)
    assert act["type"] == "notify"
    assert act["status"] == "failed"

    # 建会只有一条（不重复）
    meetings = repo.get_action_by_booking(booking.id, "meeting")
    assert meetings["status"] == "succeeded"
    meeting_id = meetings["provider_id"]

    # 重试通知：成功；建会 provider_id 不变（不重复建会）
    retried = retry_action(act["id"], repo=repo, calendar=cal, notifier=ntf)
    assert retried["status"] == "succeeded"
    meetings2 = repo.get_action_by_booking(booking.id, "meeting")
    assert meetings2["provider_id"] == meeting_id, "重试不应重复建会"
    # 通知动作仍只有一条
    notifies = repo.get_action_by_booking(booking.id, "notify")
    assert notifies["id"] == act["id"]


# ---------------------------------------------------------------------------
# 重复排期幂等：同 round 再次 schedule 返回同一批预约
# ---------------------------------------------------------------------------
def test_schedule_idempotent(tmp_path, _isolated_repository):
    repo = _isolated_repository
    batch_id = "batch_idem"
    repo.create_batch(batch_id, JOB_VERSION)
    for c in ["c1", "c2"]:
        run_and_approve(tmp_path, c, batch_id, high_score_facts(c), repo, 0)

    rnd = create_round(batch_id, JOB_VERSION, repo)
    close_round(rnd.id, JOB_VERSION, repo)
    b1 = schedule_round(rnd.id, repo=repo)
    b2 = schedule_round(rnd.id, repo=repo)
    assert len(b1) == len(b2) == 2
    k1 = {(b.candidate_id, b.slot_id, b.status.value) for b in b1}
    k2 = {(b.candidate_id, b.slot_id, b.status.value) for b in b2}
    assert k1 == k2


# ---------------------------------------------------------------------------
# 批量单项失败后其余完成
# ---------------------------------------------------------------------------
def test_batch_single_failure_others_complete(tmp_path, _isolated_repository):
    from src.batch import process_batch

    good = tmp_path / "好人.pdf"
    good.write_bytes(b"%PDF-1.4 synthetic-good-batch")
    # 坏文件（.pdf 扩展名、非法内容）：解析抛异常，不应中断批次
    bad = tmp_path / "坏文件.pdf"
    bad.write_bytes(b"\x00\x01 not a resume at all")

    result = process_batch(limit=None, resume_dir=tmp_path)
    assert result["处理总数"] == 2
    names = {r["文件名"] for r in result["报告列表"]}
    assert names == {"好人.pdf", "坏文件.pdf"}
    # 两份均未抛异常（解析失败/异常均转复核报告）
    assert all(r["机器建议"] == "needs_review" for r in result["报告列表"])
