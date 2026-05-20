"""
存储 Bot 发出的消息，按 receive_id 索引，方便测试侧轮询查询。
内存实现，进程内有效；如果你需要持久化或多进程，自己换成 Redis 即可。
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional


class SentMessageStore:
    def __init__(self, max_per_receiver: int = 200):
        self._lock = threading.Lock()
        # key: receive_id -> deque of messages
        self._by_receiver: Dict[str, Deque[dict]] = defaultdict(
            lambda: deque(maxlen=max_per_receiver)
        )
        # 全部消息（按收到顺序），便于不指定 receiver 时查询
        self._all: Deque[dict] = deque(maxlen=2000)

    def add(
        self,
        *,
        receive_id: str,
        receive_id_type: str,
        msg_type: str,
        content: str,
        raw_request: dict,
    ) -> dict:
        record = {
            "message_id": f"om_mock_send_{uuid.uuid4().hex}",
            "receive_id": receive_id,
            "receive_id_type": receive_id_type,
            "msg_type": msg_type,
            "content": content,
            "raw_request": raw_request,
            "create_time_ms": int(time.time() * 1000),
        }
        with self._lock:
            self._by_receiver[receive_id].append(record)
            self._all.append(record)
        return record

    def list(
        self,
        *,
        receive_id: Optional[str] = None,
        since_ms: Optional[int] = None,
        limit: int = 100,
    ) -> List[dict]:
        with self._lock:
            src = list(self._by_receiver.get(receive_id, [])) if receive_id else list(
                self._all
            )
        if since_ms is not None:
            src = [m for m in src if m["create_time_ms"] > since_ms]
        return src[-limit:]

    def clear(self, receive_id: Optional[str] = None) -> int:
        with self._lock:
            if receive_id is None:
                n = len(self._all)
                self._all.clear()
                self._by_receiver.clear()
                return n
            n = len(self._by_receiver.get(receive_id, []))
            self._by_receiver.pop(receive_id, None)
            # 同步从 _all 中过滤
            self._all = deque(
                (m for m in self._all if m["receive_id"] != receive_id),
                maxlen=self._all.maxlen,
            )
            return n


# 单例
store = SentMessageStore()
