"""服务注册表、工具元数据与复核（MCP 方案设计 §3.2、§4.1–§4.3、§8.3）。

登记服务：① 接入检查（不带网关凭据访问上游，必须被拒）→ ② 在用户中心登记数据对象，
注册人成为所有者（登记失败则注册失败，不留「有服务、没有授权数据」的状态）→ ③ 带网关凭据
从上游拉工具定义存库。

元数据：网关是事实源。定期重新拉取，也可以手动刷新；刷新失败保留旧定义并记下原因
（服务短暂宕机不该让已有的工具消失，调用时自会报错）。定义变化时保留变化前的一版。

★ 数据面在 tools/list 的返回里看到和库里不一样的定义时（上游改了、网关还没刷新），
  调 observe() 把这版定义写进库（demo F2）：要求复核的服务，这个工具随即变成待复核，
  之后的 tools/call 一并拒绝。只更新看到的工具，不据此判断删除（返回可能分页）。
★ 网络 I/O（接入检查、拉取）在锁外做；写库时锁住服务行，刷新与 observe 串行。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit
from .db.models import McpService, McpTool, McpToolReview
from .db.types import utcnow
from .definitions import Definition, ToolStatus, content_hash, normalize, tool_status
from .errors import Conflict, Invalid, NotFound, TooSoon
from .perms import Permissions
from .settings import GatewaySettings
from .upstream import AccessCheck, Upstream, describe_error

logger = logging.getLogger(__name__)

__all__ = ["Registry", "ToolView"]


@dataclass(frozen=True)
class ToolView:
    row: McpTool
    status: ToolStatus
    review: McpToolReview | None


class Registry:
    def __init__(self, session: AsyncSession, settings: GatewaySettings) -> None:
        self.session = session
        self.settings = settings

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

    async def tools(self, svc: McpService, *, include_removed: bool = False) -> list[ToolView]:
        """服务的工具及复核状态。复核结论按「工具 + 当前指纹」取。"""
        query = (
            select(McpTool, McpToolReview)
            .outerjoin(
                McpToolReview,
                and_(
                    McpToolReview.service_uuid == McpTool.service_uuid,
                    McpToolReview.tool_name == McpTool.name,
                    McpToolReview.digest == McpTool.digest,
                ),
            )
            .where(McpTool.service_uuid == svc.uuid)
            .order_by(McpTool.name)
        )
        if not include_removed:
            query = query.where(McpTool.removed_at.is_(None))
        out = []
        for tool, review in (await self.session.execute(query)).all():
            status = tool_status(
                issues=tool.issues,
                review_required=svc.review_required,
                decision=review.decision if review else None,
            )
            out.append(ToolView(tool, status, review))
        return out

    async def tool(self, svc: McpService, name: str) -> ToolView | None:
        """数据面：tools/call 的复核判断。工具不在册（或已删除）为 None。"""
        query = (
            select(McpTool, McpToolReview)
            .outerjoin(
                McpToolReview,
                and_(
                    McpToolReview.service_uuid == McpTool.service_uuid,
                    McpToolReview.tool_name == McpTool.name,
                    McpToolReview.digest == McpTool.digest,
                ),
            )
            .where(
                McpTool.service_uuid == svc.uuid,
                McpTool.name == name,
                McpTool.removed_at.is_(None),
            )
        )
        found = (await self.session.execute(query)).first()
        if found is None:
            return None
        row, review = found
        status = tool_status(
            issues=row.issues,
            review_required=svc.review_required,
            decision=review.decision if review else None,
        )
        return ToolView(row, status, review)

    # ------------------------------------------------------------------ 登记 / 修改

    async def create(
        self,
        perms: Permissions,
        *,
        name: str,
        display_name: str,
        description: str,
        upstream_name: str,
        review_required: bool,
        call_timeout_s: float | None,
        credential_mode: str,
        actor: str,
    ) -> McpService:
        if await self.find(name) is not None:
            raise Conflict(
                f"MCP 服务 {name} 已存在（服务名是工具前缀，停用后也不能复用）",
                code="SERVICE_EXISTS",
            )
        upstream = Upstream(self.settings, upstream_name)
        check = await upstream.check_access()
        if check.ok is not True:
            raise Invalid(f"接入检查不通过：{check.detail}", code="ACCESS_CHECK_FAILED")
        # 用户中心登记失败则注册失败（异常直接往上抛，库里什么都没写）
        await perms.register(name, display_name, actor)
        row = McpService(
            name=name,
            display_name=display_name,
            description=description,
            upstream_kind="higress",
            upstream_name=upstream_name,
            credential_mode=credential_mode,
            review_required=review_required,
            call_timeout_s=call_timeout_s,
            status="enabled",
            created_by=actor,
            updated_by=actor,
        )
        self.session.add(row)
        await self.session.flush()
        audit.record(
            self.session,
            actor=actor,
            action="service.create",
            target=name,
            upstream=upstream_name,
            review_required=review_required,
        )
        await self.refresh(row, checked=check)
        return row

    async def update(
        self, name: str, changes: dict[str, Any], *, perms: Permissions, actor: str
    ) -> McpService:
        row = await self.get(name)
        check: AccessCheck | None = None
        if "upstream_name" in changes and changes["upstream_name"] != row.upstream_name:
            # 换上游 = 换了一个服务实体：同样要先过接入检查（§8.4）
            check = await Upstream(self.settings, changes["upstream_name"]).check_access()
            if check.ok is not True:
                raise Invalid(f"接入检查不通过：{check.detail}", code="ACCESS_CHECK_FAILED")
        if "display_name" in changes and changes["display_name"] != row.display_name:
            await perms.rename(name, changes["display_name"] or name)
        for key, value in changes.items():
            setattr(row, key, value)
        row.updated_by = actor
        audit.record(
            self.session, actor=actor, action="service.update", target=name, changes=changes
        )
        if check is not None:
            await self.refresh(row, checked=check)
        return row

    async def set_enabled(self, name: str, enabled: bool, *, actor: str) -> McpService:
        """停用在网关立即生效：所有请求都经过网关，已打开的 ACP 会话也会被拦下。"""
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

    # ------------------------------------------------------------------ 元数据

    async def refresh(
        self, row: McpService, *, manual: bool = False, checked: AccessCheck | None = None
    ) -> McpService:
        """接入检查复查 + 带网关凭据拉一次完整的工具列表，存库。失败保留旧定义。

        checked：刚做过的接入检查（登记、换上游时），不再重做一次。
        """
        if manual and row.fetched_at is not None:
            since = (utcnow() - row.fetched_at).total_seconds()
            if since < self.settings.manual_refresh_min_interval_s:
                raise TooSoon(
                    f"{self.settings.manual_refresh_min_interval_s:g} 秒内只能刷新一次",
                    service=row.name,
                )
        upstream = Upstream(self.settings, row.upstream_name)
        check = checked or await upstream.check_access()
        try:
            raw = await upstream.list_tools()
            error = None
        except Exception as exc:  # 拉取失败的细节给管理员看，不往上抛
            raw, error = None, describe_error(exc)
        await self._lock(row)
        row.access_checked_at, row.access_check_ok = utcnow(), check.ok
        row.access_check_detail = check.detail
        if check.ok is False:
            # ★ 告警：Higress 上这个服务变成了可以直连（§8.3、§10）
            logger.error("MCP 服务 %s 的上游可以不带网关凭据直接调通：%s", row.name, check.detail)
        row.fetched_at = utcnow()
        row.refresh_claimed_at = None
        if raw is None:
            logger.warning("MCP 服务 %s 刷新失败，继续使用旧定义：%s", row.name, error)
            row.fetch_error = error
            return row
        row.fetch_error = None
        await self._apply(row, [normalize(row.name, t) for t in raw], complete=True)
        return row

    async def observe(self, row: McpService, raw: list[dict[str, Any]]) -> list[str]:
        """数据面看到的定义（tools/list 的返回）→ 和库里不同的写进库。返回变了的工具名。"""
        await self._lock(row)
        return await self._apply(row, [normalize(row.name, t) for t in raw], complete=False)

    async def _lock(self, row: McpService) -> None:
        """锁住服务行并重新读一遍（别的副本可能刚刷新过）。先 flush：重读会覆盖没写下去的改动。"""
        await self.session.flush()
        await self.session.refresh(row, with_for_update=True)

    async def _apply(self, svc: McpService, defs: list[Definition], *, complete: bool) -> list[str]:
        rows = {
            t.name: t
            for t in await self.session.scalars(
                select(McpTool).where(McpTool.service_uuid == svc.uuid)
            )
        }
        now = utcnow()
        changed: list[str] = []
        seen: set[str] = set()
        for d in defs:
            if d.name in seen:
                continue  # 上游给了同名的两个工具：只认第一个
            seen.add(d.name)
            row = rows.get(d.name)
            if row is None:
                rows[d.name] = row = McpTool(
                    service_uuid=svc.uuid,
                    name=d.name,
                    description=d.description,
                    input_schema=d.input_schema,
                    annotations=d.annotations,
                    digest=d.digest,
                    issues=list(d.issues),
                )
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
            row.annotations, row.digest = d.annotations, d.digest
            row.issues = list(d.issues)
            row.removed_at = None
        if complete:
            for name, row in rows.items():
                if name not in seen and row.removed_at is None:
                    row.removed_at = now
                    changed.append(name)
        digest = content_hash([r.digest for r in rows.values() if r.removed_at is None])
        if digest != svc.content_hash:
            if svc.content_hash is not None:
                logger.warning(
                    "MCP 服务 %s 的工具定义变了：%s → %s（%s）",
                    svc.name,
                    svc.content_hash[:12],
                    digest[:12],
                    "、".join(changed),
                )
            svc.content_hash = digest
            svc.changed_at = now
        await self.session.flush()
        return changed

    async def claim_due(self) -> list[str]:
        """维护任务：领到该刷新的服务（多副本只让一个去刷新：领取 = 写租约时间）。"""
        now = utcnow()
        due = now - timedelta(seconds=self.settings.refresh_interval_s)
        lease = now - timedelta(seconds=max(self.settings.discovery_timeout_s * 4, 60))
        result = await self.session.execute(
            update(McpService)
            .where(
                McpService.status == "enabled",
                or_(McpService.fetched_at.is_(None), McpService.fetched_at < due),
                or_(McpService.refresh_claimed_at.is_(None), McpService.refresh_claimed_at < lease),
            )
            .values(refresh_claimed_at=now)
            .returning(McpService.name)
        )
        return [r[0] for r in result.all()]

    # ------------------------------------------------------------------ 复核

    async def review(
        self, name: str, tool_name: str, *, digest: str, decision: str, comment: str, actor: str
    ) -> ToolView:
        """复核批准 / 拒绝的是**这一版**定义：指纹必须是当前的（防止批准一版已经过时的定义）。"""
        svc = await self.get(name)
        view = await self.tool(svc, tool_name)
        if view is None:
            raise NotFound(f"MCP 服务 {name} 上没有工具 {tool_name}", tool=tool_name)
        if view.row.digest != digest:
            raise Conflict(
                "工具定义已经变了，请按最新的定义复核",
                code="DIGEST_CHANGED",
                current=view.row.digest,
            )
        if view.row.issues:
            raise Invalid(
                "机械检查不通过的定义不能复核", code="TOOL_INVALID", issues=view.row.issues
            )
        existing = view.review
        if existing is None:
            self.session.add(
                McpToolReview(
                    service_uuid=svc.uuid,
                    tool_name=tool_name,
                    digest=digest,
                    decision=decision,
                    comment=comment,
                    reviewed_by=actor,
                )
            )
        else:
            existing.decision, existing.comment, existing.reviewed_by = decision, comment, actor
        audit.record(
            self.session,
            actor=actor,
            action=f"tool.{decision}",
            target=f"{name}/{tool_name}",
            digest=digest,
            comment=comment,
        )
        await self.session.flush()
        found = await self.tool(svc, tool_name)
        assert found is not None
        return found
