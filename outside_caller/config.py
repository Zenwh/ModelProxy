"""
外网客户端配置。所有脚本共用。
"""
import os
import tempfile

# 飞书 app（默认 app: cli_a955f5aa04f81bda）
# APP_ID/APP_SECRET 必须通过环境变量注入。生产 systemd unit 用 EnvironmentFile 配置，
# 不再保留硬编码 fallback（旧 fallback "WgVfCkJ..." 已废弃，请使用环境变量）。
# 缺失任一项立即 raise，避免运行时拿到错误身份。
_APP_ID = os.getenv("FEISHU_APP_ID", "cli_a955f5aa04f81bda")
_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")

if not _APP_ID or not _APP_SECRET:
    raise RuntimeError(
        "FEISHU_APP_ID and FEISHU_APP_SECRET must be set as environment variables. "
        "Formerly-default fallback values were removed for security."
    )

APP_ID = _APP_ID
APP_SECRET = _APP_SECRET

# OAuth
REDIRECT_HOST = os.getenv("OAUTH_REDIRECT_HOST", "localhost")
REDIRECT_PORT = int(os.getenv("OAUTH_REDIRECT_PORT", "8766"))
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/callback"

# token / chat 缓存
STATE_DIR = os.path.expanduser(
    os.getenv("FEISHU_STATE_DIR", "~/.feishu_outside_caller")
)
TOKEN_FILE = os.path.join(STATE_DIR, f"tokens_{APP_ID}.json")

# 飞书开放平台
FEISHU_BASE = os.getenv("FEISHU_BASE", "https://open.feishu.cn").rstrip("/")

# Model Proxy（内网）
_MODELPROXY_API_KEY = os.getenv("MODELPROXY_API_KEY", "")
if not _MODELPROXY_API_KEY:
    raise RuntimeError(
        "MODELPROXY_API_KEY must be set as environment variable. "
        "Formerly-default fallback value was removed for security."
    )
MODELPROXY_BASE = os.getenv("MODELPROXY_BASE", "https://models-proxy.stepfun-inc.com").rstrip("/")
MODELPROXY_API_KEY = _MODELPROXY_API_KEY

# 目标 bot（阿月老师）
BOT_OPEN_ID = os.getenv("BOT_OPEN_ID", "")
CHAT_ID = os.getenv("CHAT_ID", "")

# Relay 服务
RELAY_HOST = os.getenv("RELAY_HOST", "0.0.0.0")
RELAY_PORT = int(os.getenv("RELAY_PORT", "9100"))
RELAY_API_KEY = os.getenv("RELAY_API_KEY", "sk-feishu-relay-default")

# Agent heartbeat auth secret
_AGENT_SECRET = os.getenv("AGENT_SECRET", "")
if not _AGENT_SECRET:
    raise RuntimeError(
        "AGENT_SECRET must be set as environment variable. "
        "This secret is shared between the relay server and bot nodes for heartbeat/offline authentication."
    )
AGENT_SECRET = _AGENT_SECRET

# API Key 管理
API_KEYS_FILE = os.path.join(STATE_DIR, "api_keys.json")

# 访问日志
ACCESS_LOG_FILE = os.path.join(STATE_DIR, "access.log")

# 轮询配置（v3：流式需要更短间隔，0.3s 平均 0.15s 首 token）
POLL_INTERVAL_S = float(os.getenv("POLL_INTERVAL_S", "0.3"))
POLL_TIMEOUT_S = int(os.getenv("POLL_TIMEOUT_S", "240"))

# v3 协议参数
# - MAX_INPUT_TOKENS:        超过此 token 数触发 truncator 中间截断（保留 system + 尾部）
# - MULTIPART_CHUNK_BYTES:   分片大小；140KB 单消息限制 - envelope 余量
# - STREAM_BUFFER_FLUSH_*:   StreamEmitter 触发条件（worker 端用）
MAX_INPUT_TOKENS = int(os.getenv("MAX_INPUT_TOKENS", "1000000"))
MULTIPART_CHUNK_BYTES = int(os.getenv("MULTIPART_CHUNK_BYTES", "120000"))
# 上行分片发送的最大 QPS（每个 worker）；飞书 app msg API 名义 5 QPS，留余量
MULTIPART_SEND_QPS = float(os.getenv("MULTIPART_SEND_QPS", "4.0"))
STREAM_BUFFER_FLUSH_BYTES = int(os.getenv("STREAM_BUFFER_FLUSH_BYTES", "1024"))
STREAM_BUFFER_FLUSH_MS = int(os.getenv("STREAM_BUFFER_FLUSH_MS", "1000"))

# v0.4 cluster upgrade
# - BOOTSTRAP_TOKEN: 全局凭证，新节点 install.sh 时携带；empty 表示禁用 bootstrap 端点
# - CLUSTER_HEARTBEAT_INTERVAL_S: bootstrap 响应里下发给节点的默认心跳间隔
BOOTSTRAP_TOKEN = os.getenv("BOOTSTRAP_TOKEN", "")
CLUSTER_HEARTBEAT_INTERVAL_S = int(os.getenv("CLUSTER_HEARTBEAT_INTERVAL_S", "30"))


# ---- Atomic JSON write helper --------------------------------------------------

import json as _json


def atomic_write_json(path: str, data: dict) -> None:
    """
    Write *data* as JSON to *path* atomically.

    Strategy:
      1. Serialize to an in-memory string first.
      2. Write to a same-directory temp file via NamedTemporaryFile.
      3. os.replace() to the final destination — POSIX guarantees atomicity
         for renames within the same filesystem.

    If the process is killed between steps 2 and 3 the stale tempfile is
    left behind but the original file is untouched.
    """
    payload = _json.dumps(data, indent=2, ensure_ascii=False)
    target_dir = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=target_dir, suffix=".tmp")
    try:
        os.write(fd, payload.encode("utf-8"))
        os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
