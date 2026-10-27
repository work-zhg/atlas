"""RunHooks 的组装（与执行循环分离）。

InProcessExecutor 此前把「组装能力」（MCP 解析、压缩、标题、审批、子模型）
和「执行循环」（起任务、收事件、落库）揉在一个 400 行的类里 ——
换 arq/Celery worker 时这 400 行几乎要整体搬迁，RunExecutor Protocol
承诺的「只加一个实现类」兑现不了。

现在组装归本模块：换执行器时搬走的是循环，HookAssembly 原样复用。

PreparedRun 是「任务是什么」的载体（对应 engine run() 签名的前半），
HookAssembly.build() 产出「用什么能力跑」（RunHooks，签名的后半）。
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID

import redis.asyncio as aioredis
from atlas_engine.kernel.middleware.compaction import CompactionMiddleware
from langchain_core.messages import BaseMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atlas_agent.domain.spec import AgentSpec, resolve_summarizer_model
from atlas_agent.domain.tool_registry import BASH_TOOL, MEMORY_TOOL
from atlas_agent.domain.translator import extract_text
from atlas_agent.errors import CapabilityUnavailable
from atlas_agent.executor.context import load_instructions
from atlas_agent.executor.runner import Titler
from atlas_agent.providers.knowledge import format_context, make_knowledge_tool, require_client
from atlas_agent.providers.llm.factory import build_chat_model
from atlas_agent.providers.opensandbox import SandboxProvisioner
from atlas_agent.providers.sandbox import PodSandbox
from atlas_agent.telemetry.model_callback import ModelSpanHandler

from ..config import Settings
from ..configplane import skill_directory
from ..db.models import Thread
from ..domain.events import EventType
from ..outbound import current_identity
from ..providers.filesystem import WorkspaceRouting, make_workspace
from ..providers.filesystem.skill_copy import (
    resolve_skills,
    seed_session_skills,
    skill_refs_for,
)
from ..providers.mcp import (
    MCP_PREFIX,
    CallContext,
    GatewayUnavailable,
    KeyAuth,
    McpSessions,
    NativeMcpTools,
    make_mcp_catalog,
    mcp_subject_access,
)
from ..providers.mcp.gateway import forget_access, servers_in
from ..repositories.model_catalog import ModelCatalogRepository
from ..services.approval import RedisApprovalGate
from ..services.compaction import ThreadSummarizer, make_failure_hook, make_persist_hook
from ..services.title import TitleService
from ..stream.relay import EventRelay
from .build import build_graph

if TYPE_CHECKING:
    from ..memory import MemoryRuntime

logger = logging.getLogger(__name__)


class ModelBuilder(Protocol):
    def __call__(self, model_spec: Any, *, base_url: str, api_key: str) -> Any: ...


def default_model_builder(model_spec: Any, *, base_url: str, api_key: str) -> Any:
    """★ 遥测回调挂在这里，因为**全部**模型构造都过这一个函数：主对话、
    标题生成、上下文摘要。挂在这里三处一并覆盖，而且不用碰 runner ——
    它是纯计算层，往那里塞回调会让「run 一轮」不再能脱离基础设施单测。

    ★ 用构造器级 callbacks 而不是逐次调用传 config：调用点分散在三处，
      漏一处的表现是「某类模型调用在链路图上凭空消失」，而那恰恰最难发现。

    未启用遥测时回调照常挂着，但它拿到的是 no-op span —— 不写 if 分支。
    """
    return build_chat_model(
        model_spec, base_url=base_url, api_key=api_key, callbacks=[ModelSpanHandler()]
    )


def _deferred_mcp_tools(tools: list[Any], settings: Settings) -> list[Any]:
    """MCP 工具数量或定义总量超过阈值 → 全部改为延迟工具；否则照常常驻。"""
    if not tools:
        return []
    from atlas_engine.kernel.middleware._context import estimate_text
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


class SandboxNotEnabled(CapabilityUnavailable):
    """勾了 Shell 但部署没开沙箱 —— 明确报错（§13.2），不静默少装 execute。"""


class MemoryNotConfigured(CapabilityUnavailable):
    """勾了「记忆」但部署没接入记忆服务 —— 明确报错（§13.2），不静默少装写入工具。

    ★ 只管「没配置」。记忆服务**运行时**不可用不报错：本轮不用记忆，对话照常
      （记忆设计 §4.1：记忆不能成为会话的硬依赖）。
    """


@dataclass(frozen=True)
class PreparedRun:
    """从库里还原出的「任务是什么」。

    history_upto: 本轮起始时历史末尾的时间 —— 若本轮触发压缩，摘要覆盖到
    这里。★ 必须随任务传递而不是存在执行器实例上：执行器是进程级单例，
    同时跑多个 run，实例属性会互相覆盖。
    """

    spec: AgentSpec
    input_content: Any
    thread: Thread
    #: 本次执行用的配置版本。子 run 沿用**同一个** —— 子智能体的配置就住在
    #: 这份快照的 subagents 里，换一份等于换掉了「当时用的哪份配置」。
    agent_version_id: UUID | None = None
    history: list[BaseMessage] = field(default_factory=list)
    history_upto: datetime | None = None
    #: 事件序号的起点 = 已发出的最后一个 seq。一个 run 分多段执行时，后一段
    #: 必须接着前一段往下数（契约规则 2 的「无空洞」是跨段成立的）。
    #: 0 = 全新的 run。
    start_seq: int = 0
    #: True = 这个 run 之前已经跑过一段，本次是续跑。本轮输入早已在 history
    #: 里，不能再 append 一遍（那会让模型看到同一个问题问了两次）。
    resume: bool = False
    #: 这个 run 在之前几段里已经花掉的 token。max_total_tokens 是跨段累计的。
    prior_tokens: int = 0
    #: 这个 run 在委派树里的深度：主 run 是 0，子 run 是 1。进 TraceEvent.depth，
    #: 前端据此把子智能体的过程渲染进卡片而不是主对话。
    base_depth: int = 0
    #: 本段是接管来的（横向扩展设计 §5）：None = 正常领取；"handoff" / "expired" = 上一个
    #: 执行器停机交接 / 租约过期。acp 据此只 attach 不重开一轮。
    takeover: str | None = None
    #: acp：上一个执行器已处理到的 bridge 实例与序号（§5.2），接管时从这里 attach 补发。
    acp_bridge_instance: str | None = None
    acp_acked_seq: int = 0
    #: 续跑时交回图的挂起现场（contracts/suspension.py）：上一段还在等的调用
    #: （run.waiting_on，已剪掉历史里有结果的），和 server 查好的结果。首段都为空。
    waiting_on: dict[str, Any] = field(default_factory=dict)
    #: approval → ApprovalOutcome（结构体）；subagent → 结论文本
    decisions: dict[str, Any] = field(default_factory=dict)
    #: 本段开始时的审批模式（run.approval_mode；doc/native-approval-mode-design.html）。
    #: native 交给图；acp 子智能体据此与自己的 permission_mode 取更严格的
    approval_mode: str | None = None
    #: 本轮用户消息的时间。Agent 在这一轮写入记忆时用它作为「信息成立的时间」（记忆设计 §6.4）
    input_at: datetime | None = None

    @property
    def thread_id(self) -> UUID:
        return self.thread.id


class HookAssembly:
    """把一次 run 所需的全部能力注入组装成 RunHooks。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        model_builder: ModelBuilder,
        executor: Any = None,
        memory: MemoryRuntime | None = None,
    ) -> None:
        #: 委派要能起子 run。循环引用是刻意的且只有一层：执行器持有本对象，
        #: 本对象只在构造委派回调时回调它的 submit —— 不在初始化期用到。
        self._executor = executor
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._build_model = model_builder
        #: 记忆（doc/memory-design.html）。None = 没接入记忆服务
        self._memory = memory

    async def build(
        self,
        prepared: PreparedRun,
        *,
        run_id: UUID,
        redis: aioredis.Redis,
        relay: EventRelay,
        notices: list[tuple[EventType, dict[str, Any]]] | None = None,
        closers: list[Callable[[], Awaitable[None]]] | None = None,
    ) -> tuple[Any, Titler | None]:
        """收集本次 run 的全部能力对象，装配出图。返回 (graph, titler)。

        策略（装什么、什么顺序）在 build.py::build_graph —— 那里是纯函数；
        这里只做 IO：解析 MCP、起沙箱容器、连对象存储、建 Redis 门禁。
        titler 单独返回：它是 runner 的注入点，不进图。
        closers: 本 Run 段结束时要收尾的（MCP 会话），由调用方在段结束时执行。
        """
        # MCP 工具在这里解析成 BaseTool —— engine 不认识 MCP 的传输与凭据
        # （§14）。解析失败要冒泡成 run.failed，而不是少装几个工具就默默跑
        # （§13.2）。工具定义读目录缓存，命中时装配不连网（MCP 详设 §05）。
        extra_tools = await self._mcp_tool_list(
            prepared, run_id=run_id, redis=redis, notices=notices, closers=closers
        )
        # MCP 工具多时改为按需加载：定义不常驻上下文，模型用 tool_search 加载（上下文管理 §13）
        deferred = _deferred_mcp_tools(extra_tools, self._settings)

        # ★ 网页搜索不再是内置工具：它经 Higress 以 MCP 提供
        #   （mcp:serpapi:google_search），后端 key 只在网关里，走上面那条路。

        # 记忆（doc/memory-design.html §4–§5）：常驻块进系统提示词，load_memory 随记忆挂上；
        # 勾了「记忆」的主会话再挂 save_memory / forget_memory。★ 关联主键（会话所属用户）
        # 闭包捕获，不进工具参数 —— 一旦可由模型指定，提示词注入就能读写别人的记忆（§4.8）。
        wants_write = MEMORY_TOOL in prepared.spec.tool_names
        if wants_write and self._memory is None and prepared.thread.memory != "off":
            raise MemoryNotConfigured("勾选了「记忆」但服务端未接入记忆服务（MEMORY_BASE_URL）")
        memory = (
            await self._memory.turn(
                prepared.thread,
                run_id=run_id,
                writable=wants_write,
                resume=prepared.resume,
                history_upto=prepared.history_upto,
                occurred_at=prepared.input_at,
            )
            if self._memory is not None
            else None
        )
        if memory is not None:
            extra_tools = [*extra_tools, *memory.tools()]

        # 知识库（tool 模式）：库 id 闭包捕获，不进参数（doc/atlas-rag-design.html §10）
        knowledge = prepared.spec.knowledge
        if knowledge.enabled and knowledge.mode == "tool":
            extra_tools = [
                *extra_tools,
                make_knowledge_tool(
                    require_client(self._settings),
                    knowledge,
                    names=None,
                    source_ref=f"thread:{prepared.thread_id}/run:{run_id}",
                ),
            ]

        # Shell 沙箱（doc/sandbox-opensandbox-design.html）。★ 这里只构造客户端，不建沙箱 ——
        #   沙箱在第一次 execute 时才按需创建（多数原生会话用不到）。
        #   按工作区建：子智能体（workspace_thread_id = 父会话）与父会话共用一个。
        ws_thread = str(prepared.thread.workspace_thread_id)
        provisioner = (
            SandboxProvisioner(self._settings, redis) if self._settings.sandbox_enabled else None
        )
        sandbox = None
        if BASH_TOOL in prepared.spec.tool_names:
            if provisioner is None:
                raise SandboxNotEnabled(
                    "勾选了 Shell（bash）但服务端未开启沙箱（SANDBOX_ENABLED=false）"
                )
            settings = self._settings
            sandbox = PodSandbox(
                provisioner=provisioner,
                workspace_thread_id=ws_thread,
                user_id=str(prepared.thread.created_by),
                run_id=str(run_id),
                poll_min_s=settings.sandbox_poll_min_s,
                poll_max_s=settings.sandbox_poll_max_s,
                output_limit=settings.sandbox_output_limit_bytes,
                idle_s=settings.acp_tool_idle_timeout_s,
                stall_cpu_pct=settings.sandbox_stall_cpu_pct,
            )

        # ★ workspace 挂**父会话的**前缀，skills / system 仍按自己的 thread 走
        #   （Thread.workspace_thread_id 是 parent_thread_id or id）。
        #   没配对象存储 = **没有文件能力**（make_workspace 返回 None，
        #   不回落任何内存实现）：文件工具届时一个都不注册。
        #   ★ 工作区的读写路由跟着**工作区**走，不跟着本智能体有没有 bash 走：没勾 bash 的子智能体
        #     与父会话共用工作区，父会话的沙箱在跑时它的文件工具也要进沙箱（与命令同一个挂载，§9）。
        workspace = make_workspace(
            self._settings,
            prepared.thread.created_by,
            prepared.thread_id,
            workspace_thread_id=prepared.thread.workspace_thread_id,
            routing=WorkspaceRouting(provisioner, ws_thread) if provisioner else None,
        )

        approvals = RedisApprovalGate(self._sessionmaker, redis, run_id)

        # 技能清单：描述取自配置服务；已下架的跳过并告诉用户（技能 / MCP 设计 §7.3）
        skill_metas, skipped = await resolve_skills(
            prepared.spec.skills, skill_directory(self._settings)
        )
        for item in skipped:
            if notices is not None:
                notices.append((EventType.SKILL_SKIPPED, item))
            if workspace is not None:
                # ★ 删掉本会话里的副本：只从索引里拿掉的话，模型仍可能凭记忆去读
                await workspace.adelete(f"/skills/{item['slug']}", recursive=True)

        chat = self._build_model(
            prepared.spec.model,
            base_url=str(self._settings.litellm_base_url),
            api_key=self._settings.litellm_key.get_secret_value(),
        )

        # 持久指令（L1）：服务不可用时跳过，不让本轮失败
        instructions = await load_instructions(
            self._settings, workspace, prepared.thread.created_by
        )

        graph = build_graph(
            prepared.spec,
            chat,
            instructions=instructions,
            memory=memory.block if memory is not None else None,
            deferred_tools=deferred,
            filesystem=workspace,
            sandbox=sandbox,
            approvals=approvals,
            compactor=await self._compactor(
                prepared,
                workspace=workspace,
                # 记忆的更新提示（§5.5）：每次调模型时现取，Agent 重新加载过的主题不再列出
                dynamic_attachments=memory.reminders if memory is not None else None,
            ),
            # ★ 技能在**会话创建**时就已拷进 skills 前缀（见 skill_copy），
            #   这里只把清单渲染给中间件 —— 不扫描、不读文件。
            skills=skill_refs_for(skill_metas),
            extra_tools=extra_tools,
            # ★ SubAgent 不在图内跑子图 —— SubAgentService 在子会话上起子 run，本段挂起。
            spawner=self._subagent_spawner(prepared, run_id=run_id, relay=relay),
            prompt_cache_ttl=await self._prompt_cache_ttl(prepared),
        )
        return graph, self._titler(prepared)

    async def with_knowledge(self, prepared: PreparedRun, *, run_id: UUID) -> PreparedRun:
        """auto 模式：先用本轮提问检索，参考资料并入**这一轮**的输入（同 Dify 对话应用的上下文）。

        ★ 只改模型这一轮看到的输入：用户消息早已按原文落库，Transcript 也只收模型与工具的输出，
          所以历史里没有这段资料 —— 下一轮按下一轮的提问重新检索。
        ★ 续跑段（resume）不再检索：本轮输入已在 history 里，没有可并入的位置。
        ★ 检索失败让这一轮明确失败（KnowledgeUnavailable），不静默当作「没查到」（§13.2）。
        """
        knowledge = prepared.spec.knowledge
        if not knowledge.enabled or knowledge.mode != "auto" or prepared.resume:
            return prepared
        query = extract_text(prepared.input_content).strip()
        if not query:
            return prepared
        records = await require_client(self._settings).retrieve(
            knowledge,
            query[:2000],
            source="agent",
            source_ref=f"thread:{prepared.thread_id}/run:{run_id}",
        )
        return dataclasses.replace(
            prepared, input_content=format_context(prepared.input_content, records)
        )

    async def _mcp_tool_list(
        self,
        prepared: PreparedRun,
        *,
        run_id: UUID,
        redis: aioredis.Redis,
        notices: list[tuple[EventType, dict[str, Any]]] | None,
        closers: list[Callable[[], Awaitable[None]]] | None = None,
    ) -> list[Any]:
        """spec 里的 mcp:* → BaseTool。未复核 / 漂移 / 没权限 / 没 Key 的经 notices 告知用户。

        经 MCP 网关（MCP 方案设计 §6.1）：按这一轮的权限主体问网关「能用哪些服务、Key 是什么」
        （一次），没权限、没 Key 的服务跳过并提示；工具定义读缓存的元数据，装配不连 MCP 服务。
        同一个 Run 段对同一个服务复用一个会话（§6.3），段结束时经 closers 关掉。
        """
        if not any(name.startswith(MCP_PREFIX) for name in prepared.spec.tool_names):
            return []  # 不碰注册表：没用 MCP 的 agent 不该因为配置服务不可用而失败
        s = self._settings
        ident = current_identity()
        ctx = CallContext(
            run_id=run_id,
            thread_id=prepared.thread_id,
            user_id=prepared.thread.created_by,
            # 执行器在 run 开始时设好的出站身份（以 Agent 为权限时是数字员工）
            auth_uid=ident.auth_uid if ident else None,
        )
        catalog = await make_mcp_catalog(s, redis)
        subject = ctx.auth_uid or ctx.user_id
        try:
            access = await mcp_subject_access(s, subject)
        except GatewayUnavailable as exc:
            # 网关不可用 = 这些服务此刻用不了：跳过并提示，对话照常（§6.7 ③）
            for server in sorted(servers_in(prepared.spec.tool_names)):
                if notices is not None:
                    notices.append(
                        (
                            EventType.MCP_TOOL_SKIPPED,
                            {
                                "server": server,
                                "tools": [],
                                "reason": "unavailable",
                                "subject": str(subject),
                                "message": f"{exc}，本轮不装这个服务的工具",
                            },
                        )
                    )
            return []
        sessions = McpSessions(
            auth=KeyAuth(access.key) if access.key else None,
            fetch_key=self._key_fetcher(subject),
        )
        if closers is not None:
            closers.append(sessions.aclose)
        tools = NativeMcpTools(
            catalog,
            call_timeout_s=s.mcp_call_timeout_s,
            max_result_chars=s.mcp_max_result_chars,
        )
        return await tools.tools_for(
            prepared.spec.tool_names,
            ctx,
            digests=prepared.spec.mcp_tool_digests,
            drift_policy=prepared.spec.mcp_drift_policy,
            reviews=catalog.reviews(),
            notices=notices,
            access=access,
            sessions=sessions,
        )

    def _key_fetcher(self, subject: UUID) -> Callable[[], Awaitable[str | None]]:
        """Key 被网关拒绝时重新取一次（§6.7 ④）。取不到（网关也不可用）当作没有新 Key。"""

        async def fetch() -> str | None:
            forget_access(subject)
            try:
                return (await mcp_subject_access(self._settings, subject)).key
            except GatewayUnavailable:
                return None

        return fetch

    def _subagent_spawner(self, prepared: PreparedRun, *, run_id: UUID, relay: EventRelay) -> Any:
        """构造本段派 SubAgent 的入口（SubAgentSpawner 的实现）。

        没有 SubAgent 就返回 None —— kernel 据此**根本不装** `task`，
        与「没配沙箱就没有 execute」同一条纪律：模型看不见 = 不会去调。

        ★ 子 run 也走这里：native SubAgent 的 spec 带着同一份名录（spec_for_subagent），
          所以它也能继续派 SubAgent；acp SubAgent 的名录为空，天然返回 None。
        """
        if not prepared.spec.subagents or self._executor is None:
            return None
        if prepared.agent_version_id is None:
            logger.warning("run %s 缺 agent_version_id，SubAgent 不启用", run_id)
            return None

        from ..services.subagent import SubAgentService

        return SubAgentService(
            self._sessionmaker,
            self._settings,
            self._executor,
            relay,
            parent_run_id=run_id,
            parent_thread=prepared.thread,
            parent_spec=prepared.spec,
            agent_version_id=prepared.agent_version_id,
            seed_skills=self._seed_subagent_skills,
        )

    async def _seed_subagent_skills(self, thread: Thread, sub: Any) -> None:
        """把子智能体的技能拷进**它自己的** skills 前缀。

        用的是子会话的 thread_id —— workspace 参数在这里无关紧要（只写 skills），
        但仍按真实形态构造，免得将来有人在这里顺手读工作区时拿到错的根。
        """
        if not sub.skills:
            return
        fs = make_workspace(
            self._settings,
            thread.created_by,
            thread.id,
            workspace_thread_id=thread.workspace_thread_id,
        )
        if fs is None:
            return
        metas, _skipped = await resolve_skills(
            sub.skills, skill_directory(self._settings), with_descriptions=False
        )
        await seed_session_skills(fs, metas)

    # ------------------------------------------------------------------ 各能力

    def _titler(self, prepared: PreparedRun) -> Titler | None:
        """仅当会话还没有标题时才返回生成器（§8.1）。

        返回 None 时 engine 直接跳过，不产出 thread.title_generated ——
        已有标题的会话不该每轮都重新生成，那既费钱又会覆盖用户改过的名字。
        """
        thread = prepared.thread
        if thread.title_source != "pending":
            return None

        question = extract_text(prepared.input_content)
        service = TitleService(self._settings, self._build_model)

        async def titler(answer: str) -> dict[str, Any] | None:
            return await service.generate(question, answer)

        return titler

    async def _prompt_cache_ttl(self, prepared: PreparedRun) -> Literal["5m", "1h"] | None:
        """开了缓存、且模型实测支持时才打 cache_control —— 编辑器里的开关就是这里生效。

        ★ supports_cache 为假的模型（DeepSeek）塞了 cache_control 也不起作用（迁移 0008 实测），
          它自己的上下文缓存不受影响。
        """
        if not prepared.spec.model.prompt_cache:
            return None
        async with self._sessionmaker() as session:
            if not await ModelCatalogRepository(session).supports_cache(prepared.spec.model.model):
                return None
        return self._settings.prompt_cache_ttl

    async def _compactor(
        self,
        prepared: PreparedRun,
        *,
        workspace: Any = None,
        dynamic_attachments: Any = None,
    ) -> CompactionMiddleware | None:
        """按 spec 与模型目录构造上下文管理中间件（doc/context-management-design.html）。

        context_window / max_output_tokens 来自 model_catalog —— 实测落库（§3 D3），
        不能硬编码：opus/sonnet/fable 是 1M，haiku 只有 200k，差 5 倍。
        """
        spec = prepared.spec
        cfg = spec.compaction
        if not cfg.enabled:
            return None

        async with self._sessionmaker() as session:
            limits = await ModelCatalogRepository(session).limits(spec.model.model)
        if limits is None:
            # 目录里没有这个模型 —— 不猜一个窗口值：猜大了压缩永不触发，
            # 猜小了每轮都压。宁可不压，让 token 上限去兜底。
            logger.warning("model_catalog 无 %s 的窗口，跳过压缩", spec.model.model)
            return None
        window, model_max_output = limits
        effective_max_output = min(spec.model.max_output_tokens, model_max_output)

        # 距上次回复多久：超过缓存有效期 → 缓存反正失效，顺手清旧工具结果（§8 ②）
        idle_s = None
        if prepared.history_upto is not None:
            idle_s = (datetime.now(UTC) - prepared.history_upto).total_seconds()
        state = prepared.thread.context_state or {}

        return CompactionMiddleware(
            context_window=window,
            max_output_tokens=effective_max_output,
            max_output_cap=model_max_output,
            trigger_ratio=cfg.trigger_ratio,
            clear_ratio=cfg.clear_ratio,
            warn_ratio=cfg.warn_ratio,
            keep_recent_turns=cfg.keep_recent_turns,
            keep_recent_tool_results=cfg.keep_recent_tool_results,
            batch_cap_tokens=self._settings.context_batch_cap_tokens,
            idle_s=idle_s,
            cache_ttl_s=self._settings.prompt_cache_ttl_s,
            filesystem=workspace,
            restore_files=cfg.restore_files,
            dynamic_attachments=dynamic_attachments,
            attachment_budget_tokens=int(
                (window - min(effective_max_output, 20_000)) * cfg.attachment_budget_ratio
            ),
            failure_count=int(state.get("compaction_failures") or 0),
            on_failed=make_failure_hook(self._sessionmaker, prepared.thread_id),
            summarizer=ThreadSummarizer(
                self._settings,
                self._build_model,
                model=resolve_summarizer_model(cfg.summarizer_model, spec.model.model),
            ),
            on_compacted=make_persist_hook(
                self._sessionmaker, prepared.thread_id, prepared.history_upto
            ),
        )
