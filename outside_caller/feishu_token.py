"""
飞书 token 管理：user_access_token 的加载、刷新、持久化。

Gateway 侧使用，Bot 侧不需要。
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

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
