from __future__ import annotations

from relay_bot.config import Config
from relay_bot.worker import Worker


def _worker() -> Worker:
    return Worker(Config(node_id="test-node", mp_url="http://mp.local", mp_api_key="sk-test"))


def test_build_chat_payload_preserves_extra_fields():
    worker = _worker()
    path, payload = worker._build_chat_payload({
        "_relay_v": 3,
        "type": "req",
        "req_id": "r1",
        "endpoint": "chat",
        "model": "deepseek-v3.2-think-ks",
        "messages": [{
            "role": "assistant",
            "tool_calls": [{"id": "call_1"}],
        }],
        "tools": [{"type": "function", "function": {"name": "x"}}],
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"thinking": True},
        "stream_options": {"include_usage": True},
    }, stream=True)

    assert path == "/v1/chat/completions"
    assert payload["stream"] is True
    assert payload["tools"][0]["function"]["name"] == "x"
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["chat_template_kwargs"] == {"thinking": True}
    assert payload["messages"][0]["tool_calls"][0]["id"] == "call_1"
    assert "req_id" not in payload


def test_build_responses_payload_preserves_native_input_and_maps_chat_messages():
    worker = _worker()
    path, payload = worker._build_chat_payload({
        "endpoint": "responses",
        "model": "gpt-5.5",
        "input": [{"role": "user", "content": "hi"}],
        "reasoning": {"effort": "medium"},
        "metadata": {"k": "v"},
    }, stream=False)

    assert path == "/v1/responses"
    assert payload["input"] == [{"role": "user", "content": "hi"}]
    assert payload["reasoning"] == {"effort": "medium"}
    assert payload["metadata"] == {"k": "v"}

    _, chat_payload = worker._build_chat_payload({
        "endpoint": "responses",
        "model": "gpt-5.5",
        "messages": [{"role": "user", "content": "hi"}],
    }, stream=True)
    assert chat_payload["input"] == [{"role": "user", "content": "hi"}]
    assert "messages" not in chat_payload

    _, native_payload = worker._build_chat_payload({
        "endpoint": "responses",
        "model": "gpt-5.5",
        "input": "hi",
        "messages": "legacy shim",
    }, stream=False)
    assert native_payload["input"] == "hi"
    assert "messages" not in native_payload


def test_call_messages_native_passes_anthropic_headers(monkeypatch):
    worker = _worker()
    captured = {}

    def fake_post(path, payload, extra_headers=None):
        captured["path"] = path
        captured["payload"] = payload
        captured["headers"] = extra_headers
        return 200, {"content": [{"type": "text", "text": "ok"}], "usage": {}}

    monkeypatch.setattr(worker, "_mp_post", fake_post)
    status, result = worker._call_mp_messages_native({
        "_relay_v": 3,
        "type": "req",
        "req_id": "r1",
        "endpoint": "messages",
        "mode": "messages_native",
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "hi"}],
        "anthropic_headers": {"anthropic-beta": "tools-2024-01-01"},
        "thinking": {"type": "enabled", "budget_tokens": 1024},
    })

    assert status == 200
    assert result["raw_anthropic"]["content"][0]["text"] == "ok"
    assert captured["path"] == "/v1/messages"
    assert captured["headers"] == {"anthropic-beta": "tools-2024-01-01"}
    assert "anthropic_headers" not in captured["payload"]
    assert captured["payload"]["thinking"] == {"type": "enabled", "budget_tokens": 1024}


def test_call_responses_returns_raw_response(monkeypatch):
    worker = _worker()
    raw = {
        "id": "resp_1",
        "object": "response",
        "output": [],
        "usage": {"input_tokens": 4, "output_tokens": 5},
    }
    monkeypatch.setattr(worker, "_mp_post", lambda path, payload: (200, raw))

    status, result = worker._call_mp_chat({
        "endpoint": "responses",
        "model": "gpt-5.5",
        "input": "hi",
    })

    assert status == 200
    assert result["raw_response"] == raw
    assert result["usage"] == {
        "prompt_tokens": 4,
        "completion_tokens": 5,
        "total_tokens": 9,
    }
