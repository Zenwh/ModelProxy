"""
Feishu-as-Tunnel LLM Relay Service
====================================

把飞书 Bot 当隧道，包装成 OpenAI-compatible 的 /v1/chat/completions API。

    外网客户端
      → POST /v1/chat/completions (Bearer sk-relay-xxx)
      → Relay 以 zen 的身份给阿月老师发飞书消息
      → 内网 Agent 处理（调 Model Proxy Claude）
      → Relay 轮询拿到回复
      → 返回 OpenAI 格式响应

启动：
    .venv-mock/bin/uvicorn outside_caller.relay_server:app --host 0.0.0.0 --port 9100
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from . import config
from .api_keys import KeyInfo, manager as key_mgr
from .feishu_client import TokenExpiredError, client as feishu
from .models import is_supported, list_models

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("relay")


# ============================================================================
# 访问日志
# ============================================================================

_access_logger: Optional[logging.Logger] = None


def _init_access_log():
    global _access_logger
    os.makedirs(os.path.dirname(config.ACCESS_LOG_FILE), exist_ok=True)
    _access_logger = logging.getLogger("access")
    _access_logger.setLevel(logging.INFO)
    _access_logger.propagate = False
    handler = logging.FileHandler(config.ACCESS_LOG_FILE, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    _access_logger.addHandler(handler)


def _log_access(
    key_name: str,
    model: str,
    status: int,
    duration_s: float,
    user_text: str,
):
    if _access_logger:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        snippet = user_text[:60].replace("\n", " ")
        _access_logger.info(
            '%s | %s | %s | %d | %.1fs | "%s"',
            ts, key_name, model, status, duration_s, snippet,
        )


# ============================================================================
# 后台 token 刷新
# ============================================================================

REFRESH_CHECK_INTERVAL = 1800  # 每 30 分钟检查一次


async def _token_refresh_loop():
    """后台定时检查 token，提前刷新避免过期。"""
    while True:
        await asyncio.sleep(REFRESH_CHECK_INTERVAL)
        try:
            feishu.maybe_refresh()
        except Exception as e:
            logger.warning("后台 token 刷新失败: %s", e)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _init_access_log()
    logger.info(
        "token 状态: remaining=%.0fs refresh_token=%s",
        feishu.token_remaining_s,
        "有" if feishu.has_refresh_token else "无",
    )
    logger.info(
        "API Keys: %d 个（%d 活跃）",
        key_mgr.key_count, key_mgr.active_count,
    )
    task = asyncio.create_task(_token_refresh_loop())
    logger.info("后台 token 刷新 loop 已启动（间隔 %ds）", REFRESH_CHECK_INTERVAL)
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="Feishu Relay — OpenAI-compatible API",
    description="通过飞书 Bot 隧道访问内网 Agent",
    version="0.2.0",
    lifespan=_lifespan,
)


# ============================================================================
# Pydantic models — OpenAI Chat Completions 格式
# ============================================================================


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str = "feishu/default"
    messages: List[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


class ChoiceMessage(BaseModel):
    role: str = "assistant"
    content: str


class Choice(BaseModel):
    index: int = 0
    message: ChoiceMessage
    finish_reason: str = "stop"


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[Choice]
    usage: Usage = Field(default_factory=Usage)


# ============================================================================
# 鉴权
# ============================================================================


def _extract_key(request: Request) -> str:
    """从 Authorization header 提取 key 字符串。"""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    return auth[7:]


def _check_auth(request: Request) -> KeyInfo:
    """验证 API key，返回 KeyInfo。"""
    key = _extract_key(request)
    info = key_mgr.validate(key)
    if not info:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return info


def _check_admin(request: Request) -> KeyInfo:
    """验证 admin key。"""
    info = _check_auth(request)
    if not info.is_admin:
        raise HTTPException(status_code=403, detail="Admin key required")
    return info


# ============================================================================
# 核心接口
# ============================================================================


@app.post("/v1/chat/completions", response_model=ChatResponse)
async def chat_completions(req: ChatRequest, request: Request):
    key_info = _check_auth(request)
    t0 = time.time()

    if req.stream:
        raise HTTPException(
            status_code=400,
            detail="stream=true 暂不支持（飞书轮询模式无法流式）",
        )

    # 校验模型在白名单
    if not is_supported(req.model):
        raise HTTPException(
            status_code=400,
            detail=f"unsupported model: {req.model}. 支持的模型: {list_models()}",
        )

    if not req.messages:
        raise HTTPException(status_code=400, detail="messages 不能为空")

    # 摘取最后一条 user 消息用于日志
    last_user = ""
    for m in reversed(req.messages):
        if m.role == "user":
            last_user = m.content
            break

    # 构造 relay 协议 payload
    req_id = uuid.uuid4().hex[:24]
    payload = {
        "_relay_v": 1,
        "req_id": req_id,
        "model": req.model,
        "messages": [m.model_dump() for m in req.messages],
    }
    if req.temperature is not None:
        payload["temperature"] = req.temperature
    if req.max_tokens is not None:
        payload["max_tokens"] = req.max_tokens

    logger.info("→ [%s] [%s] req_id=%s msgs=%d last=%s",
                key_info.name, req.model, req_id, len(req.messages), last_user[:60])

    before_ms = int(time.time() * 1000)

    # 发 JSON 消息到 bot
    try:
        await feishu.send_message(json.dumps(payload, ensure_ascii=False))
    except TokenExpiredError as e:
        _log_access(key_info.name, req.model, 401, time.time() - t0, last_user)
        raise HTTPException(status_code=401, detail=str(e))
    except RuntimeError as e:
        _log_access(key_info.name, req.model, 502, time.time() - t0, last_user)
        raise HTTPException(status_code=502, detail=str(e))

    # 按 req_id 轮询 bot 响应
    reply = await feishu.poll_reply_by_req_id(req_id, after_ms=before_ms)
    if reply is None:
        _log_access(key_info.name, req.model, 504, time.time() - t0, last_user)
        raise HTTPException(
            status_code=504,
            detail=f"Bot 在 {config.POLL_TIMEOUT_S}s 内没有匹配 req_id={req_id} 的回复",
        )

    duration = time.time() - t0

    if not reply.get("ok"):
        status = reply.get("status", 502)
        msg = reply.get("message", "upstream error")
        logger.warning("← [%s] [%s] req_id=%s FAIL status=%d msg=%s",
                       key_info.name, req.model, req_id, status, msg[:120])
        _log_access(key_info.name, req.model, status, duration, last_user)
        raise HTTPException(status_code=status, detail=msg)

    content = reply.get("content", "")
    usage_dict = reply.get("usage") or {}
    finish_reason = reply.get("finish_reason", "stop")

    logger.info("← [%s] [%s] req_id=%s %.1fs tokens=%d/%d %s",
                key_info.name, req.model, req_id, duration,
                usage_dict.get("prompt_tokens", 0),
                usage_dict.get("completion_tokens", 0),
                content[:60])
    _log_access(key_info.name, req.model, 200, duration, last_user)

    return ChatResponse(
        id=f"chatcmpl-{req_id}",
        created=int(time.time()),
        model=req.model,
        choices=[Choice(
            message=ChoiceMessage(content=content),
            finish_reason=finish_reason,
        )],
        usage=Usage(
            prompt_tokens=usage_dict.get("prompt_tokens", 0),
            completion_tokens=usage_dict.get("completion_tokens", 0),
            total_tokens=usage_dict.get("total_tokens", 0),
        ),
    )


# ============================================================================
# 辅助接口
# ============================================================================


@app.get("/v1/models")
async def list_models_endpoint(request: Request):
    _check_auth(request)
    return {
        "object": "list",
        "data": [
            {
                "id": name,
                "object": "model",
                "created": 0,
                "owned_by": "stepfun-relay",
            }
            for name in list_models()
        ],
    }


@app.get("/health")
async def health():
    token_ok = False
    remaining = 0
    try:
        feishu.get_token()
        token_ok = True
        remaining = feishu.token_remaining_s
    except TokenExpiredError:
        pass
    return {
        "status": "ok" if token_ok else "token_expired",
        "token_remaining_s": int(remaining),
        "refresh_token_available": feishu.has_refresh_token,
        "api_keys_total": key_mgr.key_count,
        "api_keys_active": key_mgr.active_count,
        "relay_port": config.RELAY_PORT,
    }


@app.get("/")
async def root():
    return {
        "service": "feishu-relay",
        "version": "0.2.0",
        "description": "OpenAI-compatible API via Feishu Bot tunnel",
        "endpoints": {
            "chat": "POST /v1/chat/completions",
            "models": "GET /v1/models",
            "health": "GET /health",
            "admin_keys": "GET/POST/DELETE /admin/keys (admin only)",
        },
    }


# ============================================================================
# Admin 接口 — API Key 管理
# ============================================================================


class CreateKeyRequest(BaseModel):
    name: str
    is_admin: bool = False


@app.post("/admin/keys")
async def admin_create_key(req: CreateKeyRequest, request: Request):
    _check_admin(request)
    info = key_mgr.create_key(req.name, is_admin=req.is_admin)
    return {
        "key": info.key,
        "name": info.name,
        "is_admin": info.is_admin,
        "created_at": info.created_at,
    }


@app.get("/admin/keys")
async def admin_list_keys(request: Request):
    _check_admin(request)
    keys = key_mgr.list_keys()
    return {
        "total": len(keys),
        "keys": [
            {
                "key_prefix": k.key[:12] + "***",
                "name": k.name,
                "enabled": k.enabled,
                "is_admin": k.is_admin,
                "created_at": k.created_at,
            }
            for k in keys
        ],
    }


@app.delete("/admin/keys/{key}")
async def admin_revoke_key(key: str, request: Request):
    _check_admin(request)
    if key_mgr.revoke_key(key):
        return {"status": "revoked", "key_prefix": key[:12] + "***"}
    raise HTTPException(status_code=404, detail="Key not found")
