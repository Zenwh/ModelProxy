"""
管控指令处理：升级、重启、优雅下线。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .worker import Worker

logger = logging.getLogger("relay-bot.ctrl")


def handle_ctrl(worker: "Worker", msg: dict):
    """处理来自 Gateway 的管控指令。"""
    action = msg.get("action", "")
    logger.info("收到管控指令: action=%s", action)

    if action == "upgrade":
        target_version = msg.get("target_version", "latest")
        _do_upgrade(target_version)
    elif action == "restart":
        _do_restart()
    elif action == "drain":
        _do_drain(worker)
    else:
        logger.warning("未知管控指令: %s", action)


def _do_upgrade(target_version: str):
    """升级 bot 包并退出（systemd 会自动重启）。"""
    from .upgrade import upgrade_and_exit
    upgrade_and_exit(target_version)


def _do_restart():
    """直接退出，由 systemd 重启。"""
    import sys
    logger.info("执行重启: exit(0)")
    sys.exit(0)


def _do_drain(worker: "Worker"):
    """优雅下线：停止接新请求，等现有请求处理完，退出。"""
    import sys
    import time
    logger.info("执行优雅下线: drain")
    worker._running = False
    # 等待 5 秒让正在处理的请求完成
    time.sleep(5)
    sys.exit(0)
