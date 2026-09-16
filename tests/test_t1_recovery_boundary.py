# -*- coding: utf-8 -*-
"""T1 恢复边界测试：审批消费时序（workflow.resume_run）。

验收（对应清单 T1）：
- 恢复中途崩溃（invoke 抛异常）→ 审批保持未消费，再次恢复可重放并完成
- 恢复成功但收尾前崩溃（消费/置 finished 前退出）→ 再次恢复不重复执行，只补收尾
- 审批状态（未消费→已消费）与 run 状态（pending→resumed→finished）可核对一致
"""
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import src.workflow as wf
from src.config import JOB_VERSION
from src.schemas import Fact, FactStatus, Review, ReviewDecision
from src.workflow import resume_run, run_single


def make_fact(kind, value, document_id="d"):
    return Fact(
        id=f"f_{kind}_{document_id}",
        document_id=document_id,
        kind=kind,
        value=value,
        status=FactStatus.SUPPORTED,
    )


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


def seed_paused_run(tmp_path, repo, name="候选A", batch_id="batch_t1"):
    """跑单份分析停在审批节点，并手工入库一条未消费审批（模拟崩溃前已审批）。"""
    repo.create_batch(batch_id, JOB_VERSION)
    p = tmp_path / f"{name}.pdf"
    p.write_bytes(f"%PDF-1.4 synthetic-{name}".encode())
    run_single(
        str(p), thread_id=f"t_{name}", facts=high_score_facts(),
        employments=[], batch_id=batch_id, document_id=f"doc_{name}",
    )
    run = repo.list_runs(batch_id=batch_id)[0]
    assessment_id = run["assessment_id"]
    review = Review(
        id="rev_t1", request_id="req_t1", assessment_id=assessment_id,
        assessment_version=JOB_VERSION, decision=ReviewDecision.APPROVE,
        reason="T1 测试审批", created_at=datetime.now(UTC),
    )
    repo.create_review(review)
    return run, assessment_id


def test_resume_crash_mid_invoke_then_replay(tmp_path, _isolated_repository, monkeypatch):
    """恢复中途崩溃（invoke 抛异常）：审批不得被消费，再次恢复可重放完成。"""
    repo = _isolated_repository
    run, assessment_id = seed_paused_run(tmp_path, repo)

    real_build = wf.build_resume_graph
    calls = {"n": 0}

    class FlakyGraph:
        def __init__(self, inner):
            self._inner = inner

        def invoke(self, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("模拟进程在恢复中途退出")
            return self._inner.invoke(*a, **k)

        def get_state(self, *a, **k):
            return self._inner.get_state(*a, **k)

    def flaky_build():
        return FlakyGraph(real_build())

    monkeypatch.setattr(wf, "build_resume_graph", flaky_build)

    # 第一次恢复：invoke 中途崩溃 → 审批必须保持未消费
    with pytest.raises(RuntimeError, match="恢复中途退出"):
        resume_run(run["id"])
    assert repo.get_unconsumed_review(assessment_id) is not None, "崩溃后审批不应被消费"

    # 第二次恢复：重放成功，审批消费一次，run 置 finished
    report = resume_run(run["id"])
    assert report["审批状态"] == "approve"
    assert repo.get_unconsumed_review(assessment_id) is None
    assert repo.get_run(run["id"])["status"] == "finished"
    assert calls["n"] == 2, "invoke 应恰好执行两次（第一次崩溃，第二次成功）"


def test_resume_crash_after_invoke_replay_without_reexecute(tmp_path, _isolated_repository, monkeypatch):
    """恢复成功但收尾前崩溃（消费时退出）：再次恢复不重复执行图，只补消费与收尾。"""
    repo = _isolated_repository
    run, assessment_id = seed_paused_run(tmp_path, repo)

    calls = {"consume": 0, "invoke": 0}

    class FlakyRepo:
        def __getattr__(self, name):
            return getattr(repo, name)

        def mark_review_consumed(self, review_id):
            calls["consume"] += 1
            if calls["consume"] == 1:
                raise RuntimeError("模拟在消费审批时退出")
            return repo.mark_review_consumed(review_id)

    real_build = wf.build_resume_graph

    class CountingGraph:
        def __init__(self, inner):
            self._inner = inner

        def invoke(self, *a, **k):
            calls["invoke"] += 1
            return self._inner.invoke(*a, **k)

        def get_state(self, *a, **k):
            return self._inner.get_state(*a, **k)

    def build():
        return CountingGraph(real_build())

    monkeypatch.setattr(wf, "get_repository", lambda: FlakyRepo())
    monkeypatch.setattr(wf, "build_resume_graph", build)

    # 第一次恢复：invoke 已成功（检查点到达 END），消费时崩溃
    with pytest.raises(RuntimeError, match="消费审批时退出"):
        resume_run(run["id"])
    assert repo.get_unconsumed_review(assessment_id) is not None
    assert repo.get_run(run["id"])["status"] != "finished"

    # 第二次恢复：走重放保护，不重新 invoke，只补消费与收尾
    report = resume_run(run["id"])
    assert report["审批状态"] == "approve"
    assert repo.get_unconsumed_review(assessment_id) is None
    assert repo.get_run(run["id"])["status"] == "finished"
    assert calls["invoke"] == 1, "重放不得重复执行图"


def test_approval_and_run_state_ledger(tmp_path, _isolated_repository):
    """正常路径：审批/run 状态机可核对，恢复完成后重复调用幂等。"""
    repo = _isolated_repository
    run, assessment_id = seed_paused_run(tmp_path, repo)

    # 恢复前：审批未消费，run pending
    assert repo.get_unconsumed_review(assessment_id) is not None
    assert repo.get_run(run["id"])["status"] == "pending"

    report = resume_run(run["id"])
    assert report["审批状态"] == "approve"

    r = repo.get_run(run["id"])
    assert r["status"] == "finished"
    assert r["finished_at"] is not None
    assert repo.get_unconsumed_review(assessment_id) is None

    # 已完成后再恢复：返回同一结果，不抛错、不重复消费
    report2 = resume_run(run["id"])
    assert report2["审批状态"] == "approve"
    assert repo.get_unconsumed_review(assessment_id) is None
