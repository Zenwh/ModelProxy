"""
Anthropic SSE 事件流生成（伪流式）：
把 MP /v1/messages 的完整响应转成 Anthropic Messages stream 事件序列。

Anthropic 官方流协议事件类型：
  - message_start
  - content_block_start (per block)
  - content_block_delta (text_delta / input_json_delta)
  - content_block_stop
  - message_delta (stop_reason + usage 增量)
  - message_stop

参考：https://docs.anthropic.com/en/api/messages-streaming
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Dict, List


def _sse_event(event_type: str, data: Dict[str, Any]) -> str:
    """构造一条 SSE event。"""
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _split_text(text: str, chunk_chars: int = 12) -> List[str]:
    if not text:
        return []
    return [text[i:i + chunk_chars] for i in range(0, len(text), chunk_chars)]


async def anthropic_sse_stream(
    raw: Dict[str, Any],
    chunk_chars: int = 12,
    chunk_delay: float = 0.02,
) -> AsyncIterator[str]:
    """
    把 raw_anthropic 响应（来自 MP /v1/messages）转成 Anthropic SSE 事件流。

    raw 形如:
    {
      "id": "msg_xxx",
      "type": "message",
      "role": "assistant",
      "model": "claude-opus-4-7",
      "content": [
        {"type": "text", "text": "..."},
        {"type": "tool_use", "id": "toolu_xxx", "name": "...", "input": {...}}
      ],
      "stop_reason": "end_turn|tool_use|max_tokens|stop_sequence",
      "stop_sequence": null,
      "usage": {"input_tokens": N, "output_tokens": N, ...}
    }
    """
    msg_id = raw.get("id", "msg_unknown")
    model = raw.get("model", "")
    usage = raw.get("usage", {}) or {}
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    stop_reason = raw.get("stop_reason")
    stop_sequence = raw.get("stop_sequence")
    content_blocks = raw.get("content", []) or []

    # 1. message_start —— 含 message 元数据，content 字段为空数组
    yield _sse_event("message_start", {
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
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
            },
        },
    })

    # 2. 每个 block 一组 start / delta(s) / stop
    for idx, block in enumerate(content_blocks):
        btype = block.get("type")

        if btype == "text":
            text = block.get("text", "") or ""
            yield _sse_event("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {"type": "text", "text": ""},
            })
            for piece in _split_text(text, chunk_chars):
                yield _sse_event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "text_delta", "text": piece},
                })
                if chunk_delay > 0:
                    await asyncio.sleep(chunk_delay)
            yield _sse_event("content_block_stop", {
                "type": "content_block_stop",
                "index": idx,
            })

        elif btype == "tool_use":
            tool_id = block.get("id", "")
            tool_name = block.get("name", "")
            tool_input = block.get("input", {}) or {}
            yield _sse_event("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": tool_name,
                    "input": {},
                },
            })
            # input 序列化后整段一次性 partial_json delta
            json_str = json.dumps(tool_input, ensure_ascii=False)
            yield _sse_event("content_block_delta", {
                "type": "content_block_delta",
                "index": idx,
                "delta": {"type": "input_json_delta", "partial_json": json_str},
            })
            yield _sse_event("content_block_stop", {
                "type": "content_block_stop",
                "index": idx,
            })

        else:
            # 未知 block 类型：原样作为 content_block_start 透出
            yield _sse_event("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": block,
            })
            yield _sse_event("content_block_stop", {
                "type": "content_block_stop",
                "index": idx,
            })

    # 3. message_delta —— stop_reason / stop_sequence + usage 增量
    yield _sse_event("message_delta", {
        "type": "message_delta",
        "delta": {
            "stop_reason": stop_reason,
            "stop_sequence": stop_sequence,
        },
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    })

    # 4. message_stop
    yield _sse_event("message_stop", {"type": "message_stop"})
