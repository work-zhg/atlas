"""原生运行时 · 一轮的编排：为一段装配能力、建图。

见 c4-model/agent/native-runtime/code-native-runtime-turn.html。

CapabilityAssembly 只编排、不实现。顺序固定：模型先于一切（with_models）→ 展开知识集
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
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

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
from atlas_agent.providers.knowledge import resolve_knowledge
from atlas_agent.sandbox.component import SandboxComponent
from atlas_agent.sandbox.native import WorkspaceRef

from ..browser.component import BrowserComponent, refresh_in_background
from ..browser.login import BrowserNotConfigured
from ..browser.plan import BrowserPlan
from ..config import Settings
from ..providers.filesystem import WorkspaceRouting, make_workspace
from ..providers.llm.model_platform import (
    admit_models,
    agent_key,
    for_session,
    key_endpoint,
    sees_images,
    with_platform_model,
)
from ..runtime_config import RuntimeConfig
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
        thread = segment.thread
        # 浏览器（c4-model/agent/browser/design-browser.html）：沙箱建好时附加件导入登录态、
        #   拉起守护；段结束后异步刷新这个人存过的站点的登录态（不拖慢这一轮）
        browser = BrowserComponent.attach(self._settings, sandbox) if sandbox else None
        if browser is not None and segment.spec.sandbox.browser and closers is not None:
            closers.append(_refresher(self._settings, thread))
        ws = WorkspaceRef.of_thread(thread)
        # ★ 用会话的工作目录（子会话与根会话相同），隐藏目录 .atlas/{会话}/ 按自己；技能只放行本
        #   智能体绑定的版本。
        #   没配对象存储 = **没有文件能力**（make_workspace 返回 None，不回落任何内存实现）。
        #   ★ 读写路由跟着**这个人的沙箱**走，不跟着本智能体有没有 bash 走：沙箱把这个人的整个前缀
        #     挂在 /data，它在跑时文件工具也要进沙箱（与命令同一个挂载，只有一个写入方）。
        workspace = make_workspace(
            self._settings,
            thread.owner_uuid,
            segment.thread_id,
            thread.workspace_dir,
            skills=[(s.slug, s.version) for s in segment.spec.skills],
            routing=WorkspaceRouting(sandbox.native, sandbox.exec, ws) if sandbox else None,
            space=thread.space_prefix,
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

        models = models_of(segment)
        endpoint = models.endpoint
        chat = self._build_model(
            segment.spec.model,
            base_url=endpoint.base_url,
            api_key=endpoint.api_key,
            headers=endpoint.headers,
        )
        caps = Capabilities(
            model=chat,
            # 看截图（inspect_screenshot）复用主模型（同一把 Key）：主模型能看图才交，
            # 不看开没开浏览器
            vision_model=chat if models.sees_images(segment.spec.model.model) else None,
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
            knowledge=segment.knowledge.catalog() if segment.knowledge else None,
            prompt_cache_ttl=await self._prompt_cache_ttl(segment),
            workdir=ws.layout().workdir,
            # 每段装配时取一次：跨过零点的会话下一段就换成新的一天
            today=datetime.now(ZoneInfo(self._settings.prompt_timezone)).date(),
            prior_tokens=segment.prior_tokens,
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

        取这个智能体的客户端 Key（每轮都取最新的；没有就补建，「待分配」），
        检查主模型和压缩模型都在可用模型里，用不了就本轮不开始（ModelNotAllowed）。
        端点 = 模型网关 + 这把 Key；调用一律走 Anthropic Messages（线路是别的格式由网关转换）。
        ★ 与权限类型无关：同一个智能体对所有使用者能用的模型相同。
        ★ SubAgent 的模型只换调用方式：它们在自己的 Run 开始时检查（内联的沿用同一个智能体的 Key）。
        """
        s = self._settings
        spec = segment.spec
        needed = [spec.model.model]
        if spec.compaction.enabled:
            cfg = spec.compaction
            needed.append(resolve_summarizer_model(cfg.summarizer_model, spec.model.model))
        key = await agent_key(s, segment.thread.agent_uuid, spec.name)
        admit_models(key, needed)
        assert key.key is not None
        main = with_platform_model(spec.model)
        # ★ 最大输出不超过模型的上限：对话里换了模型时，智能体配置的值是按原来的模型设的，
        #   超了供应商会拒绝整轮
        entry = key.get(main.model)
        if entry is not None and entry.max_output_tokens:
            main = dataclasses.replace(
                main, max_output_tokens=min(main.max_output_tokens, entry.max_output_tokens)
            )
        spec = dataclasses.replace(
            spec,
            model=main,
            subagents=tuple(
                dataclasses.replace(sub, model=with_platform_model(sub.model))
                for sub in spec.subagents
            ),
        )
        # 主模型、看截图、压缩摘要都用这个端点：带上会话头（网关的调用记录、线路粘滞按它）
        endpoint = for_session(key_endpoint(s, key.key), segment.thread_id)
        models = RunModels(endpoint, {m.name: m for m in key.models})
        return dataclasses.replace(segment, spec=spec, models=models)

    async def with_browser(self, segment: Segment) -> Segment:
        """开了浏览器：沙箱规格换成浏览器沙箱镜像、带 browser 属性，技能里加上浏览器技能
        （c4-model/agent/browser/code-browser-host.html · BrowserPlan）。

        ★ 镜像和技能每轮从运行期配置读：管理员改了，下一个新建的沙箱就用新的。部署没有浏览器镜像时
          抛 BrowserNotConfigured，整轮失败并告诉用户（不静默少一个能力）。
        """
        spec = segment.spec
        if not spec.browser:
            return segment
        if not self._settings.sandbox_enabled:
            raise BrowserNotConfigured("开了浏览器，但服务端没有开启沙箱（SANDBOX_ENABLED=false）")
        config = await RuntimeConfig(self._settings, self._sessionmaker).browser_config()
        plan = BrowserPlan.of(spec, config)
        if plan is None:
            return segment
        # 模型能看图再带上 computer use 技能（模型平台登记时标注）
        vision = await sees_images(self._settings, spec.model.model)
        spec = dataclasses.replace(
            spec,
            sandbox=plan.sandbox(spec.sandbox),
            skills=plan.skills(spec.skills, vision=vision),
        )
        return dataclasses.replace(segment, spec=spec)

    async def with_knowledge(
        self,
        segment: Segment,
        *,
        run_id: UUID,
        notices: list[tuple[EventType, dict[str, Any]]] | None = None,
    ) -> Segment:
        """把智能体的知识集按这一轮的权限展开（doc/design/knowledge-set-design.html §4）。

        展开的结果交给后面两处：提示词里的知识集目录（第一层）、knowledge_list / knowledge_search
        两个工具（第二层，工具面）。★ 不做自动检索：查不查、查哪里由模型决定（§2.3）。
        ★ 续跑段（resume）也要展开：工具要照常能用。
        ★ 用不了的知识集、知识库服务不可用，经 notices 告诉用户（knowledge.skipped），
          这一轮照常进行。
        """
        resolution = await resolve_knowledge(self._settings, segment.spec.knowledge)
        if notices is not None:
            notices.extend(resolution.notices())
        if resolution.turn is None:
            return segment
        return dataclasses.replace(segment, knowledge=resolution.turn)

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
        """开了缓存才打 cache_control —— 编辑器里的开关就是这里生效。

        ★ 不看模型支不支持：转成别的格式的上游时网关会去掉缓存标记。
        """
        if not segment.spec.model.prompt_cache:
            return None
        return self._settings.prompt_cache_ttl


def _refresher(settings: Settings, thread: Any) -> Callable[[], Awaitable[None]]:
    """段结束时的收尾：异步刷新登录态（refresh_in_background 自己开资源，不等它做完）。"""

    async def close() -> None:
        refresh_in_background(settings, thread)

    return close
