"""
Bot 路由池：维护多个 bot 节点，round-robin 负载均衡。

所有 Gateway ↔ Bot 通信通过飞书消息完成：
  - Gateway → Bot：飞书 REST API 发消息（下行）
  - Bot → Gateway：轮询飞书消息收回复（上行）

心跳仅作为参考信息（版本、负载等），不影响路由判断。
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
    load: float = 0.0
    models: List[str] = field(default_factory=list)
    started_at: str = ""
    last_request_at: str = ""
    first_seen_at: str = ""
    request_count: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0


class BotPool:
    """
    多 bot 路由池。

    - select(): round-robin 选一个 bot
    - record_request(): 每次请求时记录时间
    - send_to_bot(): 给指定 bot 发飞书消息
    - poll_reply(): 按 req_id 轮询 bot 回复
    """

    def __init__(self, file_path: Optional[str] = None):
        self._file = file_path or os.path.join(config.STATE_DIR, "bot_pool.json")
        self._nodes: Dict[str, BotNode] = {}
        self._lock = threading.Lock()
        self._rr = itertools.cycle([])
        self._rr_dirty = True
        self._load()
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
            rec.pop("status", None)
            rec.pop("heartbeats_count", None)
            rec.pop("last_heartbeat_at", None)
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
        """兼容旧配置：BOT_OPEN_ID + CHAT_ID 存在时注册为 legacy 节点。"""
        if not config.BOT_OPEN_ID or not config.CHAT_ID:
            return
        legacy_id = "legacy-default"
        with self._lock:
            if legacy_id in self._nodes:
                return
            self._nodes[legacy_id] = BotNode(
                node_id=legacy_id,
                open_id=config.BOT_OPEN_ID,
                chat_id=config.CHAT_ID,
                first_seen_at=_now_iso(),
            )
            self._rr_dirty = True
            self._save()
            logger.info("注册 legacy bot: open_id=%s chat_id=%s", config.BOT_OPEN_ID, config.CHAT_ID)

    # ---- 节点管理 ---------------------------------------------------------------

    def register(self, payload: Dict[str, Any]) -> BotNode:
        """注册或更新一个 bot 节点（从飞书心跳消息解析）。"""
        node_id = payload.get("node_id")
        if not node_id:
            raise ValueError("missing node_id")

        with self._lock:
            existing = self._nodes.get(node_id)
            if existing:
                existing.version = payload.get("version") or existing.version
                existing.hostname = payload.get("hostname") or existing.hostname
                existing.ip = payload.get("ip") or existing.ip
                existing.open_id = payload.get("open_id") or existing.open_id
                existing.chat_id = payload.get("chat_id") or existing.chat_id
                existing.load = payload.get("load", existing.load)
                existing.models = payload.get("models") or existing.models
                existing.started_at = payload.get("started_at") or existing.started_at
                node = existing
            else:
                node = BotNode(
                    node_id=node_id,
                    open_id=payload.get("open_id", ""),
                    chat_id=payload.get("chat_id", ""),
                    version=payload.get("version", ""),
                    hostname=payload.get("hostname", ""),
                    ip=payload.get("ip", ""),
                    load=payload.get("load", 0.0),
                    models=payload.get("models", []),
                    first_seen_at=_now_iso(),
                )
                self._nodes[node_id] = node
                logger.info("新 bot 注册: %s (open_id=%s)", node_id, node.open_id)
            self._rr_dirty = True
            self._save()
            return node

    def record_request(self, node: BotNode):
        """每次请求时记录。"""
        with self._lock:
            node.last_request_at = _now_iso()
            node.request_count += 1

    def record_usage(self, node: BotNode, prompt_tokens: int, completion_tokens: int):
        """请求成功后记录 token 用量。"""
        with self._lock:
            node.total_prompt_tokens += prompt_tokens
            node.total_completion_tokens += completion_tokens
            node.total_tokens += prompt_tokens + completion_tokens
            self._save()

    # ---- 路由选择 --------------------------------------------------------------

    def _rebuild_rr(self):
        available = [n for n in self._nodes.values() if n.chat_id]
        self._rr = itertools.cycle(available) if available else itertools.cycle([])
        self._rr_dirty = False

    def select(self) -> Optional[BotNode]:
        """Round-robin 选一个 bot。无可用节点返回 None。"""
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
            key=lambda n: n.last_request_at or n.first_seen_at,
            reverse=True,
        )

    @property
    def count(self) -> int:
        return len(self._nodes)

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
        """轮询指定 bot 会话，按 req_id 匹配响应。"""
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

                # 心跳不受时间过滤，随时更新节点信息
                if parsed.get("_relay_v") == 2 and parsed.get("type") == "heartbeat":
                    self.register(parsed)
                    continue

                # 响应消息只看请求之后的
                if ct <= after_ms:
                    continue

                if parsed.get("req_id") == req_id:
                    return parsed

            await asyncio.sleep(interval)
        return None

    async def send_ctrl(self, node: BotNode, action: str, **kwargs) -> dict:
        """给指定 bot 发管控指令（通过飞书消息）。"""
        payload = {
            "_relay_v": 2,
            "type": "ctrl",
            "action": action,
            **kwargs,
        }
        return await self.send_to_bot(node, json.dumps(payload, ensure_ascii=False))

    async def broadcast_ctrl(self, action: str, **kwargs) -> List[str]:
        """给所有 bot 广播管控指令。"""
        sent = []
        for node in self.list_all():
            if not node.open_id or not node.chat_id:
                continue
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
                    line_parts.append(seg.get("text", ""))
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
