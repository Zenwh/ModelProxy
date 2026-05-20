"""
API Key 命令行管理工具。

用法：
  python -m outside_caller.keys create "用户名"           # 创建普通 key
  python -m outside_caller.keys create "admin" --admin     # 创建 admin key
  python -m outside_caller.keys list                       # 列出所有 key
  python -m outside_caller.keys revoke sk-relay-xxx        # 禁用 key
  python -m outside_caller.keys enable sk-relay-xxx        # 重新启用 key
  python -m outside_caller.keys delete sk-relay-xxx        # 永久删除 key
"""
from __future__ import annotations

import argparse
import sys

from .api_keys import APIKeyManager
from . import config


def _mask_key(key: str) -> str:
    """显示 key 的前 12 位和最后 4 位，中间用 *** 代替。"""
    if len(key) <= 16:
        return key
    return key[:12] + "***" + key[-4:]


def cmd_create(args):
    mgr = APIKeyManager()
    info = mgr.create_key(args.name, is_admin=args.admin)
    print(f"\n✅ 已创建 API Key:")
    print(f"   name:  {info.name}")
    print(f"   key:   {info.key}")
    print(f"   admin: {'是' if info.is_admin else '否'}")
    print(f"\n⚠️  请保存好这个 key，之后无法再完整显示。")
    print(f"\n使用方式:")
    print(f'   curl -H "Authorization: Bearer {info.key}" ...')


def cmd_list(args):
    mgr = APIKeyManager()
    keys = mgr.list_keys()
    if not keys:
        print("没有 API Key。用 create 命令创建一个。")
        return

    print(f"\n共 {len(keys)} 个 API Key:\n")
    print(f"  {'状态':<6} {'名称':<16} {'Key':<28} {'Admin':<6} {'创建时间'}")
    print(f"  {'─'*6} {'─'*16} {'─'*28} {'─'*6} {'─'*20}")
    for k in keys:
        status = "✅" if k.enabled else "❌"
        print(
            f"  {status:<6} {k.name:<16} {_mask_key(k.key):<28} "
            f"{'是' if k.is_admin else '否':<6} {k.created_at}"
        )
    print()


def cmd_revoke(args):
    mgr = APIKeyManager()
    if mgr.revoke_key(args.key):
        print(f"✅ 已禁用 key: {_mask_key(args.key)}")
    else:
        print(f"❌ 未找到 key: {args.key}")
        sys.exit(1)


def cmd_enable(args):
    mgr = APIKeyManager()
    if mgr.enable_key(args.key):
        print(f"✅ 已启用 key: {_mask_key(args.key)}")
    else:
        print(f"❌ 未找到 key: {args.key}")
        sys.exit(1)


def cmd_delete(args):
    mgr = APIKeyManager()
    if mgr.delete_key(args.key):
        print(f"✅ 已删除 key: {_mask_key(args.key)}")
    else:
        print(f"❌ 未找到 key: {args.key}")
        sys.exit(1)


def cmd_set_limit(args):
    mgr = APIKeyManager()
    ok = mgr.set_limits(
        args.key,
        rpm_limit=args.rpm,
        daily_token_limit=args.daily_tokens,
        clear_rpm=args.clear_rpm,
        clear_daily=args.clear_daily,
    )
    if not ok:
        print(f"❌ 未找到 key: {args.key}")
        sys.exit(1)
    info = mgr._keys[args.key]
    print(f"✅ 已更新 key {_mask_key(args.key)} 的限额：")
    print(f"   RPM:          {info.rpm_limit if info.rpm_limit else '无限制'}")
    print(f"   Daily Tokens: {info.daily_token_limit if info.daily_token_limit else '无限制'}")


def main():
    parser = argparse.ArgumentParser(
        prog="python -m outside_caller.keys",
        description="Feishu Relay API Key 管理",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # create
    p_create = sub.add_parser("create", help="创建新 API Key")
    p_create.add_argument("name", help="Key 的名称/用途标识")
    p_create.add_argument("--admin", action="store_true", help="设为 admin key")
    p_create.set_defaults(func=cmd_create)

    # list
    p_list = sub.add_parser("list", help="列出所有 API Key")
    p_list.set_defaults(func=cmd_list)

    # revoke
    p_revoke = sub.add_parser("revoke", help="禁用 API Key")
    p_revoke.add_argument("key", help="要禁用的 key")
    p_revoke.set_defaults(func=cmd_revoke)

    # enable
    p_enable = sub.add_parser("enable", help="重新启用 API Key")
    p_enable.add_argument("key", help="要启用的 key")
    p_enable.set_defaults(func=cmd_enable)

    # delete
    p_delete = sub.add_parser("delete", help="永久删除 API Key")
    p_delete.add_argument("key", help="要删除的 key")
    p_delete.set_defaults(func=cmd_delete)

    # set-limit
    p_limit = sub.add_parser("set-limit", help="设置 key 的限额")
    p_limit.add_argument("key", help="要修改的 key")
    p_limit.add_argument("--rpm", type=int, help="每分钟请求数上限")
    p_limit.add_argument("--daily-tokens", type=int, help="每日 token 上限")
    p_limit.add_argument("--clear-rpm", action="store_true", help="清除 RPM 限制")
    p_limit.add_argument("--clear-daily", action="store_true", help="清除日 token 限制")
    p_limit.set_defaults(func=cmd_set_limit)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
