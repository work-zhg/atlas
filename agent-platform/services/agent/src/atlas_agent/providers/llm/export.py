"""导出给模型网关的存量迁移（doc/model-design.html §6.3「存量迁移」）。

    python -m atlas_agent export-models > atlas-models.json
    python -m atlas_modelgateway import --file atlas-models.json --owner-account admin --all-users

导出三样：
    models              model_catalog 表里的模型，能力换成模型网关的词汇 —— 把这张表和 spec.py 里
                        按模型名写死的规则（思考关不掉、关思考时档位上限）一起换算过去
    digital_employees   以 Agent 为权限的 Agent：它的数字员工按 Agent 现有配置里用到的模型直接授权
    purposes            平台用途沿用部署配置里的模型（标题、记忆补记、新建 Agent 的默认模型）

另给出 unknown_models：Agent 配置里引用了、但 model_catalog 里没有的模型（迁移前要先补上）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...config import Settings
from ...db.models import Agent, AgentVersion, ModelCatalog
from ...domain import spec as rules
from ...schemas.agent import AgentSpecIn

__all__ = ["export_models"]


def _capabilities(row: ModelCatalog) -> dict[str, Any]:
    return {
        "tools": True,
        "thinking": row.supports_thinking,
        "adaptive_thinking": row.supports_adaptive_thinking,
        "thinking_always_on": row.model in rules._THINKING_ALWAYS_ON,
        "effort": row.supports_effort,
        # 原来对所有模型一样：关闭思考时 xhigh / max 不合法
        "thinking_off_max_effort": "high" if row.supports_effort else None,
        "temperature": row.supports_temperature,
        "prompt_cache": row.supports_cache,
        "min_cacheable_tokens": row.min_cacheable_tokens,
        # Atlas 的调用一律要流式用量（usage.updated）
        "stream_usage": True,
    }


def _models_of(spec: AgentSpecIn) -> set[str]:
    names = {spec.model.model, *(sub.model.model for sub in spec.subagents)}
    if spec.compaction.enabled and spec.compaction.summarizer_model:
        names.add(
            rules.resolve_summarizer_model(spec.compaction.summarizer_model, spec.model.model)
        )
    return names


async def export_models(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
) -> dict[str, Any]:
    async with sessionmaker() as session:
        catalog = list(await session.scalars(select(ModelCatalog).order_by(ModelCatalog.model)))
        agents = (
            await session.execute(
                select(Agent, AgentVersion).join(
                    AgentVersion, AgentVersion.id == Agent.current_version_id
                )
            )
        ).all()
    known = {row.model for row in catalog}
    referenced: set[str] = set()
    digital: list[dict[str, Any]] = []
    for agent, version in agents:
        names = _models_of(AgentSpecIn.model_validate(version.spec))
        referenced |= names
        if agent.auth_subject == "agent" and agent.digital_employee_uid is not None:
            digital.append(
                {
                    "uid": str(agent.digital_employee_uid),
                    "name": agent.name,
                    "agent": agent.slug,
                    "models": sorted(names),
                }
            )
    return {
        "models": [
            {
                "name": row.model,
                "kind": "chat",
                "display_name": row.display_name,
                "protocols": [row.provider],
                "capabilities": _capabilities(row),
                "context_window": row.context_window,
                "max_output_tokens": row.max_output_tokens,
                "available": row.is_available,
            }
            for row in catalog
        ],
        "digital_employees": digital,
        "purposes": {
            "title": settings.summarizer_model,
            "memory_backfill": settings.memory_backfill_model or settings.summarizer_model,
            "agent_default": settings.default_model,
        },
        "unknown_models": sorted(referenced - known),
    }
