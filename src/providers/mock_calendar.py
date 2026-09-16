# -*- coding: utf-8 -*-
"""Mock 日历服务（对应 DEVELOPMENT_SPEC.md §8）。

- create_meeting(booking_id, idempotency_key) -> (ok, provider_id, error)
- 幂等键保证同一 booking 重复调用返回稳定 provider_id。
- 测试可注入故障（fail_times）模拟外部失败。
"""
from __future__ import annotations

import uuid
from typing import Dict, Optional, Tuple


class MockCalendar:
    """内存 Mock 日历；幂等键 → 稳定会议 ID。"""

    def __init__(self, fail_times: int = 0):
        self._meetings: Dict[str, str] = {}
        self._fail_remaining = fail_times

    def create_meeting(self, booking_id: str, idempotency_key: str) -> Tuple[bool, Optional[str], Optional[str]]:
        if idempotency_key in self._meetings:
            return True, self._meetings[idempotency_key], None  # 幂等：返回既有会议
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            return False, None, "mock calendar 暂不可用（注入故障）"
        meeting_id = f"mtg_{uuid.uuid4().hex[:10]}"
        self._meetings[idempotency_key] = meeting_id
        return True, meeting_id, None


_default_calendar: Optional[MockCalendar] = None


def get_calendar() -> MockCalendar:
    global _default_calendar
    if _default_calendar is None:
        _default_calendar = MockCalendar()
    return _default_calendar
