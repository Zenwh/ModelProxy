"""
Token 估算（简版）：char/4 + per-message overhead。

不依赖 tiktoken / anthropic-tokenizer，避免给 gateway 加重量级依赖。
误差控制在 ±15-25% 内；超过 1M tokens 时 truncator 用这个估算切片，
保守一点宁可多截一点也不冒爆 MP context window 的风险。
"""
from __future__ import annotations

from typing import Any, Iterable, List, Optional

# 经验值：英文 ~4 chars/token，中文 ~2 chars/token。简单平均偏保守 → 3.5。
_CHARS_PER_TOKEN = 3.5
# 每条 message 的 role + 分隔符等固定开销
_PER_MESSAGE_OVERHEAD = 20
# 每个 tool 定义大致开销（schema + name + description）
_PER_TOOL_OVERHEAD = 50


def _text_tokens(text: str) -> int:
    if not text:
        return 0
    return int(len(text) / _CHARS_PER_TOKEN) + 1


def _content_tokens(content: Any) -> int:
    """Anthropic content 可以是 str / List[block]，block 可能是 text / tool_use / tool_result / image。"""
    if content is None:
        return 0
    if isinstance(content, str):
        return _text_tokens(content)
    if not isinstance(content, list):
        return _text_tokens(str(content))
    total = 0
    for blk in content:
        if not isinstance(blk, dict):
            total += _text_tokens(str(blk))
            continue
        btype = blk.get("type")
        if btype == "text":
            total += _text_tokens(blk.get("text", ""))
        elif btype == "tool_use":
            total += _text_tokens(blk.get("name", "")) + 10
            inp = blk.get("input")
            if inp is not None:
                import json as _json
                total += _text_tokens(_json.dumps(inp, ensure_ascii=False))
        elif btype == "tool_result":
            total += 10
            inner = blk.get("content")
            total += _content_tokens(inner)
        elif btype == "image":
            # vision 输入算 ~1500 tokens（保守值，实际取决于尺寸）
            total += 1500
        else:
            import json as _json
            total += _text_tokens(_json.dumps(blk, ensure_ascii=False))
    return total


def _system_tokens(system: Any) -> int:
    """system 可以是 str / List[{type:text,text:...}]。"""
    if system is None:
        return 0
    if isinstance(system, str):
        return _text_tokens(system)
    if isinstance(system, list):
        total = 0
        for blk in system:
            if isinstance(blk, dict):
                total += _text_tokens(blk.get("text", ""))
            else:
                total += _text_tokens(str(blk))
        return total
    return _text_tokens(str(system))


def _tool_tokens(tool: Any) -> int:
    if not isinstance(tool, dict):
        return _PER_TOOL_OVERHEAD
    import json as _json
    name = tool.get("name", "")
    desc = tool.get("description", "")
    schema = tool.get("input_schema") or tool.get("parameters") or {}
    return (
        _text_tokens(name)
        + _text_tokens(desc)
        + _text_tokens(_json.dumps(schema, ensure_ascii=False))
        + _PER_TOOL_OVERHEAD
    )


def estimate(
    messages: Optional[Iterable[Any]] = None,
    system: Optional[Any] = None,
    tools: Optional[Iterable[Any]] = None,
) -> int:
    """估算总 prompt token 数。"""
    total = _system_tokens(system)
    for tool in tools or []:
        total += _tool_tokens(tool)
    for msg in messages or []:
        if isinstance(msg, dict):
            total += _content_tokens(msg.get("content")) + _PER_MESSAGE_OVERHEAD
        else:
            # ChatMessage / AnthropicMessage pydantic model
            content = getattr(msg, "content", None)
            total += _content_tokens(content) + _PER_MESSAGE_OVERHEAD
    return total


def estimate_message(msg: Any) -> int:
    """估算单条 message 的 token 占用（含 overhead）。truncator 反向累加用。"""
    if isinstance(msg, dict):
        content = msg.get("content")
    else:
        content = getattr(msg, "content", None)
    return _content_tokens(content) + _PER_MESSAGE_OVERHEAD
