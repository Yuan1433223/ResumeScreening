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


@pytest.mark.parametrize('extractor', ['mock-v1', 'model-p1', 'unknown-future'])
def test_review_uses_persisted_snapshot_without_extracting(monkeypatch, extractor):
    import src.workflow as wf
    from src.schemas import Fact, FactStatus, Evidence, EvidenceSource
    calls = []
    def extract(document_id, pages, extractor_version='mock'):
        calls.append(extractor_version)
        page = pages[0]
        quote = 'English'
        start = page.text.index(quote)
        return ExtractionReport(extractor_version=extractor, facts=[Fact(
            id='stable_fact', document_id=document_id, kind='english', value='info',
            status=FactStatus.SUPPORTED,
            evidences=[Evidence(id='stable_evidence', document_id=document_id, page_number=1,
                                start=start, end=start+len(quote), quote=quote,
                                source_type=EvidenceSource.PAGE_TEXT)])])
    monkeypatch.setattr(wf, 'extract_facts', extract)
    task = _create_and_wait(limit=1)
    row = task['rows'][0]
    def forbidden(*a, **k):
        raise AssertionError('Reading a review must never call an extractor')
    monkeypatch.setattr(api_module, 'extract_facts', forbidden)
    url = f"/api/documents/{row['document_id']}/review"
    first = client.get(url).json()
    second = client.get(url).json()
    assert calls == ['mock']
    assert first == second
    assert first['snapshot_available'] is True
    assert first['assessment']['id'] == row['assessment_id']
    assert first['assessment']['extractor_version'] == extractor
    assert first['facts'][0]['id'] == 'stable_fact'
    assert first['assessment']['dimensions']['english']['matched_facts'] == ['stable_fact']
    assert first['pages'][0]['text'][first['facts'][0]['start']:first['facts'][0]['end']] == 'English'


def test_legacy_assessment_is_not_silently_reextracted(monkeypatch):
    body = _create_and_wait(limit=1)
    doc_id = body['rows'][0]['document_id']
    repo = api_module._repo()
    previous = repo.get_assessment_by_document(doc_id)
    repo.save_assessment(Assessment(id='legacy', document_id=doc_id, job_version='v1', extractor_version='model-p1'))
    monkeypatch.setattr(api_module, 'extract_facts', lambda *a, **k: pytest.fail('no cloud on GET'))
    response = client.get(f'/api/documents/{doc_id}/review').json()
    assert response['assessment']['id'] == 'legacy'
    assert response['snapshot_available'] is False
    assert response['facts'] == []
    assert response['warnings']
    assert repo.get_assessment(previous['id']) is not None


def test_review_detail_exposes_payload_score_fields():
    body = _create_and_wait(limit=1)
    data = client.get(f"/api/documents/{body['rows'][0]['document_id']}/review").json()
    assert {'relevant_domain','experience','management','summary'} <= data['assessment']['hard_results'].keys()
    assert len(data['assessment']['dimensions']) == 8
    assert isinstance(data['assessment']['missing_items'], list)


def test_missing_snapshot_does_not_hide_document_not_found():
    assert client.get('/api/documents/missing/review').status_code == 404
