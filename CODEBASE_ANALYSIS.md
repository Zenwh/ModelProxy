# ModelProxy Codebase Analysis

**Date**: 2026-05-20  
**Purpose**: Understanding the current code layout for building an OpenAI-compatible API wrapper service

---

## 1. Current Codebase Structure

### Directory Layout
```
ModelProxy/
├── API.md                          # Complete API documentation
├── README.md                       # Project overview & demo info
├── feishu_mock/                    # Mock Feishu server (test infrastructure)
│   ├── app.py                      # FastAPI server (11KB)
│   ├── config.py                   # Configuration
│   ├── event_builder.py            # Feishu event construction
│   ├── feishu_crypto.py            # Signature verification
│   ├── store.py                    # In-memory message store
│   ├── examples/
│   │   └── bot_claude.py           # Bot that calls ModelProxy Claude
│   └── requirements.txt
├── outside_caller/                 # External caller utilities
│   ├── config.py                   # Feishu app credentials & OAuth config
│   ├── talk.py                     # Send messages to bot & poll replies (7.2KB)
│   └── oauth_once.py               # OAuth token acquisition
├── PRD/                            # Product documentation
├── demo/                           # High-fidelity demo (HTML/CSS/JS)
└── docker/                         # Docker configuration
```

---

## 2. Key Components Analysis

### 2.1 Feishu Integration Layer (`outside_caller/`)

**Purpose**: Enables external callers to interact with Feishu bots via OAuth.

**Key Files**:

#### `config.py` - Configuration
```python
APP_ID = "cli_a955f5aa04f81bda"  # Feishu app ID
APP_SECRET = "kETZGoqR0S6eEwhFhLszLd7bqsKSt7cr"
FEISHU_BASE = "https://open.feishu.cn"
```

#### `talk.py` - Core Message Flow (7.2KB)
**Main Functions**:
- `find_bot_open_id(tokens)` - Enumerate user's P2P chats to find target bot
- `send_to_bot(tokens, bot_open_id, text)` - Send text message to bot via Feishu API
- `get_p2p_chat_id(tokens, bot_open_id)` - Get P2P chat ID with bot
- `poll_reply(tokens, chat_id, after_ms, bot_open_id, timeout_s)` - Poll chat history for bot's first reply after timestamp
- `render_message(msg)` - Parse Feishu message format

**Message Flow**:
1. Load OAuth tokens from `~/.feishu_outside_caller/tokens_*.json`
2. Find bot's open_id by enumerating P2P chats
3. Send user message via `POST /open-apis/im/v1/messages`
4. Poll `GET /open-apis/im/v1/messages` with `sort_type=ByCreateTimeDesc`
5. Filter for messages from bot (sender_type="app" or sender_id matches bot)

**Key Implementation Details**:
- Uses `httpx` for async HTTP
- Polls with 1-second intervals, respects timeout
- Tracks seen message IDs to avoid duplicates
- Renders different message types (text, card, etc.)

---

### 2.2 Bot Example (`feishu_mock/examples/bot_claude.py`)

**Purpose**: Reference bot that calls ModelProxy's Claude endpoint.

**Key Code**:
```python
MODELPROXY_BASE = "https://stepcode.basemind.com"
MODELPROXY_API_KEY = "ak-xqmsbezufm409fkaxruv35njq4vlnvtq"
MODELPROXY_MODEL = "claude-opus-4-5-20251101"

async def ask_claude(user_text: str) -> str:
    payload = {
        "model": MODELPROXY_MODEL,
        "messages": [{"role": "user", "content": user_text}],
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=120) as cli:
        r = await cli.post(
            f"{MODELPROXY_BASE}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {MODELPROXY_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    data = r.json()
    choices = data.get("choices") or []
    if not choices:
        return f"[Claude 调用失败] {data.get('msg') or data}"
    return choices[0]["message"]["content"]
```

**Architecture**:
- Receives webhook from `feishu_mock` at `POST /feishu/webhook`
- Calls ModelProxy's `/v1/chat/completions` endpoint
- Sends reply back via `POST /open-apis/im/v1/messages` to feishu_mock
- Uses background tasks to avoid 3-second webhook timeout

---

## 3. OpenAI Chat Completions API Format

### Request Format (`POST /v1/chat/completions`)

**Headers**:
```
Authorization: Bearer <API_KEY>
Content-Type: application/json
```

**Minimal Request Body**:
```json
{
  "model": "claude-sonnet-4-6",
  "messages": [{"role": "user", "content": "hi"}],
  "max_tokens": 50
}
```

**Full Request Options**:
- `model` (required): Model identifier (e.g., "claude-sonnet-4-6", "gpt-4o")
- `messages` (required): Array of message objects with `role` and `content`
- `max_tokens` (optional): Max output tokens
- `temperature` (optional): Sampling temperature
- `top_p` (optional): Nucleus sampling parameter
- `system` (optional): System prompt
- `stream` (optional): Enable streaming (SSE format)
- `tools` (optional): Tool/function definitions for tool calling

**Message Object**:
```json
{
  "role": "user|assistant|system",
  "content": "text content or array of content blocks"
}
```

### Response Format (Non-Streaming)

**Success Response (200)**:
```json
{
  "id": "msg_bdrk_01Dt9eNi4dK4Ubaui56F5kY8",
  "object": "chat.completion",
  "created": 1778492585,
  "model": "claude-sonnet-4-6",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "Hi there! How are you doing?"
    },
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

**Error Response (non-2xx)**:
```json
{
  "code": 503,
  "msg": "model xxx is currently offline [trace_id=...]",
  "data": null
}
```

### Streaming Response Format

With `"stream": true`, response is Server-Sent Events (SSE):
```
data: {"id":"...","choices":[{"index":0,"delta":{"role":"assistant"}}], "usage":{...}}
data: {"id":"...","choices":[{"index":0,"delta":{"content":"1, "}}], "usage":{...}}
data: {"id":"...","choices":[{"index":0,"delta":{},"finish_reason":"stop"}], "usage":{...}}
data: [DONE]
```

### Tool Calling Support

**Request with Tools**:
```json
{
  "model": "gpt-4o",
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
}
```

**Response with Tool Call**:
```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "",
      "tool_calls": [{
        "id": "call_WoZAOVjO5ENj8UHyfqlIFIXb",
        "type": "function",
        "function": {
          "name": "get_weather",
          "arguments": "{\"city\":\"Shanghai\"}"
        }
      }]
    },
    "finish_reason": "tool_calls"
  }]
}
```

---

## 4. Supported Models (Chat Completions)

**Tested & Working** (as of 2026-05-11):
- Claude: `claude-sonnet-4-6`, `claude-sonnet-4-5-20250929`
- GPT: `gpt-4o`, `gpt-4.1`, `gpt-5`, `gpt-5.1`, `gpt-5.1-chat-latest`
- Gemini: `gemini-2.5-pro`
- Kimi: `kimi-k2-0905-preview`, `kimi-k2-thinking`, `kimi-k2.6`
- Doubao: `doubao-1.5-pro-32k`, `doubao-seed-1.6`
- DeepSeek: `deepseek-r1`
- GLM: `glm-5`
- Qwen: `qwen3-235b-a22b-instruct-2507`, `qwen3-32b` (streaming only)

**Model List Endpoint**:
```
GET /v1/models
Authorization: Bearer <API_KEY>
```
Returns 403+ models with `support_apis` field indicating which endpoints each model supports.

---

## 5. Existing OpenAI-Compatible Server Code

**Status**: ✅ **FOUND** - `feishu_mock/examples/bot_claude.py`

This is a **working reference implementation** that:
1. Receives Feishu webhook events
2. Calls ModelProxy's `/v1/chat/completions` endpoint
3. Returns responses via Feishu API

**Key Characteristics**:
- Uses FastAPI for HTTP server
- Implements async/await pattern
- Handles background task processing
- Properly formats OpenAI chat completions requests
- Extracts response from standard OpenAI format

**NOT a generic wrapper** - it's specifically designed for Feishu bot integration.

---

## 6. Key Insights for Building OpenAI-Compatible Wrapper

### What You Have
1. ✅ **Working reference implementation** (`bot_claude.py`) showing how to call `/v1/chat/completions`
2. ✅ **Complete API documentation** (`API.md`) with exact request/response formats
3. ✅ **Message polling pattern** (`talk.py`) for async communication
4. ✅ **FastAPI infrastructure** already in use

### What's Missing
1. ❌ **Generic OpenAI-compatible server** - current code is Feishu-specific
2. ❌ **Request validation/transformation layer** - no middleware for format conversion
3. ❌ **Streaming implementation** - bot_claude.py uses `stream: False`
4. ❌ **Error handling wrapper** - no standardized error response transformation
5. ❌ **Rate limiting/auth** - no API key validation beyond Bearer token pass-through

### Design Recommendations

**For a Generic OpenAI-Compatible Wrapper**:

1. **Endpoint Structure**:
   ```
   POST /v1/chat/completions          # Main endpoint
   GET  /v1/models                    # Model listing
   POST /v1/chat/completions/stream   # Optional: explicit streaming endpoint
   ```

2. **Request Flow**:
   - Accept OpenAI-format request
   - Validate against OpenAI schema
   - Transform to ModelProxy format (if needed)
   - Forward to `https://stepcode.basemind.com/v1/chat/completions`
   - Transform response back to OpenAI format
   - Return to client

3. **Key Differences to Handle**:
   - ModelProxy uses same format as OpenAI, so minimal transformation needed
   - Auth: Both use `Authorization: Bearer <KEY>` (compatible)
   - Response format: Already OpenAI-compatible

4. **Streaming Considerations**:
   - ModelProxy returns SSE format (same as OpenAI)
   - Can proxy stream directly without transformation
   - Need to handle `[DONE]` marker properly

---

## 7. API Documentation Summary

**Base URL**: `https://stepcode.basemind.com`

**Three Core Interfaces**:
1. **`POST /v1/messages`** (Anthropic Messages) - For Claude SDK, Claude Code, Claude App
2. **`POST /v1/chat/completions`** (OpenAI Chat) - **Most compatible, recommended for wrappers**
3. **`POST /v1/responses`** (OpenAI Responses) - For GPT-5, Codex CLI, reasoning models

**Authentication**: 
- Bearer Token: `Authorization: Bearer <API_KEY>`
- Anthropic style: `x-api-key: <API_KEY>` + `anthropic-version: 2023-06-01`

**Timeout Recommendations**:
- Regular models: ≥ 5 minutes (recommend 10)
- Thinking/Reasoning models: 20-30 minutes

**Error Response Format**:
```json
{
  "code": <HTTP_CODE>,
  "msg": "error message [trace_id=...]",
  "data": null
}
```

---

## 8. Testing Infrastructure

**Feishu Mock Server** (`feishu_mock/app.py`):
- Simulates Feishu webhook and API endpoints
- Stores sent messages for verification
- Endpoints:
  - `POST /mock/feishu/receive` - Trigger user message
  - `GET /mock/feishu/sent` - Query sent messages
  - `DELETE /mock/feishu/sent` - Clear messages

**Usage Pattern**:
1. Start feishu_mock on port 8000
2. Configure bot to use `http://localhost:8000` as Feishu API base
3. Send test messages via `/mock/feishu/receive`
4. Verify responses via `/mock/feishu/sent`

---

## Summary

The ModelProxy codebase provides:
- **Complete OpenAI-compatible API documentation** with exact formats
- **Working reference implementation** showing how to call the API
- **Test infrastructure** (feishu_mock) for integration testing
- **Async/await patterns** using FastAPI and httpx

**To build an OpenAI-compatible wrapper**, you can:
1. Use `bot_claude.py` as a template
2. Generalize it to accept any OpenAI-format request
3. Forward to ModelProxy's `/v1/chat/completions` endpoint
4. Return responses in OpenAI format (already compatible)
5. Add streaming support by proxying SSE directly

The main work is **removing Feishu-specific logic** and **adding generic request/response handling**, not implementing the API protocol itself.
