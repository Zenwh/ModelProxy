"""
Bot Gateway (WebSocket 长连接版)
==================================

⚠️  这是 PoC 版本。生产环境推荐使用独立产品仓库：
    https://github.com/Zenwh/feishu-relay-bot
    pip install feishu-relay-bot
    feishu-relay-bot run --config config.yaml

接收 relay 协议的 JSON 消息，路由到 Model Proxy 的对应模型，把响应包装成
JSON 发回飞书。bot 在内网运行，relay 在外网，通过飞书消息通道穿透 NAT。

Relay → Bot 协议（飞书文本消息）:
  {
    "_relay_v": 1,
    "req_id": "<24位hex>",
    "model": "claude-opus-4-7",
    "messages": [...],
    "temperature": 0.7,    # optional
    "max_tokens": 1000      # optional
  }

Bot → Relay 协议:
  成功 {"_relay_v":1, "req_id":..., "ok":true, "content":..., "usage":..., "finish_reason":...}
  失败 {"_relay_v":1, "req_id":..., "ok":false, "status":429, "error":..., "message":...}

环境变量：
  FEISHU_APP_ID       飞书 app ID（默认：阿月老师）
  FEISHU_APP_SECRET   飞书 app secret
  MODELPROXY_BASE     Model Proxy 地址
  MODELPROXY_API_KEY  Model Proxy API Key
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from typing import Any, Optional

import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

# 让 bot 能 import outside_caller.models（共用模型映射表）
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
from outside_caller.models import to_mp_name, to_endpoint, is_supported  # noqa: E402

# ---- 配置 -------------------------------------------------------------------

APP_ID = os.getenv("FEISHU_APP_ID", "cli_a955f5aa04f81bda")
APP_SECRET = os.getenv("FEISHU_APP_SECRET", "WgVfCkJcdggcJqkoJDVKB6YkL2JqoT16")

MODELPROXY_BASE = os.getenv("MODELPROXY_BASE", "https://models-proxy.stepfun-inc.com").rstrip("/")
MODELPROXY_API_KEY = os.getenv("MODELPROXY_API_KEY", "ak-c9pttfhr2xoxrwuo4a7hvtd91h7zfedh")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bot-gateway")


# ---- Model Proxy 调用 -------------------------------------------------------

DEFAULT_MAX_TOKENS = 4096  # /v1/messages 要求 max_tokens 必填


def _mp_post(path: str, payload: dict) -> tuple[int, dict]:
    """通用 MP POST。"""
    with httpx.Client(timeout=240) as cli:
        r = cli.post(
            f"{MODELPROXY_BASE}{path}",
            headers={
                "Authorization": f"Bearer {MODELPROXY_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text[:500]}
    return r.status_code, data


def _split_system(messages: list) -> tuple[str, list]:
    """从 OpenAI messages 数组拆出 system 文本和剩余 messages。"""
    sys_parts = []
    rest = []
    for m in messages:
        if m.get("role") == "system":
            sys_parts.append(m.get("content", ""))
        else:
            rest.append(m)
    return "\n\n".join(sys_parts), rest


def _normalize_to_openai(content: str, finish_reason: str, usage: dict) -> dict:
    """统一返回结构（送回 relay 用）。"""
    # 把不同接口的 usage 字段名归一化到 prompt/completion/total
    p = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
    c = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
    t = usage.get("total_tokens", 0) or (p + c)
    return {
        "content": content,
        "finish_reason": finish_reason,
        "usage": {
            "prompt_tokens": p,
            "completion_tokens": c,
            "total_tokens": t,
        },
    }


# ---- 三种端点的 adapter ----

def call_mp_messages(mp_model: str, messages: list, max_tokens: int, temperature: Optional[float]) -> tuple[int, dict]:
    """Anthropic /v1/messages 适配。"""
    system, rest = _split_system(messages)
    payload: dict[str, Any] = {
        "model": mp_model,
        "messages": rest,
        "max_tokens": max_tokens,
    }
    if system:
        payload["system"] = system
    if temperature is not None:
        payload["temperature"] = temperature

    status, data = _mp_post("/v1/messages", payload)
    if status != 200:
        return status, data

    # 提取内容
    content_parts = []
    for blk in data.get("content", []):
        if blk.get("type") == "text":
            content_parts.append(blk.get("text", ""))
    content = "".join(content_parts)
    finish = data.get("stop_reason", "end_turn")
    if finish == "end_turn":
        finish = "stop"
    elif finish == "max_tokens":
        finish = "length"

    return 200, _normalize_to_openai(content, finish, data.get("usage") or {})


def call_mp_responses(mp_model: str, messages: list, max_tokens: int, temperature: Optional[float]) -> tuple[int, dict]:
    """OpenAI /v1/responses 适配。"""
    system, rest = _split_system(messages)

    # input 字段：单条 user 用字符串，多轮用数组
    if len(rest) == 1 and rest[0].get("role") == "user":
        input_val: Any = rest[0].get("content", "")
    else:
        input_val = rest

    payload: dict[str, Any] = {
        "model": mp_model,
        "input": input_val,
        "max_output_tokens": max_tokens,
    }
    if system:
        payload["instructions"] = system
    if temperature is not None:
        payload["temperature"] = temperature

    status, data = _mp_post("/v1/responses", payload)
    if status != 200:
        return status, data

    # 提取内容
    content_parts = []
    for item in data.get("output", []):
        if item.get("type") == "message":
            for blk in item.get("content", []):
                if blk.get("type") == "output_text":
                    content_parts.append(blk.get("text", ""))
    content = "".join(content_parts)
    finish = "stop"  # responses 没有显式 finish_reason，按 status 判断
    for item in data.get("output", []):
        if item.get("status") == "incomplete":
            finish = "length"
            break

    return 200, _normalize_to_openai(content, finish, data.get("usage") or {})


def call_mp_chat(mp_model: str, messages: list, max_tokens: Optional[int], temperature: Optional[float]) -> tuple[int, dict]:
    """OpenAI /v1/chat/completions 适配（直通）。"""
    payload: dict[str, Any] = {
        "model": mp_model,
        "messages": messages,
        "stream": False,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if temperature is not None:
        payload["temperature"] = temperature

    status, data = _mp_post("/v1/chat/completions", payload)
    if status != 200:
        return status, data

    choices = data.get("choices") or []
    if not choices:
        return 502, {"error": "no_choices", "message": "empty response"}
    choice = choices[0]
    content = choice.get("message", {}).get("content", "")
    finish = choice.get("finish_reason", "stop")
    return 200, _normalize_to_openai(content, finish, data.get("usage") or {})


def call_mp(model_pub: str, messages: list, max_tokens: Optional[int], temperature: Optional[float]) -> tuple[int, dict]:
    """按模型路由到对应 MP 端点，返回 (http_status, normalized_response)。"""
    mp_model = to_mp_name(model_pub)
    endpoint = to_endpoint(model_pub)
    if not mp_model or not endpoint:
        return 400, {"error": "unknown_model", "message": f"unsupported model: {model_pub}"}

    if endpoint == "messages":
        # max_tokens 必填
        return call_mp_messages(mp_model, messages, max_tokens or DEFAULT_MAX_TOKENS, temperature)
    elif endpoint == "responses":
        return call_mp_responses(mp_model, messages, max_tokens or DEFAULT_MAX_TOKENS, temperature)
    elif endpoint == "chat":
        return call_mp_chat(mp_model, messages, max_tokens, temperature)
    else:
        return 500, {"error": "bad_routing", "message": f"unknown endpoint type: {endpoint}"}


# ---- 飞书回复 ----------------------------------------------------------------

lark_client: lark.Client = None  # type: ignore


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


def send_relay_response(chat_id: str, payload: dict):
    """把 relay 响应序列化成 JSON 发回飞书。"""
    text = json.dumps(payload, ensure_ascii=False)
    reply_text(chat_id, text)
    logger.info("→ 回复 req_id=%s ok=%s len=%d",
                payload.get("req_id"), payload.get("ok"), len(text))


# ---- 事件处理 ----------------------------------------------------------------

def parse_relay_req(raw: str) -> Optional[dict]:
    """尝试把消息文本解析为 relay 协议请求。返回 dict 或 None。"""
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    if d.get("_relay_v") != 1:
        return None
    if not d.get("req_id"):
        return None
    return d


def on_message(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    """收到 im.message.receive_v1 事件。"""
    event = data.event
    msg = event.message
    sender = event.sender
    sender_type = getattr(sender.sender_id, "open_id", "?") if sender else "?"

    logger.info("[event] msg_id=%s type=%s sender=%s",
                msg.message_id, msg.message_type, sender_type)

    if msg.message_type != "text":
        logger.info("忽略非文本消息: type=%s", msg.message_type)
        return

    content = json.loads(msg.content)
    raw_text = content.get("text", "")
    chat_id = msg.chat_id

    req = parse_relay_req(raw_text)
    if req is None:
        logger.info("非 relay 协议消息，忽略: %.60s", raw_text)
        return

    req_id = req["req_id"]
    model_pub = req.get("model", "")
    mode = req.get("mode", "openai_chat")
    messages = req.get("messages", [])
    logger.info("← req_id=%s mode=%s model=%s msgs=%d", req_id, mode, model_pub, len(messages))

    # ---- 分支：Anthropic 原生 /v1/messages ----
    if mode == "messages_native":
        _spawn_handle_anthropic(req, chat_id)
        return

    def _handle():
        try:
            if not is_supported(model_pub):
                send_relay_response(chat_id, {
                    "_relay_v": 1,
                    "req_id": req_id,
                    "ok": False,
                    "status": 400,
                    "error": "unsupported_model",
                    "message": f"unsupported model: {model_pub}",
                })
                return

            status, resp = call_mp(
                model_pub,
                messages,
                max_tokens=req.get("max_tokens"),
                temperature=req.get("temperature"),
            )

            if status == 200:
                # resp 已被 normalize 成 {content, finish_reason, usage} 格式
                send_relay_response(chat_id, {
                    "_relay_v": 1,
                    "req_id": req_id,
                    "ok": True,
                    "content": resp["content"],
                    "usage": resp["usage"],
                    "finish_reason": resp["finish_reason"],
                })
            else:
                # 上游错误
                err_msg = (
                    resp.get("error") if isinstance(resp.get("error"), str)
                    else (resp.get("msg") or str(resp)[:300])
                )
                send_relay_response(chat_id, {
                    "_relay_v": 1,
                    "req_id": req_id,
                    "ok": False,
                    "status": status if status >= 400 else 502,
                    "error": "upstream_error",
                    "message": err_msg,
                })
        except Exception as e:
            logger.exception("处理 req_id=%s 异常", req_id)
            send_relay_response(chat_id, {
                "_relay_v": 1,
                "req_id": req_id,
                "ok": False,
                "status": 500,
                "error": "bot_exception",
                "message": f"{type(e).__name__}: {e}",
            })

    # 子线程处理，避免阻塞 websocket（飞书 3s ACK 限制）
    threading.Thread(target=_handle, daemon=True).start()


# ---- Anthropic /v1/messages 通道 -------------------------------------------

def _spawn_handle_anthropic(req: dict, chat_id: str) -> None:
    """处理 mode=messages_native 请求：直通 MP /v1/messages，原响应打包回 relay。"""
    req_id = req["req_id"]
    model_pub = req.get("model", "")

    def _handle():
        try:
            mp_model = to_mp_name(model_pub)
            if not mp_model:
                send_relay_response(chat_id, {
                    "_relay_v": 1, "req_id": req_id,
                    "ok": False, "status": 400,
                    "error": "unsupported_model",
                    "message": f"unsupported model: {model_pub}",
                })
                return

            # 构造 MP /v1/messages payload
            # 把 relay 协议 fields 拿掉，剩下的就是 Anthropic 字段
            payload = {
                k: v for k, v in req.items()
                if k not in ("_relay_v", "req_id", "mode", "model", "stream")
            }
            payload["model"] = mp_model
            # 显式不要 stream
            payload.pop("stream", None)

            status, mp_resp = _mp_post("/v1/messages", payload)

            if status == 200 and "content" in mp_resp:
                send_relay_response(chat_id, {
                    "_relay_v": 1, "req_id": req_id,
                    "ok": True,
                    "mode": "messages_native",
                    "raw_anthropic": mp_resp,
                })
            else:
                # 上游错误：尽量保留原 message
                err_msg = (
                    mp_resp.get("error", {}).get("message") if isinstance(mp_resp.get("error"), dict)
                    else mp_resp.get("msg")
                    or str(mp_resp)[:300]
                )
                send_relay_response(chat_id, {
                    "_relay_v": 1, "req_id": req_id,
                    "ok": False,
                    "status": status if status >= 400 else 502,
                    "error": "upstream_error",
                    "message": err_msg,
                })
        except Exception as e:
            logger.exception("处理 messages_native req_id=%s 异常", req_id)
            send_relay_response(chat_id, {
                "_relay_v": 1, "req_id": req_id,
                "ok": False, "status": 500,
                "error": "bot_exception",
                "message": f"{type(e).__name__}: {e}",
            })

    threading.Thread(target=_handle, daemon=True).start()


# ---- 主入口 ------------------------------------------------------------------

def main():
    global lark_client

    logger.info("启动 bot-gateway-ws")
    logger.info("  APP_ID=%s", APP_ID)
    logger.info("  MODELPROXY_BASE=%s", MODELPROXY_BASE)

    lark_client = lark.Client.builder() \
        .app_id(APP_ID) \
        .app_secret(APP_SECRET) \
        .log_level(lark.LogLevel.INFO) \
        .build()

    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(on_message) \
        .build()

    ws_client = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )

    logger.info("连接飞书 websocket ...")
    ws_client.start()


if __name__ == "__main__":
    main()
