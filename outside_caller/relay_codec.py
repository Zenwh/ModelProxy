"""
relay_codec — 飞书消息通道编解码。

NOTE: this module is a verbatim copy of the same implementation in the
``feishu-relay-bot`` repository.  Both repos are maintained in parallel so
any change here MUST be mirrored there, and vice-versa.  This should
eventually be extracted into a shared PyPI package, but for now keep both
copies in sync manually.
"""
from __future__ import annotations

import base64
import json
import logging
import zlib
from typing import Any, Dict

logger = logging.getLogger("relay-codec")

FEISHU_LIMIT = 140_000
COMPRESS_THRESHOLD = 50_000


class PayloadTooLargeError(Exception):
    def __init__(self, raw_size: int, encoded_size: int = 0):
        self.raw_size = raw_size
        self.encoded_size = encoded_size
        self.size_kb = raw_size / 1024
        super().__init__(
            f"Payload too large: {raw_size} bytes "
            f"(limit {FEISHU_LIMIT} bytes, ~{FEISHU_LIMIT // 1024}KB)"
        )


_ZLIB_PREFIX = "__zb64__"


def encode(payload: Dict[str, Any], *, allow_compress: bool = True) -> str:
    raw = json.dumps(payload, ensure_ascii=False)
    raw_bytes = raw.encode("utf-8")
    size = len(raw_bytes)

    if size <= COMPRESS_THRESHOLD or not allow_compress:
        if size > FEISHU_LIMIT:
            raise PayloadTooLargeError(size)
        return raw

    compressed = zlib.compress(raw_bytes, level=6)
    b64 = base64.b64encode(compressed).decode("ascii")
    encoded = _ZLIB_PREFIX + b64
    encoded_size = len(encoded.encode("utf-8"))

    if encoded_size > FEISHU_LIMIT:
        raise PayloadTooLargeError(size, encoded_size)

    logger.debug("compressed %d -> %d bytes (%.1fx)", size, encoded_size, size / encoded_size)
    return encoded


def decode(text: str) -> Dict[str, Any]:
    if text.startswith(_ZLIB_PREFIX):
        b64_data = text[len(_ZLIB_PREFIX):]
        compressed = base64.b64decode(b64_data)
        raw = zlib.decompress(compressed)
        return json.loads(raw)
    return json.loads(text)
