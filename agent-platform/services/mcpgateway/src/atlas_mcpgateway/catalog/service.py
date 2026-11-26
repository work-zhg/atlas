"""ServiceCatalog：服务目录的唯一入口（c4-model/mcpgateway/code-catalog.html）。

登记服务：① 校验连接与凭据 → ② 发现工具（上游接入：MCP 拉 tools/list；REST 解析 OpenAPI；gRPC
反射 / 描述文件）→ ③ 存库 → ④ 发 service_registered（授权在用户中心登记 McpServer，登记人成为
所有者；失败则整个登记回滚）。

    MCP 代理      工具定义从上游拉来，全部登记；定期重新拉取（维护任务），也可以手动刷新
                  （DefinitionSync）。上游删掉的工具标成已删除
    REST / gRPC  工具由管理员挑选、编辑；改动保存即生效

★ 不认识用户中心：登记、改名、删工具时发目录事件（CatalogEvents），用户中心里的数据对象由授权去改。
★ 不连上游：连接、凭据、映射转成值对象交给上游接入（McpService.upstream_spec、McpTool.binding）。
★ 网络 I/O（发现、拉取）在锁外做；写库时锁住服务行，多个实例的刷新串行。
★ 平台服务（ServiceCatalog(…, platform=True)，只有运维命令行这样建）：登记一次，所有租户可见；
  只能是 REST、gRPC 转换，都是公开的，不在用户中心登记数据对象，不记租户的管理审计。租户的目录里
  看得到、改不了（写操作一律 403），租户也不能再建同名服务。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from atlas_tenancy import SystemReason, TenantScope
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..db.models import McpService, McpTool
from ..db.types import utcnow
from ..errors import Conflict, Forbidden, Invalid
from ..settings import GatewaySettings
from ..upstream import DiscoveryFailed, UpstreamUnavailable
from ..upstream import grpc as grpc_up
from ..upstream import rest as rest_up
from ..upstream.credentials import UpstreamCredential
from ..upstream.gateway import UpstreamGateway
from ..upstream.mcp import TRANSPORTS
from ..upstream.spec import Discovered, UpstreamSpec
from .definitions import (
    SERVICE_NAME_PATTERN,
    ToolDefinition,
    annotations_for,
    content_hash,
    define,
    normalize,
    schema_from_params,
)
from .events import CatalogEvents
from .repository import AnyService, ServiceRepository
from .sync import DefinitionSync, tool_from

__all__ = ["Preview", "ServiceCatalog", "validate_connection"]

logger = logging.getLogger(__name__)

_SERVICE_NAME = re.compile(SERVICE_NAME_PATTERN)
_ADDRESS = re.compile(r"^[\w.-]+:\d{1,5}$")
_DISCOVERY = ("reflection", "descriptor")


def _http_url(value: Any, label: str, *, required: bool = True) -> str:
    url = str(value or "").strip()
    if not url and not required:
        return ""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise Invalid(f"{label}要以 http:// 或 https:// 开头")
    return url


def validate_connection(kind: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    """接口给的连接 → 存库的形状；不合规抛 Invalid。"""
    if kind == "mcp":
        transport = str(raw.get("transport") or "streamable")
        if transport not in TRANSPORTS:
            raise Invalid("传输只能是 streamable（Streamable HTTP）或 sse（旧版协议）")
        return {"url": _http_url(raw.get("url"), "MCP Server 地址"), "transport": transport}
    if kind == "rest":
        return {
            "base_url": _http_url(raw.get("base_url"), "Base URL"),
            "spec_url": _http_url(raw.get("spec_url"), "OpenAPI 地址", required=False),
        }
    if kind == "grpc":
        address = str(raw.get("address") or "").strip()
        if not _ADDRESS.match(address):
            raise Invalid("gRPC 地址写成 host:port")
        tls = str(raw.get("tls") or "plaintext")
        if tls not in grpc_up.TLS_MODES:
            raise Invalid("传输安全只能是 plaintext、tls、mtls")
        discovery = str(raw.get("discovery") or "reflection")
        if discovery not in _DISCOVERY:
            raise Invalid("方法发现只能是 reflection（服务端反射）或 descriptor（上传描述文件）")
        return {"address": address, "tls": tls, "discovery": discovery, "services": []}
    raise Invalid(f"不支持的服务类型 {kind}")


def _defined(
    service: str, name: str, description: str, schema: Mapping[str, Any], effect: str
) -> ToolDefinition:
    return define(service, name, description, schema, annotations_for(effect))


@dataclass
class Preview:
    """登记前、重新导入时给管理员看的：上游接入发现的，加上 MCP 代理规范化后的工具定义。"""

    found: Discovered
    definitions: list[ToolDefinition] = field(default_factory=list)


class ServiceCatalog:
    def __init__(
        self,
        session: AsyncSession,
        settings: GatewaySettings,
        upstream: UpstreamGateway,
        events: CatalogEvents,
        *,
        platform: bool = False,
    ) -> None:
        self.session = session
        self.settings = settings
        self.upstream = upstream
        self.events = events
        #: 平台服务的目录（运维命令行）；否则是当前租户的
        self.platform = platform
        self.repo = ServiceRepository(session, platform=platform)
        self.sync = DefinitionSync(self.repo, upstream, settings)

    # ------------------------------------------------------------------ 读目录

    async def list(self) -> list[McpService]:
        """本租户的服务（平台目录：全部平台服务）。智能体配置里选服务用它：平台服务另有入口。"""
        return await self.repo.list()  # type: ignore[return-value]

    async def platform_services(self) -> list[AnyService]:
        """平台服务：租户的管理员看得到、改不了。"""
        return await self.repo.platform_services()  # type: ignore[return-value]

    async def find(self, name: str) -> McpService | None:
        return await self.repo.find(name)

    async def get(self, name: str) -> McpService:
        return await self.repo.get(name)

    async def tools(self, svc: McpService, *, include_removed: bool = False) -> list[McpTool]:
        return await self.repo.tools(svc, include_removed=include_removed)

    async def callable_tools(self, svc: McpService) -> list[McpTool]:
        return [t for t in await self.repo.tools(svc) if t.callable()]

    # ------------------------------------------------------------------ 发现（预览）

    async def discover(
        self,
        *,
        kind: str,
        connection: Mapping[str, Any],
        credential: Mapping[str, Any],
        secret: str | None,
        descriptor: bytes | None,
        service: str = "preview",
        previous: McpService | None = None,
    ) -> Preview:
        """登记前、重新导入时给管理员看：能转换成哪些工具。不写库。"""
        conn = validate_connection(kind, connection)
        if previous is not None and not secret:
            secret = previous.credential_secret
        cred = UpstreamCredential.validate(
            kind, credential, secret, previous=previous.credential if previous else None
        )
        if kind == "grpc" and conn["discovery"] == "descriptor" and descriptor is None:
            if previous is not None and previous.descriptor is not None:
                descriptor = previous.descriptor
            else:
                raise Invalid("请上传描述文件（FileDescriptorSet）")
        spec = UpstreamSpec(kind, conn, UpstreamCredential.of(cred, secret), descriptor)
        await self._check_addresses(spec)
        try:
            found = await self.upstream.discover(spec)
        except DiscoveryFailed as exc:
            raise Invalid(str(exc), code="DISCOVERY_FAILED") from exc
        return Preview(found, [normalize(service, t) for t in found.tools])

    async def _check_addresses(self, spec: UpstreamSpec) -> None:
        """登记、改连接、改凭据时：租户填的地址不能指向平台内部（upstream/guard.py）。"""
        try:
            await self.upstream.check_addresses(spec)
        except UpstreamUnavailable as exc:  # 含 UpstreamBlocked
            raise Invalid(str(exc), code="UPSTREAM_ADDRESS") from exc

    # ------------------------------------------------------------------ 登记 / 修改

    async def register(
        self,
        *,
        kind: str,
        name: str,
        display_name: str,
        description: str,
        visibility: str,
        connection: Mapping[str, Any],
        credential: Mapping[str, Any],
        secret: str | None,
        pass_identity: bool,
        selected: Sequence[str],
        descriptor: bytes | None,
        actor: str,
    ) -> McpService:
        if not _SERVICE_NAME.match(name):
            raise Invalid("服务名：小写字母开头，2–24 位小写字母、数字、连字符")
        existing = await self.repo.find(name)
        if existing is not None and existing.platform and not self.platform:
            raise Conflict(f"服务名 {name} 被平台服务占用了，换一个", code="SERVICE_EXISTS")
        if existing is not None or (self.platform and await self._taken_by_a_tenant(name)):
            raise Conflict(
                f"MCP 服务 {name} 已存在（服务名是工具前缀，停用后也不能复用）",
                code="SERVICE_EXISTS",
            )
        if self.platform:
            if kind == "mcp":
                raise Invalid("平台服务只能是 REST、gRPC 转换（工具在登记时定好，不从上游拉）")
            visibility = "public"
        if visibility not in ("public", "private"):
            raise Invalid("可见性只能是 public 或 private")
        conn = validate_connection(kind, connection)
        cred = UpstreamCredential.validate(kind, credential, secret, previous=None)
        preview = await self.discover(
            kind=kind,
            connection=conn,
            credential=cred,
            secret=secret,
            descriptor=descriptor,
            service=name,
        )
        tools = self._pick(name, preview, selected)
        if kind == "grpc":
            conn["services"] = preview.found.services
        row = self.repo.service_model(
            name=name,
            display_name=display_name or name,
            description=description,
            kind=kind,
            visibility=visibility,
            connection=conn,
            credential=cred,
            credential_secret=secret or None,
            descriptor=preview.found.descriptor,
            pass_identity=pass_identity,
            status="enabled",
            created_by=actor,
            updated_by=actor,
        )
        self.session.add(row)
        await self.session.flush()
        for t in tools:
            t.service_uuid = row.uuid
            self.session.add(t)
        if kind == "mcp":
            row.fetched_at = utcnow()
            row.content_hash = content_hash(t.digest for t in tools)
        await self.session.flush()
        if not self.platform:
            # 授权在用户中心登记 McpServer；失败则异常往上抛，整个登记回滚。
            # 平台服务都是公开的，不登记数据对象
            await self.events.service_registered(row, actor)
        self._audit(
            actor=actor,
            action="service.create",
            target=name,
            kind=kind,
            visibility=visibility,
            tools=[t.name for t in tools],
        )
        return row

    async def _taken_by_a_tenant(self, name: str) -> bool:
        """平台服务的名字全平台保留：哪个租户已经用了这个名字就不能登记（运维命令行，系统范围）。"""
        with TenantScope.system(SystemReason.OPS):
            hit = await self.session.scalar(
                select(McpService.id).where(McpService.name == name).limit(1)
            )
        return hit is not None

    async def _editable(self, name: str) -> AnyService:
        """要改的服务。租户的目录里，平台服务看得到、改不了。"""
        row = await self.repo.get(name)
        if row.platform and not self.platform:
            raise Forbidden(f"{name} 是平台服务：只能由运维命令行修改")
        return row

    def _audit(self, *, actor: str, action: str, target: str, **detail: Any) -> None:
        """管理审计记在租户里；平台服务的改动（运维命令行）只写日志。"""
        if self.platform:
            logger.info("平台服务 %s：%s（%s）%s", target, action, actor, detail)
            return
        audit.record(self.session, actor=actor, action=action, target=target, **detail)

    def _pick(self, service: str, preview: Preview, selected: Sequence[str]) -> list[McpTool]:
        found = preview.found
        if found.kind == "mcp":
            return [tool_from(d, self.repo.tool_model) for d in preview.definitions]
        wanted = list(dict.fromkeys(selected))
        if found.kind == "rest":
            by_key = {o.key: o for o in found.operations}
            ops = [by_key.get(k) for k in wanted]
            missing = [k for k, o in zip(wanted, ops, strict=True) if o is None]
            if missing:
                raise Invalid(f"接口定义里没有这些操作：{'、'.join(missing)}")
            bad = [o.key for o in ops if o is not None and o.unsupported]
            if bad:
                raise Invalid(f"这些操作不支持转换：{'、'.join(bad)}")
            if found.operations and not wanted:
                raise Invalid("请至少选一个接口")
            return [
                self._rest_tool(service, o.name, o.description, o.effect, o.mapping())
                for o in ops
                if o is not None
            ]
        by_key = {m.key: m for m in found.methods}
        methods = [by_key.get(k) for k in wanted]
        missing = [k for k, m in zip(wanted, methods, strict=True) if m is None]
        if missing:
            raise Invalid(f"描述里没有这些方法：{'、'.join(missing)}")
        bad = [m.key for m in methods if m is not None and m.unsupported]
        if bad:
            raise Invalid(f"这些方法不支持转换：{'、'.join(bad)}")
        if not wanted:
            raise Invalid("请至少选一个方法")
        return [self._grpc_tool(service, m) for m in methods if m is not None]

    def _rest_tool(
        self, service: str, name: str, description: str, effect: str, mapping: Mapping[str, Any]
    ) -> McpTool:
        clean = rest_up.validate(mapping)
        d = _defined(service, name, description, schema_from_params(clean["params"]), effect)
        tool = tool_from(d, self.repo.tool_model)
        tool.mapping, tool.effect = clean, effect
        return tool

    def _grpc_tool(self, service: str, m: grpc_up.Method) -> McpTool:
        d = _defined(service, m.name, f"gRPC {m.key}", m.input_schema, m.effect)
        tool = tool_from(d, self.repo.tool_model)
        tool.mapping, tool.effect = m.mapping(), m.effect
        return tool

    async def update(self, name: str, changes: Mapping[str, Any], *, actor: str) -> McpService:
        """基本信息：显示名称、描述、可见性。改了显示名称发 service_renamed。"""
        row = await self._editable(name)
        if "visibility" in changes and changes["visibility"] not in ("public", "private"):
            raise Invalid("可见性只能是 public 或 private")
        renamed = bool(changes.get("display_name")) and changes["display_name"] != row.display_name
        for key in ("display_name", "description", "visibility"):
            if key in changes and changes[key] is not None:
                setattr(row, key, changes[key])
        row.updated_by = actor
        if renamed:
            await self.events.service_renamed(row)
        self._audit(actor=actor, action="service.update", target=name, changes=dict(changes))
        return row

    async def change_connection(
        self, name: str, connection: Mapping[str, Any], *, pass_identity: bool, actor: str
    ) -> McpService:
        """MCP 代理改了连接立即重新拉一次。"""
        row = await self._editable(name)
        conn = validate_connection(row.kind, connection)
        await self._check_addresses(
            UpstreamSpec(row.kind, conn, UpstreamCredential.of(row.credential, None))
        )
        if row.kind == "grpc":
            conn["services"] = row.connection.get("services") or []
        row.connection, row.pass_identity, row.updated_by = conn, pass_identity, actor
        self._audit(actor=actor, action="service.connection", target=name, connection=conn)
        if row.kind == "mcp":
            await self.refresh(row)
        return row

    async def change_credential(
        self, name: str, credential: Mapping[str, Any], secret: str | None, *, actor: str
    ) -> McpService:
        row = await self._editable(name)
        cred = UpstreamCredential.validate(row.kind, credential, secret, previous=row.credential)
        await self._check_addresses(
            UpstreamSpec(row.kind, row.connection, UpstreamCredential.of(cred, None))
        )
        if secret:
            row.credential_secret = secret
        elif cred["type"] == "none":
            row.credential_secret = None
        row.credential, row.updated_by = cred, actor
        self._audit(actor=actor, action="service.credential", target=name, type=cred["type"])
        return row

    async def set_enabled(self, name: str, enabled: bool, *, actor: str) -> McpService:
        """停用在网关立即生效：所有调用都经过网关。"""
        row = await self._editable(name)
        row.status = "enabled" if enabled else "disabled"
        row.updated_by = actor
        self._audit(
            actor=actor,
            action="service.enable" if enabled else "service.disable",
            target=name,
        )
        return row

    # ------------------------------------------------------------------ MCP 代理：刷新定义

    async def claim_due(self) -> list[tuple[str, UUID]]:
        return await self.sync.claim_due()

    async def refresh(self, row: McpService, *, manual: bool = False) -> McpService:
        removed = await self.sync.refresh(row, manual=manual)
        if removed:
            await self.events.tools_removed(row, removed)
        return row

    # ------------------------------------------------------------------ REST / gRPC：重新导入

    async def reimport(
        self,
        name: str,
        selected: Sequence[str],
        *,
        descriptor: bytes | None,
        actor: str,
    ) -> list[str]:
        """重新导入（REST）/ 重新发现（gRPC）后，把挑中的、还没有的工具加进来。→ 加了哪些。

        gRPC 同时换成新的描述（已有工具的映射按类型名引用，新描述里没了的类型调用时会报错）。
        """
        row = await self._editable(name)
        if row.kind == "mcp":
            raise Invalid("MCP 代理的工具从上游拉取，用「立即刷新」")
        preview = await self.preview_of(row, descriptor=descriptor)
        existing = {t.name for t in await self.repo.tools(row, include_removed=True)}
        tools = [t for t in self._pick(row.name, preview, selected) if t.name not in existing]
        for t in tools:
            t.service_uuid = row.uuid
            self.session.add(t)
        if row.kind == "grpc":
            row.descriptor = preview.found.descriptor
            row.connection = {**row.connection, "services": preview.found.services}
        await self.session.flush()
        self._audit(
            actor=actor,
            action="service.import",
            target=name,
            tools=[t.name for t in tools],
        )
        return [t.name for t in tools]

    async def preview_of(self, row: McpService, *, descriptor: bytes | None = None) -> Preview:
        """按保存的连接和凭据发现一次（测试连接、重新发现）。不写库。"""
        return await self.discover(
            kind=row.kind,
            connection=row.connection,
            credential=row.credential,
            secret=None,
            descriptor=descriptor,
            service=row.name,
            previous=row,
        )

    # ------------------------------------------------------------------ 工具

    async def add_tool(
        self,
        name: str,
        *,
        tool_name: str,
        description: str,
        effect: str,
        mapping: Mapping[str, Any],
        actor: str,
    ) -> McpTool:
        """REST：手工添加一个接口。"""
        row = await self._editable(name)
        if row.kind != "rest":
            raise Invalid("只有 REST 转换的服务能手工添加接口")
        rest_up.check_name(tool_name)
        if not description.strip():
            raise Invalid("请填写描述")
        if effect not in ("read", "write", "destructive"):
            raise Invalid("副作用只能是 read、write、destructive")
        if any(t.name == tool_name for t in await self.repo.tools(row, include_removed=True)):
            raise Conflict(f"工具名 {tool_name} 已存在")
        tool = self._rest_tool(row.name, tool_name, description.strip(), effect, mapping)
        tool.service_uuid = row.uuid
        self.session.add(tool)
        await self.session.flush()
        self._audit(actor=actor, action="tool.create", target=f"{name}/{tool_name}")
        return tool

    async def edit_tool(
        self,
        name: str,
        tool_name: str,
        *,
        description: str | None,
        effect: str | None,
        mapping: Mapping[str, Any] | None,
        actor: str,
    ) -> McpTool:
        """REST / gRPC：改描述、副作用（REST 还能改映射）。工具名不能改：它是智能体配置里的
        引用、用户中心里工具级授权的数据标识。"""
        row = await self._editable(name)
        if row.kind == "mcp":
            raise Invalid("MCP 代理的工具定义来自上游，不能在网关里改")
        tool = await self.repo.tool(row, tool_name)
        if effect is not None and effect not in ("read", "write", "destructive"):
            raise Invalid("副作用只能是 read、write、destructive")
        if description is not None and not description.strip():
            raise Invalid("描述不能为空")
        new_desc = description.strip() if description is not None else tool.description
        new_effect = effect or tool.effect
        if row.kind == "rest":
            new_mapping = rest_up.validate(
                {**(tool.mapping or {}), **mapping} if mapping is not None else tool.mapping or {}
            )
            schema = schema_from_params(new_mapping["params"])
        else:
            new_mapping, schema = tool.mapping, tool.input_schema
        d = _defined(row.name, tool.name, new_desc, schema, new_effect)
        if d.digest != tool.digest:
            tool.previous = {
                "description": tool.description,
                "input_schema": tool.input_schema,
                "annotations": tool.annotations,
                "digest": tool.digest,
            }
            tool.changed_at = utcnow()
        tool.description, tool.input_schema, tool.annotations = d.description, schema, d.annotations
        tool.digest, tool.issues, tool.effect, tool.mapping = (
            d.digest,
            list(d.issues),
            new_effect,
            new_mapping,
        )
        self._audit(actor=actor, action="tool.update", target=f"{name}/{tool_name}")
        return tool

    async def delete_tool(self, name: str, tool_name: str, *, actor: str) -> None:
        """REST / gRPC：删掉一个工具；发 tools_removed（授权删掉用户中心里它的对象和授权）。"""
        row = await self._editable(name)
        if row.kind == "mcp":
            raise Invalid("MCP 代理的工具来自上游，不能删除，可以停用")
        tool = await self.repo.tool(row, tool_name)
        await self.session.delete(tool)
        await self.session.flush()
        await self.events.tools_removed(row, [tool_name])
        self._audit(actor=actor, action="tool.delete", target=f"{name}/{tool_name}")

    async def set_tool_status(
        self, name: str, tool_name: str, *, active: bool, actor: str
    ) -> McpTool:
        row = await self._editable(name)
        tool = await self.repo.tool(row, tool_name)
        tool.status = "active" if active else "disabled"
        self._audit(
            actor=actor,
            action="tool.enable" if active else "tool.disable",
            target=f"{name}/{tool_name}",
        )
        return tool
