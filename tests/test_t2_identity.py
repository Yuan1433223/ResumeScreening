# -*- coding: utf-8 -*-
"""T2 任务身份分离测试：文档 / 评估版本 / 执行（run=thread）。

验收（对应清单 T2）：
- 同内容（同 sha）重复上传 → 复用同一 document，不重复评分
- 同文件重跑 → 复用同一 run/thread，评估链到真实 run，审批恢复同一任务
- 两轮 batch 同一文件同一 thread；不同文件绝不共用 thread
- 新 job_version 重评估 → 新评估版本键，旧版本审批不能用于新版本
"""
import copy
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import src.workflow as wf
from src.config import JOB_PROFILE, JOB_VERSION
from src.review import VersionConflictError, submit_review
from src.schemas import Fact, FactStatus, ReviewDecision
from src.workflow import run_single


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


def test_duplicate_upload_reuses_document(tmp_path, _isolated_repository):
    """同内容不同路径：复用同一 document；评估版本键唯一，不重复评分。"""
    repo = _isolated_repository
    content = b"%PDF-1.4 synthetic-dupe"
    p1 = tmp_path / "路径1.pdf"
    p1.write_bytes(content)
    p2 = tmp_path / "路径2.pdf"
    p2.write_bytes(content)

    run_single(str(p1), facts=high_score_facts("dupe1"), employments=[])
    run_single(str(p2), facts=high_score_facts("dupe2"), employments=[])

    sha = hashlib.sha256(content).hexdigest()
    doc = repo.get_document_by_sha(sha)
    assert doc is not None, "同内容应命中同一 document"

    runs = repo.list_runs()
    assert len(runs) == 1, "同内容文件应复用同一 run（执行身份 = thread 身份）"
    assert runs[0]["document_id"] == doc["id"]
    assert runs[0]["assessment_id"] == f"{doc['id']}_{JOB_VERSION}"

    # 不重复评分：该 document + 版本 只有一条评估
    assessments = repo._conn.execute(
        "SELECT id FROM assessments WHERE document_id=?", (doc["id"],)
    ).fetchall()
    assert len(assessments) == 1


def test_rerun_same_file_reuses_run_and_resume(tmp_path, _isolated_repository):
    """同文件重跑：复用同一 run/thread，批次归属更新，评估链到真实 run，审批恢复同一任务。"""
    repo = _isolated_repository
    p = tmp_path / "张三.pdf"
    p.write_bytes(b"%PDF-1.4 synthetic-zhangsan")

    run_single(str(p), facts=high_score_facts(), employments=[], batch_id="b1")
    run1 = repo.list_runs()[0]

    run_single(str(p), facts=high_score_facts(), employments=[], batch_id="b2")
    runs = repo.list_runs()
    assert len(runs) == 1, "同文件重跑应复用同一 run"
    r = runs[0]
    assert r["id"] == run1["id"]
    assert r["thread_id"] == run1["thread_id"]
    assert r["batch_id"] == "b2", "重复分析应归属最新批次"
    assert r["status"] == "pending", "新一轮执行应从 pending 开始"
    assert r["assessment_id"] is not None, "评估必须链到真实 run（不能再静默 no-op）"

    # 审批 → 恢复同一任务 → run finished
    submit_review(r["assessment_id"], JOB_VERSION, ReviewDecision.APPROVE,
                  reason="T2", request_id="req_t2_rerun")
    done = repo.get_run(r["id"])
    assert done["status"] == "finished"
    assert done["finished_at"] is not None


def test_batch_cross_run_stable_thread(tmp_path, _isolated_repository):
    """两轮 batch：同一文件同一 thread，不同文件绝不共用 thread。"""
    repo = _isolated_repository
    from src.batch import process_batch

    (tmp_path / "甲.pdf").write_bytes(b"%PDF-1.4 synthetic-batchA")
    (tmp_path / "乙.pdf").write_bytes(b"%PDF-1.4 synthetic-batchB")

    process_batch(limit=None, resume_dir=tmp_path)
    first = {r["thread_id"]: r["document_id"] for r in repo.list_runs()}

    process_batch(limit=None, resume_dir=tmp_path)
    second = {r["thread_id"]: r["document_id"] for r in repo.list_runs()}

    assert first == second, "跨批次 thread 映射必须稳定"
    assert len(first) == 2, "两份文件应有两个独立线程"
    docs = list(first.values())
    assert len(set(docs)) == 2, "不同文件不得共用 thread"
    # 批处理入口不再产生 resume_{idx} 位置编号
    assert not any(t.startswith("resume_") for t in first)


def test_new_job_version_creates_new_assessment(tmp_path, _isolated_repository, monkeypatch):
    """岗位版本升级：新评估版本键，旧版本审批不能用于新版本。"""
    repo = _isolated_repository
    p = tmp_path / "李四.pdf"
    p.write_bytes(b"%PDF-1.4 synthetic-lisi")

    run_single(str(p), facts=high_score_facts(), employments=[], document_id="doc_ver")
    a1 = repo.get_assessment(f"doc_ver_{JOB_VERSION}")
    assert a1 is not None
    submit_review(f"doc_ver_{JOB_VERSION}", JOB_VERSION, ReviewDecision.APPROVE,
                  reason="v1 审批", request_id="req_v1")

    # 岗位规则升级到 v2
    prof2 = copy.deepcopy(JOB_PROFILE)
    prof2.version = "v2"
    monkeypatch.setattr(wf, "JOB_PROFILE", prof2)
    monkeypatch.setattr(wf, "JOB_VERSION", "v2")

    run_single(str(p), facts=high_score_facts(), employments=[], document_id="doc_ver")
    a2 = repo.get_assessment("doc_ver_v2")
    assert a2 is not None
    assert a2["job_version"] == "v2"
    assert a2["id"] != a1["id"], "新版本必须使用新评估版本键"

    # 旧版本号提交到新评估 → 版本冲突
    with pytest.raises(VersionConflictError):
        submit_review("doc_ver_v2", "v1", ReviewDecision.APPROVE, request_id="req_v2_bad")
    # 新版本匹配 → 可审批
    r = submit_review("doc_ver_v2", "v2", ReviewDecision.APPROVE, request_id="req_v2")
    assert r.decision == ReviewDecision.APPROVE
    assert repo.get_run(repo.list_runs()[0]["id"])["status"] == "finished"
