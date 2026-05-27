"""
StreamEmitter：worker 端流式增量缓冲器。

把上游 MP 的 SSE 增量按 1KB / 1s 一片聚合后，通过 send_relay_response 发飞书 stream_chunk。
带本地令牌桶（默认 4 msg/s, 容量 5）防飞书消息频率限流。

flush 触发条件（任一）：
- 累计文本字节 ≥ flush_bytes (默认 1024)
- 距上次 flush ≥ flush_ms (默认 1000ms)
- tool_use 闭合（partial_json 收齐）
- 显式 flush() / close()
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .worker import Worker

logger = logging.getLogger("stream-emitter")


class _TokenBucket:
    def __init__(self, rate: float, capacity: int):
        self.rate = rate
        self.capacity = capacity
        self.tokens = float(capacity)
        self.last = time.time()
        self.lock = threading.Lock()

    def consume(self, n: int = 1) -> float:
        """阻塞前的等待秒数（>=0）。返回 0 立即可用。"""
        with self.lock:
            now = time.time()
            elapsed = now - self.last
            self.last = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            if self.tokens >= n:
                self.tokens -= n
                return 0.0
            need = n - self.tokens
            wait = need / self.rate
            self.tokens = 0
            return wait


class StreamEmitter:
    """worker 端单次请求的流式增量缓冲器。"""

    def __init__(
        self,
        worker: "Worker",
        chat_id: str,
        req_id: str,
        mode: str,                # "chat" | "messages_native"
        flush_bytes: int = 1024,
        flush_ms: int = 1000,
        send_qps: float = 4.0,
    ):
        self._worker = worker
        self._chat_id = chat_id
        self._req_id = req_id
        self._mode = mode
        self._flush_bytes = flush_bytes
        self._flush_ms = flush_ms
        self._seq = 0
        self._buf_text: List[str] = []
        self._buf_events: List[Dict[str, Any]] = []   # anthropic 事件
        self._buf_size = 0
        self._last_flush = time.time()
        self._lock = threading.Lock()
        self._bucket = _TokenBucket(rate=send_qps, capacity=max(2, int(send_qps + 1)))

    @property
    def seq_total(self) -> int:
        return self._seq

    def feed_text(self, text: str):
        if not text:
            return
        with self._lock:
            self._buf_text.append(text)
            self._buf_size += len(text.encode("utf-8"))
        self._maybe_flush()

    def feed_event(self, event_type: str, data: Dict[str, Any]):
        """messages_native 模式下，把 anthropic SSE 事件原样攒进 buffer。"""
        with self._lock:
            self._buf_events.append({"event": event_type, "data": data})
            # 估算尺寸用 event_type + data 的简单序列化长度
            self._buf_size += len(event_type) + 64
            for v in data.values():
                if isinstance(v, str):
                    self._buf_size += len(v.encode("utf-8"))
        self._maybe_flush()

    def feed_tool_use(self, tool_id: str, name: str, partial_json: str):
        """chat 模式 tool_calls 累计。tool 闭合后立即 flush。"""
        delta = {"tool_use": {"id": tool_id, "name": name, "partial_json": partial_json}}
        self._send_chunk_now(delta)

    def feed_thinking(self, text: str):
        if not text:
            return
        delta = {"thinking": text}
        self._send_chunk_now(delta)

    def _maybe_flush(self):
        with self._lock:
            elapsed_ms = (time.time() - self._last_flush) * 1000
            if (
                self._buf_size >= self._flush_bytes
                or elapsed_ms >= self._flush_ms
            ):
                self._flush_locked()

    def flush(self):
        with self._lock:
            self._flush_locked()

    def _flush_locked(self):
        if not self._buf_text and not self._buf_events:
            return
        text = "".join(self._buf_text)
        events = list(self._buf_events)
        self._buf_text.clear()
        self._buf_events.clear()
        self._buf_size = 0
        self._last_flush = time.time()

        delta: Dict[str, Any] = {}
        if text:
            delta["text"] = text
        if events:
            delta["events"] = events

        # 出 lock 再发飞书（避免阻塞 feed）
        seq = self._seq
        self._seq += 1
        # release lock implicitly when method returns; here we still hold it.
        # Do send outside the lock by deferring with a thread? — simpler: we accept
        # that flush is rare (1/s) and tolerable to block briefly.
        self._send_with_throttle({"seq": seq, "mode": self._mode, "delta": delta})

    def _send_chunk_now(self, delta: Dict[str, Any]):
        """tool_use / thinking 等闭合事件不走 buffer，立刻发出。"""
        with self._lock:
            seq = self._seq
            self._seq += 1
            self._last_flush = time.time()
        self._send_with_throttle({"seq": seq, "mode": self._mode, "delta": delta})

    def _send_with_throttle(self, payload: Dict[str, Any]):
        wait = self._bucket.consume(1)
        if wait > 0:
            time.sleep(min(wait, 2.0))
        envelope = {
            "type": "stream_chunk",
            "req_id": self._req_id,
            **payload,
        }
        try:
            self._worker.send_relay_response(self._chat_id, envelope)
        except Exception:
            logger.exception("emit stream_chunk failed req_id=%s seq=%d", self._req_id, payload.get("seq"))

    def close(self):
        self.flush()
