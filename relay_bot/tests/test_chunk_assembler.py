"""ChunkAssembler 单元测试：乱序、重复、超时回调、part_total 不一致。"""
from __future__ import annotations

import json
import time

import pytest

from relay_bot.chunk_assembler import ChunkAssembler


def _split(payload: dict, n: int) -> list[str]:
    s = json.dumps(payload, ensure_ascii=False)
    size = (len(s) + n - 1) // n
    return [s[i * size : (i + 1) * size] for i in range(n)]


def test_in_order_assembly():
    asm = ChunkAssembler()
    payload = {"hello": "world", "items": list(range(50))}
    parts = _split(payload, 4)

    out = None
    for idx, chunk in enumerate(parts):
        out = asm.add_part("req-1", idx, len(parts), chunk, chat_id="c1")
        if idx < len(parts) - 1:
            assert out is None
    assert out == payload


def test_out_of_order_assembly():
    asm = ChunkAssembler()
    payload = {"a": "x" * 100, "b": list(range(80))}
    parts = _split(payload, 5)
    order = [3, 1, 4, 0, 2]
    out = None
    for i, idx in enumerate(order):
        out = asm.add_part("req-2", idx, len(parts), parts[idx], chat_id="c1")
        if i < len(order) - 1:
            assert out is None
    assert out == payload


def test_duplicate_part_idempotent():
    asm = ChunkAssembler()
    payload = {"hi": "there"}
    parts = _split(payload, 2)
    assert asm.add_part("req-3", 0, 2, parts[0], chat_id="c1") is None
    # 重发 part 0 不应推进 received
    assert asm.add_part("req-3", 0, 2, parts[0], chat_id="c1") is None
    out = asm.add_part("req-3", 1, 2, parts[1], chat_id="c1")
    assert out == payload


def test_part_total_mismatch_dropped():
    asm = ChunkAssembler()
    asm.add_part("req-4", 0, 3, "{", chat_id="c1")
    # 后续片声称 total=5 → 应被丢弃，原 inflight 还在
    res = asm.add_part("req-4", 1, 5, "x", chat_id="c1")
    assert res is None
    assert asm.inflight_count == 1


def test_invalid_indices():
    asm = ChunkAssembler()
    assert asm.add_part("req-5", -1, 3, "x") is None
    assert asm.add_part("req-5", 3, 3, "x") is None
    assert asm.add_part("req-5", 0, 0, "x") is None


def test_timeout_triggers_callback():
    captured: list[tuple] = []

    def on_timeout(req_id, chat_id, info):
        captured.append((req_id, chat_id, info))

    asm = ChunkAssembler(timeout_s=1, on_timeout=on_timeout, sweep_interval_s=1)
    asm.start()
    try:
        asm.add_part("req-6", 0, 3, "{", chat_id="chat-x")
        # 等待超时 + 一次 sweep
        time.sleep(2.5)
        assert any(c[0] == "req-6" for c in captured), captured
        triggered = next(c for c in captured if c[0] == "req-6")
        assert triggered[1] == "chat-x"
        assert triggered[2]["expected"] == 3
        assert triggered[2]["received"] == 1
    finally:
        asm.stop()


def test_assembly_clears_inflight():
    asm = ChunkAssembler()
    payload = {"x": 1}
    parts = _split(payload, 2)
    asm.add_part("req-7", 0, 2, parts[0])
    assert asm.inflight_count == 1
    asm.add_part("req-7", 1, 2, parts[1])
    assert asm.inflight_count == 0


def test_meta_only_from_part_zero():
    asm = ChunkAssembler()
    payload = {"k": "v"}
    parts = _split(payload, 2)
    asm.add_part("req-8", 1, 2, parts[1], meta={"endpoint": "ignored"})
    # part 0 的 meta 才应被记录；这里 part 1 带 meta 应被忽略
    out = asm.add_part("req-8", 0, 2, parts[0], meta={"endpoint": "chat"})
    assert out == payload


def test_zb64_compressed_assembly():
    """payload_encoding=zb64 时，组装后用 base64+zlib 还原。"""
    import base64
    import zlib

    asm = ChunkAssembler()
    payload = {"messages": [{"role": "user", "content": "a" * 5000}] * 20}
    raw = json.dumps(payload).encode("utf-8")
    compressed_b64 = base64.b64encode(zlib.compress(raw, level=6)).decode("ascii")
    # 分 3 片
    n = 3
    size = (len(compressed_b64) + n - 1) // n
    parts = [compressed_b64[i * size : (i + 1) * size] for i in range(n)]

    out = asm.add_part(
        "req-z", 0, n, parts[0], chat_id="c1",
        meta={"payload_encoding": "zb64", "endpoint": "chat"},
    )
    assert out is None
    out = asm.add_part("req-z", 1, n, parts[1], chat_id="c1")
    assert out is None
    out = asm.add_part("req-z", 2, n, parts[2], chat_id="c1")
    assert out == payload


def test_zb64_corrupt_returns_none():
    """zb64 base64 损坏时返回 None 而非抛异常。"""
    asm = ChunkAssembler()
    out = asm.add_part(
        "req-bad", 0, 1, "not-valid-base64!!!", chat_id="c1",
        meta={"payload_encoding": "zb64"},
    )
    assert out is None
