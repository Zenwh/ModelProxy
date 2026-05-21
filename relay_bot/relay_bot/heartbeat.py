"""
心跳上报：定期通过飞书 WS 发送心跳消息给 Gateway。
"""
from __future__ import annotations

import json
import logging
import os
import platform
import socket
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .worker import Worker

from . import __version__

logger = logging.getLogger("relay-bot.heartbeat")


def _build_heartbeat(worker: "Worker") -> dict:
    return {
        "_relay_v": 2,
        "type": "heartbeat",
        "node_id": worker.cfg.node_id,
        "version": __version__,
        "hostname": socket.gethostname(),
        "ip": _get_ip(),
        "started_at": _started_at,
        "load": _get_load(),
        "models": [],
    }


_started_at = datetime.now().isoformat(timespec="seconds")


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
    """后台线程：定期发心跳。"""
    interval = worker.cfg.heartbeat_interval_s
    # 启动后等几秒再发第一次心跳（等 WS 连接建立）
    time.sleep(5)

    while True:
        try:
            hb = _build_heartbeat(worker)
            # 通过飞书 lark SDK 发消息（走 WS 通道回 Gateway 所在会话）
            # 心跳消息发到 bot 自己所在的会话，Gateway 通过轮询读到
            text = json.dumps(hb, ensure_ascii=False)
            # 使用 lark client 的 reply 机制 — 实际上 bot 需要知道跟 Gateway 的 chat_id
            # 这里先 log，具体发送逻辑取决于 Gateway 如何收心跳
            logger.info("heartbeat: node_id=%s version=%s load=%.2f",
                        hb["node_id"], hb["version"], hb["load"])

            # 如果有 Gateway chat_id，发送心跳消息
            gateway_chat_id = os.getenv("GATEWAY_CHAT_ID", "")
            if gateway_chat_id and worker._lark_client:
                worker.reply_text(gateway_chat_id, text)

        except Exception as e:
            logger.warning("心跳发送失败: %s", e)

        time.sleep(interval)


def start_heartbeat(worker: "Worker") -> threading.Thread:
    """启动心跳后台线程。"""
    t = threading.Thread(target=_heartbeat_loop, args=(worker,), daemon=True)
    t.start()
    return t
