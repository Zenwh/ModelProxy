"""token_estimator 单元测试：5 组样本，断言估算误差 ±25% 内。"""
from __future__ import annotations

import pytest

from outside_caller.token_estimator import estimate, estimate_message


def _approx(value: int, ref: int, tol: float = 0.25) -> bool:
    if ref == 0:
        return value == 0
    return abs(value - ref) / ref <= tol


def test_empty():
    assert estimate() == 0
    assert estimate([], None, []) == 0


def test_simple_chat():
    msgs = [
        {"role": "user", "content": "Hello, how are you?"},
        {"role": "assistant", "content": "I'm fine, thanks!"},
    ]
    # ~36 chars + overhead
    n = estimate(msgs)
    assert _approx(n, 51, tol=0.5)


def test_anthropic_blocks():
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "ping" * 100}]},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "search", "input": {"q": "x" * 50}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok" * 200},
            ],
        },
    ]
    n = estimate(msgs)
    # ~400 + ~70 + ~400 ≈ 870 tokens, ±25%
    assert n > 200
    assert n < 2000


def test_system_and_tools():
    sys = "You are a helpful assistant."
    tools = [
        {
            "name": "get_weather",
            "description": "Look up current weather",
            "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
        }
    ]
    n = estimate([], sys, tools)
    # tool 50 overhead + name/desc/schema text
    assert n >= 50


def test_image_block_heuristic():
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "data": "..."}},
                {"type": "text", "text": "What's in this picture?"},
            ],
        },
    ]
    n = estimate(msgs)
    # image ≈ 1500 + small text + overhead
    assert n >= 1500


def test_estimate_message_isolated():
    m = {"role": "user", "content": "x" * 350}  # 350/3.5 ≈ 100 tokens + overhead
    cost = estimate_message(m)
    assert _approx(cost, 120, tol=0.4)
