# feishu-relay-gateway

`feishu-relay` 系统的 **Gateway** 端 — 给 [`relay_bot/`](relay_bot/) 子目录下的 bot 节点提供心跳接入、请求中转、客户端 API key 管理。对外暴露 OpenAI / Anthropic 兼容 API，把请求经飞书消息隧道派发给内网 bot 节点。

> v3 起 bot 主仓即本仓 [`relay_bot/`](relay_bot/) 子目录。
> 老的独立仓库 [`Zenwh/feishu-relay-bot`](https://github.com/Zenwh/feishu-relay-bot) 已 archive，不再接受 issue / PR。

## 架构

```
                   ┌──────────────────────────────────────────┐
   OpenAI/         │                  Gateway                  │
   Anthropic SDK   │  outside_caller/relay_server.py:9100     │
   ───────────►   │                                          │
                   │  - /v1/chat/completions, /v1/messages    │
                   │  - sk-relay-xxx API key 鉴权 + 配额      │
                   │  - bot_pool RR 调度 + 飞书消息收发       │
                   └──────────────┬───────────────────────────┘
                                  │ 飞书 IM REST + WS
                                  ▼
                          ┌───────────────────┐
                          │  feishu-relay-bot │  ← relay_bot/ 子目录
                          │   (内网员工机器)   │
                          │                   │
                          │  POST /v1/messages│
                          └─────────┬─────────┘
                                    │
                                    ▼
                            上游 LLM Provider
```

bot 节点通过飞书 IM 隧道把内网 LLM 调用能力暴露给外网。Gateway 是入口 + 调度器；bot 是执行器。

## 部署

### 启动 Gateway

```bash
pip install fastapi uvicorn httpx pyyaml pydantic
uvicorn outside_caller.relay_server:app --host 0.0.0.0 --port 9100
```

### 必要环境变量

| 变量 | 说明 | 默认 |
|---|---|---|
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | Gateway 用的飞书 App 凭证（注意：bot 节点用各自的 App） | `cli_xxx` / `WgV...` |
| `RELAY_PORT` | HTTP 监听端口 | `9100` |
| `RELAY_HOST` | 监听地址 | `0.0.0.0` |
| `FEISHU_STATE_DIR` | 状态文件目录（含 token、bot pool、usage） | `~/.feishu_outside_caller` |
| `POLL_INTERVAL_S` | bot 响应轮询间隔（秒） | `1.5` |
| `POLL_TIMEOUT_S` | 单请求最长等待（秒） | `240` |

完整字段见 [`outside_caller/config.py`](outside_caller/config.py)。

### 首次启动

需要先跑一次 OAuth 给 Gateway 拿飞书 user token：

```bash
python -m outside_caller.oauth_once
# 浏览器走完 OAuth，token 落到 ~/.feishu_outside_caller/tokens_<app_id>.json
```

## API

### 客户端 API（外部 LLM SDK）

| Method | Path | 说明 |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI 风格 chat completion |
| POST | `/v1/messages` | Anthropic 风格 messages |
| GET | `/v1/models` | 可用模型列表 |
| GET | `/health` | 健康检查 |

鉴权用 `Authorization: Bearer sk-relay-xxx`，key 通过 `/admin/keys` 管理。

### Bot 节点 API（feishu-relay-bot 节点用）

| Method | Path | 说明 |
|---|---|---|
| POST | `/agent/heartbeat` | bot 节点心跳上报（节点信息、模型清单、用量） |
| POST | `/agent/offline` | bot 节点主动下线 |

### Admin API（需 admin 权限的 sk-relay-xxx）

| Method | Path | 说明 |
|---|---|---|
| GET/POST/DELETE/PATCH | `/admin/keys` | 客户端 API key 管理 |
| GET | `/admin/keys/{key}/usage` | 单 key 用量统计 |
| GET | `/admin/usage` | 全局用量汇总 |
| GET | `/admin/nodes` | bot 节点列表 |
| POST | `/admin/nodes/{node_id}/{upgrade,restart,drain}` | 单节点管控 |
| POST | `/admin/nodes/upgrade-all` | 广播升级 |
| GET | `/admin/dashboard` | Web 管理控制台 |

## 关键模块

- [`outside_caller/relay_server.py`](outside_caller/relay_server.py) — FastAPI 入口，所有 endpoint
- [`outside_caller/bot_pool.py`](outside_caller/bot_pool.py) — bot 节点池 + RR 调度 + 飞书消息收发
- [`outside_caller/api_keys.py`](outside_caller/api_keys.py) — 客户端 API key
- [`outside_caller/usage.py`](outside_caller/usage.py) — 用量统计
- [`outside_caller/rate_limit.py`](outside_caller/rate_limit.py) — RPM / daily quota
- [`outside_caller/feishu_token.py`](outside_caller/feishu_token.py) + [`oauth_once.py`](outside_caller/oauth_once.py) — 飞书 OAuth 长 token 维护
- [`outside_caller/relay_codec.py`](outside_caller/relay_codec.py) — 飞书消息隧道编解码（zlib 压缩）
- [`outside_caller/models.py`](outside_caller/models.py) — 模型白名单 + endpoint 路由
- [`outside_caller/dashboard/`](outside_caller/dashboard/) — admin 控制台前端

## 当前部署

生产 Gateway：`https://offer.yxzrkj.cn/llm/api`
（阿里云杭州 `i-bp1a7ky0stlf4agxzlhn` / `47.97.3.198`）

## 协议

跟 bot 节点之间走 `relay_protocol v3`（详见 [`relay_bot/feishu_relay_bot/relay_protocol.py`](relay_bot/feishu_relay_bot/relay_protocol.py)）。所有消息走飞书 IM 文本隧道，超 50KB 自动 zlib 压缩；超 140KB 拆 multipart。
