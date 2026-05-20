"""
以你本人的身份给 bot 发消息，然后轮询拉 bot 的回复。

用法：
  python -m outside_caller.talk "你要问 bot 的话"
  python -m outside_caller.talk --bot ou_xxx "..."   # 指定 bot open_id

第一次跑前先：
  python -m outside_caller.oauth_once
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional

import httpx

from . import config


def _load_tokens() -> dict:
    if not os.path.exists(config.TOKEN_FILE):
        raise SystemExit(
            f"找不到 {config.TOKEN_FILE}，请先运行：python -m outside_caller.oauth_once"
        )
    with open(config.TOKEN_FILE) as f:
        return json.load(f)


def _auth_header(tokens: dict) -> dict:
    return {
        "Authorization": f"Bearer {tokens['user_access_token']}",
        "Content-Type": "application/json",
    }


def find_bot_open_id(tokens: dict) -> str:
    """枚举当前用户的会话，找到包含目标 app_id 的 P2P 会话对端。"""
    if config.TARGET_BOT_OPEN_ID:
        return config.TARGET_BOT_OPEN_ID

    url = f"{config.FEISHU_BASE}/open-apis/im/v1/chats"
    page = ""
    while True:
        params = {"page_size": 50}
        if page:
            params["page_token"] = page
        r = httpx.get(url, params=params, headers=_auth_header(tokens), timeout=15)
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"列 chats 失败: {d}")
        for c in (d.get("data") or {}).get("items", []) or []:
            # P2P 会话里 owner_id 是 app_id（bot 的）
            if c.get("chat_mode") == "p2p" and c.get("owner_id") == config.APP_ID:
                # 拿到 chat_id 后查成员，找到不是自己的那个 open_id
                cid = c["chat_id"]
                return _peer_open_id_from_chat(tokens, cid)
        page = (d.get("data") or {}).get("page_token", "")
        if not page:
            break
    raise SystemExit(
        f"没找到 app_id={config.APP_ID} 的 P2P 会话。\n"
        "请先在飞书客户端给这个 bot 发一条消息建立会话，再重试。"
    )


def _peer_open_id_from_chat(tokens: dict, chat_id: str) -> str:
    """P2P chat 里取对端 open_id（这里就是 bot 的 open_id）。"""
    r = httpx.get(
        f"{config.FEISHU_BASE}/open-apis/im/v1/chats/{chat_id}/members",
        params={"member_id_type": "open_id"},
        headers=_auth_header(tokens),
        timeout=15,
    )
    d = r.json()
    me = tokens["open_id"]
    for m in (d.get("data") or {}).get("items", []) or []:
        if m.get("member_id") != me:
            return m["member_id"]
    raise RuntimeError(f"chat {chat_id} 里找不到对端")


def send_to_bot(tokens: dict, bot_open_id: str, text: str) -> dict:
    """以当前用户身份给 bot 发文本消息。"""
    r = httpx.post(
        f"{config.FEISHU_BASE}/open-apis/im/v1/messages",
        params={"receive_id_type": "open_id"},
        headers=_auth_header(tokens),
        json={
            "receive_id": bot_open_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
        timeout=15,
    )
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"发消息失败: {d}")
    return d["data"]


def get_p2p_chat_id(tokens: dict, bot_open_id: str) -> str:
    """根据 bot open_id 拿到与之的 P2P chat_id。"""
    url = f"{config.FEISHU_BASE}/open-apis/im/v1/chats"
    page = ""
    while True:
        params = {"page_size": 50}
        if page:
            params["page_token"] = page
        r = httpx.get(url, params=params, headers=_auth_header(tokens), timeout=15)
        d = r.json()
        for c in (d.get("data") or {}).get("items", []) or []:
            if c.get("chat_mode") != "p2p":
                continue
            cid = c["chat_id"]
            try:
                peer = _peer_open_id_from_chat(tokens, cid)
            except Exception:
                continue
            if peer == bot_open_id:
                return cid
        page = (d.get("data") or {}).get("page_token", "")
        if not page:
            break
    raise RuntimeError("找不到对应的 P2P chat_id")


def poll_reply(
    tokens: dict,
    chat_id: str,
    after_ms: int,
    bot_open_id: str,
    timeout_s: int = 60,
) -> Optional[dict]:
    """轮询会话历史，拿到 bot 在 after_ms 之后发的第一条消息。"""
    deadline = time.time() + timeout_s
    seen_ids: set[str] = set()
    while time.time() < deadline:
        r = httpx.get(
            f"{config.FEISHU_BASE}/open-apis/im/v1/messages",
            params={
                "container_id_type": "chat",
                "container_id": chat_id,
                "sort_type": "ByCreateTimeDesc",
                "page_size": 20,
            },
            headers=_auth_header(tokens),
            timeout=15,
        )
        d = r.json()
        if d.get("code") != 0:
            print(f"  poll 错误: {d}", file=sys.stderr)
            time.sleep(1.0)
            continue
        items = (d.get("data") or {}).get("items", []) or []
        for m in items:
            mid = m.get("message_id")
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            ct_ms = int(m.get("create_time", "0"))
            if ct_ms <= after_ms:
                continue
            sender_id = (m.get("sender") or {}).get("id", "")
            sender_type = (m.get("sender") or {}).get("sender_type", "")
            # bot 回复时 sender_type 是 "app" 或 id 是 bot 的 open_id
            if sender_type == "app" or sender_id == bot_open_id:
                return m
        time.sleep(1.0)
    return None


def render_message(msg: dict) -> str:
    body = (msg.get("body") or {}).get("content") or ""
    msg_type = msg.get("msg_type")
    try:
        c = json.loads(body)
    except Exception:
        return f"[{msg_type}] {body}"
    if msg_type == "text":
        return c.get("text", "")
    return f"[{msg_type}] {json.dumps(c, ensure_ascii=False)}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", help="要发给 bot 的话")
    ap.add_argument("--bot", default="", help="bot 的 open_id（不传则自动找）")
    ap.add_argument("--timeout", type=int, default=60, help="等回复的秒数")
    args = ap.parse_args()

    tokens = _load_tokens()
    print(f"我是: {tokens.get('name')!r} (open_id={tokens['open_id']})")

    bot_open_id = args.bot or find_bot_open_id(tokens)
    print(f"目标 bot open_id: {bot_open_id}")

    chat_id = get_p2p_chat_id(tokens, bot_open_id)
    print(f"P2P chat_id: {chat_id}")

    before_ms = int(time.time() * 1000)
    sent = send_to_bot(tokens, bot_open_id, args.text)
    print(f"→ 已发: message_id={sent.get('message_id')}")

    print(f"等 bot 回复（最多 {args.timeout}s）...")
    reply = poll_reply(tokens, chat_id, before_ms, bot_open_id, args.timeout)
    if reply is None:
        print("⏱  超时，没收到 bot 回复")
        sys.exit(2)

    print("\n=== bot 回复 ===")
    print(render_message(reply))


if __name__ == "__main__":
    main()
