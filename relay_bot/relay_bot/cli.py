"""
CLI 入口：feishu-relay-bot run|version
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import __version__


def main():
    parser = argparse.ArgumentParser(
        prog="feishu-relay-bot",
        description="Feishu Relay Bot — 飞书消息通道 AI 中继节点",
    )
    sub = parser.add_subparsers(dest="command")

    # run
    run_parser = sub.add_parser("run", help="启动 bot")
    run_parser.add_argument("--config", "-c", default=None, help="配置文件路径 (yaml)")

    # version
    sub.add_parser("version", help="显示版本号")

    args = parser.parse_args()

    if args.command == "version" or not args.command:
        if not args.command:
            parser.print_help()
            sys.exit(0)
        print(f"feishu-relay-bot {__version__}")
        return

    if args.command == "run":
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
        logger = logging.getLogger("relay-bot")

        from .config import Config
        from .worker import Worker

        cfg = Config.load(args.config)
        if not cfg.feishu_app_id or not cfg.feishu_app_secret:
            logger.error("缺少飞书配置: FEISHU_APP_ID / FEISHU_APP_SECRET")
            sys.exit(1)

        logger.info("feishu-relay-bot v%s", __version__)
        logger.info("  node_id: %s", cfg.node_id)
        logger.info("  mp_url: %s", cfg.mp_url)
        logger.info("  heartbeat: %ds", cfg.heartbeat_interval_s)

        worker = Worker(cfg)
        worker.start()


if __name__ == "__main__":
    main()
