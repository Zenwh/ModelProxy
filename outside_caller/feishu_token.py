"""
飞书 token 管理：user_access_token 的加载、刷新、持久化。

Gateway 侧使用，Bot 侧不需要。

v0.4 (cluster upgrade) 起新增 AppTokenManager + TokenManagerPool：
- AppTokenManager 用 tenant_access_token (app_id+app_secret) — 无需 OAuth
- TokenManagerPool 缓存 app_id → AppTokenManager，每个集群节点独立 token
- 老的 token_mgr (user_access_token) 仍然保留给 legacy 节点 / dashboard 用
详见 arch-cluster-upgrade.md §3.1, §3.6.2。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Callable, Optional

import httpx

from . import config

logger = logging.getLogger("feishu-token")

TOKEN_REFRESH_MARGIN = 300  # 距过期还剩多少秒时提前刷新


class TokenExpiredError(Exception):
    """user_access_token 已过期且无法自动刷新。"""


class TokenManager:
    def __init__(self):
        self._token: Optional[str] = None
        self._token_expires_at: float = 0
        self._refresh: Optional[str] = None
        self._load_token()

    def _load_token(self):
        if not os.path.exists(config.TOKEN_FILE):
            logger.warning("token 文件不存在: %s", config.TOKEN_FILE)
            return
        with open(config.TOKEN_FILE) as f:
            data = json.load(f)
        self._token = data.get("user_access_token")
        self._refresh = data.get("refresh_token") or None
        expires_in = data.get("expires_in") or 7200
        mtime = os.path.getmtime(config.TOKEN_FILE)
        self._token_expires_at = mtime + expires_in
        remaining = self._token_expires_at - time.time()

        rt_expires_in = data.get("refresh_token_expires_in") or 604800
        rt_remaining = (mtime + rt_expires_in) - time.time()

        logger.info(
            "加载 token，剩余 %.0f 秒（%.1f 分钟），refresh_token=%s",
            remaining, remaining / 60,
            "有" if self._refresh else "无",
        )
        if self._refresh and rt_remaining <= 0:
            logger.warning(
                "refresh_token 已过期（%.1f 天前），需要重新 OAuth: "
                "python -m outside_caller.oauth_once",
                -rt_remaining / 86400,
            )
            self._refresh = None
        elif self._refresh:
            logger.info("refresh_token 剩余 %.1f 天", rt_remaining / 86400)

    def _save_token(self, data: dict):
        os.makedirs(os.path.dirname(config.TOKEN_FILE), exist_ok=True)
        with open(config.TOKEN_FILE, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info("token 已写回 %s", config.TOKEN_FILE)

    def _do_refresh(self):
        if not self._refresh:
            raise TokenExpiredError("没有 refresh_token，无法自动刷新")

        logger.info("正在用 refresh_token 刷新 access_token ...")
        with httpx.Client(timeout=15) as cli:
            r = cli.post(
                f"{config.FEISHU_BASE}/open-apis/authen/v2/oauth/token",
                headers={"Content-Type": "application/json"},
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh,
                    "client_id": config.APP_ID,
                    "client_secret": config.APP_SECRET,
                },
            )
        res = r.json()
        if res.get("code") != 0:
            err_msg = res.get("msg") or res.get("error_description") or str(res)
            logger.error("refresh 失败: code=%s msg=%s", res.get("code"), err_msg)
            self._refresh = None
            raise TokenExpiredError(f"refresh_token 刷新失败: {err_msg}")

        data = res.get("data") or res
        new_token = data.get("access_token")
        new_refresh = data.get("refresh_token", "")
        expires_in = data.get("expires_in") or 7200

        self._token = new_token
        self._refresh = new_refresh or self._refresh
        self._token_expires_at = time.time() + expires_in

        out = {
            "user_access_token": new_token,
            "refresh_token": new_refresh or (self._refresh or ""),
            "refresh_token_expires_in": data.get("refresh_token_expires_in", 0),
            "open_id": data.get("open_id"),
            "user_id": data.get("user_id"),
            "name": data.get("name", ""),
            "expires_in": expires_in,
            "scope": data.get("scope", ""),
        }
        self._save_token(out)
        logger.info(
            "token 刷新成功，新有效期 %ds（%.1f 分钟）",
            expires_in, expires_in / 60,
        )

    def _needs_refresh(self) -> bool:
        return time.time() >= (self._token_expires_at - TOKEN_REFRESH_MARGIN)

    def get_token(self) -> str:
        if self._token and not self._needs_refresh():
            return self._token

        if self._refresh:
            try:
                self._do_refresh()
                return self._token
            except Exception as e:
                logger.warning("自动 refresh 失败: %s", e)

        self._load_token()
        if self._token and not self._needs_refresh():
            return self._token

        raise TokenExpiredError(
            "user_access_token 已过期且无法自动刷新，请重新运行: "
            "python -m outside_caller.oauth_once"
        )

    def maybe_refresh(self):
        if not self._needs_refresh():
            remaining = self._token_expires_at - time.time()
            logger.debug("token 还剩 %.0fs，暂不刷新", remaining)
            return
        if not self._refresh:
            logger.warning("token 即将过期但没有 refresh_token")
            return
        self._do_refresh()

    def auth_header(self) -> dict:
        return {
            "Authorization": f"Bearer {self.get_token()}",
            "Content-Type": "application/json",
        }

    @property
    def has_refresh_token(self) -> bool:
        return bool(self._refresh)

    @property
    def token_remaining_s(self) -> float:
        return max(0, self._token_expires_at - time.time())


token_mgr = TokenManager()


# ============================================================================
# v0.4 cluster upgrade: per-app tenant_access_token
# ============================================================================
#
# 设计选择：
# - 每个 cluster 节点绑定独立 (app_id, app_secret)，通过 tenant_access_token
#   (app-level token，不需要 OAuth) 拉/发该节点的 chat 消息。
# - 老的 user_access_token (TokenManager / token_mgr) 仍然保留，给 legacy 节点
#   和 dashboard 用，避免大规模回归。
# - Pool 模式 + Lazy 初始化：第一次用到某个 app_id 时再去拿 token，避免
#   gateway 启动时一次性向飞书发 N 个请求。
# - 线程安全：内部加锁；refresh 串行化，防止并发请求导致重复刷新。


class AppTokenManager:
    """单个飞书 app 的 tenant_access_token 管理。

    与 TokenManager (user_access_token) 的关键区别：
    - 用 app_id + app_secret 直接换 token，没有 OAuth refresh_token 流程
    - 过期就重新去拿，不需要持久化 refresh token
    - 内存存活；进程重启后重新拉一次（飞书侧 cap 2h，无所谓）
    """

    def __init__(self, app_id: str, app_secret: str):
        if not app_id or not app_secret:
            raise ValueError("app_id and app_secret required")
        self.app_id = app_id
        self._app_secret = app_secret
        self._token: Optional[str] = None
        self._expires_at: float = 0
        self._lock = threading.Lock()

    def _needs_refresh(self) -> bool:
        return not self._token or time.time() >= (self._expires_at - TOKEN_REFRESH_MARGIN)

    def _do_refresh(self) -> None:
        logger.info("[token-pool] fetching tenant_access_token app_id=%s", self.app_id)
        with httpx.Client(timeout=10) as cli:
            r = cli.post(
                f"{config.FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self._app_secret},
            )
        res = r.json()
        if res.get("code") != 0:
            msg = res.get("msg") or str(res)
            raise TokenExpiredError(
                f"tenant_access_token failed app_id={self.app_id} code={res.get('code')} msg={msg}"
            )
        self._token = res["tenant_access_token"]
        # 飞书返回 expire 字段；保守起见至少 60s
        expire = int(res.get("expire") or 7200)
        self._expires_at = time.time() + max(60, expire)
        logger.info(
            "[token-pool] token refreshed app_id=%s expires_in=%ds",
            self.app_id, expire,
        )

    def get_token(self) -> str:
        with self._lock:
            if self._needs_refresh():
                self._do_refresh()
            return self._token  # type: ignore[return-value]

    def auth_header(self) -> dict:
        return {
            "Authorization": f"Bearer {self.get_token()}",
            "Content-Type": "application/json",
        }

    @property
    def remaining_s(self) -> float:
        return max(0, self._expires_at - time.time())


# secret_resolver(app_id) -> app_secret or None
SecretResolverFn = Callable[[str], Optional[str]]


class TokenManagerPool:
    """app_id → AppTokenManager 的缓存池。

    使用方式（典型来自 bot_pool / poller / send_to_bot）：

        # 启动时：
        token_pool.bind_resolver(slot_pool.get_secret)  # slot_pool 在 M2-3 落地

        # 运行时：
        headers = token_pool.auth_header(node.app_id)
        async with httpx.AsyncClient() as cli:
            await cli.post(url, headers=headers, json=body)

    Resolver 一般由 SlotPool 提供：给定 app_id，返回 app_secret（来自分配过的 slot）。
    legacy 节点 app_id == "" 时调用方应该走老的 token_mgr 路径。
    """

    def __init__(self, secret_resolver: Optional[SecretResolverFn] = None):
        self._resolver: Optional[SecretResolverFn] = secret_resolver
        self._cache: dict[str, AppTokenManager] = {}
        self._lock = threading.Lock()

    def bind_resolver(self, resolver: SecretResolverFn) -> None:
        """运行时注入 resolver（解决 SlotPool 与 token 的循环导入）。"""
        self._resolver = resolver

    def get(self, app_id: str) -> AppTokenManager:
        if not app_id:
            raise ValueError("app_id required (legacy nodes should use token_mgr instead)")
        with self._lock:
            mgr = self._cache.get(app_id)
            if mgr is not None:
                return mgr
            if self._resolver is None:
                raise RuntimeError(
                    "TokenManagerPool: secret_resolver not bound; "
                    "call bind_resolver(slot_pool.get_secret) at startup"
                )
            secret = self._resolver(app_id)
            if not secret:
                raise ValueError(f"no app_secret available for app_id={app_id}")
            mgr = AppTokenManager(app_id, secret)
            self._cache[app_id] = mgr
            return mgr

    def auth_header(self, app_id: str) -> dict:
        return self.get(app_id).auth_header()

    def evict(self, app_id: str) -> None:
        """slot 释放 / app_id 失效时清缓存。"""
        with self._lock:
            self._cache.pop(app_id, None)

    def evict_all(self) -> None:
        with self._lock:
            self._cache.clear()


# 全局单例。bind_resolver() 在 SlotPool (M2-3) 启动时调用。
token_pool = TokenManagerPool()
