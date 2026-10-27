"""MCP 方案设计（doc/mcp-design.html 第二版）的主流程 demo：各模块怎么分工。

    Atlas / Pod 里的 CLI ──主体 Key──▶ MCP 网关 ──网关凭据──▶ Higress ──后端凭据──▶ 业务系统
                                         │
                                         └──判权──▶ 用户中心

    依赖方向：atlas_server ──▶ atlas_engine ──▶ langchain / langgraph
              atlas_server ──▶ fastmcp / mcp（MCP 协议在 server，engine 不认识 MCP）

运行（用仓库的 .venv：langchain 1.4 + fastmcp + mcp 2.x）：
    .venv/bin/python demo/mcp-gateway/main.py
说明：demo/mcp-gateway/README.html（模块职责、时序图、实测输出）

    main.py             场景：起各个服务，按 ①~⑤ 演示
    usercenter.py       用户中心（mock）：数据对象登记、授权、按主体查可用服务
    gateway.py          MCP 网关这个服务 = 控制面 + 数据面 + 网关自己的库
    gateway_store.py      网关的库（mock）：注册表、工具元数据与复核结论、API Key、访问日志
    gateway_control.py    控制面：登记服务（接入检查）、拉元数据、复核、发 Key、授权；
                          给 Atlas 的内部接口
    gateway_proxy.py      数据面：认人 → 判权 → 复核 → 改写身份 → 换网关凭据转发
    higress.py          Higress（mock）：只认网关凭据；REST 转 MCP；保管后端凭据
    backends.py         业务系统（mock）：天气、工单 REST 接口，只认后端凭据
    atlas_server/       atlas-server ★ 不依赖 langchain / langgraph
      server.py           Agent、权限主体、编辑器、Native Run、ACP 建会话
      mcp_tools.py        MCP 接入：元数据缓存、按主体过滤、造 ToolSpec、会话池、ACP 下发
    atlas_engine/       atlas-engine：依赖 langchain / langgraph，不认识 MCP
      contracts.py        和 server 之间的契约：ToolSpec / ToolResult / ModelSpec / TurnResult
      tools.py            ToolSpec → LangChain 工具
      models.py           ModelSpec → 聊天模型（本例只有 mock：按规则发 tool_call）
      runner.py           run_turn：组装图、驱动"模型 ↔ 工具"循环
    check_layers.py     静态检查依赖方向
    pod_cli.py          ACP 会话 Pod 里的 CLI（mock）
    hops.py             给每一跳编号打印
"""

from __future__ import annotations

import os
import sys
import warnings

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 本机的服务不走代理：开着 HTTP(S)_PROXY 时，httpx / httpx2 会把发往 127.0.0.1 的请求也送进代理
os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(
    filter(
        None, [os.environ.get("NO_PROXY") or os.environ.get("no_proxy"), "127.0.0.1", "localhost"]
    )
)
warnings.filterwarnings("ignore", message=".*langchain.mcp.*")

import asyncio  # noqa: E402
import logging  # noqa: E402
import secrets  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from contextlib import ExitStack, contextmanager  # noqa: E402
from typing import Any  # noqa: E402

import backends  # noqa: E402
import higress  # noqa: E402
import pod_cli  # noqa: E402
import uvicorn  # noqa: E402
from atlas_server.mcp_tools import AtlasMcp  # noqa: E402
from atlas_server.server import Atlas  # noqa: E402
from gateway import Gateway  # noqa: E402
from hops import detail, hop, stage  # noqa: E402

import usercenter  # noqa: E402

ADMIN = "u-admin"  # MCP 管理员（注册服务的人 = 服务所有者）


@contextmanager
def background(app: Any) -> Iterator[str]:
    """在后台线程里起一个 ASGI 应用（含 lifespan），交出 http://127.0.0.1:<端口>。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.should_exit = True
        thread.join(timeout=5)


async def main() -> None:
    for name in ("mcp", "httpx", "httpx2", "fastmcp"):  # FastMCP 装了 rich 日志，HTTP 请求会刷屏
        logging.getLogger(name).setLevel(logging.WARNING)
    # 网关凭据只给 MCP 网关和 Higress；后端凭据只给 Higress 和业务系统
    gateway_credential = f"gw-{secrets.token_hex(8)}"
    backend_keys = {"weather": f"bk-{secrets.token_hex(4)}", "ticket": f"bk-{secrets.token_hex(4)}"}

    with ExitStack() as stack:
        uc = stack.enter_context(background(usercenter.app()))
        backend = stack.enter_context(background(backends.app(backend_keys)))
        higress_url = stack.enter_context(
            background(
                higress.app(
                    credential=gateway_credential, backend=backend, backend_keys=backend_keys
                )
            )
        )
        gw = Gateway(higress_url=higress_url, uc_url=uc, credential=gateway_credential)
        gateway_url = stack.enter_context(background(gw.app()))
        atlas = Atlas(AtlasMcp(gateway_url))

        stage("① MCP 管理员在 MCP 网关登记服务、复核、发 Key、授权")
        for name, review in (("weather", False), ("ticket", True), ("report", False)):
            await gw.control.register_service(
                name=name, title=name, upstream=name, review_required=review, operator=ADMIN
            )
        gw.control.approve("ticket", "create_ticket", operator=ADMIN)  # delete_ticket 留着待复核
        for subject in ("u-alice", "de-admin"):
            gw.control.issue_key(subject)
        for service, subject in (
            ("weather", "u-alice"),
            ("ticket", "u-alice"),
            ("ticket", "de-admin"),
        ):
            await gw.control.grant(operator=ADMIN, service=service, subject=subject)
        await atlas.mcp.refresh_metadata()

        stage("② 编辑器：按权限主体列出可选的 MCP 工具")
        await atlas.editor("trip", "u-alice")
        await atlas.editor("admin", "u-bob")

        stage("③ Native · 以人为权限：alice 用自己的「出行助理」")
        q = "上海今天下雨吗？下雨就建一张提醒带伞的工单"
        hop("用户 alice", q)
        hop("用户 alice", f"收到回复：{await atlas.run('trip', 'u-alice', q)}")

        stage("④ Native · 以 Agent 为权限：bob 用「行政助手」，按数字员工 de-admin 的权限")
        q = "会议室投影仪坏了，帮我建一张报修工单"
        hop("用户 bob", q)
        hop("用户 bob", f"收到回复：{await atlas.run('admin', 'u-bob', q)}")

        stage("⑤ ACP：alice 打开「命令行助手」，Pod 里的 CLI 自己连 MCP 网关")
        servers = await atlas.open_acp("cli", "u-alice")
        await pod_cli.run(servers, higress_found_in_cluster=higress_url)

        stage("MCP 网关的访问日志")
        for row in gw.store.access_log:
            if row["request"] in ("GET", "notifications/initialized"):
                continue
            who = row["subject"] or "-"
            detail(f"{who:<9} {row['service']:<8} {row['request']:<26} {row['result']}")


if __name__ == "__main__":
    asyncio.run(main())
