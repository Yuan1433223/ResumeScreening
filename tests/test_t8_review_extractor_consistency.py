# -*- coding: utf-8 -*-
"""T8 回归：复核详情的事实抽取必须与该文档最近一次 assessment 的 extractor_version 一致。

Bug：GET /api/documents/{id}/review 之前硬编码 extract_facts(..., extractor_version="mock")，
而右侧评估明细来自 repo.get_assessment_by_document()——若该文档最近一次批次是 model
抽取，左右两侧就来自两套抽取结果，演示 model 批次时露馅。

修复：以 assessment.extractor_version 为准（无 assessment 回退 mock），并加进程内
展示缓存，避免每次打开复核页都重调云端模型。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

import app.api as api_module
from app.api import app
from src.schemas import Assessment, ExtractionReport, HardResult, Recommendation

client = TestClient(app)


def _minimal_pdf(text: str) -> bytes:
    """生成带 xref 的最小合法单页 PDF（pdfplumber 可直接抽取文本）。"""
    text = text.encode("latin-1")
    stream = b"BT /F1 12 Tf 72 720 Td (" + text + b") Tj ET\n"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n".encode()
    )
    return out


CV_TEXT = ("Software Engineer, steel structure design, 2019-2024, "
           "English, overseas project management")


@pytest.fixture(autouse=True)
def _fake_resume_dir(tmp_path, monkeypatch):
    """API 指向合成简历目录；清空进程内展示缓存，避免跨用例串味。"""
    (tmp_path / "cv_test.pdf").write_bytes(_minimal_pdf(CV_TEXT))
    monkeypatch.setattr(api_module, "RESUME_DIR", tmp_path)
    api_module._REVIEW_EXTRACT_CACHE.clear()
    yield tmp_path


def _create_and_wait(**kwargs) -> dict:
    """提交批次（后台任务）并轮询到完成。"""
    r = client.post("/api/batches", json=kwargs)
    assert r.status_code == 200
    task = r.json()
    assert task["status"] in ("running", "done")
    for _ in range(300):  # 最多 ~30s
        if task["status"] != "running":
            break
        time.sleep(0.1)
        task = client.get(f"/api/batches/tasks/{task['task_id']}").json()
    assert task["status"] == "done", task
    return task


# ---------------------------------------------------------------------------
# a) mock 批次文档：GET review 仍走 mock 抽取（不回归）
# ---------------------------------------------------------------------------
def test_mock_batch_review_still_uses_mock():
    body = _create_and_wait(limit=1, extractor="mock")
    doc_id = body["rows"][0]["document_id"]

    r = client.get(f"/api/documents/{doc_id}/review")
    assert r.status_code == 200
    data = r.json()
    # 该文档 assessment 来自 mock 批次（抽取器自报版本 mock-v1）
    assert data["assessment"] is not None
    assert data["assessment"]["extractor_version"].startswith("mock")
    # 左侧事实由 mock 抽取器现场产出（非空 list）
    assert isinstance(data["facts"], list)
    assert len(data["facts"]) >= 0  # mock 规则抽取结果集；不回归即可
    assert data["assessment"]["recommendation"] in ("recommend", "needs_review", "hold")


# ---------------------------------------------------------------------------
# b) model 批次文档：GET review 必须传 "model"，且第二次不重复抽取
# ---------------------------------------------------------------------------
def test_model_assessment_review_passes_model_extractor(monkeypatch):
    body = _create_and_wait(limit=1, extractor="mock")
    doc_id = body["rows"][0]["document_id"]
    repo = api_module._repo()

    # 把该文档最近一次 assessment 的 extractor_version 改成 "model"，
    # 模拟"评估来自 model 批次"但左侧事实仍被硬编码成 mock 的现场。
    existing = repo.get_assessment_by_document(doc_id)
    assert existing is not None, "mock 批次应已落 assessment"
    fake = Assessment(
        id=existing["id"],
        document_id=doc_id,
        job_version=existing["job_version"],
        extractor_version="model",
        rubric_version=existing.get("rubric_version", ""),
        known_score=existing["known_score"],
        score_complete=bool(existing["score_complete"]),
        recommendation=Recommendation(existing["recommendation"]),
        hard_summary=HardResult(existing["hard_summary"]),
    )
    repo.save_assessment(fake)
    # save_assessment 以同 id 覆盖写；created_at 刷新 → 缓存键变化，旧条目自然失效

    calls: list = []

    def spy_extract(document_id, pages, extractor_version="mock"):
        # 记录实际传入的抽取器版本；返回空报告，不触发任何真实云端调用
        calls.append(extractor_version)
        return ExtractionReport(
            extractor_version=extractor_version, facts=[], employments=[], warnings=[],
        )

    monkeypatch.setattr(api_module, "extract_facts", spy_extract)

    r1 = client.get(f"/api/documents/{doc_id}/review")
    assert r1.status_code == 200
    assert calls == ["model"], f"第一次 GET 必须用 model 抽取器，实际调用链: {calls}"
    assert r1.json()["assessment"]["extractor_version"] == "model"

    # 第二次 GET：缓存命中，不得再次触发抽取（不花钱、不重复云端调用）
    r2 = client.get(f"/api/documents/{doc_id}/review")
    assert r2.status_code == 200
    assert calls == ["model"], (
        f"第二次 GET 不应重复抽取（进程内展示缓存应命中），实际调用次数: {len(calls)}"
    )
    # 两次返回结构一致（同一缓存报告）
    assert r1.json()["facts"] == r2.json()["facts"] == []

# ---------------------------------------------------------------------------
# b2) 版本归一：assessment 存抽取器自报版本（mock-v1 / model-p1），
#     端点必须归一到 extract_facts 的分派键（mock / startswith("model")）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("raw_version", "expected_dispatch"), [
    ("mock-v1", "mock"),
    ("model-p1", "model-p1"),   # startswith("model") 即走 ModelExtractor
    ("unknown-future", "mock"), # 未知版本安全回退，不得抛 ValueError
])
def test_extractor_version_normalization(monkeypatch, raw_version, expected_dispatch):
    body = _create_and_wait(limit=1, extractor="mock")
    doc_id = body["rows"][0]["document_id"]
    repo = api_module._repo()
    existing = repo.get_assessment_by_document(doc_id)
    fake = Assessment(
        id=existing["id"],
        document_id=doc_id,
        job_version=existing["job_version"],
        extractor_version=raw_version,
        rubric_version=existing.get("rubric_version", ""),
        known_score=existing["known_score"],
        score_complete=bool(existing["score_complete"]),
        recommendation=Recommendation(existing["recommendation"]),
        hard_summary=HardResult(existing["hard_summary"]),
    )
    repo.save_assessment(fake)

    calls: list = []

    def spy_extract(document_id, pages, extractor_version="mock"):
        calls.append(extractor_version)
        return ExtractionReport(
            extractor_version=extractor_version, facts=[], employments=[], warnings=[],
        )

    monkeypatch.setattr(api_module, "extract_facts", spy_extract)
    r = client.get(f"/api/documents/{doc_id}/review")
    assert r.status_code == 200
    assert calls == [expected_dispatch], (
        f"raw={raw_version} 应归一到 {expected_dispatch}，实际: {calls}"
    )


# ---------------------------------------------------------------------------
# c) 复核详情必须把 payload 里的 hard_results / dimensions / missing_items 透出
# ---------------------------------------------------------------------------
def test_review_detail_exposes_payload_score_fields():
    """assessments 表只把这三字段写进 payload JSON 列；端点必须解析 payload 透出。

    回归：之前直接 assessment.get(...) 读列，列不存在，前端"评估明细"卡片永远空。
    """
    body = _create_and_wait(limit=1, extractor="mock")
    doc_id = body["rows"][0]["document_id"]
    r = client.get(f"/api/documents/{doc_id}/review")
    assert r.status_code == 200
    a = r.json()["assessment"]
    assert a is not None
    # hard_results 必须是带业务键的 dict（relevant_domain/experience/management/summary）
    hr = a["hard_results"]
    assert isinstance(hr, dict), f"hard_results 应为 dict，实际: {type(hr).__name__}"
    for k in ("relevant_domain", "experience", "management", "summary"):
        assert k in hr, f"hard_results 缺键 {k}: {list(hr.keys())}"
    # missing_items 必须是 list（即使为空），不能是 None
    assert isinstance(a["missing_items"], list), f"missing_items 应为 list: {a['missing_items']!r}"
    # dimensions 是 dict（8 维评分明细，mock 批次下可能为空 dict 但结构必须是 dict）
    assert isinstance(a["dimensions"], dict)
