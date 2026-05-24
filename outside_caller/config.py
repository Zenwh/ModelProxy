"""
外网客户端配置。所有脚本共用。
"""
import os
import tempfile

# 飞书 app
_APP_ID = os.getenv("FEISHU_APP_ID", "")
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

# 轮询配置
POLL_INTERVAL_S = float(os.getenv("POLL_INTERVAL_S", "1.5"))
POLL_TIMEOUT_S = int(os.getenv("POLL_TIMEOUT_S", "240"))


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
