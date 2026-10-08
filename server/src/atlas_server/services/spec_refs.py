"""保存 agent 时解析 spec 里的外部引用（技能 / MCP 设计 §7.2、§9、§11）。

  技能   version=None → 当前 latest；校验存在、未下架、停用不可新增；描述总长预算
  MCP    记录每个引用工具此刻的定义指纹 —— 运行时据此发现「定义变了」

★ 写进 agent_version.spec 的永远是确定的东西：钉死的版本号、保存那一刻的指纹。
  于是历史 run 可精确复现，技能 / 工具升级不会悄悄改变已有配置的含义。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from ..configplane import ConfigPlaneUnavailable, skill_directory
from ..configplane.mcp import make_mcp_catalog
from ..configplane.skills import SkillNotFound
from ..domain.mcp_naming import parse_tool_id
from ..errors import CapabilityUnavailable, DependencyUnavailable, InvalidReference
from ..schemas.agent import AgentSpecIn, SkillRefIn

if TYPE_CHECKING:
    import redis.asyncio as aioredis

    from ..config import Settings

logger = logging.getLogger(__name__)

__all__ = ["resolve_spec_refs", "skill_keys"]


def skill_keys(spec: AgentSpecIn | None) -> set[tuple[str, int]]:
    """spec（含子智能体）里已钉死的全部技能引用。"""
    if spec is None:
        return set()
    refs = [*spec.skills, *(r for sub in spec.subagents for r in sub.skills)]
    return {(r.slug, r.version) for r in refs if r.version is not None}


async def resolve_spec_refs(
    spec: AgentSpecIn,
    *,
    settings: Settings,
    redis: aioredis.Redis | None,
    previous: AgentSpecIn | None = None,
) -> AgentSpecIn:
    keep = skill_keys(previous)
    updates: dict[str, Any] = {
        "skills": await _pin_skills(spec.skills, settings, keep, owner="主智能体"),
        "mcp_tool_digests": await _digests(spec.tool_names, settings, redis),
    }
    subs = []
    for sub in spec.subagents:
        subs.append(
            sub.model_copy(
                update={
                    "skills": await _pin_skills(sub.skills, settings, keep, owner=sub.name),
                    "mcp_tool_digests": await _digests(sub.tool_names, settings, redis),
                }
            )
        )
    updates["subagents"] = subs
    return spec.model_copy(update=updates)


async def _pin_skills(
    refs: list[SkillRefIn], settings: Settings, keep: set[tuple[str, int]], *, owner: str
) -> list[SkillRefIn]:
    if not refs:
        return []
    directory = skill_directory(settings)
    if not directory.configured:
        # 没接配置服务：保持接入前的行为（照 spec 投送），但不再允许没钉死的版本
        missing = [r.slug for r in refs if r.version is None]
        if missing:
            raise InvalidReference(
                "未接入技能目录（CONFIG_BASE_URL），技能引用必须写明版本号", skills=missing
            )
        return refs

    out: list[SkillRefIn] = []
    budget = 0
    try:
        for ref in refs:
            if ref.version is None:
                info = await directory.latest(ref.slug)
                if info is None:
                    raise InvalidReference(f"技能 {ref.slug} 没有已发布的版本", skill=ref.slug)
            else:
                try:
                    info = await directory.get(ref.slug, ref.version)
                except SkillNotFound as exc:
                    raise InvalidReference(str(exc), skill=ref.slug) from exc
            key = (info.slug, info.version)
            if info.status == "revoked":
                raise InvalidReference(
                    f"技能 {info.slug} v{info.version} 已被紧急下架，请移除",
                    skill=info.slug,
                    reason=info.status_reason or "",
                )
            if info.status == "disabled" and key not in keep:
                # ★ 沿用（上一版本已引用）放行 —— 否则引用了停用技能的 agent
                #   再也改不了别的配置
                raise InvalidReference(
                    f"技能 {info.slug} v{info.version} 已停用，不能新增引用", skill=info.slug
                )
            budget += len(info.description)
            out.append(SkillRefIn(slug=info.slug, version=info.version))
    except ConfigPlaneUnavailable as exc:
        # ★ 不能确认技能存在就写进快照，等于允许一份指向空气的配置进库
        raise DependencyUnavailable(f"技能目录暂时不可用，无法保存：{exc}") from exc

    if budget > settings.skill_index_budget_chars:
        raise InvalidReference(
            f"{owner} 挂的技能描述共 {budget} 字符，超过上限 {settings.skill_index_budget_chars}"
            " —— 描述常驻每一轮上下文，请精简或减少技能",
            total=budget,
        )
    return out


async def _digests(
    tool_names: Iterable[str], settings: Settings, redis: aioredis.Redis | None
) -> dict[str, str]:
    wanted: dict[str, list[str]] = {}
    for name in tool_names:
        if parsed := parse_tool_id(name):
            wanted.setdefault(parsed[0], []).append(parsed[1])
    if not wanted:
        return {}
    try:
        catalog = await make_mcp_catalog(settings, redis)
    except ConfigPlaneUnavailable as exc:
        raise DependencyUnavailable(f"MCP 注册表暂时不可用，无法保存：{exc}") from exc

    out: dict[str, str] = {}
    for server, tools in sorted(wanted.items()):
        if catalog.server(server) is None:
            raise InvalidReference(f"MCP server {server} 未注册或已停用", server=server)
        try:
            snap = await catalog.snapshot(server)
        except CapabilityUnavailable as exc:
            # server 暂时连不上且没有缓存：照常保存，只是这几个工具没有指纹
            logger.warning("保存时拿不到 MCP server %s 的工具定义，不记录指纹：%s", server, exc)
            continue
        for tool in tools:
            found = snap.tool(tool)
            if found is None:
                raise InvalidReference(f"MCP server {server} 上没有工具 {tool}", tool=tool)
            if not found.usable:
                raise InvalidReference(
                    f"MCP 工具 {server}/{tool} 不可用：{'；'.join(found.issues)}", tool=tool
                )
            out[f"mcp:{server}:{tool}"] = found.digest
    return out
