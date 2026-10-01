# -*- coding: utf-8 -*-
"""T9 回归：原始文件端点必须允许浏览器内联渲染（PDF iframe 不能空白）。

Bug：GET /api/documents/{id}/file 用 FileResponse(..., filename=...)，Starlette
默认 Content-Disposition: attachment。PDF 在复核页 <iframe> 中是导航类请求，
收到 attachment 会被浏览器直接下载、面板空白；图片走 <img> 子资源请求不受影响，
所以表现为"图片正常、PDF 空白"。

修复：pdf/jpg/png/bmp 等可内联类型显式返回 Content-Disposition: inline。
本测试在 API 层固化端点契约（响应头 + 内容魔数），不依赖浏览器。
"""
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


CV_TEXT = "Senior Project Manager, steel structure, 10+ years, English"


@pytest.fixture(autouse=True)
def _fake_resume_dir(tmp_path, monkeypatch):
    (tmp_path / "cv_test.pdf").write_bytes(_minimal_pdf(CV_TEXT))
    monkeypatch.setattr(api_module, "RESUME_DIR", tmp_path)
    api_module._REVIEW_EXTRACT_CACHE.clear()
    yield tmp_path


def _run_mock_batch() -> str:
    r = client.post("/api/batches", json={"limit": 1, "extractor": "mock"})
    assert r.status_code == 200
    task = r.json()
    for _ in range(300):
        if task["status"] != "running":
            break
        time.sleep(0.1)
        task = client.get(f"/api/batches/tasks/{task['task_id']}").json()
    assert task["status"] == "done", task
    return task["rows"][0]["document_id"]


def test_pdf_file_endpoint_is_inline_and_parseable():
    doc_id = _run_mock_batch()

    r = client.get(f"/api/documents/{doc_id}/file")
    assert r.status_code == 200

    # Content-Type 必须是 PDF
    assert r.headers["content-type"] == "application/pdf"

    # 关键回归：必须 inline，浏览器才会在 <iframe> 里渲染而不是弹下载
    disposition = r.headers.get("content-disposition", "")
    assert disposition.lower().startswith("inline;"), (
        f"PDF 必须内联渲染，实际 Content-Disposition: {disposition!r}"
    )
    assert "cv_test.pdf" in disposition

    # 内容必须是真实 PDF（魔数），长度与文件一致
    assert r.content.startswith(b"%PDF-"), "响应体不是合法 PDF"
    assert len(r.content) == len(_minimal_pdf(CV_TEXT))


def test_missing_file_returns_404():
    # 不存在的 document_id → 404，而不是 500
    r = client.get("/api/documents/does-not-exist/file")
    assert r.status_code == 404
