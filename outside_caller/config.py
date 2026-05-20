"""
外网客户端配置。所有脚本共用。
"""
import os

# 飞书 app（cli_a955f5aa04f81bda）
APP_ID = os.getenv("FEISHU_APP_ID", "cli_a955f5aa04f81bda")
APP_SECRET = os.getenv("FEISHU_APP_SECRET", "WgVfCkJcdggcJqkoJDVKB6YkL2JqoT16")

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
FEISHU_BASE = "https://open.feishu.cn"

# Model Proxy（内网）
MODELPROXY_BASE = os.getenv("MODELPROXY_BASE", "https://models-proxy.stepfun-inc.com").rstrip("/")
MODELPROXY_API_KEY = os.getenv("MODELPROXY_API_KEY", "ak-c9pttfhr2xoxrwuo4a7hvtd91h7zfedh")

# 目标 bot（阿月老师）
BOT_OPEN_ID = os.getenv("BOT_OPEN_ID", "ou_62bd50151cb45ff8fa60f2c9920ba17b")
CHAT_ID = os.getenv("CHAT_ID", "oc_1b306e7ee93a675a6bae0c5b46aa28c4")

# Relay 服务
RELAY_HOST = os.getenv("RELAY_HOST", "0.0.0.0")
RELAY_PORT = int(os.getenv("RELAY_PORT", "9100"))
RELAY_API_KEY = os.getenv("RELAY_API_KEY", "sk-feishu-relay-default")

# API Key 管理
API_KEYS_FILE = os.path.join(STATE_DIR, "api_keys.json")

# 访问日志
ACCESS_LOG_FILE = os.path.join(STATE_DIR, "access.log")

# 轮询配置
POLL_INTERVAL_S = float(os.getenv("POLL_INTERVAL_S", "1.5"))
POLL_TIMEOUT_S = int(os.getenv("POLL_TIMEOUT_S", "240"))
