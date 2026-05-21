"""
节点注册表（中心端）：节点 = 一个跑着 feishu-relay-bot 的机器。

每个节点会定期 POST /agent/heartbeat 上报状态，中心维护这张表，
admin 端 + dashboard 可以看到「谁部署了脚本、在不在线、各自能力清单」。

存储：JSON 文件持久化 + 内存缓存。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import config

logger = logging.getLogger("nodes")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class NodeRecord:
    """单个节点的状态快照。"""
    node_id: str
    version: str = ""
    hostname: str = ""
    ip: str = ""
    started_at: str = ""
    last_heartbeat_at: str = ""
    status: str = "online"            # online | offline | stale
    bots: List[Dict[str, Any]] = field(default_factory=list)
    models: List[str] = field(default_factory=list)
    upstream: Dict[str, Any] = field(default_factory=dict)
    stats: Dict[str, Any] = field(default_factory=dict)
    # 中心维护字段
    first_seen_at: str = ""
    heartbeats_count: int = 0


class NodeRegistry:
    """所有节点的状态。线程安全，JSON 持久化。"""

    def __init__(self, file_path: Optional[str] = None):
        self._file = file_path or os.path.join(config.STATE_DIR, "nodes.json")
        self._nodes: Dict[str, NodeRecord] = {}
        self._lock = threading.Lock()
        self._load()

    # ---- 持久化 -------------------------------------------------------------

    def _load(self):
        if not os.path.exists(self._file):
            logger.info("nodes 文件不存在，从空开始: %s", self._file)
            return
        try:
            with open(self._file) as f:
                data = json.load(f)
        except Exception as e:
            logger.warning("读取 nodes 失败: %s", e)
            return
        for nid, rec in data.get("nodes", {}).items():
            self._nodes[nid] = NodeRecord(**rec)
        logger.info("加载 %d 个节点记录", len(self._nodes))

    def _save(self):
        os.makedirs(os.path.dirname(self._file), exist_ok=True)
        with open(self._file, "w") as f:
            json.dump(
                {"nodes": {k: asdict(v) for k, v in self._nodes.items()}},
                f, indent=2, ensure_ascii=False,
            )

    # ---- 增删改查 -----------------------------------------------------------

    def upsert(self, payload: Dict[str, Any]) -> NodeRecord:
        """节点心跳上报：插入或更新。"""
        node_id = payload.get("node_id")
        if not node_id:
            raise ValueError("missing node_id in heartbeat")

        with self._lock:
            existing = self._nodes.get(node_id)
            now = _now_iso()
            if existing:
                existing.version = payload.get("version", existing.version)
                existing.hostname = payload.get("hostname", existing.hostname)
                existing.ip = payload.get("ip", existing.ip)
                existing.started_at = payload.get("started_at", existing.started_at)
                existing.last_heartbeat_at = now
                existing.status = payload.get("status", "online")
                existing.bots = payload.get("bots", [])
                existing.models = payload.get("models", [])
                existing.upstream = payload.get("upstream", {})
                existing.stats = payload.get("stats", {})
                existing.heartbeats_count += 1
                rec = existing
            else:
                rec = NodeRecord(
                    node_id=node_id,
                    version=payload.get("version", ""),
                    hostname=payload.get("hostname", ""),
                    ip=payload.get("ip", ""),
                    started_at=payload.get("started_at", ""),
                    last_heartbeat_at=now,
                    status=payload.get("status", "online"),
                    bots=payload.get("bots", []),
                    models=payload.get("models", []),
                    upstream=payload.get("upstream", {}),
                    stats=payload.get("stats", {}),
                    first_seen_at=now,
                    heartbeats_count=1,
                )
                self._nodes[node_id] = rec
                logger.info("新节点注册: %s", node_id)
            self._save()
            return rec

    def mark_offline(self, node_id: str, reason: str = "client") -> bool:
        with self._lock:
            rec = self._nodes.get(node_id)
            if not rec:
                return False
            rec.status = "offline"
            rec.last_heartbeat_at = _now_iso()
            self._save()
            logger.info("节点 %s 下线（%s）", node_id, reason)
            return True

    def gc_stale(self, stale_after_s: int = 90) -> int:
        """超过 stale_after_s 秒没心跳的节点标 offline。返回处理的节点数。"""
        n = 0
        with self._lock:
            now_t = time.time()
            for rec in self._nodes.values():
                if rec.status == "offline":
                    continue
                try:
                    last_t = datetime.fromisoformat(rec.last_heartbeat_at).timestamp()
                except Exception:
                    continue
                if now_t - last_t > stale_after_s:
                    rec.status = "stale"
                    n += 1
            if n > 0:
                self._save()
                logger.info("GC: %d 个节点标记为 stale", n)
        return n

    def get(self, node_id: str) -> Optional[NodeRecord]:
        return self._nodes.get(node_id)

    def list_all(self) -> List[NodeRecord]:
        return sorted(
            self._nodes.values(),
            key=lambda r: r.last_heartbeat_at,
            reverse=True,
        )

    def list_online(self) -> List[NodeRecord]:
        return [r for r in self.list_all() if r.status == "online"]

    @property
    def count(self) -> int:
        return len(self._nodes)


# 单例（与其他 manager 一致）
manager = NodeRegistry()
