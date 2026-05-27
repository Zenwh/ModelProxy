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

    # v3 流式参数
    stream_flush_bytes: int = 1024
    stream_flush_ms: int = 1000
    stream_send_qps: float = 4.0       # 飞书消息令牌桶速率
    multipart_timeout_s: int = 60      # 分片重组超时

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "Config":
        data: dict = {}
        if config_path and os.path.exists(config_path):
            with open(config_path) as f:
                raw = yaml.safe_load(f) or {}
            feishu = raw.get("feishu", {})
            mp = raw.get("mp", {})
            stream = raw.get("stream", {}) or {}
            data = {
                "feishu_app_id": feishu.get("app_id", ""),
                "feishu_app_secret": feishu.get("app_secret", ""),
                "mp_url": mp.get("url", ""),
                "mp_api_key": mp.get("api_key", ""),
                "node_id": raw.get("node_id", ""),
                "chat_id": raw.get("chat_id", ""),
                "open_id": raw.get("open_id", ""),
                "heartbeat_interval_s": raw.get("heartbeat_interval_s", 30),
                "stream_flush_bytes": stream.get("flush_bytes", 1024),
                "stream_flush_ms": stream.get("flush_ms", 1000),
                "stream_send_qps": stream.get("send_qps", 4.0),
                "multipart_timeout_s": raw.get("multipart_timeout_s", 60),
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
            stream_flush_bytes=int(os.getenv("STREAM_FLUSH_BYTES", data.get("stream_flush_bytes", 1024))),
            stream_flush_ms=int(os.getenv("STREAM_FLUSH_MS", data.get("stream_flush_ms", 1000))),
            stream_send_qps=float(os.getenv("STREAM_SEND_QPS", data.get("stream_send_qps", 4.0))),
            multipart_timeout_s=int(os.getenv("MULTIPART_TIMEOUT_S", data.get("multipart_timeout_s", 60))),
        )

        if not cfg.node_id:
            import socket
            cfg.node_id = f"bot-{socket.gethostname()}"

        return cfg
