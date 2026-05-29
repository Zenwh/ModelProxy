from __future__ import annotations

import asyncio

from outside_caller import stream_router


def _collect(async_iter):
    async def run():
        return "".join([chunk async for chunk in async_iter])

    return asyncio.get_event_loop().run_until_complete(run())


def test_anthropic_native_events_are_not_wrapped_twice(monkeypatch):
    async def fake_poll_stream(node, req_id, *, after_ms):
        yield {
            "type": "stream_chunk",
            "delta": {
                "events": [
                    {
                        "event": "message_start",
                        "data": {
                            "type": "message_start",
                            "message": {"usage": {"input_tokens": 3}},
                        },
                    },
                    {
                        "event": "message_stop",
                        "data": {"type": "message_stop"},
                    },
                ],
            },
        }
        yield {
            "type": "resp",
            "ok": True,
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }

    monkeypatch.setattr(stream_router.bot_pool, "poll_stream", fake_poll_stream)
    monkeypatch.setattr(stream_router.usage_mgr, "record", lambda *args, **kwargs: None)
    monkeypatch.setattr(stream_router.bot_pool, "record_usage", lambda *args, **kwargs: None)

    out = _collect(stream_router.anthropic_stream_from_worker(
        node=object(),
        req_id="r1",
        model="claude-sonnet-4-6",
        after_ms=0,
        key_name="k",
    ))

    assert out.count("event: message_start") == 1
    assert out.count("event: message_stop") == 1
    assert "event: message_delta" not in out


def test_responses_stream_forwards_native_events(monkeypatch):
    async def fake_poll_stream(node, req_id, *, after_ms):
        yield {
            "type": "stream_chunk",
            "delta": {
                "events": [{
                    "event": "response.output_text.delta",
                    "data": {"type": "response.output_text.delta", "delta": "hi"},
                }],
            },
        }
        yield {
            "type": "resp",
            "ok": True,
            "usage": {"prompt_tokens": 1, "completion_tokens": 2},
        }

    monkeypatch.setattr(stream_router.bot_pool, "poll_stream", fake_poll_stream)
    monkeypatch.setattr(stream_router.usage_mgr, "record", lambda *args, **kwargs: None)
    monkeypatch.setattr(stream_router.bot_pool, "record_usage", lambda *args, **kwargs: None)

    out = _collect(stream_router.responses_stream_from_worker(
        node=object(),
        req_id="r2",
        model="gpt-5.5",
        after_ms=0,
        key_name="k",
    ))

    assert "event: response.output_text.delta" in out
    assert '"delta": "hi"' in out
    assert "event: response.completed" in out


def test_openai_stream_emits_deepseek_reasoning(monkeypatch):
    async def fake_poll_stream(node, req_id, *, after_ms):
        yield {
            "type": "stream_chunk",
            "delta": {"thinking": "reason"},
        }
        yield {
            "type": "resp",
            "ok": True,
            "usage": {"prompt_tokens": 1, "completion_tokens": 2},
        }

    monkeypatch.setattr(stream_router.bot_pool, "poll_stream", fake_poll_stream)
    monkeypatch.setattr(stream_router.usage_mgr, "record", lambda *args, **kwargs: None)
    monkeypatch.setattr(stream_router.bot_pool, "record_usage", lambda *args, **kwargs: None)

    out = _collect(stream_router.openai_stream_from_worker(
        node=object(),
        req_id="r3",
        model="deepseek-v3.2-think-ks",
        after_ms=0,
        key_name="k",
    ))

    assert '"reasoning_content": "reason"' in out
    assert "data: [DONE]" in out
