"""
自升级：pip install -U feishu-relay-bot + exit。
"""
from __future__ import annotations

import logging
import subprocess
import sys

logger = logging.getLogger("relay-bot.upgrade")


def upgrade_and_exit(target_version: str = "latest"):
    """
    升级 feishu-relay-bot 包然后退出进程。
    systemd/supervisor 会自动拉起新版本。
    """
    pkg = "feishu-relay-bot"
    if target_version and target_version != "latest":
        pkg = f"feishu-relay-bot=={target_version}"

    logger.info("开始升级: %s", pkg)

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", pkg],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            logger.info("升级成功，准备重启\n%s", result.stdout[-200:] if result.stdout else "")
        else:
            logger.error("升级失败 (code=%d):\n%s", result.returncode, result.stderr[-500:])
            return  # 升级失败不退出
    except subprocess.TimeoutExpired:
        logger.error("升级超时（120s）")
        return
    except Exception as e:
        logger.error("升级异常: %s", e)
        return

    logger.info("退出进程，等待 systemd 重启新版本 ...")
    sys.exit(0)
