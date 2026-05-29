#!/usr/bin/env bash
# Feishu Relay Worker v3 一键安装脚本
#
# 用法（在目标机器上）：
#   sudo NODE_ID=bot-cn-shanghai-01 \
#        WHEEL=/tmp/feishu_relay_bot-3.0.1-py3-none-any.whl \
#        bash install.sh
#
# 或者从内网 PyPI 装：
#   sudo NODE_ID=bot-cn-shanghai-01 \
#        PYPI_HOST=10.x.y.z PYPI_PORT=9080 \
#        bash install.sh
#
# 装完后还需要手动 vim /etc/relay-bot/config.yaml 填实 app_secret / mp.api_key / chat_id，
# 然后 systemctl start relay-bot。

set -euo pipefail

WHEEL="${WHEEL:-}"
PYPI_HOST="${PYPI_HOST:-}"
PYPI_PORT="${PYPI_PORT:-9080}"
NODE_ID="${NODE_ID:-bot-$(hostname)}"
INSTALL_DIR="${INSTALL_DIR:-/opt/relay-bot}"
CONFIG_DIR="${CONFIG_DIR:-/etc/relay-bot}"
SERVICE_NAME="relay-bot"

log() { printf '\033[1;36m[install]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[install]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || err "请用 sudo / root 执行"

# 1. Python ≥ 3.9
log "检查 Python ..."
if ! command -v python3 >/dev/null; then err "未找到 python3"; fi
PYVER=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
[ "$(printf '%s\n3.9\n' "$PYVER" | sort -V | head -1)" = "3.9" ] \
  || err "Python $PYVER 太旧，需要 ≥ 3.9"
log "Python $PYVER OK"

# 2. 飞书 WS 出方向
log "检查飞书 WS 连通性 ..."
if command -v nc >/dev/null; then
  nc -zv -w 5 msg-frontier.feishu.cn 443 2>&1 \
    | grep -q -E 'succeeded|open' || err "无法连接 msg-frontier.feishu.cn:443"
elif command -v curl >/dev/null; then
  curl -fsI --max-time 5 https://open.feishu.cn/ >/dev/null \
    || err "无法访问 open.feishu.cn"
fi
log "飞书出方向 OK"

# 3. 装包
log "创建 venv: $INSTALL_DIR/.venv"
mkdir -p "$INSTALL_DIR"
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip

if [ -n "$WHEEL" ]; then
  [ -f "$WHEEL" ] || err "wheel 文件不存在：$WHEEL"
  log "从本地 wheel 安装: $WHEEL"
  "$INSTALL_DIR/.venv/bin/pip" install --quiet "$WHEEL"
elif [ -n "$PYPI_HOST" ]; then
  log "从内网 PyPI 安装: http://$PYPI_HOST:$PYPI_PORT/simple/"
  "$INSTALL_DIR/.venv/bin/pip" install --quiet \
    --index-url "http://$PYPI_HOST:$PYPI_PORT/simple/" \
    --trusted-host "$PYPI_HOST" \
    "feishu-relay-bot==3.0.1"
else
  err "需要设置 WHEEL=<path> 或 PYPI_HOST=<host>"
fi

VER=$("$INSTALL_DIR/.venv/bin/feishu-relay-bot" version | awk '{print $2}')
[ "$VER" = "3.0.1" ] || err "安装版本 $VER 非 3.0.1"
log "feishu-relay-bot $VER 安装成功"

# 4. 配置文件
log "准备配置文件 $CONFIG_DIR/config.yaml ..."
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
  cat > "$CONFIG_DIR/config.yaml" <<EOF
# Feishu Relay Worker v3 — 由 install.sh 生成
feishu:
  app_id: cli_REPLACE_ME
  app_secret: REPLACE_ME

mp:
  url: https://models-proxy.example.com
  api_key: ak-REPLACE_ME

chat_id: oc_REPLACE_ME

node_id: $NODE_ID
heartbeat_interval_s: 30

stream:
  flush_bytes: 1024
  flush_ms: 1000
  send_qps: 4.0

multipart_timeout_s: 180
EOF
  chmod 600 "$CONFIG_DIR/config.yaml"
  log "已写入模板，你需要手动填实 feishu.app_secret / mp.api_key / chat_id 然后再 systemctl start"
else
  log "$CONFIG_DIR/config.yaml 已存在，跳过覆盖"
fi

# 5. systemd unit
log "写入 systemd unit ..."
cat > "/etc/systemd/system/$SERVICE_NAME.service" <<EOF
[Unit]
Description=Feishu Relay Bot (v3)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$INSTALL_DIR/.venv/bin/feishu-relay-bot run --config $CONFIG_DIR/config.yaml
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
log "systemd unit 安装完成（已 enable，但**没有 start**）"

cat <<EOF

================================================================
✅ Worker v3 安装完成

下一步：
  1) 编辑 $CONFIG_DIR/config.yaml，填实：
       feishu.app_secret
       mp.api_key
       chat_id
       node_id（当前: $NODE_ID）

  2) 启动：
       sudo systemctl start $SERVICE_NAME
       sudo journalctl -u $SERVICE_NAME -f

  3) 在 gateway 主机上确认注册：
       sudo journalctl -u llm-relay | grep "node=$NODE_ID"
================================================================
EOF
