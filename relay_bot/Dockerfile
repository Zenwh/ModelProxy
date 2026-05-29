# Feishu Relay Worker v3
#
# Build:
#   cd relay_bot
#   docker build -t feishu-relay-bot:3.0.1 .
#
# Or via docker compose from repo root:
#   docker compose up -d worker-local --build
#
# Runtime config is mounted at /etc/relay-bot/config.yaml；不要 COPY 进镜像。

FROM python:3.11-slim

# 1) 系统层精简：不装编译器，只装运行时所需
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 2) 装 wheel（构建上下文必须是 relay_bot/，否则找不到 dist/）
COPY dist/feishu_relay_bot-3.0.1-py3-none-any.whl /tmp/
RUN pip install /tmp/feishu_relay_bot-3.0.1-py3-none-any.whl \
    && rm /tmp/feishu_relay_bot-3.0.1-py3-none-any.whl

# 3) 非 root 跑（容器内不需要任何特权）
RUN useradd --system --uid 10001 --shell /usr/sbin/nologin relay \
    && mkdir -p /etc/relay-bot \
    && chown -R relay:relay /etc/relay-bot /app
USER relay

# 4) 健康检查：检测 PID 1 是否还在跑 feishu-relay-bot（worker 没 HTTP 端口）
# 用 stdlib 而不是 pgrep / procps，避免镜像膨胀
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python3 -c "import sys; sys.exit(0 if b'feishu-relay-bot' in open('/proc/1/cmdline','rb').read() else 1)"

# 5) 默认入口；config.yaml 通过 volume 挂载进来
ENTRYPOINT ["feishu-relay-bot"]
CMD ["run", "--config", "/etc/relay-bot/config.yaml"]
