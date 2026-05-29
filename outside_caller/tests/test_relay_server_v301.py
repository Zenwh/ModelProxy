from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from outside_caller.errors import OpenAIError
from outside_caller.relay_server import (
    ChatRequest,
    _apply_deepseek_chat_template_kwargs,
    _force_chat_stream_usage,
    _legacy_responses_object,
    chat_completions,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_chat_request_preserves_extra_fields_and_message_fields():
    req = ChatRequest(
        model="deepseek-v3.2-think-ks",
        messages=[{
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "x", "arguments": "{}"},
            }],
        }],
        response_format={"type": "json_object"},
        tool_choice="auto",
    )

    data = req.model_dump(exclude_none=True)

    assert data["response_format"] == {"type": "json_object"}
    assert data["tool_choice"] == "auto"
    assert data["messages"][0]["tool_calls"][0]["id"] == "call_1"


def test_deepseek_template_kwargs_and_stream_usage():
    payload = {"model": "deepseek-v3.2-think-ks", "stream": True}
    _apply_deepseek_chat_template_kwargs(payload)
    _force_chat_stream_usage(payload)

    assert payload["chat_template_kwargs"] == {"thinking": True}
    assert payload["stream_options"] == {"include_usage": True}

    payload = {"model": "deepseek-v3.2-ks", "stream": False}
    _apply_deepseek_chat_template_kwargs(payload)
    assert payload["chat_template_kwargs"] == {"thinking": False}

    payload = {"model": "ccr/deepseek-v3.2-think-ks", "stream": False}
    _apply_deepseek_chat_template_kwargs(payload)
    assert payload["chat_template_kwargs"] == {"thinking": True}


def test_claude_chat_completions_returns_use_messages_error(monkeypatch):
    monkeypatch.setattr(
        "outside_caller.relay_server._check_auth",
        lambda request: SimpleNamespace(name="test", rpm_limit=None, daily_token_limit=None),
    )
    req = ChatRequest(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
    )

    with pytest.raises(OpenAIError) as exc:
        _run(chat_completions(req, request=SimpleNamespace()))

    assert exc.value.status_code == 400
    assert exc.value.error_code == "use_messages_endpoint"
    assert "/v1/messages" in exc.value.detail


def test_legacy_worker_response_can_be_wrapped_as_responses_object():
    obj = _legacy_responses_object(
        req_id="r1",
        model="gpt-5.5",
        content="PONG",
        usage={"prompt_tokens": 3, "completion_tokens": 2},
    )

    assert obj["id"] == "resp_r1"
    assert obj["object"] == "response"
    assert obj["status"] == "completed"
    assert obj["output"][0]["content"][0]["type"] == "output_text"
    assert obj["output"][0]["content"][0]["text"] == "PONG"
    assert obj["usage"] == {
        "input_tokens": 3,
        "output_tokens": 2,
        "total_tokens": 5,
    }
