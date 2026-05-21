"""
Bot 路由池：维护多个 bot 节点，支持负载均衡选择、心跳更新、故障摘除。

Gateway 通过飞书 REST API 给选中的 bot 发消息（下行），
通过轮询飞书消息收 bot 回复（上行），在轮询过程中顺带解析心跳。
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from . import config
from .feishu_token import TokenExpiredError, token_mgr

logger = logging.getLogger("bot-pool")

STALE_AFTER_S = 90


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class BotNode:
    """一个 bot 节点的状态。"""
    node_id: str
    open_id: str = ""
    chat_id: str = ""
    version: str = ""
    hostname: str = ""
    ip: str = ""
    status: str = "online"          # online | stale | offline
    load: float = 0.0               # 0.0-1.0
    models: List[str] = field(default_factory=list)
    started_at: str = ""
    last_heartbeat_at: str = ""
    first_seen_at: str = ""
    heartbeats_count: int = 0
    active_requests: int = 0        # 当前正在处理的请求数


class BotPool:
    """
    多 bot 路由池。

    - upsert_heartbeat(): 心跳更新节点状态
    - select(): round-robin 选一个在线 bot
    - send_to_bot(): 给指定 bot 发飞书消息
    - poll_reply(): 按 req_id 轮询 bot 回复（顺带解析心跳）
    - gc_stale(): 标记超时节点
    """

    def __init__(self, file_path: Optional[str] = None):
        self._file = file_path or os.path.join(config.STATE_DIR, "bot_pool.json")
        self._nodes: Dict[str, BotNode] = {}
        self._lock = threading.Lock()
        self._rr = itertools.cycle([])  # round-robin iterator
        self._rr_dirty = True
        self._load()
        # 兼容：如果配置了旧的单 bot，自动注册为 legacy 节点
        self._ensure_legacy_bot()

    # ---- 持久化 ---------------------------------------------------------------

    def _load(self):
        if not os.path.exists(self._file):
            return
        try:
            with open(self._file) as f:
                data = json.load(f)
        except Exception as e:
            logger.warning("读取 bot_pool 失败: %s", e)
            return
        for nid, rec in data.get("nodes", {}).items():
            rec.pop("active_requests", None)
            self._nodes[nid] = BotNode(**rec)
        self._rr_dirty = True
        logger.info("加载 %d 个 bot 节点", len(self._nodes))

    def _save(self):
        os.makedirs(os.path.dirname(self._file), exist_ok=True)
        with open(self._file, "w") as f:
            json.dump(
                {"nodes": {k: asdict(v) for k, v in self._nodes.items()}},
                f, indent=2, ensure_ascii=False,
            )

    def _ensure_legacy_bot(self):
        """兼容旧配置：如果 BOT_OPEN_ID 和 CHAT_ID 存在，注册为 legacy 节点。"""
        if not config.BOT_OPEN_ID or not config.CHAT_ID:
            return
        legacy_id = "legacy-default"
        if legacy_id in self._nodes:
            return
        with self._lock:
            self._nodes[legacy_id] = BotNode(
                node_id=legacy_id,
                open_id=config.BOT_OPEN_ID,
                chat_id=config.CHAT_ID,
                status="online",
                first_seen_at=_now_iso(),
                last_heartbeat_at=_now_iso(),
            )
            self._rr_dirty = True
            self._save()
            logger.info("注册 legacy bot: open_id=%s chat_id=%s", config.BOT_OPEN_ID, config.CHAT_ID)

    # ---- 心跳 -----------------------------------------------------------------

    def upsert_heartbeat(self, payload: Dict[str, Any]) -> BotNode:
        """Bot 心跳上报（从飞书消息解析或 HTTP 接口）。"""
        node_id = payload.get("node_id")
        if not node_id:
            raise ValueError("missing node_id in heartbeat")

        with self._lock:
            now = _now_iso()
            existing = self._nodes.get(node_id)
            if existing:
                existing.version = payload.get("version", existing.version)
                existing.hostname = payload.get("hostname", existing.hostname)
                existing.ip = payload.get("ip", existing.ip)
                existing.open_id = payload.get("open_id", existing.open_id)
                existing.chat_id = payload.get("chat_id", existing.chat_id)
                existing.started_at = payload.get("started_at", existing.started_at)
                existing.last_heartbeat_at = now
                existing.status = "online"
                existing.load = payload.get("load", 0.0)
                existing.models = payload.get("models", existing.models)
                existing.heartbeats_count += 1
                node = existing
            else:
                node = BotNode(
                    node_id=node_id,
                    open_id=payload.get("open_id", ""),
                    chat_id=payload.get("chat_id", ""),
                    version=payload.get("version", ""),
                    hostname=payload.get("hostname", ""),
                    ip=payload.get("ip", ""),
                    started_at=payload.get("started_at", ""),
                    last_heartbeat_at=now,
                    status="online",
                    load=payload.get("load", 0.0),
                    models=payload.get("models", []),
                    first_seen_at=now,
                    heartbeats_count=1,
                )
                self._nodes[node_id] = node
                logger.info("新 bot 注册: %s (open_id=%s)", node_id, node.open_id)
            self._rr_dirty = True
            self._save()
            return node

    def mark_offline(self, node_id: str, reason: str = "client") -> bool:
        with self._lock:
            node = self._nodes.get(node_id)
            if not node:
                return False
            node.status = "offline"
            node.last_heartbeat_at = _now_iso()
            self._rr_dirty = True
            self._save()
            logger.info("bot %s 下线（%s）", node_id, reason)
            return True

    def gc_stale(self, stale_after_s: int = STALE_AFTER_S) -> int:
        n = 0
        with self._lock:
            now_t = time.time()
            for node in self._nodes.values():
                if node.status != "online":
                    continue
                try:
                    last_t = datetime.fromisoformat(node.last_heartbeat_at).timestamp()
                except Exception:
                    continue
                if now_t - last_t > stale_after_s:
                    node.status = "stale"
                    n += 1
            if n > 0:
                self._rr_dirty = True
                self._save()
                logger.info("GC: %d 个 bot 标记为 stale", n)
        return n

    # ---- 路由选择 --------------------------------------------------------------

    def _rebuild_rr(self):
        online = [n for n in self._nodes.values()
                  if n.status == "online" and n.open_id and n.chat_id]
        self._rr = itertools.cycle(online) if online else itertools.cycle([])
        self._rr_dirty = False

    def select(self) -> Optional[BotNode]:
        """Round-robin 选一个在线 bot。无可用节点返回 None。"""
        with self._lock:
            if self._rr_dirty:
                self._rebuild_rr()
            try:
                return next(self._rr)
            except StopIteration:
                return None

    def get(self, node_id: str) -> Optional[BotNode]:
        return self._nodes.get(node_id)

    def list_all(self) -> List[BotNode]:
        return sorted(
            self._nodes.values(),
            key=lambda n: n.last_heartbeat_at,
            reverse=True,
        )

    def list_online(self) -> List[BotNode]:
        return [n for n in self.list_all() if n.status == "online"]

    @property
    def count(self) -> int:
        return len(self._nodes)

    @property
    def online_count(self) -> int:
        return sum(1 for n in self._nodes.values() if n.status == "online")

    # ---- 飞书消息收发 ----------------------------------------------------------

    async def send_to_bot(self, node: BotNode, text: str) -> dict:
        """通过飞书 REST API 给指定 bot 发消息。"""
        async with httpx.AsyncClient(timeout=15) as cli:
            r = await cli.post(
                f"{config.FEISHU_BASE}/open-apis/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers=token_mgr.auth_header(),
                json={
                    "receive_id": node.chat_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                },
            )
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"发消息失败: code={d.get('code')} msg={d.get('msg')}")
        return d["data"]

    async def poll_reply_by_req_id(
        self,
        node: BotNode,
        req_id: str,
        after_ms: int,
        timeout_s: Optional[int] = None,
    ) -> Optional[dict]:
        """轮询指定 bot 会话，按 req_id 匹配响应。顺带解析心跳消息。"""
        timeout_s = timeout_s or config.POLL_TIMEOUT_S
        deadline = time.time() + timeout_s
        interval = config.POLL_INTERVAL_S
        seen: set = set()

        while time.time() < deadline:
            async with httpx.AsyncClient(timeout=15) as cli:
                r = await cli.get(
                    f"{config.FEISHU_BASE}/open-apis/im/v1/messages",
                    params={
                        "container_id_type": "chat",
                        "container_id": node.chat_id,
                        "sort_type": "ByCreateTimeDesc",
                        "page_size": 10,
                    },
                    headers=token_mgr.auth_header(),
                )
            d = r.json()
            if d.get("code") != 0:
                logger.warning("poll err: code=%s msg=%s", d.get("code"), d.get("msg"))
                await asyncio.sleep(interval)
                continue

            for m in (d.get("data") or {}).get("items") or []:
                ct = int(m.get("create_time", "0"))
                if ct <= after_ms:
                    continue
                sender_type = (m.get("sender") or {}).get("sender_type", "")
                if sender_type != "app":
                    continue
                mid = m.get("message_id")
                if mid in seen:
                    continue
                seen.add(mid)

                text = _extract_text(m)
                try:
                    parsed = json.loads(text)
                except (ValueError, TypeError):
                    continue
                if not isinstance(parsed, dict):
                    continue

                # 顺带处理心跳
                if parsed.get("_relay_v") == 2 and parsed.get("type") == "heartbeat":
                    self.upsert_heartbeat(parsed)
                    continue

                if parsed.get("req_id") == req_id:
                    return parsed

            await asyncio.sleep(interval)
        return None

    async def send_ctrl(self, node: BotNode, action: str, **kwargs) -> dict:
        """给指定 bot 发管控指令。"""
        payload = {
            "_relay_v": 2,
            "type": "ctrl",
            "action": action,
            **kwargs,
        }
        return await self.send_to_bot(node, json.dumps(payload, ensure_ascii=False))

    async def broadcast_ctrl(self, action: str, **kwargs) -> List[str]:
        """给所有在线 bot 广播管控指令。返回成功发送的 node_id 列表。"""
        sent = []
        for node in self.list_online():
            try:
                await self.send_ctrl(node, action, **kwargs)
                sent.append(node.node_id)
            except Exception as e:
                logger.warning("给 %s 发 ctrl 失败: %s", node.node_id, e)
        return sent


def _extract_text(msg: dict) -> str:
    """从飞书消息结构提取纯文本。"""
    body = (msg.get("body") or {}).get("content", "")
    msg_type = msg.get("msg_type", "")
    try:
        parsed = json.loads(body)
    except Exception:
        return body

    if msg_type == "text":
        return parsed.get("text", body)
    elif msg_type == "post":
        parts = []
        for line in parsed.get("content", []):
            line_parts = []
            for seg in line:
                tag = seg.get("tag", "")
                if tag == "text":
                    line_parts.append(seg.get("text", ""))
                elif tag == "a":
                    line_parts.append(seg.get("text", seg.get("href", "")))
                elif tag == "code_block":
                    lang = seg.get("language", "")
                    code = seg.get("text", "")
                    line_parts.append(f"```{lang}\n{code}\n```")
            parts.append("".join(line_parts))
        return "\n".join(parts)
    elif msg_type == "interactive":
        elements = parsed.get("elements", [])
        parts = []
        _walk_card_elements(elements, parts)
        if parts:
            return "\n".join(parts)
        title = (parsed.get("header") or {}).get("title", {}).get("content", "")
        return title or body
    else:
        return body


def _walk_card_elements(elements, parts: list):
    if isinstance(elements, list):
        for el in elements:
            _walk_card_elements(el, parts)
    elif isinstance(elements, dict):
        tag = elements.get("tag", "")
        if tag in ("plain_text", "lark_md", "markdown"):
            t = elements.get("content", "") or elements.get("text", "")
            if t:
                parts.append(t)
        elif tag == "div":
            text_obj = elements.get("text") or {}
            t = text_obj.get("content", "") or text_obj.get("text", "")
            if t:
                parts.append(t)
        for key in ("elements", "columns", "body", "rows", "cells"):
            child = elements.get(key)
            if child:
                _walk_card_elements(child, parts)


pool = BotPool()
