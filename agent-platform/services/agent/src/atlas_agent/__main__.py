"""atlas-server 命令行（服务本身由 uvicorn 启动：atlas_agent.main:create_app）。

  python -m atlas_agent register-uc --account admin   把 Atlas 登记到用户中心（权限设计 §6）
  python -m atlas_agent reassign-threads map.json      TeamFlow 的存量会话改归真实的人（§14）
  python -m atlas_agent backfill-agent-identity        存量 Agent 补数字员工、登记 AtlasAgent
                                                       （数字员工设计 §12，迁移 0023 之后）
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m atlas_agent")
    sub = parser.add_subparsers(dest="command", required=True)
    reg = sub.add_parser("register-uc", help="把操作码、菜单、角色、数据编码登记到用户中心")
    reg.add_argument("--account", required=True, help="用户中心管理员账号")
    reg.add_argument("--uc-url", default=None)
    reg.add_argument(
        "--tenant", default=None, help="管理员所在的企业代码（用户中心有多个租户时必填）"
    )
    rt = sub.add_parser(
        "reassign-threads",
        help="按映射 {会话 id: 用户 id} 改写会话所有者（TeamFlow export-thread-owners 导出）",
    )
    rt.add_argument("map", help="JSON 文件；- 表示从标准输入读")
    bf = sub.add_parser(
        "backfill-agent-identity",
        help="存量 Agent 补建数字员工、在用户中心登记（创建者 OWNER，已启用的根部门含下级 READ）",
    )
    bf.add_argument("--uc-url", default=None)
    bf.add_argument("--dry-run", action="store_true", help="只统计，不写")
    args = parser.parse_args(argv)

    if args.command == "register-uc":
        from .auth.register import register
        from .config import get_settings

        password = os.environ.get("UC_ADMIN_PASSWORD") or getpass.getpass(
            f"用户中心账号 {args.account} 的密码："
        )
        register(
            args.uc_url or get_settings().uc_base_url, args.account, password, tenant=args.tenant
        )
    elif args.command == "reassign-threads":
        import asyncio
        import json
        from pathlib import Path

        from .auth.reassign import reassign_threads

        raw = sys.stdin.read() if args.map == "-" else Path(args.map).read_text("utf-8")
        print(asyncio.run(reassign_threads(json.loads(raw))))
    elif args.command == "backfill-agent-identity":
        import asyncio
        import json

        from .auth.backfill import backfill_agent_identity
        from .auth.uc import UCClient
        from .config import get_settings

        settings = get_settings()
        if args.uc_url:
            settings = settings.model_copy(update={"uc_base_url": args.uc_url})

        async def _run() -> dict[str, object]:
            uc = UCClient(settings)
            try:
                return await backfill_agent_identity(uc, dry_run=args.dry_run)
            finally:
                await uc.aclose()

        print(json.dumps(asyncio.run(_run()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
