"""truncator 单元测试：保留尾部、头部插标记、base 爆 budget 抛 413。"""
from __future__ import annotations

import pytest

from outside_caller.errors import OpenAIError
from outside_caller.truncator import truncate, _TRUNCATION_MARKER_TEXT


def _msg(role: str, text: str) -> dict:
    return {"role": role, "content": text}


def test_no_truncation_under_budget():
    msgs = [_msg("user", "hi"), _msg("assistant", "hello")]
    out, info = truncate(msgs, system="be kind", tools=None, budget=1_000_000)
    assert info["truncated"] is False
    assert info["kept_count"] == 2
    assert info["dropped_count"] == 0
    assert out == msgs


def test_truncates_keeps_tail_inserts_marker():
    # 构造 ~1.2M tokens：每条 4200 chars ≈ 1200 tokens，1000 条 ≈ 1.2M tokens
    msgs = []
    for i in range(1000):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append(_msg(role, f"msg-{i}-" + ("x" * 4000)))
    out, info = truncate(msgs, system=None, tools=None, budget=100_000)
    assert info["truncated"] is True
    assert info["kept_count"] < 1000
    assert info["dropped_count"] > 0
    assert info["marker_inserted"] is True
    # 头部第一条应是 marker
    assert out[0]["role"] == "user"
    assert _TRUNCATION_MARKER_TEXT in out[0]["content"]
    # 尾部最后一条应来自原始末尾
    assert "msg-999" in out[-1]["content"]


def test_base_overflow_raises_413():
    huge_system = "x" * 5_000_000  # ~1.4M tokens
    with pytest.raises(OpenAIError) as exc:
        truncate([_msg("user", "hi")], system=huge_system, budget=1_000_000)
    assert exc.value.status_code == 413
    assert exc.value.error_type == "invalid_request_error"


def test_tool_result_orphan_dropped_at_head():
    """若尾部窗口起点是孤立的 tool_result（缺前置 tool_use）→ 应被裁掉。"""
    # 大量填充把窗口推到中间
    msgs = []
    for i in range(200):
        msgs.append(_msg("user", "u" * 4000))
        msgs.append(_msg("assistant", "a" * 4000))
    # 末尾插入一对完整 tool_use / tool_result
    msgs.append({
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}],
    })
    msgs.append({
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "done"}],
    })
    out, info = truncate(msgs, budget=50_000)
    assert info["truncated"] is True
    # 头部 marker 之后第一条不应是 tool_result
    assert out[0]["content"] == _TRUNCATION_MARKER_TEXT
    after_marker = out[1]
    content = after_marker.get("content")
    if isinstance(content, list):
        for blk in content:
            assert blk.get("type") != "tool_result"


def test_pydantic_messages_via_model_dump():
    """truncate 接收带 model_dump 的 pydantic 风格对象。"""
    class FakeMsg:
        def __init__(self, role, content):
            self.role = role
            self.content = content
        def model_dump(self):
            return {"role": self.role, "content": self.content}
    msgs = [FakeMsg("user", "ping"), FakeMsg("assistant", "pong")]
    out, info = truncate(msgs, budget=1_000_000)
    assert info["truncated"] is False
    assert out[0] == {"role": "user", "content": "ping"}
