"""原生运行时 · 工具面（c4-model/agent/native-runtime/code-native-runtime-tools.html）。

这一段模型能调用哪些工具：MCP（多时改为延迟加载）、知识库检索、记忆工具、Shell 沙箱，加上
技能清单和会话文件。内置工具的知识在 domain/tool_registry.py；这里只为一段装上它们需要的东西。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID

import redis.asyncio as aioredis

from atlas_agent.domain.tool_registry import BASH_TOOL, FILESYSTEM_TOOL, reminders_for
from atlas_agent.errors import CapabilityUnavailable
from atlas_agent.executor.segment_data import Segment
from atlas_agent.providers.knowledge import make_knowledge_tool, require_client
from atlas_agent.sandbox.component import SandboxComponent
from atlas_agent.sandbox.exec import PollingPolicy, ShellSandbox
from atlas_agent.sandbox.native import WorkspaceRef

from ..config import Settings
from ..domain.events import EventType
from ..outbound import current_identity
from ..providers.filesystem.skill_copy import resolve_skills, skill_refs_for
from ..providers.mcp import (
    MCP_PREFIX,
    CallContext,
    GatewayUnavailable,
    KeyAuth,
    McpSessions,
    NativeMcpTools,
    make_mcp_catalog,
    mcp_agent_access,
)
from ..providers.mcp.gateway import forget_access, servers_in
from ..skills import skill_directory

if TYPE_CHECKING:
    from ..memory import MemoryTurn

logger = logging.getLogger(__name__)


class SandboxNotEnabled(CapabilityUnavailable):
    """勾了 Shell 但部署没开沙箱 —— 明确报错（§13.2），不静默少装 execute。"""


@dataclass
class ToolContribution:
    """工具面为一段交出的东西。"""

    #: 直接可见的外部工具（MCP、知识库、记忆）
    extra_tools: list[Any] = field(default_factory=list)
    #: MCP 工具多时改为延迟工具：模型平时只看到名字，用 tool_search 按需加载
    deferred_tools: list[Any] = field(default_factory=list)
    #: execute 背后的沙箱。None = 没勾 Shell
    sandbox: Any = None
    #: 技能清单（已拷进会话的技能，只渲染、不扫描）
    skills: list[Any] = field(default_factory=list)
    #: 勾选的工具给出的提醒来源（待办），交给上下文管理追加
    reminders: list[Any] = field(default_factory=list)
    #: 模型能用 read_file 读回来的存储（勾了文件工具时就是工作区）。大结果外置写进这里；
    #: None = 读不回来，外置只能截断
    readable_store: Any = None


class ToolSurface:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def contribute(
        self,
        segment: Segment,
        *,
        run_id: UUID,
        redis: aioredis.Redis,
        sandbox: SandboxComponent | None,
        workspace: Any,
        memory: MemoryTurn | None,
        notices: list[tuple[EventType, dict[str, Any]]] | None = None,
        closers: list[Callable[[], Awaitable[None]]] | None = None,
    ) -> ToolContribution:
        """装上这一段的工具。MCP 解析失败冒泡成 run.failed，而不是少装几个工具就默默跑（§13.2）。"""
        # MCP 工具在这里解析成 BaseTool —— engine 不认识 MCP 的传输与凭据
        # （§14）。工具定义读目录缓存，命中时装配不连网（MCP 详设 §05）。
        extra_tools = await self._mcp_tool_list(
            segment, run_id=run_id, redis=redis, notices=notices, closers=closers
        )
        # MCP 工具多时改为按需加载：定义不常驻上下文，模型用 tool_search 加载（上下文管理 §13）
        deferred = _deferred_mcp_tools(extra_tools, self._settings)

        # ★ 网页搜索不再是内置工具：它经 Higress 以 MCP 提供
        #   （mcp:serpapi:google_search），后端 key 只在网关里，走上面那条路。

        # 记忆工具：load_memory 随记忆挂上；勾了「记忆」的主会话再挂 save_memory / forget_memory
        if memory is not None:
            extra_tools = [*extra_tools, *memory.tools()]

        # 知识库（tool 模式）：库 id 闭包捕获，不进参数（doc/atlas-rag-design.html §10）
        knowledge = segment.spec.knowledge
        if knowledge.enabled and knowledge.mode == "tool":
            extra_tools = [
                *extra_tools,
                make_knowledge_tool(
                    require_client(self._settings),
                    knowledge,
                    names=None,
                    source_ref=f"thread:{segment.thread_id}/run:{run_id}",
                ),
            ]

        # Shell 沙箱（doc/sandbox-opensandbox-design.html）。★ 这里只构造客户端，不建沙箱 ——
        #   沙箱在第一次 execute 时才按需创建（多数原生会话用不到）。
        #   按工作区建：子智能体（workspace_thread_id = 父会话）与父会话共用一个。
        shell = None
        if BASH_TOOL in segment.spec.tool_names:
            if sandbox is None:
                raise SandboxNotEnabled(
                    "勾选了 Shell（bash）但服务端未开启沙箱（SANDBOX_ENABLED=false）"
                )
            settings = self._settings
            shell = ShellSandbox(
                native=sandbox.native,
                exec=sandbox.exec,
                ledger=sandbox.ledger,
                ws=WorkspaceRef(segment.thread.workspace_thread_id, segment.thread.created_by_uuid),
                run_id=str(run_id),
                spec=segment.spec.sandbox,
                polling=PollingPolicy(settings.sandbox_poll_min_s, settings.sandbox_poll_max_s),
                output_limit=settings.sandbox_output_limit_bytes,
                idle_s=settings.cli_tool_idle_timeout_s,
                stall_cpu_pct=settings.sandbox_stall_cpu_pct,
            )

        # 技能清单：描述取自技能应用；已下架的跳过并告诉用户（技能 / MCP 设计 §7.3）
        skill_metas, skipped = await resolve_skills(
            segment.spec.skills, skill_directory(self._settings)
        )
        for item in skipped:
            if notices is not None:
                notices.append((EventType.SKILL_SKIPPED, item))
            if workspace is not None:
                # ★ 删掉本会话里的副本：只从索引里拿掉的话，模型仍可能凭记忆去读
                await workspace.adelete(f"/skills/{item['slug']}", recursive=True)

        return ToolContribution(
            extra_tools=extra_tools,
            deferred_tools=deferred,
            sandbox=shell,
            # ★ 技能在**会话创建**时就已拷进 skills 前缀（见 skill_copy），
            #   这里只把清单渲染给中间件 —— 不扫描、不读文件。
            skills=skill_refs_for(skill_metas),
            reminders=reminders_for(set(segment.spec.tool_names)),
            readable_store=workspace if FILESYSTEM_TOOL in segment.spec.tool_names else None,
        )

    async def _mcp_tool_list(
        self,
        segment: Segment,
        *,
        run_id: UUID,
        redis: aioredis.Redis,
        notices: list[tuple[EventType, dict[str, Any]]] | None,
        closers: list[Callable[[], Awaitable[None]]] | None = None,
    ) -> list[Any]:
        """spec 里的 mcp:* → BaseTool。漂移 / 没授权 / Key 停用 / 工具停用的经 notices 告知用户。

        经 MCP 网关：问网关「这个智能体的 Key 能用哪些服务和工具、Key 是什么」（一次），
        用不了的跳过并提示；工具定义读缓存的元数据，装配不连 MCP 服务。
        同一个 Run 段对同一个服务复用一个会话，段结束时经 closers 关掉。
        ★ 子智能体的会话记在父智能体名下：用同一把 Key。
        """
        if not any(name.startswith(MCP_PREFIX) for name in segment.spec.tool_names):
            return []  # 不碰注册表：没用 MCP 的 agent 不该因为 MCP 网关不可用而失败
        s = self._settings
        ident = current_identity()
        ctx = CallContext(
            run_id=run_id,
            thread_id=segment.thread_id,
            user_id=segment.thread.created_by_uuid,
            # 执行器在 run 开始时设好的出站身份（以 Agent 为权限时是数字员工）
            auth_uid=ident.auth_uid if ident else None,
        )
        catalog = await make_mcp_catalog(s, redis)
        agent_id = segment.thread.agent_uuid
        try:
            access = await mcp_agent_access(
                s, agent_id, name=segment.spec.name, owner=segment.thread.created_by_uuid
            )
        except GatewayUnavailable as exc:
            # 网关不可用 = 这些服务此刻用不了：跳过并提示，对话照常（§6.7 ③）
            for server in sorted(servers_in(segment.spec.tool_names)):
                if notices is not None:
                    notices.append(
                        (
                            EventType.MCP_TOOL_SKIPPED,
                            {
                                "server": server,
                                "tools": [],
                                "reason": "unavailable",
                                "subject": str(agent_id),
                                "message": f"{exc}，本轮不装这个服务的工具",
                            },
                        )
                    )
            return []
        sessions = McpSessions(
            auth=KeyAuth(access.key) if access.key else None,
            fetch_key=self._key_fetcher(segment),
        )
        if closers is not None:
            closers.append(sessions.aclose)
        tools = NativeMcpTools(
            catalog,
            call_timeout_s=s.mcp_call_timeout_s,
            max_result_chars=s.mcp_max_result_chars,
        )
        return await tools.tools_for(
            segment.spec.tool_names,
            ctx,
            digests=segment.spec.mcp_tool_digests,
            drift_policy=segment.spec.mcp_drift_policy,
            reviews=catalog.reviews(),
            notices=notices,
            access=access,
            sessions=sessions,
        )

    def _key_fetcher(self, segment: Segment) -> Callable[[], Awaitable[str | None]]:
        """Key 被网关拒绝时重新取一次（可能刚被重置）。取不到（网关也不可用）当作没有新 Key。"""
        agent_id = segment.thread.agent_uuid

        async def fetch() -> str | None:
            forget_access(agent_id)
            try:
                access = await mcp_agent_access(
                    self._settings,
                    agent_id,
                    name=segment.spec.name,
                    owner=segment.thread.created_by_uuid,
                )
            except GatewayUnavailable:
                return None
            return access.key

        return fetch


def _deferred_mcp_tools(tools: list[Any], settings: Settings) -> list[Any]:
    """MCP 工具数量或定义总量超过阈值 → 全部改为延迟工具；否则照常常驻。"""
    if not tools:
        return []
    from atlas_engine.kernel.context import estimate_text
    from langchain_core.utils.function_calling import convert_to_openai_tool

    size = 0
    for t in tools:
        try:
            size += estimate_text(json.dumps(convert_to_openai_tool(t), ensure_ascii=False))
        except Exception:
            size += estimate_text(str(getattr(t, "description", "")))
    if len(tools) > settings.deferred_tools_min_count or size > settings.deferred_tools_min_tokens:
        return list(tools)
    return []
