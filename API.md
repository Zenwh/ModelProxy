# ModelProxy API 文档

统一的大模型 API 中转服务，聚合 Claude、GPT、Gemini、Doubao、DeepSeek、Kimi、GLM 等 400+ 模型。

## 核心接口对照表

平台围绕三套兼容协议组织接口，应用按场景挑一个即可：

| 接口类型（`support_apis.id`） | 端点 | 协议 | 典型场景 |
|---|---|---|---|
| `claude_native` | `POST /v1/messages` | Anthropic Messages | Claude Code、Claude App、Anthropic SDK |
| `chat` | `POST /v1/chat/completions` | OpenAI Chat Completions | 通用聊天 / 代码助手 / 多家 SDK 互通 |
| `responses` | `POST /v1/responses` | OpenAI Responses | Codex CLI、GPT-5 系列、需要 reasoning trace 的场景 |

> 实测可用性见 [§4 三大核心接口](#4-三大核心接口) 末尾的"实测矩阵"。**生产前请用自己的 Key 重测目标模型**——本文档使用测试 Key `ak-xqmsbezufm409fkaxruv35njq4vlnvtq` 实测，部分模型在该 Key 下受配额限制。

文档标注约定：
- ✅ 已实测通过（含真实请求/响应样例）
- ⚠️ 协议确认，但测试 Key 无额度/权限/通道
- ❌ 测试 Key 无权限或调用失败（仅作协议参考）

---

## 目录

- [1. 基础信息](#1-基础信息)
- [2. 鉴权](#2-鉴权)
- [3. 通用约定](#3-通用约定)
- [4. 三大核心接口](#4-三大核心接口)
  - [4.1 `POST /v1/messages` —— Anthropic Messages（`claude_native`）](#41-post-v1messages--anthropic-messagesclaude_native)
  - [4.2 `POST /v1/chat/completions` —— OpenAI Chat Completions（`chat`）](#42-post-v1chatcompletions--openai-chat-completionschat)
  - [4.3 `POST /v1/responses` —— OpenAI Responses（`responses`）](#43-post-v1responses--openai-responsesresponses)
  - [4.4 实测矩阵](#44-实测矩阵)
- [5. 辅助接口](#5-辅助接口)
- [6. SDK 接入](#6-sdk-接入)
- [7. FAQ](#7-faq)

---

## 1. 基础信息

| 项 | 值 |
|---|---|
| Base URL | `https://stepcode.basemind.com` |
| 鉴权方式 | Bearer Token（OpenAI 风格）/ `x-api-key`（Anthropic 风格） |
| 数据格式 | JSON（音频/图像/视频类响应除外） |
| 协议兼容 | OpenAI、Anthropic Messages、Google Gemini 原生 |

---

## 2. 鉴权

**OpenAI / Responses 接口**

```
Authorization: Bearer <API_KEY>
```

**Anthropic Messages 接口**（同样接受 Bearer）

```
x-api-key: <API_KEY>
anthropic-version: 2023-06-01
```

> 一个 Key 不建议多人共用。不同业务请申请独立 Key 以便配额隔离。

---

## 3. 通用约定

### 3.1 超时建议

- 普通模型：≥ 5 分钟，推荐 10 分钟。
- Thinking / Reasoning 模型（`-thinking`、`-reasoner`、`o3-pro`、`gpt-5-pro` 等）：推荐 20–30 分钟。
- 异步任务（视频生成等）：发起后通过查询接口轮询。

### 3.2 错误响应

非 2xx 时统一为：

```json
{
  "code": 503,
  "msg": "model xxx is currently offline [trace_id=...]",
  "data": null
}
```

实测常见错误：

| code | 实测样例 | 处理 |
|---|---|---|
| 400 | `invalid model name`、`parameter.enable_thinking must be set to false for non-streaming calls` | 参数 / 模型名错误，按 `msg` 修复 |
| 401 | — | 检查 API Key |
| 424 | `error, status code: 403, message: User has been banned` | 当前 Key 对该模型无权限；换 Key |
| 424 | `model config unresolvable from MODELS_MAP` | 模型名平台未路由，先 `/v1/models` 确认 |
| 424 | `error, status code: 401, message: invalid_iam_token` | 上游账号 token 异常 |
| 429 | `reaching limitation of api_key:[max_limit:4]` | 降并发 |
| 429 | `当前分组上游负载已饱和` | 资源不足，重试 / 反馈扩容 |
| 503 | `model xxx is currently offline` | 模型通道下线 |
| 503 | `No available channel for model xxx under group default` | 暂无可用通道 |

排障时请保留 `msg` 末尾的 `trace_id`。

### 3.3 请求 ID

请求头 `X-Request-ID` 可自定义；不传则服务自动生成。响应头：

- `X-Request-ID`：与请求一致 / 自动生成的 trace
- `X-Model-ID`：实际下游模型（仅 chat、messages、responses 返回）

### 3.4 模型后缀约定

| 后缀 | 含义 | 示例 |
|---|---|---|
| 无 | 默认 chat 接口 | `gpt-4o`、`gemini-2.5-pro` |
| `-img-gen` | 文生图 | `doubao-img-gen` |
| `-video-gen` | 视频生成 | `kling-v1.6-video-gen` |
| `-tts` | 文本转语音 | `gpt-tts-hd` |
| `-asr` | 语音转文本 | `doubao-asr` |
| `-thinking` / `-think` / `-reasoner` | 思考 / 推理 | `claude-sonnet-4-5-20250929-thinking` |
| `-realtime` | Realtime 语音对话 | `gpt-realtime` |
| `-vision` | 视觉理解专用 | `gemini-2.5-pro-vision-provider` |
| `-native` | 模型原生协议 | `claude-opus-4-7` |
| `-backup` / `:palm-aws` / `:ksyun-azure` | 通道分流 | `claude-sonnet-4-6:palm-aws` |
| `-discard` | 即将下线 | — |

---

## 4. 三大核心接口

### 4.1 `POST /v1/messages` —— Anthropic Messages（`claude_native`）

✅ **实测可用**。Claude App、Claude Code、Anthropic SDK 等工具直接对接。

#### 鉴权

```
x-api-key: $API_KEY
anthropic-version: 2023-06-01
Content-Type: application/json
```

#### 最小示例

```bash
curl https://stepcode.basemind.com/v1/messages \
  -H "x-api-key: $API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-6",
    "max_tokens": 50,
    "messages": [{"role": "user", "content": "hi"}]
  }'
```

实测响应：

```json
{
  "id": "msg_bdrk_017koFb7QBCJkNgzETmM1dWY",
  "type": "message",
  "role": "assistant",
  "model": "mcs-5",
  "content": [{"type": "text", "text": "Hi there! How are you doing? 😊"}],
  "stop_reason": "end_turn",
  "stop_sequence": "",
  "usage": {
    "input_tokens": 8,
    "output_tokens": 24,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
    "cache_creation": {
      "ephemeral_1h_input_tokens": 0,
      "ephemeral_5m_input_tokens": 0
    }
  }
}
```

> 注意：响应中的 `model` 会是下游实际路由名（如 `mcs-5`），与请求的 `model` 不一定一致。这是平台路由行为，按 Anthropic 标准字段使用即可。

#### 系统提示 + 工具调用 ✅

```bash
curl https://stepcode.basemind.com/v1/messages \
  -H "x-api-key: $API_KEY" -H "anthropic-version: 2023-06-01" -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-6",
    "max_tokens": 120,
    "system": "You are a weather assistant.",
    "messages": [{"role": "user", "content": "What is the weather in Shanghai?"}],
    "tools": [{
      "name": "get_weather",
      "description": "Get weather",
      "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"]
      }
    }]
  }'
```

实测响应（工具调用）：

```json
{
  "content": [{
    "id": "toolu_bdrk_01UJir6K1np1c7egDFLq414u",
    "input": {"city": "Shanghai"},
    "name": "get_weather",
    "type": "tool_use"
  }],
  "id": "msg_bdrk_01DkY72E9gYWggjuGLxJvVap",
  "model": "mcs-5",
  "role": "assistant",
  "stop_reason": "tool_use",
  "type": "message",
  "usage": { "input_tokens": 568, "output_tokens": 54, ... }
}
```

#### 流式（SSE，Anthropic 标准事件） ✅

加 `"stream": true`，响应是标准 Anthropic 事件流：

```
event: message_start
data: {"type":"message_start","message":{...,"usage":{...}}}

event: content_block_start
data: {"type":"content_block_start","content_block":{"text":"","type":"text"},"index":0}

event: content_block_delta
data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"1, "},"index":0}

...

event: content_block_stop
event: message_delta
event: message_stop
```

#### Prompt Caching

支持 `cache_control: {"type": "ephemeral"}`，响应 `usage` 中会回报 `cache_creation_input_tokens` / `cache_read_input_tokens`。

#### 实测通过模型

`claude-sonnet-4-6`、`claude-opus-4-7`、`claude-sonnet-4-5-20250929`、`claude-haiku-4-5-20251001`、`kimi-k2-thinking`、`kimi-k2.6`、`deepseek-v4-pro`、`mimo-v2-pro`

#### 计数 token：`POST /v1/messages/count_tokens` ⚠️

Body 同 `/v1/messages`，返回 `{"input_tokens": <n>}`。**测试 Key 下所有 Claude 模型返回 403 banned，未能实测通过**——协议形态依旧可参考。

---

### 4.2 `POST /v1/chat/completions` —— OpenAI Chat Completions（`chat`）

✅ **实测可用**。覆盖最广，适用于通用聊天、代码助手、多家 SDK 互通。

#### 鉴权

```
Authorization: Bearer $API_KEY
Content-Type: application/json
```

#### 最小示例（非流式）

```bash
curl https://stepcode.basemind.com/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-6",
    "messages": [{"role": "user", "content": "hi"}],
    "max_tokens": 50
  }'
```

实测响应：

```json
{
  "id": "msg_bdrk_01Dt9eNi4dK4Ubaui56F5kY8",
  "object": "chat.completion",
  "created": 1778492585,
  "model": "claude-sonnet-4-6",
  "choices": [{
    "index": 0,
    "message": {"role": "assistant", "content": "Hi there! How are you doing?"},
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 8,
    "completion_tokens": 20,
    "total_tokens": 28,
    "completion_tokens_details": {"reasoning_tokens": 0},
    "prompt_tokens_details": {"cached_tokens": 0}
  }
}
```

#### 系统提示 + 多轮 ✅

```bash
curl https://stepcode.basemind.com/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o",
    "max_tokens": 40,
    "messages": [
      {"role": "system", "content": "You always reply in lowercase."},
      {"role": "user", "content": "Say HELLO"},
      {"role": "assistant", "content": "hello"},
      {"role": "user", "content": "now say GOODBYE"}
    ]
  }'
# -> {"choices":[{"message":{"role":"assistant","content":"goodbye"},"finish_reason":"stop"}], ...}
```

#### 工具调用 ✅

```bash
curl https://stepcode.basemind.com/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o",
    "max_tokens": 120,
    "messages": [{"role": "user", "content": "What is the weather in Shanghai?"}],
    "tools": [{
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "Get weather",
        "parameters": {
          "type": "object",
          "properties": {"city": {"type": "string"}},
          "required": ["city"]
        }
      }
    }]
  }'
```

实测响应：

```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "",
      "tool_calls": [{
        "id": "call_WoZAOVjO5ENj8UHyfqlIFIXb",
        "type": "function",
        "function": {"name": "get_weather", "arguments": "{\"city\":\"Shanghai\"}"}
      }]
    },
    "finish_reason": "tool_calls"
  }]
}
```

Claude 系列模型通过 chat 接口同样支持 `tools`，响应除 `tool_calls` 外还可能带说明文本：

```json
{
  "message": {
    "role": "assistant",
    "content": "I'll check the weather in Shanghai for you right away!",
    "tool_calls": [{"id": "toolu_bdrk_...", "type": "function", "function": {...}}]
  }
}
```

#### 流式（SSE） ✅

`"stream": true`：

```
data: {"id":"...","choices":[{"index":0,"delta":{"role":"assistant"}}], "usage":{...}}
data: {"id":"...","choices":[{"index":0,"delta":{"content":"1, "}}], "usage":{...}}
data: {"id":"...","choices":[{"index":0,"delta":{},"finish_reason":"stop"}], "usage":{"prompt_tokens":11,"completion_tokens":12,"total_tokens":23}}
data: [DONE]
```

#### 多模态（图像理解）

```json
{
  "model": "gpt-4o",
  "max_tokens": 1000,
  "messages": [{
    "role": "user",
    "content": [
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0K..."}},
      {"type": "text", "text": "图片中写的是什么？"}
    ]
  }]
}
```

> ⚠️ 实测：传入的图片必须是有效图片数据，损坏的 base64 会返回 `400 The image data you provided does not represent a valid image`。

Python 工具：

```python
import base64
from mimetypes import guess_type

def image_to_data_url(path: str) -> str:
    mime, _ = guess_type(path)
    mime = mime or "application/octet-stream"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return f"data:{mime};base64,{b64}"
```

#### 模型差异提示（实测）

- `o1-mini` 不支持 `max_tokens`、`system`、`temperature`。
- **`qwen3-*b`（≤32b）必须用 `stream: true`**——非流式会报 `parameter.enable_thinking must be set to false for non-streaming calls`。
- `qwq-*` 系列同样仅支持流式。
- `qwen2.5-math-72b-instruct` 的 `max_tokens` 上限 3072；`glm-4v-plus` 上限 1024。
- **Claude 4.5+**：`temperature` 与 `top_p` 不可同时设置，优先 `temperature`。
- **Gemini 系列**：实测 `gemini-2.5-flash` 返回 `invalid character '<' looking for beginning of value`（上游解析异常），`gemini-2.5-pro` 正常；具体每个 Gemini 模型建议先单测确认。

#### 实测通过模型

`claude-sonnet-4-6`、`claude-sonnet-4-5-20250929`、`gpt-4o`、`gpt-4.1`、`gpt-5`、`gpt-5.1`、`gpt-5.1-chat-latest`、`gemini-2.5-pro`、`kimi-k2-0905-preview`、`kimi-k2-thinking`、`kimi-k2.6`、`doubao-1.5-pro-32k`、`doubao-seed-1.6`、`deepseek-r1`、`glm-5`、`qwen3-235b-a22b-instruct-2507`、`qwen3-32b`（流式）

---

### 4.3 `POST /v1/responses` —— OpenAI Responses（`responses`）

✅ **实测可用**。Codex CLI、GPT-5 系列、需要 reasoning trace 的工具走这里。

#### 鉴权

```
Authorization: Bearer $API_KEY
Content-Type: application/json
```

#### 最小示例

```bash
curl https://stepcode.basemind.com/v1/responses \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-5.1", "input": "reply with single word: ok"}'
```

实测响应（节选关键字段）：

```json
{
  "id": "resp_07de79bc85778a45006a01b3af51bc8195a0e71b2cd06c501b",
  "object": "response",
  "status": "completed",
  "model": "gpt-5.1-global",
  "instructions": "You are a helpful coding assistant.",
  "output": [{
    "id": "msg_07de79bc85778a45006a01b3b004dc81959e6fee67f9ad0cc8",
    "type": "message",
    "status": "completed",
    "role": "assistant",
    "content": [{
      "type": "output_text",
      "annotations": [],
      "logprobs": [],
      "text": "ok"
    }]
  }],
  "reasoning": {"effort": "none", "summary": null},
  "text": {"format": {"type": "text"}, "verbosity": "medium"},
  "tools": [],
  "usage": {
    "input_tokens": 23,
    "output_tokens": 11,
    "total_tokens": 34,
    "input_tokens_details": {"cached_tokens": 0},
    "output_tokens_details": {"reasoning_tokens": 0}
  }
}
```

> 提取文本：`response.output[0].content[0].text`。
> ⚠️ 实测：未显式传 `instructions` 时，平台会注入默认值 `"You are a helpful coding assistant."`。需要无 system 行为的场景建议显式传入空字符串或自定义。

#### 自定义 instructions

```bash
curl https://stepcode.basemind.com/v1/responses \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.1",
    "instructions": "You always reply in lowercase.",
    "input": "Say HELLO"
  }'
```

#### 工具调用 ✅

```bash
curl https://stepcode.basemind.com/v1/responses \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.1",
    "input": "What is the weather in Shanghai?",
    "tools": [{
      "type": "function",
      "name": "get_weather",
      "description": "Get weather",
      "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"]
      }
    }]
  }'
```

工具调用在 `output[]` 中以独立 item 出现（`type: function_call`），并按 OpenAI 规范返回 `call_id`、`name`、`arguments`。

#### 流式（SSE） ✅

`"stream": true`，事件遵循 OpenAI Responses 标准：

```
event: response.created
data: {"type":"response.created","response":{"id":"resp_xxx","status":"in_progress",...}}

event: response.in_progress
event: response.output_item.added
event: response.content_part.added
event: response.output_text.delta
data: {"type":"response.output_text.delta","delta":"ok","item_id":"msg_xxx",...}
...
event: response.completed
```

> 与早期文档"不支持流式"的说法不一致，**实测当前是支持的**。

#### 适用模型 / 注意事项

- **GPT-5 系列**：`gpt-5`、`gpt-5-mini`、`gpt-5-nano`、`gpt-5.1`、`gpt-5.1-codex`、`gpt-5.1-codex-mini`、`gpt-5.1-codex-max`、`gpt-5.2`、`gpt-5.2-chat-latest`、`gpt-5-codex`、`gpt-5-pro`、`o3-pro`、`o3-deep-research`、`o4-mini-deep-research`。
- **`o3-pro` / `gpt-5-pro` 等深度推理模型耗时长**（实测 `o3-pro` 90 秒未返回），建议 ≥ 20 分钟超时。
- **deep-research 模型**必须传工具，且工具只能是 `web_search_preview` 或 `web_search_preview_2025_03_11`；`search_context_size` 不支持 `low`；`input` 内的 type 字段（`message`、`function_call`、`reasoning`、`web_search_call`、`code_interpreter_call`、`mcp_call` 等）请按 OpenAI 规范完整传入。

#### 实测通过模型

`gpt-5`、`gpt-5.1`、`gpt-5.1-codex`、`gpt-5.1-codex-mini`、`gpt-5.1-codex-max`、`gpt-5.2`、`gpt-5.2-chat-latest`、`gpt-5-codex`、`gpt-5-pro`

#### 查询接口：`GET /v1/responses/{response_id}` ⚠️

未实测。注意 `model` 必须通过 query 或 `X-Model` Header 传入：

```bash
curl "https://stepcode.basemind.com/v1/responses/resp_xxx?model=gpt-5.1" \
  -H "Authorization: Bearer $API_KEY"
```

---

### 4.4 实测矩阵

> Key：`ak-xqmsbezufm409fkaxruv35njq4vlnvtq`，时间：2026-05-11。**实际可用性以你的 Key 权限为准。**

#### chat（`POST /v1/chat/completions`）

| 模型 | 状态 | 备注 |
|---|---|---|
| `claude-sonnet-4-6` | ✅ | — |
| `claude-sonnet-4-5-20250929` | ✅ | — |
| `gpt-4o` | ✅ | — |
| `gpt-4.1` | ✅ | — |
| `gpt-5` | ✅ | — |
| `gpt-5.1` | ✅ | — |
| `gpt-5.1-chat-latest` | ✅ | — |
| `gemini-2.5-pro` | ✅ | — |
| `gemini-2.5-flash` | ❌ | `invalid character '<' looking for beginning of value`（上游异常） |
| `kimi-k2-0905-preview` | ✅ | — |
| `kimi-k2-thinking` | ✅ | — |
| `kimi-k2.6` | ✅ | — |
| `doubao-1.5-pro-32k` | ✅ | — |
| `doubao-seed-1.6` | ✅ | — |
| `deepseek-r1` | ✅ | — |
| `deepseek-v3` / `deepseek-v3.1` | ❌ | `invalid_iam_token`（上游凭据问题） |
| `glm-5` | ✅ | — |
| `glm-4.6` | ❌ | 403 banned |
| `qwen3-235b-a22b-instruct-2507` | ✅ | — |
| `qwen3-32b` | ⚠️ | 非流式报 `enable_thinking must be set to false`；改用 `stream: true` ✅ |
| `grok-4` | ❌ | 403 banned |

#### claude_native（`POST /v1/messages`）

| 模型 | 状态 | 备注 |
|---|---|---|
| `claude-sonnet-4-6` | ✅ | — |
| `claude-opus-4-7` | ✅ | — |
| `claude-sonnet-4-5-20250929` | ✅ | — |
| `claude-haiku-4-5-20251001` | ✅ | — |
| `claude-opus-4-6` | ❌ | 30s+ 超时无响应（通道异常） |
| `claude-opus-4-1-20250805` | ❌ | 403 用户已被封禁 |
| `claude-3-5-sonnet-20241022` | ❌ | 403 用户已被封禁 |
| `kimi-k2-thinking` | ✅ | Anthropic 协议透传 |
| `kimi-k2.6` | ✅ | — |
| `deepseek-v4-pro` | ✅ | — |
| `mimo-v2-pro` | ✅ | — |
| `glm-4.7` / `glm-5` | ❌ | 403 banned |

#### responses（`POST /v1/responses`）

| 模型 | 状态 | 备注 |
|---|---|---|
| `gpt-5` | ✅ | — |
| `gpt-5.1` | ✅ | — |
| `gpt-5.1-codex` | ✅ | — |
| `gpt-5.1-codex-mini` | ✅ | — |
| `gpt-5.1-codex-max` | ✅ | — |
| `gpt-5.2` | ✅ | — |
| `gpt-5.2-chat-latest` | ✅ | — |
| `gpt-5-codex` | ✅ | — |
| `gpt-5-pro` | ✅ | — |
| `o3-pro` | ⚠️ | 90s 内未返回（推理模型耗时长，需 ≥ 20 分钟超时） |

#### 模型列表 `GET /v1/models` ✅

实测可用，返回 403 个模型条目。**生产前请用此接口确认目标模型存在及其 `support_apis`。**

---

## 5. 辅助接口

> 以下接口在测试 Key 下大多权限受限，仅作协议参考。生产前请用自己的 Key 验证。

### 5.1 模型列表 `GET /v1/models` ✅

```bash
curl https://stepcode.basemind.com/v1/models -H "Authorization: Bearer $API_KEY"
```

返回 `{ "data": [{ "id": "...", "support_apis": [{"id": "chat", ...}] }] }`，按 `support_apis.id` 判断模型可走哪些接口。

### 5.2 文生图 `POST /v1/images/generations` ✅

实测通过：`doubao-seedream-4.0`、`doubao-img-gen`。

```bash
curl https://stepcode.basemind.com/v1/images/generations \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"doubao-seedream-4.0","prompt":"a cute cat"}'
```

响应：

```json
{
  "created": 1778492623,
  "data": [{"url": "https://eval-model.tos-cn-shanghai.volces.com/.../xxx.jpeg?...&X-Tos-Expires=259200&..."}],
  "usage": {"input_tokens": 0, "input_tokens_details": {"image_tokens": 0}}
}
```

> URL 实测带 `X-Tos-Expires=259200`（约 3 天）。需要长期保存请下载到自有存储。

支持 `extra_body` 透传非标准参数（如 `controlnet_args`、`seed`、`scale` 等）。

部分模型在测试 Key 下不可用：`flux-pro` / `dall-e-3` / `sd3-large` / `hunyuan-img-gen` / `kling-v1` 等。

### 5.3 语音转文本 `POST /v1/audio/transcriptions` ✅

实测通过：`doubao-asr`。

```bash
curl https://stepcode.basemind.com/v1/audio/transcriptions \
  -H "Authorization: Bearer $API_KEY" \
  -F "model=doubao-asr" \
  -F "file=@/path/to/audio.mp3"
```

响应：

```json
{
  "task": "c71db955-...",
  "duration": 1512,
  "segments": [{"id": 0, "start": 200, "end": 1240, "text": "Hello, this is a test.", ...}],
  "text": "Hello, this is a test."
}
```

测试 Key 下 `whisper-1` 返回 403 banned；`hunyuan-asr` 资源包耗尽。

### 5.4 文件上传 `POST /tools/v1/file/upload` ✅

工具类接口，返回带签名的 TOS URL（约 3 天有效），可作为图像编辑 / 图生视频 / 多模态输入。

```bash
curl https://stepcode.basemind.com/tools/v1/file/upload \
  -H "Authorization: Bearer $API_KEY" \
  -F "file=@/path/to/image.png"
```

响应：

```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "file_key": "models-proxy/prod/file_upload/2026/05/11/1778492888-xxx.png",
    "url": "https://eval-model.tos-cn-shanghai.volces.com/...?X-Tos-Expires=259200&..."
  }
}
```

Base64 形式：传 `file=data:image/png;base64,...`（不带前缀时需要同时传 `filename`）。

### 5.5 其他接口（协议确认，测试 Key 受限） ⚠️

| 端点 | 协议 | 实测错误 |
|---|---|---|
| `POST /v1/completions` | OpenAI Completions（旧版） | `deepseek-chat` 403 banned |
| `POST /v1/embeddings` | OpenAI Embeddings | `text-embedding-3-*` 全部 403/503 |
| `POST /v1/audio/speech` | OpenAI TTS | `gpt-tts` 403、`tts-1` 非法模型名、其他资源包耗尽 |
| `POST /v1/images/edits` | OpenAI Image Edit | `gpt-image-1` 403 |
| `POST /v1/audio/clone-upload` | 豆包语音复刻 | 未实测 |
| `WSS /v1/realtime` | OpenAI Realtime | 未实测 |
| `POST /v1/videos/generations` | 视频生成（异步） | `hailuo-video-gen` / `kling-v1.6-video-gen` 403 |
| `GET /v1/videos/query` | 视频任务查询 | 未实测 |
| `POST /v1/ocr` | Mistral OCR | `mistral-ocr-latest` 返回空 `{usage_info:{}}` |
| `POST /gemini/v1alpha/models/{model}:generateContent` | Google Gemini 原生 | 503 No available Gemini accounts |

各接口的请求/响应格式与原始厂商一致，参考：

- OpenAI：<https://platform.openai.com/docs/api-reference>
- Anthropic：<https://docs.anthropic.com/en/api/messages>
- Google Gemini：<https://ai.google.dev/gemini-api/docs/gemini-3>
- Mistral OCR：<https://docs.mistral.ai/api/#tag/ocr>

---

## 6. SDK 接入

### 6.1 Python · OpenAI SDK ✅ 实测可用

```python
from openai import OpenAI

client = OpenAI(
    api_key="ak-xxx",
    base_url="https://stepcode.basemind.com/v1",
    timeout=600,
)

# chat
stream = client.chat.completions.create(
    model="claude-sonnet-4-6",
    messages=[{"role": "user", "content": "用一句话介绍 Rust"}],
    stream=True,
    max_tokens=200,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="")

# responses
resp = client.responses.create(model="gpt-5.1", input="hi")
print(resp.output[0].content[0].text)
```

### 6.2 Python · Anthropic SDK ✅ 实测可用

```python
from anthropic import Anthropic

client = Anthropic(
    api_key="ak-xxx",
    base_url="https://stepcode.basemind.com",
)

msg = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=512,
    system="You are concise.",
    messages=[{"role": "user", "content": "你好"}],
)
print(msg.content[0].text)
```

### 6.3 Node.js · OpenAI SDK

```javascript
import OpenAI from "openai";

const client = new OpenAI({
  apiKey: "ak-xxx",
  baseURL: "https://stepcode.basemind.com/v1",
  timeout: 600_000,
});

const resp = await client.chat.completions.create({
  model: "claude-sonnet-4-6",
  messages: [{ role: "user", content: "你好" }],
});
console.log(resp.choices[0].message.content);
```

### 6.4 Claude Code / Codex CLI

- **Claude Code** 可直接将 `ANTHROPIC_BASE_URL=https://stepcode.basemind.com` 配置后使用 `/v1/messages` 兼容接口。
- **Codex CLI** 通过 `/v1/responses` 接口对接 `gpt-5.1-codex` 等模型，注意将其 base url 指向本服务。

### 6.5 原生 HTTP（流式）

```python
import json, requests

resp = requests.post(
    "https://stepcode.basemind.com/v1/chat/completions",
    headers={
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    },
    data=json.dumps({
        "model": "claude-sonnet-4-6",
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 200,
    }),
    stream=True,
    timeout=600,
)
for line in resp.iter_lines(decode_unicode=True):
    if line:
        print(line)
```

---

## 7. FAQ

### 7.1 三个接口怎么选？

- 用 **Anthropic SDK / Claude Code / Claude App** → `/v1/messages`（`claude_native`）
- 用 **OpenAI SDK 等通用客户端** → `/v1/chat/completions`（`chat`）
- 用 **Codex / GPT-5 系列 / 需要 reasoning trace** → `/v1/responses`（`responses`）

> 同一个模型可能同时支持多个接口（如 Claude 4.x 同时支持 `chat` + `claude_native`，GPT-5 系列同时支持 `chat` + `responses`），按你客户端原生协议选即可。

### 7.2 `424 ... User has been banned` / `用户已被封禁`

测试 Key 对该模型 / 通道无权限。换用你自己已开通的 Key。

### 7.3 `424 model config unresolvable from MODELS_MAP`

模型名拼错或平台暂未路由。先 `GET /v1/models` 确认模型 id 是否存在以及其 `support_apis`。

### 7.4 `503 model xxx is currently offline` / `No available channel`

上游通道临时下线或无可用账号。建议：

- 关键链路准备主备模型；
- 同名模型的 `-backup`、`:palm-aws`、`:ksyun-azure` 等变体参数兼容，可作为容灾切换。

### 7.5 `400 parameter.enable_thinking must be set to false for non-streaming calls`

`qwen3-*b` 系列（如 `qwen3-32b`）必须使用流式调用。请加 `"stream": true`。

### 7.6 `429 reaching limitation of api_key`

每个 API Key 有固定 QPS / 并发上限。降并发或申请独立 Key。

### 7.7 TPM 用尽

`TPM (Tokens Per Minute) limit exceeded` 或 `Too many tokens, please wait before trying again.`：下一分钟重试；持续触发需申请独立 TPM 分组。

### 7.8 `context canceled`

客户端主动断开连接，多数是超时设置过短。普通模型 10 分钟，推理模型 20–30 分钟。

### 7.9 我的图片 / 视频 URL 多久过期？

实测对象存储 URL 携带 `X-Tos-Expires=259200`（约 3 天）。长期保存请下载到自有存储。

### 7.10 流式输出最后一帧带 token 用量

- chat：最后一个 `data: {...}` 帧（在 `[DONE]` 之前）的 `usage` 含完整 token 统计；
- claude_native：`message_delta` 事件的 `usage` 字段；
- responses：`response.completed` 事件的 `response.usage`。

### 7.11 如何定位线上问题

所有非 200 响应都附带 `trace_id`，报问题时一并提供。

- 客户端可通过 `X-Request-ID` 自定义 trace；不传时服务自动生成。
- 响应头 `X-Request-ID` 始终返回；chat / messages / responses 接口还会返回 `X-Model-ID` 表示实际下游模型。
