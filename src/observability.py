# -*- coding: utf-8 -*-
"""T6 可观测性：批次处理 trace 落盘（JSONL，data/traces/）。

用途：从候选人（document_id）追到模型调用（calls/耗时/token/费用）、
校验失败点（warnings）、审批与重试。每行一条 trace，按天分文件，
仅供本地调试与演示，不入版本控制（data/ 已 gitignore）。

约定：record_trace 线程安全；ts 由本模块生成；调用方只传业务字段。
"""
from __future__ import annotations

import itertools
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List

_ROOT = Path(__file__).resolve().parent.parent
TRACES_DIR = _ROOT / "data" / "traces"

_LOCK = threading.Lock()
_SEQ = itertools.count(1)


def record_trace(entry: Dict[str, Any]) -> None:
    """追加一条 trace（自动带 UTC 时间戳与进程内序号）。"""
    with _LOCK:
        TRACES_DIR.mkdir(parents=True, exist_ok=True)
        day = datetime.now(UTC).strftime("%Y%m%d")
        path = TRACES_DIR / f"{day}.jsonl"
        line = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "seq": next(_SEQ),
            **entry,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def read_traces(limit: int = 50, batch_id: str = "", document_id: str = "") -> List[Dict[str, Any]]:
    """读取最近 limit 条 trace（可只筛某个批次 / 某份文档）；倒序（最新在前）。

    document_id 与 batch_id 语义一致：非空则精确匹配 d.get("document_id")。
    两个过滤器可同时使用（AND）。保持对旧调用方（只传 limit/batch_id）的兼容。
    """
    if not TRACES_DIR.is_dir():
        return []
    rows: List[Dict[str, Any]] = []
    for fp in sorted(TRACES_DIR.glob("*.jsonl"), reverse=True):
        for line in fp.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if batch_id and d.get("batch_id") != batch_id:
                continue
            if document_id and d.get("document_id") != document_id:
                continue
            rows.append(d)
        if len(rows) >= limit:
            break
    rows.sort(key=lambda r: (r.get("ts", ""), r.get("seq", 0)), reverse=True)
    return rows[:limit]
