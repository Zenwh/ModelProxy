"""
限流引擎：
  - RPM 滑动窗口 (in-memory)
  - Daily token 累计 (基于 usage)
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional, Tuple

from .usage import UsageManager


class RateLimiter:
    """
    内存滑动窗口 RPM 限流 + 委托 usage 做日 token 配额检查。
    重启后 RPM 计数清零（短期限流），daily 计数走 usage 持久化。
    """

    def __init__(self, usage_mgr: UsageManager):
        self._rpm_buckets: Dict[str, Deque[float]] = defaultdict(deque)
        self._usage = usage_mgr
        self._lock = threading.Lock()

    def check_rpm(self, key_name: str, limit: int) -> Tuple[bool, int]:
        """
        滑动窗口检查 RPM。
        返回 (ok, retry_after_seconds)。
        如果 ok，会顺带把当前时间戳记到 bucket 里。
        """
        if not limit:
            return True, 0

        with self._lock:
            now = time.time()
            window_start = now - 60.0
            bucket = self._rpm_buckets[key_name]

            # 清理超过 60s 的旧时间戳
            while bucket and bucket[0] < window_start:
                bucket.popleft()

            if len(bucket) >= limit:
                # 限流：retry_after = 最早那条时间戳过期的剩余秒数
                oldest = bucket[0]
                retry = max(1, int(60 - (now - oldest)) + 1)
                return False, retry

            bucket.append(now)
            return True, 0

    def check_daily_tokens(
        self,
        key_name: str,
        limit: int,
        projected: int = 0,
    ) -> Tuple[bool, int]:
        """
        检查日 token 配额。
        projected：本次预估输入 token（可以拿 max_tokens 估）。
        返回 (ok, remaining)。
        """
        if not limit:
            return True, 0

        used = self._usage.daily_token_count(key_name)
        remaining = limit - used
        if used + projected > limit:
            return False, max(0, remaining)
        return True, remaining

    def rpm_current(self, key_name: str) -> int:
        """当前 RPM bucket 里的请求数（用于 dashboard 显示）。"""
        with self._lock:
            now = time.time()
            window_start = now - 60.0
            bucket = self._rpm_buckets.get(key_name)
            if not bucket:
                return 0
            return sum(1 for ts in bucket if ts >= window_start)
