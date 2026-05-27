"""
Stream router：把 worker 端 v3 stream_chunk 翻译成 OpenAI / Anthropic SSE。

worker 那侧 StreamEmitter 喂的 delta 形如：
- chat:            {"text": "..."}
- messages_native: {"text": "..."} 或 {"tool_use": {id, name, partial_json}}
                   或 {"thinking": "..."} 或 {"content_block_start": {...}}
                   或 {"content_block_stop": {"index": N}}

为了简单起见，gateway 这层不做 anthropic 的 content_block_start/stop 协议补全；
worker StreamEmitter 在 delta 里直接带 "events" 数组（已是 anthropic SSE event dict）。
chat 模式则只需把 text 拼成 OpenAI delta chunk。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator, Dict, Optional

from . import config
from .bot_pool import BotNode, pool as bot_pool
from .usage import manager as usage_mgr

logger = logging.getLogger("stream-router")


def _openai_chunk(req_id: str, model: str, delta: dict, finish_reason: Optional[str]) -> str:
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


def _sse_event(event_type: str, data: Dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def openai_stream_from_worker(
    *,
    node: BotNode,
    req_id: str,
    model: str,
    after_ms: int,
    key_name: str,
) -> AsyncIterator[str]:
    """OpenAI chat/responses 流式 SSE 生成器。"""
    yield _openai_chunk(req_id, model, {"role": "assistant"}, None)

    finish_reason = "stop"
    got_resp = False
    p_tok = c_tok = 0
    err_msg: Optional[str] = None

    try:
        async for parsed in bot_pool.poll_stream(node, req_id, after_ms=after_ms):
            ptype = parsed.get("type")
            if ptype == "stream_chunk":
                delta = parsed.get("delta") or {}
                text = delta.get("text") or ""
                if text:
                    yield _openai_chunk(req_id, model, {"content": text}, None)
                tool = delta.get("tool_use")
                if tool and isinstance(tool, dict):
                    # OpenAI tool_calls 增量格式
                    yield _openai_chunk(req_id, model, {
                        "tool_calls": [{
                            "index": 0,
                            "id": tool.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": tool.get("name", ""),
                                "arguments": tool.get("partial_json", ""),
                            },
                        }],
                    }, None)

                # messages_native worker mode：worker 把 anthropic SSE events 原样塞进 delta.events
                # 这里翻译成 OpenAI delta.content / tool_calls。否则 claude-* 模型走
                # /v1/chat/completions 流式时 client 收不到任何正文（只有 role+finish）。
                events = delta.get("events")
                if events and isinstance(events, list):
                    for ev in events:
                        ed = ev.get("data") if isinstance(ev.get("data"), dict) else ev
                        et = ev.get("event") or ev.get("type") or (ed.get("type") if isinstance(ed, dict) else None)
                        if et == "content_block_delta":
                            ev_delta = ed.get("delta") or {}
                            dtype = ev_delta.get("type")
                            if dtype == "text_delta":
                                t = ev_delta.get("text") or ""
                                if t:
                                    yield _openai_chunk(req_id, model, {"content": t}, None)
                            elif dtype == "input_json_delta":
                                pj = ev_delta.get("partial_json") or ""
                                if pj:
                                    yield _openai_chunk(req_id, model, {
                                        "tool_calls": [{
                                            "index": 0,
                                            "function": {"arguments": pj},
                                        }],
                                    }, None)
                        elif et == "content_block_start":
                            cb = ed.get("content_block") or {}
                            if cb.get("type") == "tool_use":
                                yield _openai_chunk(req_id, model, {
                                    "tool_calls": [{
                                        "index": 0,
                                        "id": cb.get("id", ""),
                                        "type": "function",
                                        "function": {
                                            "name": cb.get("name", ""),
                                            "arguments": "",
                                        },
                                    }],
                                }, None)
                        # message_delta / message_stop 由下方 resp 分支统一收尾，不在这里 yield finish
            elif ptype == "resp":
                got_resp = True
                if not parsed.get("ok", True):
                    err_msg = parsed.get("message") or parsed.get("error") or "upstream_error"
                    finish_reason = "error"
                else:
                    finish_reason = parsed.get("finish_reason") or "stop"
                usage = parsed.get("usage") or {}
                p_tok = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                c_tok = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
                break
    except Exception as e:
        logger.exception("openai_stream error req_id=%s", req_id)
        err_msg = f"stream_error: {type(e).__name__}: {e}"

    if err_msg:
        yield _openai_chunk(req_id, model, {"content": f"\n\n[stream error] {err_msg}"}, "error")
    else:
        yield _openai_chunk(req_id, model, {}, finish_reason)
    yield "data: [DONE]\n\n"

    if got_resp and (p_tok or c_tok):
        usage_mgr.record(key_name, model, p_tok, c_tok)
        bot_pool.record_usage(node, p_tok, c_tok)
    elif not got_resp:
        usage_mgr.record_failed(key_name, model)


async def anthropic_stream_from_worker(
    *,
    node: BotNode,
    req_id: str,
    model: str,
    after_ms: int,
    key_name: str,
) -> AsyncIterator[str]:
    """Anthropic /v1/messages 流式 SSE 生成器。

    worker 端 StreamEmitter (messages_native) 直接发送已是 anthropic 协议格式的
    `events` 数组（每个元素 {"event": "content_block_delta", "data": {...}}），
    gateway 这里只负责按序输出。

    若 delta 里只携带原始 text/tool_use 而非 events 数组，回退到本地补全
    message_start / content_block_* 事件包装。
    """
    msg_id = f"msg_{req_id}"
    started = False
    block_started = False  # text block 是否已开 start
    cur_block_idx = 0
    in_text_block = False
    p_tok = c_tok = 0
    stop_reason = "end_turn"
    got_resp = False
    err_msg: Optional[str] = None

    def _start_message_event(input_tokens: int = 0) -> str:
        return _sse_event("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        })

    def _open_text_block(idx: int) -> str:
        return _sse_event("content_block_start", {
            "type": "content_block_start",
            "index": idx,
            "content_block": {"type": "text", "text": ""},
        })

    def _text_delta(idx: int, text: str) -> str:
        return _sse_event("content_block_delta", {
            "type": "content_block_delta",
            "index": idx,
            "delta": {"type": "text_delta", "text": text},
        })

    def _close_block(idx: int) -> str:
        return _sse_event("content_block_stop", {
            "type": "content_block_stop",
            "index": idx,
        })

    try:
        async for parsed in bot_pool.poll_stream(node, req_id, after_ms=after_ms):
            ptype = parsed.get("type")
            if ptype == "stream_chunk":
                delta = parsed.get("delta") or {}
                # 优先：worker 直接预编排好的 events 列表
                events = delta.get("events")
                if events and isinstance(events, list):
                    if not started:
                        yield _start_message_event(0)
                        started = True
                    for ev in events:
                        et = ev.get("event") or ev.get("type")
                        ed = ev.get("data") or ev
                        if et:
                            yield _sse_event(et, ed)
                    continue

                # 回退：自动包装 text → text_delta
                text = delta.get("text") or ""
                if text:
                    if not started:
                        yield _start_message_event(0)
                        started = True
                    if not in_text_block:
                        yield _open_text_block(cur_block_idx)
                        in_text_block = True
                        block_started = True
                    yield _text_delta(cur_block_idx, text)

            elif ptype == "resp":
                got_resp = True
                if not parsed.get("ok", True):
                    err_msg = parsed.get("message") or parsed.get("error") or "upstream_error"
                    stop_reason = "error"
                else:
                    stop_reason = parsed.get("stop_reason") or parsed.get("finish_reason") or "end_turn"
                usage = parsed.get("usage") or {}
                p_tok = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
                c_tok = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
                break
    except Exception as e:
        logger.exception("anthropic_stream error req_id=%s", req_id)
        err_msg = f"stream_error: {type(e).__name__}: {e}"

    # 若全程没收到 chunk，至少发个空 message_start
    if not started:
        yield _start_message_event(p_tok)
        started = True

    if in_text_block:
        yield _close_block(cur_block_idx)
        in_text_block = False

    yield _sse_event("message_delta", {
        "type": "message_delta",
        "delta": {
            "stop_reason": stop_reason,
            "stop_sequence": None,
        },
        "usage": {
            "input_tokens": p_tok,
            "output_tokens": c_tok,
        },
    })
    yield _sse_event("message_stop", {"type": "message_stop"})

    if err_msg:
        # Anthropic 协议在 stream 结束后单独发 error event
        yield _sse_event("error", {
            "type": "error",
            "error": {"type": "api_error", "message": err_msg},
        })

    if got_resp and (p_tok or c_tok):
        usage_mgr.record(key_name, model, p_tok, c_tok)
        bot_pool.record_usage(node, p_tok, c_tok)
    elif not got_resp:
        usage_mgr.record_failed(key_name, model)
