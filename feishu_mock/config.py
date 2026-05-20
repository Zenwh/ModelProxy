"""
配置：可通过环境变量覆盖。
"""
import os


# 被测服务（OpenClaw / ModelProxy Bot 处理器）的 webhook 地址
# 形如 http://localhost:9000/feishu/webhook
TARGET_WEBHOOK_URL = os.getenv(
    "TARGET_WEBHOOK_URL",
    "http://localhost:9000/feishu/webhook",
)

# 飞书应用的安全配置（要和被测服务里的配置一致，才能通过签名/Token 校验）
VERIFICATION_TOKEN = os.getenv("FEISHU_VERIFICATION_TOKEN", "mock-verification-token")
ENCRYPT_KEY = os.getenv("FEISHU_ENCRYPT_KEY", "")  # 留空表示不加密
APP_ID = os.getenv("FEISHU_APP_ID", "cli_mock_app_id")
APP_SECRET = os.getenv("FEISHU_APP_SECRET", "mock_app_secret")
TENANT_KEY = os.getenv("FEISHU_TENANT_KEY", "mock_tenant_key")

# Mock 服务自身监听端口
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# 转发 webhook 的超时
WEBHOOK_TIMEOUT = float(os.getenv("WEBHOOK_TIMEOUT", "10"))
