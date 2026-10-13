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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

import redis.asyncio as aioredis
from atlas_engine.kernel.middleware.compaction import CompactionMiddleware
from langchain_core.messages import BaseMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atlas_server.domain.spec import AgentSpec, resolve_summarizer_model
from atlas_server.domain.tool_registry import BASH_TOOL, MEMORY_SEARCH_TOOL
from atlas_server.domain.translator import extract_text
from atlas_server.errors import CapabilityUnavailable
from atlas_server.executor.context import load_instructions, memory_attachments
from atlas_server.executor.runner import Titler
from atlas_server.memory.tool import MemoryUnavailable, make_memory_search_tool
from atlas_server.providers.cluster import ClusterPods
from atlas_server.providers.knowledge import format_context, make_knowledge_tool, require_client
from atlas_server.providers.llm.factory import build_chat_model
from atlas_server.providers.sandbox import PodSandbox
from atlas_server.telemetry.model_callback import ModelSpanHandler

from ..config import Settings
from ..configplane import skill_directory
from ..configplane.mcp import make_mcp_catalog, mcp_reviews
from ..db.models import Thread
from ..domain.events import EventType
from ..providers.filesystem import make_workspace
from ..providers.filesystem.skill_copy import (
    resolve_skills,
    seed_session_skills,
    skill_refs_for,
)
from ..providers.mcp import MCP_PREFIX, CallContext, NativeMcpTools
from ..repositories.model_catalog import ModelCatalogRepository
from ..services.approval import RedisApprovalGate
from ..services.compaction import ThreadSummarizer, make_failure_hook, make_persist_hook
from ..services.title import TitleService
from ..stream.relay import EventRelay
from .build import build_graph

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
        memory: Any = None,
    ) -> None:
        #: 委派要能起子 run。循环引用是刻意的且只有一层：执行器持有本对象，
        #: 本对象只在构造委派回调时回调它的 submit —— 不在初始化期用到。
        self._executor = executor
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._build_model = model_builder
        #: 记忆客户端。None = 没开记忆 —— 勾了 search_memory 也不会注入
        #: 那个工具，而是进 unsupported_tools 明确报出来（§13.2）。
        self._memory = memory

    async def build(
        self,
        prepared: PreparedRun,
        *,
        run_id: UUID,
        redis: aioredis.Redis,
        relay: EventRelay,
        notices: list[tuple[EventType, dict[str, Any]]] | None = None,
    ) -> tuple[Any, Titler | None]:
        """收集本次 run 的全部能力对象，装配出图。返回 (graph, titler)。

        策略（装什么、什么顺序）在 build.py::build_graph —— 那里是纯函数；
        这里只做 IO：解析 MCP、起沙箱容器、连对象存储、建 Redis 门禁。
        titler 单独返回：它是 runner 的注入点，不进图。
        """
        # MCP 工具在这里解析成 BaseTool —— engine 不认识 MCP 的传输与凭据
        # （§14）。解析失败要冒泡成 run.failed，而不是少装几个工具就默默跑
        # （§13.2）。工具定义读目录缓存，命中时装配不连网（MCP 详设 §05）。
        extra_tools = await self._mcp_tool_list(
            prepared, run_id=run_id, redis=redis, notices=notices
        )
        # MCP 工具多时改为按需加载：定义不常驻上下文，模型用 tool_search 加载（上下文管理 §13）
        deferred = _deferred_mcp_tools(extra_tools, self._settings)

        # ★ 网页搜索不再是内置工具：它经 Higress 以 MCP 提供
        #   （mcp:serpapi:google_search），后端 key 只在网关里，走上面那条路。

        # 记忆检索（只读）。★ user_id 从**会话所有者**闭包捕获，不进工具
        #   的参数 schema —— 一旦它可由模型指定，就是一个读取他人记忆的
        #   口子，而提示词注入是真实存在的攻击面（记忆设计 §09）。
        # 会话关闭了用户记忆（如服务账号的会话）→ 不挂检索工具：这是会话的设定，不是缺前提
        if MEMORY_SEARCH_TOOL in prepared.spec.tool_names and prepared.thread.memory != "off":
            # ★ 缺前提要**明确失败**，不能静默少装一个工具（§13.2）。
            #   静默跳过的话，模型会
            #   以为自己没有记忆可查，而用户以为记忆在工作。
            if self._memory is None:
                raise MemoryUnavailable(
                    "勾选了 search_memory 但服务端未开启记忆（MEMORY_ENABLED=false）"
                )
            extra_tools = [
                *extra_tools,
                make_memory_search_tool(self._memory, user_id=prepared.thread.created_by),
            ]

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
                    actor=prepared.thread.created_by,
                ),
            ]

        # Shell 沙箱（doc/native-sandbox-design.html）。★ 这里只构造客户端，不建 Pod ——
        #   Pod 在第一次 execute 时才按需创建（多数原生会话用不到沙箱）。
        #   按工作区建：子智能体（workspace_thread_id = 父会话）与父会话共用一个。
        sandbox = None
        if BASH_TOOL in prepared.spec.tool_names:
            if not self._settings.sandbox_enabled:
                raise SandboxNotEnabled(
                    "勾选了 Shell（bash）但服务端未开启沙箱（SANDBOX_ENABLED=false）"
                )
            sandbox = PodSandbox(
                pods=ClusterPods(self._settings),
                workspace_thread_id=str(prepared.thread.workspace_thread_id),
                user_id=str(prepared.thread.created_by),
                run_id=str(run_id),
                git_profile=prepared.thread.git_profile or "",
                idle_s=self._settings.acp_tool_idle_timeout_s,
            )

        # ★ workspace 挂**父会话的**前缀，skills / system 仍按自己的 thread 走
        #   （Thread.workspace_thread_id 是 parent_thread_id or id）。
        #   没配对象存储 = **没有文件能力**（make_workspace 返回 None，
        #   不回落任何内存实现）：文件工具届时一个都不注册。
        workspace = make_workspace(
            self._settings,
            prepared.thread.created_by,
            prepared.thread_id,
            workspace_thread_id=prepared.thread.workspace_thread_id,
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

        # 持久指令（L1）与本轮记忆附件（L4）：服务不可用时跳过，不让本轮失败
        instructions = await load_instructions(
            self._settings, workspace, prepared.thread.created_by
        )
        attachments = await memory_attachments(
            self._settings,
            self._memory,
            user_id=prepared.thread.created_by,
            input_content=prepared.input_content,
            thread_memory=prepared.thread.memory,
        )

        graph = build_graph(
            prepared.spec,
            chat,
            instructions=instructions,
            deferred_tools=deferred,
            filesystem=workspace,
            sandbox=sandbox,
            approvals=approvals,
            compactor=await self._compactor(prepared, workspace=workspace, attachments=attachments),
            # ★ 技能在**会话创建**时就已拷进 skills 前缀（见 skill_copy），
            #   这里只把清单渲染给中间件 —— 不扫描、不读文件。
            skills=skill_refs_for(skill_metas),
            extra_tools=extra_tools,
            # ★ 委派不在图内跑子图 —— 交给 SubagentService 在子会话上起子 run。
            subagents=self._subagent_gateway(prepared, run_id=run_id, relay=relay),
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
            actor=prepared.thread.created_by,
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
    ) -> list[Any]:
        """spec 里的 mcp:* → BaseTool。未复核 / 漂移的定义经 notices 告知用户（§9）。"""
        if not any(name.startswith(MCP_PREFIX) for name in prepared.spec.tool_names):
            return []  # 不碰注册表：没用 MCP 的 agent 不该因为配置服务不可用而失败
        s = self._settings
        catalog = await make_mcp_catalog(s, redis)
        tools = NativeMcpTools(
            catalog,
            call_timeout_s=s.mcp_call_timeout_s,
            max_result_chars=s.mcp_max_result_chars,
        )
        return await tools.tools_for(
            prepared.spec.tool_names,
            CallContext(
                run_id=run_id,
                thread_id=prepared.thread_id,
                user_id=prepared.thread.created_by,
            ),
            digests=prepared.spec.mcp_tool_digests,
            drift_policy=prepared.spec.mcp_drift_policy,
            reviews=await mcp_reviews(s, catalog.servers()),
            notices=notices,
        )

    def _subagent_gateway(self, prepared: PreparedRun, *, run_id: UUID, relay: EventRelay) -> Any:
        """构造本次 run 的委派受理方（DelegationProtocol 的实现）。

        没有子智能体就返回 None —— kernel 据此**根本不装** `task`，
        与「没配沙箱就没有 execute」同一条纪律：模型看不见 = 不会去调。

        子会话不能再委派（spec_for_subagent 已把 subagents 清空且
        max_subagent_depth=0），所以这里对子 run 天然返回 None。
        """
        if not prepared.spec.subagents or self._executor is None:
            return None
        if prepared.agent_version_id is None:
            logger.warning("run %s 缺 agent_version_id，委派能力不启用", run_id)
            return None

        from ..services.subagent import SubagentService

        return SubagentService(
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

    async def _compactor(
        self,
        prepared: PreparedRun,
        *,
        workspace: Any = None,
        attachments: list[tuple[str, str]] | None = None,
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
            attachments=attachments or [],
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
