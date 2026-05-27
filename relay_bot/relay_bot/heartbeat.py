"""
心跳上报：定期通过飞书消息发送心跳给 Gateway。

Gateway 轮询会话消息时解析到心跳 → 自动注册/更新节点。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .worker import Worker

from . import __version__

logger = logging.getLogger("relay-bot.heartbeat")

_started_at = datetime.now().isoformat(timespec="seconds")


def _build_heartbeat(worker: "Worker") -> dict:
    inflight = 0
    try:
        inflight = worker._assembler.inflight_count
    except Exception:
        pass
    return {
        "_relay_v": 3,
        "type": "heartbeat",
        "node_id": worker.cfg.node_id,
        "open_id": worker.cfg.open_id,
        "chat_id": worker.chat_id or worker.cfg.chat_id,
        "version": __version__,
        "hostname": socket.gethostname(),
        "ip": _get_ip(),
        "started_at": _started_at,
        "load": _get_load(),
        "models": [
            "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6",
            "gpt-5.5", "gpt-5.4", "kimi-k2.6", "glm-5.1",
        ],
        "capabilities": ["zlib", "relay_v3", "multipart_in", "stream_out"],
        "inflight_multipart": inflight,
    }


def _get_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "unknown"


def _get_load() -> float:
    try:
        return os.getloadavg()[0] / os.cpu_count()
    except (OSError, AttributeError):
        return 0.0


def _heartbeat_loop(worker: "Worker"):
    """后台线程：定期发心跳到 Gateway 所在会话。"""
    interval = worker.cfg.heartbeat_interval_s
    time.sleep(5)

    while True:
        try:
            chat_id = worker.chat_id
            if chat_id and worker._lark_client:
                hb = _build_heartbeat(worker)
                text = json.dumps(hb, ensure_ascii=False)
                worker.reply_text(chat_id, text)
                logger.info("heartbeat sent: node_id=%s version=%s load=%.2f",
                            hb["node_id"], hb["version"], hb["load"])
            else:
                logger.debug("heartbeat skipped: no chat_id yet")
        except Exception as e:
            logger.warning("心跳发送失败: %s", e)

        time.sleep(interval)


def start_heartbeat(worker: "Worker") -> threading.Thread:
    """启动心跳后台线程。"""
    t = threading.Thread(target=_heartbeat_loop, args=(worker,), daemon=True)
    t.start()
    return t
