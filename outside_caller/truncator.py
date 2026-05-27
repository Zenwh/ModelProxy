"""
中间截断：超长上下文时保留 system + tools + 尾部最近 messages，中间一段插一条
"[Earlier messages truncated]" 标记给模型一个 hint。

切点对齐规则：
- 不能切断 tool_use / tool_result 配对：tool_use 在 assistant 里，tool_result 在
  user 里；如果尾部包含 tool_result，必须把对应 tool_use（assistant 上一条）一起保留。
- 优先保留 user→assistant→user→... 的完整轮次：从尾向头累加，遇到 user 时再判断
  budget 是否耗尽；耗尽就停在那个 user 之前。

base = system + tools + 头部截断标记，本身就要超过 budget → 抛 413。
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

from .errors import OpenAIError
from .token_estimator import (
    _content_tokens,
    _system_tokens,
    _tool_tokens,
    _PER_MESSAGE_OVERHEAD,
    estimate_message,
)

logger = logging.getLogger("truncator")

_TRUNCATION_MARKER_TEXT = "[Earlier messages truncated to fit context window]"


def _make_marker(role: str = "user") -> dict:
    """生成一条占位 message。Anthropic 端 user 必须有内容；OpenAI 端也一样。"""
    return {"role": role, "content": _TRUNCATION_MARKER_TEXT}


def _is_dict(msg: Any) -> bool:
    return isinstance(msg, dict)


def _msg_role(msg: Any) -> str:
    if _is_dict(msg):
        return msg.get("role", "")
    return getattr(msg, "role", "")


def _msg_content(msg: Any) -> Any:
    if _is_dict(msg):
        return msg.get("content")
    return getattr(msg, "content", None)


def _msg_has_tool_result(msg: Any) -> bool:
    """user 消息里若含 tool_result block，则上一条 assistant 必须保留（含 tool_use）。"""
    if _msg_role(msg) != "user":
        return False
    content = _msg_content(msg)
    if not isinstance(content, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "tool_result"
        for b in content
    )


def _msg_has_tool_use(msg: Any) -> bool:
    if _msg_role(msg) != "assistant":
        return False
    content = _msg_content(msg)
    if not isinstance(content, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "tool_use"
        for b in content
    )


def truncate(
    messages: List[Any],
    system: Optional[Any] = None,
    tools: Optional[List[Any]] = None,
    budget: int = 1_000_000,
) -> Tuple[List[dict], dict]:
    """
    返回 (truncated_messages_as_dicts, info)。

    info: {
        "truncated": bool,
        "kept_count": int,
        "dropped_count": int,
        "estimated_tokens": int,
        "marker_inserted": bool,
    }

    base_tokens (system + tools + marker overhead) 已经 >= budget → 抛 OpenAIError(413)。
    """
    base = _system_tokens(system)
    for tool in tools or []:
        base += _tool_tokens(tool)
    # 预留 marker overhead（即使最终不插入也不影响，保守估算）
    marker_tokens = _content_tokens(_TRUNCATION_MARKER_TEXT) + _PER_MESSAGE_OVERHEAD
    safety_margin = 1024  # 给响应留 ~1K tokens 缓冲

    if base + marker_tokens + safety_margin >= budget:
        raise OpenAIError(
            "invalid_request_error",
            f"system + tools size ({base} tokens) exceeds budget ({budget}). "
            f"Reduce system prompt or tool definitions.",
            status=413,
            param="system",
        )

    msgs_as_dicts: List[dict] = [
        m if _is_dict(m) else m.model_dump() if hasattr(m, "model_dump") else dict(m)  # type: ignore[arg-type]
        for m in messages
    ]

    # 计算总 token
    total_msg_tokens = sum(estimate_message(m) for m in msgs_as_dicts)
    total = base + total_msg_tokens

    if total <= budget:
        return msgs_as_dicts, {
            "truncated": False,
            "kept_count": len(msgs_as_dicts),
            "dropped_count": 0,
            "estimated_tokens": total,
            "marker_inserted": False,
        }

    # 尾部累加直到 budget 耗尽
    available = budget - base - marker_tokens - safety_margin
    if available <= 0:
        raise OpenAIError(
            "invalid_request_error",
            f"system + tools too large to fit any message; reduce them.",
            status=413,
            param="system",
        )

    kept: List[dict] = []
    used = 0
    for msg in reversed(msgs_as_dicts):
        cost = estimate_message(msg)
        if used + cost > available:
            break
        kept.append(msg)
        used += cost
    kept.reverse()

    # 切点合法化：
    # 1) 头部不能是 tool_result（会缺少对应 tool_use）→ 删除头部连续的 tool_result
    # 2) 头部最好是 user role，不是 user 就再裁掉
    while kept:
        head = kept[0]
        if _msg_has_tool_result(head):
            kept.pop(0)
            continue
        if _msg_role(head) != "user":
            kept.pop(0)
            continue
        break

    # 头部插入截断标记。如果第一条就是 user，marker 也用 user role 会和它合并难看；
    # 但 Anthropic 协议允许 user 连续两条（会被服务器合并），且这里 marker 在前更明确。
    # OpenAI 协议同样允许多条 user。
    truncated_msgs: List[dict] = [_make_marker("user")] + kept

    dropped = len(msgs_as_dicts) - len(kept)
    final_total = base + marker_tokens + sum(estimate_message(m) for m in kept)

    logger.info(
        "truncate: budget=%d total=%d kept=%d dropped=%d final=%d",
        budget, total, len(kept), dropped, final_total,
    )

    return truncated_msgs, {
        "truncated": True,
        "kept_count": len(kept),
        "dropped_count": dropped,
        "estimated_tokens": final_total,
        "marker_inserted": True,
    }
