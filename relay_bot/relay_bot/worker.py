"""
Worker：收飞书消息 → 调 Model Proxy → 回飞书消息。

v3 协议：上行多片重组（ChunkAssembler）+ 真流式（StreamEmitter）+ 终结 resp。
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
from .chunk_assembler import ChunkAssembler
from .config import Config
from .relay_codec import PayloadTooLargeError, decode as codec_decode, encode as codec_encode
from .stream_emitter import StreamEmitter

logger = logging.getLogger("relay-bot")


class Worker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lark_client: Optional[lark.Client] = None
        self._ws_client = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._running = False
        self.chat_id: Optional[str] = cfg.chat_id or None
        self._assembler = ChunkAssembler(
            timeout_s=cfg.multipart_timeout_s,
            on_timeout=self._on_multipart_timeout,
        )

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

        # 启动 assembler 超时清扫线程 + 心跳线程
        self._assembler.start()
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
        """发送 relay v3 响应（resp / stream_chunk）。"""
        payload["_relay_v"] = 3
        payload.setdefault("type", "resp")
        payload["node_id"] = self.cfg.node_id
        try:
            text = codec_encode(payload)
        except PayloadTooLargeError:
            payload.pop("raw_anthropic", None)
            if "content" in payload:
                payload["content"] = payload["content"][:8000] + "\n...[truncated]"
            payload["finish_reason"] = "length"
            text = codec_encode(payload, allow_compress=False)
        self.reply_text(chat_id, text)

    # ------------------------------------------------------------------
    # 飞书入口
    # ------------------------------------------------------------------

    def _on_message(self, data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        """飞书消息事件处理。仅识别 v3 req_part / ctrl。"""
        event = data.event
        msg = event.message

        if msg.message_type != "text":
            return

        content = json.loads(msg.content)
        raw_text = content.get("text", "")
        chat_id = msg.chat_id

        if not self.chat_id:
            self.chat_id = chat_id

        try:
            parsed = codec_decode(raw_text)
        except Exception:
            return
        if not isinstance(parsed, dict):
            return

        if parsed.get("_relay_v") != 3:
            # 旧版 v1/v2 已废弃；忽略
            return

        msg_type = parsed.get("type", "")

        if msg_type == "ctrl":
            from .ctrl import handle_ctrl
            handle_ctrl(self, parsed)
            return

        if msg_type == "req_part":
            self._on_req_part(parsed, chat_id)
            return

        # heartbeat / stream_chunk / resp 等是 worker 自己发出的回环，忽略
        return

    def _on_req_part(self, env: dict, chat_id: str) -> None:
        req_id = env.get("req_id") or ""
        if not req_id:
            return
        try:
            part_index = int(env.get("part_index"))
            part_total = int(env.get("part_total"))
        except (TypeError, ValueError):
            logger.warning("bad req_part envelope req_id=%s", req_id)
            return

        chunk = env.get("payload_chunk") or ""
        meta = {}
        if part_index == 0:
            for k in ("endpoint", "mode", "stream", "model", "payload_encoding"):
                if k in env:
                    meta[k] = env[k]

        full = self._assembler.add_part(
            req_id=req_id,
            part_index=part_index,
            part_total=part_total,
            payload_chunk=chunk,
            chat_id=chat_id,
            meta=meta,
        )
        if full is None:
            return

        # 重组完成 → 投到处理线程
        threading.Thread(
            target=self._handle_request,
            args=(full, chat_id),
            daemon=True,
        ).start()

    def _on_multipart_timeout(self, req_id: str, chat_id: str, info: dict) -> None:
        if not chat_id:
            return
        self.send_relay_response(chat_id, {
            "req_id": req_id,
            "type": "resp",
            "ok": False,
            "status": 408,
            "error": "multipart_timeout",
            "message": f"only {info.get('received')}/{info.get('expected')} parts received",
        })

    # ------------------------------------------------------------------
    # 业务处理
    # ------------------------------------------------------------------

    def _handle_request(self, req: dict, chat_id: str):
        """处理 AI 请求：调 MP，返回结果（流式或非流式）。"""
        req_id = req.get("req_id", "")
        model = req.get("model", "")
        endpoint = req.get("endpoint") or ("messages" if req.get("mode") == "messages_native" else "chat")
        stream = bool(req.get("stream", False))

        logger.info("← req_id=%s model=%s endpoint=%s stream=%s", req_id, model, endpoint, stream)

        try:
            if endpoint == "messages":
                self._dispatch_messages(req, chat_id, stream=stream)
            else:
                self._dispatch_chat(req, chat_id, stream=stream)
        except Exception as e:
            logger.exception("处理 req_id=%s 异常", req_id)
            self.send_relay_response(chat_id, {
                "req_id": req_id,
                "type": "resp",
                "ok": False,
                "status": 500,
                "error": "bot_exception",
                "message": f"{type(e).__name__}: {e}",
            })

    def _dispatch_chat(self, req: dict, chat_id: str, *, stream: bool):
        req_id = req.get("req_id", "")
        if stream:
            self._stream_chat(req, chat_id)
        else:
            status, result = self._call_mp_chat(req)
            self._finalize(chat_id, req_id, status, result)

    def _dispatch_messages(self, req: dict, chat_id: str, *, stream: bool):
        req_id = req.get("req_id", "")
        if stream:
            self._stream_messages_native(req, chat_id)
        else:
            status, result = self._call_mp_messages_native(req)
            self._finalize(chat_id, req_id, status, result)

    def _finalize(self, chat_id: str, req_id: str, status: int, result: dict):
        if status == 200:
            self.send_relay_response(chat_id, {
                "req_id": req_id,
                "type": "resp",
                "ok": True,
                **result,
            })
        else:
            err_msg = result.get("message") or result.get("error") or str(result)[:300]
            self.send_relay_response(chat_id, {
                "req_id": req_id,
                "type": "resp",
                "ok": False,
                "status": status if status >= 400 else 502,
                "error": "upstream_error",
                "message": err_msg,
            })

    # ------------------------------------------------------------------
    # MP 调用：非流式
    # ------------------------------------------------------------------

    def _mp_post(self, path: str, payload: dict) -> tuple[int, dict]:
        """非流式 MP 调用。"""
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

    def _build_chat_payload(self, req: dict, *, stream: bool) -> tuple[str, dict]:
        """OpenAI chat/responses 模式的 MP 路径 + payload。"""
        model = req.get("model", "")
        messages = req.get("messages", [])
        endpoint = req.get("endpoint", "chat")

        if endpoint == "responses":
            payload: dict[str, Any] = {
                "model": model,
                "input": messages,
                "stream": stream,
            }
            if req.get("max_tokens"):
                payload["max_tokens"] = req["max_tokens"]
            if req.get("temperature") is not None:
                payload["temperature"] = req["temperature"]
            return "/v1/responses", payload

        payload = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }
        if req.get("max_tokens"):
            payload["max_tokens"] = req["max_tokens"]
        if req.get("temperature") is not None:
            payload["temperature"] = req["temperature"]
        return "/v1/chat/completions", payload

    def _call_mp_chat(self, req: dict) -> tuple[int, dict]:
        """OpenAI chat/responses 非流式。"""
        path, payload = self._build_chat_payload(req, stream=False)
        status, data = self._mp_post(path, payload)
        if status != 200:
            return status, data

        if path.endswith("/responses"):
            output = data.get("output") or []
            content = ""
            for item in output:
                if item.get("type") == "message":
                    for c in item.get("content", []):
                        if c.get("type") == "output_text":
                            content += c.get("text", "")
            usage = data.get("usage") or {}
            return 200, {
                "content": content,
                "finish_reason": "stop",
                "usage": {
                    "prompt_tokens": usage.get("input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                },
            }

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
        """Anthropic messages 非流式直通 MP。"""
        payload = {
            k: v for k, v in req.items()
            if k not in ("_relay_v", "req_id", "mode", "type", "node_id", "stream", "endpoint")
        }
        payload["stream"] = False

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

    # ------------------------------------------------------------------
    # MP 调用：流式
    # ------------------------------------------------------------------

    def _stream_chat(self, req: dict, chat_id: str):
        """OpenAI chat/responses 流式：httpx.stream() + StreamEmitter。"""
        req_id = req.get("req_id", "")
        path, payload = self._build_chat_payload(req, stream=True)
        emitter = StreamEmitter(
            self, chat_id, req_id, mode="chat",
            flush_bytes=self.cfg.stream_flush_bytes,
            flush_ms=self.cfg.stream_flush_ms,
            send_qps=self.cfg.stream_send_qps,
        )

        finish_reason = "stop"
        prompt_tokens = completion_tokens = 0
        err_status: Optional[int] = None
        err_msg: Optional[str] = None

        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.cfg.mp_api_key:
            headers["Authorization"] = f"Bearer {self.cfg.mp_api_key}"

        try:
            with httpx.Client(timeout=httpx.Timeout(240, connect=15)) as cli:
                with cli.stream("POST", f"{self.cfg.mp_url}{path}", headers=headers, json=payload) as r:
                    if r.status_code != 200:
                        err_status = r.status_code
                        err_msg = r.read().decode("utf-8", errors="ignore")[:300]
                    else:
                        for raw_line in r.iter_lines():
                            if not raw_line:
                                continue
                            line = raw_line if isinstance(raw_line, str) else raw_line.decode("utf-8", errors="ignore")
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                break
                            try:
                                obj = json.loads(data)
                            except Exception:
                                continue
                            # OpenAI chat: {choices:[{delta:{content:"..."}, finish_reason:"..."}]}
                            choices = obj.get("choices") or []
                            if choices:
                                ch = choices[0]
                                delta = ch.get("delta") or {}
                                text = delta.get("content")
                                if text:
                                    emitter.feed_text(text)
                                # tool_calls 增量
                                tcs = delta.get("tool_calls")
                                if tcs:
                                    for tc in tcs:
                                        fn = tc.get("function") or {}
                                        emitter.feed_tool_use(
                                            tc.get("id", ""),
                                            fn.get("name", ""),
                                            fn.get("arguments", ""),
                                        )
                                if ch.get("finish_reason"):
                                    finish_reason = ch.get("finish_reason")
                            # OpenAI responses: {type:"response.output_text.delta", delta:"..."}
                            otype = obj.get("type")
                            if otype == "response.output_text.delta":
                                emitter.feed_text(obj.get("delta", "") or "")
                            elif otype == "response.completed":
                                usage = (obj.get("response") or {}).get("usage") or {}
                                prompt_tokens = usage.get("input_tokens", prompt_tokens)
                                completion_tokens = usage.get("output_tokens", completion_tokens)
                            usage = obj.get("usage")
                            if isinstance(usage, dict):
                                prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                                completion_tokens = usage.get("completion_tokens", completion_tokens)
        except Exception as e:
            logger.exception("stream chat failed req_id=%s", req_id)
            err_msg = f"{type(e).__name__}: {e}"
            err_status = err_status or 502

        emitter.close()

        if err_status:
            self.send_relay_response(chat_id, {
                "req_id": req_id,
                "type": "resp",
                "ok": False,
                "status": err_status,
                "error": "upstream_error",
                "message": err_msg or "stream failed",
                "seq_total": emitter.seq_total,
            })
        else:
            self.send_relay_response(chat_id, {
                "req_id": req_id,
                "type": "resp",
                "ok": True,
                "finish_reason": finish_reason,
                "seq_total": emitter.seq_total,
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            })

    def _stream_messages_native(self, req: dict, chat_id: str):
        """Anthropic messages 流式：把上游 SSE 事件原样攒到 emitter（events 数组）。"""
        req_id = req.get("req_id", "")
        emitter = StreamEmitter(
            self, chat_id, req_id, mode="messages_native",
            flush_bytes=self.cfg.stream_flush_bytes,
            flush_ms=self.cfg.stream_flush_ms,
            send_qps=self.cfg.stream_send_qps,
        )

        payload = {
            k: v for k, v in req.items()
            if k not in ("_relay_v", "req_id", "mode", "type", "node_id", "stream", "endpoint")
        }
        payload["stream"] = True

        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.cfg.mp_api_key:
            headers["Authorization"] = f"Bearer {self.cfg.mp_api_key}"

        stop_reason = "end_turn"
        input_tokens = output_tokens = 0
        err_status: Optional[int] = None
        err_msg: Optional[str] = None

        try:
            with httpx.Client(timeout=httpx.Timeout(240, connect=15)) as cli:
                with cli.stream("POST", f"{self.cfg.mp_url}/v1/messages", headers=headers, json=payload) as r:
                    if r.status_code != 200:
                        err_status = r.status_code
                        err_msg = r.read().decode("utf-8", errors="ignore")[:300]
                    else:
                        cur_event: Optional[str] = None
                        for raw_line in r.iter_lines():
                            line = raw_line if isinstance(raw_line, str) else (raw_line or b"").decode("utf-8", errors="ignore")
                            if not line:
                                cur_event = None
                                continue
                            if line.startswith("event:"):
                                cur_event = line[6:].strip()
                                continue
                            if line.startswith("data:"):
                                data_str = line[5:].strip()
                                try:
                                    obj = json.loads(data_str)
                                except Exception:
                                    continue
                                et = cur_event or obj.get("type") or ""
                                if not et:
                                    continue
                                # 收集 usage
                                if et == "message_start":
                                    msg_obj = obj.get("message") or {}
                                    u = msg_obj.get("usage") or {}
                                    input_tokens = u.get("input_tokens", input_tokens)
                                elif et == "message_delta":
                                    delta = obj.get("delta") or {}
                                    if delta.get("stop_reason"):
                                        stop_reason = delta["stop_reason"]
                                    u = obj.get("usage") or {}
                                    output_tokens = u.get("output_tokens", output_tokens)
                                elif et == "message_stop":
                                    # gateway 那侧会自己补 message_stop，这里也照样转发
                                    pass
                                emitter.feed_event(et, obj)
        except Exception as e:
            logger.exception("stream messages failed req_id=%s", req_id)
            err_msg = f"{type(e).__name__}: {e}"
            err_status = err_status or 502

        emitter.close()

        if err_status:
            self.send_relay_response(chat_id, {
                "req_id": req_id,
                "type": "resp",
                "ok": False,
                "status": err_status,
                "error": "upstream_error",
                "message": err_msg or "stream failed",
                "seq_total": emitter.seq_total,
            })
        else:
            self.send_relay_response(chat_id, {
                "req_id": req_id,
                "type": "resp",
                "ok": True,
                "stop_reason": stop_reason,
                "seq_total": emitter.seq_total,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
                "mode": "messages_native",
            })
