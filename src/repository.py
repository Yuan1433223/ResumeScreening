# -*- coding: utf-8 -*-
"""SQLite 持久化仓库（对应 DEVELOPMENT_SPEC.md §5/§7/§8）。

设计要点：
- 幂等：同 document sha256 复用 Document；review 以 request_id 幂等；booking 以
  (round_id, candidate_id) 唯一；external_action 以 (booking_id, type) 唯一。
- 唯一约束由数据库层强制，服务层不依赖"先查后写"的竞态。
- 审批先入库再恢复任务：进程在两步之间退出时，恢复程序读取未消费审批继续执行。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DB_PATH
from .schemas import (
    Batch,
    Booking,
    ExternalAction,
    Review,
    ReviewDecision,
    SchedulingRound,
    Slot,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Repository:
    """单文件 SQLite 仓库；所有写操作使用显式事务。"""

    def __init__(self, db_path: str | Path = DB_PATH):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL UNIQUE,
                storage_path TEXT NOT NULL,
                mime TEXT NOT NULL DEFAULT '',
                parse_status TEXT NOT NULL DEFAULT 'pending',
                parser_version TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pages (
                document_id TEXT NOT NULL REFERENCES documents(id),
                page_number INTEGER NOT NULL,
                text TEXT NOT NULL,
                text_hash TEXT NOT NULL DEFAULT '',
                extraction_method TEXT NOT NULL DEFAULT 'pdfplumber',
                quality_flags TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (document_id, page_number)
            );

            CREATE TABLE IF NOT EXISTS assessments (
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES documents(id),
                job_version TEXT NOT NULL,
                extractor_version TEXT NOT NULL DEFAULT '',
                rubric_version TEXT NOT NULL DEFAULT '',
                known_score INTEGER NOT NULL DEFAULT 0,
                score_complete INTEGER NOT NULL DEFAULT 0,
                recommendation TEXT NOT NULL DEFAULT 'needs_review',
                hard_summary TEXT NOT NULL DEFAULT 'unknown',
                payload TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reviews (
                id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                assessment_id TEXT NOT NULL,
                assessment_version TEXT NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                reviewer_id TEXT NOT NULL DEFAULT 'hr',
                consumed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL UNIQUE,
                document_id TEXT NOT NULL REFERENCES documents(id),
                batch_id TEXT,
                assessment_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                last_error TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT
            );

            CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY,
                job_version TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                frozen_at TEXT
            );

            CREATE TABLE IF NOT EXISTS rounds (
                id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL REFERENCES batches(id),
                round_no INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'open',
                expected_job_version TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                frozen_at TEXT
            );

            CREATE TABLE IF NOT EXISTS slots (
                id TEXT PRIMARY KEY,
                round_id TEXT NOT NULL REFERENCES rounds(id),
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                capacity INTEGER NOT NULL DEFAULT 1,
                UNIQUE (round_id, start_at)
            );

            CREATE TABLE IF NOT EXISTS bookings (
                id TEXT PRIMARY KEY,
                round_id TEXT NOT NULL REFERENCES rounds(id),
                candidate_id TEXT NOT NULL,
                slot_id TEXT REFERENCES slots(id),
                status TEXT NOT NULL DEFAULT 'pending',
                idempotency_key TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE (round_id, candidate_id)
            );

            CREATE TABLE IF NOT EXISTS external_actions (
                id TEXT PRIMARY KEY,
                booking_id TEXT NOT NULL REFERENCES bookings(id),
                type TEXT NOT NULL,
                idempotency_key TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                provider_id TEXT,
                attempt INTEGER NOT NULL DEFAULT 1,
                last_error TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (booking_id, type)
            );
            """
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Documents / Pages / Assessments
    # ------------------------------------------------------------------
    def upsert_document(self, doc_id: str, sha256: str, storage_path: str,
                        mime: str, parse_status: str = "pending",
                        parser_version: str = "") -> None:
        self._conn.execute(
            """INSERT INTO documents (id, sha256, storage_path, mime, parse_status, parser_version, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 parse_status=excluded.parse_status, parser_version=excluded.parser_version""",
            (doc_id, sha256, storage_path, mime, parse_status, parser_version, _now()),
        )
        self._conn.commit()

    def get_document_by_sha(self, sha256: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM documents WHERE sha256=?", (sha256,)
        ).fetchone()
        return dict(row) if row else None

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
        return dict(row) if row else None

    def save_pages(self, document_id: str, pages: List[Any]) -> None:
        with self._conn:
            for p in pages:
                self._conn.execute(
                    """INSERT OR REPLACE INTO pages
                       (document_id, page_number, text, text_hash, extraction_method, quality_flags)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (document_id, p.page_number, p.text, p.text_hash,
                     p.extraction_method, json.dumps(p.quality_flags, ensure_ascii=False)),
                )

    def get_pages(self, document_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM pages WHERE document_id=? ORDER BY page_number", (document_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def save_assessment(self, assessment: Any) -> None:
        payload = assessment.model_dump(mode="json") if hasattr(assessment, "model_dump") else assessment
        with self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO assessments
                   (id, document_id, job_version, extractor_version, rubric_version,
                    known_score, score_complete, recommendation, hard_summary, payload, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (assessment.id, assessment.document_id, assessment.job_version,
                 assessment.extractor_version, assessment.rubric_version,
                 assessment.known_score, int(assessment.score_complete),
                 assessment.recommendation.value,
                 assessment.hard_summary.value,
                 json.dumps(payload, ensure_ascii=False),
                 assessment.created_at.isoformat()),
            )

    def get_assessment(self, assessment_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM assessments WHERE id=?", (assessment_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_assessment_by_document(self, document_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM assessments WHERE document_id=? ORDER BY created_at DESC LIMIT 1",
            (document_id,),
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Reviews（审批）：request_id 幂等 + 版本绑定
    # ------------------------------------------------------------------
    def create_review(self, review: Review) -> Review:
        """幂等写入审批。同 request_id 重复提交：返回已有记录，不重复创建。"""
        existing = self._conn.execute(
            "SELECT * FROM reviews WHERE request_id=?", (review.request_id,)
        ).fetchone()
        if existing:
            return self._row_to_review(existing)
        with self._conn:
            self._conn.execute(
                """INSERT INTO reviews
                   (id, request_id, assessment_id, assessment_version, decision,
                    reason, reviewer_id, consumed, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                (review.id, review.request_id, review.assessment_id,
                 review.assessment_version, review.decision.value,
                 review.reason, review.reviewer_id, review.created_at.isoformat()),
            )
        return review

    def get_unconsumed_review(self, assessment_id: str) -> Optional[Review]:
        row = self._conn.execute(
            """SELECT * FROM reviews
               WHERE assessment_id=? AND consumed=0
               ORDER BY created_at ASC LIMIT 1""",
            (assessment_id,),
        ).fetchone()
        return self._row_to_review(row) if row else None

    def mark_review_consumed(self, review_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE reviews SET consumed=1 WHERE id=?", (review_id,)
            )

    def get_reviews(self, assessment_id: str) -> List[Review]:
        rows = self._conn.execute(
            "SELECT * FROM reviews WHERE assessment_id=? ORDER BY created_at", (assessment_id,)
        ).fetchall()
        return [self._row_to_review(r) for r in rows]

    def list_approved_candidates(self, batch_id: str, job_version: str) -> List[Dict[str, Any]]:
        """冻结名单：批次内已 approve 且评估版本匹配的候选人（含评分 payload）。"""
        rows = self._conn.execute(
            """SELECT a.document_id AS candidate_id, a.known_score, a.payload
               FROM assessments a
               JOIN reviews rv ON rv.assessment_id = a.id AND rv.decision='approve' AND rv.consumed=1
               JOIN runs ru ON ru.assessment_id = a.id AND ru.batch_id = ?
               WHERE a.job_version = ?
               GROUP BY a.document_id""",
            (batch_id, job_version),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _row_to_review(row: sqlite3.Row) -> Review:
        return Review(
            id=row["id"],
            request_id=row["request_id"],
            assessment_id=row["assessment_id"],
            assessment_version=row["assessment_version"],
            decision=ReviewDecision(row["decision"]),
            reason=row["reason"],
            reviewer_id=row["reviewer_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # ------------------------------------------------------------------
    # Runs（任务跟踪）
    # ------------------------------------------------------------------
    def create_run(self, run_id: str, thread_id: str, document_id: str,
                   batch_id: Optional[str] = None) -> Dict[str, Any]:
        with self._conn:
            self._conn.execute(
                """INSERT OR IGNORE INTO runs
                   (id, thread_id, document_id, batch_id, status, started_at)
                   VALUES (?, ?, ?, ?, 'pending', ?)""",
                (run_id, thread_id, document_id, batch_id, _now()),
            )
        row = self._conn.execute(
            "SELECT * FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            # 同 thread_id 已存在：执行身份 = thread 身份，复用同一 run 行；
            # 更新为最新批次归属，并重置为新一轮执行（status pending、清除 finished_at）。
            with self._conn:
                self._conn.execute(
                    """UPDATE runs SET batch_id=?, status='pending', finished_at=NULL
                       WHERE thread_id=?""",
                    (batch_id, thread_id),
                )
            row = self._conn.execute(
                "SELECT * FROM runs WHERE thread_id=?", (thread_id,)
            ).fetchone()
        return dict(row)

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def get_run_by_thread(self, thread_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE thread_id=?", (thread_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_runs(self, assessment_id: Optional[str] = None,
                  batch_id: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM runs WHERE 1=1"
        params: List[Any] = []
        if assessment_id:
            sql += " AND assessment_id=?"
            params.append(assessment_id)
        if batch_id:
            sql += " AND batch_id=?"
            params.append(batch_id)
        sql += " ORDER BY started_at DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def update_run(self, run_id: str, **fields: Any) -> None:
        allowed = {"status", "assessment_id", "last_error", "finished_at"}
        sets, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return
        vals.append(run_id)
        with self._conn:
            self._conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id=?", vals)

    # ------------------------------------------------------------------
    # Batches / Rounds / Slots / Bookings
    # ------------------------------------------------------------------
    def create_batch(self, batch_id: str, job_version: str) -> Batch:
        with self._conn:
            self._conn.execute(
                """INSERT INTO batches (id, job_version, status, created_at)
                   VALUES (?, ?, 'open', ?)""",
                (batch_id, job_version, _now()),
            )
        return Batch(id=batch_id, job_version=job_version)

    def latest_batch_id(self) -> Optional[str]:
        """最近一次有 run 行的批次（按行插入顺序，重启后仍可用）。"""
        row = self._conn.execute(
            "SELECT batch_id FROM runs WHERE batch_id IS NOT NULL "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return row["batch_id"] if row else None

    def get_batch(self, batch_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        return dict(row) if row else None

    def freeze_batch(self, batch_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE batches SET status='frozen', frozen_at=? WHERE id=?",
                (_now(), batch_id),
            )

    def create_round(self, round_id: str, batch_id: str, round_no: int,
                     expected_job_version: str) -> SchedulingRound:
        with self._conn:
            self._conn.execute(
                """INSERT INTO rounds (id, batch_id, round_no, status, expected_job_version, created_at)
                   VALUES (?, ?, ?, 'open', ?, ?)""",
                (round_id, batch_id, round_no, expected_job_version, _now()),
            )
        return SchedulingRound(id=round_id, batch_id=batch_id, round_no=round_no,
                               expected_job_version=expected_job_version)

    def get_round(self, round_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute("SELECT * FROM rounds WHERE id=?", (round_id,)).fetchone()
        return dict(row) if row else None

    def freeze_round(self, round_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE rounds SET status='frozen', frozen_at=? WHERE id=?",
                (_now(), round_id),
            )

    def mark_round_scheduled(self, round_id: str) -> None:
        with self._conn:
            self._conn.execute("UPDATE rounds SET status='scheduled' WHERE id=?", (round_id,))

    def insert_slots(self, slots: List[Slot]) -> None:
        with self._conn:
            for s in slots:
                self._conn.execute(
                    """INSERT OR IGNORE INTO slots
                       (id, round_id, start_at, end_at, timezone, capacity)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (s.id, s.round_id, s.start_at.isoformat(), s.end_at.isoformat(),
                     s.timezone, s.capacity),
                )

    def get_slots(self, round_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM slots WHERE round_id=? ORDER BY start_at", (round_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def create_booking(self, booking: Booking) -> Booking:
        """幂等建预约：同 (round_id, candidate_id) 重复只返回已有记录。"""
        existing = self._conn.execute(
            "SELECT * FROM bookings WHERE round_id=? AND candidate_id=?",
            (booking.round_id, booking.candidate_id),
        ).fetchone()
        if existing:
            return self._row_to_booking(existing)
        with self._conn:
            self._conn.execute(
                """INSERT INTO bookings
                   (id, round_id, candidate_id, slot_id, status, idempotency_key, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (booking.id, booking.round_id, booking.candidate_id, booking.slot_id,
                 booking.status.value, booking.idempotency_key, _now()),
            )
        return booking

    def claim_slot(self, booking_id: str, slot_id: str) -> bool:
        """原子占用时段：同 slot 已被有效占用则失败（唯一约束 + 状态检查）。"""
        try:
            with self._conn:
                taken = self._conn.execute(
                    """SELECT 1 FROM bookings WHERE slot_id=? AND status='confirmed'""",
                    (slot_id,),
                ).fetchone()
                if taken:
                    return False
                cur = self._conn.execute(
                    "SELECT status FROM bookings WHERE id=?", (booking_id,)
                ).fetchone()
                if cur is None or cur["status"] != "pending":
                    return False
                self._conn.execute(
                    "UPDATE bookings SET slot_id=?, status='confirmed' WHERE id=?",
                    (slot_id, booking_id),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def get_bookings(self, round_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM bookings WHERE round_id=? ORDER BY created_at", (round_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_booking(self, booking_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM bookings WHERE id=?", (booking_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_booking_as_model(self, booking_id: str) -> Booking:
        row = self._conn.execute(
            "SELECT * FROM bookings WHERE id=?", (booking_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"booking 不存在: {booking_id}")
        return self._row_to_booking(row)

    def update_booking_status(self, booking_id: str, status: BookingStatus) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE bookings SET status=? WHERE id=?",
                (status.value, booking_id),
            )

    @staticmethod
    def _row_to_booking(row: sqlite3.Row) -> Booking:
        return Booking(
            id=row["id"],
            round_id=row["round_id"],
            candidate_id=row["candidate_id"],
            slot_id=row["slot_id"],
            status=row["status"],
            idempotency_key=row["idempotency_key"],
        )

    # ------------------------------------------------------------------
    # External Actions（建会/通知，幂等）
    # ------------------------------------------------------------------
    def create_external_action(self, action: ExternalAction) -> ExternalAction:
        """幂等：同 (booking_id, type) 重复返回已有记录。"""
        existing = self._conn.execute(
            "SELECT * FROM external_actions WHERE booking_id=? AND type=?",
            (action.booking_id, action.type),
        ).fetchone()
        if existing:
            return self._row_to_action(existing)
        with self._conn:
            self._conn.execute(
                """INSERT INTO external_actions
                   (id, booking_id, type, idempotency_key, status, provider_id, attempt, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (action.id, action.booking_id, action.type, action.idempotency_key,
                 action.status.value, action.provider_id, action.attempt, _now()),
            )
        return action

    def update_action_status(self, action_id: str, status: str,
                             provider_id: Optional[str] = None,
                             last_error: Optional[str] = None) -> None:
        with self._conn:
            self._conn.execute(
                """UPDATE external_actions
                   SET status=?, provider_id=COALESCE(?, provider_id),
                       last_error=?, attempt=attempt+1
                   WHERE id=?""",
                (status, provider_id, last_error, action_id),
            )

    def get_action(self, action_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM external_actions WHERE id=?", (action_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_action_by_booking(self, booking_id: str, type_: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM external_actions WHERE booking_id=? AND type=?",
            (booking_id, type_),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _row_to_action(row: sqlite3.Row) -> ExternalAction:
        return ExternalAction(
            id=row["id"],
            booking_id=row["booking_id"],
            type=row["type"],
            idempotency_key=row["idempotency_key"],
            status=row["status"],
            provider_id=row["provider_id"],
            attempt=row["attempt"],
            last_error=row["last_error"],
        )

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 本地清理（DEVELOPMENT_SPEC.md §12）：按 document_id 删除
    # ------------------------------------------------------------------
    def delete_document(self, document_id: str) -> List[str]:
        """删除与文档关联的全部业务数据，返回关联 thread_id（供清理检查点）。

        级联顺序（依赖 foreign_keys=ON，按子表先行）：
        reviews(经 assessments) → external_actions → bookings → slots → rounds →
        assessments → runs → pages → documents。
        不删除原文件本身（storage_path 由调用方决定是否删）。
        """
        if self.get_document(document_id) is None:
            return []

        thread_ids = [
            r["thread_id"] for r in self._conn.execute(
                "SELECT thread_id FROM runs WHERE document_id=?", (document_id,)
            ).fetchall()
        ]
        assessment_ids = [
            r["id"] for r in self._conn.execute(
                "SELECT id FROM assessments WHERE document_id=?", (document_id,)
            ).fetchall()
        ]
        run_ids = [
            r["id"] for r in self._conn.execute(
                "SELECT id FROM runs WHERE document_id=?", (document_id,)
            ).fetchall()
        ]

        with self._conn:
            for aid in assessment_ids:
                self._conn.execute("DELETE FROM reviews WHERE assessment_id=?", (aid,))
            # 该候选人的预约与外部动作（先子表后父表）
            self._conn.execute(
                """DELETE FROM external_actions WHERE booking_id IN
                   (SELECT id FROM bookings WHERE candidate_id=?)""",
                (document_id,),
            )
            self._conn.execute("DELETE FROM bookings WHERE candidate_id=?", (document_id,))
            for rid in run_ids:
                self._conn.execute("DELETE FROM runs WHERE id=?", (rid,))
            self._conn.execute("DELETE FROM assessments WHERE document_id=?", (document_id,))
            self._conn.execute("DELETE FROM pages WHERE document_id=?", (document_id,))
            self._conn.execute("DELETE FROM documents WHERE id=?", (document_id,))
        return thread_ids

    @staticmethod
    def delete_checkpoint_threads(thread_ids: List[str]) -> None:
        """清理 LangGraph 检查点（与业务库同目录的 checkpoints.db）。

        workflow._get_checkpointer() 复用的是同一 SqliteSaver 实例，
        因此直接删除线程即可；若缓存未建立则静默跳过。
        """
        if not thread_ids:
            return
        from .workflow import _get_checkpointer

        try:
            saver = _get_checkpointer()
        except Exception:
            return
        for tid in thread_ids:
            try:
                saver.delete_thread(tid)
            except Exception:
                continue


_default_repo: Optional[Repository] = None


def get_repository(db_path: str | Path | None = None) -> Repository:
    """进程内单例仓库（CLI/测试共享）。

    默认参数为 None，运行时解析 DB_PATH，便于测试 monkeypatch 生效。
    """
    global _default_repo
    if db_path is not None:
        return Repository(db_path)
    if _default_repo is None:
        _default_repo = Repository(DB_PATH)
    return _default_repo
