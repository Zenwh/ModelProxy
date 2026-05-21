"""
Worker：收飞书消息 → 调 Model Proxy → 回飞书消息。

Bot 的核心：极薄的转发层。不管 key、不管路由、不管 token。
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Optional

import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from . import __version__
from .config import Config

logger = logging.getLogger("relay-bot")


class Worker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lark_client: Optional[lark.Client] = None
        self._ws_client = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._running = False

    def start(self):
        """启动 bot：连接飞书 WS，开始接收消息。"""
        self._running = True

        self._lark_client = lark.Client.builder() \
            .app_id(self.cfg.feishu_app_id) \
            .app_secret(self.cfg.feishu_app_secret) \
            .log_level(lark.LogLevel.INFO) \
            .build()

        handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(self._on_message) \
            .build()

        self._ws_client = lark.ws.Client(
            self.cfg.feishu_app_id,
            self.cfg.feishu_app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )

        # 启动心跳线程
        from .heartbeat import start_heartbeat
        self._heartbeat_thread = start_heartbeat(self)

        logger.info("连接飞书 WebSocket (node_id=%s) ...", self.cfg.node_id)
        self._ws_client.start()

    def reply_text(self, chat_id: str, text: str):
        """通过 lark SDK 回复消息（走 WS 通道）。"""
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()
            ).build()
        resp = self._lark_client.im.v1.message.create(req)
        if not resp.success():
            logger.error("回复失败: code=%s msg=%s", resp.code, resp.msg)

    def send_relay_response(self, chat_id: str, payload: dict):
        """发送 relay 协议响应。"""
        payload["_relay_v"] = 2
        payload["type"] = "resp"
        payload["node_id"] = self.cfg.node_id
        text = json.dumps(payload, ensure_ascii=False)
        self.reply_text(chat_id, text)

    def _on_message(self, data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        """飞书消息事件处理。"""
        event = data.event
        msg = event.message

        if msg.message_type != "text":
            return

        content = json.loads(msg.content)
        raw_text = content.get("text", "")
        chat_id = msg.chat_id

        try:
            parsed = json.loads(raw_text)
        except (ValueError, TypeError):
            return
        if not isinstance(parsed, dict):
            return

        relay_v = parsed.get("_relay_v")
        if relay_v not in (1, 2):
            return

        msg_type = parsed.get("type", "req")

        if msg_type == "ctrl":
            from .ctrl import handle_ctrl
            handle_ctrl(self, parsed)
            return

        if msg_type == "req" or parsed.get("req_id"):
            threading.Thread(
                target=self._handle_request,
                args=(parsed, chat_id),
                daemon=True,
            ).start()

    def _handle_request(self, req: dict, chat_id: str):
        """处理 AI 请求：调 MP，返回结果。"""
        req_id = req.get("req_id", "")
        model = req.get("model", "")
        endpoint = req.get("endpoint", "chat")

        logger.info("← req_id=%s model=%s endpoint=%s", req_id, model, endpoint)

        try:
            if endpoint == "messages":
                status, result = self._call_mp_messages_native(req)
            else:
                status, result = self._call_mp_chat(req)

            if status == 200:
                self.send_relay_response(chat_id, {
                    "req_id": req_id,
                    "ok": True,
                    **result,
                })
            else:
                err_msg = result.get("message") or result.get("error") or str(result)[:300]
                self.send_relay_response(chat_id, {
                    "req_id": req_id,
                    "ok": False,
                    "status": status if status >= 400 else 502,
                    "error": "upstream_error",
                    "message": err_msg,
                })
        except Exception as e:
            logger.exception("处理 req_id=%s 异常", req_id)
            self.send_relay_response(chat_id, {
                "req_id": req_id,
                "ok": False,
                "status": 500,
                "error": "bot_exception",
                "message": f"{type(e).__name__}: {e}",
            })

    def _mp_post(self, path: str, payload: dict) -> tuple[int, dict]:
        """调 Model Proxy。有 api_key 配置时带上 Authorization。"""
        headers = {"Content-Type": "application/json"}
        if self.cfg.mp_api_key:
            headers["Authorization"] = f"Bearer {self.cfg.mp_api_key}"
        with httpx.Client(timeout=240) as cli:
            r = cli.post(
                f"{self.cfg.mp_url}{path}",
                headers=headers,
                json=payload,
            )
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text[:500]}
        return r.status_code, data

    def _call_mp_chat(self, req: dict) -> tuple[int, dict]:
        """OpenAI chat/responses 模式调 MP。"""
        model = req.get("model", "")
        messages = req.get("messages", [])
        endpoint = req.get("endpoint", "chat")
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
        }
        if req.get("max_tokens"):
            payload["max_tokens"] = req["max_tokens"]
        if req.get("temperature") is not None:
            payload["temperature"] = req["temperature"]

        path = "/v1/responses" if endpoint == "responses" else "/v1/chat/completions"
        status, data = self._mp_post(path, payload)
        if status != 200:
            return status, data

        choices = data.get("choices") or []
        if not choices:
            return 502, {"error": "no_choices", "message": "empty response"}
        choice = choices[0]
        content = choice.get("message", {}).get("content", "")
        finish = choice.get("finish_reason", "stop")
        usage = data.get("usage") or {}
        return 200, {
            "content": content,
            "finish_reason": finish,
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }

    def _call_mp_messages_native(self, req: dict) -> tuple[int, dict]:
        """Anthropic messages 模式直通 MP。"""
        payload = {
            k: v for k, v in req.items()
            if k not in ("_relay_v", "req_id", "mode", "type", "node_id", "stream")
        }
        payload.pop("stream", None)

        status, mp_resp = self._mp_post("/v1/messages", payload)

        if status == 200 and "content" in mp_resp:
            return 200, {
                "mode": "messages_native",
                "raw_anthropic": mp_resp,
            }
        else:
            err_msg = (
                mp_resp.get("error", {}).get("message") if isinstance(mp_resp.get("error"), dict)
                else mp_resp.get("msg")
                or str(mp_resp)[:300]
            )
            return status if status >= 400 else 502, {
                "error": "upstream_error",
                "message": err_msg,
            }
