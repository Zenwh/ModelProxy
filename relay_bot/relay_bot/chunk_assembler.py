"""
Worker 端 v3 多片重组：

gateway 把单条大请求按 MULTIPART_CHUNK_BYTES (~120KB) 切成 N 个 req_part 飞书消息发过来；
worker 收到第 0 片就建一个 inflight 槽位，按 part_index 装回 dict。

特性：
- 全量到达即触发回调（一次性）
- 60s 超时清理 + 触发 timeout 回调（让 worker 回 `resp(ok=false, error="multipart_timeout")`）
- 重复到达同一 part_index 直接覆盖（飞书理论不会重投，但兜底）
- 乱序到达没问题：parts[idx] 写位即可
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("chunk-assembler")


@dataclass
class _Inflight:
    req_id: str
    part_total: int
    parts: List[Optional[str]]
    received: int = 0
    deadline: float = 0.0
    # 仅 part_index=0 携带的元信息，重组完整 dict 时附加
    meta: Dict[str, object] = field(default_factory=dict)
    # chat_id（飞书消息所在会话），timeout 回调要发回去
    chat_id: str = ""


class ChunkAssembler:
    """v3 req_part 重组器。线程安全。"""

    def __init__(
        self,
        timeout_s: int = 60,
        on_timeout: Optional[Callable[[str, str, dict], None]] = None,
        sweep_interval_s: int = 5,
    ):
        self._timeout_s = timeout_s
        self._on_timeout = on_timeout
        self._sweep_interval = sweep_interval_s
        self._inflight: Dict[str, _Inflight] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sweeper: Optional[threading.Thread] = None

    def start(self):
        if self._sweeper and self._sweeper.is_alive():
            return
        self._stop.clear()
        self._sweeper = threading.Thread(
            target=self._sweep_loop, name="chunk-assembler-sweep", daemon=True,
        )
        self._sweeper.start()

    def stop(self):
        self._stop.set()

    @property
    def inflight_count(self) -> int:
        with self._lock:
            return len(self._inflight)

    def add_part(
        self,
        req_id: str,
        part_index: int,
        part_total: int,
        payload_chunk: str,
        chat_id: str = "",
        meta: Optional[Dict[str, object]] = None,
    ) -> Optional[dict]:
        """
        添加一个 part；齐全后返回组装好的请求 dict，否则返回 None。
        """
        if part_total <= 0 or part_index < 0 or part_index >= part_total:
            logger.warning(
                "bad part: req_id=%s idx=%d total=%d",
                req_id, part_index, part_total,
            )
            return None

        with self._lock:
            inflight = self._inflight.get(req_id)
            if inflight is None:
                inflight = _Inflight(
                    req_id=req_id,
                    part_total=part_total,
                    parts=[None] * part_total,
                    deadline=time.time() + self._timeout_s,
                    chat_id=chat_id or "",
                )
                self._inflight[req_id] = inflight
            else:
                # part_total 不一致说明协议错乱，丢弃
                if inflight.part_total != part_total:
                    logger.warning(
                        "part_total mismatch req_id=%s old=%d new=%d",
                        req_id, inflight.part_total, part_total,
                    )
                    return None

            if inflight.parts[part_index] is None:
                inflight.received += 1
            inflight.parts[part_index] = payload_chunk
            if chat_id and not inflight.chat_id:
                inflight.chat_id = chat_id
            if part_index == 0 and meta:
                inflight.meta.update(meta)

            if inflight.received < inflight.part_total:
                return None

            # 全部到齐：拼接 + json.loads
            self._inflight.pop(req_id, None)

        body = "".join(p or "" for p in inflight.parts)
        # payload_encoding 由 part_index=0 在 meta 里指明（plain / zb64）；
        # zb64 = base64(zlib(body))，先 base64 decode 再 zlib decompress
        encoding = inflight.meta.get("payload_encoding", "plain") if inflight.meta else "plain"
        if encoding == "zb64":
            import base64
            import zlib
            try:
                body = zlib.decompress(base64.b64decode(body)).decode("utf-8")
            except Exception as e:
                logger.warning(
                    "multipart zb64 decode failed req_id=%s: %s", req_id, e,
                )
                return None
        try:
            full = json.loads(body)
        except Exception as e:
            logger.warning("multipart re-assemble json.loads failed req_id=%s: %s", req_id, e)
            return None
        if not isinstance(full, dict):
            logger.warning("multipart re-assembled not dict req_id=%s", req_id)
            return None
        # meta 用于 inflight 期间路由判断；最终请求 dict 以拼接结果为准
        return full

    def _sweep_loop(self):
        while not self._stop.is_set():
            try:
                self._sweep_once()
            except Exception:
                logger.exception("sweep loop error")
            self._stop.wait(self._sweep_interval)

    def _sweep_once(self):
        now = time.time()
        timed_out: List[_Inflight] = []
        with self._lock:
            for req_id, inflight in list(self._inflight.items()):
                if now >= inflight.deadline:
                    timed_out.append(inflight)
                    self._inflight.pop(req_id, None)

        for inflight in timed_out:
            logger.warning(
                "multipart timeout req_id=%s received=%d/%d",
                inflight.req_id, inflight.received, inflight.part_total,
            )
            if self._on_timeout:
                try:
                    self._on_timeout(
                        inflight.req_id,
                        inflight.chat_id,
                        {
                            "received": inflight.received,
                            "expected": inflight.part_total,
                        },
                    )
                except Exception:
                    logger.exception("on_timeout callback failed")
