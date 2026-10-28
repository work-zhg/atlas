"""atlas-server 命令行（服务本身由 uvicorn 启动：atlas_agent.main:create_app）。

  python -m atlas_agent register-uc --account admin   把 Atlas 登记到用户中心（权限设计 §6）
  python -m atlas_agent reassign-threads map.json      TeamFlow 的存量会话改归真实的人（§14）
  python -m atlas_agent backfill-agent-identity        存量 Agent 补数字员工、登记 AtlasAgent
                                                       （数字员工设计 §12，迁移 0023 之后）
  python -m atlas_agent register-agent-keys             存量智能体在模型管理平台上补建客户端 Key
                                                       （「待分配」；已有的不动），
                                                       每行打印 id 与状态
  python -m atlas_agent service-model list              平台服务（会话标题、记忆补记）用哪个模型
  python -m atlas_agent service-model set title --client-type Service --client-code session-title \
                        --model deepseek-flash          配置（要先在模型管理平台建好 Key、分配模型）
  python -m atlas_agent service-model clear title
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
    sub.add_parser(
        "register-agent-keys",
        help="存量智能体在模型管理平台上补建客户端 Key（待分配；已有的不动）",
    )
    sm = sub.add_parser(
        "service-model",
        help="平台服务（会话标题、记忆补记、自动模式判断器）用模型管理平台上的哪把 Key、哪个模型",
    ).add_subparsers(dest="action", required=True)
    sm.add_parser("list")
    sm_set = sm.add_parser("set")
    sm_set.add_argument("purpose", help="title | memory_backfill | approval_judge")
    sm_set.add_argument("--client-type", required=True)
    sm_set.add_argument("--client-code", required=True)
    sm_set.add_argument("--model", required=True)
    sm.add_parser("clear").add_argument("purpose")
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
    elif args.command == "register-agent-keys":
        import asyncio

        from sqlalchemy import select

        from .config import get_settings
        from .db.models import Agent
        from .db.session import get_engine, get_sessionmaker
        from .providers.llm.model_platform import agent_key, uses_model_platform

        async def _register() -> int:
            settings = get_settings()
            if not uses_model_platform(settings):
                print("✗ 没有接入模型管理平台（MODEL_PLATFORM_URL）", file=sys.stderr)
                return 1
            try:
                async with get_sessionmaker()() as session:
                    live = select(Agent).where(Agent.status != "archived")
                    rows = list(await session.scalars(live))
            finally:
                await get_engine().dispose()
            for agent in rows:
                key = await agent_key(settings, agent.id, agent.name)
                print(f"{agent.id}\t{key.status}\t{agent.name}")
            return 0

        return asyncio.run(_register())
    elif args.command == "service-model":
        import asyncio

        from .config import get_settings
        from .db.session import get_engine, get_sessionmaker
        from .services.service_models import ServiceModelService

        async def _service_model() -> None:
            try:
                async with get_sessionmaker()() as session:
                    service = ServiceModelService(session, get_settings())
                    if args.action == "set":
                        await service.set(
                            args.purpose,
                            client_type=args.client_type,
                            client_code=args.client_code,
                            model=args.model,
                            actor=None,
                        )
                    elif args.action == "clear":
                        await service.clear(args.purpose)
                    await session.commit()
                    for v in await service.list():
                        where = f"{v.client_type}:{v.client_code} / {v.model}" if v.model else "—"
                        state = "✓" if v.ready else f"✗ {v.problem}"
                        print(f"{v.name}（{v.purpose}）  {where}  {state}")
            finally:
                await get_engine().dispose()

        asyncio.run(_service_model())
    return 0


if __name__ == "__main__":
    sys.exit(main())
