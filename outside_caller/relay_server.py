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
import pathlib
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config
from .anthropic_sse import anthropic_sse_stream
from .api_keys import KeyInfo, manager as key_mgr
from .errors import AnthropicError, OpenAIError, error_handler, validation_error_handler
from .feishu_client import TokenExpiredError, client as feishu
from .models import is_supported, list_models
from .nodes import manager as node_mgr
from .rate_limit import RateLimiter
from .usage import manager as usage_mgr

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("relay")

rate_limiter = RateLimiter(usage_mgr)


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

REFRESH_CHECK_INTERVAL = 1800  # 每 30 分钟检查一次 user_access_token
NODE_GC_INTERVAL = 60          # 每 60s 检查节点心跳
NODE_STALE_AFTER_S = 90        # 90s 没心跳标 stale


async def _token_refresh_loop():
    """后台定时检查 token，提前刷新避免过期。"""
    while True:
        await asyncio.sleep(REFRESH_CHECK_INTERVAL)
        try:
            feishu.maybe_refresh()
        except Exception as e:
            logger.warning("后台 token 刷新失败: %s", e)


async def _node_gc_loop():
    """后台标记长时间没心跳的节点为 stale。"""
    while True:
        await asyncio.sleep(NODE_GC_INTERVAL)
        try:
            node_mgr.gc_stale(stale_after_s=NODE_STALE_AFTER_S)
        except Exception as e:
            logger.warning("节点 GC 失败: %s", e)


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
    logger.info("Nodes: %d 个已知节点", node_mgr.count)

    task_refresh = asyncio.create_task(_token_refresh_loop())
    task_gc = asyncio.create_task(_node_gc_loop())
    logger.info(
        "后台任务已启动：token-refresh(%ds), node-gc(%ds)",
        REFRESH_CHECK_INTERVAL, NODE_GC_INTERVAL,
    )
    yield
    task_refresh.cancel()
    task_gc.cancel()
    for t in (task_refresh, task_gc):
        try:
            await t
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Feishu Relay — OpenAI-compatible API",
    description="通过飞书 Bot 隧道访问内网 Agent",
    version="0.2.0",
    lifespan=_lifespan,
)

# 错误格式（根据路径自动 OpenAI/Anthropic 切换）
app.add_exception_handler(HTTPException, error_handler)

# 422 校验错误也走对应格式
from fastapi.exceptions import RequestValidationError
app.add_exception_handler(RequestValidationError, validation_error_handler)

# Dashboard 静态文件
_DASHBOARD_DIR = pathlib.Path(__file__).parent / "dashboard"
if _DASHBOARD_DIR.exists():
    app.mount(
        "/admin/dashboard/static",
        StaticFiles(directory=_DASHBOARD_DIR / "static"),
        name="dashboard-static",
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
# Pydantic models — Anthropic Messages API
# ============================================================================


class AnthropicMessage(BaseModel):
    """Anthropic 的 message：role + content（content 可以是 string 或 block 数组）。"""
    role: str
    content: Any   # str | List[dict]


class AnthropicTool(BaseModel):
    name: str
    description: Optional[str] = None
    input_schema: dict


DEFAULT_MAX_TOKENS = 4096   # /v1/messages 客户端没传时的默认上限

class MessagesRequest(BaseModel):
    model: str
    messages: List[AnthropicMessage]
    max_tokens: Optional[int] = None   # 缺省时自动填 DEFAULT_MAX_TOKENS
    system: Optional[Any] = None      # str | List[dict]
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    stop_sequences: Optional[List[str]] = None
    stream: bool = False
    tools: Optional[List[AnthropicTool]] = None
    tool_choice: Optional[dict] = None
    metadata: Optional[dict] = None


# ============================================================================
# 鉴权
# ============================================================================


def _extract_key(request: Request) -> str:
    """从 Authorization 或 x-api-key (Anthropic SDK) header 提取 key。"""
    # Anthropic SDK 默认用 x-api-key header
    xkey = request.headers.get("x-api-key", "")
    if xkey:
        return xkey
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    raise OpenAIError("authentication_error", "Missing Authorization header", status=401)


def _check_auth(request: Request) -> KeyInfo:
    """验证 API key，返回 KeyInfo。"""
    key = _extract_key(request)
    info = key_mgr.validate(key)
    if not info:
        raise OpenAIError("authentication_error", "Invalid API key",
                          status=401, code="invalid_api_key")
    return info


def _check_admin(request: Request) -> KeyInfo:
    """验证 admin key。"""
    info = _check_auth(request)
    if not info.is_admin:
        raise OpenAIError("permission_error", "Admin key required",
                          status=403, code="admin_required")
    return info


# ============================================================================
# 核心接口
# ============================================================================


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest, request: Request):
    key_info = _check_auth(request)
    t0 = time.time()

    # 校验模型在白名单
    if not is_supported(req.model):
        raise OpenAIError(
            "invalid_request_error",
            f"Unsupported model: {req.model}. Supported: {list_models()}",
            status=400, code="model_not_found", param="model",
        )

    if not req.messages:
        raise OpenAIError("invalid_request_error", "messages cannot be empty",
                          status=400, param="messages")

    # 限流检查
    if key_info.rpm_limit:
        ok, retry = rate_limiter.check_rpm(key_info.name, key_info.rpm_limit)
        if not ok:
            raise OpenAIError(
                "rate_limit_error",
                f"Rate limit ({key_info.rpm_limit}/min) exceeded, retry in {retry}s",
                status=429, code="rate_limit_exceeded", retry_after=retry,
            )

    if key_info.daily_token_limit:
        used = usage_mgr.daily_token_count(key_info.name)
        if used >= key_info.daily_token_limit:
            raise OpenAIError(
                "rate_limit_error",
                f"Daily token quota exhausted ({used}/{key_info.daily_token_limit})",
                status=429, code="daily_quota_exceeded",
            )

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

    logger.info("→ [%s] [%s] req_id=%s msgs=%d stream=%s last=%s",
                key_info.name, req.model, req_id, len(req.messages),
                req.stream, last_user[:60])

    before_ms = int(time.time() * 1000)

    # 发 JSON 消息到 bot
    try:
        await feishu.send_message(json.dumps(payload, ensure_ascii=False))
    except TokenExpiredError as e:
        usage_mgr.record_failed(key_info.name, req.model)
        _log_access(key_info.name, req.model, 401, time.time() - t0, last_user)
        raise OpenAIError("authentication_error", str(e), status=401)
    except RuntimeError as e:
        usage_mgr.record_failed(key_info.name, req.model)
        _log_access(key_info.name, req.model, 502, time.time() - t0, last_user)
        raise OpenAIError("api_error", str(e), status=502)

    # 按 req_id 轮询 bot 响应
    reply = await feishu.poll_reply_by_req_id(req_id, after_ms=before_ms)
    if reply is None:
        usage_mgr.record_failed(key_info.name, req.model)
        _log_access(key_info.name, req.model, 504, time.time() - t0, last_user)
        raise OpenAIError(
            "api_error",
            f"Bot did not respond within {config.POLL_TIMEOUT_S}s (req_id={req_id})",
            status=504,
        )

    duration = time.time() - t0

    if not reply.get("ok"):
        usage_mgr.record_failed(key_info.name, req.model)
        status = reply.get("status", 502)
        msg = reply.get("message", "upstream error")
        err_type = "rate_limit_error" if status == 429 else "api_error"
        logger.warning("← [%s] [%s] req_id=%s FAIL status=%d msg=%s",
                       key_info.name, req.model, req_id, status, msg[:120])
        _log_access(key_info.name, req.model, status, duration, last_user)
        raise OpenAIError(err_type, msg, status=status)

    content = reply.get("content", "")
    usage_dict = reply.get("usage") or {}
    finish_reason = reply.get("finish_reason", "stop")
    p_tok = usage_dict.get("prompt_tokens", 0)
    c_tok = usage_dict.get("completion_tokens", 0)
    t_tok = usage_dict.get("total_tokens", p_tok + c_tok)

    # 记录用量（成功）
    usage_mgr.record(key_info.name, req.model, p_tok, c_tok)

    logger.info("← [%s] [%s] req_id=%s %.1fs tokens=%d/%d %s",
                key_info.name, req.model, req_id, duration, p_tok, c_tok,
                content[:60])
    _log_access(key_info.name, req.model, 200, duration, last_user)

    # ----- 流式分支（伪流式） -----
    if req.stream:
        return StreamingResponse(
            _sse_stream(req_id, req.model, content, finish_reason),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",      # nginx 关 buffering
            },
        )

    # ----- 普通响应 -----
    return ChatResponse(
        id=f"chatcmpl-{req_id}",
        created=int(time.time()),
        model=req.model,
        choices=[Choice(
            message=ChoiceMessage(content=content),
            finish_reason=finish_reason,
        )],
        usage=Usage(prompt_tokens=p_tok, completion_tokens=c_tok, total_tokens=t_tok),
    )


# ----- SSE chunk emit -----

def _sse_chunk(req_id: str, model: str, delta: dict, finish_reason: Optional[str]) -> str:
    """构造一条 OpenAI 格式的 SSE chunk。"""
    obj = {
        "id": f"chatcmpl-{req_id}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    }
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _split_for_stream(text: str, chunk_chars: int = 12) -> list:
    """把完整文本切成小块用于"伪流式"。"""
    if not text:
        return [""]
    return [text[i:i + chunk_chars] for i in range(0, len(text), chunk_chars)]


async def _sse_stream(req_id: str, model: str, content: str, finish_reason: str):
    """SSE 生成器：role chunk + N 个 content chunk + finish chunk + [DONE]。"""
    # 1) role chunk
    yield _sse_chunk(req_id, model, {"role": "assistant"}, None)
    await asyncio.sleep(0.01)

    # 2) content chunks
    chunks = _split_for_stream(content, chunk_chars=12)
    for piece in chunks:
        yield _sse_chunk(req_id, model, {"content": piece}, None)
        await asyncio.sleep(0.02)   # 每块 20ms，模拟流式

    # 3) finish chunk
    yield _sse_chunk(req_id, model, {}, finish_reason)
    yield "data: [DONE]\n\n"


# ============================================================================
# /v1/messages —— Anthropic 原生协议入口
# ============================================================================


@app.post("/v1/messages")
async def messages_endpoint(req: MessagesRequest, request: Request):
    """Anthropic Messages API 入口。仅 Claude 系模型。"""
    key_info = _check_auth(request)
    t0 = time.time()

    # 1. 模型限制：仅 Claude
    if not req.model.startswith("claude-"):
        raise AnthropicError(
            "invalid_request_error",
            f"/v1/messages only accepts Claude models, got: {req.model}",
            status=400,
        )
    if not is_supported(req.model):
        raise AnthropicError(
            "invalid_request_error",
            f"Unsupported model: {req.model}. Supported Claude: "
            + ", ".join(m for m in list_models() if m.startswith("claude-")),
            status=400,
        )

    # 2. 限流
    if key_info.rpm_limit:
        ok, retry = rate_limiter.check_rpm(key_info.name, key_info.rpm_limit)
        if not ok:
            raise AnthropicError(
                "rate_limit_error",
                f"Rate limit ({key_info.rpm_limit}/min) exceeded, retry in {retry}s",
                status=429, retry_after=retry,
            )
    if key_info.daily_token_limit:
        used = usage_mgr.daily_token_count(key_info.name)
        if used >= key_info.daily_token_limit:
            raise AnthropicError(
                "rate_limit_error",
                f"Daily token quota exhausted ({used}/{key_info.daily_token_limit})",
                status=429,
            )

    # 3. 构造 relay 协议（messages_native 模式）
    req_id = uuid.uuid4().hex[:24]
    payload = {
        "_relay_v": 1,
        "req_id": req_id,
        "mode": "messages_native",
        **req.model_dump(exclude_none=True),
    }
    # MP /v1/messages 要求 max_tokens 必填
    if not payload.get("max_tokens"):
        payload["max_tokens"] = DEFAULT_MAX_TOKENS
    # stream 由 relay 端伪流，bot 端不需要
    payload.pop("stream", None)

    # 截一段 user 文本用日志
    last_user = ""
    if req.messages:
        last = req.messages[-1]
        c = last.content
        if isinstance(c, str):
            last_user = c
        elif isinstance(c, list):
            for blk in c:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    last_user = blk.get("text", "")
                    break

    logger.info("→ [%s] [%s] req_id=%s mode=anthropic msgs=%d stream=%s last=%s",
                key_info.name, req.model, req_id, len(req.messages),
                req.stream, last_user[:60])

    before_ms = int(time.time() * 1000)

    # 4. 发飞书 + 轮询
    try:
        await feishu.send_message(json.dumps(payload, ensure_ascii=False))
    except TokenExpiredError as e:
        usage_mgr.record_failed(key_info.name, req.model)
        _log_access(key_info.name, req.model, 401, time.time() - t0, last_user)
        raise AnthropicError("authentication_error", str(e), status=401)
    except RuntimeError as e:
        usage_mgr.record_failed(key_info.name, req.model)
        _log_access(key_info.name, req.model, 502, time.time() - t0, last_user)
        raise AnthropicError("api_error", str(e), status=502)

    reply = await feishu.poll_reply_by_req_id(req_id, after_ms=before_ms)
    if reply is None:
        usage_mgr.record_failed(key_info.name, req.model)
        _log_access(key_info.name, req.model, 504, time.time() - t0, last_user)
        raise AnthropicError(
            "api_error",
            f"Bot did not respond within {config.POLL_TIMEOUT_S}s (req_id={req_id})",
            status=504,
        )

    duration = time.time() - t0

    if not reply.get("ok"):
        usage_mgr.record_failed(key_info.name, req.model)
        status = reply.get("status", 502)
        msg = reply.get("message", "upstream error")
        err_type = "rate_limit_error" if status == 429 else "api_error"
        if status == 401:
            err_type = "authentication_error"
        elif status == 400:
            err_type = "invalid_request_error"
        logger.warning("← [%s] [%s] req_id=%s FAIL status=%d msg=%s",
                       key_info.name, req.model, req_id, status, msg[:120])
        _log_access(key_info.name, req.model, status, duration, last_user)
        raise AnthropicError(err_type, msg, status=status)

    raw = reply.get("raw_anthropic") or {}
    if not raw or "content" not in raw:
        usage_mgr.record_failed(key_info.name, req.model)
        raise AnthropicError("api_error", "Empty/invalid response from bot",
                             status=502)

    # 5. 记录用量
    rusage = raw.get("usage") or {}
    in_tok = rusage.get("input_tokens", 0)
    out_tok = rusage.get("output_tokens", 0)
    usage_mgr.record(key_info.name, req.model, in_tok, out_tok)

    logger.info("← [%s] [%s] req_id=%s %.1fs in/out=%d/%d stop=%s",
                key_info.name, req.model, req_id, duration,
                in_tok, out_tok, raw.get("stop_reason"))
    _log_access(key_info.name, req.model, 200, duration, last_user)

    # 6. 响应：流式 or 非流式
    if req.stream:
        return StreamingResponse(
            anthropic_sse_stream(raw),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # 非流式直接返回 raw（即 MP 的原 Anthropic 响应）
    return raw


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
                "endpoints": (
                    ["/v1/chat/completions", "/v1/messages"]
                    if name.startswith("claude-")
                    else ["/v1/chat/completions"]
                ),
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
                "key": k.key,                       # admin 自己看可以拿完整 key
                "key_prefix": k.key[:12] + "***",
                "name": k.name,
                "enabled": k.enabled,
                "is_admin": k.is_admin,
                "rpm_limit": k.rpm_limit,
                "daily_token_limit": k.daily_token_limit,
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
    raise OpenAIError("not_found_error", "Key not found", status=404)


class PatchKeyRequest(BaseModel):
    rpm_limit: Optional[int] = None
    daily_token_limit: Optional[int] = None
    clear_rpm: bool = False
    clear_daily: bool = False
    enabled: Optional[bool] = None


@app.patch("/admin/keys/{key}")
async def admin_patch_key(key: str, req: PatchKeyRequest, request: Request):
    _check_admin(request)

    if key not in key_mgr._keys:
        raise OpenAIError("not_found_error", "Key not found", status=404)

    if req.enabled is True:
        key_mgr.enable_key(key)
    elif req.enabled is False:
        key_mgr.revoke_key(key)

    if (
        req.rpm_limit is not None
        or req.daily_token_limit is not None
        or req.clear_rpm
        or req.clear_daily
    ):
        key_mgr.set_limits(
            key,
            rpm_limit=req.rpm_limit,
            daily_token_limit=req.daily_token_limit,
            clear_rpm=req.clear_rpm,
            clear_daily=req.clear_daily,
        )

    info = key_mgr._keys[key]
    return {
        "key_prefix": info.key[:12] + "***",
        "name": info.name,
        "enabled": info.enabled,
        "is_admin": info.is_admin,
        "rpm_limit": info.rpm_limit,
        "daily_token_limit": info.daily_token_limit,
    }


@app.get("/admin/keys/{key}/usage")
async def admin_key_usage(key: str, request: Request):
    _check_admin(request)
    info = key_mgr._keys.get(key)
    if not info:
        raise OpenAIError("not_found_error", "Key not found", status=404)

    stats = usage_mgr.get(info.name)
    daily = usage_mgr.daily_token_count(info.name)
    current_rpm = rate_limiter.rpm_current(info.name)

    if stats is None:
        return {
            "key_prefix": info.key[:12] + "***",
            "name": info.name,
            "total_requests": 0,
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_tokens": 0,
            "last_used_at": "",
            "by_model": {},
            "by_day": {},
            "daily_token_count_today": 0,
            "current_rpm": current_rpm,
            "rpm_limit": info.rpm_limit,
            "daily_token_limit": info.daily_token_limit,
        }

    return {
        "key_prefix": info.key[:12] + "***",
        "name": info.name,
        "total_requests": stats.total_requests,
        "total_prompt_tokens": stats.total_prompt_tokens,
        "total_completion_tokens": stats.total_completion_tokens,
        "total_tokens": stats.total_tokens,
        "last_used_at": stats.last_used_at,
        "by_model": stats.by_model,
        "by_day": stats.by_day,
        "daily_token_count_today": daily,
        "current_rpm": current_rpm,
        "rpm_limit": info.rpm_limit,
        "daily_token_limit": info.daily_token_limit,
    }


@app.get("/admin/usage")
async def admin_usage_summary(request: Request):
    _check_admin(request)
    today = usage_mgr.global_today()
    all_stats = usage_mgr.all()

    per_key = []
    for key_info in key_mgr.list_keys():
        s = all_stats.get(key_info.name)
        per_key.append({
            "key": key_info.key,                       # admin 看完整 key
            "key_prefix": key_info.key[:12] + "***",
            "name": key_info.name,
            "enabled": key_info.enabled,
            "is_admin": key_info.is_admin,
            "rpm_limit": key_info.rpm_limit,
            "daily_token_limit": key_info.daily_token_limit,
            "total_requests": s.total_requests if s else 0,
            "total_tokens": s.total_tokens if s else 0,
            "today_tokens": usage_mgr.daily_token_count(key_info.name),
            "current_rpm": rate_limiter.rpm_current(key_info.name),
            "last_used_at": s.last_used_at if s else "",
        })

    return {
        "today": today,
        "keys": per_key,
    }


# ============================================================================
# Agent 接口（节点 bot 上报）
# ============================================================================


class HeartbeatRequest(BaseModel):
    """节点 bot 的心跳上报。"""
    node_id: str
    version: Optional[str] = ""
    hostname: Optional[str] = ""
    ip: Optional[str] = ""
    started_at: Optional[str] = ""
    status: Optional[str] = "online"
    bots: Optional[List[Dict[str, Any]]] = None
    models: Optional[List[str]] = None
    upstream: Optional[Dict[str, Any]] = None
    stats: Optional[Dict[str, Any]] = None


class AgentIdRequest(BaseModel):
    node_id: str


@app.post("/agent/heartbeat")
async def agent_heartbeat(req: HeartbeatRequest, request: Request):
    """节点 bot 上报心跳（公开接口，不需要 API key）。"""
    rec = node_mgr.upsert(req.model_dump(exclude_none=False))
    return {
        "status": "ok",
        "node_id": rec.node_id,
        "first_seen_at": rec.first_seen_at,
        "heartbeats_count": rec.heartbeats_count,
        "center_message": None,
    }


@app.post("/agent/offline")
async def agent_offline(req: AgentIdRequest):
    """节点 bot 优雅下线通知。"""
    ok = node_mgr.mark_offline(req.node_id, reason="client")
    return {"status": "ok" if ok else "not_found", "node_id": req.node_id}


@app.get("/admin/nodes")
async def admin_list_nodes(request: Request):
    """Admin 查节点列表。"""
    _check_admin(request)
    nodes = node_mgr.list_all()
    online_count = sum(1 for n in nodes if n.status == "online")
    return {
        "total": len(nodes),
        "online": online_count,
        "nodes": [
            {
                "node_id": n.node_id,
                "version": n.version,
                "hostname": n.hostname,
                "ip": n.ip,
                "started_at": n.started_at,
                "first_seen_at": n.first_seen_at,
                "last_heartbeat_at": n.last_heartbeat_at,
                "status": n.status,
                "bots": n.bots,
                "models": n.models,
                "upstream": n.upstream,
                "stats": n.stats,
                "heartbeats_count": n.heartbeats_count,
            }
            for n in nodes
        ],
    }


# ============================================================================
# Dashboard
# ============================================================================


@app.get("/admin/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def admin_dashboard():
    """Web 管理控制台。"""
    html_file = _DASHBOARD_DIR / "index.html"
    if not html_file.exists():
        return HTMLResponse("<h1>Dashboard not deployed</h1>", status_code=503)
    return HTMLResponse(html_file.read_text(encoding="utf-8"))
