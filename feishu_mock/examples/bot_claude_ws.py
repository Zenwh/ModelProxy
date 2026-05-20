"""
Bot Claude (WebSocket 长连接版)
================================

用飞书 SDK 的 websocket 模式连接飞书，收到用户消息后调 Model Proxy Claude，
把纯文本回复发回飞书。不需要公网入口。

部署到内网机器，替代 OpenClaw 作为阿月老师的后端。

环境变量：
  FEISHU_APP_ID       飞书 app ID（默认：阿月老师）
  FEISHU_APP_SECRET   飞书 app secret
  MODELPROXY_BASE     Model Proxy 地址（默认 https://stepcode.basemind.com）
  MODELPROXY_API_KEY  Model Proxy API Key
  MODELPROXY_MODEL    模型名（默认 claude-opus-4-5-20251101）

用法：
  pip install lark-oapi httpx
  python bot_claude_ws.py
"""
from __future__ import annotations

import json
import logging
import os
import threading

import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

# ---- 配置 -------------------------------------------------------------------

APP_ID = os.getenv("FEISHU_APP_ID", "cli_a955f5aa04f81bda")
APP_SECRET = os.getenv("FEISHU_APP_SECRET", "kETZGoqR0S6eEwhFhLszLd7bqsKSt7cr")

MODELPROXY_BASE = os.getenv("MODELPROXY_BASE", "https://stepcode.basemind.com").rstrip("/")
MODELPROXY_API_KEY = os.getenv("MODELPROXY_API_KEY", "ak-xqmsbezufm409fkaxruv35njq4vlnvtq")
MODELPROXY_MODEL = os.getenv("MODELPROXY_MODEL", "claude-opus-4-5-20251101")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bot-claude-ws")


# ---- Model Proxy 调用 -------------------------------------------------------

def ask_claude(user_text: str) -> str:
    """同步调用 Model Proxy Claude。"""
    payload = {
        "model": MODELPROXY_MODEL,
        "messages": [{"role": "user", "content": user_text}],
        "stream": False,
    }
    with httpx.Client(timeout=120) as cli:
        r = cli.post(
            f"{MODELPROXY_BASE}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {MODELPROXY_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    data = r.json()
    choices = data.get("choices") or []
    if not choices:
        return f"[Claude 调用失败] {data.get('msg') or data}"
    return choices[0]["message"]["content"]


# ---- 飞书回复 ----------------------------------------------------------------

# 全局 lark client，在 main() 里初始化
lark_client: lark.Client = None


def reply_text(chat_id: str, text: str):
    """以 bot 身份给会话发纯文本消息。"""
    req = CreateMessageRequest.builder() \
        .receive_id_type("chat_id") \
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        ).build()
    resp = lark_client.im.v1.message.create(req)
    if not resp.success():
        logger.error("回复失败: code=%s msg=%s", resp.code, resp.msg)
    else:
        logger.info("→ 回复 chat=%s 长度=%d", chat_id, len(text))


# ---- 事件处理 ----------------------------------------------------------------

def on_message(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    """收到 im.message.receive_v1 事件。"""
    event = data.event
    msg = event.message
    sender = event.sender

    # 只处理文本消息
    if msg.message_type != "text":
        logger.info("忽略非文本消息: type=%s", msg.message_type)
        return

    content = json.loads(msg.content)
    user_text = content.get("text", "")
    user_id = sender.sender_id.user_id or sender.sender_id.open_id or "?"
    chat_id = msg.chat_id

    logger.info("← 收到消息 user=%s chat=%s text=%r", user_id, chat_id, user_text[:80])

    # 在子线程里调 Claude，避免阻塞 websocket（3s 超时限制）
    def _handle():
        try:
            reply = ask_claude(user_text)
        except Exception as e:
            reply = f"[Claude 异常] {type(e).__name__}: {e}"
            logger.exception("Claude 调用失败")
        reply_text(chat_id, reply)

    threading.Thread(target=_handle, daemon=True).start()


# ---- 主入口 ------------------------------------------------------------------

def main():
    global lark_client

    logger.info("启动 bot-claude-ws")
    logger.info("  APP_ID=%s", APP_ID)
    logger.info("  MODEL=%s", MODELPROXY_MODEL)
    logger.info("  MODELPROXY_BASE=%s", MODELPROXY_BASE)

    # 构建 lark client（用于发消息）
    lark_client = lark.Client.builder() \
        .app_id(APP_ID) \
        .app_secret(APP_SECRET) \
        .log_level(lark.LogLevel.INFO) \
        .build()

    # 构建事件处理器
    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(on_message) \
        .build()

    # 启动 websocket 长连接
    ws_client = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )

    logger.info("连接飞书 websocket ...")
    ws_client.start()  # 阻塞，直到进程退出


if __name__ == "__main__":
    main()
