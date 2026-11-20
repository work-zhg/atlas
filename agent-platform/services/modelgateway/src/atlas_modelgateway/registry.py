"""模型和线路：模型是对外的名字，线路决定背后走哪个账号的哪个上游模型。

    模型名      调用时传的 model，租户内唯一、不能改；Key、授权、用量都按它记
    能力        对调用方的承诺（看图、工具、思考、上下文、最大输出），登记时按第一条线路的预置带出，
                管理员可以改
    价格        对内统一价（元 / 百万 token），不管走哪条线路都按它算；空 = 不计费
    线路        至少一条；同一账号的同一上游模型不能出现两次；每条线路都要做到模型承诺的能力
                （按预置规则查，查不到的由管理员负责）
    公有 / 私有  公有的谁都能选；私有的授权给人或部门后才能选（授权在用户中心：数据编码 Model，
                数据标识 = 模型名）

★ 改完把配置版本号加一（notify.mark）；公有改私有、删除、停用会影响 Key 的有效模型，由调用方
  （接口层）接着重算受影响的 Key。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit, notify
from .db.models import GwAccount, GwModel, GwRoute
from .errors import Conflict, Invalid, NotFound
from .gateway.kernel import ModelCaps, ModelKind, Price, ThinkingSupport
from .gateway.presets import PresetRules
from .perms import Grant, Permissions

__all__ = ["MODEL_NAME_PATTERN", "ModelInput", "ModelRegistry", "RouteInput"]

logger = logging.getLogger(__name__)

MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


@dataclass(frozen=True)
class RouteInput:
    account_uuid: UUID
    upstream_model: str = ""
    priority: int = 1
    weight: int = 1


@dataclass(frozen=True)
class ModelInput:
    display_name: str
    kind: ModelKind = ModelKind.CHAT
    visibility: str = "private"
    description: str = ""
    #: None = 按第一条线路的预置带出
    caps: ModelCaps | None = None
    price: Price = field(default_factory=Price)
    enabled: bool = True
    routes: Sequence[RouteInput] = ()


class ModelRegistry:
    def __init__(
        self, session: AsyncSession, perms: Permissions, presets: PresetRules | None = None
    ) -> None:
        self.session = session
        self.perms = perms
        self.presets = presets or PresetRules()

    # ── 查 ─────────────────────────────────────────────────────────────

    async def rows(self) -> list[GwModel]:
        return list(await self.session.scalars(select(GwModel).order_by(GwModel.name)))

    async def get(self, uuid: UUID) -> GwModel:
        row = await self.session.scalar(select(GwModel).where(GwModel.uuid == uuid))
        if row is None:
            raise NotFound("模型不存在")
        return row

    async def by_name(self, name: str) -> GwModel | None:
        return await self.session.scalar(select(GwModel).where(GwModel.name == name))

    async def by_names(self) -> dict[str, GwModel]:
        return {m.name: m for m in await self.rows()}

    async def routes(self, model_uuids: Sequence[UUID] | None = None) -> dict[UUID, list[GwRoute]]:
        stmt = select(GwRoute).order_by(GwRoute.priority, GwRoute.created_at)
        if model_uuids is not None:
            stmt = stmt.where(GwRoute.model_uuid.in_(list(model_uuids)))
        out: dict[UUID, list[GwRoute]] = {}
        for r in await self.session.scalars(stmt):
            out.setdefault(r.model_uuid, []).append(r)
        return out

    async def accounts(self) -> dict[UUID, GwAccount]:
        return {a.uuid: a for a in await self.session.scalars(select(GwAccount))}

    def suggest_caps(
        self, provider: str, upstream_model: str, kind: ModelKind = ModelKind.CHAT
    ) -> ModelCaps:
        """登记时带出的默认能力：有预置用预置；没有的只标类型（其余按「不支持」，由管理员填）。"""
        found = self.presets.caps_for(provider, upstream_model)
        return found if found is not None else ModelCaps(kind=kind)

    # ── 改 ─────────────────────────────────────────────────────────────

    async def create(
        self, name: str, data: ModelInput, *, grants: Sequence[Grant] = (), actor: str
    ) -> GwModel:
        name = name.strip()
        if not MODEL_NAME_PATTERN.match(name):
            raise Invalid("模型名只能用字母、数字和 . _ : / -，以字母或数字开头，最长 128 个字符")
        if await self.by_name(name) is not None:
            raise Conflict(f"已经有叫 {name} 的模型了")
        self._check_basics(data)
        accounts = await self.accounts()
        caps = data.caps or self._default_caps(data, accounts)
        row = GwModel(
            name=name,
            display_name=(data.display_name or name)[:100],
            kind=data.kind.value,
            visibility=data.visibility,
            description=data.description,
            caps=replace(caps, kind=data.kind).to_json(),
            price=data.price.to_json(),
            enabled=data.enabled,
            created_by=actor,
            updated_by=actor,
        )
        self.session.add(row)
        await self.session.flush()
        await self._set_routes(row, data.routes, accounts)
        # 用户中心里登记成一条数据（登记人成为所有者）；私有模型顺带授权
        await self.perms.register(row.name, row.display_name, actor)
        if grants:
            await self.perms.grant(row.name, actor, list(grants))
        audit.record(self.session, actor=actor, action="model.create", target=f"model:{row.name}")
        notify.mark(self.session)
        return row

    async def update(self, row: GwModel, data: ModelInput, *, actor: str) -> bool:
        """→ 公有 / 私有、启用状态有没有变（变了要重算 Key 的有效模型）。"""
        self._check_basics(data)
        if data.kind.value != row.kind:
            raise Invalid("模型类型建好后不能改")
        accounts = await self.accounts()
        affects_keys = row.visibility != data.visibility or row.enabled != data.enabled
        renamed = row.display_name != (data.display_name or row.name)
        row.display_name = (data.display_name or row.name)[:100]
        row.visibility = data.visibility
        row.description = data.description
        row.caps = replace(data.caps or ModelCaps.from_json(row.caps), kind=data.kind).to_json()
        row.price = data.price.to_json()
        row.enabled = data.enabled
        row.updated_by = actor
        await self._set_routes(row, data.routes, accounts)
        if renamed:
            await self.perms.rename(row.name, row.display_name)
        audit.record(self.session, actor=actor, action="model.update", target=f"model:{row.name}")
        notify.mark(self.session)
        return affects_keys

    async def set_enabled(self, row: GwModel, enabled: bool, *, actor: str) -> None:
        row.enabled = enabled
        row.updated_by = actor
        audit.record(
            self.session,
            actor=actor,
            action="model.enable" if enabled else "model.disable",
            target=f"model:{row.name}",
        )
        notify.mark(self.session)

    async def delete(self, row: GwModel, *, actor: str) -> None:
        await self.session.execute(delete(GwRoute).where(GwRoute.model_uuid == row.uuid))
        audit.record(self.session, actor=actor, action="model.delete", target=f"model:{row.name}")
        await self.session.delete(row)
        await self.session.flush()
        await self.perms.remove(row.name)
        notify.mark(self.session)

    # ── 线路 ───────────────────────────────────────────────────────────

    async def _set_routes(
        self, row: GwModel, routes: Sequence[RouteInput], accounts: dict[UUID, GwAccount]
    ) -> None:
        if not routes:
            raise Invalid("至少要有一条线路")
        seen: set[tuple[UUID, str]] = set()
        caps = ModelCaps.from_json(row.caps)
        for r in routes:
            account = accounts.get(r.account_uuid)
            if account is None:
                raise Invalid("线路里的账号不存在")
            upstream = (r.upstream_model or row.name).strip()
            if (r.account_uuid, upstream) in seen:
                raise Invalid(f"同一账号「{account.name}」的同一上游模型 {upstream} 出现了两次")
            seen.add((r.account_uuid, upstream))
            if r.weight < 1 or r.priority < 0:
                raise Invalid("权重至少是 1，优先级不能是负数")
            if missing := self._missing_caps(caps, account.provider, upstream):
                raise Invalid(
                    f"线路「{account.name} · {upstream}」做不到模型承诺的能力：{'、'.join(missing)}"
                )
        await self.session.execute(delete(GwRoute).where(GwRoute.model_uuid == row.uuid))
        for r in routes:
            self.session.add(
                GwRoute(
                    model_uuid=row.uuid,
                    account_uuid=r.account_uuid,
                    upstream_model=(r.upstream_model or row.name).strip()[:200],
                    priority=r.priority,
                    weight=r.weight,
                )
            )
        await self.session.flush()

    def _missing_caps(self, promised: ModelCaps, provider: str, upstream: str) -> list[str]:
        """按预置查这条线路做不做得到；查不到预置的不拦（由管理员负责）。"""
        preset = self.presets.caps_for(provider, upstream)
        if preset is None:
            return []
        missing = []
        if promised.vision and not preset.vision:
            missing.append("看图")
        if promised.tools and not preset.tools:
            missing.append("调用工具")
        if (
            promised.thinking is not ThinkingSupport.NONE
            and preset.thinking is ThinkingSupport.NONE
        ):
            missing.append("思考")
        if (
            promised.context_window
            and preset.context_window
            and promised.context_window > preset.context_window
        ):
            missing.append(f"上下文 {promised.context_window}")
        return missing

    def _default_caps(self, data: ModelInput, accounts: dict[UUID, GwAccount]) -> ModelCaps:
        first = data.routes[0] if data.routes else None
        account = accounts.get(first.account_uuid) if first else None
        if account is None or first is None:
            return ModelCaps(kind=data.kind)
        return self.suggest_caps(account.provider, first.upstream_model or "", data.kind)

    @staticmethod
    def _check_basics(data: ModelInput) -> None:
        if data.visibility not in ("public", "private"):
            raise Invalid("可见范围只能是公有或私有")
        if any(p < 0 for p in (data.price.input, data.price.cache_hit, data.price.output)):
            raise Invalid("价格不能是负数")
