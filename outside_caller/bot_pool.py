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
from .feishu_token import TokenExpiredError, token_mgr, token_pool
from .relay_codec import PayloadTooLargeError, decode as codec_decode, encode as codec_encode

logger = logging.getLogger("bot-pool")

# v0.4 heartbeat envelope text prefix —— bot 发 "OPENCLAW_HB:" + base64(json)
_HB_V3_PREFIX = "OPENCLAW_HB:"
# 防 replay 的窗口（秒）
_HB_V3_TS_WINDOW_S = 5 * 60


def _hmac_v3(claw_secret: str, claw_id: str, ts: str, nonce: str) -> str:
    """与 bot 端 heartbeat._sign_v3 完全一致；用于签名校验。"""
    import hashlib
    import hmac
    if not claw_secret:
        return ""
    msg = f"{claw_id}|{ts}|{nonce}".encode("utf-8")
    return hmac.new(
        claw_secret.encode("utf-8"), msg, hashlib.sha256,
    ).hexdigest()


def _parse_hb_v3_envelope(text: str) -> Optional[dict]:
    """解析 OPENCLAW_HB:<base64> 消息为 dict envelope；非心跳格式返回 None。"""
    import base64
    if not text or not text.startswith(_HB_V3_PREFIX):
        return None
    b64 = text[len(_HB_V3_PREFIX):].strip()
    try:
        raw = base64.b64decode(b64, validate=True)
        env = json.loads(raw.decode("utf-8"))
    except Exception as e:
        logger.debug("parse v3 envelope failed: %s", e)
        return None
    if not isinstance(env, dict):
        return None
    if env.get("_relay_v") != 3 or env.get("type") != "heartbeat":
        return None
    return env


# Module-level shared AsyncClient — httpx clients are safe for concurrent
# use within a single event loop and avoid TCP handshake churn per request.
_http_client: Optional[httpx.AsyncClient] = None


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=15)
    return _http_client


async def close_http_client() -> None:
    """Best-effort close on process shutdown."""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        try:
            await _http_client.aclose()
        except Exception:
            pass
        finally:
            _http_client = None


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _auth_header_for(node: "BotNode") -> dict:
    """根据节点选 token 源（v0.4 cluster upgrade §3.6.2 + Task #50 hotfix）：

    20260526 hotfix (Task #50): 之前 per-app tenant_access_token 路径让 gateway 用
    bot 自身 app 的 token 发消息，sender_id == bot 自身 app_id → 飞书反回环抑制吞掉
    p2_im_message_receive_v1 事件，bot WS 永远收不到请求。

    现在强制优先 user_access_token (token_mgr) — sender_type=user，bot 端正常触发事件。
    仅在 token_mgr 无 token 且节点有 app_id 时退回 tenant token（不可工作但保持向后兼容）。
    多 slot / 多 user OAuth 场景待 v0.5 重新设计 token 路由。

    20260528 xpage 多 app 补丁:
    当 bot 节点持有自己独立的 app_id（与 gateway 的 APP_ID 不同），用该 app 的 tenant
    token 发送 — 因为 sender app_id != bot app_id，不会触发反回环抑制，bot 能正常收到事件。
    这是 v3 多 worker bot 共存（per-bot 独立 app_id）的必备路由。
    """
    # xpage fix: 节点有自己的 app_id 时优先用该 app 的 tenant token
    if node.app_id and node.app_id != config.APP_ID:
        try:
            return token_pool.auth_header(node.app_id)
        except Exception:
            pass
    # 优先 user OAuth
    if getattr(token_mgr, "_token", None):
        return token_mgr.auth_header()
    if node.app_id:
        return token_pool.auth_header(node.app_id)
    return token_mgr.auth_header()


@dataclass
class BotNode:
    """一个 bot 节点的状态。

    v0.4 (cluster upgrade) 新增字段（详见 arch-cluster-upgrade.md §3.1）：
    - enabled       admin 软开关；false 时不参与 select() 路由，但记录保留
    - cluster_id    集群标签，伪装为 OpenClaw cluster。老节点默认 "legacy"
    - app_id        节点绑定的飞书 app_id（v0.4 之前由 gateway 全局共用 token，
                    现在每个节点独立 app + token；老节点保持空，走 legacy 路径）
    - slot_id       SlotPool 里对应的 slot 标识（v0.4 bootstrap 时写入）
    - agent_secret  bootstrap 返回的 claw_secret，用于 HMAC 校验心跳 v3 envelope。
                    runtime 内存常驻，落 JSON 也仅本机可读（保护参考 §3.3.5）
    """
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
    capabilities: List[str] = field(default_factory=list)
    # ---- v0.4 cluster upgrade fields ----
    enabled: bool = True
    cluster_id: str = "legacy"
    app_id: str = ""
    slot_id: str = ""
    agent_secret: str = ""


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
        # 已知字段集合（防止旧版本字段 / 未来回滚字段炸 BotNode(**rec)）
        known = {f.name for f in BotNode.__dataclass_fields__.values()}
        skipped = 0
        for nid, rec in data.get("nodes", {}).items():
            # 黑名单节点不加载（清掉历史遗留的僵尸记录）
            if nid in config.HEARTBEAT_NODE_BLOCKLIST:
                skipped += 1
                continue
            # 历史遗留字段清理
            for legacy in ("active_requests", "status", "heartbeats_count", "last_heartbeat_at"):
                rec.pop(legacy, None)
            # 过滤掉所有不认识的 key（向后兼容）
            rec = {k: v for k, v in rec.items() if k in known}
            self._nodes[nid] = BotNode(**rec)
        self._rr_dirty = True
        if skipped:
            logger.info("加载 %d 个 bot 节点（黑名单跳过 %d 个）", len(self._nodes), skipped)
            self._save()  # 持久化掉被跳过的僵尸，避免下次又读到
        else:
            logger.info("加载 %d 个 bot 节点", len(self._nodes))

    def _save(self):
        os.makedirs(os.path.dirname(self._file), exist_ok=True)
        config.atomic_write_json(self._file, {
            "nodes": {k: asdict(v) for k, v in self._nodes.items()},
        })

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
                cluster_id="legacy",
                # legacy 节点没有 app_id（用 gateway 全局 token），保持 ""
            )
            self._rr_dirty = True
            self._save()
            logger.info("注册 legacy bot: open_id=%s chat_id=%s", config.BOT_OPEN_ID, config.CHAT_ID)

    # ---- 节点管理 ---------------------------------------------------------------

    def register(self, payload: Dict[str, Any]) -> BotNode:
        """注册或更新一个 bot 节点（从飞书心跳消息解析）。

        兼容 v2 (legacy HTTP / IM 心跳) 和 v3 (OpenClaw IM envelope) 两种 payload：
        - v2 字段：node_id, version, hostname, ip, open_id, chat_id, load, models, started_at, capabilities
        - v3 额外字段：cluster_id, app_id, slot_id, agent_secret (由 _poll_node_heartbeat 解析后传入)

        优化：仅当节点的 round-robin 资格集（chat_id / enabled / relay_v3 caps）实际
        发生变化时才置 _rr_dirty=True，避免每次心跳都重置 itertools.cycle 的位置
        导致路由偏向第一个节点。
        """
        node_id = payload.get("node_id")
        if not node_id:
            raise ValueError("missing node_id")

        # 黑名单丢弃：返回一个未入池的临时 BotNode 让上游不至于 NameError，
        # 但绝不调用 self._save / _rr_dirty / _nodes[node_id]= 任何写池操作。
        if node_id in config.HEARTBEAT_NODE_BLOCKLIST:
            logger.info("blocklist drop heartbeat from node_id=%s", node_id)
            return BotNode(
                node_id=node_id,
                open_id=payload.get("open_id", ""),
                chat_id=payload.get("chat_id", ""),
                version=payload.get("version", ""),
                enabled=False,
            )

        def _eligibility(n: "BotNode") -> tuple:
            return (
                bool(n.chat_id),
                bool(n.enabled),
                "relay_v3" in (n.capabilities or []),
            )

        with self._lock:
            existing = self._nodes.get(node_id)
            if existing:
                before_elig = _eligibility(existing)
                existing.version = payload.get("version") or existing.version
                existing.hostname = payload.get("hostname") or existing.hostname
                existing.ip = payload.get("ip") or existing.ip
                existing.open_id = payload.get("open_id") or existing.open_id
                existing.chat_id = payload.get("chat_id") or existing.chat_id
                existing.load = payload.get("load", existing.load)
                existing.models = payload.get("models") or existing.models
                existing.started_at = payload.get("started_at") or existing.started_at
                existing.capabilities = payload.get("capabilities") or existing.capabilities
                # v0.4 新增字段：心跳带就更新，不带保持现状（admin 改过的 enabled 不被覆盖）
                if payload.get("cluster_id"):
                    existing.cluster_id = payload["cluster_id"]
                if payload.get("app_id"):
                    existing.app_id = payload["app_id"]
                if payload.get("slot_id"):
                    existing.slot_id = payload["slot_id"]
                if payload.get("agent_secret"):
                    existing.agent_secret = payload["agent_secret"]
                node = existing
                # 只有 eligibility 变化才 invalidate cycle
                if _eligibility(existing) != before_elig:
                    self._rr_dirty = True
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
                    cluster_id=payload.get("cluster_id") or "legacy",
                    app_id=payload.get("app_id", ""),
                    slot_id=payload.get("slot_id", ""),
                    agent_secret=payload.get("agent_secret", ""),
                )
                self._nodes[node_id] = node
                self._rr_dirty = True
                logger.info(
                    "新 bot 注册: %s (cluster=%s app_id=%s open_id=%s)",
                    node_id, node.cluster_id, node.app_id or "-", node.open_id,
                )
            self._save()
            return node

    def set_enabled(self, node_id: str, enabled: bool) -> bool:
        """admin 设置节点 enabled 开关。返回是否找到节点。

        disable 时只是从 round-robin 取消，节点和 slot 都不删除；
        re-enable 直接切回。decommission 才会真的 remove()。
        """
        with self._lock:
            node = self._nodes.get(node_id)
            if not node:
                return False
            if node.enabled == enabled:
                return True
            node.enabled = enabled
            self._rr_dirty = True
            self._save()
            logger.info("set_enabled: node=%s enabled=%s", node_id, enabled)
            return True

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
        # 只让 enabled、有 chat_id 且声明 relay_v3 capability 的节点参与 round-robin
        # 老节点（capabilities 缺 relay_v3）→ 自动跳过 + 一次性告警
        available: List[BotNode] = []
        for n in self._nodes.values():
            if not (n.chat_id and n.enabled):
                continue
            caps = n.capabilities or []
            # legacy 节点（首次注册没收到心跳）暂时放行；后续心跳到来再校验
            if caps and "relay_v3" not in caps:
                if not getattr(n, "_warned_no_v3", False):
                    logger.warning(
                        "node=%s cluster=%s capabilities=%s 缺 relay_v3，已从路由排除（请升级 worker）",
                        n.node_id, n.cluster_id, caps,
                    )
                    try:
                        setattr(n, "_warned_no_v3", True)
                    except Exception:
                        pass
                continue
            available.append(n)
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

    def remove(self, node_id: str) -> None:
        """移除一个 bot 节点（下线）。也移除其子节点。"""
        with self._lock:
            removed = node_id in self._nodes
            self._nodes.pop(node_id, None)
            prefix = f"{node_id}/"
            to_del = [k for k in self._nodes if k.startswith(prefix)]
            for k in to_del:
                del self._nodes[k]
            if removed or to_del:
                self._rr_dirty = True
                self._save()
                logger.info("节点下线: %s (+%d 子节点)", node_id, len(to_del))

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

    async def send_to_bot(self, node: BotNode, payload, *, allow_compress: bool = True) -> dict:
        """通过飞书 REST API 给指定 bot 发消息。payload 可以是 dict 或已编码 str。"""
        if isinstance(payload, dict):
            can_compress = allow_compress and ("zlib" in (node.capabilities or []))
            text = codec_encode(payload, allow_compress=can_compress)
        else:
            text = payload
        cli = get_http_client()
        r = await cli.post(
            f"{config.FEISHU_BASE}/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers=_auth_header_for(node),
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

    async def send_request(
        self,
        node: BotNode,
        payload: Dict[str, Any],
    ) -> int:
        """
        v3 上行：把 payload dict 序列化后按 MULTIPART_CHUNK_BYTES 切片，串行发到同一 chat。

        - payload 必须含 req_id / endpoint / model / stream / messages 等字段
        - 所有 part 都送给 *同一个* node（分片亲和）
        - 当节点声明 zlib 能力且 payload 较大时，先 zlib+base64 压缩整个 body，
          再按 chunk 切片；worker 端 assembler 检测到 payload_encoding=zb64 后
          自动还原。这能把高度重复内容（如长对话）压缩 100x+，极大降低 part 数。
        - 返回 part_total，调用方用日志打印用
        """
        import base64
        import zlib

        req_id = payload.get("req_id") or ""
        if not req_id:
            raise ValueError("payload missing req_id")

        endpoint = payload.get("endpoint") or "chat"
        mode = payload.get("mode")
        stream = bool(payload.get("stream", False))
        model = payload.get("model", "")

        body_str = json.dumps(payload, ensure_ascii=False)
        raw_bytes = body_str.encode("utf-8")

        # 压缩：节点支持 zlib + 原始 body > COMPRESS_THRESHOLD 时启用
        can_compress = "zlib" in (node.capabilities or [])
        encoding = "plain"
        if can_compress and len(raw_bytes) > 50_000:
            compressed = zlib.compress(raw_bytes, level=6)
            b64 = base64.b64encode(compressed).decode("ascii")
            body_bytes = b64.encode("ascii")
            encoding = "zb64"
            logger.debug(
                "send_request: compressed %d → %d bytes (%.1fx) req_id=%s",
                len(raw_bytes), len(body_bytes),
                len(raw_bytes) / max(len(body_bytes), 1), req_id,
            )
        else:
            body_bytes = raw_bytes

        chunk_size = max(1024, config.MULTIPART_CHUNK_BYTES)
        total = max(1, (len(body_bytes) + chunk_size - 1) // chunk_size)

        # 速率控制：多 part 时按 MULTIPART_SEND_QPS 间隔串行发送，避免飞书 msg API
        # 5 QPS 上限触发限流或 lark WS frontier 丢消息（实测 30+ parts 无 pacing 会丢）
        send_qps = max(0.5, getattr(config, "MULTIPART_SEND_QPS", 4.0))
        min_interval = 1.0 / send_qps
        last_send = 0.0

        for idx in range(total):
            start = idx * chunk_size
            end = start + chunk_size
            chunk = body_bytes[start:end].decode("utf-8", errors="ignore")
            envelope: Dict[str, Any] = {
                "_relay_v": 3,
                "type": "req_part",
                "req_id": req_id,
                "part_index": idx,
                "part_total": total,
                "payload_chunk": chunk,
            }
            if idx == 0:
                envelope["endpoint"] = endpoint
                envelope["mode"] = mode
                envelope["stream"] = stream
                envelope["model"] = model
                envelope["payload_encoding"] = encoding
            # 分片消息本身不再压缩（已切片，单条肯定 < FEISHU_LIMIT）
            # 节流：保证两次 send 之间至少 min_interval 秒
            if idx > 0:
                gap = time.time() - last_send
                if gap < min_interval:
                    await asyncio.sleep(min_interval - gap)
            await self.send_to_bot(node, envelope, allow_compress=False)
            last_send = time.time()
        return total

    async def poll_reply_by_req_id(
        self,
        node: BotNode,
        req_id: str,
        after_ms: int,
        timeout_s: Optional[int] = None,
    ) -> Optional[dict]:
        """轮询指定 bot 会话，按 req_id 匹配 v3 resp。

        为防并发高时消息被挤到第二页，page_size 加大到 50。
        成功消费后后台删除该消息，避免聊天窗口永久堆积。
        """
        timeout_s = timeout_s or config.POLL_TIMEOUT_S
        deadline = time.time() + timeout_s
        interval = config.POLL_INTERVAL_S
        seen: set = set()

        while time.time() < deadline:
            cli = get_http_client()
            r = await cli.get(
                f"{config.FEISHU_BASE}/open-apis/im/v1/messages",
                params={
                    "container_id_type": "chat",
                    "container_id": node.chat_id,
                    "sort_type": "ByCreateTimeDesc",
                    "page_size": 50,
                },
                headers=_auth_header_for(node),
            )
            d = r.json()
            if d.get("code") != 0:
                logger.warning("poll err: code=%s msg=%s", d.get("code"), d.get("msg"))
                await asyncio.sleep(max(interval, 1.5))
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
                    parsed = codec_decode(text)
                except (ValueError, TypeError, Exception):
                    continue
                if not isinstance(parsed, dict):
                    continue

                # 心跳不受时间过滤
                if parsed.get("type") == "heartbeat":
                    self.register(parsed)
                    continue

                if ct <= after_ms:
                    continue

                if parsed.get("req_id") != req_id:
                    continue

                # v3 resp 终结即返回；流式增量 stream_chunk 在此函数里忽略（poll_stream 处理）
                if parsed.get("_relay_v") == 3 and parsed.get("type") == "resp":
                    # 后台删除已消费消息，防止堆积拖慢后续轮询
                    asyncio.create_task(_try_delete_message(node, mid))
                    return parsed

            await asyncio.sleep(interval)
        return None

    async def poll_stream(
        self,
        node: BotNode,
        req_id: str,
        after_ms: int,
        timeout_s: Optional[int] = None,
    ):
        """
        v3 流式：async generator，按 seq 顺序 yield stream_chunk，遇 resp 后再 yield resp 并 break。

        - 每 POLL_INTERVAL_S 拉最近 30 条消息；按 message_id 去重；按 (req_id, seq) 去重 + 排序
        - 网络/限流错误退避到 1.5s
        - 整个超时受 POLL_TIMEOUT_S 控制
        """
        timeout_s = timeout_s or config.POLL_TIMEOUT_S
        deadline = time.time() + timeout_s
        interval = config.POLL_INTERVAL_S
        seen_msg_ids: set = set()
        seen_seqs: set = set()
        next_seq = 0
        pending: Dict[int, dict] = {}
        finished = False

        while time.time() < deadline and not finished:
            try:
                async with httpx.AsyncClient(timeout=15) as cli:
                    r = await cli.get(
                        f"{config.FEISHU_BASE}/open-apis/im/v1/messages",
                        params={
                            "container_id_type": "chat",
                            "container_id": node.chat_id,
                            "sort_type": "ByCreateTimeDesc",
                            "page_size": 30,
                        },
                        headers=_auth_header_for(node),
                    )
                d = r.json()
            except Exception as e:
                logger.warning("poll_stream net err: %s", e)
                await asyncio.sleep(1.5)
                continue

            if d.get("code") != 0:
                logger.warning("poll_stream err: code=%s msg=%s", d.get("code"), d.get("msg"))
                await asyncio.sleep(1.5)
                continue

            # 飞书返回的是降序，按 create_time 升序处理避免乱序
            items = list((d.get("data") or {}).get("items") or [])
            items.sort(key=lambda x: int(x.get("create_time", "0")))

            for m in items:
                ct = int(m.get("create_time", "0"))
                sender_type = (m.get("sender") or {}).get("sender_type", "")
                if sender_type != "app":
                    continue
                mid = m.get("message_id")
                if mid in seen_msg_ids:
                    continue
                seen_msg_ids.add(mid)

                text = _extract_text(m)
                try:
                    parsed = codec_decode(text)
                except Exception:
                    continue
                if not isinstance(parsed, dict):
                    continue

                if parsed.get("type") == "heartbeat":
                    self.register(parsed)
                    continue

                if ct <= after_ms:
                    continue
                if parsed.get("_relay_v") != 3:
                    continue
                if parsed.get("req_id") != req_id:
                    continue

                ptype = parsed.get("type")
                if ptype == "stream_chunk":
                    seq = parsed.get("seq")
                    if not isinstance(seq, int) or seq in seen_seqs:
                        continue
                    seen_seqs.add(seq)
                    pending[seq] = parsed
                elif ptype == "resp":
                    pending["__resp__"] = parsed
                    finished = True
                else:
                    continue

            # yield 已就绪的 chunk（按 seq 顺序）
            while next_seq in pending:
                yield pending.pop(next_seq)
                next_seq += 1

            if finished and "__resp__" in pending:
                # 把剩余乱序到达的 chunk 按 seq 排序 yield 完
                leftover = sorted(k for k in pending.keys() if isinstance(k, int))
                for s in leftover:
                    yield pending.pop(s)
                yield pending.pop("__resp__")
                return

            if not finished:
                await asyncio.sleep(interval)

        # 超时
        return

    async def send_ctrl(self, node: BotNode, action: str, **kwargs) -> dict:
        """给指定 bot 发管控指令（通过飞书消息）。"""
        payload = {
            "_relay_v": 3,
            "type": "ctrl",
            "action": action,
            **kwargs,
        }
        return await self.send_to_bot(node, payload, allow_compress=False)

    async def broadcast_ctrl(self, action: str, **kwargs) -> List[str]:
        """给所有 bot 广播管控指令。只要有 chat_id 就能发；open_id 是历史字段，HTTP 上报路径不带。"""
        sent = []
        for node in self.list_all():
            if not node.chat_id:
                continue
            try:
                await self.send_ctrl(node, action, **kwargs)
                sent.append(node.node_id)
            except Exception as e:
                logger.warning("给 %s 发 ctrl 失败: %s", node.node_id, e)
        return sent

    # ---- 后台心跳轮询 ----------------------------------------------------------

    def start_heartbeat_poller(self, interval_s: int = 30):
        """启动后台协程，定时扫描所有 bot chat 的心跳消息。"""
        self._hb_poll_interval = interval_s
        self._hb_poll_task: Optional[asyncio.Task] = None

    async def run_heartbeat_poller(self):
        """后台轮询协程：每隔 interval_s 扫描所有节点的 chat 获取心跳。"""
        interval = getattr(self, "_hb_poll_interval", 30)
        logger.info("heartbeat poller started, interval=%ds", interval)
        while True:
            try:
                await self._poll_all_heartbeats()
            except Exception as e:
                logger.warning("heartbeat poll error: %s", e)
            await asyncio.sleep(interval)

    async def _poll_all_heartbeats(self):
        """扫描所有有 chat_id 的节点，查找心跳消息。"""
        nodes = [n for n in self.list_all() if n.chat_id]
        for node in nodes:
            try:
                await self._poll_node_heartbeat(node)
            except TokenExpiredError:
                logger.warning("token expired during heartbeat poll")
                break
            except Exception as e:
                logger.debug("poll heartbeat for %s failed: %s", node.node_id, e)

    async def _poll_node_heartbeat(self, node: BotNode):
        """扫描单个节点的 chat，解析心跳消息（兼容 v2 codec + v3 OPENCLAW_HB）。

        同一 chat 可能承载多个 worker（多 app / 多 slot），消息列表里既会有
        OPENCLAW_HB envelope 心跳，也会有 plain codec 心跳。这里遍历整页
        消息，按 node_id 去重，每个不同的 node_id 只注册其最新一条心跳，
        避免因为某个 worker 的消息更靠前而吃掉另一个 worker 的心跳。
        """
        cli = get_http_client()
        r = await cli.get(
            f"{config.FEISHU_BASE}/open-apis/im/v1/messages",
            params={
                "container_id_type": "chat",
                "container_id": node.chat_id,
                "sort_type": "ByCreateTimeDesc",
                "page_size": 10,
            },
            headers=_auth_header_for(node),
        )
        d = r.json()
        if d.get("code") != 0:
            return

        seen_nodes: set = set()
        for m in (d.get("data") or {}).get("items") or []:
            sender_type = (m.get("sender") or {}).get("sender_type", "")
            if sender_type != "app":
                continue
            text = _extract_text(m)

            # ---- v3 OPENCLAW_HB envelope（IM transport, §3.6）----
            env = _parse_hb_v3_envelope(text)
            if env is not None:
                # OPENCLAW envelope 仅当 claw.id 与 poll-target node 完全匹配时
                # 才校验通过；多 app 共享 chat 时它会过滤掉非自己的心跳，所以
                # 不能用它来识别 plain-codec 心跳所属的 node。
                payload = self._validate_and_map_v3(env, node, m)
                if payload is not None:
                    nid = payload.get("node_id")
                    if nid and nid not in seen_nodes:
                        seen_nodes.add(nid)
                        self.register(payload)
                continue

            # ---- 兼容：v2/v3 plain codec heartbeat（非 OPENCLAW_HB 签名包）----
            try:
                parsed = codec_decode(text)
            except Exception:
                continue
            if not isinstance(parsed, dict):
                continue
            if parsed.get("type") != "heartbeat":
                continue
            if parsed.get("_relay_v") not in (2, 3):
                continue
            nid = parsed.get("node_id")
            if not nid or nid in seen_nodes:
                continue
            seen_nodes.add(nid)
            self.register(parsed)

    def _validate_and_map_v3(
        self,
        env: dict,
        node: "BotNode",
        msg: dict,
    ) -> Optional[dict]:
        """v3 envelope 校验 + 转 register() 用的 payload。

        校验项（§3.6 步骤 1-4）：
        1. signature 用已注册的 claw_secret 重算匹配
        2. ts 在 ±5min 内（防 replay）
        3. envelope.app_id 与 node 持有 slot 的 app_id 一致

        sender app_id 校验（步骤 1）暂时跳过——飞书消息 API 在该端点不直接
        返回 sender 的 app_id，要查 user/v3，开销高且 chat 内已是节点自己的 app。
        """
        import datetime as _dt

        claw = env.get("claw") or {}
        env_claw_id = claw.get("id") or ""
        if not env_claw_id:
            logger.warning("[hb-v3] envelope missing claw.id; chat=%s", node.chat_id)
            return None
        if env_claw_id != node.node_id:
            logger.warning(
                "[hb-v3] claw.id=%s does not match poll-target node=%s",
                env_claw_id, node.node_id,
            )
            return None

        # 校验 1 + 2: signature
        ts = env.get("ts") or ""
        nonce = env.get("nonce") or ""
        sig = env.get("signature") or ""
        if not node.agent_secret:
            # 老节点（legacy / bootstrap 之前）没有 agent_secret —— 兼容地放过签名校验
            # 但不更新 sensitive fields；只做基础 register 让节点出现在列表里
            logger.debug(
                "[hb-v3] node=%s has no agent_secret; accepting unsigned heartbeat",
                node.node_id,
            )
        else:
            expected = _hmac_v3(node.agent_secret, env_claw_id, ts, nonce)
            if not sig or not _const_time_eq(sig, expected):
                logger.warning(
                    "[hb-v3] bad signature for node=%s (ts=%s)", node.node_id, ts,
                )
                return None

        # 校验 3: ts 窗口
        if ts:
            try:
                t_env = _dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=_dt.timezone.utc
                )
                now = _dt.datetime.now(_dt.timezone.utc)
                delta = abs((now - t_env).total_seconds())
                if delta > _HB_V3_TS_WINDOW_S:
                    logger.warning(
                        "[hb-v3] ts skew %ds > %ds; node=%s",
                        int(delta), _HB_V3_TS_WINDOW_S, node.node_id,
                    )
                    return None
            except ValueError as e:
                logger.warning("[hb-v3] bad ts=%s: %s", ts, e)
                return None

        # 校验 4: app_id 匹配
        env_app_id = env.get("app_id") or ""
        if node.app_id and env_app_id and env_app_id != node.app_id:
            logger.warning(
                "[hb-v3] app_id mismatch envelope=%s node=%s",
                env_app_id, node.app_id,
            )
            return None

        # 把 v3 envelope 映射成 register() 认识的扁平 payload
        # （capabilities → models，cluster → cluster_id，保留内部 BotNode 语义不变）
        return {
            "node_id": env_claw_id,
            "version": claw.get("version", ""),
            "hostname": env.get("hostname", ""),
            "ip": env.get("ip", ""),
            "open_id": env.get("open_id", ""),
            "chat_id": env.get("chat_id") or node.chat_id,
            "load": env.get("load", 0.0),
            "models": env.get("capabilities") or env.get("models") or [],
            "started_at": env.get("started_at", ""),
            "capabilities": env.get("capabilities") or [],
            # v0.4 cluster fields
            "cluster_id": env.get("cluster") or node.cluster_id,
            "app_id": env_app_id or node.app_id,
            # slot_id / agent_secret 不从 envelope 取（不可信），保留 node 已有的
        }


def _const_time_eq(a: str, b: str) -> bool:
    """常量时间字符串比较，防止 timing oracle。"""
    import hmac as _hmac
    return _hmac.compare_digest(a or "", b or "")


async def _get_tenant_token(app_id: Optional[str] = None) -> str:
    """获取 tenant_access_token（用于删除 bot 自己发的消息）。

    多 app（xpage）路由：
    - app_id 显式给出时，走 token_pool 拿该 app 的 token —— 这样删除的是该 app 发的消息
    - 否则回退到全局 APP_ID/APP_SECRET（单 app 时代行为）
    """
    if app_id and app_id != config.APP_ID:
        try:
            # token_pool.auth_header returns {"Authorization": "Bearer <tok>"}
            hdr = token_pool.auth_header(app_id)
            return hdr["Authorization"].split(" ", 1)[1]
        except Exception as e:
            logger.debug("token_pool tenant for %s failed: %s; fallback to global", app_id, e)
    cli = get_http_client()
    r = await cli.post(
        f"{config.FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": config.APP_ID, "app_secret": config.APP_SECRET},
        timeout=15,
    )
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"tenant_token: {d}")
    return d["tenant_access_token"]


async def _try_delete_message(node: "BotNode", message_id: str) -> None:
    """Best-effort 删除已消费的 bot 响应消息，防止聊天窗口堆积。

    用 node 自身 app 的 tenant token 删除 —— xpage 多 app 部署下，bot 的消息是
    bot 自己的 app 发的，必须用同一个 app 的 token 才有权限删除，否则 403。
    """
    try:
        token = await _get_tenant_token(node.app_id or None)
        cli = get_http_client()
        r = await cli.delete(
            f"{config.FEISHU_BASE}/open-apis/im/v1/messages/{message_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if r.status_code == 200:
            logger.debug("deleted consumed message %s", message_id)
        else:
            logger.debug("delete message %s: HTTP %d", message_id, r.status_code)
    except Exception as e:
        # 大概率是应用没有 im:message:delete 权限，静默忽略
        logger.debug("delete message failed: %s", e)


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
