"""
配置加载：环境变量 > config.yaml > 默认值。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import yaml


@dataclass
class Config:
    # 飞书
    feishu_app_id: str = ""
    feishu_app_secret: str = ""

    # Model Proxy
    mp_url: str = "http://localhost:8000"
    mp_api_key: str = ""

    # 节点标识
    node_id: str = ""

    # Gateway 通信
    chat_id: str = ""
    open_id: str = ""

    # 心跳
    heartbeat_interval_s: int = 30

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "Config":
        data: dict = {}
        if config_path and os.path.exists(config_path):
            with open(config_path) as f:
                raw = yaml.safe_load(f) or {}
            feishu = raw.get("feishu", {})
            mp = raw.get("mp", {})
            data = {
                "feishu_app_id": feishu.get("app_id", ""),
                "feishu_app_secret": feishu.get("app_secret", ""),
                "mp_url": mp.get("url", ""),
                "mp_api_key": mp.get("api_key", ""),
                "node_id": raw.get("node_id", ""),
                "chat_id": raw.get("chat_id", ""),
                "open_id": raw.get("open_id", ""),
                "heartbeat_interval_s": raw.get("heartbeat_interval_s", 30),
            }

        cfg = cls(
            feishu_app_id=os.getenv("FEISHU_APP_ID", data.get("feishu_app_id", "")),
            feishu_app_secret=os.getenv("FEISHU_APP_SECRET", data.get("feishu_app_secret", "")),
            mp_url=os.getenv("MP_URL", data.get("mp_url", "http://localhost:8000")),
            mp_api_key=os.getenv("MP_API_KEY", data.get("mp_api_key", "")),
            node_id=os.getenv("NODE_ID", data.get("node_id", "")),
            chat_id=os.getenv("CHAT_ID", data.get("chat_id", "")),
            open_id=os.getenv("BOT_OPEN_ID", data.get("open_id", "")),
            heartbeat_interval_s=int(os.getenv("HEARTBEAT_INTERVAL_S", data.get("heartbeat_interval_s", 30))),
        )

        if not cfg.node_id:
            import socket
            cfg.node_id = f"bot-{socket.gethostname()}"

        return cfg
