"""原生运行时 · 一轮的编排：为一段装配能力、建图。

见 c4-model/agent/native-runtime/code-native-runtime-turn.html。

CapabilityAssembly 只编排、不实现。顺序固定：模型先于一切（with_models）→ 知识库预检索
（with_knowledge）→ 段内共享的资源（记忆、沙箱供给器、工作区）→ 按固定顺序问三个能力领域
要本段的东西 —— 工具面（tool_surface.py）、挂起与人机协作（collaboration.py）、上下文管理
（context_manager.py）—— 交给 build.py::build_graph 建图（纯函数，中间件按槽位排）。新增一种
能力只改对应领域，不改这里。

它由组合根（executor/compose.py）构造，不认识执行器；委派只拿到入队这一个回调。
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

import redis.asyncio as aioredis
from atlas_engine.contracts.events import EventType
from atlas_engine.contracts.spec import resolve_summarizer_model
from atlas_engine.runtime.assemble import Capabilities
from atlas_engine.runtime.runner import Titler
from atlas_engine.runtime.tool_registry import MEMORY_TOOL
from atlas_engine.runtime.translator import extract_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atlas_agent.errors import CapabilityUnavailable
from atlas_agent.executor.segment_data import Segment
from atlas_agent.providers.knowledge import (
    format_context,
    require_client,
    skipped_message,
)
from atlas_agent.sandbox.component import SandboxComponent
from atlas_agent.sandbox.native import WorkspaceRef

from ..config import Settings
from ..outbound import current_identity
from ..providers.filesystem import WorkspaceRouting, make_workspace
from ..providers.llm.model_platform import (
    admit_models,
    agent_key,
    direct_endpoint,
    key_endpoint,
    uses_model_platform,
    with_platform_model,
)
from ..repositories.model_catalog import ModelCatalogRepository
from ..services.title import TitleService
from ..stream.relay import EventRelay
from .base import DispatchRun
from .collaboration import Collaboration
from .context_manager import ContextManager
from .run_models import ModelBuilder, RunModels, models_of
from .tool_surface import ToolSurface

if TYPE_CHECKING:
    from ..memory import MemoryRuntime, MemoryTurn

logger = logging.getLogger(__name__)


class MemoryNotConfigured(CapabilityUnavailable):
    """勾了「记忆」但部署没接入记忆服务 —— 明确报错（§13.2），不静默少装写入工具。

    ★ 只管「没配置」。记忆服务**运行时**不可用不报错：本轮不用记忆，对话照常
      （记忆设计 §4.1：记忆不能成为会话的硬依赖）。
    """


class CapabilityAssembly:
    """为一段装配能力并建图（原 HookAssembly，800 行一个类：模型、知识库、MCP、沙箱、审批、
    子智能体、技能、判断器、压缩全在里面）。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        model_builder: ModelBuilder,
        dispatch_run: DispatchRun | None = None,
        memory: MemoryRuntime | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._build_model = model_builder
        #: 记忆（doc/memory-design.html）。None = 没接入记忆服务
        self._memory = memory
        self._tools = ToolSurface(settings)
        self._collaboration = Collaboration(sessionmaker, settings, model_builder, dispatch_run)
        self._context = ContextManager(sessionmaker, settings, model_builder)

    async def capabilities(
        self,
        segment: Segment,
        *,
        run_id: UUID,
        redis: aioredis.Redis,
        relay: EventRelay,
        notices: list[tuple[EventType, dict[str, Any]]] | None = None,
        closers: list[Callable[[], Awaitable[None]]] | None = None,
    ) -> tuple[Capabilities, Titler | None]:
        """问三个能力领域要本段的东西，收成交给 engine 的能力端口。返回 (能力, titler)。

        这里只做 IO；装什么、什么顺序在 engine（atlas_engine/runtime/assemble.py）。
        titler 单独返回：它是 runner 的注入点，不进图。
        closers: 本 Run 段结束时要收尾的（MCP 会话），由调用方在段结束时执行。
        """
        # ── 段内共享的资源：几个领域都要用，在这里建一次 ──────────────────────────
        memory = await self._memory_turn(segment, run_id=run_id)
        sandbox = (
            SandboxComponent.build(self._settings, redis)
            if self._settings.sandbox_enabled
            else None
        )
        ws = WorkspaceRef(segment.thread.workspace_thread_id, segment.thread.created_by_uuid)
        # ★ workspace 挂**父会话的**前缀，skills / system 仍按自己的 thread 走。
        #   没配对象存储 = **没有文件能力**（make_workspace 返回 None，不回落任何内存实现）。
        #   ★ 工作区的读写路由跟着**工作区**走，不跟着本智能体有没有 bash 走：没勾 bash 的子智能体
        #     与父会话共用工作区，父会话的沙箱在跑时它的文件工具也要进沙箱（与命令同一个挂载，§9）。
        workspace = make_workspace(
            self._settings,
            segment.thread.created_by_uuid,
            segment.thread_id,
            workspace_thread_id=segment.thread.workspace_thread_id,
            routing=WorkspaceRouting(sandbox.native, sandbox.exec, ws) if sandbox else None,
        )

        # ── 三个能力领域，顺序固定 ────────────────────────────────────────────
        tools = await self._tools.contribute(
            segment,
            run_id=run_id,
            redis=redis,
            sandbox=sandbox,
            workspace=workspace,
            memory=memory,
            notices=notices,
            closers=closers,
        )
        collaboration = self._collaboration.contribute(segment, run_id=run_id, relay=relay)
        context = await self._context.contribute(
            segment,
            workspace=workspace,
            memory=memory,
            reminders=tools.reminders,
            readable_store=tools.readable_store,
        )

        endpoint = models_of(segment, self._settings).endpoint
        chat = self._build_model(
            segment.spec.model,
            base_url=endpoint.base_url,
            api_key=endpoint.api_key,
            headers=endpoint.headers,
        )
        caps = Capabilities(
            model=chat,
            tools=tools.extra_tools,
            deferred_tools=tools.deferred_tools,
            filesystem=workspace,
            sandbox=tools.sandbox,
            skills=tools.skills,
            approvals=collaboration.approvals,
            judge=collaboration.judge,
            spawner=collaboration.spawner,
            context=context.ports,
            instructions=context.instructions,
            memory=context.memory_block,
            prompt_cache_ttl=await self._prompt_cache_ttl(segment),
        )
        return caps, self._titler(segment)

    async def _memory_turn(self, segment: Segment, *, run_id: UUID) -> MemoryTurn | None:
        """这一段的记忆：常驻块进系统提示词（上下文管理），记忆工具挂上（工具面）。

        ★ 关联主键（会话所属用户）闭包捕获，不进工具参数 —— 一旦可由模型指定，提示词注入
          就能读写别人的记忆（§4.8）。
        """
        wants_write = MEMORY_TOOL in segment.spec.tool_names
        if wants_write and self._memory is None and segment.thread.memory != "off":
            raise MemoryNotConfigured("勾选了「记忆」但服务端未接入记忆服务（MEMORY_BASE_URL）")
        if self._memory is None:
            return None
        return await self._memory.turn(
            segment.thread,
            run_id=run_id,
            writable=wants_write,
            resume=segment.resume,
            history_upto=segment.history_upto,
            occurred_at=segment.input_at,
            compact_count=segment.compact_count,
        )

    async def with_models(self, segment: Segment) -> Segment:
        """这一轮用到的模型能不能用、怎么连（模型管理方案设计 7.3）。

        经模型管理平台时：取这个智能体的客户端 Key（每轮都取最新的；没有就补建，「待分配」），
        检查主模型和压缩模型都在可用模型里，用不了就本轮不开始（ModelNotAllowed）。
        端点 = LiteLLM + 这把 Key；调用一律走 Anthropic Messages（LiteLLM 转换）。
        ★ 与权限类型无关：同一个智能体对所有使用者能用的模型相同。
        ★ SubAgent 的模型只换调用方式：它们在自己的 Run 开始时检查（内联的沿用同一个智能体的 Key）。
        """
        s = self._settings
        if not uses_model_platform(s):
            return dataclasses.replace(segment, models=RunModels(direct_endpoint(s)))
        spec = segment.spec
        needed = [spec.model.model]
        if spec.compaction.enabled:
            cfg = spec.compaction
            needed.append(resolve_summarizer_model(cfg.summarizer_model, spec.model.model))
        key = await agent_key(s, segment.thread.agent_uuid, spec.name)
        admit_models(key, needed)
        assert key.key is not None
        spec = dataclasses.replace(
            spec,
            model=with_platform_model(spec.model),
            subagents=tuple(
                dataclasses.replace(sub, model=with_platform_model(sub.model))
                for sub in spec.subagents
            ),
        )
        models = RunModels(key_endpoint(s, key.key), {m.name: m for m in key.models})
        return dataclasses.replace(segment, spec=spec, models=models)

    async def with_knowledge(
        self,
        segment: Segment,
        *,
        run_id: UUID,
        notices: list[tuple[EventType, dict[str, Any]]] | None = None,
    ) -> Segment:
        """auto 模式：先用本轮提问检索，参考资料并入**这一轮**的输入（同 Dify 对话应用的上下文）。

        ★ 只改模型这一轮看到的输入：用户消息早已按原文落库，Transcript 也只收模型与工具的输出，
          所以历史里没有这段资料 —— 下一轮按下一轮的提问重新检索。
        ★ 续跑段（resume）不再检索：本轮输入已在 history 里，没有可并入的位置。
        ★ 检索失败让这一轮明确失败（KnowledgeUnavailable），不静默当作「没查到」（§13.2）。
        ★ 权限主体读不到的库由知识库跳过：其余照常检索，经 notices 告知用户（同 MCP）。
        """
        knowledge = segment.spec.knowledge
        if not knowledge.enabled or knowledge.mode != "auto" or segment.resume:
            return segment
        query = extract_text(segment.input_content).strip()
        if not query:
            return segment
        got = await require_client(self._settings).retrieve(
            knowledge,
            query[:2000],
            source="agent",
            source_ref=f"thread:{segment.thread_id}/run:{run_id}",
        )
        if got.skipped and notices is not None:
            ident = current_identity()
            notices.append(
                (
                    EventType.KNOWLEDGE_SKIPPED,
                    {
                        "dataset_ids": got.skipped,
                        "subject": str(ident.auth_uid) if ident else None,
                        "message": skipped_message(
                            len(got.skipped),
                            acts_as_agent=ident is not None and ident.auth_uid != ident.uid,
                        ),
                    },
                )
            )
        return dataclasses.replace(
            segment, input_content=format_context(segment.input_content, got.records)
        )

    def _titler(self, segment: Segment) -> Titler | None:
        """仅当会话还没有标题时才返回生成器（§8.1）。

        返回 None 时 engine 直接跳过，不产出 thread.title_generated ——
        已有标题的会话不该每轮都重新生成，那既费钱又会覆盖用户改过的名字。
        """
        thread = segment.thread
        if thread.title_source != "pending":
            return None

        question = extract_text(segment.input_content)
        service = TitleService(self._settings, self._build_model)

        async def titler(answer: str) -> dict[str, Any] | None:
            return await service.generate(question, answer)

        return titler

    async def _prompt_cache_ttl(self, segment: Segment) -> Literal["5m", "1h"] | None:
        """开了缓存、且模型实测支持时才打 cache_control —— 编辑器里的开关就是这里生效。

        ★ supports_cache 为假的模型（DeepSeek）塞了 cache_control 也不起作用（迁移 0008 实测），
          它自己的上下文缓存不受影响。
        """
        if not segment.spec.model.prompt_cache:
            return None
        async with self._sessionmaker() as session:
            if not await ModelCatalogRepository(session).supports_cache(segment.spec.model.model):
                return None
        return self._settings.prompt_cache_ttl
