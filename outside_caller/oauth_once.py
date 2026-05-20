"""
一次性 OAuth 授权：
  1) 开浏览器到飞书授权页
  2) 本机起一个临时 HTTP server 接收 code
  3) 用 code 换 user_access_token，存到 ~/.feishu_outside_caller/tokens_<app_id>.json

之后 talk.py 直接读这个文件即可。

用法：
  python -m outside_caller.oauth_once
"""
from __future__ import annotations

import http.server
import json
import os
import socketserver
import threading
import urllib.parse
import webbrowser

import httpx

from . import config


# ---- 一个最小的回调 server ----------------------------------------------------

_received: dict = {}


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):  # 静音 access log
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        qs = urllib.parse.parse_qs(parsed.query)
        _received["code"] = (qs.get("code") or [""])[0]
        _received["state"] = (qs.get("state") or [""])[0]
        body = (
            "<html><body style='font-family:sans-serif;padding:40px'>"
            "<h2>✅ 已收到授权码</h2>"
            "<p>可以关掉这个标签页回到终端了。</p>"
            "</body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _wait_for_code(timeout_s: int = 300) -> str:
    httpd = socketserver.TCPServer(("", config.REDIRECT_PORT), _Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        import time
        elapsed = 0
        while elapsed < timeout_s and "code" not in _received:
            time.sleep(0.2)
            elapsed += 0.2
        if "code" not in _received:
            raise TimeoutError("等了 5 分钟还没拿到 code，放弃")
        return _received["code"]
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---- 真正的 OAuth 流程 -------------------------------------------------------


def _get_app_access_token() -> str:
    """app_access_token 是换 user_access_token 的前置。"""
    r = httpx.post(
        f"{config.FEISHU_BASE}/open-apis/auth/v3/app_access_token/internal",
        json={"app_id": config.APP_ID, "app_secret": config.APP_SECRET},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"取 app_access_token 失败: {data}")
    return data["app_access_token"]


def _exchange_code(code: str) -> dict:
    """新版 v2 接口：用 client_id + client_secret + code 直接换 token，不再需要 app_access_token。"""
    r = httpx.post(
        f"{config.FEISHU_BASE}/open-apis/authen/v2/oauth/token",
        headers={"Content-Type": "application/json"},
        json={
            "grant_type": "authorization_code",
            "client_id": config.APP_ID,
            "client_secret": config.APP_SECRET,
            "code": code,
            "redirect_uri": config.REDIRECT_URI,
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def main():
    os.makedirs(config.STATE_DIR, exist_ok=True)

    # 1) 拼授权 URL（新版：passport.feishu.cn，显式声明 scope）
    state = os.urandom(8).hex()
    params = {
        "client_id": config.APP_ID,
        "redirect_uri": config.REDIRECT_URI,
        "response_type": "code",
        "state": state,
        "scope": "im:message im:message:readonly im:chat im:chat:readonly offline_access",
    }
    auth_url = (
        "https://passport.feishu.cn/suite/passport/oauth/authorize?"
        + urllib.parse.urlencode(params)
    )
    print("打开浏览器去授权（如果没自动开就手动点这条 URL）：")
    print(auth_url)
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    # 2) 等回调
    print(f"\n监听 {config.REDIRECT_URI} ...")
    code = _wait_for_code()
    print(f"拿到 code，长度 {len(code)}")

    # 3) 用 code 换 user_access_token（v2 接口）
    print("用 v2 接口换 user_access_token ...")
    res = _exchange_code(code)
    if res.get("code") != 0:
        raise SystemExit(f"换 token 失败: {res}")

    data = res.get("data") or res  # v2 可能直接平铺
    out = {
        "user_access_token": data.get("access_token"),
        "refresh_token": data.get("refresh_token", ""),
        "refresh_token_expires_in": data.get("refresh_token_expires_in", 0),
        "open_id": data.get("open_id"),
        "user_id": data.get("user_id"),
        "name": data.get("name", ""),
        "expires_in": data.get("expires_in"),
        "scope": data.get("scope", ""),
    }
    with open(config.TOKEN_FILE, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\n✅ 已保存到 {config.TOKEN_FILE}")
    has_refresh = bool(out["refresh_token"])
    print(f"   refresh_token: {'✅ 有' if has_refresh else '❌ 无（需要 offline_access 权限）'}")
    print(f"   scope: {out['scope']}")
    print(f"   expires_in: {out['expires_in']}s")


if __name__ == "__main__":
    main()
