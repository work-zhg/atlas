"""Atlas（对应 atlas-server）：Agent 配置、权限主体、编辑器、Native Run、ACP 建会话。

    权限主体   以人为权限（私人助理）= 当前使用者；以 Agent 为权限 = 这个 Agent 的数字员工。
              选工具、装配过滤、调用带的 Key 都用同一个主体，下游不需要知道权限类型
    editor     编辑器的工具列表（按主体）
    run        Native：装配（mcp_tools）→ ToolSpec + ModelSpec 交给 engine 跑 → Run 结束关会话池
    open_acp   ACP：同样的过滤，把下发配置交给 Pod 里的 CLI（pod_cli）

Agent 放在内存里，代替 Atlas 的库。

★ 不 import LangChain：模型由 engine 按 ModelSpec 建；engine 交回的是 TurnResult。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from atlas_engine import ModelSpec, run_turn
from hops import detail, hop

from atlas_server.mcp_tools import AtlasMcp

MOCK_MODEL = ModelSpec(provider="mock", name="scripted")


@dataclass(frozen=True)
class Agent:
    id: str
    name: str
    kind: Literal["native", "acp"]
    auth_subject: Literal["user", "agent"]  # 以人为权限 / 以 Agent 为权限
    tool_names: tuple[str, ...]
    digital_employee: str | None = None  # 以 Agent 为权限时的数字员工
    model: ModelSpec = MOCK_MODEL


AGENTS = {
    a.id: a
    for a in [
        Agent(
            "trip",
            "出行助理",
            "native",
            "user",
            ("mcp:weather:get_weather", "mcp:ticket:create_ticket", "mcp:ticket:delete_ticket"),
        ),
        Agent(
            "admin",
            "行政助手",
            "native",
            "agent",
            ("mcp:weather:get_weather", "mcp:ticket:create_ticket"),
            digital_employee="de-admin",
        ),
        Agent("cli", "命令行助手", "acp", "user", ("mcp:weather:*", "mcp:ticket:*")),
    ]
}


class Atlas:
    def __init__(self, mcp: AtlasMcp) -> None:
        self.mcp = mcp

    def subject_of(self, agent: Agent, user: str) -> str:
        if agent.auth_subject == "agent":
            assert agent.digital_employee is not None
            return agent.digital_employee
        return user

    def _describe(self, agent: Agent, user: str) -> str:
        kind = "以人为权限" if agent.auth_subject == "user" else "以 Agent 为权限"
        subject = self.subject_of(agent, user)
        return f"「{agent.name}」（{agent.kind}，{kind}），使用者 {user} → 权限主体 {subject}"

    async def editor(self, agent_id: str, user: str) -> None:
        agent = AGENTS[agent_id]
        subject = self.subject_of(agent, user)
        hop("Atlas", f"编辑器打开{self._describe(agent, user)}")
        listed, hidden = await self.mcp.editor_tools(subject)
        detail(f"工具 · MCP 服务按 {subject} 的权限列出")
        for service, tools in listed.items():
            detail(f"  ☐ {service:<8} {' · '.join(tools)}")
        if hidden:
            detail(f"  另有 {hidden} 个 MCP 服务 {subject} 没有权限")

    async def run(self, agent_id: str, user: str, message: str) -> str:
        agent = AGENTS[agent_id]
        subject, run_id = self.subject_of(agent, user), f"run-{uuid.uuid4().hex[:6]}"
        hop("Atlas", f"{run_id}：{self._describe(agent, user)}")
        asm = await self.mcp.assemble(
            list(agent.tool_names), subject=subject, user=user, run_id=run_id
        )
        for notice in asm.notices:
            hop("Atlas", f"对话里提示：{notice}")
        hop(
            "Atlas",
            f"装配完成：{[t.name for t in asm.tools]}（读元数据缓存造 ToolSpec，没连 MCP）"
            f" → 连同 ModelSpec({agent.model.provider}) 交给 engine",
        )
        try:
            result = await run_turn(agent.model, asm.tools, message)
        finally:
            await asm.pool.aclose()
        return result.answer

    async def open_acp(self, agent_id: str, user: str) -> list[dict[str, object]]:
        agent = AGENTS[agent_id]
        subject = self.subject_of(agent, user)
        hop("Atlas", f"建 ACP 会话：{self._describe(agent, user)}")
        servers, notices = await self.mcp.acp_servers(
            list(agent.tool_names), subject=subject, user=user
        )
        for notice in notices:
            hop("Atlas", f"提示：{notice}")
        hop(
            "Atlas",
            f"session/new 下发 mcpServers：{[s['url'] for s in servers]}（带主体 Key 和 X-Uid）",
        )
        return servers
