#!/usr/bin/env python3
"""
一键启动 Feishu Relay 全套服务
================================

启动两个组件：
  1. relay_server  — OpenAI-compatible API (端口 9100)
  2. bot_claude_ws — websocket 连飞书，收消息调 Claude

用法：
  python run_relay.py              # 启动全套
  python run_relay.py --relay-only # 只启动 relay（bot 在其他机器上跑）
  python run_relay.py --bot-only   # 只启动 bot

停止：Ctrl+C 会同时杀掉所有子进程。
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time


def find_python():
    """找到 .venv-mock 里的 python。"""
    here = os.path.dirname(os.path.abspath(__file__))
    venv = os.path.join(here, ".venv-mock", "bin", "python")
    if os.path.exists(venv):
        return venv
    return sys.executable


def main():
    parser = argparse.ArgumentParser(description="启动 Feishu Relay 服务")
    parser.add_argument("--relay-only", action="store_true", help="只启动 relay server")
    parser.add_argument("--bot-only", action="store_true", help="只启动 bot_claude_ws")
    parser.add_argument("--port", type=int, default=9100, help="relay 端口 (默认 9100)")
    args = parser.parse_args()

    python = find_python()
    here = os.path.dirname(os.path.abspath(__file__))
    procs = []

    def cleanup(sig=None, frame=None):
        print("\n正在停止所有服务 ...")
        for name, p in procs:
            if p.poll() is None:
                print(f"  停止 {name} (PID {p.pid})")
                p.terminate()
        for name, p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    # ---- 启动 bot_claude_ws ----
    if not args.relay_only:
        bot_script = os.path.join(here, "feishu_mock", "examples", "bot_gateway_ws.py")
        if not os.path.exists(bot_script):
            print(f"⚠️  找不到 {bot_script}，跳过 bot")
        else:
            print(f"🤖 启动 bot_gateway_ws ...")
            p = subprocess.Popen(
                [python, bot_script],
                cwd=here,
            )
            procs.append(("bot_gateway_ws", p))
            time.sleep(2)  # 等 websocket 连接

    # ---- 启动 relay_server ----
    if not args.bot_only:
        print(f"🚀 启动 relay_server (端口 {args.port}) ...")
        env = os.environ.copy()
        env["RELAY_PORT"] = str(args.port)
        p = subprocess.Popen(
            [
                python, "-m", "uvicorn",
                "outside_caller.relay_server:app",
                "--host", "0.0.0.0",
                "--port", str(args.port),
            ],
            cwd=here,
            env=env,
        )
        procs.append(("relay_server", p))

    if not procs:
        print("没有服务被启动。")
        return

    print()
    print("=" * 50)
    print("✅ 服务已启动：")
    for name, p in procs:
        print(f"   {name:20s} PID={p.pid}")
    print()
    print(f"   API:    http://localhost:{args.port}/v1/chat/completions")
    print(f"   Health: http://localhost:{args.port}/health")
    print(f"   Models: http://localhost:{args.port}/v1/models")
    print()
    print("按 Ctrl+C 停止所有服务")
    print("=" * 50)

    # 等任一进程退出
    while True:
        for name, p in procs:
            ret = p.poll()
            if ret is not None:
                print(f"\n⚠️  {name} 已退出 (code={ret})，停止所有服务")
                cleanup()
        time.sleep(1)


if __name__ == "__main__":
    main()
