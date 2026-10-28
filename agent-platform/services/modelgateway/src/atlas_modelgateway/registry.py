"""模型（方案第 3 章）：平台登记的模型与 LiteLLM 里的部署按模型名合并。

    listing          合并列表：平台登记的（可调用 / LiteLLM 中缺失）+ LiteLLM 直配的（只读，可纳入）
    register         登记：先在 LiteLLM 登记，再写平台记录、用户中心；
                     后两步失败就撤回 LiteLLM 里的登记
    update           显示名称、供应商、可见性、描述（平台）；地址、凭据、规格价格（LiteLLM）
    reregister       LiteLLM 中缺失时，用新填的参数重新建部署（凭据要重新填）
    adopt            纳入 LiteLLM 直配的模型：只写平台记录和用户中心，不改 LiteLLM
    delete           LiteLLM 里的部署、平台记录、用户中心的数据对象一起删

★ 模型名 = LiteLLM 的 model_name：按名字合并，平台内（含 LiteLLM 直配的）不能重名，登记后不能改。
★ 改可见性、删除模型会影响用户 Key 的有效模型：由接口层在这里返回后调 KeyService 重新同步。
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit
from .db.models import GwModel
from .db.types import uuid7
from .errors import Conflict, Invalid, NotFound, Unavailable
from .litellm_api import Deployment, LiteLLM
from .perms import Grant, Permissions

logger = logging.getLogger(__name__)

__all__ = [
    "MODEL_NAME_PATTERN",
    "PROVIDERS",
    "DeploymentsCache",
    "ModelFields",
    "ModelRegistry",
    "ModelView",
    "RouteFields",
]

MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")

#: 供应商（= LiteLLM 的 custom_llm_provider）。
#: needs_base：必须填 API 地址；key_optional：可以不填凭据
PROVIDERS: dict[str, dict[str, Any]] = {
    "deepseek": {"name": "DeepSeek"},
    "anthropic": {"name": "Anthropic"},
    "openai": {"name": "OpenAI / OpenAI 兼容"},
    "azure": {"name": "Azure OpenAI", "needs_base": True},
    "dashscope": {"name": "通义千问（DashScope）"},
    "volcengine": {"name": "火山方舟"},
    "moonshot": {"name": "Moonshot（Kimi）"},
    "hosted_vllm": {"name": "vLLM 自部署", "needs_base": True, "key_optional": True},
}


@dataclass(frozen=True)
class ModelFields:
    """平台保存的基本信息。"""

    display_name: str
    provider: str
    visibility: str
    description: str = ""


@dataclass(frozen=True)
class RouteFields:
    """只交给 LiteLLM 的：地址、凭据、规格价格。api_key 为空 = 不改（编辑时）。"""

    api_base: str | None = None
    api_key: str | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    #: 美元 / 百万 token
    input_price: float | None = None
    output_price: float | None = None

    def model_info(self) -> dict[str, Any]:
        """规格：进 LiteLLM 的 model_info。"""
        info = {
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
        }
        return {k: v for k, v in info.items() if v is not None}

    def pricing(self) -> dict[str, float]:
        """价格：进 LiteLLM 的 litellm_params（美元 / token；放 model_info 不生效）。"""
        prices = {
            "input_cost_per_token": None if self.input_price is None else self.input_price / 1e6,
            "output_cost_per_token": None if self.output_price is None else self.output_price / 1e6,
        }
        return {k: v for k, v in prices.items() if v is not None}


@dataclass
class ModelView:
    """合并列表的一行：平台记录（可能没有）+ LiteLLM 里同名的部署（可能没有，可能多个）。"""

    name: str
    row: GwModel | None
    deployments: list[Deployment] = field(default_factory=list)
    #: LiteLLM 没取到（不可达）：状态未知
    unknown: bool = False

    @property
    def source(self) -> str:
        return "platform" if self.row is not None else "litellm"

    @property
    def missing(self) -> bool:
        """平台登记了，LiteLLM 里却没有。"""
        return self.row is not None and not self.deployments and not self.unknown

    @property
    def callable(self) -> bool:
        """能放进 Key 的：平台登记的，且 LiteLLM 里有（LiteLLM 不可达时按有处理）。"""
        return self.row is not None and not self.missing

    @property
    def primary(self) -> Deployment | None:
        if not self.deployments:
            return None
        own = self.row.litellm_model_id if self.row is not None else None
        return next((d for d in self.deployments if d.id == own), self.deployments[0])


class DeploymentsCache:
    """LiteLLM 里的模型部署：短时缓存。LiteLLM 不可达时沿用上一份；从没取到过返回 None。"""

    def __init__(self, litellm: LiteLLM, ttl_s: float) -> None:
        self.litellm = litellm
        self._ttl = ttl_s
        self._value: list[Deployment] | None = None
        self._at = 0.0

    async def get(self, *, fresh: bool = False) -> list[Deployment] | None:
        if not fresh and self._value is not None and time.monotonic() - self._at < self._ttl:
            return self._value
        try:
            self._value = await self.litellm.models()
            self._at = time.monotonic()
        except Unavailable as exc:
            if self._value is not None:
                logger.warning("LiteLLM 不可达，沿用上一份模型部署：%s", exc)
            else:
                logger.warning("LiteLLM 不可达，取不到模型部署：%s", exc)
        return self._value

    def forget(self) -> None:
        self._at = 0.0


def _check_provider(provider: str) -> None:
    if provider not in PROVIDERS:
        raise Invalid(f"不认识的供应商：{provider}", provider=provider)


def _check_route(provider: str, route: RouteFields, *, need_key: bool) -> None:
    spec = PROVIDERS[provider]
    if spec.get("needs_base") and not route.api_base:
        raise Invalid(f"{spec['name']} 需要填写 API 地址")
    if need_key and not spec.get("key_optional") and not route.api_key:
        raise Invalid("请填写供应商 API Key")


class ModelRegistry:
    def __init__(
        self,
        session: AsyncSession,
        litellm: LiteLLM,
        deployments: DeploymentsCache,
        perms: Permissions,
    ) -> None:
        self.session = session
        self.litellm = litellm
        self.deployments = deployments
        self.perms = perms

    # ── 查询

    async def rows(self) -> list[GwModel]:
        return list(await self.session.scalars(select(GwModel).order_by(GwModel.name)))

    async def get(self, uuid: UUID) -> GwModel:
        row = await self.session.scalar(select(GwModel).where(GwModel.uuid == uuid))
        if row is None:
            raise NotFound("模型不存在", model=str(uuid))
        return row

    async def by_name(self, name: str) -> GwModel | None:
        return await self.session.scalar(select(GwModel).where(GwModel.name == name))

    async def listing(self, *, fresh: bool = False) -> tuple[list[ModelView], bool]:
        """→ (合并列表, LiteLLM 是否可达)。平台登记的在前，各自按名字排序。"""
        deployments = await self.deployments.get(fresh=fresh)
        reachable = deployments is not None
        by_name: dict[str, list[Deployment]] = {}
        for d in deployments or []:
            by_name.setdefault(d.model_name, []).append(d)
        views = [
            ModelView(row.name, row, by_name.pop(row.name, []), unknown=not reachable)
            for row in await self.rows()
        ]
        views.extend(ModelView(name, None, deps) for name, deps in sorted(by_name.items()))
        return views, reachable

    async def callable(self) -> dict[str, GwModel]:
        """能放进 Key 的模型：平台登记的、LiteLLM 里存在的（LiteLLM 不可达时按存在处理）。"""
        views, _ = await self.listing()
        return {v.name: v.row for v in views if v.callable and v.row is not None}

    # ── 登记

    async def test_connection(
        self, *, name: str, provider: str, route: RouteFields
    ) -> tuple[bool, str]:
        _check_provider(provider)
        _check_route(provider, route, need_key=True)
        return await self.litellm.test_connection(
            model_name=name, provider=provider, api_base=route.api_base, api_key=route.api_key
        )

    async def _ensure_unused(self, name: str) -> None:
        if not MODEL_NAME_PATTERN.match(name):
            raise Invalid("模型名只能包含字母、数字和 . _ : / -，且不超过 128 个字符")
        if await self.by_name(name) is not None:
            raise Conflict(f"已经登记过模型 {name}", model=name)
        deployments = await self.deployments.get(fresh=True)
        if deployments is None:
            raise Unavailable("LiteLLM 暂时不可用，无法登记模型", code="LITELLM_UNAVAILABLE")
        if any(d.model_name == name for d in deployments):
            raise Conflict(
                f"LiteLLM 里已经有模型 {name}（直接配置的）：请用「纳入平台」", model=name
            )

    async def register(
        self,
        *,
        name: str,
        fields: ModelFields,
        route: RouteFields,
        grants: Sequence[Grant],
        actor: str,
    ) -> GwModel:
        _check_provider(fields.provider)
        _check_route(fields.provider, route, need_key=True)
        await self._ensure_unused(name)
        row = GwModel(
            uuid=uuid7(),
            name=name,
            display_name=fields.display_name or name,
            provider=fields.provider,
            visibility=fields.visibility,
            description=fields.description,
            created_by=actor,
            updated_by=actor,
        )
        row.litellm_model_id = str(row.uuid)
        await self.litellm.add_model(
            model_id=row.litellm_model_id,
            model_name=name,
            provider=fields.provider,
            api_base=route.api_base,
            api_key=route.api_key,
            info=route.model_info(),
            pricing=route.pricing(),
        )
        try:
            self.session.add(row)
            await self.session.flush()
            await self._register_data(row, grants, actor)
        except Exception:
            await self._undo_add(row.litellm_model_id)
            raise
        self.deployments.forget()
        audit.record(
            self.session,
            actor=actor,
            action="model.register",
            target=f"model:{name}",
            provider=fields.provider,
            visibility=fields.visibility,
        )
        return row

    async def _register_data(self, row: GwModel, grants: Sequence[Grant], actor: str) -> None:
        await self.perms.register(row.name, row.display_name, actor)
        if row.visibility == "private" and grants:
            await self.perms.grant(row.name, actor, list(grants))

    async def _undo_add(self, model_id: str) -> None:
        try:
            await self.litellm.delete_model(model_id)
        except Exception:
            logger.exception("撤回 LiteLLM 里的模型 %s 失败：需要在 LiteLLM 里手工删除", model_id)

    async def adopt(
        self, *, name: str, fields: ModelFields, grants: Sequence[Grant], actor: str
    ) -> GwModel:
        """纳入 LiteLLM 直配的模型：供应商取 LiteLLM 里的，不改 LiteLLM。"""
        if await self.by_name(name) is not None:
            raise Conflict(f"已经登记过模型 {name}", model=name)
        deployments = await self.deployments.get(fresh=True)
        if deployments is None:
            raise Unavailable("LiteLLM 暂时不可用，无法纳入", code="LITELLM_UNAVAILABLE")
        found = [d for d in deployments if d.model_name == name]
        if not found:
            raise NotFound(f"LiteLLM 里没有模型 {name}", model=name)
        row = GwModel(
            name=name,
            display_name=fields.display_name or name,
            provider=found[0].provider or fields.provider,
            visibility=fields.visibility,
            description=fields.description,
            litellm_model_id=None,
            created_by=actor,
            updated_by=actor,
        )
        self.session.add(row)
        await self.session.flush()
        await self._register_data(row, grants, actor)
        audit.record(self.session, actor=actor, action="model.adopt", target=f"model:{name}")
        return row

    # ── 修改

    async def update(
        self, row: GwModel, *, fields: ModelFields, route: RouteFields | None, actor: str
    ) -> bool:
        """→ 可见性有没有变（变了要重新同步用户 Key）。

        route 为 None：只改平台信息（纳入平台的模型，平台不改 LiteLLM）。
        """
        if row.litellm_model_id is not None and route is not None:
            _check_provider(fields.provider)
            _check_route(fields.provider, route, need_key=False)
            await self.litellm.update_model(
                model_id=row.litellm_model_id,
                model_name=row.name,
                provider=fields.provider,
                api_base=route.api_base,
                api_key=route.api_key,
                info=route.model_info(),
                pricing=route.pricing(),
            )
            row.provider = fields.provider
            self.deployments.forget()
        renamed = bool(fields.display_name) and fields.display_name != row.display_name
        visibility_changed = fields.visibility != row.visibility
        if fields.display_name:
            row.display_name = fields.display_name
        row.visibility = fields.visibility
        row.description = fields.description
        row.updated_by = actor
        await self.session.flush()
        if renamed:
            await self.perms.rename(row.name, row.display_name)
        audit.record(
            self.session,
            actor=actor,
            action="model.update",
            target=f"model:{row.name}",
            visibility=row.visibility,
            route=route is not None and row.litellm_model_id is not None,
            new_api_key=bool(route and route.api_key),
        )
        return visibility_changed

    async def reregister(
        self, row: GwModel, *, provider: str, route: RouteFields, actor: str
    ) -> GwModel:
        """LiteLLM 中缺失：用新的参数重新建部署。可见性和授权不变。"""
        _check_provider(provider)
        _check_route(provider, route, need_key=True)
        views, reachable = await self.listing(fresh=True)
        if not reachable:
            raise Unavailable("LiteLLM 暂时不可用", code="LITELLM_UNAVAILABLE")
        view = next(v for v in views if v.row is not None and v.row.uuid == row.uuid)
        if not view.missing:
            raise Conflict(f"LiteLLM 里还有模型 {row.name}，不需要重新登记", model=row.name)
        model_id = str(row.uuid)
        await self.litellm.add_model(
            model_id=model_id,
            model_name=row.name,
            provider=provider,
            api_base=route.api_base,
            api_key=route.api_key,
            info=route.model_info(),
            pricing=route.pricing(),
        )
        row.litellm_model_id = model_id
        row.provider = provider
        row.updated_by = actor
        self.deployments.forget()
        audit.record(
            self.session, actor=actor, action="model.reregister", target=f"model:{row.name}"
        )
        return row

    async def delete(self, row: GwModel, *, actor: str) -> None:
        if row.litellm_model_id is not None:
            try:
                await self.litellm.delete_model(row.litellm_model_id)
            except NotFound:
                logger.info("LiteLLM 里已经没有模型 %s，只删平台记录", row.name)
        await self.perms.remove(row.name)
        await self.session.delete(row)
        await self.session.flush()
        self.deployments.forget()
        audit.record(self.session, actor=actor, action="model.delete", target=f"model:{row.name}")
