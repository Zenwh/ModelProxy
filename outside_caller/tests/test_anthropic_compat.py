from __future__ import annotations

from outside_caller.anthropic_compat import (
    is_claude_model,
    prepare_anthropic_request,
    sanitize_empty_text_blocks,
    sanitize_empty_thinking_signature,
)


def test_prepare_preserves_unknown_fields_and_removes_input_examples():
    req = {
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "hi"}],
        "thinking": {"type": "enabled", "budget_tokens": 1024},
        "cache_control": {"type": "ephemeral"},
        "container": {"id": "c1"},
        "mcp_servers": [{"name": "fs"}],
        "tools": [{
            "name": "search",
            "input_schema": {"type": "object"},
            "input_examples": [{"query": "x"}],
            "cache_control": {"type": "ephemeral"},
        }],
    }

    stats = prepare_anthropic_request(req)

    assert stats["removed_input_examples"] == 1
    assert "input_examples" not in req["tools"][0]
    assert req["tools"][0]["cache_control"] == {"type": "ephemeral"}
    assert req["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert req["cache_control"] == {"type": "ephemeral"}
    assert req["container"] == {"id": "c1"}
    assert req["mcp_servers"] == [{"name": "fs"}]


def test_empty_text_removed_only_next_to_tool_use():
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "  "},
                {"type": "tool_use", "id": "t1", "name": "x", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "text", "text": "  "}],
        },
    ]

    assert sanitize_empty_text_blocks(messages) == 1
    assert messages[0]["content"] == [
        {"type": "tool_use", "id": "t1", "name": "x", "input": {}},
    ]
    assert messages[1]["content"] == [{"type": "text", "text": "  "}]


def test_empty_thinking_signature_removed_for_claude():
    messages = [{
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "draft", "signature": ""},
            {"type": "thinking", "thinking": "ok", "signature": "sig"},
            {"type": "text", "text": "done"},
        ],
    }]

    assert sanitize_empty_thinking_signature(messages) == 1
    assert messages[0]["content"] == [
        {"type": "thinking", "thinking": "ok", "signature": "sig"},
        {"type": "text", "text": "done"},
    ]


def test_is_claude_model_accepts_ccr_prefix():
    assert is_claude_model("claude-sonnet-4-6")
    assert is_claude_model("ccr/claude-sonnet-4-6")
    assert not is_claude_model("gpt-5.5")
