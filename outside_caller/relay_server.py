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
import hashlib
import hmac
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
from .api_keys import KeyInfo, manager as key_mgr
from .bot_pool import close_http_client, pool as bot_pool
from .errors import AnthropicError, OpenAIError, error_handler, validation_error_handler
from .feishu_token import TokenExpiredError, token_mgr
from .models import is_supported, list_models, to_endpoint
from .rate_limit import RateLimiter
from .relay_codec import PayloadTooLargeError
from .slot_pool import slot_pool
from .stream_router import openai_stream_from_worker, anthropic_stream_from_worker
from .token_estimator import estimate as estimate_tokens
from .truncator import truncate as truncate_messages
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


def _extract_user_text(content) -> str:
    """从 message content（str 或 list）提取纯文本用于日志。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") == "image_url":
                    parts.append("[image]")
            elif isinstance(item, str):
                parts.append(item)
        return " ".join(parts)
    return str(content)


def _log_access(
    key_name: str,
    model: str,
    status: int,
    duration_s: float,
    user_text: Any,
):
    if _access_logger:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        snippet = _extract_user_text(user_text)[:60].replace("\n", " ")
        _access_logger.info(
            '%s | %s | %s | %d | %.1fs | "%s"',
            ts, key_name, model, status, duration_s, snippet,
        )


# ============================================================================
# 后台 token 刷新
# ============================================================================

REFRESH_CHECK_INTERVAL = 1800  # 每 30 分钟检查一次 user_access_token


async def _token_refresh_loop():
    """后台定时检查 token，提前刷新避免过期。"""
    while True:
        await asyncio.sleep(REFRESH_CHECK_INTERVAL)
        try:
            token_mgr.maybe_refresh()
        except Exception as e:
            logger.warning("后台 token 刷新失败: %s", e)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _init_access_log()
    logger.info(
        "token 状态: remaining=%.0fs refresh_token=%s",
        token_mgr.token_remaining_s,
        "有" if token_mgr.has_refresh_token else "无",
    )
    logger.info(
        "API Keys: %d 个（%d 活跃）",
        key_mgr.key_count, key_mgr.active_count,
    )
    logger.info("Bot Pool: %d 个节点", bot_pool.count)

    task_refresh = asyncio.create_task(_token_refresh_loop())
    task_hb_poll = asyncio.create_task(bot_pool.run_heartbeat_poller())
    logger.info("后台任务已启动：token-refresh(%ds), heartbeat-poller(30s)", REFRESH_CHECK_INTERVAL)
    yield
    task_refresh.cancel()
    task_hb_poll.cancel()
    try:
        await task_refresh
    except asyncio.CancelledError:
        pass
    try:
        await task_hb_poll
    except asyncio.CancelledError:
        pass
    try:
        await close_http_client()
    except Exception:
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
    content: Any  # str | List[dict] for vision/image_url support


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


# ---- Agent 心跳鉴权（X-Agent-Secret HMAC 固定密码 / HMAC-SHA256，取其一） -----

_AGENT_HMAC_HEADER = "X-Agent-Secret"


async def _verify_agent(request: Request) -> None:
    """
    Verify X-Agent-Secret.

    Two accepted modes:
    1. Agent sends AGENT_SECRET in plain: header == config.AGENT_SECRET.
       Fastest, no body read needed; good for internal/trusted tunnels.
    2. Agent sends HMAC-SHA256 of body: hex digest must be
       hmac.compare_digest(sent, hmac.new(AGENT_SECRET, body, sha256).hexdigest()).
       Body must contain a fresh "timestamp" (Unix seconds, ±60s) to prevent replay.
    """
    provided = request.headers.get(_AGENT_HMAC_HEADER, "")
    if not provided:
        raise HTTPException(status_code=401, detail="missing X-Agent-Secret")

    # Mode 1: plain shared-secret comparison (constant-time)
    if hmac.compare_digest(provided, config.AGENT_SECRET):
        return

    # Mode 2: HMAC-SHA256 of request body
    body: bytes = await request.body()
    expected = hmac.new(
        config.AGENT_SECRET.encode("utf-8"), body, hashlib.sha256,
    ).hexdigest()
    if hmac.compare_digest(provided, expected):
        # Replay protection: require a fresh timestamp in body
        try:
            payload = json.loads(body)
            ts = int(payload.get("timestamp", 0))
            now = int(time.time())
            if abs(now - ts) <= 60:
                return
        except Exception:
            pass
        raise HTTPException(status_code=403, detail="request expired or missing timestamp")

    raise HTTPException(status_code=403, detail="invalid X-Agent-Secret")


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
            last_user = _extract_user_text(m.content)
            break

    # 构造 relay 协议 payload
    # model 传 public 名透传给 bot，由 bot 端 ModelRegistry 翻译成上游名 + 路由到对应 endpoint
    req_id = uuid.uuid4().hex[:24]
    endpoint = to_endpoint(req.model) or "chat"

    # v3：超过 MAX_INPUT_TOKENS 时中间截断
    raw_messages = [m.model_dump() for m in req.messages]
    est_in = estimate_tokens(raw_messages)
    if est_in > config.MAX_INPUT_TOKENS:
        try:
            truncated, info = truncate_messages(
                raw_messages,
                system=None,
                tools=None,
                budget=config.MAX_INPUT_TOKENS,
            )
        except OpenAIError:
            usage_mgr.record_failed(key_info.name, req.model)
            _log_access(key_info.name, req.model, 413, time.time() - t0, last_user)
            raise
        logger.info(
            "[%s] req_id=%s truncated: kept=%d dropped=%d est_in=%d → %d",
            key_info.name, req_id, info["kept_count"], info["dropped_count"],
            est_in, info["estimated_tokens"],
        )
        raw_messages = truncated

    logger.info("→ [%s] [%s] req_id=%s msgs=%d stream=%s last=%s",
                key_info.name, req.model, req_id, len(raw_messages),
                req.stream, last_user[:60])

    before_ms = int(time.time() * 1000)

    # 选一个 bot
    node = bot_pool.select()
    if not node:
        usage_mgr.record_failed(key_info.name, req.model)
        _log_access(key_info.name, req.model, 503, time.time() - t0, last_user)
        raise OpenAIError("api_error", "No available bot nodes", status=503)
    bot_pool.record_request(node)

    # v3 envelope
    payload: Dict[str, Any] = {
        "_relay_v": 3,
        "type": "req",
        "req_id": req_id,
        "model": req.model,
        "endpoint": endpoint,
        "mode": None,
        "stream": bool(req.stream),
        "messages": raw_messages,
    }
    if req.temperature is not None:
        payload["temperature"] = req.temperature
    if req.max_tokens is not None:
        payload["max_tokens"] = req.max_tokens

    # v3 上行：拆包发送 req_part
    try:
        await bot_pool.send_request(node, payload)
    except PayloadTooLargeError as e:
        # 单 part 仍超限 → 切片大小有问题，理论不该发生
        usage_mgr.record_failed(key_info.name, req.model)
        _log_access(key_info.name, req.model, 413, time.time() - t0, last_user)
        raise OpenAIError(
            "invalid_request_error",
            f"Multipart envelope still oversized ({e.size_kb:.0f}KB).",
            status=413, param="messages",
        )
    except TokenExpiredError as e:
        usage_mgr.record_failed(key_info.name, req.model)
        _log_access(key_info.name, req.model, 401, time.time() - t0, last_user)
        raise OpenAIError("authentication_error", str(e), status=401)
    except RuntimeError as e:
        usage_mgr.record_failed(key_info.name, req.model)
        _log_access(key_info.name, req.model, 502, time.time() - t0, last_user)
        raise OpenAIError("api_error", str(e), status=502)

    # 流式：实时 SSE（gateway 边轮询边推）
    if req.stream:
        return StreamingResponse(
            openai_stream_from_worker(
                node=node,
                req_id=req_id,
                model=req.model,
                after_ms=before_ms,
                key_name=key_info.name,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # 非流式：轮询直到 v3 resp
    reply = await bot_pool.poll_reply_by_req_id(node, req_id, after_ms=before_ms)
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

    # messages_native 模式：从 raw_anthropic 提取内容
    if reply.get("mode") == "messages_native" and reply.get("raw_anthropic"):
        raw = reply["raw_anthropic"]
        parts = []
        for blk in raw.get("content", []):
            if blk.get("type") == "text":
                parts.append(blk.get("text", ""))
        content = "".join(parts)
        au = raw.get("usage") or {}
        usage_dict = {
            "prompt_tokens": au.get("input_tokens", 0),
            "completion_tokens": au.get("output_tokens", 0),
            "total_tokens": au.get("input_tokens", 0) + au.get("output_tokens", 0),
        }
        finish_reason = raw.get("stop_reason", "stop")
        if finish_reason == "end_turn":
            finish_reason = "stop"

    p_tok = usage_dict.get("prompt_tokens", 0)
    c_tok = usage_dict.get("completion_tokens", 0)
    t_tok = usage_dict.get("total_tokens", p_tok + c_tok)

    # 记录用量（成功）
    usage_mgr.record(key_info.name, req.model, p_tok, c_tok)
    bot_pool.record_usage(node, p_tok, c_tok)

    logger.info("← [%s] [%s] req_id=%s %.1fs tokens=%d/%d %s",
                key_info.name, req.model, req_id, duration, p_tok, c_tok,
                content[:60])
    _log_access(key_info.name, req.model, 200, duration, last_user)

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


# ----- SSE chunk emit (helpers shared with stream_router) -----

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

    # 3. 构造 relay 协议
    req_id = uuid.uuid4().hex[:24]
    req_data = req.model_dump(exclude_none=True)
    # stream 字段透传给 bot；max_tokens 兜底
    if not req_data.get("max_tokens"):
        req_data["max_tokens"] = DEFAULT_MAX_TOKENS

    # v3：超过 MAX_INPUT_TOKENS 时中间截断
    raw_messages = req_data.get("messages") or []
    sys_field = req_data.get("system")
    tools_field = req_data.get("tools") or []
    est_in = estimate_tokens(raw_messages, system=sys_field, tools=tools_field)
    if est_in > config.MAX_INPUT_TOKENS:
        try:
            truncated, info = truncate_messages(
                raw_messages,
                system=sys_field,
                tools=tools_field,
                budget=config.MAX_INPUT_TOKENS,
            )
        except OpenAIError as e:
            # 转 Anthropic 错误格式
            usage_mgr.record_failed(key_info.name, req.model)
            _log_access(key_info.name, req.model, 413, time.time() - t0, "")
            raise AnthropicError("invalid_request_error", e.detail, status=413)
        logger.info(
            "[%s] req_id=%s truncated: kept=%d dropped=%d est_in=%d → %d",
            key_info.name, req_id, info["kept_count"], info["dropped_count"],
            est_in, info["estimated_tokens"],
        )
        req_data["messages"] = truncated

    payload = {
        "_relay_v": 3,
        "type": "req",
        "req_id": req_id,
        "endpoint": "messages",
        "mode": "messages_native",
        "stream": bool(req.stream),
        **req_data,
    }
    # model_dump 已经把 stream 写进 req_data，避免重复 key 冲突
    payload["stream"] = bool(req.stream)

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

    # 选一个 bot
    node = bot_pool.select()
    if not node:
        usage_mgr.record_failed(key_info.name, req.model)
        _log_access(key_info.name, req.model, 503, time.time() - t0, last_user)
        raise AnthropicError("api_error", "No available bot nodes", status=503)
    bot_pool.record_request(node)

    # 4. 发飞书 + 轮询
    try:
        await bot_pool.send_request(node, payload)
    except PayloadTooLargeError as e:
        usage_mgr.record_failed(key_info.name, req.model)
        _log_access(key_info.name, req.model, 413, time.time() - t0, last_user)
        raise AnthropicError(
            "invalid_request_error",
            f"Multipart envelope still oversized ({e.size_kb:.0f}KB).",
            status=413,
        )
    except TokenExpiredError as e:
        usage_mgr.record_failed(key_info.name, req.model)
        _log_access(key_info.name, req.model, 401, time.time() - t0, last_user)
        raise AnthropicError("authentication_error", str(e), status=401)
    except RuntimeError as e:
        usage_mgr.record_failed(key_info.name, req.model)
        _log_access(key_info.name, req.model, 502, time.time() - t0, last_user)
        raise AnthropicError("api_error", str(e), status=502)

    # 流式：实时 SSE
    if req.stream:
        return StreamingResponse(
            anthropic_stream_from_worker(
                node=node,
                req_id=req_id,
                model=req.model,
                after_ms=before_ms,
                key_name=key_info.name,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    reply = await bot_pool.poll_reply_by_req_id(node, req_id, after_ms=before_ms)
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
    bot_pool.record_usage(node, in_tok, out_tok)

    logger.info("← [%s] [%s] req_id=%s %.1fs in/out=%d/%d stop=%s",
                key_info.name, req.model, req_id, duration,
                in_tok, out_tok, raw.get("stop_reason"))
    _log_access(key_info.name, req.model, 200, duration, last_user)

    # 非流式：直接返回 raw（即 MP 的原 Anthropic 响应）
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
        token_mgr.get_token()
        token_ok = True
        remaining = token_mgr.token_remaining_s
    except TokenExpiredError:
        pass
    return {
        "status": "ok" if token_ok else "token_expired",
        "token_remaining_s": int(remaining),
        "refresh_token_available": token_mgr.has_refresh_token,
        "api_keys_total": key_mgr.key_count,
        "api_keys_active": key_mgr.active_count,
        "bot_nodes": bot_pool.count,
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
                "key": k.key,                        # admin 有权限查看完整 key
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
    resolved = key_mgr._resolve_key(key)
    if resolved is None:
        raise OpenAIError("not_found_error", "Key not found", status=404)
    if key_mgr.revoke_key(resolved):
        return {"status": "revoked", "key_prefix": resolved[:12] + "***"}
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

    resolved = key_mgr._resolve_key(key)
    if resolved is None or resolved not in key_mgr._keys:
        raise OpenAIError("not_found_error", "Key not found", status=404)

    if req.enabled is True:
        key_mgr.enable_key(resolved)
    elif req.enabled is False:
        key_mgr.revoke_key(resolved)

    if (
        req.rpm_limit is not None
        or req.daily_token_limit is not None
        or req.clear_rpm
        or req.clear_daily
    ):
        key_mgr.set_limits(
            resolved,
            rpm_limit=req.rpm_limit,
            daily_token_limit=req.daily_token_limit,
            clear_rpm=req.clear_rpm,
            clear_daily=req.clear_daily,
        )

    info = key_mgr._keys[resolved]
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
    resolved = key_mgr._resolve_key(key)
    if resolved is None or resolved not in key_mgr._keys:
        raise OpenAIError("not_found_error", "Key not found", status=404)
    info = key_mgr._keys[resolved]
    name = info.name

    stats = usage_mgr.get(name)
    daily = usage_mgr.daily_token_count(name)
    current_rpm = rate_limiter.rpm_current(name)

    if stats is None:
        return {
            "key_prefix": resolved[:12] + "***",
            "name": name,
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
        "key_prefix": resolved[:12] + "***",
        "name": name,
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
# Admin 管控接口
# ============================================================================


@app.get("/admin/nodes")
async def admin_list_nodes(request: Request):
    """Admin 查节点列表。"""
    _check_admin(request)
    nodes = bot_pool.list_all()
    return {
        "total": len(nodes),
        "nodes": [
            {
                "node_id": n.node_id,
                "version": n.version,
                "hostname": n.hostname,
                "ip": n.ip,
                "open_id": n.open_id,
                "chat_id": n.chat_id,
                "started_at": n.started_at,
                "first_seen_at": n.first_seen_at,
                "last_request_at": n.last_request_at,
                "load": n.load,
                "models": n.models,
                "request_count": n.request_count,
                "total_prompt_tokens": n.total_prompt_tokens,
                "total_completion_tokens": n.total_completion_tokens,
                "total_tokens": n.total_tokens,
                # v0.4 cluster fields（详见 arch-cluster-upgrade.md §3.1）
                "enabled": n.enabled,
                "cluster_id": n.cluster_id,
                "app_id": n.app_id,
                "slot_id": n.slot_id,
                # agent_secret 永远不外泄
            }
            for n in nodes
        ],
    }


@app.get("/api/discovery")
async def api_discovery(request: Request):
    """服务发现：返回 bot 列表（需鉴权）。"""
    _check_auth(request)
    nodes = bot_pool.list_all()
    return {
        "total": len(nodes),
        "nodes": [
            {
                "node_id": n.node_id,
                "version": n.version,
                "models": n.models,
                "load": n.load,
                "last_request_at": n.last_request_at,
                "request_count": n.request_count,
            }
            for n in nodes
        ],
    }


class CtrlRequest(BaseModel):
    target_version: Optional[str] = None


@app.post("/admin/nodes/{node_id:path}/upgrade")
async def admin_upgrade_node(node_id: str, req: CtrlRequest, request: Request):
    """给指定 bot 发升级指令。"""
    _check_admin(request)
    node = bot_pool.get(node_id)
    if not node:
        raise OpenAIError("not_found_error", f"Node {node_id} not found", status=404)
    await bot_pool.send_ctrl(node, "upgrade", target_version=req.target_version or "latest")
    return {"status": "sent", "node_id": node_id, "action": "upgrade"}


@app.post("/admin/nodes/{node_id:path}/restart")
async def admin_restart_node(node_id: str, request: Request):
    """给指定 bot 发重启指令。"""
    _check_admin(request)
    node = bot_pool.get(node_id)
    if not node:
        raise OpenAIError("not_found_error", f"Node {node_id} not found", status=404)
    await bot_pool.send_ctrl(node, "restart")
    return {"status": "sent", "node_id": node_id, "action": "restart"}


@app.post("/admin/nodes/{node_id:path}/drain")
async def admin_drain_node(node_id: str, request: Request):
    """给指定 bot 发优雅下线指令。"""
    _check_admin(request)
    node = bot_pool.get(node_id)
    if not node:
        raise OpenAIError("not_found_error", f"Node {node_id} not found", status=404)
    await bot_pool.send_ctrl(node, "drain")
    return {"status": "sent", "node_id": node_id, "action": "drain"}


# ---- v0.4 cluster upgrade: enable / disable / decommission ----
# 详见 arch-cluster-upgrade.md §3.1 (admin 软开关) 和 §3.5 (节点生命周期)


@app.post("/admin/nodes/{node_id:path}/enable")
async def admin_enable_node(node_id: str, request: Request):
    """启用节点：放回 round-robin 路由池。"""
    _check_admin(request)
    if not bot_pool.set_enabled(node_id, True):
        raise OpenAIError("not_found_error", f"Node {node_id} not found", status=404)
    return {"status": "ok", "node_id": node_id, "enabled": True}


@app.post("/admin/nodes/{node_id:path}/disable")
async def admin_disable_node(node_id: str, request: Request):
    """禁用节点：从 round-robin 移除但保留记录（不释放 slot）。"""
    _check_admin(request)
    if not bot_pool.set_enabled(node_id, False):
        raise OpenAIError("not_found_error", f"Node {node_id} not found", status=404)
    return {"status": "ok", "node_id": node_id, "enabled": False}


@app.post("/admin/nodes/{node_id:path}/decommission")
async def admin_decommission_node(node_id: str, request: Request):
    """彻底下线节点：从 pool 移除 + 释放绑定的 slot（如有）。

    SlotPool 集成在后续 M2-3 完成后接入；当前只做 bot_pool.remove()。
    """
    _check_admin(request)
    node = bot_pool.get(node_id)
    if not node:
        raise OpenAIError("not_found_error", f"Node {node_id} not found", status=404)
    slot_id = node.slot_id
    bot_pool.remove(node_id)
    if slot_id:
        slot_pool.release(slot_id)
    return {
        "status": "ok",
        "node_id": node_id,
        "action": "decommission",
        "released_slot_id": slot_id or None,
    }


# ---- v0.4 cluster upgrade: SlotPool 管理 API ----
# 详见 arch-cluster-upgrade.md §3.2


class CreateSlotRequest(BaseModel):
    app_id: str
    app_secret: str
    chat_id: str
    mp_key: str
    mp_base_url: str = ""
    notes: str = ""
    capabilities: Optional[List[str]] = None
    default_max_tokens: int = 4096
    heartbeat_interval_s: int = 30
    slot_id: Optional[str] = None  # 不指定则自动编号


def _slot_to_dict(s, include_secrets: bool = False) -> dict:
    """slot dump 为 dict。admin 查看时默认隐藏 app_secret / mp_key，
    防止运维误把列表贴到聊天工具被泄露。
    """
    d = {
        "slot_id": s.slot_id,
        "app_id": s.app_id,
        "chat_id": s.chat_id,
        "mp_base_url": s.mp_base_url,
        "notes": s.notes,
        "capabilities": s.capabilities,
        "default_max_tokens": s.default_max_tokens,
        "heartbeat_interval_s": s.heartbeat_interval_s,
        "claimed_by": s.claimed_by,
        "claimed_at": s.claimed_at,
        "created_at": s.created_at,
        "is_free": s.is_free,
    }
    if include_secrets:
        d["app_secret"] = s.app_secret
        d["mp_key"] = s.mp_key
    else:
        d["app_secret_preview"] = (s.app_secret[:4] + "***" + s.app_secret[-2:]) if s.app_secret else ""
        d["mp_key_preview"] = (s.mp_key[:4] + "***" + s.mp_key[-2:]) if s.mp_key else ""
    return d


@app.get("/admin/pool/slots")
async def admin_list_slots(request: Request):
    """列出所有 slot 及 claim 状态。默认隐藏 secret，传 ?reveal=1 查全量。"""
    _check_admin(request)
    reveal = request.query_params.get("reveal") == "1"
    slots = slot_pool.list_slots()
    return {
        "total": len(slots),
        "free_count": slot_pool.free_count,
        "claimed_count": slot_pool.claimed_count,
        "slots": [_slot_to_dict(s, include_secrets=reveal) for s in slots],
    }


@app.post("/admin/pool/slots")
async def admin_create_slot(req: CreateSlotRequest, request: Request):
    """灌一条新 slot 进池子。"""
    _check_admin(request)
    try:
        slot = slot_pool.add(
            app_id=req.app_id,
            app_secret=req.app_secret,
            chat_id=req.chat_id,
            mp_key=req.mp_key,
            mp_base_url=req.mp_base_url,
            notes=req.notes,
            capabilities=req.capabilities,
            default_max_tokens=req.default_max_tokens,
            heartbeat_interval_s=req.heartbeat_interval_s,
            slot_id=req.slot_id,
        )
    except ValueError as e:
        raise OpenAIError("invalid_request_error", str(e), status=400)
    return {"status": "ok", "slot": _slot_to_dict(slot, include_secrets=False)}


@app.delete("/admin/pool/slots/{slot_id}")
async def admin_delete_slot(slot_id: str, request: Request):
    """删除一条 slot（必须先 release）。"""
    _check_admin(request)
    try:
        ok = slot_pool.delete(slot_id)
    except ValueError as e:
        # claimed slot 不让删
        raise OpenAIError("invalid_request_error", str(e), status=409)
    if not ok:
        raise OpenAIError("not_found_error", f"Slot {slot_id} not found", status=404)
    return {"status": "ok", "slot_id": slot_id, "action": "deleted"}


@app.post("/admin/pool/slots/{slot_id}/release")
async def admin_release_slot(slot_id: str, request: Request):
    """强制 release 一个 slot（紧急用：节点失联但 slot 仍被持有）。

    注意：这只是让 slot 回到空闲池，bot_pool 里那个节点不会自动消失；
    要彻底下线请用 /admin/nodes/{node_id}/decommission。
    """
    _check_admin(request)
    ok = slot_pool.release(slot_id)
    if not ok:
        raise OpenAIError("not_found_error", f"Slot {slot_id} not found", status=404)
    return {"status": "ok", "slot_id": slot_id, "action": "released"}


# ---- v0.4 cluster upgrade: /bootstrap (install-time only) ----
# 详见 arch-cluster-upgrade.md §3.3
#
# 路径设计：nginx 上 `/llm/api/bootstrap` 剥去前缀后落到 `/bootstrap`。
# 这是节点全生命周期里**与 gateway 域名的唯一一次 HTTP 交互**——install 时
# 拿配置，之后切到 Feishu IM transport，不再访问 gateway 域名。


class BootstrapClawIdentity(BaseModel):
    id: str
    type: str = "InferenceClaw"
    version: str = ""


class BootstrapRequest(BaseModel):
    claw: BootstrapClawIdentity
    cluster: str = ""
    hostname: str = ""
    ip: str = ""


def _build_bootstrap_response(claw_secret: str, slot, heartbeat_interval_s: int) -> dict:
    """构造 bootstrap 返回体（节点拿到后写入 config.yaml + config.yaml.secret）。"""
    return {
        "claw_secret": claw_secret,
        "slot_id": slot.slot_id,
        "app_id": slot.app_id,
        "app_secret": slot.app_secret,
        "chat_id": slot.chat_id,
        "mp_key": slot.mp_key,
        "mp_base_url": slot.mp_base_url or config.MODELPROXY_BASE,
        "capabilities": list(slot.capabilities or list_models()),
        "default_max_tokens": slot.default_max_tokens or 4096,
        "heartbeat_interval_s": heartbeat_interval_s,
    }


@app.post("/bootstrap")
async def bootstrap_endpoint(req: BootstrapRequest, request: Request):
    """节点装机时调用：用 BOOTSTRAP_TOKEN 换一份 slot 配置 + 长效 claw_secret。

    幂等性：同 claw_id 重复调用返回已绑定的 slot 配置（不重新 claim）。
    错误码：
    - 401  bad_bootstrap_token
    - 503  no_free_slot  池子无空闲
    - 500  internal error
    """
    if not config.BOOTSTRAP_TOKEN:
        # 端点存在但未启用：返回 503 而不是 404，避免泄露功能存在与否
        raise HTTPException(503, "bootstrap not enabled")

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != config.BOOTSTRAP_TOKEN:
        raise HTTPException(401, "invalid bootstrap token")

    node_id = req.claw.id
    if not node_id:
        raise HTTPException(400, "claw.id required")

    # 幂等：该 node_id 已注册并绑定了 slot → 直接返回
    existing = bot_pool.get(node_id)
    if existing and existing.slot_id and existing.agent_secret:
        slot = slot_pool.get(existing.slot_id)
        if slot:
            logger.info(
                "[bootstrap] idempotent re-bootstrap: node=%s slot=%s",
                node_id, slot.slot_id,
            )
            return _build_bootstrap_response(
                existing.agent_secret, slot, config.CLUSTER_HEARTBEAT_INTERVAL_S,
            )
        # slot 居然丢了（人工 release 但 bot_pool 没清）→ 把节点也清掉，按新节点处理
        logger.warning(
            "[bootstrap] node=%s has slot_id=%s but slot missing; re-claiming",
            node_id, existing.slot_id,
        )
        bot_pool.remove(node_id)

    # claim 一个空闲 slot
    slot = slot_pool.claim(node_id)
    if not slot:
        logger.warning(
            "[bootstrap] no_free_slot: node=%s cluster=%s",
            node_id, req.cluster,
        )
        raise HTTPException(503, "no_free_slot")

    # 生成长效 claw_secret 并注册节点
    import secrets as _secrets
    claw_secret = _secrets.token_hex(24)  # 48-char hex
    try:
        bot_pool.register({
            "node_id": node_id,
            "cluster_id": req.cluster or "default",
            "hostname": req.hostname,
            "ip": req.ip,
            "app_id": slot.app_id,
            "chat_id": slot.chat_id,
            "slot_id": slot.slot_id,
            "agent_secret": claw_secret,
            "version": req.claw.version,
        })
    except Exception:
        # 注册失败要回滚 slot，避免泄漏
        slot_pool.release(slot.slot_id)
        raise

    logger.info(
        "[bootstrap] new node registered: node=%s cluster=%s slot=%s app_id=%s",
        node_id, req.cluster, slot.slot_id, slot.app_id,
    )
    return _build_bootstrap_response(
        claw_secret, slot, config.CLUSTER_HEARTBEAT_INTERVAL_S,
    )


@app.post("/admin/nodes/upgrade-all")
async def admin_upgrade_all(req: CtrlRequest, request: Request):
    """广播升级指令给所有在线 bot。"""
    _check_admin(request)
    sent = await bot_pool.broadcast_ctrl("upgrade", target_version=req.target_version or "latest")
    return {"status": "sent", "count": len(sent), "node_ids": sent}


# ============================================================================
# Agent 心跳上报（Bot HTTP push 模式）
# ============================================================================


@app.post("/agent/heartbeat")
async def agent_heartbeat(request: Request):
    """Bot 主动上报心跳，注册/更新节点。"""
    await _verify_agent(request)
    body = await request.json()
    node_id = body.get("node_id")
    if not node_id:
        raise HTTPException(400, "missing node_id")

    bots = body.get("bots") or []
    if bots:
        for b in bots:
            bid = b.get("name") or b.get("app_id", "")
            sub_node_id = f"{node_id}/{bid}" if bid else node_id
            bot_pool.register({
                "node_id": sub_node_id,
                "open_id": b.get("open_id", ""),
                "chat_id": b.get("chat_id", ""),
                "version": body.get("version", ""),
                "hostname": body.get("hostname", ""),
                "ip": body.get("ip", ""),
                "models": body.get("models", []),
                "started_at": body.get("started_at", ""),
                "capabilities": body.get("capabilities", []),
            })
    else:
        bot_pool.register(body)

    logger.info("heartbeat from %s, bots=%d", node_id, len(bots) or 1)
    return {"status": "ok"}


@app.post("/agent/offline")
async def agent_offline(request: Request):
    """Bot 主动下线通知。"""
    await _verify_agent(request)
    body = await request.json()
    node_id = body.get("node_id")
    if not node_id:
        raise HTTPException(400, "missing node_id")
    bot_pool.remove(node_id)
    logger.info("offline from %s", node_id)
    return {"status": "ok"}


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
