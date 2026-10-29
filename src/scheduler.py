# -*- coding: utf-8 -*-
"""批次排期器（对应 DEVELOPMENT_SPEC.md §8）。

- 每轮 4 个 45 分钟时段（带时区，可注入时钟；默认 Asia/Shanghai）。
- 仅对"已批准且属于冻结名单"的候选人排序：total_score DESC → MiC 维 DESC →
  海外维 DESC → 管理维 DESC → candidate_id ASC。
- 按候选人可用时段集合分配最早可用时段；集合为空/全部占用 → 候补。
- 数据库事务 + 唯一约束防重：round+candidate 唯一有效预约、slot 唯一占用。
- 会议成功后才允许通知；通知失败只重试通知，不重复建会。
"""
from __future__ import annotations

import json
import threading
from functools import wraps
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .config import JOB_PROFILE
from .repository import Repository, get_repository
from .schemas import Booking, BookingStatus, ExternalAction, SchedulingRound, Slot
from .providers.mock_calendar import MockCalendar, get_calendar
from .providers.mock_notifier import MockNotifier, get_notifier

TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
ROUND_SLOT_COUNT = 4
SLOT_MINUTES = 45
_SCHEDULE_LOCK = threading.RLock()

def _serialized(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        with _SCHEDULE_LOCK:
            return fn(*args, **kwargs)
    return call


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def create_round(batch_id: str, job_version: str = JOB_PROFILE.version,
                 repo: Repository | None = None) -> SchedulingRound:
    """为批次创建排期轮：生成 4 个未来 45 分钟时段（可注入 now）。"""
    repo = repo or get_repository()
    batch = repo.get_batch(batch_id)
    if batch is None:
        raise ValueError(f"batch 不存在: {batch_id}")

    rounds = _list_rounds(batch_id, repo)
    round_no = (max(r["round_no"] for r in rounds) + 1) if rounds else 1
    round_id = _new_id("rnd")
    sround = repo.create_round(round_id, batch_id, round_no, job_version)

    slots = _make_slots(round_id, now=_now())
    repo.insert_slots(slots)
    return sround


def _make_slots(round_id: str, now: datetime) -> List[Slot]:
    # 对齐到下一整点，生成连续 4 个 45 分钟时段
    start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    slots = []
    for i in range(ROUND_SLOT_COUNT):
        s = start + timedelta(minutes=i * SLOT_MINUTES)
        slots.append(Slot(
            id=f"slot_{round_id}_{i+1}",
            round_id=round_id,
            start_at=s,
            end_at=s + timedelta(minutes=SLOT_MINUTES),
            timezone="Asia/Shanghai",
            capacity=1,
        ))
    return slots


def _now() -> datetime:
    return datetime.now(TZ)


def close_round(round_id: str, expected_job_version: str,
                repo: Repository | None = None) -> SchedulingRound:
    """冻结排期轮（批次审批截止）：记录期望版本，阻止后续变动。"""
    repo = repo or get_repository()
    r = repo.get_round(round_id)
    if r is None:
        raise ValueError(f"round 不存在: {round_id}")
    if r["expected_job_version"] != expected_job_version:
        raise ValueError(
            f"排期轮版本不匹配: {r['expected_job_version']} != {expected_job_version}"
        )
    if r["status"] != "open":
        raise ValueError(f"round 状态不允许冻结: {r['status']}")
    repo.freeze_round(round_id)
    repo.freeze_batch(r["batch_id"])
    return SchedulingRound(
        id=round_id, batch_id=r["batch_id"], round_no=r["round_no"],
        status="frozen", expected_job_version=expected_job_version,
    )


@_serialized
def schedule_round(round_id: str,
                   availability: Optional[Dict[str, List[int]]] = None,
                   repo: Repository | None = None) -> List[Booking]:
    """全局排序 + 原子占用时段；availability: candidate_id -> slot 序号列表。

    未提供 availability 时视为所有时段可用（演示配置，规格 §8 允许）。
    """
    repo = repo or get_repository()
    r = repo.get_round(round_id)
    if r is None:
        raise ValueError(f"round 不存在: {round_id}")
    if r["status"] == "scheduled":
        # 幂等：重复排期返回既有结果，不重复分配
        return [_get_booking(b["id"], repo) for b in repo.get_bookings(round_id)]
    if r["status"] not in ("open", "frozen"):
        raise ValueError(f"round 状态不允许排期: {r['status']}")

    slots = repo.get_slots(round_id)
    if len(slots) < ROUND_SLOT_COUNT:
        raise ValueError(f"时段不足: 需要 {ROUND_SLOT_COUNT} 个，当前 {len(slots)}")

    candidates = _approved_candidates(r["batch_id"], r["expected_job_version"], repo)
    candidates.sort(key=_rank_key)
    avail = availability or {}

    bookings: List[Booking] = []
    occupied = {b["slot_id"] for b in repo.get_bookings(round_id)
                if b["status"] == "confirmed" and b["slot_id"]}
    slot_used: Dict[str, bool] = {s["id"]: s["id"] in occupied for s in slots}

    for cand in candidates:
        cand_id = cand["candidate_id"]
        slot_ids = _candidate_slots(cand_id, slots, avail)
        chosen = None
        for sid in slot_ids:
            if not slot_used[sid]:
                chosen = sid
                break

        # 先插入 pending 预约（slot 未占用），claim 成功才置 confirmed
        booking = Booking(
            id=_new_id("bk"),
            round_id=round_id,
            candidate_id=cand_id,
            slot_id=None,
            status=BookingStatus.PENDING,
            idempotency_key=f"bk_{round_id}_{cand_id}",
        )
        created = repo.create_booking(booking)  # 幂等：同 round+candidate 复用
        if created.id != booking.id and created.status != BookingStatus.PENDING:
            # 已完成的预约保持原样；中断留下的 pending 继续占位。
            bookings.append(created)
            continue
        booking = created
        if chosen and repo.claim_slot(booking.id, chosen):
            slot_used[chosen] = True
            bookings.append(repo.get_booking_as_model(booking.id))
        else:
            repo.update_booking_status(booking.id, BookingStatus.WAITLIST)
            bookings.append(repo.get_booking_as_model(booking.id))

    repo.mark_round_scheduled(round_id)
    return bookings


def _candidate_slots(cand_id: str, slots: List[Dict[str, Any]],
                     avail: Dict[str, List[int]]) -> List[str]:
    if cand_id not in avail:
        return [s["id"] for s in slots]
    wanted = avail[cand_id]
    ordered = [s["id"] for i, s in enumerate(slots, start=1) if i in wanted]
    return ordered


def _approved_candidates(batch_id: str, job_version: str,
                         repo: Repository) -> List[Dict[str, Any]]:
    """冻结名单：该批次内已 approve 且评估版本匹配的候选人。"""
    rows = repo.list_approved_candidates(batch_id, job_version)
    out = []
    for row in rows:
        payload = json.loads(row["payload"]) if row["payload"] else {}
        dims = payload.get("dimensions", {})
        out.append({
            "candidate_id": row["candidate_id"],
            "known_score": row["known_score"],
            "mic_score": _dim_score(dims, "steel_mic_experience"),
            "overseas_score": _dim_score(dims, "overseas"),
            "management_score": _dim_score(dims, "management"),
        })
    return out


def _dim_score(dims: Dict[str, Any], key: str) -> int:
    d = dims.get(key) or {}
    return d.get("score") if isinstance(d.get("score"), int) else -1


def _rank_key(c: Dict[str, Any]) -> tuple:
    # total_score DESC → MiC DESC → 海外 DESC → 管理 DESC → candidate_id ASC
    return (-c["known_score"], -c["mic_score"], -c["overseas_score"],
            -c["management_score"], c["candidate_id"])


@_serialized
def run_external(booking: Booking, repo: Repository | None = None,
                 calendar: Optional[MockCalendar] = None,
                 notifier: Optional[MockNotifier] = None) -> Dict[str, Any]:
    """沿用预约和动作幂等键，只执行尚未成功的动作。"""
    repo = repo or get_repository()
    calendar = calendar or get_calendar()
    notifier = notifier or get_notifier()
    if repo.get_booking(booking.id)['status'] != 'confirmed':
        raise ValueError('只有已确认预约可以建会和通知')
    for kind, prefix in [('meeting', 'meet'), ('notify', 'ntf')]:
        action = repo.create_external_action(ExternalAction(
            id=_new_id('act'), booking_id=booking.id, type=kind,
            idempotency_key=f'{prefix}_{booking.id}', status='pending', attempt=0))
        result = repo.get_action(action.id)
        if result['status'] != 'succeeded':
            result = retry_action(action.id, repo, calendar, notifier)
        if result['status'] != 'succeeded':
            return result
    return result


@_serialized
def retry_action(action_id: str, repo: Repository | None = None,
                 calendar: Optional[MockCalendar] = None,
                 notifier: Optional[MockNotifier] = None) -> Dict[str, Any]:
    """成功动作直接返回；通知只在会议已成功时允许重试。"""
    repo = repo or get_repository()
    calendar = calendar or get_calendar()
    notifier = notifier or get_notifier()
    action = repo.get_action(action_id)
    if action is None:
        raise ValueError(f'action 不存在: {action_id}')
    if action['status'] == 'succeeded':
        return action
    booking = _get_booking(action['booking_id'], repo)
    if booking.status != BookingStatus.CONFIRMED:
        raise ValueError('预约未确认，不能执行外部动作')
    if action['type'] == 'meeting':
        ok, provider_id, err = calendar.create_meeting(booking.id, action['idempotency_key'])
    elif action['type'] == 'notify':
        meeting = repo.get_action_by_booking(booking.id, 'meeting')
        if not meeting or meeting['status'] != 'succeeded':
            raise ValueError('会议尚未创建，不能发送通知')
        ok, provider_id, err = notifier.send(meeting['provider_id'], action['idempotency_key'])
    else:
        raise ValueError('未知外部动作类型')
    repo.update_action_status(action_id, 'succeeded' if ok else 'failed',
                              provider_id=provider_id, last_error=err)
    return repo.get_action(action_id)


def _get_booking(booking_id: str, repo: Repository) -> Booking:
    row = repo.get_booking(booking_id)
    if row is None:
        raise ValueError(f"booking 不存在: {booking_id}")
    return Booking(
        id=row["id"], round_id=row["round_id"], candidate_id=row["candidate_id"],
        slot_id=row["slot_id"], status=row["status"],
        idempotency_key=row["idempotency_key"],
    )


def _list_rounds(batch_id: str, repo: Repository) -> List[Dict[str, Any]]:
    rows = repo._conn.execute(
        "SELECT * FROM rounds WHERE batch_id=? ORDER BY round_no", (batch_id,)
    ).fetchall()
    return [dict(r) for r in rows]
