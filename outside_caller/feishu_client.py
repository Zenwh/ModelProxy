"""
Feishu API 通信封装：以用户身份给 bot 发消息、轮询拿回复。
支持 refresh_token 自动续期。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Optional

import httpx

from . import config

logger = logging.getLogger("feishu-client")

# 距过期还剩多少秒时就提前刷新
TOKEN_REFRESH_MARGIN = 300  # 5 分钟


class TokenExpiredError(Exception):
    """user_access_token 已过期且无法自动刷新。"""


class FeishuClient:
    def __init__(self):
        self._token: Optional[str] = None
        self._token_expires_at: float = 0
        self._refresh: Optional[str] = None
        self._load_token()

    # ---- token 管理 ----------------------------------------------------------

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

        # 检查 refresh_token 是否也过期了（7 天有效期）
        rt_expires_in = data.get("refresh_token_expires_in") or 604800
        rt_remaining = (mtime + rt_expires_in) - time.time()

        logger.info(
            "加载 token，剩余 %.0f 秒（%.1f 分钟），refresh_token=%s",
            remaining, remaining / 60,
            "有" if self._refresh else "无",
        )
        if self._refresh and rt_remaining <= 0:
            logger.warning(
                "⚠️  refresh_token 已过期（%.1f 天前），需要重新 OAuth: "
                "python -m outside_caller.oauth_once",
                -rt_remaining / 86400,
            )
            self._refresh = None  # 标记不可用
        elif self._refresh:
            logger.info(
                "refresh_token 剩余 %.1f 天",
                rt_remaining / 86400,
            )

    def _save_token(self, data: dict):
        """把新 token 写回文件。"""
        os.makedirs(os.path.dirname(config.TOKEN_FILE), exist_ok=True)
        with open(config.TOKEN_FILE, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info("token 已写回 %s", config.TOKEN_FILE)

    def _do_refresh(self):
        """用 refresh_token 刷新 user_access_token（v2 接口）。"""
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
            # refresh_token 可能也过期了，清掉
            self._refresh = None
            raise TokenExpiredError(f"refresh_token 刷新失败: {err_msg}")

        data = res.get("data") or res
        new_token = data.get("access_token")
        new_refresh = data.get("refresh_token", "")
        expires_in = data.get("expires_in") or 7200

        # 更新内存
        self._token = new_token
        self._refresh = new_refresh or self._refresh  # 有的实现不返回新 refresh
        self._token_expires_at = time.time() + expires_in

        # 写回文件
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
            "✅ token 刷新成功，新有效期 %ds（%.1f 分钟）",
            expires_in, expires_in / 60,
        )

    def _needs_refresh(self) -> bool:
        """是否需要刷新（距过期 < MARGIN 或已过期）。"""
        return time.time() >= (self._token_expires_at - TOKEN_REFRESH_MARGIN)

    def get_token(self) -> str:
        """获取有效的 user_access_token，必要时自动刷新。"""
        # 还没过期也没到 margin
        if self._token and not self._needs_refresh():
            return self._token

        # 先尝试 refresh
        if self._refresh:
            try:
                self._do_refresh()
                return self._token
            except Exception as e:
                logger.warning("自动 refresh 失败: %s", e)

        # refresh 失败，尝试重新加载文件（也许外部 oauth_once 刚刷新了）
        self._load_token()
        if self._token and not self._needs_refresh():
            return self._token

        raise TokenExpiredError(
            "user_access_token 已过期且无法自动刷新，请重新运行: "
            "python -m outside_caller.oauth_once"
        )

    def maybe_refresh(self):
        """后台定时调用：如果距过期 < margin 且有 refresh_token，提前刷新。"""
        if not self._needs_refresh():
            remaining = self._token_expires_at - time.time()
            logger.debug("token 还剩 %.0fs，暂不刷新", remaining)
            return
        if not self._refresh:
            logger.warning("token 即将过期但没有 refresh_token")
            return
        self._do_refresh()

    def _auth_header(self) -> dict:
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

    # ---- 发消息 ---------------------------------------------------------------

    async def send_message(self, text: str) -> dict:
        """以用户身份给 bot 发文本消息。返回飞书 API 的 data 字段。"""
        async with httpx.AsyncClient(timeout=15) as cli:
            r = await cli.post(
                f"{config.FEISHU_BASE}/open-apis/im/v1/messages",
                params={"receive_id_type": "open_id"},
                headers=self._auth_header(),
                json={
                    "receive_id": config.BOT_OPEN_ID,
                    "msg_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                },
            )
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"发消息失败: code={d.get('code')} msg={d.get('msg')}")
        return d["data"]

    # ---- 轮询回复 -------------------------------------------------------------

    async def poll_reply(
        self,
        after_ms: int,
        timeout_s: Optional[int] = None,
    ) -> Optional[str]:
        """
        轮询 P2P 会话历史，拿到 bot 在 after_ms 之后发的第一条消息文本。
        返回 None 表示超时。
        """
        timeout_s = timeout_s or config.POLL_TIMEOUT_S
        deadline = time.time() + timeout_s
        interval = config.POLL_INTERVAL_S

        while time.time() < deadline:
            text = await self._check_once(after_ms)
            if text is not None:
                return text
            await asyncio.sleep(interval)
        return None

    async def _check_once(self, after_ms: int) -> Optional[str]:
        async with httpx.AsyncClient(timeout=15) as cli:
            r = await cli.get(
                f"{config.FEISHU_BASE}/open-apis/im/v1/messages",
                params={
                    "container_id_type": "chat",
                    "container_id": config.CHAT_ID,
                    "sort_type": "ByCreateTimeDesc",
                    "page_size": 5,
                },
                headers=self._auth_header(),
            )
        d = r.json()
        if d.get("code") != 0:
            logger.warning("poll err: code=%s msg=%s", d.get("code"), d.get("msg"))
            return None

        for m in (d.get("data") or {}).get("items") or []:
            ct = int(m.get("create_time", "0"))
            if ct <= after_ms:
                continue
            sender_type = (m.get("sender") or {}).get("sender_type", "")
            if sender_type != "app":
                continue
            return self._extract_text(m)
        return None

    # ---- 按 req_id 匹配 ------------------------------------------------------

    async def poll_reply_by_req_id(
        self,
        req_id: str,
        after_ms: int,
        timeout_s: Optional[int] = None,
    ) -> Optional[dict]:
        """
        轮询找到 bot 的 JSON 响应，且 req_id 匹配的那一条。
        返回解析后的 dict，超时返回 None。
        """
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
                        "container_id": config.CHAT_ID,
                        "sort_type": "ByCreateTimeDesc",
                        "page_size": 10,
                    },
                    headers=self._auth_header(),
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

                text = self._extract_text(m)
                try:
                    parsed = json.loads(text)
                except (ValueError, TypeError):
                    continue
                if not isinstance(parsed, dict):
                    continue
                if parsed.get("req_id") == req_id:
                    return parsed

            await asyncio.sleep(interval)
        return None

    @staticmethod
    def _extract_text(msg: dict) -> str:
        body = (msg.get("body") or {}).get("content", "")
        msg_type = msg.get("msg_type", "")
        try:
            parsed = json.loads(body)
        except Exception:
            return body

        if msg_type == "text":
            return parsed.get("text", body)
        elif msg_type == "post":
            # 富文本：拼所有 text 节点
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
            # 卡片消息：尝试提取所有 text 元素
            elements = parsed.get("elements", [])
            parts = []
            FeishuClient._walk_card_elements(elements, parts)
            if parts:
                return "\n".join(parts)
            # fallback：title
            title = (parsed.get("header") or {}).get("title", {}).get("content", "")
            return title or body
        else:
            return body

    @staticmethod
    def _walk_card_elements(elements, parts: list):
        """递归提取卡片里的文本。"""
        if isinstance(elements, list):
            for el in elements:
                FeishuClient._walk_card_elements(el, parts)
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
            # 递归子元素
            for key in ("elements", "columns", "body", "rows", "cells"):
                child = elements.get(key)
                if child:
                    FeishuClient._walk_card_elements(child, parts)


# 单例
client = FeishuClient()
