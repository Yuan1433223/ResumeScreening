# -*- coding: utf-8 -*-
"""批次任务持久化（T10）：把批次任务从进程内内存 dict 落 SQLite。

背景与设计意图：
- 之前 app/api.py 用模块级 ``_BATCH_TASKS``（dict）+ 一把锁保存批次任务：
  进程一重启全部丢失，前端 localStorage 里存的 task_id 刷新后轮询直接 404。
  落库后任务行跨进程可读，重启后仍能查到 running/done 任务。
- 表独立于业务 Repository（新表 ``batch_tasks``），连接模式照抄
  ``src/repository.py`` 的 ``__init__``：``sqlite3.connect(check_same_thread=False)
  + RLock + PRAGMA WAL``——后台 worker 线程与 FastAPI 请求线程共用同一连接时，
  由实例 RLock 串行化所有数据库访问。不复用 Repository 类：任务表生命周期
  与业务表不同（任务行保留不清理，供 SSE 完成事件与历史查询）。
- ``rows`` 是前端表格直接消费的行数组，用 ``rows_json`` 文本列存
  （json.dumps/loads），不做行展开——它只是 worker 进度的快照，
  事实来源仍是 runs/assessments 表。

P2 接缝（换 Redis 队列，不实际实现）：
- 本模块抽象出 ``TaskStore``，其中 ``enqueue(task) -> str`` 是"任务投递"接缝。
  当前 ``SqliteTaskStore.enqueue`` 直接落库（与 create 等价），任务即建即跑。
  P2 若引入 Redis/消息队列：新增 ``RedisTaskStore(TaskStore)``，``enqueue`` 改为
  RPUSH 入队、worker 侧 BRPOP 消费；``get/update/delete`` 可继续写 SQLite
  （或改 Redis hash）。API 层只依赖 TaskStore 抽象，届时无需改动。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from abc import ABC, abstractmethod
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DB_PATH


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TaskStore(ABC):
    """批次任务存储抽象边界。app/api.py 只依赖本接口，P2 可换实现。"""

    @abstractmethod
    def create(self, task: Dict[str, Any]) -> str:
        """新建任务行，返回 task_id。"""

    @abstractmethod
    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        """读任务；不存在返回 None。返回 dict 中 rows 已反序列化为 list。"""

    @abstractmethod
    def update(self, task_id: str, **fields: Any) -> None:
        """部分更新任务字段（status/done/current_file/rows/error 等）。"""

    @abstractmethod
    def list(self) -> List[Dict[str, Any]]:
        """列出全部任务（按创建先后顺序）。"""

    @abstractmethod
    def delete(self, task_id: str) -> None:
        """删除任务行。"""

    @abstractmethod
    def enqueue(self, task: Dict[str, Any]) -> str:
        """P2 接缝：把任务投递到执行队列。

        P2 由 Redis 队列实现（RPUSH/LPUSH 入队，worker 侧 BRPOP 消费）；
        当前 SqliteTaskStore 内 enqueue 直接落库（与 create 等价），不实际起队列。
        """


# update() 允许透传的业务字段白名单（列名直接拼 SQL，必须白名单防注入）
_UPDATE_FIELDS = {"batch_id", "extractor", "status", "total", "done", "current_file", "error"}


class SqliteTaskStore(TaskStore):
    """``batch_tasks`` 表的 SQLite 实现；连接模式照抄 Repository。

    所有公开方法经 ``self._lock``（RLock）包裹，线程安全：后台批次 worker 与
    FastAPI 请求线程、SSE 长连接轮询线程并发访问同一连接时串行化。
    """

    def __init__(self, db_path: str | Path = DB_PATH):
        self._lock = threading.RLock()
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：worker 线程与 API 请求线程共用一条连接
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema：CREATE TABLE IF NOT EXISTS，无迁移负担
    # ------------------------------------------------------------------
    def _init_schema(self) -> None:
        # seq：单调递增修订号，每次 update +1，供 SSE 判断快照是否变化。
        # （SQLite 的 AUTOINCREMENT 只挂 INTEGER PRIMARY KEY，故 seq 手动递增。）
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS batch_tasks (
                    task_id      TEXT PRIMARY KEY,
                    batch_id     TEXT    NOT NULL DEFAULT '',
                    extractor    TEXT    NOT NULL DEFAULT '',
                    status       TEXT    NOT NULL DEFAULT 'running',
                    total        INTEGER NOT NULL DEFAULT 0,
                    done         INTEGER NOT NULL DEFAULT 0,
                    current_file TEXT    NOT NULL DEFAULT '',
                    rows_json    TEXT    NOT NULL DEFAULT '[]',
                    error        TEXT    NOT NULL DEFAULT '',
                    created_at   TEXT    NOT NULL,
                    updated_at   TEXT    NOT NULL,
                    seq          INTEGER NOT NULL DEFAULT 1
                );
                """
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    def create(self, task: Dict[str, Any]) -> str:
        now = _now()
        with self._lock:
            self._conn.execute(
                """INSERT INTO batch_tasks
                   (task_id, batch_id, extractor, status, total, done, current_file,
                    rows_json, error, created_at, updated_at, seq)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (
                    task["task_id"],
                    task.get("batch_id", ""),
                    task.get("extractor", ""),
                    task.get("status", "running"),
                    task.get("total", 0),
                    task.get("done", 0),
                    task.get("current_file", ""),
                    json.dumps(task.get("rows", []), ensure_ascii=False),
                    task.get("error", ""),
                    now,
                    now,
                ),
            )
            self._conn.commit()
        return task["task_id"]

    def enqueue(self, task: Dict[str, Any]) -> str:
        # P2 由 Redis 队列实现；当前直接落库（见模块 docstring）
        return self.create(task)

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM batch_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return self._row_to_task(row) if row else None

    def update(self, task_id: str, **fields: Any) -> None:
        sets: List[str] = []
        vals: List[Any] = []
        # rows 走 JSON 列
        if "rows" in fields:
            sets.append("rows_json=?")
            vals.append(json.dumps(fields.pop("rows"), ensure_ascii=False))
        for k, v in fields.items():
            if k in _UPDATE_FIELDS:
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return
        # 每次更新递增修订号、刷新 updated_at（SSE 据此判断快照变化）
        sets.append("seq=seq+1")
        sets.append("updated_at=?")
        vals.append(_now())
        vals.append(task_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE batch_tasks SET {', '.join(sets)} WHERE task_id=?", vals
            )
            self._conn.commit()

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            # 按 rowid（插入顺序）返回，update 引发的 seq 递增不打乱创建顺序
            rows = self._conn.execute(
                "SELECT * FROM batch_tasks ORDER BY rowid"
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def delete(self, task_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM batch_tasks WHERE task_id=?", (task_id,))
            self._conn.commit()

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Dict[str, Any]:
        # rows_json 反序列化回 list；调用方拿到的是独立副本，改不到连接状态
        return {
            "task_id": row["task_id"],
            "batch_id": row["batch_id"],
            "extractor": row["extractor"],
            "status": row["status"],
            "total": row["total"],
            "done": row["done"],
            "current_file": row["current_file"],
            "rows": json.loads(row["rows_json"]),
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "seq": row["seq"],
        }

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 单例工厂（与 get_repository 同模式：默认参数 None，运行时解析模块级 DB_PATH，
# 便于测试 monkeypatch 模块属性生效；测试可重置本变量或 monkeypatch 本函数替换）
# ---------------------------------------------------------------------------
_default_store: Optional[TaskStore] = None


def get_task_store(db_path: str | Path | None = None) -> TaskStore:
    """进程内单例任务存储。

    - db_path 显式传入：新建独立实例（测试用 tmp_path 隔离）。
    - 否则复用模块缓存；首次创建时读模块级 DB_PATH（conftest 会 patch 它）。
    """
    global _default_store
    if db_path is not None:
        return SqliteTaskStore(db_path)
    if _default_store is None:
        _default_store = SqliteTaskStore(DB_PATH)
    return _default_store
