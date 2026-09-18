# -*- coding: utf-8 -*-
"""T6 可观测性单测：trace 落盘/读回/排序/批次过滤。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import observability as obs


def test_record_and_read_back(tmp_path, monkeypatch):
    monkeypatch.setattr(obs, "TRACES_DIR", tmp_path / "traces")
    obs.record_trace({"batch_id": "batch_a", "document_id": "doc_1", "stats": {"calls": 2}})
    obs.record_trace({"batch_id": "batch_a", "document_id": "doc_2"})
    obs.record_trace({"batch_id": "batch_b", "document_id": "doc_3"})

    all_rows = obs.read_traces(limit=10)
    assert len(all_rows) == 3
    assert all("ts" in r for r in all_rows), "trace 应自动带时间戳"
    # 最新在前
    assert all_rows[0]["document_id"] == "doc_3"

    only_a = obs.read_traces(batch_id="batch_a")
    assert [r["document_id"] for r in only_a] == ["doc_2", "doc_1"]

    limited = obs.read_traces(limit=2)
    assert len(limited) == 2


def test_record_skips_empty_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(obs, "TRACES_DIR", tmp_path / "nonexistent")
    assert obs.read_traces() == []
