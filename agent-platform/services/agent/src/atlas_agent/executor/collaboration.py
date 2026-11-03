"""原生运行时 · 挂起与人机协作。

见 c4-model/agent/native-runtime/code-native-runtime-suspension.html。

审批模式、向用户提问、派子智能体，都是同一种挂起：登记一项等待、本段结束、等到了续跑。
这里为一段接上它们的服务端实现：审批门禁（审批组件）、自动模式的判断器、子智能体发起方
（会话管理的子智能体领域）。中间件本身由 build.py 按槽位交给 MiddlewareStack。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from atlas_engine.kernel.middleware.auto_judge import AutoJudge, Instruction, JudgeSetup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from atlas_agent.domain.spec import ModelSpec
from atlas_agent.domain.translator import extract_text
from atlas_agent.errors import CapabilityUnavailable
from atlas_agent.executor.segment_data import Segment

from ..approval import RunApprovalGate
from ..config import Settings
from ..db.models import Thread
from ..providers.filesystem import make_workspace
from ..providers.filesystem.skill_copy import resolve_skills, seed_session_skills
from ..repositories.model_catalog import ModelCatalogRepository
from ..repositories.run import RunRepository
from ..services.service_models import resolve_service_model, service_model_configured
from ..skills import skill_directory
from ..stream.relay import EventRelay
from .base import SubmitRun
from .run_models import ModelBuilder

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)



@dataclass
class CollaborationContribution:
    """挂起与人机协作为一段交出的东西。"""

    #: ApprovalGate 的实现（审批组件）
    approvals: Any
    #: 自动模式的判断器。None = 智能体不允许自动模式，或没配置判断器
    judge: AutoJudge | None
    #: 派子智能体的入口（SubAgentSpawner）。None = 没有子智能体：内核根本不装 task
    spawner: Any


class Collaboration:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        model_builder: ModelBuilder,
        submit_run: SubmitRun | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._build_model = model_builder
        #: 委派要能起子 run：入队由组合根注入，装配不认识执行器。None = 不能委派
        self._submit_run = submit_run

    def contribute(
        self, segment: Segment, *, run_id: UUID, relay: EventRelay
    ) -> CollaborationContribution:
        return CollaborationContribution(
            approvals=RunApprovalGate(self._sessionmaker, run_id),
            judge=self._auto_judge(segment),
            # ★ SubAgent 不在图内跑子图 —— SubAgentService 在子会话上起子 run，本段挂起。
            spawner=self._subagent_spawner(segment, run_id=run_id, relay=relay),
        )

    def _subagent_spawner(self, segment: Segment, *, run_id: UUID, relay: EventRelay) -> Any:
        """构造本段派 SubAgent 的入口（SubAgentSpawner 的实现）。

        没有 SubAgent 就返回 None —— kernel 据此**根本不装** `task`，
        与「没配沙箱就没有 execute」同一条纪律：模型看不见 = 不会去调。

        ★ 子 run 也走这里：native SubAgent 的 spec 带着同一份名录（spec_for_subagent），
          所以它也能继续派 SubAgent；acp SubAgent 的名录为空，天然返回 None。
        """
        if not segment.spec.subagents or self._submit_run is None:
            return None
        if segment.agent_version_id is None:
            logger.warning("run %s 缺 agent_version_id，SubAgent 不启用", run_id)
            return None

        from ..services.subagent import SubAgentService

        return SubAgentService(
            self._sessionmaker,
            self._settings,
            self._submit_run,
            relay,
            parent_run_id=run_id,
            parent_thread=segment.thread,
            parent_spec=segment.spec,
            agent_version_id=segment.agent_version_id,
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
            thread.created_by_uuid,
            thread.thread_uuid,
            workspace_thread_id=thread.workspace_thread_id,
        )
        if fs is None:
            return
        metas, _skipped = await resolve_skills(
            sub.skills, skill_directory(self._settings), with_descriptions=False
        )
        await seed_session_skills(fs, metas)

    def _auto_judge(self, segment: Segment) -> AutoJudge | None:
        """自动模式的判断器（kernel/middleware/auto_judge.py）。智能体不允许自动模式时不装。

        ★ 模型和用户消息在第一次判断时才取：多数段落用不到判断器，不该为它多一次平台请求。
          模型按平台服务模型配置（service_model「自动模式判断器」）取 Key；没接平台时用部署配置里的
          approval_judge_model（空 = summarizer_model）。
        ★ 没配置 → None（PermissionMiddleware 按没有判断器处理）；
          配了但现在用不了 → 抛错（退回请示）
        """
        if "auto" not in segment.spec.approval.allowed_modes:
            return None
        settings = self._settings
        build_model = self._build_model
        sessionmaker = self._sessionmaker
        thread = segment.thread

        async def setup() -> JudgeSetup | None:
            found = await resolve_service_model(
                settings,
                "approval_judge",
                legacy_model=settings.approval_judge_model or settings.summarizer_model,
            )
            if found is None:
                if not await service_model_configured(settings, "approval_judge"):
                    return None
                raise CapabilityUnavailable("自动模式判断器的模型现在用不了（原因见日志）")
            model = build_model(
                ModelSpec(
                    model=found.model,
                    provider=found.provider,  # type: ignore[arg-type]
                    max_output_tokens=2048,
                ),
                base_url=found.endpoint.base_url,
                api_key=found.endpoint.api_key,
                headers=found.endpoint.headers,
            )
            async with sessionmaker() as session:
                instructions = await _judge_instructions(RunRepository(session), thread)
                # 显式缓存标记只对 Anthropic 有用；DeepSeek 这类自己做前缀缓存
                cache = found.provider == "anthropic" and await ModelCatalogRepository(
                    session
                ).supports_cache(found.model)
            return JudgeSetup(model=model, instructions=instructions, cache=cache)

        return AutoJudge(setup, timeout_s=settings.approval_judge_timeout_s)


async def _judge_instructions(runs: RunRepository, thread: Thread) -> list[Instruction]:
    """判断器眼里的「要求」：用户的消息；子智能体再加上自己会话里的任务书（上级智能体写的）。

    ★ 子会话里 role=user 的消息是派活的智能体写的，不是用户说的 —— 用户的话取根会话的。
    """
    own = [extract_text(m.content) for m in await runs.user_inputs(thread.thread_uuid)]
    root = thread.root_thread_uuid or thread.parent_thread_uuid
    if root is None:
        return [Instruction("user", text) for text in own if text]
    users = [extract_text(m.content) for m in await runs.user_inputs(root)]
    return [
        *(Instruction("user", text) for text in users if text),
        *(Instruction("delegator", text) for text in own if text),
    ]
