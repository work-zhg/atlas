"""授权：给 API Key 分配服务、工具的权限；服务的所有者。都写在用户中心（perms.py）。

    服务级   McpServer / 服务名        → Key 主体 READ：全部工具，含以后新增的
    工具级   McpTool / 服务名/工具名    → Key 主体 READ：只能用这个工具
    所有者   McpServer / 服务名        → 人 OWNER：能给 Key 分配这个服务的权限

一把 Key 在一个服务上只有一种范围：服务级，或者若干个工具。改范围时先收回原来的再写新的。

★ 用户中心要求改授权的人是这条数据的 Owner：
  - McpTool 数据对象第一次工具级授权时才登记，Owner = 服务此刻的所有者（不需要操作人）；
  - 增删服务的所有者时，同步到这个服务已登记的每个工具对象。
  MCP 管理员靠「全部 MCP 服务 / 全部 MCP 工具 · 所有者」（数据编码级授权）。
★ 谁能授权由网关先判（MCP 管理员，或这个服务的所有者），用户中心再按操作人校验一遍。
★ 收回、新授后清掉本进程里这把 Key 的判权缓存；别的实例等缓存过期。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from . import audit
from .db.models import McpApiKey, McpService, McpTool
from .errors import Conflict, Forbidden, Invalid
from .keys import KeyService
from .perms import (
    DATA_SERVER,
    DATA_TOOL,
    AccessCache,
    AclEntry,
    KeyAccess,
    Permissions,
    tool_data_id,
)
from .registry import Registry

__all__ = ["GrantService", "KeyGrant", "ServerGrants"]


@dataclass
class KeyGrant:
    """一把 Key 在一个服务上的权限。scope：all = 服务级；tools = 只有 tools 里这几个。"""

    key: McpApiKey
    scope: str
    tools: list[str] = field(default_factory=list)


@dataclass
class ServerGrants:
    owners: list[AclEntry]
    keys: list[KeyGrant]


class GrantService:
    def __init__(
        self,
        registry: Registry,
        keys: KeyService,
        perms: Permissions,
        access: AccessCache,
    ) -> None:
        self.registry = registry
        self.keys = keys
        self.perms = perms
        self.access = access

    # ── 谁能授权

    async def can_assign(self, operator: str, service: str, *, manager: bool) -> bool:
        if manager:
            return True
        owned, everything = await self.perms.owned(operator)
        return everything or service in owned

    async def require_assign(self, operator: str, service: str, *, manager: bool) -> None:
        if not await self.can_assign(operator, service, manager=manager):
            raise Forbidden(f"只有 MCP 服务 {service} 的所有者或 MCP 管理员可以授权")

    # ── 查询

    async def owners(self, svc: McpService) -> list[AclEntry]:
        return [
            e
            for e in await self.perms.acl(DATA_SERVER, svc.name)
            if e.level == "OWNER" and e.subject_type == "USER"
        ]

    async def of_server(self, svc: McpService) -> ServerGrants:
        """一个服务的所有者，以及授给了哪些 Key（服务级、工具级）。"""
        by_subject = await self.keys.by_subjects()
        acl = await self.perms.acl(DATA_SERVER, svc.name)
        owners = [e for e in acl if e.level == "OWNER" and e.subject_type == "USER"]
        grants: dict[str, KeyGrant] = {}
        for e in acl:
            key = by_subject.get(e.subject_id)
            if key is not None and e.level == "READ":
                grants[str(key.uuid)] = KeyGrant(key, "all")
        registered = [t for t in await self.registry.tools(svc) if t.uc_registered]
        tool_acls = await asyncio.gather(
            *(self.perms.acl(DATA_TOOL, tool_data_id(svc.name, t.name)) for t in registered)
        )
        for tool, rows in zip(registered, tool_acls, strict=True):
            for e in rows:
                key = by_subject.get(e.subject_id)
                if key is None or e.level != "READ":
                    continue
                row = grants.setdefault(str(key.uuid), KeyGrant(key, "tools"))
                if row.scope == "tools":
                    row.tools.append(tool.name)
        return ServerGrants(
            owners, sorted(grants.values(), key=lambda g: (g.key.key_type, g.key.name))
        )

    async def of_key(self, key: McpApiKey, *, fresh: bool = False) -> KeyAccess:
        if fresh:
            self.access.forget(str(key.subject_uid))
        return await self.access.get(str(key.subject_uid))

    # ── 给 Key 分配

    async def set_key(
        self,
        svc: McpService,
        key: McpApiKey,
        *,
        scope: str,
        tools: Sequence[str],
        operator: str,
    ) -> None:
        """把这把 Key 在这个服务上的权限设成 scope（all / tools / none）。"""
        if svc.visibility == "public" and scope != "none":
            raise Invalid("公开服务不需要授权：任何有效的 Key 都能用全部工具")
        subject = str(key.subject_uid)
        all_tools = {t.name: t for t in await self.registry.tools(svc)}
        wanted = list(dict.fromkeys(tools)) if scope == "tools" else []
        if scope == "tools":
            unknown = [n for n in wanted if n not in all_tools]
            if unknown:
                raise Invalid(f"MCP 服务 {svc.name} 上没有工具：{'、'.join(unknown)}")
            if not wanted:
                raise Invalid("请至少选一个工具")
        elif scope not in ("all", "none"):
            raise Invalid("范围只能是 all（全部工具）、tools（指定工具）或 none（收回）")

        # 服务级
        server_rows = [
            e for e in await self.perms.acl(DATA_SERVER, svc.name) if e.subject_id == subject
        ]
        if scope == "all" and not server_rows:
            await self.perms.grant(DATA_SERVER, svc.name, operator, level="READ", users=[subject])
        if scope != "all":
            for e in server_rows:
                await self.perms.revoke(e.id, operator)
        # 工具级
        owners: list[str] | None = None
        for tool in all_tools.values():
            data_id = tool_data_id(svc.name, tool.name)
            rows: list[AclEntry] = []
            if tool.uc_registered:
                rows = [
                    e for e in await self.perms.acl(DATA_TOOL, data_id) if e.subject_id == subject
                ]
            if tool.name in wanted:
                if rows:
                    continue
                if tool.uc_registered:
                    await self.perms.grant(
                        DATA_TOOL, data_id, operator, level="READ", users=[subject]
                    )
                else:
                    if owners is None:
                        owners = [e.subject_id for e in await self.owners(svc)]
                    await self._register_tool(svc, tool, owners=owners, readers=[subject])
            else:
                for e in rows:
                    await self.perms.revoke(e.id, operator)
        self.access.forget(subject)
        audit.record(
            self.registry.session,
            actor=operator,
            action="grant.set",
            target=f"{svc.name}:{key.uuid}",
            scope=scope,
            tools=wanted,
        )

    async def _register_tool(
        self, svc: McpService, tool: McpTool, *, owners: list[str], readers: list[str]
    ) -> None:
        data_id = tool_data_id(svc.name, tool.name)
        name = f"{svc.display_name or svc.name} · {tool.name}"
        try:
            await self.perms.register(DATA_TOOL, data_id, name, owners=owners, readers=readers)
        except Conflict:
            # 用户中心里有同名的、网关不知道的数据对象（工具删掉又加回来、旧版本留下的）：清掉重来
            await self.perms.drop(DATA_TOOL, data_id)
            await self.perms.register(DATA_TOOL, data_id, name, owners=owners, readers=readers)
        tool.uc_registered = True

    # ── 所有者

    async def add_owner(self, svc: McpService, user: str, *, operator: str) -> None:
        info = await self.perms.user(user)
        if info is None or info.get("status") != "active":
            raise Invalid("用户中心里找不到这个用户，或已停用")
        if info.get("user_type") == "digital":
            raise Invalid("所有者必须是人")
        for data_code, data_id in await self._objects(svc):
            current = await self.perms.acl(data_code, data_id)
            if not any(e.subject_id == user and e.level == "OWNER" for e in current):
                await self.perms.grant(data_code, data_id, operator, level="OWNER", users=[user])
        audit.record(
            self.registry.session, actor=operator, action="owner.add", target=svc.name, user=user
        )

    async def remove_owner(self, svc: McpService, user: str, *, operator: str) -> None:
        for data_code, data_id in await self._objects(svc):
            for e in await self.perms.acl(data_code, data_id):
                if e.subject_id == user and e.level == "OWNER":
                    await self.perms.revoke(e.id, operator)
        audit.record(
            self.registry.session, actor=operator, action="owner.remove", target=svc.name, user=user
        )

    async def _objects(self, svc: McpService) -> Iterable[tuple[str, str]]:
        tools = [t for t in await self.registry.tools(svc, include_removed=True) if t.uc_registered]
        return [
            (DATA_SERVER, svc.name),
            *((DATA_TOOL, tool_data_id(svc.name, t.name)) for t in tools),
        ]
