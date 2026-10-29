# -*- coding: utf-8 -*-
"""T5a API 测试：批次、复核、审批（TestClient + 隔离数据库 + 合成 PDF，不碰真实网络/简历库）。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

import app.api as api_module
from app.api import app

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
CV_TEXT_2 = ("Mechanical engineer, product design, 2016-2020, "
             "quality control, Mandarin")


@pytest.fixture(autouse=True)
def _fake_resume_dir(tmp_path, monkeypatch):
    """API 指向合成简历目录：两份不同内容 PDF（避免 sha 去重合并为一行）。"""
    (tmp_path / "cv_test.pdf").write_bytes(_minimal_pdf(CV_TEXT))
    (tmp_path / "cv_test2.pdf").write_bytes(_minimal_pdf(CV_TEXT_2))
    monkeypatch.setattr(api_module, "RESUME_DIR", tmp_path)
    yield tmp_path


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


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


def test_create_batch_mock():
    body = _create_and_wait(limit=2)
    assert body["total"] == 2
    assert body["batch_id"].startswith("batch_")
    assert body["extractor"] == "mock"
    rows = body["rows"]
    assert len(rows) == 2
    for row in rows:
        assert row["document_id"]
        assert row["candidate"]
        assert row["recommendation"] in ("recommend", "needs_review", "hold")
        assert row["review_decision"] in ("pending", "approve", "hold", "needs_review")


def test_get_batch_after_create():
    created = _create_and_wait(limit=1)
    batch_id = created["batch_id"]
    r = client.get(f"/api/batches/{batch_id}")
    assert r.status_code == 200
    assert r.json()["total"] == 1


def test_get_batch_not_found():
    r = client.get("/api/batches/batch_nonexistent")
    assert r.status_code == 404


def test_batch_task_not_found():
    r = client.get("/api/batches/tasks/task_nonexistent")
    assert r.status_code == 404


def test_create_batch_bad_extractor():
    r = client.post("/api/batches", json={"limit": 1, "extractor": "magic"})
    assert r.status_code == 400


def test_batch_row_carries_extractor_version():
    """T5c：批次行必须携带所用抽取器版本（评估落库来源可追溯）。"""
    body = _create_and_wait(limit=1, extractor="mock")
    row = body["rows"][0]
    assert row["extractor_version"].startswith("mock"), row["extractor_version"]


def test_rerun_uses_page_cache(monkeypatch):
    """T5c：同一文档重跑时解析走 pages 缓存，不再调用 parse_document（OCR 跳过）。

    第一跑落缓存；第二跑把 parse_document 换成必炸实现，若仍完成则证明走了缓存。
    """
    created = _create_and_wait(limit=1, extractor="mock")
    doc_id = created["rows"][0]["document_id"]
    assert api_module._repo().get_pages(doc_id), "第一跑应已落 pages 缓存"

    import src.workflow as wf

    def boom(*args, **kwargs):
        raise RuntimeError("OCR/解析不应被再次调用（应命中页面缓存）")

    monkeypatch.setattr(wf, "parse_document", boom)
    created2 = _create_and_wait(limit=1, extractor="mock")
    rows2 = created2["rows"]
    assert rows2, created2
    assert rows2[0]["document_id"] == doc_id


def test_create_batch_model_disabled_degrades_to_review(monkeypatch):
    """model 抽取未启用时任务照常完成，行进入人工复核（不报错、不调真实 API）。"""
    from src.schemas import ModelConfig
    import src.extraction

    monkeypatch.setattr(src.extraction, "MODEL_CONFIG", ModelConfig(enabled=False))
    body = _create_and_wait(limit=1, extractor="model")
    assert body["extractor"] == "model"
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["recommendation"] in ("recommend", "needs_review", "hold")
    assert row["review_decision"] in ("pending", "approve", "hold", "needs_review")


def test_review_detail():
    created = _create_and_wait(limit=1)
    doc_id = created["rows"][0]["document_id"]
    r = client.get(f"/api/documents/{doc_id}/review")
    assert r.status_code == 200
    body = r.json()
    assert body["document_id"] == doc_id
    assert len(body["pages"]) >= 1
    assert any(p["text"] for p in body["pages"])
    assert isinstance(body["facts"], list)
    assert body["evidence_valid"] is not None
    assert body["assessment"] is not None
    assert body["assessment"]["recommendation"] in ("recommend", "needs_review", "hold")


def test_review_detail_not_found():
    r = client.get("/api/documents/doc_nonexistent/review")
    assert r.status_code == 404


def test_document_file_serves_original():
    """T5 复核改进：原始简历文件直出（PDF 返回 application/pdf 且内容为原文件）。"""
    created = _create_and_wait(limit=1)
    doc_id = created["rows"][0]["document_id"]
    r = client.get(f"/api/documents/{doc_id}/file")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/pdf")
    assert len(r.content) > 100
    # review 详情携带扩展名（前端据此选择 iframe/img 呈现）
    rev = client.get(f"/api/documents/{doc_id}/review").json()
    assert rev["ext"] == "pdf"


def test_document_file_not_found():
    r = client.get("/api/documents/doc_nonexistent/file")
    assert r.status_code == 404


def test_traces_endpoint_after_batch(monkeypatch, tmp_path):
    """T6：批次完成后 /api/traces 返回该批次每份简历的抽取 trace。"""
    import src.observability as obs

    monkeypatch.setattr(obs, "TRACES_DIR", tmp_path / "traces")
    body = _create_and_wait(limit=1, extractor="mock")
    batch_id = body["batch_id"]
    r = client.get(f"/api/traces?batch_id={batch_id}")
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["batch_id"] == batch_id
    assert row["extractor"] == "mock"
    assert row["document_id"], "trace 应带 document_id"
    assert row["parse_status"] == "parsed"
    assert "stats" in row, "trace 应带抽取统计（calls/耗时/token）"
    assert "warnings" in row, "trace 应带校验失败点"


def test_approve_flow():
    created = _create_and_wait(limit=1)
    doc_id = created["rows"][0]["document_id"]
    r = client.post(f"/api/documents/{doc_id}/approve",
                    json={"decision": "approve", "reason": "测试批准", "assessment_id": created['rows'][0]['assessment_id'], "expected_version": api_module.JOB_PROFILE.version, "request_id": 'test_flow'})
    assert r.status_code == 200
    body = r.json()
    assert body["review_id"]
    assert body["decision"] == "approve"
    # 审批后批次行内状态应更新
    batch_id = created["batch_id"]
    rows = client.get(f"/api/batches/{batch_id}").json()["rows"]
    assert any(row["document_id"] == doc_id and row["review_decision"] == "approve"
               for row in rows)


def test_approve_bad_decision():
    created = _create_and_wait(limit=1)
    doc_id = created["rows"][0]["document_id"]
    r = client.post(f"/api/documents/{doc_id}/approve", json={"decision": "maybe"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# T5b 补充端点：简历库 / 上传 / 排期
# ---------------------------------------------------------------------------
def test_list_resumes():
    r = client.get("/api/resumes")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert {f["file_name"] for f in body["files"]} == {"cv_test.pdf", "cv_test2.pdf"}


def test_upload_resume(tmp_path):
    pdf = _minimal_pdf("New candidate, bridge design, 2020-2023")
    r = client.post("/api/upload",
                    files={"file": ("new_cv.pdf", pdf, "application/pdf")})
    assert r.status_code == 200
    assert r.json()["file_name"] == "new_cv.pdf"
    # 已写入 fake RESUME_DIR，可被批次扫描到
    r = client.get("/api/resumes")
    assert r.json()["total"] == 3


def test_upload_rejects_bad_ext():
    r = client.post("/api/upload",
                    files={"file": ("evil.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 400


def test_scheduling_empty_before_batch():
    r = client.get("/api/scheduling")
    assert r.status_code == 200
    body = r.json()
    assert body["batch_id"] is None
    assert body["round"] is None


def test_scheduling_run_requires_batch():
    r = client.post("/api/scheduling/run")
    assert r.status_code == 400


def test_scheduling_flow():
    created = _create_and_wait(limit=1)
    doc_id = created["rows"][0]["document_id"]
    client.post(f"/api/documents/{doc_id}/approve", json={"decision": "approve", "assessment_id": created['rows'][0]['assessment_id'], "expected_version": api_module.JOB_PROFILE.version, "request_id": 'test_scheduling'})
    r = client.post("/api/scheduling/run")
    assert r.status_code == 200
    body = r.json()
    assert body["round"] is not None
    assert body["round"]["status"] in ("frozen", "scheduled")
    assert len(body["slots"]) == 4
    assert len(body["bookings"]) >= 1
    confirmed = [b for b in body["bookings"] if b["status"] == "confirmed"]
    assert confirmed, "已批准候选人应获得确认时段"
    # 幂等：重复排期返回已有预约，并允许只重试失败的外部动作
    r2 = client.post("/api/scheduling/run")
    assert r2.status_code == 200
    assert [b["booking_id"] for b in r2.json()["bookings"]] == [b["booking_id"] for b in body["bookings"]]
