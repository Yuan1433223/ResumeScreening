# -*- coding: utf-8 -*-
"""T13 后端三项改造测试：上传加固 / Dashboard 聚合 / trace document_id 过滤。

不碰前端、README、docs/、git；仅覆盖 upload 与 traces 区域 + 新增 dashboard 端点。
- 上传：合法 PDF 成功；伪造扩展名（PNG 字节改名 .pdf）400；超大小上限 413；
  docx 成功（python-docx 现场合成）；校验失败不落盘。
- Dashboard：空库返回全 0/null 结构；经 repository 直接插入 mock 数据后断言
  kpis / total_score 桶 / dimensions / funnel 六段顺序与单调性。
- trace：record_trace 两条不同 document_id，read_traces 与 API 层过滤生效。
合成 PDF 复用 test_t8/test_t5 的 _minimal_pdf 模式。
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

import app.api as api_module
from app.api import app
from src import observability as obs
from src.schemas import (
    Assessment,
    DimensionScore,
    FactStatus,
    HardResult,
    Page,
    Recommendation,
    Review,
    ReviewDecision,
)

client = TestClient(app)


# ---------------------------------------------------------------------------
# 合成器
# ---------------------------------------------------------------------------
def _minimal_pdf(text: str) -> bytes:
    """带 xref 的最小合法单页 PDF（pdfplumber 可直接抽取文本）。"""
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


# 合法 PNG 字节（89 50 4E 47 0D 0A 1A 0A 头 + 最小 IHDR/IDAT/EOCD 占位）
def _fake_png_bytes() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR" + b"\x00" * 13 +
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _make_docx_bytes(paragraphs) -> bytes:
    """用 python-docx 在内存合成最小合法 .docx（含 word/document.xml）。"""
    from docx import Document

    buf = io.BytesIO()
    doc = Document()
    for t in paragraphs:
        doc.add_paragraph(t)
    doc.save(buf)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _fake_resume_dir(tmp_path, monkeypatch):
    """API 指向空合成简历目录；trace 目录隔离。"""
    monkeypatch.setattr(api_module, "RESUME_DIR", tmp_path)
    monkeypatch.setattr(obs, "TRACES_DIR", tmp_path / "traces")
    # 清空进程内复核缓存，避免跨用例串味
    api_module._REVIEW_EXTRACT_CACHE.clear()
    yield tmp_path


# ===========================================================================
# 任务 1：上传加固
# ===========================================================================
class TestUploadHardening:
    def test_valid_pdf_uploads(self, tmp_path):
        pdf = _minimal_pdf("Alice, steel structure, 2019-2024")
        r = client.post("/api/upload", files={"file": ("alice.pdf", pdf, "application/pdf")})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["file_name"] == "alice.pdf"
        assert Path(body["saved"]).exists()

    def test_forged_extension_rejected(self, tmp_path):
        """PNG 字节改名为 .pdf：魔数不符 → 400，且不落盘。"""
        png = _fake_png_bytes()
        r = client.post("/api/upload", files={"file": ("evil.pdf", png, "application/pdf")})
        assert r.status_code == 400, r.text
        # 校验失败文件不得落盘
        assert not (tmp_path / "evil.pdf").exists()
        assert list(p.name for p in tmp_path.iterdir() if p.is_file()) == []

    def test_oversize_rejected(self, tmp_path, monkeypatch):
        """monkeypatch 调小上限 → 合法 PDF 也因超限 413，不落盘。"""
        pdf = _minimal_pdf("Bob, bridge design, 2020-2023")
        monkeypatch.setattr(api_module, "MAX_UPLOAD_BYTES", 16)  # 远小于合成 PDF
        r = client.post("/api/upload", files={"file": ("bob.pdf", pdf, "application/pdf")})
        assert r.status_code in (400, 413), r.text
        assert "超过上限" in r.json().get("detail", "") or "上限" in r.json().get("detail", "")
        assert not (tmp_path / "bob.pdf").exists()

    def test_docx_uploads(self, tmp_path):
        """python-docx 合成的 .docx：PK 魔数 + word/document.xml 校验通过。"""
        docx = _make_docx_bytes(["张三 钢结构工程师", "2016-2024 主导海外 MiC 项目。"])
        r = client.post("/api/upload",
                        files={"file": ("zhangsan.docx", docx,
                                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
        assert r.status_code == 200, r.text
        assert r.json()["file_name"] == "zhangsan.docx"
        assert (tmp_path / "zhangsan.docx").exists()

    def test_docx_without_document_xml_rejected(self, tmp_path):
        """裸 zip（PK 头但无 word/document.xml）改名为 .docx 应被拒。"""
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("not/a/real.docx", b"hello")
        bad_zip = buf.getvalue()
        r = client.post("/api/upload",
                        files={"file": ("fake.docx", bad_zip,
                                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
        assert r.status_code == 400, r.text
        assert not (tmp_path / "fake.docx").exists()

    def test_bad_ext_rejected(self, tmp_path):
        r = client.post("/api/upload", files={"file": ("x.exe", b"MZ", "application/octet-stream")})
        assert r.status_code == 400


# ===========================================================================
# 任务 2：Dashboard 聚合
# ===========================================================================
def _insert_mock_data() -> None:
    """直接经 repository 插入 3 份文档的全链路 mock 数据（可控、快）。

    漏斗设计（保证上游 >= 下游）：
      uploaded=3, parsed=3, extracted=3, hard_passed=2, scored=2, approved=1
    """
    repo = api_module._repo()
    repo.create_batch("batch_dash", "v1")

    docs = [
        # doc_a：硬条件 pass、score_complete=1、known_score=80、recommend、已 approve
        ("doc_a", 80, Recommendation.RECOMMEND, HardResult.PASS, True,
         {"steel_mic_experience": 30, "overseas": 15}),
        # doc_b：硬条件 pass、score_complete=1、known_score=50、needs_review、未审批
        # （硬条件通过但分数中等 → needs_review；与 doc_a 共同构成 hard_passed=scored=2）
        ("doc_b", 50, Recommendation.NEEDS_REVIEW, HardResult.PASS, True,
         {"steel_mic_experience": 10}),
        # doc_c：硬条件 fail、未完成评分、hold
        ("doc_c", 0, Recommendation.HOLD, HardResult.FAIL, False, {}),
    ]

    for doc_id, score, rec, hard, complete, dims in docs:
        repo.upsert_document(
            doc_id, sha256=f"sha_{doc_id}", storage_path=f"/tmp/{doc_id}.pdf",
            mime="pdf", parse_status="parsed", parser_version="test",
        )
        # 所有文档都有 pages（parsed=3）
        repo.save_pages(doc_id, [Page(
            document_id=doc_id, page_number=1, text="some resume text " * 5,
            text_hash=f"h_{doc_id}", extraction_method="test", quality_flags=[],
        )])
        # 所有文档都有 run（extracted=3）
        repo.create_run(f"run_{doc_id}", thread_id=f"tid_{doc_id}",
                        document_id=doc_id, batch_id="batch_dash")

        assessment = Assessment(
            id=f"asm_{doc_id}",
            document_id=doc_id,
            job_version="v1",
            extractor_version="mock-v1",
            rubric_version="v1",
            hard_results={"relevant_domain": hard},
            hard_summary=hard,
            dimensions={
                k: DimensionScore(score=v, max_score=v, status=FactStatus.SUPPORTED)
                for k, v in dims.items()
            },
            known_score=score,
            score_complete=complete,
            recommendation=rec,
        )
        repo.save_assessment(assessment)

    # doc_a 审批通过（approved=1，去重 assessment_id）
    repo.create_review(Review(
        id="rev_a", request_id="req_a", assessment_id="asm_doc_a",
        assessment_version="v1", decision=ReviewDecision.APPROVE, reason="ok",
    ))
    return {}


class TestDashboard:
    def test_empty_returns_zero_structure(self):
        r = client.get("/api/dashboard")
        assert r.status_code == 200
        body = r.json()
        # 顶层键
        assert set(body.keys()) == {"kpis", "score_distribution", "funnel"}
        k = body["kpis"]
        assert k["total_documents"] == 0
        assert k["total_batches"] == 0
        assert k["recommended"] == 0
        assert k["needs_review"] == 0
        assert k["hold"] == 0
        assert k["approved"] == 0
        assert k["avg_score"] is None
        assert k["review_rate"] == 0
        # total_score 六桶结构固定
        buckets = body["score_distribution"]["total_score"]
        assert [b["bucket"] for b in buckets] == [
            "0-20", "20-40", "40-60", "60-75", "75-90", "90-100"]
        assert all(b["count"] == 0 for b in buckets)
        assert body["score_distribution"]["dimensions"] == {}
        # funnel 六段顺序固定
        stages = [f["stage"] for f in body["funnel"]]
        assert stages == ["uploaded", "parsed", "extracted", "hard_passed",
                           "scored", "approved"]
        assert all(f["count"] == 0 for f in body["funnel"])

    def test_aggregates_match_mock_data(self):
        _insert_mock_data()
        r = client.get("/api/dashboard")
        assert r.status_code == 200
        body = r.json()
        k = body["kpis"]
        assert k["total_documents"] == 3
        assert k["total_batches"] == 1
        assert k["recommended"] == 1
        assert k["needs_review"] == 1
        assert k["hold"] == 1
        assert k["approved"] == 1
        assert k["avg_score"] == pytest.approx((80 + 50) / 2)
        # review_rate = needs_review(1) / total_assessments(3)
        assert k["review_rate"] == pytest.approx(1 / 3, rel=1e-3)

        # total_score 桶：80 -> 75-90；50 -> 40-60
        buckets = {b["bucket"]: b["count"] for b in body["score_distribution"]["total_score"]}
        assert buckets["40-60"] == 1
        assert buckets["75-90"] == 1
        assert buckets["0-20"] == 0
        assert buckets["20-40"] == 0
        assert buckets["60-75"] == 0
        assert buckets["90-100"] == 0

        # dimensions：steel_mic_experience 出现 level=30(doc_a) 与 level=10(doc_b)
        dims = body["score_distribution"]["dimensions"]
        assert set(dims.keys()) == {"steel_mic_experience", "overseas"}
        sme = {d["level"]: d["count"] for d in dims["steel_mic_experience"]}
        assert sme == {30: 1, 10: 1}
        ov = {d["level"]: d["count"] for d in dims["overseas"]}
        assert ov == {15: 1}

        # funnel 六段顺序 + 单调不减
        funnel = body["funnel"]
        counts = [f["count"] for f in funnel]
        assert counts == [3, 3, 3, 2, 2, 1]
        for i in range(1, len(counts)):
            assert counts[i - 1] >= counts[i], f"funnel 非单调: {counts}"

    def test_extracted_dedup_across_reruns(self):
        """冒烟回归：
        (1) 同一批文档再跑一批，runs 行数翻倍但 extracted 按 document_id 去重，
            不出现 uploaded=3 → extracted=6 的回跳；
        (2) 解析失败文档（有 run 无 pages 无 assessment）不进入 parsed/extracted，
            漏斗不回跳（extracted <= parsed <= uploaded）。
        """
        _insert_mock_data()
        repo = api_module._repo()
        # 模拟"第二批次"：为同 3 份文档各再落一条 run（新 thread_id，不动 documents/assessments）
        for doc_id in ("doc_a", "doc_b", "doc_c"):
            repo.create_run(f"run2_{doc_id}", thread_id=f"tid2_{doc_id}",
                            document_id=doc_id, batch_id="batch_dash2")
        # 再模拟一份"解析失败"文档：有 document 行 + 有 run 行，但无 pages、无 assessment
        repo.upsert_document("doc_fail", sha256="sha_doc_fail",
                             storage_path="/tmp/doc_fail.pdf", mime="pdf",
                             parse_status="pending", parser_version="test")
        repo.create_run("run_fail", thread_id="tid_fail",
                        document_id="doc_fail", batch_id="batch_dash2")

        r = client.get("/api/dashboard")
        assert r.status_code == 200
        funnel = r.json()["funnel"]
        counts = [f["count"] for f in funnel]
        # uploaded=4（含失败文档），parsed=3（失败文档无 pages），extracted=3
        # （失败文档有 run 但无 assessment，不计入），后续 hard/scored/approved 不变
        assert counts == [4, 3, 3, 2, 2, 1], counts
        # 严格后继：extracted <= parsed <= uploaded
        assert funnel[2]["count"] <= funnel[1]["count"] <= funnel[0]["count"]
        for i in range(1, len(counts)):
            assert counts[i - 1] >= counts[i], f"重跑后 funnel 非单调: {counts}"


# ===========================================================================
# 任务 3：trace document_id 过滤
# ===========================================================================
class TestTraceDocumentFilter:
    def test_read_traces_filters_by_document_id(self, tmp_path):
        obs.record_trace({"batch_id": "b1", "document_id": "doc_x", "k": 1})
        obs.record_trace({"batch_id": "b1", "document_id": "doc_y", "k": 2})
        obs.record_trace({"batch_id": "b2", "document_id": "doc_x", "k": 3})

        only_x = obs.read_traces(document_id="doc_x")
        assert [r["document_id"] for r in only_x] == ["doc_x", "doc_x"]
        only_y = obs.read_traces(document_id="doc_y")
        assert [r["document_id"] for r in only_y] == ["doc_y"]
        # batch_id 兼容仍生效
        only_b1 = obs.read_traces(batch_id="b1")
        assert {r["document_id"] for r in only_b1} == {"doc_x", "doc_y"}
        # 空 document_id = 不过滤（兼容旧调用）
        all_rows = obs.read_traces(limit=10)
        assert len(all_rows) == 3

    def test_api_traces_query_param(self, tmp_path):
        obs.record_trace({"batch_id": "b9", "document_id": "doc_api_x"})
        obs.record_trace({"batch_id": "b9", "document_id": "doc_api_y"})
        r = client.get("/api/traces?document_id=doc_api_x")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert body["rows"][0]["document_id"] == "doc_api_x"
        # 形状不变
        assert set(body.keys()) == {"total", "rows"}
