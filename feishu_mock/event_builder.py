"""
按飞书事件结构（event v2.0，im.message.receive_v1）构造事件 payload。
参考：
https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/events/receive
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Optional

from . import config


def _now_ms() -> str:
    return str(int(time.time() * 1000))


def build_receive_message_event(
    *,
    user_id: str,
    text: str,
    chat_id: Optional[str] = None,
    chat_type: str = "p2p",
    message_id: Optional[str] = None,
    open_id: Optional[str] = None,
    union_id: Optional[str] = None,
) -> dict:
    """
    构造一个 im.message.receive_v1 事件 payload（明文，未加密）。
    """
    chat_id = chat_id or f"oc_mock_{uuid.uuid4().hex[:12]}"
    message_id = message_id or f"om_mock_{uuid.uuid4().hex}"
    open_id = open_id or f"ou_mock_{uuid.uuid4().hex[:24]}"
    union_id = union_id or f"on_mock_{uuid.uuid4().hex[:24]}"

    content_json = json.dumps({"text": text}, ensure_ascii=False)

    return {
        "schema": "2.0",
        "header": {
            "event_id": uuid.uuid4().hex,
            "token": config.VERIFICATION_TOKEN,
            "create_time": _now_ms(),
            "event_type": "im.message.receive_v1",
            "tenant_key": config.TENANT_KEY,
            "app_id": config.APP_ID,
        },
        "event": {
            "sender": {
                "sender_id": {
                    "union_id": union_id,
                    "user_id": user_id,
                    "open_id": open_id,
                },
                "sender_type": "user",
                "tenant_key": config.TENANT_KEY,
            },
            "message": {
                "message_id": message_id,
                "root_id": "",
                "parent_id": "",
                "create_time": _now_ms(),
                "chat_id": chat_id,
                "chat_type": chat_type,
                "message_type": "text",
                "content": content_json,
                "mentions": [],
            },
        },
    }


def build_url_verification_event(challenge: str) -> dict:
    """飞书首次配置 webhook 时的握手包。可用于让用户测试被测服务的握手逻辑。"""
    return {
        "challenge": challenge,
        "token": config.VERIFICATION_TOKEN,
        "type": "url_verification",
    }
