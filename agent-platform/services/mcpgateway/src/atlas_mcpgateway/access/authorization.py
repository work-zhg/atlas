"""Authorization：授权的唯一入口（c4-model/mcpgateway/code-access.html）。

回答三件事：一个主体（Key）能用哪些服务和工具；一个人能不能管理网关、能给哪些服务授权；给主体
授权、增删所有者。还替 API Key 在用户中心登记、停用权限主体。授权本身都在用户中心。

    服务级   McpServer / 服务名        → 主体 READ：全部工具，含以后新增的
    工具级   McpTool / 服务名/工具名    → 主体 READ：只能用这个工具
    所有者   McpServer / 服务名        → 人 OWNER：能给 Key 分配这个服务的权限

★ 只认主体 uid，不认识 Key：授权列表给出「主体 → 范围」，由管理接口换成 Key。
★ 一个主体在一个服务上只有一种范围：服务级，或者若干个工具。改范围时先收回再授予（中途失败只会
  比目标少，不会多）。
★ 用户中心要求改授权的人是这条数据的 Owner：McpTool 第一次工具级授权时才登记，Owner = 服务此刻
  的所有者；增删服务的所有者时同步到这个服务登记过的每个工具对象。MCP 管理员靠「全部 MCP 服务 /
  全部 MCP 工具 · 所有者」（数据编码级授权）。
★ 谁能授权由网关先判（MCP 管理员，或这个服务的所有者），用户中心再按操作人校验一遍。
★ 收回、新授后清掉本进程里这个主体的判权缓存；别的实例等缓存过期。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..catalog.repository import ServiceRepository
from ..db.models import McpService
from ..errors import Conflict, Forbidden, Invalid, Unauthorized
from .key_access import AccessCache, KeyAccess
from .objects import DataObjectRegistry
from .usercenter import DATA_SERVER, DATA_TOOL, AclEntry, UserCenter, tool_data_id

__all__ = ["MCP_MANAGE", "Authorization", "Operator", "ServerGrants", "SubjectScope"]

#: 登记、修改、停用服务；改连接、上游凭据和工具；管理全部 API Key。
#: 给 Key 分配某个服务的权限不靠操作码，靠该服务的「所有者」数据权限
MCP_MANAGE = "mcp:manage"


@dataclass(frozen=True)
class Operator:
    """管理端的操作人：X-Uid 谁在用，X-Auth-Uid 按谁判。"""

    uid: str
    auth_uid: str
    operations: frozenset[str] = frozenset()
    #: 是哪些服务的所有者（Authorization.with_ownership 之后才有）
    owned: frozenset[str] | None = None
    #: 有「全部 MCP 服务 · 所有者」
    owns_all: bool = False

    @property
    def can_manage(self) -> bool:
        return MCP_MANAGE in self.operations

    def can_assign(self, service: str) -> bool:
        """能不能给这个服务授权：MCP 管理员，或它的所有者。"""
        if self.can_manage:
            return True
        if self.owned is None:
            raise RuntimeError("先用 Authorization.with_ownership 取所有者信息")
        return self.owns_all or service in self.owned

    @property
    def grants_anything(self) -> bool:
        """MCP 管理员，或至少是一个服务的所有者。"""
        return self.can_manage or self.owns_all or bool(self.owned)


@dataclass(frozen=True)
class SubjectScope:
    """一个主体在一个服务上的范围：all = 服务级；tools = 只有 tools 里这几个。"""

    subject: str
    scope: str
    tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServerGrants:
    owners: list[AclEntry]
    subjects: list[SubjectScope]


class Authorization:
    def __init__(
        self,
        session: AsyncSession,
        uc: UserCenter,
        cache: AccessCache,
        services: ServiceRepository,
    ) -> None:
        self._s = session
        self._uc = uc
        self._cache = cache
        self._services = services
        self._objects = DataObjectRegistry(session)

    # ------------------------------------------------------------------ 判权

    async def access_of(self, subject: str, *, fresh: bool = False) -> KeyAccess:
        """这个主体能用什么（缓存）。用户中心不可用、缓存又过期时抛 Unavailable。"""
        if fresh:
            self._cache.forget(subject)
        return await self._cache.get(subject)

    # ------------------------------------------------------------------ 管理端身份

    async def operator(self, uid: str, auth_uid: str, tenant: UUID) -> Operator:
        """授权快照里的操作码。不在这个租户里、账号停用 → 401。"""
        snap = await self._uc.authz(auth_uid, tenant)
        if snap.tenant is not None and snap.tenant != tenant:
            raise Unauthorized("请求头里的租户和用户所属的不一致")
        return Operator(uid=uid, auth_uid=auth_uid, operations=snap.operations)

    async def with_ownership(self, op: Operator) -> Operator:
        """补上这个人是哪些服务的所有者（每次都问用户中心，不缓存）。"""
        if op.owned is not None:
            return op
        owned, owns_all = await self._uc.owned(op.auth_uid)
        return replace(op, owned=owned, owns_all=owns_all)

    async def require_assign(self, op: Operator, service: str) -> Operator:
        op = op if op.can_manage else await self.with_ownership(op)
        if not op.can_assign(service):
            raise Forbidden(f"只有 MCP 服务 {service} 的所有者或 MCP 管理员可以授权")
        return op

    async def require_grantor(self, op: Operator) -> Operator:
        op = op if op.can_manage else await self.with_ownership(op)
        if not op.grants_anything:
            raise Forbidden("只有服务所有者或 MCP 管理员可以授权")
        return op

    # ------------------------------------------------------------------ 查询

    async def owners(self, svc: McpService) -> list[AclEntry]:
        return [
            e
            for e in await self._uc.acl(DATA_SERVER, svc.name)
            if e.level == "OWNER" and e.subject_type == "USER"
        ]

    async def grants_of(self, svc: McpService) -> ServerGrants:
        """一个服务的所有者，以及授给了哪些主体（服务级、工具级）。"""
        acl = await self._uc.acl(DATA_SERVER, svc.name)
        owners = [e for e in acl if e.level == "OWNER" and e.subject_type == "USER"]
        scopes: dict[str, tuple[str, list[str]]] = {
            e.subject_id: ("all", []) for e in acl if e.level == "READ"
        }
        tools = [o for o in await self._objects.of_service(svc.uuid) if o.data_code == DATA_TOOL]
        tool_acls = await asyncio.gather(*(self._uc.acl(DATA_TOOL, o.data_id) for o in tools))
        for obj, rows in zip(tools, tool_acls, strict=True):
            tool = obj.data_id.partition("/")[2]
            for e in rows:
                if e.level != "READ":
                    continue
                scope, names = scopes.setdefault(e.subject_id, ("tools", []))
                if scope == "tools":
                    names.append(tool)
        return ServerGrants(
            owners,
            [SubjectScope(s, scope, tuple(sorted(names))) for s, (scope, names) in scopes.items()],
        )

    # ------------------------------------------------------------------ 给主体授权

    async def set_scope(
        self,
        svc: McpService,
        subject: str,
        *,
        scope: str,
        tools: list[str],
        operator: str,
    ) -> None:
        """把这个主体在这个服务上的权限设成 scope（all / tools / none）。"""
        if svc.visibility == "public" and scope != "none":
            raise Invalid("公开服务不需要授权：任何有效的 Key 都能用全部工具")
        all_tools = [t.name for t in await self._services.tools(svc)]
        wanted = list(dict.fromkeys(tools)) if scope == "tools" else []
        if scope == "tools":
            unknown = [n for n in wanted if n not in all_tools]
            if unknown:
                raise Invalid(f"MCP 服务 {svc.name} 上没有工具：{'、'.join(unknown)}")
            if not wanted:
                raise Invalid("请至少选一个工具")
        elif scope not in ("all", "none"):
            raise Invalid("范围只能是 all（全部工具）、tools（指定工具）或 none（收回）")

        # 服务级：改成服务级时先授（它已经覆盖全部工具），其余先收回
        server_rows = [
            e for e in await self._uc.acl(DATA_SERVER, svc.name) if e.subject_id == subject
        ]
        if scope == "all" and not server_rows:
            await self._uc.grant(DATA_SERVER, svc.name, operator, level="READ", users=[subject])
        if scope != "all":
            for e in server_rows:
                await self._uc.revoke(e.id, operator)
        # 工具级
        registered = {
            o.data_id for o in await self._objects.of_service(svc.uuid) if o.data_code == DATA_TOOL
        }
        owners: list[str] | None = None
        for name in all_tools:
            data_id = tool_data_id(svc.name, name)
            rows: list[AclEntry] = []
            if data_id in registered:
                rows = [
                    e for e in await self._uc.acl(DATA_TOOL, data_id) if e.subject_id == subject
                ]
            if name in wanted:
                if rows:
                    continue
                if data_id in registered:
                    await self._uc.grant(
                        DATA_TOOL, data_id, operator, level="READ", users=[subject]
                    )
                else:
                    if owners is None:
                        owners = [e.subject_id for e in await self.owners(svc)]
                    await self._register_tool(svc, name, owners=owners, readers=[subject])
            else:
                for e in rows:
                    await self._uc.revoke(e.id, operator)
        self._cache.forget(subject)
        audit.record(
            self._s,
            actor=operator,
            action="grant.set",
            target=f"{svc.name}:{subject}",
            scope=scope,
            tools=wanted,
        )

    async def _register_tool(
        self, svc: McpService, tool: str, *, owners: list[str], readers: list[str]
    ) -> None:
        data_id = tool_data_id(svc.name, tool)
        name = f"{svc.display_name or svc.name} · {tool}"
        try:
            await self._uc.register(DATA_TOOL, data_id, name, owners=owners, readers=readers)
        except Conflict:
            # 用户中心里有同名的、网关不知道的数据对象（工具删掉又加回来、旧版本留下的）：清掉重来
            await self._uc.drop(DATA_TOOL, data_id)
            await self._uc.register(DATA_TOOL, data_id, name, owners=owners, readers=readers)
        await self._objects.add(DATA_TOOL, data_id, svc.uuid)

    # ------------------------------------------------------------------ 所有者

    async def add_owner(self, svc: McpService, user: str, *, operator: str) -> None:
        info = await self._uc.user(user)
        if info is None or info.get("status") != "active":
            raise Invalid("用户中心里找不到这个用户，或已停用")
        if info.get("user_type") == "digital":
            raise Invalid("所有者必须是人")
        for data_code, data_id in await self._data_objects(svc):
            current = await self._uc.acl(data_code, data_id)
            if not any(e.subject_id == user and e.level == "OWNER" for e in current):
                await self._uc.grant(data_code, data_id, operator, level="OWNER", users=[user])
        audit.record(self._s, actor=operator, action="owner.add", target=svc.name, user=user)

    async def remove_owner(self, svc: McpService, user: str, *, operator: str) -> None:
        for data_code, data_id in await self._data_objects(svc):
            for e in await self._uc.acl(data_code, data_id):
                if e.subject_id == user and e.level == "OWNER":
                    await self._uc.revoke(e.id, operator)
        audit.record(self._s, actor=operator, action="owner.remove", target=svc.name, user=user)

    async def _data_objects(self, svc: McpService) -> list[tuple[str, str]]:
        """服务的 McpServer，加上它登记过的每个 McpTool。"""
        tools = [o for o in await self._objects.of_service(svc.uuid) if o.data_code == DATA_TOOL]
        return [(DATA_SERVER, svc.name), *((DATA_TOOL, o.data_id) for o in tools)]

    # ------------------------------------------------------------------ 权限主体（API Key 用）

    async def register_subject(self, *, ref: str, name: str, owner: str) -> str:
        """按 ref 幂等登记（数字员工）。→ 主体 uid。"""
        return await self._uc.create_subject(ref=ref, name=name, owner=owner)

    async def set_subject_active(self, uid: str, active: bool) -> None:
        """停用后用户中心对它一律回「无权限」。"""
        await self._uc.set_subject_active(uid, active)

    async def person(self, uid: str) -> dict[str, Any] | None:
        return await self._uc.user(uid)

    async def search_people(self, q: str) -> list[dict[str, Any]]:
        return await self._uc.search_users(q)
