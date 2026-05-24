"""
API Key 管理模块。
JSON 文件存储，内存缓存，支持 CRUD。
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from . import config

logger = logging.getLogger("api-keys")

KEY_PREFIX = "sk-relay-"


@dataclass
class KeyInfo:
    key: str
    name: str
    created_at: str
    enabled: bool = True
    is_admin: bool = False
    rpm_limit: Optional[int] = None             # 每分钟请求数；None = unlimited
    daily_token_limit: Optional[int] = None     # 每日 token 上限；None = unlimited


class APIKeyManager:
    """API Key 管理器。JSON 文件持久化 + 内存缓存。"""

    def __init__(self, key_file: Optional[str] = None):
        self._file = key_file or config.API_KEYS_FILE
        self._keys: Dict[str, KeyInfo] = {}
        self._lock = threading.Lock()
        self._load()

    # ---- 加载 / 保存 ---------------------------------------------------------

    def _load(self):
        if not os.path.exists(self._file):
            # Require the api_keys JSON file to exist.  Falling back to the single
            # RELAY_API_KEY env var and quietly granting it admin would let anyone
            # who knows (or guesses) that well-known default value take full admin
            # control of the relay, so we no longer do that.
            logger.info(
                "api_keys 文件不存在 (%s)。"
                "先运行 'python -m outside_caller.keys create <name>' 创建 key，"
                "或将 RELAY_API_KEY 写入 %s。",
                self._file, self._file,
            )
            return

        with open(self._file) as f:
            data = json.load(f)

        for item in data.get("keys", []):
            info = KeyInfo(
                key=item["key"],
                name=item.get("name", ""),
                created_at=item.get("created_at", ""),
                enabled=item.get("enabled", True),
                is_admin=item.get("is_admin", False),
                rpm_limit=item.get("rpm_limit"),
                daily_token_limit=item.get("daily_token_limit"),
            )
            self._keys[info.key] = info

        logger.info("加载了 %d 个 API key", len(self._keys))

    def _save(self):
        os.makedirs(os.path.dirname(self._file), exist_ok=True)
        config.atomic_write_json(self._file, {
            "keys": [asdict(k) for k in self._keys.values()],
        })
        logger.info("api_keys 已保存 (%d 个)", len(self._keys))

    # ---- CRUD ----------------------------------------------------------------

    def validate(self, key: str) -> Optional[KeyInfo]:
        """验证 key（完整 key 或 prefix）。返回 KeyInfo 或 None（无效/被禁用）。"""
        info = self._keys.get(key)
        if info and info.enabled:
            return info
        # prefix match — only used by admin routes where caller holds the real key
        resolved = self._resolve_key(key)
        if resolved:
            info = self._keys.get(resolved)
            if info and info.enabled:
                return info
        return None

    def create_key(self, name: str, is_admin: bool = False) -> KeyInfo:
        """生成新 key。"""
        with self._lock:
            key_str = KEY_PREFIX + secrets.token_hex(24)
            info = KeyInfo(
                key=key_str,
                name=name,
                created_at=datetime.now().isoformat(timespec="seconds"),
                enabled=True,
                is_admin=is_admin,
            )
            self._keys[key_str] = info
            self._save()
            logger.info("创建 key: name=%s admin=%s", name, is_admin)
            return info

    def revoke_key(self, key: str) -> bool:
        """禁用 key。返回是否成功。"""
        with self._lock:
            resolved = self._resolve_key(key)
            if resolved is None:
                return False
            info = self._keys[resolved]
            if not info:
                return False
            info.enabled = False
            self._save()
            logger.info("禁用 key: name=%s", info.name)
            return True

    def enable_key(self, key: str) -> bool:
        """重新启用 key。"""
        with self._lock:
            resolved = self._resolve_key(key)
            if resolved is None:
                return False
            info = self._keys[resolved]
            if not info:
                return False
            info.enabled = True
            self._save()
            logger.info("启用 key: name=%s", info.name)
            return True

    def set_limits(
        self,
        key: str,
        rpm_limit: Optional[int] = None,
        daily_token_limit: Optional[int] = None,
        clear_rpm: bool = False,
        clear_daily: bool = False,
    ) -> bool:
        """
        设置 key 的限额。
        rpm_limit/daily_token_limit 为 None 时表示不改；
        要清空请用 clear_rpm=True 或 clear_daily=True。
        """
        with self._lock:
            resolved = self._resolve_key(key)
            if resolved is None:
                return False
            info = self._keys[resolved]
            if not info:
                return False
            if clear_rpm:
                info.rpm_limit = None
            elif rpm_limit is not None:
                info.rpm_limit = rpm_limit
            if clear_daily:
                info.daily_token_limit = None
            elif daily_token_limit is not None:
                info.daily_token_limit = daily_token_limit
            self._save()
            logger.info(
                "更新 key 限额: name=%s rpm=%s daily=%s",
                info.name, info.rpm_limit, info.daily_token_limit,
            )
            return True

    def delete_key(self, key: str) -> bool:
        """永久删除 key。"""
        with self._lock:
            resolved = self._resolve_key(key)
            if resolved not in self._keys:
                return False
            name = self._keys[resolved].name
            del self._keys[resolved]
            self._save()
            logger.info("删除 key: name=%s", name)
            return True

    def list_keys(self) -> List[KeyInfo]:
        """列出所有 key（含禁用的）。"""
        return list(self._keys.values())

    def _resolve_key(self, key_or_prefix: str) -> Optional[str]:
        """
        通过完整 key 或 prefix 找到存储的真实 key 字符串。
        - 精确匹配直接返回。
        - 否则按 prefix 找唯一匹配；无或多则返回 None（上层报 404/409）。
        """
        if key_or_prefix in self._keys:
            return key_or_prefix
        matches = [k for k in self._keys if k.startswith(key_or_prefix)]
        if len(matches) == 1:
            return matches[0]
        return None

    def is_admin(self, key: str) -> bool:
        """检查是否是 admin key。"""
        info = self._keys.get(key)
        return bool(info and info.enabled and info.is_admin)

    @property
    def key_count(self) -> int:
        return len(self._keys)

    @property
    def active_count(self) -> int:
        return sum(1 for k in self._keys.values() if k.enabled)


# 单例
manager = APIKeyManager()
