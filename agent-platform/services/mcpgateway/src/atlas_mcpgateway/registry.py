"""服务注册表与工具（MCP 网关原型）。

登记服务：① 校验连接与凭据 → ② 发现工具（MCP 拉 tools/list；REST 解析 OpenAPI；gRPC 反射 /
描述文件）→ ③ 在用户中心登记数据对象 McpServer，登记人成为所有者（失败则登记失败）→ ④ 存库。

    MCP 代理      工具定义从上游拉来，全部登记；定期重新拉取（维护任务），也可以手动刷新。
                  刷新失败保留旧定义并记下原因；上游删掉的工具标成已删除
    REST / gRPC  工具由管理员挑选、编辑；改动保存即生效

★ 没有复核：上游新增、改了的工具拉到就生效；有服务级授权的 Key 自动拿到新工具。
★ 用户中心里的 McpTool 数据对象在第一次工具级授权时才登记（grants.py）；工具删除时一并删掉。
★ 网络 I/O（发现、拉取）在锁外做；写库时锁住服务行，多个实例的刷新串行。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit
from .db.models import McpService, McpTool
from .db.types import utcnow
from .definitions import (
    SERVICE_NAME_PATTERN,
    Definition,
    annotations_for,
    content_hash,
    define,
    schema_from_params,
)
from .errors import Conflict, Invalid, NotFound, TooSoon
from .perms import DATA_SERVER, DATA_TOOL, Permissions, tool_data_id
from .settings import GatewaySettings
from .upstreams import DiscoveryFailed
from .upstreams import grpc as grpc_up
from .upstreams import rest as rest_up
from .upstreams.credentials import Credential
from .upstreams.credentials import validate as validate_credential
from .upstreams.hub import Discovered, Upstreams
from .upstreams.mcp import TRANSPORTS

logger = logging.getLogger(__name__)

__all__ = ["Registry", "validate_connection"]

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


def _tool_from(service: str, d: Definition) -> McpTool:
    return McpTool(
        name=d.name,
        description=d.description,
        input_schema=d.input_schema,
        annotations=d.annotations,
        effect=d.effect,
        digest=d.digest,
        issues=list(d.issues),
        status="active",
    )


def _defined(
    service: str, name: str, description: str, schema: Mapping[str, Any], effect: str
) -> Definition:
    return define(service, name, description, schema, annotations_for(effect))


class Registry:
    def __init__(
        self,
        session: AsyncSession,
        settings: GatewaySettings,
        perms: Permissions,
        upstreams: Upstreams,
    ) -> None:
        self.session = session
        self.settings = settings
        self.perms = perms
        self.upstreams = upstreams

    # ------------------------------------------------------------------ 查询

    async def list(self) -> list[McpService]:
        return list(await self.session.scalars(select(McpService).order_by(McpService.name)))

    async def find(self, name: str) -> McpService | None:
        return await self.session.scalar(select(McpService).where(McpService.name == name))

    async def get(self, name: str) -> McpService:
        row = await self.find(name)
        if row is None:
            raise NotFound(f"MCP 服务 {name} 不存在", service=name)
        return row

    async def tools(self, svc: McpService, *, include_removed: bool = False) -> list[McpTool]:
        stmt = select(McpTool).where(McpTool.service_uuid == svc.uuid).order_by(McpTool.name)
        if not include_removed:
            stmt = stmt.where(McpTool.removed_at.is_(None))
        return list(await self.session.scalars(stmt))

    async def tool(self, svc: McpService, name: str) -> McpTool:
        row = await self.session.scalar(
            select(McpTool).where(
                McpTool.service_uuid == svc.uuid, McpTool.name == name, McpTool.removed_at.is_(None)
            )
        )
        if row is None:
            raise NotFound(f"MCP 服务 {svc.name} 上没有工具 {name}", tool=name)
        return row

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
    ) -> Discovered:
        """登记前、重新导入时给管理员看：能转换成哪些工具。不写库。"""
        conn = validate_connection(kind, connection)
        if previous is not None and not secret:
            secret = previous.credential_secret
        cred = validate_credential(
            kind, credential, secret, previous=previous.credential if previous else None
        )
        if kind == "grpc" and conn["discovery"] == "descriptor" and descriptor is None:
            if previous is not None and previous.descriptor is not None:
                descriptor = previous.descriptor
            else:
                raise Invalid("请上传描述文件（FileDescriptorSet）")
        try:
            return await self.upstreams.discover(
                kind, conn, Credential.of(cred, secret), service=service, descriptor=descriptor
            )
        except DiscoveryFailed as exc:
            raise Invalid(str(exc), code="DISCOVERY_FAILED") from exc

    # ------------------------------------------------------------------ 登记 / 修改

    async def create(
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
        if await self.find(name) is not None:
            raise Conflict(
                f"MCP 服务 {name} 已存在（服务名是工具前缀，停用后也不能复用）",
                code="SERVICE_EXISTS",
            )
        if visibility not in ("public", "private"):
            raise Invalid("可见性只能是 public 或 private")
        conn = validate_connection(kind, connection)
        cred = validate_credential(kind, credential, secret, previous=None)
        found = await self.discover(
            kind=kind,
            connection=conn,
            credential=cred,
            secret=secret,
            descriptor=descriptor,
            service=name,
        )
        tools = self._pick(name, found, selected)
        # 用户中心登记失败则登记失败（异常直接往上抛，库里什么都没写）
        await self._register_server(name, display_name or name, owner=actor)
        if kind == "grpc":
            conn["services"] = found.services
        row = McpService(
            name=name,
            display_name=display_name or name,
            description=description,
            kind=kind,
            visibility=visibility,
            connection=conn,
            credential=cred,
            credential_secret=secret or None,
            descriptor=found.descriptor,
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
        audit.record(
            self.session,
            actor=actor,
            action="service.create",
            target=name,
            kind=kind,
            visibility=visibility,
            tools=[t.name for t in tools],
        )
        return row

    def _pick(self, service: str, found: Discovered, selected: Sequence[str]) -> list[McpTool]:
        if found.kind == "mcp":
            return [_tool_from(service, d) for d in found.definitions]
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
        tool = _tool_from(service, d)
        tool.mapping, tool.effect = clean, effect
        return tool

    def _grpc_tool(self, service: str, m: grpc_up.Method) -> McpTool:
        d = _defined(service, m.name, f"gRPC {m.key}", m.input_schema, m.effect)
        tool = _tool_from(service, d)
        tool.mapping, tool.effect = m.mapping(), m.effect
        return tool

    async def _register_server(self, name: str, display_name: str, *, owner: str) -> None:
        try:
            await self.perms.register(DATA_SERVER, name, display_name, owners=[owner])
        except Conflict:
            # ★ 网关库里没有这个服务，用户中心里却有：旧版本（Higress 时代）或登记到一半失败留下的。
            #   网关库是服务是否存在的事实源，清掉重来
            logger.warning("用户中心里有没人管的 McpServer %s，清掉后重新登记", name)
            await self.perms.drop(DATA_SERVER, name)
            await self.perms.register(DATA_SERVER, name, display_name, owners=[owner])

    async def update(self, name: str, changes: Mapping[str, Any], *, actor: str) -> McpService:
        """基本信息：显示名称、描述、可见性。"""
        row = await self.get(name)
        if "visibility" in changes and changes["visibility"] not in ("public", "private"):
            raise Invalid("可见性只能是 public 或 private")
        if changes.get("display_name") and changes["display_name"] != row.display_name:
            await self.perms.rename(DATA_SERVER, name, changes["display_name"])
        for key in ("display_name", "description", "visibility"):
            if key in changes and changes[key] is not None:
                setattr(row, key, changes[key])
        row.updated_by = actor
        audit.record(
            self.session, actor=actor, action="service.update", target=name, changes=dict(changes)
        )
        return row

    async def update_connection(
        self, name: str, connection: Mapping[str, Any], *, pass_identity: bool, actor: str
    ) -> McpService:
        row = await self.get(name)
        conn = validate_connection(row.kind, connection)
        if row.kind == "grpc":
            conn["services"] = row.connection.get("services") or []
        row.connection, row.pass_identity, row.updated_by = conn, pass_identity, actor
        audit.record(
            self.session, actor=actor, action="service.connection", target=name, connection=conn
        )
        if row.kind == "mcp":
            await self.refresh(row)
        return row

    async def update_credential(
        self, name: str, credential: Mapping[str, Any], secret: str | None, *, actor: str
    ) -> McpService:
        row = await self.get(name)
        cred = validate_credential(row.kind, credential, secret, previous=row.credential)
        if secret:
            row.credential_secret = secret
        elif cred["type"] == "none":
            row.credential_secret = None
        row.credential, row.updated_by = cred, actor
        audit.record(
            self.session, actor=actor, action="service.credential", target=name, type=cred["type"]
        )
        return row

    async def set_enabled(self, name: str, enabled: bool, *, actor: str) -> McpService:
        """停用在网关立即生效：所有调用都经过网关。"""
        row = await self.get(name)
        row.status = "enabled" if enabled else "disabled"
        row.updated_by = actor
        audit.record(
            self.session,
            actor=actor,
            action="service.enable" if enabled else "service.disable",
            target=name,
        )
        return row

    # ------------------------------------------------------------------ MCP 代理：拉取定义

    async def refresh(self, row: McpService, *, manual: bool = False) -> McpService:
        """从上游拉一次完整的工具列表，存库。失败保留旧定义、记下原因。"""
        if row.kind != "mcp":
            raise Invalid("只有 MCP 代理的服务从上游拉取工具定义")
        if manual and row.fetched_at is not None:
            since = (utcnow() - row.fetched_at).total_seconds()
            if since < self.settings.manual_refresh_min_interval_s:
                raise TooSoon(
                    f"{self.settings.manual_refresh_min_interval_s:g} 秒内只能刷新一次",
                    service=row.name,
                )
        try:
            found = await self.upstreams.discover(
                "mcp",
                row.connection,
                Credential.of(row.credential, row.credential_secret),
                service=row.name,
            )
            error = None
        except DiscoveryFailed as exc:  # 拉取失败的细节给管理员看，不往上抛
            found, error = None, str(exc)
        await self._lock(row)
        row.fetched_at = utcnow()
        row.refresh_claimed_at = None
        if found is None:
            logger.warning("MCP 服务 %s 刷新失败，继续使用旧定义：%s", row.name, error)
            row.fetch_error = error
            return row
        row.fetch_error = None
        await self._apply(row, found.definitions)
        return row

    async def _lock(self, row: McpService) -> None:
        """锁住服务行并重新读一遍（别的实例可能刚刷新过）。先 flush：重读会覆盖没写下去的改动。"""
        await self.session.flush()
        await self.session.refresh(row, with_for_update=True)

    async def _apply(self, svc: McpService, defs: Sequence[Definition]) -> list[str]:
        rows = {t.name: t for t in await self.tools(svc, include_removed=True)}
        now = utcnow()
        changed: list[str] = []
        seen: set[str] = set()
        for d in defs:
            if d.name in seen:
                continue  # 上游给了同名的两个工具：只认第一个
            seen.add(d.name)
            row = rows.get(d.name)
            if row is None:
                row = _tool_from(svc.name, d)
                row.service_uuid = svc.uuid
                rows[d.name] = row
                self.session.add(row)
                changed.append(d.name)
                continue
            if row.digest != d.digest:
                row.previous = {
                    "description": row.description,
                    "input_schema": row.input_schema,
                    "annotations": row.annotations,
                    "digest": row.digest,
                }
                row.changed_at = now
                changed.append(d.name)
            elif row.removed_at is not None:
                changed.append(d.name)
            row.description, row.input_schema = d.description, d.input_schema
            row.annotations, row.digest, row.effect = d.annotations, d.digest, d.effect
            row.issues = list(d.issues)
            row.removed_at = None
        for name, row in rows.items():
            if name not in seen and row.removed_at is None:
                row.removed_at = now
                changed.append(name)
                await self._drop_tool_object(svc, row)
        digest = content_hash(r.digest for r in rows.values() if r.removed_at is None)
        if digest != svc.content_hash:
            if svc.content_hash is not None:
                logger.info("MCP 服务 %s 的工具定义变了：%s", svc.name, "、".join(changed))
            svc.content_hash = digest
            svc.changed_at = now
        await self.session.flush()
        return changed

    async def claim_due(self) -> list[str]:
        """维护任务：领到该刷新的 MCP 代理（多实例只让一个去拉：领取 = 写租约时间）。"""
        now = utcnow()
        due = now - timedelta(seconds=self.settings.refresh_interval_s)
        lease = now - timedelta(seconds=max(self.settings.discovery_timeout_s * 4, 60))
        result = await self.session.execute(
            update(McpService)
            .where(
                McpService.kind == "mcp",
                McpService.status == "enabled",
                or_(McpService.fetched_at.is_(None), McpService.fetched_at < due),
                or_(McpService.refresh_claimed_at.is_(None), McpService.refresh_claimed_at < lease),
            )
            .values(refresh_claimed_at=now)
            .returning(McpService.name)
        )
        return [r[0] for r in result.all()]

    # ------------------------------------------------------------------ REST / gRPC：重新导入

    async def add_discovered(
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
        row = await self.get(name)
        if row.kind == "mcp":
            raise Invalid("MCP 代理的工具从上游拉取，用「立即刷新」")
        found = await self.discover(
            kind=row.kind,
            connection=row.connection,
            credential=row.credential,
            secret=None,
            descriptor=descriptor,
            service=row.name,
            previous=row,
        )
        existing = {t.name for t in await self.tools(row, include_removed=True)}
        tools = [t for t in self._pick(row.name, found, selected) if t.name not in existing]
        for t in tools:
            t.service_uuid = row.uuid
            self.session.add(t)
        if row.kind == "grpc":
            row.descriptor = found.descriptor
            row.connection = {**row.connection, "services": found.services}
        await self.session.flush()
        audit.record(
            self.session,
            actor=actor,
            action="service.import",
            target=name,
            tools=[t.name for t in tools],
        )
        return [t.name for t in tools]

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
        row = await self.get(name)
        if row.kind != "rest":
            raise Invalid("只有 REST 转换的服务能手工添加接口")
        rest_up.check_name(tool_name)
        if not description.strip():
            raise Invalid("请填写描述")
        if effect not in ("read", "write", "destructive"):
            raise Invalid("副作用只能是 read、write、destructive")
        if any(t.name == tool_name for t in await self.tools(row, include_removed=True)):
            raise Conflict(f"工具名 {tool_name} 已存在")
        tool = self._rest_tool(row.name, tool_name, description.strip(), effect, mapping)
        tool.service_uuid = row.uuid
        self.session.add(tool)
        await self.session.flush()
        audit.record(self.session, actor=actor, action="tool.create", target=f"{name}/{tool_name}")
        return tool

    async def update_tool(
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
        row = await self.get(name)
        if row.kind == "mcp":
            raise Invalid("MCP 代理的工具定义来自上游，不能在网关里改")
        tool = await self.tool(row, tool_name)
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
        audit.record(self.session, actor=actor, action="tool.update", target=f"{name}/{tool_name}")
        return tool

    async def delete_tool(self, name: str, tool_name: str, *, actor: str) -> None:
        """REST / gRPC：删掉一个工具（连同用户中心里它的数据对象和工具级授权）。"""
        row = await self.get(name)
        if row.kind == "mcp":
            raise Invalid("MCP 代理的工具来自上游，不能删除，可以停用")
        tool = await self.tool(row, tool_name)
        await self._drop_tool_object(row, tool)
        await self.session.delete(tool)
        await self.session.flush()
        audit.record(self.session, actor=actor, action="tool.delete", target=f"{name}/{tool_name}")

    async def set_tool_status(
        self, name: str, tool_name: str, *, active: bool, actor: str
    ) -> McpTool:
        row = await self.get(name)
        tool = await self.tool(row, tool_name)
        tool.status = "active" if active else "disabled"
        audit.record(
            self.session,
            actor=actor,
            action="tool.enable" if active else "tool.disable",
            target=f"{name}/{tool_name}",
        )
        return tool

    async def _drop_tool_object(self, svc: McpService, tool: McpTool) -> None:
        if tool.uc_registered:
            await self.perms.drop(DATA_TOOL, tool_data_id(svc.name, tool.name))
            tool.uc_registered = False
