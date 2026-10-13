"""atlas-server 命令行（服务本身由 uvicorn 启动：atlas_server.main:create_app）。

  python -m atlas_server register-uc --account admin   把 Atlas 登记到用户中心（权限设计 §6）
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m atlas_server")
    sub = parser.add_subparsers(dest="command", required=True)
    reg = sub.add_parser("register-uc", help="把操作码、菜单、角色、数据编码登记到用户中心")
    reg.add_argument("--account", required=True, help="用户中心管理员账号")
    reg.add_argument("--uc-url", default=None)
    reg.add_argument("--rotate", action="store_true", help="应用已存在时轮换 Secret")
    args = parser.parse_args(argv)

    if args.command == "register-uc":
        from .auth.register import register
        from .config import get_settings

        password = os.environ.get("UC_ADMIN_PASSWORD") or getpass.getpass(
            f"用户中心账号 {args.account} 的密码："
        )
        out = register(
            args.uc_url or get_settings().uc_base_url, args.account, password, rotate=args.rotate
        )
        print(f"\nUC_APP_KEY={out['app_key']}")
        if out["app_secret"]:
            print(f"UC_APP_SECRET={out['app_secret']}")
            print("（Secret 只显示这一次，写入 .env）")
        else:
            print("（应用已存在，Secret 未变；需要新 Secret 时加 --rotate）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
