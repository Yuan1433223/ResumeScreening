# -*- coding: utf-8 -*-
"""T5a API 测试：批次、复核、审批（TestClient + 隔离数据库 + 合成 PDF，不碰真实网络/简历库）。"""
import sys
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


def test_create_batch_mock():
    r = client.post("/api/batches", json={"limit": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert body["batch_id"].startswith("batch_")
    rows = body["rows"]
    assert len(rows) == 2
    for row in rows:
        assert row["document_id"]
        assert row["candidate"]
        assert row["recommendation"] in ("recommend", "needs_review", "hold")
        assert row["review_decision"] in ("pending", "approve", "hold", "needs_review")


def test_get_batch_after_create():
    created = client.post("/api/batches", json={"limit": 1}).json()
    batch_id = created["batch_id"]
    r = client.get(f"/api/batches/{batch_id}")
    assert r.status_code == 200
    assert r.json()["total"] == 1


def test_get_batch_not_found():
    r = client.get("/api/batches/batch_nonexistent")
    assert r.status_code == 404


def test_create_batch_model_rejected():
    r = client.post("/api/batches", json={"limit": 1, "extractor": "model"})
    assert r.status_code == 400
    assert "mock" in r.json()["detail"]


def test_review_detail():
    created = client.post("/api/batches", json={"limit": 1}).json()
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


def test_approve_flow():
    created = client.post("/api/batches", json={"limit": 1}).json()
    doc_id = created["rows"][0]["document_id"]
    r = client.post(f"/api/documents/{doc_id}/approve",
                    json={"decision": "approve", "reason": "测试批准"})
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
    created = client.post("/api/batches", json={"limit": 1}).json()
    doc_id = created["rows"][0]["document_id"]
    r = client.post(f"/api/documents/{doc_id}/approve", json={"decision": "maybe"})
    assert r.status_code == 400
