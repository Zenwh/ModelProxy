"""bot_pool.send_request 单元测试：v3 多片切割 + 顺序 + meta 仅 part_index=0。"""
from __future__ import annotations

import asyncio
import json

import pytest

from outside_caller import config
from outside_caller.bot_pool import BotNode, BotPool


def _make_pool(monkeypatch, captured: list):
    """构造一个 pool，monkeypatch send_to_bot 捕获每条上行 envelope。"""
    pool = BotPool.__new__(BotPool)
    # 仅初始化测试需要的字段
    pool._nodes = {}
    pool._lock = __import__("threading").Lock()

    async def fake_send(node, payload, *, allow_compress=True):
        # send_request 内部传入的就是 dict envelope
        captured.append((node.node_id, payload, allow_compress))
        return {"code": 0}

    pool.send_to_bot = fake_send  # type: ignore[assignment]
    return pool


def _node():
    return BotNode(node_id="n1", open_id="ou_x", chat_id="c1")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_single_part_for_small_payload(monkeypatch):
    captured: list = []
    pool = _make_pool(monkeypatch, captured)
    payload = {
        "_relay_v": 3, "type": "req", "req_id": "abc", "endpoint": "chat",
        "model": "gpt-5.5", "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
    }
    total = _run(pool.send_request(_node(), payload))
    assert total == 1
    assert len(captured) == 1
    env = captured[0][1]
    assert env["type"] == "req_part"
    assert env["part_index"] == 0
    assert env["part_total"] == 1
    assert env["endpoint"] == "chat"
    assert env["model"] == "gpt-5.5"
    assert env["stream"] is False


def test_multipart_split_and_reassemble(monkeypatch):
    captured: list = []
    pool = _make_pool(monkeypatch, captured)
    # 强制小 chunk size，让小 payload 也能切多片（绕过 max(1024, ...)）
    monkeypatch.setattr(config, "MULTIPART_CHUNK_BYTES", 1024)
    big = "x" * 4096  # 4KB
    payload = {
        "_relay_v": 3, "type": "req", "req_id": "r2", "endpoint": "messages",
        "mode": "messages_native", "model": "claude", "stream": True,
        "messages": [{"role": "user", "content": big}],
    }
    body = json.dumps(payload, ensure_ascii=False)
    expected_total = (len(body.encode("utf-8")) + 1023) // 1024

    total = _run(pool.send_request(_node(), payload))
    assert total == expected_total
    assert len(captured) == expected_total

    # 1) part_index 单调递增
    for i, (_, env, _) in enumerate(captured):
        assert env["part_index"] == i
        assert env["part_total"] == expected_total
        assert env["req_id"] == "r2"

    # 2) 仅 part 0 携带 meta
    assert captured[0][1]["endpoint"] == "messages"
    assert captured[0][1]["mode"] == "messages_native"
    assert captured[0][1]["stream"] is True
    for _, env, _ in captured[1:]:
        assert "endpoint" not in env
        assert "mode" not in env
        assert "stream" not in env
        assert "model" not in env

    # 3) 拼回 payload_chunk 应等价于原 body
    rebuilt = "".join(env["payload_chunk"] for _, env, _ in captured)
    assert rebuilt == body
    assert json.loads(rebuilt) == payload


def test_missing_req_id_raises(monkeypatch):
    captured: list = []
    pool = _make_pool(monkeypatch, captured)
    with pytest.raises(ValueError):
        _run(pool.send_request(_node(), {"endpoint": "chat"}))


def test_compression_disabled_for_parts(monkeypatch):
    """req_part 已是切片，不再压缩 → allow_compress=False 应该传到 send_to_bot。"""
    captured: list = []
    pool = _make_pool(monkeypatch, captured)
    payload = {"_relay_v": 3, "req_id": "r3", "endpoint": "chat", "model": "x", "stream": False}
    _run(pool.send_request(_node(), payload))
    assert all(allow is False for _, _, allow in captured)
