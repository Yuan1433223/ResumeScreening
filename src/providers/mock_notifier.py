# -*- coding: utf-8 -*-
"""Mock 通知服务（对应 DEVELOPMENT_SPEC.md §8）。

- send(meeting_id, idempotency_key) -> (ok, provider_id, error)
- 通知失败只重试通知，不重建会议；幂等键保证重复发送返回稳定结果。
"""
from __future__ import annotations

import uuid
from typing import Dict, Optional, Tuple


class MockNotifier:
    """内存 Mock 通知器；幂等键 → 稳定通知 ID。"""

    def __init__(self, fail_times: int = 0):
        self._notifications: Dict[str, str] = {}
        self._fail_remaining = fail_times

    def send(self, meeting_id: str, idempotency_key: str) -> Tuple[bool, Optional[str], Optional[str]]:
        if idempotency_key in self._notifications:
            return True, self._notifications[idempotency_key], None
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            return False, None, "mock notifier 暂不可用（注入故障）"
        notify_id = f"ntf_{uuid.uuid4().hex[:10]}"
        self._notifications[idempotency_key] = notify_id
        return True, notify_id, None


_default_notifier: Optional[MockNotifier] = None


def get_notifier() -> MockNotifier:
    global _default_notifier
    if _default_notifier is None:
        _default_notifier = MockNotifier()
    return _default_notifier
