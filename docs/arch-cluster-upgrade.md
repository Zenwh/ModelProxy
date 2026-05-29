# Feishu Relay v3 — 集群升级指南

> 适用版本：`feishu-relay-bot 3.0.1` + gateway commit ≥ `relay-v3.0.1` tag
> 目标：1000k 上下文、真流式、多 worker 平摊负载

---

## 1. v3 协议字段

`_relay_v=3`。`relay_codec` 编码层保持不变（仍是 JSON / `__zb64__` zlib base64
单条压缩），上层新增三种消息 `type` 和上行多片重组。

### 1.1 上行（gateway → worker）：`req_part`

```jsonc
{
  "_relay_v": 3,
  "type": "req_part",
  "req_id": "<24-hex>",
  "part_index": 0,                       // 0-based
  "part_total": 7,
  "endpoint": "chat" | "messages" | "responses",   // 仅 part_index=0
  "mode": "messages_native" | null,                // 仅 part_index=0
  "stream": true | false,                          // 仅 part_index=0
  "model": "...",                                  // 仅 part_index=0
  "payload_encoding": "plain" | "zb64",            // 仅 part_index=0
  "payload_chunk": "<part_index 对应字节切片>"
}
```

- `payload_encoding=zb64` 时 worker 拼接所有 `payload_chunk` 后先
  `base64.b64decode` 再 `zlib.decompress`，最后 `json.loads` 拿到完整请求 dict。
- `payload_encoding=plain` 时直接拼接 + `json.loads`。
- gateway 选择压缩策略：raw body ≥ 50KB 且节点 capabilities 含 `zlib` →
  zb64；否则 plain。

### 1.2 下行流式增量（worker → gateway）：`stream_chunk`

```jsonc
{
  "_relay_v": 3, "type": "stream_chunk",
  "req_id": "...", "node_id": "...",
  "seq": 5,                              // 0 起单调递增
  "mode": "chat" | "messages_native",
  "delta": {
    "text": "...",                       // chat / messages text 增量
    "tool_use": { "id", "name", "partial_json" },
    "thinking": "..."                    // reasoning models
  }
}
```

### 1.3 终结（worker → gateway）：`resp`

```jsonc
{
  "_relay_v": 3, "type": "resp",
  "req_id": "...", "node_id": "...",
  "ok": true,
  "seq_total": 12,
  "stop_reason": "end_turn",             // anthropic
  "finish_reason": "stop",               // openai
  "usage": { "input_tokens", "output_tokens" },
  // 失败时
  "status": 502, "error": "upstream_error", "message": "..."
}
```

### 1.4 capabilities

worker 心跳 `capabilities` 必含：

```
["zlib", "relay_v3", "multipart_in", "stream_out"]
```

gateway 启动 / 收心跳时校验 `relay_v3 in caps`；缺失则
`set_enabled(False)`，dashboard 告警。

---

## 2. 多 worker 部署步骤

### 2.1 前置

- gateway 上已配置 `MAX_INPUT_TOKENS=1_000_000`、`MULTIPART_CHUNK_BYTES≈120_000`、
  `MULTIPART_SEND_QPS=4.0`、`POLL_INTERVAL_S=0.3`、`STREAM_BUFFER_FLUSH_MS=1000`、
  `STREAM_BUFFER_FLUSH_BYTES=1024`（见 `outside_caller/config.py`）。
- gateway 已升级到 v3 代码，启动后 `journalctl -u llm-relay` 应看到
  `relay-v3` 字样。
- 飞书会话 `chat_id` 已建好；每个 worker 绑定该 chat（同一 chat 内多 worker
  通过 `node_id` 区分）。

### 2.2 单 worker 部署

1. 把 `feishu_relay_bot-3.0.1-py3-none-any.whl` scp 到目标主机。
2. `pip install --upgrade feishu_relay_bot-3.0.1-py3-none-any.whl`。
3. 复制 `config.example.yaml` → `config.yaml`，填实 `feishu.app_secret`、
   `mp.api_key`、`chat_id`、`node_id`。`node_id` 在同一 chat 内**必须唯一**。
4. `feishu-relay-bot run --config config.yaml`（或 systemd 拉起）。
5. 在 gateway 看 `[hb-v3]` 日志，确认该 `node_id` 出现在
   `/admin/nodes` 列表，`capabilities` 含 `relay_v3 / multipart_in / stream_out`，
   `enabled=true`。

### 2.3 多 worker 灰度

1. 起 1 个 worker 跑通 → 起 3 个 worker → 全量。
2. `pool.select()` 默认 round-robin；连续观察 1 小时
   `request_count` 应大致均衡（±15% 以内）。
3. 缩容只需停 worker 进程；gateway 心跳超时 90s 后该节点自动 disable。

### 2.4 capability 强校验

- 心跳缺 `relay_v3` → gateway 自动 `enabled=false` 并打 WARNING；
- dashboard `/admin/nodes` 该节点红色，运维需把对应主机的 wheel 升级到 3.0.1
  并重启。

---

## 3. 1000k 上下文行为

- gateway 在 `chat_completions` / `messages_endpoint` 入口对每条请求执行
  `token_estimator.estimate(messages, system, tools)`（char/4 简版）。
- `est > MAX_INPUT_TOKENS=1_000_000` → `truncator.truncate(...)`：
  - 保留 `system` + `tools`（base）。
  - 从尾向头累加 messages 直到达 budget；切点对齐到 `assistant` 结束 /
    `tool_use` 闭合。
  - 头部插入 `{role:"user", content:"[Earlier messages truncated to fit
    context window]"}` 标记。
  - 若 base 自身爆 budget，抛 `OpenAIError(invalid_request_error,
    status_code=413)`。
- 截断完成后整条请求序列化、（必要时）zlib 压缩、分片成多个 `req_part` 顺序
  发到**同一** worker 的 chat（绑定亲和）。

---

## 4. 真流式行为

- 客户端 `stream=true` → gateway 把 `stream` 字段透传到 `req_part`。
- worker `_call_mp_chat` / `_call_mp_messages_native` 检测 `stream==True` →
  用 `httpx.Client.stream("POST", ...)` + `iter_lines()` 解析 SSE event。
- 增量按 `mode` 喂给 `StreamEmitter`：
  - 累计文本 ≥ 1024 字节 / 距上次 flush ≥ 1000ms / tool_use 闭合 / stop
    任一触发 → 飞书发一条 `stream_chunk`。
  - 本地令牌桶限速 4 msg/s（容量 5）防飞书限流。
- 终结发一条 `resp(ok=true, seq_total, finish_reason)`。
- gateway `_stream_via_worker` 按 mode 翻译成 OpenAI 或 Anthropic SSE 推给客户端。
- 首 token 实测 0.75s（200 token 输出）。

---

## 5. 回滚步骤

1. 给问题 worker 装回 v2 wheel：
   `pip install feishu_relay_bot-2.x.y-py3-none-any.whl --force-reinstall`。
2. 重启该 worker 进程。
3. 该节点心跳上报 capabilities 缺 `relay_v3` → gateway 自动 disable，
   流量自动切其它 v3 节点。
4. 若所有 worker 都需要回滚，把 gateway 也回滚到 `relay-v2-stable` tag
   即可（v3 / v2 协议在编码层完全兼容，只 type 字段不同）。

---

## 6. 限流与监控

- 单 chat 飞书 `im/v1/messages send` 实测可承受 ≈ 5 msg/s；
  `MULTIPART_SEND_QPS=4.0` + StreamEmitter 4 msg/s 本地令牌桶兜底。
- 触发 429 / 超时 → gateway poll loop 退避到 1.5s；assembler 60-180s
  inflight 超时回调发 `multipart_timeout` resp。
- 监控指标（dashboard `/admin/nodes`）：
  - 每节点 `request_count` / `enabled` / `last_heartbeat`；
  - 每节点 `capabilities` 字段，确认含 `relay_v3`；
  - assembler `inflight_count`（worker 心跳带）应 ≈ 0 长期值，>5 持续 1min
    需排查 WS 丢包。

---

## 7. 已知约束

- token 估算用 char/4 简版，偏差 ±15%；超过 25% 偏差时升级 tiktoken。
- 飞书 WS frontier 在突发情况会丢消息；MULTIPART_SEND_QPS=4 + zb64
  预压缩把大请求压到 ≤ 2 part，绝大多数请求 1 part，丢包概率显著下降。
- 单 chat 多 worker 时，所有 worker 都会收到所有 `req_part` 广播；只有
  `node_id` 匹配 gateway 选中的目标节点才处理（gateway 端通过 `chat_id`
  亲和 + worker 端 `node_id` 校验双重保证）。
