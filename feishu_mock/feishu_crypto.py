"""
飞书事件加密 / 签名工具。
- 签名算法：sha256(timestamp + nonce + encrypt_key + body)
- 加密算法：AES-256-CBC，key = sha256(encrypt_key)，IV = 密文前 16 字节
当 ENCRYPT_KEY 为空时跳过加密，body 即明文 JSON。
"""
from __future__ import annotations

import base64
import hashlib
import os
from typing import Tuple


def _pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def _unpad(data: bytes) -> bytes:
    return data[: -data[-1]]


def aes_encrypt(plaintext: str, key: str) -> str:
    """返回 base64(IV + AES-CBC(plaintext))。"""
    try:
        from Crypto.Cipher import AES  # type: ignore
    except ImportError as e:  # 仅在用户启用加密时才需要
        raise RuntimeError(
            "ENCRYPT_KEY 非空但缺少 pycryptodome，请 pip install pycryptodome"
        ) from e

    key_bytes = hashlib.sha256(key.encode("utf-8")).digest()
    iv = os.urandom(16)
    cipher = AES.new(key_bytes, AES.MODE_CBC, iv)
    enc = cipher.encrypt(_pad(plaintext.encode("utf-8")))
    return base64.b64encode(iv + enc).decode("utf-8")


def aes_decrypt(b64_ciphertext: str, key: str) -> str:
    try:
        from Crypto.Cipher import AES  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "缺少 pycryptodome，无法解密：pip install pycryptodome"
        ) from e

    raw = base64.b64decode(b64_ciphertext)
    iv, ct = raw[:16], raw[16:]
    key_bytes = hashlib.sha256(key.encode("utf-8")).digest()
    cipher = AES.new(key_bytes, AES.MODE_CBC, iv)
    return _unpad(cipher.decrypt(ct)).decode("utf-8")


def sign(timestamp: str, nonce: str, encrypt_key: str, body: bytes) -> str:
    """飞书签名：sha256(timestamp + nonce + encrypt_key + raw_body)。"""
    payload = (timestamp + nonce + encrypt_key).encode("utf-8") + body
    return hashlib.sha256(payload).hexdigest()


def build_signed_request(
    body_dict: dict, encrypt_key: str
) -> Tuple[bytes, dict]:
    """
    给定要发往被测服务的 webhook body（dict），按需加密并返回 (raw_body, headers)。
    - 如果 encrypt_key 为空：body 直接序列化为 JSON，不加密、不签名（headers 只带基础字段）。
    - 否则：用 AES 加密后包成 {"encrypt": "..."}，并计算 X-Lark-Signature。
    """
    import json
    import time
    import uuid

    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex

    if not encrypt_key:
        raw = json.dumps(body_dict, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "X-Lark-Request-Timestamp": timestamp,
            "X-Lark-Request-Nonce": nonce,
        }
        return raw, headers

    plaintext = json.dumps(body_dict, ensure_ascii=False)
    encrypted = aes_encrypt(plaintext, encrypt_key)
    wrapper = {"encrypt": encrypted}
    raw = json.dumps(wrapper, ensure_ascii=False).encode("utf-8")
    signature = sign(timestamp, nonce, encrypt_key, raw)
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "X-Lark-Request-Timestamp": timestamp,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": signature,
    }
    return raw, headers
