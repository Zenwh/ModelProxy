# Feishu Relay Bot 部署操作手册

## 架构概览

```
LLM Client (Cursor / App)
    │ HTTP
    ▼
Gateway (公网)          ←── 本仓库 outside_caller/
    │ 飞书消息通道 (NAT 穿透)
    ▼
Bot × N (内网各机器)    ←── 本包 feishu-relay-bot
    │ HTTP
    ▼
Model Proxy (内网)
```

Bot 通过飞书 WebSocket 与 Gateway 通信，不需要公网 IP。

---

## 1. 前置条件

| 项目 | 要求 |
|------|------|
| Python | >= 3.9 |
| 网络 | 目标机器能访问飞书 WS（`wss://open.feishu.cn`）和 Model Proxy |
| 飞书应用 | 每个 bot 实例需要一组 `app_id` + `app_secret` |
| 分发机器 | Docker 已安装，局域网可达 |

---

## 2. 构建 wheel

在开发机（本仓库目录）执行：

```bash
cd relay_bot
pip wheel . -w dist/
```

产出文件：`dist/feishu_relay_bot-<version>-py3-none-any.whl`

---

## 3. 启动内网 PyPI 源

```bash
# 在本仓库根目录
docker compose up -d pypi
```

服务默认监听 **9080** 端口，可通过环境变量 `PYPI_PORT` 修改。

验证：

```bash
curl http://localhost:9080/simple/feishu-relay-bot/
```

应返回包含 `.whl` 链接的 HTML 页面。

---

## 4. 目标机器安装 Bot

### 4.1 安装

```bash
pip install \
  --index-url http://10.141.32.112:9080/simple/ \
  --trusted-host 10.141.32.112 \
  feishu-relay-bot
```

> 将 `10.141.32.112` 替换为你的 Docker 宿主机局域网 IP。

验证安装：

```bash
feishu-relay-bot version
# feishu-relay-bot 0.1.0
```

### 4.2 写配置文件

```bash
sudo mkdir -p /etc/relay-bot
sudo tee /etc/relay-bot/config.yaml <<EOF
feishu:
  app_id: cli_xxx
  app_secret: your_app_secret_here

mp:
  url: http://mp.internal:8000

# 可选
node_id: ""                  # 空则自动用 hostname
heartbeat_interval_s: 30
EOF
```

配置项说明：

| 字段 | 环境变量覆盖 | 说明 |
|------|-------------|------|
| `feishu.app_id` | `FEISHU_APP_ID` | 飞书应用 App ID |
| `feishu.app_secret` | `FEISHU_APP_SECRET` | 飞书应用 App Secret |
| `mp.url` | `MP_URL` | Model Proxy 地址 |
| `node_id` | `NODE_ID` | 节点标识，默认 hostname |
| `heartbeat_interval_s` | `HEARTBEAT_INTERVAL_S` | 心跳间隔（秒） |

### 4.3 启动

手动启动（调试用）：

```bash
feishu-relay-bot run --config /etc/relay-bot/config.yaml
```

### 4.4 systemd 托管（生产推荐）

```bash
# 拷贝 service 文件
sudo cp relay-bot.service /etc/systemd/system/

# 启用并启动
sudo systemctl daemon-reload
sudo systemctl enable relay-bot
sudo systemctl start relay-bot

# 查看状态
sudo systemctl status relay-bot

# 查看日志
sudo journalctl -u relay-bot -f
```

service 文件内容（已包含在包中）：

```ini
[Unit]
Description=Feishu Relay Bot
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/feishu-relay-bot run --config /etc/relay-bot/config.yaml
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

---

## 5. 升级 Bot

### 5.1 手动升级

**开发机：** 修改 `relay_bot/__init__.py` 中的版本号，重新构建 wheel：

```bash
cd relay_bot
pip wheel . -w dist/
```

新 wheel 放入 `dist/` 后，PyPI 源自动可见（无需重启容器）。

**目标机器：**

```bash
pip install -U \
  --index-url http://10.141.32.112:9080/simple/ \
  --trusted-host 10.141.32.112 \
  feishu-relay-bot

# 重启服务
sudo systemctl restart relay-bot
```

### 5.2 通过 Dashboard 在线升级（推荐）

1. 在 Gateway Dashboard 节点管理页面，点击「升级」按钮
2. Gateway 通过飞书消息下发升级指令给 bot
3. Bot 自动执行 `pip install -U feishu-relay-bot==<target_version>`
4. 安装成功后进程退出，systemd 自动拉起新版本
5. 新版本启动后上报心跳，Dashboard 显示新版本号

也可通过 API 触发：

```bash
# 升级单个节点
curl -X POST http://gateway:8000/admin/nodes/{node_id}/upgrade \
  -H "Content-Type: application/json" \
  -d '{"target_version": "0.2.0"}'

# 升级全部节点
curl -X POST http://gateway:8000/admin/nodes/upgrade-all \
  -H "Content-Type: application/json" \
  -d '{"target_version": "0.2.0"}'
```

### 5.3 灰度升级策略

1. 先升级 1 台，观察 Dashboard 心跳版本号是否更新
2. 确认正常后，升级剩余节点（全量或分批）
3. 如有异常，回滚：手动 `pip install feishu-relay-bot==旧版本号`

---

## 6. 运维操作

### 查看节点状态

```bash
curl http://gateway:8000/api/discovery | python3 -m json.tool
```

返回所有在线节点的 node_id、版本、负载、最后心跳时间。

### 重启节点

```bash
curl -X POST http://gateway:8000/admin/nodes/{node_id}/restart
```

### 优雅下线（drain）

```bash
curl -X POST http://gateway:8000/admin/nodes/{node_id}/drain
```

bot 会停止接受新请求，处理完当前请求后退出。

### 故障排查

```bash
# 查看 bot 日志
sudo journalctl -u relay-bot -n 100

# 检查飞书 WS 连接
sudo journalctl -u relay-bot | grep -i "websocket\|连接\|connect"

# 检查 MP 可达性
curl http://mp.internal:8000/health

# 检查 PyPI 源可达性
curl http://10.141.32.112:9080/simple/
```

---

## 7. 多 Bot 部署清单

每台新机器部署步骤：

```bash
# 1. 安装
pip install --index-url http://10.141.32.112:9080/simple/ \
  --trusted-host 10.141.32.112 feishu-relay-bot

# 2. 配置（改 app_id/app_secret，每个 bot 用不同的飞书应用）
sudo mkdir -p /etc/relay-bot
sudo vim /etc/relay-bot/config.yaml

# 3. 部署 systemd
sudo tee /etc/systemd/system/relay-bot.service <<'EOF'
[Unit]
Description=Feishu Relay Bot
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/feishu-relay-bot run --config /etc/relay-bot/config.yaml
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now relay-bot

# 4. 验证
sudo systemctl status relay-bot
feishu-relay-bot version
```

部署完成后，在 Gateway Dashboard 应能看到新节点上线。

---

## 8. 目录结构

```
relay_bot/
├── pyproject.toml          # 包定义
├── README.md
├── DEPLOY.md               # 本文档
├── config.example.yaml     # 配置模板
├── relay-bot.service       # systemd 服务模板
├── dist/                   # wheel 输出（PyPI 源挂载此目录）
└── relay_bot/
    ├── __init__.py         # 版本号
    ├── cli.py              # CLI 入口
    ├── config.py           # 配置加载
    ├── worker.py           # 核心：消息收发 + 调 MP
    ├── heartbeat.py        # 心跳上报
    ├── ctrl.py             # 管控指令处理
    └── upgrade.py          # 自升级逻辑
```
