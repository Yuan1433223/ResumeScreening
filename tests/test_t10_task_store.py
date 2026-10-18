# -*- coding: utf-8 -*-
"""T10 测试：批次任务落 SQLite + SSE 推送端点。

覆盖：
1. SqliteTaskStore 单元行为：create/get/update/list/delete（独立 tmp db）。
2. API 层：创建批次后轮询接口形状不变；任务落库后用新 store 实例读同一 db
   （模拟进程重启）仍能拿到 running/done 任务。
3. SSE：TestClient stream=True 请求 events 端点，逐行解析事件流，断言
   connected / progress / done / error 协议。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

import app.api as api_module
from app.api import app
from src.task_store import SqliteTaskStore, get_task_store

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
    """API 指向合成简历目录（1 份 PDF，供 SSE/API 用例）。"""
    (tmp_path / "cv_test.pdf").write_bytes(_minimal_pdf(CV_TEXT))
    monkeypatch.setattr(api_module, "RESUME_DIR", tmp_path)
    yield tmp_path


# ---------------------------------------------------------------------------
# 1) SqliteTaskStore 单元行为
# ---------------------------------------------------------------------------
def test_store_create_get_update_list_delete(tmp_path):
    store = SqliteTaskStore(tmp_path / "tasks.db")
    tid = "task_unit_1"
    store.create({
        "task_id": tid, "batch_id": "batch_b1", "extractor": "mock",
        "status": "running", "total": 2, "done": 0,
        "current_file": "", "rows": [], "error": "",
    })
    got = store.get(tid)
    assert got is not None
    assert got["status"] == "running"
    assert got["done"] == 0
    assert got["rows"] == []          # rows_json 反序列化回 list
    assert got["seq"] == 1

    # update：done/rows/current_file/status 透传，seq 递增
    store.update(tid, done=1, current_file="a.pdf",
                 rows=[{"run_id": "r1"}], status="running")
    got = store.get(tid)
    assert got["done"] == 1
    assert got["current_file"] == "a.pdf"
    assert got["rows"] == [{"run_id": "r1"}]
    assert got["seq"] == 2, "每次 update 应递增修订号"

    # list 返回全部任务，按创建顺序
    store.create({
        "task_id": "task_unit_2", "batch_id": "batch_b2", "extractor": "mock",
        "status": "done", "total": 1, "done": 1,
        "current_file": "", "rows": [], "error": "",
    })
    ids = [t["task_id"] for t in store.list()]
    assert ids == ["task_unit_1", "task_unit_2"]

    # delete
    store.delete("task_unit_1")
    assert store.get("task_unit_1") is None
    assert store.get("task_unit_2") is not None

    # enqueue 接缝：当前 SQLite 实现直接落库
    assert store.enqueue({
        "task_id": "task_unit_3", "batch_id": "batch_b3", "extractor": "mock",
        "status": "running", "total": 1, "done": 0,
        "current_file": "", "rows": [], "error": "",
    }) == "task_unit_3"
    assert store.get("task_unit_3") is not None


def test_store_persists_across_instances(tmp_path):
    """模拟进程重启：新连接实例读同一 db，仍能拿到已落库任务。"""
    db = tmp_path / "restart.db"
    s1 = SqliteTaskStore(db)
    s1.create({
        "task_id": "task_restart", "batch_id": "batch_r", "extractor": "mock",
        "status": "running", "total": 3, "done": 2, "current_file": "x.pdf",
        "rows": [{"run_id": "r9"}], "error": "",
    })
    s1.close()
    s2 = SqliteTaskStore(db)   # 新实例 = 新连接
    got = s2.get("task_restart")
    assert got is not None
    assert got["done"] == 2
    assert got["rows"] == [{"run_id": "r9"}]
    s2.close()


# ---------------------------------------------------------------------------
# 2) API 层：轮询接口形状不变 + 落库可跨实例读
# ---------------------------------------------------------------------------
def _create_and_wait(**kwargs) -> dict:
    r = client.post("/api/batches", json=kwargs)
    assert r.status_code == 200
    task = r.json()
    for _ in range(300):
        if task["status"] != "running":
            break
        time.sleep(0.1)
        task = client.get(f"/api/batches/tasks/{task['task_id']}").json()
    assert task["status"] == "done", task
    return task


def test_poll_shape_after_batch():
    body = _create_and_wait(limit=1, extractor="mock")
    # 前端依赖的全部字段必须齐全
    for key in ("task_id", "batch_id", "extractor", "status",
                "total", "done", "current_file", "rows", "error"):
        assert key in body, f"快照缺字段 {key}"
    assert body["total"] == 1
    assert body["done"] == 1
    assert len(body["rows"]) == 1
    assert body["rows"][0]["document_id"]


def test_task_persisted_across_store_instances():
    body = _create_and_wait(limit=1, extractor="mock")
    tid = body["task_id"]
    # 当前 store 指向的 db 路径
    db_path = get_task_store().db_path
    # 模拟重启：新 store 实例读同一 db
    fresh = SqliteTaskStore(db_path)
    got = fresh.get(tid)
    assert got is not None, "任务行应落库，跨连接可读"
    assert got["status"] == "done"
    assert got["done"] == 1
    assert len(got["rows"]) == 1
    fresh.close()


def test_batch_task_not_found():
    r = client.get("/api/batches/tasks/task_nonexistent")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 3) SSE 端点
# ---------------------------------------------------------------------------
def _parse_sse_lines(lines):
    """把 iter_lines 产出的行解析成 [(event, data_obj_or_None)]。"""
    events = []
    ev, data = None, []
    for line in lines:
        if line == "":
            if ev is not None or data:
                payload = "".join(data)
                try:
                    payload = json.loads(payload) if payload else None
                except ValueError:
                    pass
                events.append((ev, payload))
            ev, data = None, []
            continue
        if line.startswith(":"):      # 心跳注释行，忽略
            continue
        if line.startswith("event:"):
            ev = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data.append(line[len("data:"):].strip())
    if ev is not None or data:
        payload = "".join(data)
        try:
            payload = json.loads(payload) if payload else None
        except ValueError:
            pass
        events.append((ev, payload))
    return events


def test_sse_stream_lifecycle():
    # 先建批次（后台线程跑 1 份合成 PDF）
    r = client.post("/api/batches", json={"limit": 1, "extractor": "mock"})
    assert r.status_code == 200
    tid = r.json()["task_id"]

    with client.stream("GET", f"/api/batches/tasks/{tid}/events") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        lines = list(resp.iter_lines())

    events = _parse_sse_lines(lines)
    types = [e for e, _ in events]
    assert types[0] == "connected", f"首个事件应为 connected，实际: {events}"
    assert events[0][1] == {"task_id": tid}
    assert "progress" in types, f"应至少收到一个 progress，实际: {events}"
    assert types[-1] == "done", f"末事件应为 done，实际: {events}"
    done_data = events[-1][1]
    assert done_data["status"] == "done"
    assert done_data["rows"], "done 事件 rows 必须非空"
    assert done_data["done"] == done_data["total"] == 1


def test_sse_task_not_found_emits_error():
    with client.stream("GET", "/api/batches/tasks/task_nonexistent/events") as resp:
        lines = list(resp.iter_lines())
    events = _parse_sse_lines(lines)
    assert events[0][0] == "error", f"应先发 error，实际: {events}"
    assert "detail" in events[0][1]
