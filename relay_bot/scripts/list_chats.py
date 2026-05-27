#!/usr/bin/env python3
"""
列出一个飞书 bot app 当前在的所有 chat（群 + p2p），含 chat_id / name / 类型 / 成员数。

用法：
  CFG=/etc/relay-bot/config.yaml python3 list_chats.py
  # 或直接传 app 凭据：
  APP_ID=cli_xxx APP_SECRET=xxx python3 list_chats.py

输出（按 chat_id 排序）：
  oc_xxx  group  20  "v3-relay 中继群-1"
  oc_yyy  group  3   "v3-relay 中继群-2"
  oc_zzz  p2p    2   "(p2p with some user)"
"""
import os, sys, json, urllib.request, urllib.parse

CFG = os.environ.get("CFG", "/etc/relay-bot/config.yaml")
APP_ID = os.environ.get("APP_ID")
APP_SECRET = os.environ.get("APP_SECRET")

if not (APP_ID and APP_SECRET):
    # 简陋的 yaml 解析（避免依赖 PyYAML）
    try:
        with open(CFG) as f:
            for line in f:
                line = line.strip()
                if line.startswith("app_id:"):
                    APP_ID = line.split(":", 1)[1].strip()
                elif line.startswith("app_secret:"):
                    APP_SECRET = line.split(":", 1)[1].strip()
    except FileNotFoundError:
        sys.exit(f"找不到 {CFG}，请用 APP_ID/APP_SECRET 环境变量或指定 CFG=")
    if not (APP_ID and APP_SECRET):
        sys.exit(f"在 {CFG} 里没解析出 app_id / app_secret")


def _post(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _get(url, token, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


# 1. 拿 tenant_access_token
tok = _post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": APP_ID, "app_secret": APP_SECRET})
if tok.get("code") != 0:
    sys.exit(f"换 token 失败: {tok}")
TAT = tok["tenant_access_token"]

# 2. 分页拉所有 chats（bot 视角：返回 bot 是成员的所有 chat）
chats, page = [], ""
while True:
    p = {"page_size": 100}
    if page:
        p["page_token"] = page
    d = _get("https://open.feishu.cn/open-apis/im/v1/chats", TAT, p)
    if d.get("code") != 0:
        sys.exit(f"列 chats 失败: {d}")
    chats += (d.get("data") or {}).get("items", []) or []
    page = (d.get("data") or {}).get("page_token", "")
    if not page:
        break

# 3. 排序输出
chats.sort(key=lambda c: c.get("chat_id", ""))
print(f"# app={APP_ID}  共 {len(chats)} 个 chat\n")
print(f"{'CHAT_ID':<60} {'MODE':<8} {'NAME'}")
print("-" * 100)
for c in chats:
    cid = c.get("chat_id") or ""
    mode = c.get("chat_mode") or "?"
    name = (c.get("name") or "").strip() or "(no name)"
    desc = (c.get("description") or "").strip()
    line = f"{cid:<60} {mode:<8} {name}"
    if desc:
        line += f"  -- {desc}"
    print(line)

print("\n提示：复制 oc_xxx 到 worker 的 config.yaml 的 chat_id 字段即可。")
print("如果这里没列出某个群，先在飞书客户端把 bot 拉进群再重新跑。")
