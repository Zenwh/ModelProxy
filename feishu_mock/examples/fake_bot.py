"""
最小可运行的"被测 Bot 服务"示例（仅用于验证 feishu_mock 链路）。
真实环境中替换为 OpenClaw / ModelProxy 的 Bot 处理器即可。

行为：
- 收到 webhook，解出 user 文本，原样 echo 回去（调用飞书 API 发消息）
- 飞书 API base_url 通过环境变量 FEISHU_API_BASE 指向 mock 服务
- 处理 url_verification 握手
"""
from __future__ import annotations

import json
import os

import httpx
from fastapi import FastAPI, Request

FEISHU_API_BASE = os.getenv("FEISHU_API_BASE", "http://localhost:8000")

app = FastAPI(title="Fake Bot under test")


@app.post("/feishu/webhook")
async def webhook(request: Request):
    body = await request.json()

    # 1) url_verification 握手
    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge")}

    # 2) im.message.receive_v1
    event = body.get("event") or {}
    msg = event.get("message") or {}
    sender = (event.get("sender") or {}).get("sender_id") or {}

    if msg.get("message_type") == "text":
        content = json.loads(msg.get("content", "{}"))
        user_text = content.get("text", "")
        user_id = sender.get("user_id") or sender.get("open_id")

        # 模拟调用飞书 API 发回消息
        async with httpx.AsyncClient(timeout=5) as cli:
            # 拿 token（mock 服务不校验）
            await cli.post(f"{FEISHU_API_BASE}/open-apis/auth/v3/tenant_access_token/internal",
                           json={"app_id": "x", "app_secret": "x"})
            # 发消息
            reply_text = f"echo: {user_text}"
            await cli.post(
                f"{FEISHU_API_BASE}/open-apis/im/v1/messages",
                params={"receive_id_type": "user_id"},
                json={
                    "receive_id": user_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": reply_text}, ensure_ascii=False),
                },
                headers={"Authorization": "Bearer t-mock-tenant-access-token"},
            )

    return {"code": 0}
