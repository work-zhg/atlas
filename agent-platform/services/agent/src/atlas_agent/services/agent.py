"""智能体服务（文档 §11.1）。

核心语义：**保存即产生新版本**。PATCH 从不原地改 spec，而是写一条
agent_version 并切换 current_version_id —— 这样每条历史 run 都能指回
"当时用的哪份配置"（§5.4）。
"""

from __future__ import annotations

import builtins
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from atlas_engine.contracts.mcp_naming import parse_tool_id
from atlas_engine.contracts.spec import resolve_summarizer_model
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.agent_access import AgentAccess, Visibility
from ..auth.catalog import AGENT_DATA
from ..auth.digital import AgentIdentity
from ..auth.principal import LEVEL_RANK
from ..auth.uc import UCError
from ..db.models import Agent, AgentVersion
from ..errors import (
    CapabilityUnavailable,
    DependencyUnavailable,
    InvalidReference,
    NotFound,
    SlugTaken,
)
from ..providers.knowledge import KnowledgeUnavailable, readable_as
from ..providers.llm.model_platform import (
    ModelPlatformUnavailable,
    agent_key,
    platform_catalog,
    register_agent,
    rename_agent,
    uses_model_platform,
)
from ..providers.mcp import make_mcp_catalog, mcp_agent_access
from ..providers.mcp.gateway import delete_agent_key, register_agent_key, rename_agent_key
from ..repositories.agent import AgentRepository
from ..schemas.agent import (
    AgentCreate,
    AgentDetailOut,
    AgentGrantCreate,
    AgentGrantPatch,
    AgentIdentityOut,
    AgentListOut,
    AgentOut,
    AgentSpecIn,
    AgentUpdate,
    AgentVersionListOut,
    AgentVersionOut,
    DigitalEmployeeOut,
    KnowledgeAccessOut,
)
from .spec_refs import resolve_spec_refs

if TYPE_CHECKING:
    import redis.asyncio as aioredis

    from ..auth.principal import Principal
    from ..auth.uc import UCClient
    from ..config import Settings

logger = logging.getLogger(__name__)


def _summarize(agent: Agent, version: AgentVersion) -> AgentOut:
    spec: dict[str, Any] = version.spec
    model = (spec.get("model") or {}).get("model", "")
    tools = spec.get("tool_names") or []
    return AgentOut(
        id=agent.agent_uuid,
        slug=agent.slug,
        name=agent.name,
        description=agent.description,
        avatar_key=agent.avatar_key,
        status=agent.status,  # type: ignore[arg-type]
        version=version.version,
        model=model,
        tool_count=len(spec.get("tool_names") or []),
        subagent_count=len(spec.get("subagents") or []),
        kind=spec.get("kind") or "native",
        builtin_tool_count=sum(1 for t in tools if not str(t).startswith("mcp:")),
        skill_count=len(spec.get("skills") or []),
        mcp_servers=sorted({p[0] for t in tools if (p := parse_tool_id(str(t))) is not None}),
        subagents=[
            {
                "name": sub.get("name", ""),
                "kind": sub.get("kind") or "native",
                "permission_mode": (sub.get("cli") or {}).get("permission_mode"),
            }
            for sub in spec.get("subagents") or []
            if isinstance(sub, dict)
        ],
        auth_subject=agent.auth_subject or "user",  # type: ignore[arg-type]
        digital_employee_uid=agent.digital_employee_uid,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
        has_draft=agent.draft_spec is not None,
    )


def _level_of(vis: Visibility | None, agent: Agent) -> str | None:
    """列表里每个智能体的有效级别：单条授权与编码级授权取大（与 AgentAccess.level 同一口径）。"""
    if vis is None:
        return None
    if vis.all_:
        return "OWNER"
    own = vis.levels.get(agent.agent_uuid, "NONE")
    return max(own, vis.type_level, key=lambda lv: LEVEL_RANK.get(lv, 0))


class AgentService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings | None = None,
        redis: aioredis.Redis | None = None,
        uc: UCClient | None = None,
    ) -> None:
        self._session = session
        self._repo = AgentRepository(session)
        #: 保存时解析外部引用要用（技能目录、MCP 目录）。None = 不解析 ——
        #: 只给不经 API 的内部调用（迁移种子等）留的口子。
        self._settings = settings
        self._redis = redis
        #: 用户中心（数字员工、AtlasAgent 数据权限）。None = 不经接口的内部调用，没有这些
        self._uc = uc
        self.access = AgentAccess(uc)
        self._identity = AgentIdentity(uc)

    # ------------------------------------------------------------------ 读

    async def list(
        self,
        *,
        q: str | None,
        status: str | None,
        model: str | None,
        limit: int,
        principal: Principal | None = None,
        scope: str | None = None,
    ) -> AgentListOut:
        """只列看得到的（数字员工设计 §8.1）：已启用要 READ、草稿要 WRITE、已归档要 OWNER。

        scope：mine = 我创建的；shared = 别人创建、共享给我的。每条带上我的级别、创建者、
        我最近一次用它的时间 —— 列表按「和我的关系」组织，不按类型。
        """
        vis = await self.access.visibility(principal) if principal else None
        uid = principal.uid if principal else None
        rows = await self._repo.list(
            q=q,
            status=status,
            model=model,
            limit=limit,
            visible=vis.condition() if vis else None,
            created_by=uid if scope == "mine" else None,
            not_created_by=uid if scope == "shared" else None,
        )
        owners = await self._repo.owner_names([a.created_by_uuid for a, _ in rows])
        used = (
            await self._repo.last_used([a.agent_uuid for a, _ in rows], user_id=uid)
            if uid
            else {}
        )
        out: builtins.list[AgentOut] = []
        for a, v in rows:
            item = _summarize(a, v)
            item.my_level = _level_of(vis, a)
            item.mine = uid is not None and a.created_by_uuid == uid
            item.owner_name = owners.get(a.created_by_uuid)
            item.last_used_at = used.get(a.agent_uuid)
            out.append(item)
        return AgentListOut(data=out)

    async def get(self, agent_id: UUID, *, principal: Principal | None = None) -> AgentDetailOut:
        agent, version = await self._require(agent_id)
        level = await self.access.require(principal, agent) if principal else None
        out = AgentDetailOut(
            **_summarize(agent, version).model_dump(),
            spec=version.spec,
            draft_spec=agent.draft_spec,
            draft_updated_at=agent.draft_updated_at,
        )
        out.my_level = level
        out.mine = principal is not None and agent.created_by_uuid == principal.uid
        return out

    async def agent(self, agent_id: UUID) -> Agent:
        return (await self._require(agent_id))[0]

    async def list_versions(self, agent_id: UUID) -> AgentVersionListOut:
        await self._require(agent_id)
        versions = await self._repo.list_versions(agent_id)
        return AgentVersionListOut(
            data=[
                AgentVersionOut(
                    id=v.agent_version_uuid,
                    version=v.version,
                    spec=v.spec,
                    created_at=v.created_at,
                    note=v.note,
                )
                for v in versions
            ]
        )

    # ------------------------------------------------------------------ 写

    async def create(
        self, payload: AgentCreate, *, user_id: UUID, principal: Principal | None = None
    ) -> AgentDetailOut:
        if await self._repo.get_by_slug(payload.slug):
            raise SlugTaken(f"标识 {payload.slug!r} 已被占用", slug=payload.slug)

        if payload.auth_subject == "agent":
            self._identity.require()
        spec = await self._resolve(payload.spec, previous=None)
        if principal is not None:
            # 以 Agent 为权限：数字员工随创建才建出来，还没有任何授权（subject=None）
            subject = principal.auth_uid if payload.auth_subject == "user" else None
            await self._check_knowledge(spec, previous=None, subject=subject)
        await self._check_mcp(spec, previous=None, agent=None)
        await self._check_models(spec, previous=None, agent=None)
        # ★ 在这里就跑 engine 的校验：opus-5 传 temperature 之类的错误
        #   在点保存时被拒，而不是等到某次 run（§3 D3）
        spec.to_engine(slug=payload.slug, name=payload.name).validate()

        agent, version = await self._repo.create(
            slug=payload.slug,
            name=payload.name,
            description=payload.description,
            avatar_key=payload.avatar_key,
            spec=spec.model_dump(mode="json"),
            created_by=user_id,
            auth_subject=payload.auth_subject,
        )
        # 数字员工 + 登记数据对象（创建者 = OWNER）。失败抛 503，整个创建随请求事务回滚
        await self._identity.ensure_digital_employee(agent, owner=user_id)
        await self._identity.register(agent, owner=user_id)
        await self._session.flush()
        # 模型管理平台上的客户端 Key（「待分配」，管理员分配模型后才能运行）。尽力而为：
        # 平台暂时不可用不挡住建智能体，编辑器、Run 开始时会补建
        if self._settings is not None:
            await register_agent(self._settings, agent.agent_uuid, agent.name)
            # MCP 网关上的 Key（只能调公开服务，私有服务要所有者给它分配）。同样尽力而为
            await register_agent_key(self._settings, agent.agent_uuid, agent.name, owner=user_id)
        await self._session.refresh(agent)  # updated_at 是 onupdate：flush 后要重新读
        out = AgentDetailOut(**_summarize(agent, version).model_dump(), spec=version.spec)
        out.my_level, out.mine = "OWNER", True
        return out

    async def update(
        self,
        agent_id: UUID,
        payload: AgentUpdate,
        *,
        user_id: UUID,
        principal: Principal | None = None,
        note: str = "",
    ) -> AgentDetailOut:
        agent, current = await self._require(agent_id)
        renamed = payload.name is not None and payload.name != agent.name

        if payload.name is not None:
            agent.name = payload.name
        if payload.description is not None:
            agent.description = payload.description
        if payload.avatar_key is not None:
            agent.avatar_key = payload.avatar_key

        version = current
        if payload.spec is not None:
            previous = AgentSpecIn.model_validate(current.spec)
            spec = await self._resolve(payload.spec, previous=previous)
            if principal is not None:
                subject = agent.digital_employee_uid if agent.acts_as_itself else principal.auth_uid
                await self._check_knowledge(spec, previous=previous, subject=subject)
            await self._check_mcp(spec, previous=previous, agent=agent)
            await self._check_models(spec, previous=previous, agent=agent)
            spec.to_engine(slug=agent.slug, name=agent.name).validate()
            version = await self._repo.add_version(
                agent=agent, spec=spec.model_dump(mode="json"), created_by=user_id, note=note
            )
        else:
            # 只改了元信息也要刷新 updated_at，列表页按它排序
            await self._session.flush()
        if renamed:
            await self._identity.renamed(agent)
            if self._settings is not None:
                await rename_agent(self._settings, agent.agent_uuid, agent.name)
                await rename_agent_key(self._settings, agent.agent_uuid, agent.name)

        await self._session.refresh(agent)
        return AgentDetailOut(**_summarize(agent, version).model_dump(), spec=version.spec)

    # ------------------------------------------------------------------ 草稿 / 发布 / 调试预览

    async def save_draft(self, agent_id: UUID, spec: AgentSpecIn) -> None:
        """编辑器自动保存：只存，不产生版本。引用、权限与引擎规则留到发布时校验。"""
        agent, _ = await self._require(agent_id)
        await self._repo.save_draft(agent, spec.model_dump(mode="json"))

    async def discard_draft(self, agent_id: UUID) -> None:
        agent, _ = await self._require(agent_id)
        await self._repo.save_draft(agent, None)

    async def publish(
        self, agent_id: UUID, *, note: str, user_id: UUID, principal: Principal | None = None
    ) -> AgentDetailOut:
        """草稿 → 新版本（与保存同一套校验），清掉草稿；草稿状态的智能体同时启用。

        没有草稿时不写重复的版本：草稿状态的就只是启用。
        """
        agent, _ = await self._require(agent_id)
        if agent.draft_spec is not None:
            await self.update(
                agent_id,
                AgentUpdate(spec=AgentSpecIn.model_validate(agent.draft_spec)),
                user_id=user_id,
                principal=principal,
                note=note,
            )
            await self._repo.save_draft(agent, None)
        if agent.status == "draft":
            await self.set_status(agent_id, "enabled")
        return await self.get(agent_id, principal=principal)

    async def preview_version(self, agent_id: UUID, *, user_id: UUID) -> UUID:
        """调试预览的一轮用哪份配置：按草稿（没有就是当前版本）拍一个快照。

        ★ 和发布走同一套引用解析与引擎校验：配置有误就当场 400，而不是跑到一半才失败。
          知识库、MCP、模型的授权不在这里查 —— 运行时照常按权限判（读不到的跳过并提示）。
        """
        agent, current = await self._require(agent_id)
        if agent.draft_spec is None:
            return current.agent_version_uuid
        previous = AgentSpecIn.model_validate(current.spec)
        spec = await self._resolve(AgentSpecIn.model_validate(agent.draft_spec), previous=previous)
        spec.to_engine(slug=agent.slug, name=agent.name).validate()
        version = await self._repo.preview_version(
            agent=agent, spec=spec.model_dump(mode="json"), created_by=user_id
        )
        return version.agent_version_uuid

    async def set_status(self, agent_id: UUID, status: str) -> AgentDetailOut:
        agent, version = await self._require(agent_id)
        before, agent.status = agent.status, status
        await self._session.flush()
        await self._identity.status_changed(agent, before=before)
        await self._session.refresh(agent)
        return AgentDetailOut(**_summarize(agent, version).model_dump(), spec=version.spec)

    async def delete(self, agent_id: UUID) -> dict[str, Any]:
        """归档语义，不是物理删除 —— 理由见 AgentRepository.soft_delete。"""
        agent, _ = await self._require(agent_id)
        before = agent.status
        archived = await self._repo.archive_threads_of(agent.agent_uuid)
        await self._repo.soft_delete(agent)
        await self._identity.status_changed(agent, before=before)
        if self._settings is not None:
            await delete_agent_key(self._settings, agent.agent_uuid)
        return {"archived": True, "archived_threads": archived}

    # ------------------------------------------------------------------ 权限类型与身份

    async def set_auth_subject(
        self, agent_id: UUID, auth_subject: str, *, user_id: UUID
    ) -> AgentDetailOut:
        """切换权限类型（OWNER，接口层校验）。只影响之后新建的 run：进行中的沿用它记下的
        auth_uid（数字员工设计 §5.2）。切到「以 Agent 为权限」时没有数字员工就补建。"""
        agent, version = await self._require(agent_id)
        if auth_subject == "agent":
            self._identity.require()
            await self._identity.ensure_digital_employee(agent, owner=agent.created_by_uuid)
        if agent.auth_subject != auth_subject:
            logger.info(
                "agent=%s 权限类型 %s → %s（操作人 %s）",
                agent.agent_uuid,
                agent.auth_subject,
                auth_subject,
                user_id,
            )
            agent.auth_subject = auth_subject
        await self._session.flush()
        await self._session.refresh(agent)
        return AgentDetailOut(**_summarize(agent, version).model_dump(), spec=version.spec)

    async def identity(self, agent_id: UUID) -> AgentIdentityOut:
        """「身份」页签：数字员工、权限类型；以 Agent 为权限时列出所配知识库数字员工能不能读。"""
        agent, version = await self._require(agent_id)
        out = AgentIdentityOut(auth_subject=agent.auth_subject)  # type: ignore[arg-type]
        if agent.digital_employee_uid is not None:
            de: dict[str, Any] = {"id": agent.digital_employee_uid}
            if self._uc is not None:
                try:
                    info = await self._uc.digital_employee(agent.digital_employee_uid)
                    de |= {k: info.get(k) for k in ("account", "name", "status", "owner_id")}
                except UCError as exc:
                    if exc.status_code != 404:
                        raise
                    out.notices.append("用户中心里找不到这个数字员工（可能被删除），请联系管理员")
            out.digital_employee = DigitalEmployeeOut(**de)
        elif agent.acts_as_itself:
            out.notices.append("还没有数字员工：依赖授权的工具都不可用，请联系管理员回填")
        dataset_ids = list(AgentSpecIn.model_validate(version.spec).knowledge.dataset_ids)
        out.knowledge = [KnowledgeAccessOut(id=d) for d in dataset_ids]
        if agent.acts_as_itself and agent.digital_employee_uid is not None and dataset_ids:
            try:
                readable = await self._readable_as(agent.digital_employee_uid, dataset_ids)
            except KnowledgeUnavailable:
                readable = None
            if readable is not None:
                names = {d["id"]: d.get("name") for d in readable}
                out.knowledge = [
                    KnowledgeAccessOut(id=d, name=names.get(d), readable=d in names)
                    for d in dataset_ids
                ]
                if any(k.readable is False for k in out.knowledge):
                    out.notices.append(
                        "数字员工读不到部分知识库：运行时这些库不可用，请库的所有者共享给它"
                    )
        return out

    async def _readable_as(
        self, auth_uid: UUID, dataset_ids: builtins.list[str]
    ) -> builtins.list[dict] | None:
        """权限主体能读哪些库。没接知识库 → None；知识库不可用抛 KnowledgeUnavailable。"""
        if self._settings is None:
            return None
        return await readable_as(self._settings, auth_uid, dataset_ids)

    # ------------------------------------------------------------------ 共享（AtlasAgent）

    def _uc_or_conflict(self) -> UCClient:
        from ..errors import Conflict

        if self._uc is None:
            raise Conflict("未接入用户中心，不能共享")
        return self._uc

    async def grants(self, agent_id: UUID) -> builtins.list[dict[str, Any]]:
        agent = await self.agent(agent_id)
        if self._uc is None:
            return []
        return await self._uc.data_acl(AGENT_DATA, str(agent.agent_uuid))

    async def add_grants(self, agent_id: UUID, body: AgentGrantCreate, *, operator: UUID) -> None:
        agent = await self.agent(agent_id)
        uc = self._uc_or_conflict()
        await uc.data_grant(
            AGENT_DATA,
            str(agent.agent_uuid),
            [
                {
                    "permission": body.level,
                    "subject_type": s.type.upper(),
                    "subject_id": s.id,
                    "include_sub": body.include_sub and s.type == "dept",
                }
                for s in body.subjects
            ],
            data_name=agent.name,
            operator_id=operator,
        )

    async def _own_acl(self, agent_id: UUID, acl_id: str) -> None:
        """授权行必须属于这个 Agent —— 用户中心只校验它属于本应用的数据编码。"""
        if acl_id not in {str(a["id"]) for a in await self.grants(agent_id)}:
            raise NotFound("授权不存在")

    async def update_grant(
        self, agent_id: UUID, acl_id: str, body: AgentGrantPatch, *, operator: UUID
    ) -> None:
        uc = self._uc_or_conflict()
        await self._own_acl(agent_id, acl_id)
        await uc.data_acl_update(acl_id, operator, level=body.level, include_sub=body.include_sub)

    async def revoke_grant(self, agent_id: UUID, acl_id: str, *, operator: UUID) -> None:
        uc = self._uc_or_conflict()
        await self._own_acl(agent_id, acl_id)
        await uc.data_acl_delete(acl_id, operator)

    # ------------------------------------------------------------------ 内部

    async def _resolve(self, spec: AgentSpecIn, *, previous: AgentSpecIn | None) -> AgentSpecIn:
        """钉死技能版本、记录 MCP 工具指纹（技能 / MCP 设计 §11）。

        界面上新建的 CLI 智能体只给 cli_type：镜像、适配器用部署配置里的默认值。
        """
        if self._settings is None:
            return spec
        cli = spec.cli
        if spec.kind == "cli" and cli is not None and not (cli.image and cli.adapter):
            spec = spec.model_copy(
                update={
                    "cli": spec.cli.model_copy(
                        update={
                            "image": spec.cli.image or self._settings.cli_default_image,
                            "adapter": spec.cli.adapter
                            or self._settings.cli_default_adapters.get(spec.cli.cli_type, ""),
                        }
                    )
                }
            )
        return await resolve_spec_refs(
            spec, settings=self._settings, redis=self._redis, previous=previous
        )

    async def _check_knowledge(
        self, spec: AgentSpecIn, *, previous: AgentSpecIn | None, subject: UUID | None
    ) -> None:
        """新绑定的知识库，权限主体必须能读（同 MCP，MCP 方案设计 §5.4）。

        ★ 绑定不是授权：运行时知识库按这一轮的 X-Auth-Uid 再判一次，读不到的库跳过并提示。
          这里先挡住「绑了也用不了」的库。
        ★ 只查**新增**的：沿用旧版本已绑定的库不重新判 —— 授权被收回以后，连改提示词都
          保存不了就太过了；运行时会跳过并提示。
        ★ 主体：以人为权限 = 保存的人；以 Agent 为权限 = 它的数字员工（subject=None：
          还没有数字员工，新绑定一律拒绝）。
        """
        if self._settings is None:
            return
        before = set(previous.knowledge.dataset_ids) if previous else set()
        added = [d for d in spec.knowledge.dataset_ids if d not in before]
        if not added:
            return
        if subject is None:
            raise InvalidReference(
                "以 Agent 为权限的 Agent 要先保存（建出数字员工），把知识库共享给数字员工后再绑定",
                dataset_ids=",".join(added),
            )
        try:
            readable = await self._readable_as(subject, added)
        except KnowledgeUnavailable as exc:
            raise DependencyUnavailable(str(exc)) from exc
        if readable is None:
            raise InvalidReference("服务端未接入知识库服务，不能绑定知识库")
        ok = {d["id"] for d in readable}
        missing = [d for d in added if d not in ok]
        if missing:
            raise InvalidReference(
                "权限主体读不到这些知识库（不存在或没有读取权限），请库的所有者共享后再绑定",
                dataset_ids=",".join(missing),
            )

    async def _check_mcp(
        self, spec: AgentSpecIn, *, previous: AgentSpecIn | None, agent: Agent | None
    ) -> None:
        """新加的 MCP 工具，这个智能体的 Key 要能用：公开服务的，或授给这把 Key 的。

        ★ 只校验**新加**的：旧版本已经选了的不重判 —— 授权被收回以后，连改提示词都
          保存不了就太过了；运行时会跳过并提示。
        ★ 新建的智能体还没有 Key：只能选公开服务。保存后在 MCP 网关给它的 Key 分配私有服务。
        ★ 与智能体的权限类型无关：Key 跟着智能体走。
        """
        if self._settings is None or not self._settings.mcp_gateway_url:
            return
        added = sorted(_mcp_tools(spec) - (_mcp_tools(previous) if previous else set()))
        if not added:
            return
        try:
            if agent is None:
                allowed = (await make_mcp_catalog(self._settings, self._redis)).public_access()
            else:
                found = await mcp_agent_access(
                    self._settings, agent.agent_uuid, name=agent.name, owner=agent.created_by_uuid
                )
                allowed = dict(found.servers)
        except CapabilityUnavailable as exc:
            raise DependencyUnavailable(f"MCP 网关暂时不可用，无法保存：{exc}") from exc
        denied = [f"{s}__{t}" for s, t in added if t not in allowed.get(s, frozenset())]
        if denied:
            where = (
                "新建的智能体只能选公开的 MCP 服务（保存后在 MCP 网关给它的 Key 分配私有服务）"
                if agent is None
                else "这个智能体的 MCP Key 没有被授权使用这些工具"
            )
            raise InvalidReference(
                f"{where}：{', '.join(denied)}，请联系服务所有者或 MCP 管理员",
                tools=",".join(denied),
            )

    async def _check_models(
        self, spec: AgentSpecIn, *, previous: AgentSpecIn | None, agent: Agent | None
    ) -> None:
        """新选的或换了的模型（模型管理方案设计 7.2）：经模型管理平台时，智能体的客户端 Key 已经
        分配了模型 → 新选的要在它的可用模型里；还没分配（新建、待分配、停用）→ 新选的要是平台上
        登记了的模型（分配之前它不能运行，Run 开始时会说清楚）。

        ★ 只校验**新选**的：旧版本已经选着的不重判 —— 模型被移出 Key 以后，连改提示词都保存不了
          就太过了；运行时会明确报出来。
        """
        if self._settings is None or not uses_model_platform(self._settings):
            return
        added = sorted(_models_of(spec) - (_models_of(previous) if previous else set()))
        if not added:
            return
        try:
            key = await agent_key(self._settings, agent.agent_uuid, agent.name) if agent else None
            if key is not None and key.status == "active":
                allowed = {m.name for m in key.models}
                where = "这个智能体的模型 Key 里"
            else:
                allowed = {m.name for m in await platform_catalog(self._settings)}
                where = "模型管理平台上"
        except ModelPlatformUnavailable as exc:
            raise DependencyUnavailable(f"模型管理平台暂时不可用，无法保存：{exc}") from exc
        denied = [m for m in added if m not in allowed]
        if denied:
            raise InvalidReference(
                f"{where}没有模型：{', '.join(denied)}，请换一个，或联系模型管理员",
                models=",".join(denied),
            )

    async def _require(self, agent_id: UUID) -> tuple[Agent, AgentVersion]:
        found = await self._repo.get(agent_id)
        if found is None:
            raise NotFound(f"智能体 {agent_id} 不存在", agent_id=str(agent_id))
        return found


def _models_of(spec: AgentSpecIn) -> set[str]:
    """spec 用到的对话模型：主模型、压缩模型（配了的话）、子智能体的模型（沿用父 Run 的主体）。"""
    names = {spec.model.model, *(sub.model.model for sub in spec.subagents)}
    if spec.compaction.enabled and spec.compaction.summarizer_model:
        names.add(resolve_summarizer_model(spec.compaction.summarizer_model, spec.model.model))
    return names


def _mcp_tools(spec: AgentSpecIn) -> set[tuple[str, str]]:
    """spec（含子智能体）引用的 MCP 工具：(服务, 线上名)。子智能体用同一把 Key。"""
    names = [*spec.tool_names, *(t for sub in spec.subagents for t in sub.tool_names)]
    return {p for name in names if (p := parse_tool_id(str(name))) is not None}


__all__ = ["AgentService", "AgentSpecIn"]
