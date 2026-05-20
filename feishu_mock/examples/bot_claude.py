"""
Bot under test：背后调 ModelProxy 的 Claude，而不是 OpenClaw。

链路：
  feishu_mock(/mock/feishu/receive)
    --webhook-->  本服务 /feishu/webhook
                  本服务 --HTTP--> stepcode.basemind.com (Claude)
                  本服务 --HTTP--> feishu_mock /open-apis/im/v1/messages
    <--reply--    feishu_mock 把 sent_messages 存起来供查询

环境变量：
  MODELPROXY_BASE     默认 https://stepcode.basemind.com
  MODELPROXY_API_KEY  ModelProxy 的 Key（默认用 API.md 里的测试 Key）
  MODELPROXY_MODEL    默认 claude-opus-4-5-20251101
  FEISHU_API_BASE     默认 http://localhost:8000（feishu_mock）
  PORT                默认 9000
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

import httpx
from fastapi import BackgroundTasks, FastAPI, Request

MODELPROXY_BASE = os.getenv("MODELPROXY_BASE", "https://stepcode.basemind.com").rstrip("/")
MODELPROXY_API_KEY = os.getenv("MODELPROXY_API_KEY", "ak-xqmsbezufm409fkaxruv35njq4vlnvtq")
MODELPROXY_MODEL = os.getenv("MODELPROXY_MODEL", "claude-opus-4-5-20251101")
FEISHU_API_BASE = os.getenv("FEISHU_API_BASE", "http://localhost:8000").rstrip("/")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bot-claude")

app = FastAPI(title="Bot under test (Claude via ModelProxy)")


async def ask_claude(user_text: str) -> str:
    payload = {
        "model": MODELPROXY_MODEL,
        "messages": [{"role": "user", "content": user_text}],
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=120) as cli:
        r = await cli.post(
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


async def send_to_feishu(user_id: str, text: str) -> dict:
    """通过 mock 的伪飞书 OpenAPI 把消息发回去；mock 会把它记到 store。"""
    async with httpx.AsyncClient(timeout=10) as cli:
        # 真实 OpenClaw 也会先拿 token，这里只是模拟一下握手
        await cli.post(
            f"{FEISHU_API_BASE}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": "x", "app_secret": "x"},
        )
        r = await cli.post(
            f"{FEISHU_API_BASE}/open-apis/im/v1/messages",
            params={"receive_id_type": "user_id"},
            json={
                "receive_id": user_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
            headers={"Authorization": "Bearer t-mock-tenant-access-token"},
        )
    return r.json()


@app.post("/feishu/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()

    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge")}

    event = body.get("event") or {}
    msg = event.get("message") or {}
    sender = (event.get("sender") or {}).get("sender_id") or {}

    if msg.get("message_type") != "text":
        logger.info("non-text message ignored: %s", msg.get("message_type"))
        return {"code": 0}

    content = json.loads(msg.get("content", "{}"))
    user_text = content.get("text", "")
    user_id = sender.get("user_id") or sender.get("open_id") or "unknown"

    logger.info("← webhook | user=%s | text=%r", user_id, user_text)

    # 飞书要求 3s 内 ACK，所以把"调模型 + 发回复"扔到后台跑
    background_tasks.add_task(_handle_async, user_id, user_text)
    return {"code": 0}


async def _handle_async(user_id: str, user_text: str) -> None:
    try:
        reply_text = await ask_claude(user_text)
    except Exception as e:
        reply_text = f"[Claude 异常] {type(e).__name__}: {e}"
        logger.exception("claude call failed")

    logger.info("→ feishu_mock | user=%s | reply=%r", user_id, reply_text[:120])
    try:
        await send_to_feishu(user_id, reply_text)
    except Exception:
        logger.exception("send_to_feishu failed")


@app.get("/")
async def root():
    return {
        "service": "bot-claude",
        "modelproxy_base": MODELPROXY_BASE,
        "model": MODELPROXY_MODEL,
        "feishu_api_base": FEISHU_API_BASE,
    }
