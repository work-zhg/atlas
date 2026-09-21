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

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

import redis.asyncio as aioredis
from atlas_engine.kernel.middleware.compaction import CompactionMiddleware
from langchain_core.messages import BaseMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atlas_server.domain.spec import AgentSpec
from atlas_server.domain.tool_registry import MEMORY_SEARCH_TOOL, WEB_SEARCH_TOOL
from atlas_server.domain.translator import extract_text
from atlas_server.executor.runner import Titler
from atlas_server.memory.tool import MemoryUnavailable, make_memory_search_tool
from atlas_server.providers.llm.factory import build_chat_model
from atlas_server.telemetry.model_callback import ModelSpanHandler

from ..config import Settings
from ..db.models import Thread
from ..providers.filesystem import make_workspace
from ..providers.filesystem.skill_copy import (
    seed_session_skills,
    skill_metas_for,
    skill_refs_for,
)
from ..repositories.model_catalog import ModelCatalogRepository
from ..services.approval import RedisApprovalGate
from ..services.compaction import ThreadSummarizer, make_persist_hook
from ..services.mcp import McpService
from ..services.search import SearchUnavailable, make_web_search_tool
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
    ) -> tuple[Any, Titler | None]:
        """收集本次 run 的全部能力对象，装配出图。返回 (graph, titler)。

        策略（装什么、什么顺序）在 build.py::build_graph —— 那里是纯函数；
        这里只做 IO：解析 MCP、起沙箱容器、连对象存储、建 Redis 门禁。
        titler 单独返回：它是 runner 的注入点，不进图。
        """
        # MCP 工具在这里解析成 BaseTool —— engine 不认识 MCP 的传输与凭据
        # （§14）。解析失败要冒泡成 run.failed，而不是少装几个工具就默默跑
        # （§13.2）。
        extra_tools = await McpService(self._settings.mcp_servers).tools_for(
            list(prepared.spec.tool_names)
        )

        # 裸网页搜索：检索策略归模型（拆词/回环/综合它自己做），
        # server 只提供一次一查的 API。凭据缺失明确失败（§13.2）。
        if WEB_SEARCH_TOOL in prepared.spec.tool_names:
            if self._settings.serpapi_key is None:
                raise SearchUnavailable("勾选了 web_search 但服务端未配置 SERPAPI_KEY")
            extra_tools = [
                *extra_tools,
                make_web_search_tool(self._settings.serpapi_key.get_secret_value()),
            ]

        # 记忆检索（只读）。★ user_id 从**会话所有者**闭包捕获，不进工具
        #   的参数 schema —— 一旦它可由模型指定，就是一个读取他人记忆的
        #   口子，而提示词注入是真实存在的攻击面（记忆设计 §09）。
        if MEMORY_SEARCH_TOOL in prepared.spec.tool_names:
            # ★ 缺前提要**明确失败**，不能静默少装一个工具（§13.2）。
            #   与 web_search 缺 SERPAPI_KEY 同款：静默跳过的话，模型会
            #   以为自己没有记忆可查，而用户以为记忆在工作。
            if self._memory is None:
                raise MemoryUnavailable(
                    "勾选了 search_memory 但服务端未开启记忆（MEMORY_ENABLED=false）"
                )
            extra_tools = [
                *extra_tools,
                make_memory_search_tool(
                    self._memory, user_id=prepared.thread.created_by
                ),
            ]

        # ★ sandbox 恒为 None：Docker 实现已删除，K8s Pod 实现（acp 详设的
        #   providers/pods/）未接入。bash 在 tool_registry 里标 implemented=False，
        #   勾了会进 unsupported_tools 明确报出来 —— 不会走到这里。
        sandbox = None

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

        approvals = RedisApprovalGate(
            self._sessionmaker,
            redis,
            run_id,
            timeout_s=self._settings.approval_timeout_s,
        )

        chat = self._build_model(
            prepared.spec.model,
            base_url=str(self._settings.litellm_base_url),
            api_key=self._settings.litellm_key.get_secret_value(),
        )

        graph = build_graph(
            prepared.spec,
            chat,
            filesystem=workspace,
            sandbox=sandbox,
            approvals=approvals,
            compactor=await self._compactor(prepared),
            # ★ 技能在**会话创建**时就已拷进 skills 前缀（见 skill_copy），
            #   这里只把清单渲染给中间件 —— 不扫描、不读文件。
            skills=skill_refs_for(skill_metas_for(prepared.spec.skills)),
            extra_tools=extra_tools,
            # ★ 委派不在图内跑子图 —— 交给 SubagentService 在子会话上起子 run。
            subagents=self._subagent_gateway(prepared, run_id=run_id, relay=relay),
        )
        return graph, self._titler(prepared)

    def _subagent_gateway(
        self, prepared: PreparedRun, *, run_id: UUID, relay: EventRelay
    ) -> Any:
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
        await seed_session_skills(fs, skill_metas_for(sub.skills))

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

    async def _compactor(self, prepared: PreparedRun) -> CompactionMiddleware | None:
        """按 spec 与模型窗口构造压缩中间件（§7）。

        context_window 来自 model_catalog —— 那是实测落库的（§3 D3），
        不能硬编码：opus/sonnet/fable 是 1M，haiku 只有 200k，差 5 倍。
        """
        spec = prepared.spec
        if not spec.compaction.enabled:
            return None

        async with self._sessionmaker() as session:
            window = await ModelCatalogRepository(session).context_window(spec.model.model)
        if window is None:
            # 目录里没有这个模型 —— 不猜一个窗口值：猜大了压缩永不触发，
            # 猜小了每轮都压。宁可不压，让 token 上限去兜底。
            logger.warning("model_catalog 无 %s 的窗口，跳过压缩", spec.model.model)
            return None

        return CompactionMiddleware(
            context_window=window,
            trigger_ratio=spec.compaction.trigger_ratio,
            keep_recent_turns=spec.compaction.keep_recent_turns,
            summarizer=ThreadSummarizer(self._settings, self._build_model),
            on_compacted=make_persist_hook(
                self._sessionmaker, prepared.thread_id, prepared.history_upto
            ),
        )
