"""
SlotPool：预分配的 (app_id, app_secret, chat_id, mp_key) 资源池。

设计依据：arch-cluster-upgrade.md §3.2 / §3.8

数据模型：
- AgentSlot：一份完整的可分配凭证集合
- SlotPool：管理所有 slot 的生命周期，持久化到 JSON

操作：
- claim(node_id)：原子地拿一个空闲 slot，标记 claimed_by/claimed_at
- release(slot_id)：清空 claim 字段，slot 回到空闲池
- get_secret(app_id)：给 TokenManagerPool 用的 resolver
- list_slots() / counts / add / delete

幂等性：
- bootstrap 端点会先查 bot_pool.get(node_id) 看是否已有 slot；
  有就直接复用，不会重复 claim。SlotPool 自身不维护 node_id → slot_id 的反向索引，
  那是 bot_pool 的职责。

线程/异步安全：内部用 threading.Lock；和 bot_pool 一样，所有方法都是同步的。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from . import config

logger = logging.getLogger("slot-pool")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class AgentSlot:
    """一份预分配的节点凭证（参见 §3.2）。

    claim 状态字段：
    - claimed_by：节点 id；None 表示空闲
    - claimed_at：被领走的时间戳

    capabilities 字段供 bootstrap 响应用，告诉节点支持哪些模型。
    None 时回退到 gateway 全局 ModelRegistry。
    """
    slot_id: str
    app_id: str
    app_secret: str
    chat_id: str
    mp_key: str
    mp_base_url: str = ""
    notes: str = ""
    capabilities: List[str] = field(default_factory=list)
    default_max_tokens: int = 4096
    heartbeat_interval_s: int = 30
    claimed_by: Optional[str] = None
    claimed_at: Optional[str] = None
    created_at: str = ""

    @property
    def is_free(self) -> bool:
        return self.claimed_by is None


class SlotPool:
    """slot 资源池：CRUD + claim/release。

    持久化：默认 {config.STATE_DIR}/slot_pool.json
    """

    def __init__(self, file_path: Optional[str] = None):
        self._file = file_path or os.path.join(config.STATE_DIR, "slot_pool.json")
        self._slots: Dict[str, AgentSlot] = {}
        self._lock = threading.Lock()
        # app_id → slot_id 反向索引（resolver 用），重建于 _load
        self._app_index: Dict[str, str] = {}
        self._load()

    # ---- 持久化 ---------------------------------------------------------------

    def _load(self) -> None:
        if not os.path.exists(self._file):
            logger.info("slot_pool file %s not found (empty pool)", self._file)
            return
        try:
            with open(self._file) as f:
                data = json.load(f)
        except Exception as e:
            logger.warning("读取 slot_pool 失败: %s", e)
            return
        known = {f.name for f in AgentSlot.__dataclass_fields__.values()}
        for sid, rec in data.get("slots", {}).items():
            rec = {k: v for k, v in rec.items() if k in known}
            self._slots[sid] = AgentSlot(**rec)
            self._app_index[self._slots[sid].app_id] = sid
        logger.info(
            "加载 %d 个 slot (%d free, %d claimed)",
            len(self._slots), self.free_count, self.claimed_count,
        )

    def _save_locked(self) -> None:
        """调用方必须持锁。"""
        os.makedirs(os.path.dirname(self._file), exist_ok=True)
        tmp = self._file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(
                {"slots": {k: asdict(v) for k, v in self._slots.items()}},
                f, indent=2, ensure_ascii=False,
            )
        os.replace(tmp, self._file)
        # 文件含 app_secret / mp_key，只本机 root 可读
        try:
            os.chmod(self._file, 0o600)
        except OSError:
            pass

    # ---- CRUD -----------------------------------------------------------------

    def add(
        self,
        app_id: str,
        app_secret: str,
        chat_id: str,
        mp_key: str,
        *,
        mp_base_url: str = "",
        notes: str = "",
        capabilities: Optional[List[str]] = None,
        default_max_tokens: int = 4096,
        heartbeat_interval_s: int = 30,
        slot_id: Optional[str] = None,
    ) -> AgentSlot:
        """管理员灌一条新 slot。slot_id 不指定时自动编号 slot-NNN。"""
        if not app_id or not app_secret or not chat_id:
            raise ValueError("app_id, app_secret, chat_id are required")
        with self._lock:
            if app_id in self._app_index:
                # 同 app_id 只能注册一次；这是为了防止同一 Feishu app 多 slot 抢同一 chat
                raise ValueError(f"slot with app_id={app_id} already exists")
            if slot_id is None:
                slot_id = self._next_slot_id_locked()
            if slot_id in self._slots:
                raise ValueError(f"slot_id={slot_id} already exists")
            slot = AgentSlot(
                slot_id=slot_id,
                app_id=app_id,
                app_secret=app_secret,
                chat_id=chat_id,
                mp_key=mp_key,
                mp_base_url=mp_base_url,
                notes=notes,
                capabilities=list(capabilities or []),
                default_max_tokens=default_max_tokens,
                heartbeat_interval_s=heartbeat_interval_s,
                created_at=_now_iso(),
            )
            self._slots[slot_id] = slot
            self._app_index[app_id] = slot_id
            self._save_locked()
            logger.info("slot added: %s app_id=%s chat_id=%s", slot_id, app_id, chat_id)
            return slot

    def delete(self, slot_id: str) -> bool:
        """删除一条 slot。须先 release（claimed slot 不让删）。"""
        with self._lock:
            slot = self._slots.get(slot_id)
            if slot is None:
                return False
            if slot.claimed_by:
                raise ValueError(
                    f"slot {slot_id} is claimed by {slot.claimed_by}; release it first"
                )
            self._slots.pop(slot_id, None)
            self._app_index.pop(slot.app_id, None)
            self._save_locked()
            logger.info("slot deleted: %s", slot_id)
            return True

    def get(self, slot_id: str) -> Optional[AgentSlot]:
        return self._slots.get(slot_id)

    def get_by_app(self, app_id: str) -> Optional[AgentSlot]:
        sid = self._app_index.get(app_id)
        return self._slots.get(sid) if sid else None

    def get_secret(self, app_id: str) -> Optional[str]:
        """TokenManagerPool resolver：根据 app_id 拿 app_secret。"""
        slot = self.get_by_app(app_id)
        return slot.app_secret if slot else None

    def list_slots(self) -> List[AgentSlot]:
        return sorted(self._slots.values(), key=lambda s: s.slot_id)

    @property
    def free_count(self) -> int:
        return sum(1 for s in self._slots.values() if s.is_free)

    @property
    def claimed_count(self) -> int:
        return sum(1 for s in self._slots.values() if not s.is_free)

    # ---- claim / release ------------------------------------------------------

    def claim(self, node_id: str) -> Optional[AgentSlot]:
        """原子拿一个空闲 slot 给 node_id。无空闲返回 None。

        分配策略：按 slot_id 字典序取第一个空闲的（确定性 + 易于排查）。
        """
        with self._lock:
            for sid in sorted(self._slots.keys()):
                slot = self._slots[sid]
                if slot.is_free:
                    slot.claimed_by = node_id
                    slot.claimed_at = _now_iso()
                    self._save_locked()
                    logger.info(
                        "slot claimed: %s by %s (free remaining=%d)",
                        sid, node_id, self.free_count,
                    )
                    return slot
            logger.warning("claim failed: no free slot (total=%d)", len(self._slots))
            return None

    def release(self, slot_id: str) -> bool:
        """释放 slot 回到空闲池。"""
        with self._lock:
            slot = self._slots.get(slot_id)
            if slot is None:
                return False
            if slot.is_free:
                return True
            prev_owner = slot.claimed_by
            slot.claimed_by = None
            slot.claimed_at = None
            self._save_locked()
            logger.info("slot released: %s (was held by %s)", slot_id, prev_owner)
            return True

    def claimed_by(self, node_id: str) -> Optional[AgentSlot]:
        """反查某个 node_id 当前持有的 slot（如果有）。"""
        for slot in self._slots.values():
            if slot.claimed_by == node_id:
                return slot
        return None

    # ---- 私有 -----------------------------------------------------------------

    def _next_slot_id_locked(self) -> str:
        n = 1
        while True:
            sid = f"slot-{n:03d}"
            if sid not in self._slots:
                return sid
            n += 1


# 全局单例。导入时即加载磁盘文件（空池子也 OK，bootstrap 时返回 503）。
slot_pool = SlotPool()


# ---- 与 TokenManagerPool 绑定 -----------------------------------------------
# slot_pool.get_secret 作为 resolver 注入到 token_pool；之后任何持有 app_id 的
# 调用方（_auth_header_for(node)）都能拿到正确的 tenant_access_token。
#
# 放在模块底部而不是 relay_server lifespan 里：
# - 任何 import slot_pool 的地方（bootstrap 端点 / admin slot API）都自动可用
# - 避免循环导入 (feishu_token 不依赖 slot_pool，slot_pool 引入 feishu_token)
from .feishu_token import token_pool as _token_pool  # noqa: E402

_token_pool.bind_resolver(slot_pool.get_secret)
logger.info("token_pool resolver bound to slot_pool.get_secret")

