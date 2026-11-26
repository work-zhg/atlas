"""ToolInvocation：数据面的编排（c4-model/mcpgateway/code-invocation.html），自己不含规则。

    ① 认 Key      Authorization: Bearer <Key>（查库，停用、重置立即生效）  -32042 / -32043
                  Key 说明了是哪个租户：之后都在这个租户里。不查租户状态（不知道它）：
                  租户停用后私有服务由用户中心回答无权限（③），公开服务照常，直到租户被删除
       身份凭据   带了 X-Identity-Grant 就验签，租户要和 Key 的一致       -32047
    ② 服务        在这个租户里存在且启用（没有再找平台服务）      -32040 / -32041
    ③ 判权        公开服务不判；私有服务以 Key 的主体 uid 问授权（短缓存），
                  这把 Key 在这个服务上一点权限都没有就拒绝          -32044 / -32045
    ④ 列工具      只列可用且这把 Key 能用的
    ⑤ 调工具      工具存在且可用 -32046；工具级判权 -32044；按参数定义查必填和类型 -32602；
                  交给上游接入：连不上 -32050，上游回了 JSON-RPC 错误 -32051
    ⑥ 身份        上游收到 X-Auth-Uid = 按谁判（带了身份凭据取凭据的，没带是 Key 的主体）、
                  X-Uid = 使用者（凭据的发起人；没带时用户 Key 是持有人，智能体 Key 是调用方带来的
                  X-Uid）、X-Tenant-Id = Key 所在的租户，以及调用方的 X-Run-Id、X-Thread-Id
                  （没带 X-Thread-Id 时取凭据里的会话）；调用方的 Key 和别的请求头不转发

服务和工具在服务目录，Key 在 API Key，权限在授权，协议转换和上游凭据在上游接入。

★ 每个请求独立，不开长事务：读目录、认 Key 各用一个短会话，转发上游时不占数据库连接。
★ 管理端「试调」走同一段逻辑（admit、execute），判权、校验、错误码都一样。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from atlas_tenancy import SystemReason, TenantScope, tenant_headers
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..access.authorization import Authorization
from ..access.identity import use_outbound
from ..access.key_access import AccessCache, KeyAccess
from ..access.usercenter import UserCenter
from ..catalog.repository import AnyService, AnyTool, ServiceRepository
from ..db.models import McpApiKey
from ..errors import Unavailable
from ..keys.service import ApiKeys
from ..upstream import InvalidArguments, UpstreamError, UpstreamUnavailable
from ..upstream.gateway import UpstreamGateway
from . import trace
from .arguments import check_arguments
from .grant import GrantInvalid, IdentityGrant
from .grant import verify as verify_grant
from .rejection import Reason, Rejection

logger = logging.getLogger(__name__)

__all__ = ["Caller", "CallerIdentity", "ToolInvocation"]


@dataclass(frozen=True)
class CallerIdentity:
    """带给上游的身份，由网关重写。服务关了「传递调用者身份」就什么都不带。"""

    key_uid: str
    #: Atlas 签发的身份凭据（验过的）：有它时按谁判、谁在用都以它为准
    grant: IdentityGrant | None = None
    #: 使用者：用户 Key 是持有人；智能体 Key 是调用方带来的 X-Uid（只作审计，网关不校验）
    user_uid: str | None = None
    #: Key 所在的租户
    tenant: UUID | None = None
    #: 调用方带来的审计头（X-Run-Id、X-Thread-Id），照转给上游
    audit: Mapping[str, str] = field(default_factory=dict)
    pass_identity: bool = True

    def headers(self) -> dict[str, str]:
        if not self.pass_identity:
            return {}
        if self.grant is not None:
            auth, uid = self.grant.auth_uid, self.grant.uid
        else:
            auth, uid = self.key_uid, self.user_uid or self.key_uid
        out = {"X-Auth-Uid": auth, "X-Uid": uid}
        if self.tenant is not None:
            out["X-Tenant-Id"] = str(self.tenant)
        if self.grant is not None and self.grant.thread:
            out["X-Thread-Id"] = self.grant.thread
        return {**out, **self.audit}


@dataclass
class Caller:
    """认过的一次调用：服务、Key、这把 Key 在私有服务上的权限（公开服务为 None = 全部工具）。"""

    service: AnyService
    key: McpApiKey
    access: KeyAccess | None
    user_uid: str | None
    audit: dict[str, str] = field(default_factory=dict)
    grant: IdentityGrant | None = None

    def allows(self, tool: str) -> bool:
        return self.access is None or self.access.allows(self.service.name, tool)

    def identity(self) -> CallerIdentity:
        return CallerIdentity(
            key_uid=str(self.key.subject_uid),
            grant=self.grant,
            user_uid=self.user_uid,
            tenant=self.key.tenant_uuid,
            audit=self.audit,
            pass_identity=self.service.pass_identity,
        )


class ToolInvocation:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        uc: UserCenter,
        cache: AccessCache,
        upstream: UpstreamGateway,
        *,
        grant_secret: str = "",
    ) -> None:
        self._sessions = sessionmaker
        self._uc = uc
        self._cache = cache
        self._upstream = upstream
        #: 验身份凭据的密钥（和 Atlas 共享）；空 = 不认身份凭据
        self._grant_secret = grant_secret

    def _authorization(self, session: AsyncSession) -> Authorization:
        return Authorization(session, self._uc, self._cache, ServiceRepository(session))

    async def admit(
        self,
        service: str,
        secret: str | None,
        *,
        user_uid: str | None,
        audit: Mapping[str, str] | None = None,
        grant: str | None = None,
    ) -> Caller:
        """①–③：先认 Key（它说明了是哪个租户），带了身份凭据就验签，再在这个租户里找服务
        （没有再找平台服务）、判权。不过就抛 Rejection。

        ★ 别的租户的服务即使同名也找不到（SERVICE_NOT_FOUND）。
        ★ 身份凭据只说明身份，不授予服务：判权仍只按 Key 的主体。
        """
        if secret is None:
            raise Rejection(
                Reason.KEY_MISSING,
                "缺少 API Key：请求头要带 Authorization: Bearer <MCP 网关的 API Key>",
            )
        async with self._sessions() as session:
            # 还不知道是哪个租户：按全局唯一的 Key 原文在系统范围里只查这一行
            with TenantScope.system(SystemReason.CREDENTIAL):
                keys = ApiKeys(session, self._authorization(session))
                key = await keys.authenticate(secret)
        if key is None:
            raise Rejection(Reason.KEY_INVALID, "API Key 无效或已停用")
        identity: IdentityGrant | None = None
        if grant:
            try:
                identity = verify_grant(self._grant_secret, grant, tenant=key.tenant_uuid)
            except GrantInvalid as exc:
                raise Rejection(Reason.GRANT_INVALID, f"身份凭据无效：{exc}") from exc
        with TenantScope.enter(key.tenant_uuid):
            async with self._sessions() as session:
                svc = await ServiceRepository(session).find(service)
            if svc is None:
                raise Rejection(Reason.SERVICE_NOT_FOUND, f"MCP 服务 {service} 不存在")
            if svc.status != "enabled":
                raise Rejection(Reason.SERVICE_DISABLED, f"MCP 服务 {service} 已被停用")
            trace.mark(service=service, key_uid=str(key.subject_uid), key_type=key.key_type)
            access: KeyAccess | None = None
            if svc.visibility == "private":
                access = await self._access(key, service)
        holder = str(key.user_uuid) if key.key_type == "user" else (user_uid or None)
        return Caller(svc, key, access, holder, dict(audit or {}), identity)

    async def _access(self, key: McpApiKey, service: str) -> KeyAccess:
        """授权领域的判权缓存（Authorization.access_of 背后的那一份）：不查库，不占连接。"""
        # ★ 调用户中心判权时 X-Auth-Uid = Key 的主体：数据面没有别的身份可透传
        subject = str(key.subject_uid)
        use_outbound({"X-Uid": subject, "X-Auth-Uid": subject, **tenant_headers(key.tenant_uuid)})
        try:
            access = await self._cache.get(subject)
        except Unavailable as exc:
            raise Rejection(
                Reason.AUTHZ_UNAVAILABLE,
                f"暂时无法确认 MCP 服务 {service} 的授权（用户中心不可用），请稍后重试",
            ) from exc
        if not access.touches(service):
            raise Rejection(
                Reason.FORBIDDEN,
                f"这把 API Key 没有被授权使用 MCP 服务 {service}，请联系服务所有者或 MCP 管理员",
                key_uid=subject,
            )
        return access

    # ------------------------------------------------------------------ ④⑤

    async def _callable_tools(self, svc: AnyService) -> list[AnyTool]:
        # ★ MCP SDK 的处理函数跑在会话管理器自己的任务里，拿不到请求的上下文：按服务所属的租户进入。
        #   平台服务不属于任何租户，它的工具在哪儿都查得到
        if svc.platform:
            async with self._sessions() as session:
                return [t for t in await ServiceRepository(session).tools(svc) if t.callable()]
        with TenantScope.enter(svc.tenant_uuid):  # type: ignore[union-attr]
            async with self._sessions() as session:
                return [t for t in await ServiceRepository(session).tools(svc) if t.callable()]

    async def visible_tools(self, caller: Caller) -> list[AnyTool]:
        """按库里的定义，不转发给上游：只列可用（启用、没被删除、合规）且这把 Key 能用的。"""
        return [t for t in await self._callable_tools(caller.service) if caller.allows(t.name)]

    async def execute(
        self, caller: Caller, name: str, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        """⑤：→ MCP 线上格式的 CallToolResult；拒绝抛 Rejection。工具自己失败是 isError 的结果。"""
        svc = caller.service
        trace.mark(tool=name)
        tool = next((t for t in await self._callable_tools(svc) if t.name == name), None)
        if tool is None:
            raise Rejection(
                Reason.TOOL_UNKNOWN, f"MCP 服务 {svc.name} 上没有可用的工具 {name}", tool=name
            )
        if not caller.allows(name):
            raise Rejection(
                Reason.FORBIDDEN,
                f"这把 API Key 没有被授权使用 {svc.name} · {name}，请联系服务所有者或 MCP 管理员",
                tool=name,
                key_uid=str(caller.key.subject_uid),
            )
        problem = check_arguments(tool.input_schema, arguments)
        if problem is not None:
            raise Rejection(Reason.INVALID_PARAMS, problem, tool=name)
        try:
            result = await self._upstream.call(
                svc.upstream_spec(), tool.binding(), arguments, caller.identity().headers()
            )
        except InvalidArguments as exc:
            raise Rejection(Reason.INVALID_PARAMS, f"参数转不成请求消息：{exc}", tool=name) from exc
        except UpstreamError as exc:
            raise Rejection(
                Reason.UPSTREAM_ERROR,
                f"MCP 服务 {svc.name} 的上游返回错误：{exc.message}",
                forwarded=True,
                tool=name,
                upstream_code=exc.code,
            ) from exc
        except UpstreamUnavailable as exc:
            logger.warning("MCP 服务 %s 的上游不可用：%s", svc.name, exc)
            raise Rejection(
                Reason.UPSTREAM_UNAVAILABLE,
                f"MCP 服务 {svc.name} 暂时不可用，请稍后重试",
                tool=name,
            ) from exc
        trace.mark(outcome="tool_error" if result.get("isError") else "ok")
        return result
