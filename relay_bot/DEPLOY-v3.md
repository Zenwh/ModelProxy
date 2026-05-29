# Feishu Relay Worker v3 部署指引

> 适用版本：`feishu-relay-bot 3.0.1`（git tag `relay-v3.0.1`）
> 目标：在新机器上把一个 worker 节点接入线上 gateway（`47.97.3.198:9100`）

---

## 0. 前置条件

| 项目 | 要求 |
|---|---|
| 操作系统 | Linux（生产推荐 Ubuntu 20.04+ / CentOS 7+），macOS 也可作开发用 |
| Python | ≥ 3.9（自带或 `pyenv`/`conda`） |
| 网络出方向 | 能访问 `wss://msg-frontier.feishu.cn`（飞书 WS）+ `https://open.feishu.cn`（飞书 REST）+ 你的 Model Proxy 地址 |
| 凭据 | 一组飞书应用的 `app_id` + `app_secret`，以及 Model Proxy 的 `api_key` |
| Gateway 信息 | 共享的飞书会话 `chat_id`（向运维要） |

---

## 1. 准备机器、确认能上飞书

```bash
# 1) Python ≥ 3.9
python3 --version

# 2) 飞书 WS 出方向连通
nc -zv msg-frontier.feishu.cn 443

# 3) 你公司 Model Proxy 出方向连通（替换成你们实际地址）
curl -sI https://models-proxy.example.com/v1/models | head -3
```

任何一项不通就先解网络/DNS，不要继续。

---

## 2. 拿到 wheel

### 方式 A：从开发机直接 scp（推荐用于内测灰度）

开发机已经构建好 `relay_bot/dist/feishu_relay_bot-3.0.1-py3-none-any.whl`：

```bash
# 在开发机
scp /path/to/ModelProxy/relay_bot/dist/feishu_relay_bot-3.0.1-py3-none-any.whl \
    user@<目标机器>:/tmp/
```

### 方式 B：内网 PyPI（多节点批量部署）

在分发机（已装 Docker）上把 PyPI 起来：

```bash
cd /path/to/ModelProxy
docker compose up -d pypi
# 默认 9080 端口；可用 PYPI_PORT=xxxx docker compose up -d pypi 改
```

验证：

```bash
curl -s http://<分发机IP>:9080/simple/feishu-relay-bot/ | grep 3.0.1
```

### 方式 C：git clone 后本地 build（开发调试用）

```bash
git clone <repo> ModelProxy && cd ModelProxy
git checkout relay-v3.0.1
cd relay_bot
python3 -m pip install --user --upgrade build
python3 -m build --wheel
ls dist/feishu_relay_bot-3.0.1-py3-none-any.whl
```

---

## 3. 安装到目标机器

强烈建议用 venv 隔离，不要污染系统 Python：

```bash
# 在目标机器
sudo mkdir -p /opt/relay-bot
sudo chown $USER /opt/relay-bot
cd /opt/relay-bot

python3 -m venv .venv
source .venv/bin/activate

# —— 方式 A 用 wheel ——
pip install --upgrade pip
pip install /tmp/feishu_relay_bot-3.0.1-py3-none-any.whl

# —— 方式 B 用内网 PyPI ——
pip install --upgrade pip
pip install \
  --index-url http://<分发机IP>:9080/simple/ \
  --trusted-host <分发机IP> \
  feishu-relay-bot==3.0.1
```

验证：

```bash
feishu-relay-bot version
# 期望输出：feishu-relay-bot 3.0.1
```

---

## 4. 写配置文件

把仓库里的 `relay_bot/config.example.yaml` 复制为本机配置：

```bash
sudo mkdir -p /etc/relay-bot
sudo cp /tmp/config.example.yaml /etc/relay-bot/config.yaml
sudo chmod 600 /etc/relay-bot/config.yaml   # 含 secret，必须 600
sudo $EDITOR /etc/relay-bot/config.yaml
```

完整字段（v3）：

```yaml
feishu:
  app_id: cli_xxxxxxxxxxxxxxxx          # 必填，飞书开放平台 App ID
  app_secret: PLACEHOLDER_REPLACE_ME    # 必填，飞书 App Secret

mp:
  url: https://models-proxy.example.com # 必填，你们的 Model Proxy
  api_key: ak-xxxxxxxxxxxxxxxxxxxxxxxx  # 必填，MP API Key

# Gateway 已分配的共享飞书会话；同一 chat 内可多 worker 共存
chat_id: oc_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 节点身份（同一 chat 内必须唯一，否则心跳互相覆盖）
# 推荐格式：bot-<region>-<seq>，如 bot-cn-shanghai-01
node_id: bot-<region>-<seq>
heartbeat_interval_s: 30

# v3 流式参数（保守值，新上线 worker 直接用默认）
stream:
  flush_bytes: 1024
  flush_ms: 1000
  send_qps: 4.0

multipart_timeout_s: 180
```

字段说明：

| 字段 | 环境变量覆盖 | 说明 |
|---|---|---|
| `feishu.app_id` | `FEISHU_APP_ID` | 飞书应用 App ID |
| `feishu.app_secret` | `FEISHU_APP_SECRET` | 飞书应用 App Secret |
| `mp.url` | `MP_URL` | Model Proxy 地址 |
| `mp.api_key` | `MP_API_KEY` | Model Proxy API Key |
| `chat_id` | `CHAT_ID` | gateway↔worker 共享会话；向运维要 |
| `node_id` | `NODE_ID` | 节点 ID；同 chat 内**唯一** |
| `heartbeat_interval_s` | `HEARTBEAT_INTERVAL_S` | 心跳间隔；默认 30s |
| `stream.flush_bytes` | `STREAM_FLUSH_BYTES` | 累计文本 ≥ N 字节触发 flush |
| `stream.flush_ms` | `STREAM_FLUSH_MS` | 距上次 flush ≥ N 毫秒触发 |
| `stream.send_qps` | `STREAM_SEND_QPS` | 本地令牌桶限速；不要超过 4 |
| `multipart_timeout_s` | `MULTIPART_TIMEOUT_S` | 上行多片重组超时；建议 180 |

---

## 5. 前台试跑（务必先跑通这一步再上 systemd）

```bash
cd /opt/relay-bot
source .venv/bin/activate
feishu-relay-bot run --config /etc/relay-bot/config.yaml
```

期望日志（关键行）：

```
relay-bot: feishu-relay-bot v3.0.1
relay-bot:   node_id: bot-<你设的>
relay-bot:   mp_url:  https://models-proxy.example.com
relay-bot: 连接飞书 WebSocket (node_id=bot-<你设的>) ...
Lark: connected to wss://msg-frontier.feishu.cn/ws/v2?...
relay-bot.heartbeat: heartbeat sent: node_id=... version=3.0.1 load=...
```

只要看到 `connected to wss://` + 30 秒间隔的 `heartbeat sent`，worker 就站起来了。
按 Ctrl-C 停掉，进 systemd。

---

## 6. 在 gateway 端确认注册

在 gateway 主机（线上是 `47.97.3.198`）：

```bash
sudo journalctl -u llm-relay --since '2 minutes ago' --no-pager \
  | grep -E "hb-v3|node_id=<你设的node_id>"
```

如果看到这种行就 OK：

```
[hb-v3] node=bot-<你设的> caps=[zlib, relay_v3, multipart_in, stream_out] enabled=True
```

如果看到的是：

```
[hb-v3] caps missing 'relay_v3' — disabling node=...
```

说明 worker 装的不是 3.0.1，回到第 3 步重装。

---

## 7. systemd 托管（生产）

```bash
sudo tee /etc/systemd/system/relay-bot.service > /dev/null <<'EOF'
[Unit]
Description=Feishu Relay Bot (v3)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/opt/relay-bot/.venv/bin/feishu-relay-bot run --config /etc/relay-bot/config.yaml
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
# 跑成普通用户更稳；如果该用户无法读 /etc/relay-bot/config.yaml 就改 chown
User=nobody
Group=nogroup

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now relay-bot
sudo systemctl status relay-bot --no-pager
sudo journalctl -u relay-bot -f
```

---

## 8. 端到端冒烟测试

在 gateway 公网入口发一条最小请求（向运维要 gateway 的 OpenAI 兼容 API key）：

```bash
curl -sN https://<gateway域名>/v1/chat/completions \
  -H "Authorization: Bearer <gateway_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
        "model": "gpt-4o-mini",
        "stream": true,
        "messages": [{"role": "user", "content": "Reply ONLY with PONG"}]
      }'
```

预期：

- 首 chunk < 2s 收到
- gateway 日志看到 `→ ... req_id=<24hex> ... msgs=1 stream=True`
- worker 日志看到对应 `req_id` 的 `← req_id=<24hex>` 和 `stream_chunk seq=0..N`
- gateway 日志看到 `← req_id=<24hex> ok=True`

任一缺失参考第 10 节排查。

---

## 9. 多 worker 同时部署的关键点

1. **`node_id` 必须全集群唯一**。重复会导致心跳互相覆盖、流量错路由。
2. **`chat_id` 用同一个**。gateway 通过同 chat round-robin 多个节点。
3. **每台机器一份独立 venv + config**，不共享文件系统。
4. **不要把多个 worker 进程跑在同一机器上**：飞书 WS 单 chat 写入是 ~5 QPS，多进程同 chat 会触发限流。要扩容就加机器。
5. **灰度顺序**：1 节点跑通 24h → 3 节点 → 全量。
6. **下线**：直接 `systemctl stop relay-bot`，gateway 心跳超时 90s 后自动 disable 该节点。

---

## 10. 常见问题排查

### 10.1 `feishu-relay-bot version` 显示 0.1.0

旧版没卸干净。`pip uninstall feishu-relay-bot -y` 再重装。

### 10.2 worker 启动后 gateway 看不到心跳

检查顺序：
1. worker 日志有没有 `connected to wss://`？没有 → 网络问题，nc/curl 排查飞书域名
2. worker 日志有没有 `heartbeat sent`？没有 → 看 ERROR 行
3. gateway 日志 grep `<node_id>` → 没有 → 大概率 `chat_id` 配错
4. gateway 看到 `caps missing 'relay_v3'` → wheel 不是 3.0.1

### 10.3 请求 504 Gateway Timeout

通常是某条 `req_part` 在飞书 WS frontier 丢了。三件事：

- gateway 日志 grep `req_id` 看分了多少 part：`msgs=1` 一般稳；`msgs>2` 容易丢
- worker 日志 grep `multipart timeout` 看 assembler 是否超时
- 如果是大请求频繁 504，把 worker `multipart_timeout_s` 提到 240，gateway `MULTIPART_SEND_QPS` 降到 3.0

### 10.4 streaming 卡顿、chunk 间隔很长

worker 的 `stream.flush_ms` 太大（默认 1000ms 已经是稳态值）。不要把 `flush_bytes` 调小到 < 256，会频繁触发飞书限流。

### 10.5 `424 Failed Dependency` from gateway

下游 Model Proxy 返回的，不是 v3 链路问题。看 gateway 日志里的具体 MP 错误（账号没开通模型 / quota 超限 / 鉴权失败）。

---

## 11. 升级到下一版

```bash
cd /opt/relay-bot
source .venv/bin/activate
pip install --upgrade /tmp/feishu_relay_bot-<新版本>-py3-none-any.whl
sudo systemctl restart relay-bot
sudo journalctl -u relay-bot -f
feishu-relay-bot version    # 新版本号
```

---

## 12. 完整回滚

```bash
sudo systemctl stop relay-bot
cd /opt/relay-bot
source .venv/bin/activate
pip install --force-reinstall /tmp/feishu_relay_bot-<旧版本>-py3-none-any.whl
sudo systemctl start relay-bot
```

旧版（< 3.0.0）启动后心跳缺 `relay_v3` capability，gateway 会自动把它 disable，
流量切到剩余 v3 节点 — 不影响线上。
