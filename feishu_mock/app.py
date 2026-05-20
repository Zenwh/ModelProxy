"""
Feishu Mock Server
==================

模拟"用户 → 飞书 → 你的 Bot Webhook"以及"你的 Bot → 飞书 API → 用户"
两段流量，让你不依赖真实飞书也能跑通整条链路。

对外测试接口（/mock/*）
  POST /mock/feishu/receive   触发一次"用户给 Bot 发消息"
  GET  /mock/feishu/sent      查询 Bot 通过飞书 API 发回的消息
  DELETE /mock/feishu/sent    清空已记录的消息

对被测服务暴露（伪装飞书 OpenAPI）
  POST /open-apis/im/v1/messages          发送消息
  POST /open-apis/im/v1/messages/{id}/reply  回复消息
  POST /open-apis/auth/v3/tenant_access_token/internal  申请 token
  GET  /open-apis/im/v1/...                兜底，记录调用

被测服务侧需要做两件事：
  1) 把 webhook URL 配成本服务的 TARGET_WEBHOOK_URL（启动时配）。
  2) 把飞书 API base_url 从 https://open.feishu.cn 改成 http://<本服务>:8000。
"""
from __future__ import annotations

import json
import logging
from typing import Optional, Union

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from . import config
from .event_builder import build_receive_message_event, build_url_verification_event
from .feishu_crypto import build_signed_request
from .store import store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("feishu-mock")

app = FastAPI(
    title="Feishu Mock Server",
    description="模拟飞书收发消息链路的测试服务",
    version="0.1.0",
)


# ============================================================================
# Part 1: 对外测试接口
# ============================================================================


class ReceiveRequest(BaseModel):
    user_id: str = Field(..., description="模拟的发送用户 ID")
    message: str = Field(..., description="用户发给 Bot 的文本内容")
    chat_id: Optional[str] = Field(None, description="会话 ID，省略则随机生成")
    chat_type: str = Field("p2p", description="p2p 或 group")
    # 可选：等待 Bot 通过飞书 API 把消息发回来再返回响应
    wait_reply_ms: int = Field(
        0,
        ge=0,
        le=60_000,
        description="等待 Bot 回复的毫秒数；0 表示立即返回不等",
    )


class ReceiveResponse(BaseModel):
    ok: bool
    target_webhook: str
    webhook_status: int
    webhook_response: Optional[Union[dict, str]] = None
    triggered_event_id: str
    user_id: str
    replies: list[dict] = Field(default_factory=list, description="若 wait_reply_ms>0 则可能包含 Bot 的回复")


@app.post("/mock/feishu/receive", response_model=ReceiveResponse, tags=["mock"])
async def mock_receive(req: ReceiveRequest):
    """
    模拟"用户给飞书 Bot 发了一条消息"。
    本接口会立即向被测服务的 webhook POST 一个 im.message.receive_v1 事件。
    """
    event = build_receive_message_event(
        user_id=req.user_id,
        text=req.message,
        chat_id=req.chat_id,
        chat_type=req.chat_type,
    )
    body_bytes, headers = build_signed_request(event, config.ENCRYPT_KEY)

    logger.info(
        "→ webhook %s | user_id=%s | text=%r",
        config.TARGET_WEBHOOK_URL,
        req.user_id,
        req.message,
    )

    since_ms = _now_ms()

    try:
        async with httpx.AsyncClient(timeout=config.WEBHOOK_TIMEOUT) as cli:
            r = await cli.post(config.TARGET_WEBHOOK_URL, content=body_bytes, headers=headers)
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=502,
            detail=f"转发 webhook 失败: {type(e).__name__}: {e}",
        )

    # 解析响应
    try:
        resp_body: Union[dict, str] = r.json()
    except Exception:
        resp_body = r.text

    replies: list[dict] = []
    if req.wait_reply_ms > 0:
        replies = await _wait_for_reply(
            user_id=req.user_id,
            since_ms=since_ms,
            timeout_ms=req.wait_reply_ms,
        )

    return ReceiveResponse(
        ok=200 <= r.status_code < 300,
        target_webhook=config.TARGET_WEBHOOK_URL,
        webhook_status=r.status_code,
        webhook_response=resp_body,
        triggered_event_id=event["header"]["event_id"],
        user_id=req.user_id,
        replies=replies,
    )


@app.get("/mock/feishu/sent", tags=["mock"])
async def mock_sent(
    receive_id: Optional[str] = None,
    since_ms: Optional[int] = None,
    limit: int = 100,
):
    """查询 Bot 通过飞书 OpenAPI 发出来的消息（即原本要发回给用户的内容）。"""
    return {"messages": store.list(receive_id=receive_id, since_ms=since_ms, limit=limit)}


@app.delete("/mock/feishu/sent", tags=["mock"])
async def mock_clear(receive_id: Optional[str] = None):
    n = store.clear(receive_id=receive_id)
    return {"cleared": n}


class UrlVerifyRequest(BaseModel):
    challenge: str = "mock-challenge-123"


@app.post("/mock/feishu/url-verify", tags=["mock"])
async def mock_url_verify(req: UrlVerifyRequest):
    """触发一次 url_verification 握手包，验证被测服务首次接入逻辑。"""
    body = build_url_verification_event(req.challenge)
    body_bytes, headers = build_signed_request(body, config.ENCRYPT_KEY)
    async with httpx.AsyncClient(timeout=config.WEBHOOK_TIMEOUT) as cli:
        r = await cli.post(config.TARGET_WEBHOOK_URL, content=body_bytes, headers=headers)
    try:
        data = r.json()
    except Exception:
        data = r.text
    ok = isinstance(data, dict) and data.get("challenge") == req.challenge
    return {
        "ok": ok,
        "status": r.status_code,
        "response": data,
        "expected_challenge": req.challenge,
    }


# ============================================================================
# Part 2: 伪装飞书 OpenAPI（被测服务把消息发到这里）
# ============================================================================


@app.post("/open-apis/auth/v3/tenant_access_token/internal", tags=["feishu-api"])
async def fake_tenant_access_token(request: Request):
    """伪造 tenant_access_token 接口，恒定返回一个 mock token。"""
    body = await _safe_json(request)
    logger.info("← /auth/v3/tenant_access_token/internal body=%s", body)
    return {
        "code": 0,
        "msg": "ok",
        "tenant_access_token": "t-mock-tenant-access-token",
        "expire": 7200,
    }


@app.post("/open-apis/auth/v3/app_access_token/internal", tags=["feishu-api"])
async def fake_app_access_token(request: Request):
    body = await _safe_json(request)
    logger.info("← /auth/v3/app_access_token/internal body=%s", body)
    return {
        "code": 0,
        "msg": "ok",
        "app_access_token": "a-mock-app-access-token",
        "expire": 7200,
    }


@app.post("/open-apis/im/v1/messages", tags=["feishu-api"])
async def fake_send_message(request: Request, receive_id_type: str = "open_id"):
    """
    拦截 Bot 调用飞书 API 发消息的请求，记录下来供测试侧查询。
    真实飞书返回结构参考 https://open.feishu.cn/document/server-docs/im-v1/message/create
    """
    body = await _safe_json(request)
    receive_id = (body or {}).get("receive_id", "")
    msg_type = (body or {}).get("msg_type", "")
    content = (body or {}).get("content", "")

    record = store.add(
        receive_id=receive_id,
        receive_id_type=receive_id_type,
        msg_type=msg_type,
        content=content,
        raw_request=body or {},
    )
    logger.info(
        "← send_message | to=%s (%s) | type=%s | content=%s",
        receive_id, receive_id_type, msg_type, content,
    )

    return {
        "code": 0,
        "msg": "success",
        "data": {
            "message_id": record["message_id"],
            "root_id": "",
            "parent_id": "",
            "msg_type": msg_type,
            "create_time": str(record["create_time_ms"]),
            "update_time": str(record["create_time_ms"]),
            "deleted": False,
            "updated": False,
            "chat_id": receive_id if receive_id_type == "chat_id" else "",
            "sender": {
                "id": config.APP_ID,
                "id_type": "app_id",
                "sender_type": "app",
                "tenant_key": config.TENANT_KEY,
            },
            "body": {"content": content},
        },
    }


@app.post("/open-apis/im/v1/messages/{message_id}/reply", tags=["feishu-api"])
async def fake_reply_message(message_id: str, request: Request):
    body = await _safe_json(request)
    receive_id = f"reply_to:{message_id}"
    msg_type = (body or {}).get("msg_type", "")
    content = (body or {}).get("content", "")
    record = store.add(
        receive_id=receive_id,
        receive_id_type="message_id",
        msg_type=msg_type,
        content=content,
        raw_request=body or {},
    )
    logger.info(
        "← reply | to_msg=%s | type=%s | content=%s",
        message_id, msg_type, content,
    )
    return {
        "code": 0,
        "msg": "success",
        "data": {
            "message_id": record["message_id"],
            "root_id": message_id,
            "parent_id": message_id,
            "msg_type": msg_type,
            "create_time": str(record["create_time_ms"]),
            "body": {"content": content},
        },
    }


# 兜底：把任何其他 /open-apis/* 请求记录一下，返回 code=0，避免被测服务 500
@app.api_route(
    "/open-apis/{full_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    tags=["feishu-api"],
)
async def fake_feishu_catchall(full_path: str, request: Request):
    body = await _safe_json(request)
    logger.warning(
        "← (catchall) %s /open-apis/%s body=%s",
        request.method, full_path, body,
    )
    return {"code": 0, "msg": "ok (mock catchall)", "data": {}}


# ============================================================================
# 工具函数
# ============================================================================


async def _safe_json(request: Request) -> Optional[dict]:
    try:
        raw = await request.body()
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)


async def _wait_for_reply(*, user_id: str, since_ms: int, timeout_ms: int) -> list[dict]:
    """轮询 store，直到出现给该用户的新消息或超时。"""
    import asyncio
    interval = 0.05  # 50ms
    elapsed = 0.0
    timeout_s = timeout_ms / 1000.0
    while elapsed < timeout_s:
        msgs = store.list(receive_id=user_id, since_ms=since_ms, limit=20)
        if msgs:
            return msgs
        await asyncio.sleep(interval)
        elapsed += interval
    return []


@app.get("/", tags=["meta"])
async def root():
    return {
        "service": "feishu-mock",
        "target_webhook": config.TARGET_WEBHOOK_URL,
        "encrypt_enabled": bool(config.ENCRYPT_KEY),
        "docs": "/docs",
    }
