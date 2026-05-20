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
            logger.info("api_keys 文件不存在，将 fallback 到 config.RELAY_API_KEY")
            # 兼容旧的单 key 模式
            if config.RELAY_API_KEY:
                legacy = KeyInfo(
                    key=config.RELAY_API_KEY,
                    name="default",
                    created_at="2026-01-01T00:00:00",
                    enabled=True,
                    is_admin=True,
                )
                self._keys[legacy.key] = legacy
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
            )
            self._keys[info.key] = info

        logger.info("加载了 %d 个 API key", len(self._keys))

    def _save(self):
        os.makedirs(os.path.dirname(self._file), exist_ok=True)
        data = {
            "keys": [asdict(k) for k in self._keys.values()],
        }
        with open(self._file, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info("api_keys 已保存 (%d 个)", len(self._keys))

    # ---- CRUD ----------------------------------------------------------------

    def validate(self, key: str) -> Optional[KeyInfo]:
        """验证 key。返回 KeyInfo 或 None（无效/被禁用）。"""
        info = self._keys.get(key)
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
            info = self._keys.get(key)
            if not info:
                return False
            info.enabled = False
            self._save()
            logger.info("禁用 key: name=%s", info.name)
            return True

    def enable_key(self, key: str) -> bool:
        """重新启用 key。"""
        with self._lock:
            info = self._keys.get(key)
            if not info:
                return False
            info.enabled = True
            self._save()
            logger.info("启用 key: name=%s", info.name)
            return True

    def delete_key(self, key: str) -> bool:
        """永久删除 key。"""
        with self._lock:
            if key not in self._keys:
                return False
            name = self._keys[key].name
            del self._keys[key]
            self._save()
            logger.info("删除 key: name=%s", name)
            return True

    def list_keys(self) -> List[KeyInfo]:
        """列出所有 key（含禁用的）。"""
        return list(self._keys.values())

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
