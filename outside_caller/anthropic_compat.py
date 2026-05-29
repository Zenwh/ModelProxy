"""
Anthropic / Claude request compatibility helpers.

The Go downstream keeps Anthropic request fields as raw JSON-compatible values
and applies a few narrow production sanitizers before forwarding to providers.
This module mirrors those compatibility rules at the Feishu gateway boundary so
the tunnel does not lose or poison Claude native block structures before the
request reaches ModelProxy-Go.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Tuple


def is_claude_model(model: str) -> bool:
    name = (model or "").strip().lower()
    if name.startswith("ccr/"):
        name = name[4:]
    return name.startswith("claude")


def remove_input_examples_from_tools(tools: Any) -> int:
    """Remove Anthropic-unsupported ``input_examples`` from tool definitions."""
    if not isinstance(tools, list):
        return 0
    removed = 0
    for tool in tools:
        if isinstance(tool, dict) and "input_examples" in tool:
            tool.pop("input_examples", None)
            removed += 1
    return removed


def _content_has_tool_use(blocks: Iterable[Any]) -> bool:
    return any(isinstance(block, dict) and block.get("type") == "tool_use" for block in blocks)


def _drop_empty_text_blocks(blocks: list[Any]) -> Tuple[list[Any], int]:
    out: list[Any] = []
    removed = 0
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if isinstance(text, str) and text.strip() == "":
                removed += 1
                continue
        out.append(block)
    return out, removed


def sanitize_empty_text_blocks(messages: Any) -> int:
    """
    Drop empty/whitespace text blocks only when the same content array contains
    a ``tool_use`` block.
    """
    if not isinstance(messages, list):
        return 0
    removed = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list) or not _content_has_tool_use(content):
            continue
        cleaned, dropped = _drop_empty_text_blocks(content)
        if dropped:
            msg["content"] = cleaned
            removed += dropped
    return removed


def _drop_empty_thinking_blocks(blocks: list[Any]) -> Tuple[list[Any], int]:
    out: list[Any] = []
    removed = 0
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "thinking":
            signature = block.get("signature", "")
            if not isinstance(signature, str) or signature.strip() == "":
                removed += 1
                continue
        out.append(block)
    return out, removed


def sanitize_empty_thinking_signature(messages: Any) -> int:
    """Drop Claude thinking blocks with missing/empty signatures."""
    if not isinstance(messages, list):
        return 0
    removed = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        cleaned, dropped = _drop_empty_thinking_blocks(content)
        if dropped:
            msg["content"] = cleaned
            removed += dropped
    return removed


def prepare_anthropic_request(data: Dict[str, Any]) -> Dict[str, int]:
    """
    Mutate an Anthropic request dict in place and return sanitizer counters.

    Unknown top-level fields and nested block/tool fields are otherwise left
    untouched for native API fidelity.
    """
    model = str(data.get("model") or "")
    stats = {
        "removed_input_examples": remove_input_examples_from_tools(data.get("tools")),
        "dropped_empty_text": sanitize_empty_text_blocks(data.get("messages")),
        "dropped_empty_thinking": 0,
    }
    if is_claude_model(model):
        stats["dropped_empty_thinking"] = sanitize_empty_thinking_signature(
            data.get("messages")
        )
    return stats
