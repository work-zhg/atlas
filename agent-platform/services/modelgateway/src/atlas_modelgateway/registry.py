"""模型目录、生命周期、价目、平台用途、额度（模型管理方案设计 §4、§5、§6.7、§10）。

登记模型：① 接入检查（不带网关凭据访问上游，必须被拒）→ ② 对话模型在用户中心登记数据对象，
登记人成为所有者（登记失败则整个登记失败，不留「有模型、没有授权数据」的状态）→ ③ 落库，
状态是「接入中」：探测、确认属性以后由管理员上线（activate）。

生命周期（§5.3）：
    接入中 ──上线──▶ 可用 ──弃用（替代模型、计划下线日期）──▶ 弃用 ──到期 / 手动──▶ 下线
    停用是紧急止血，和生命周期正交：恢复后回到原来的状态

★ 不提供删除；平台模型名不能改、下线后也不能复用（§4.2）。
★ 价目只增不改：改价 = 新增一版（带生效时间），历史成本不随改价变（§10.2）。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit
from .catalog import (
    MODEL_NAME_PATTERN,
    PURPOSES,
    Capabilities,
    Capacity,
    DataPolicy,
    protocols_for,
)
from .db.models import GwBudget, GwModel, GwModelCall, GwModelPrice, GwModelProbe, GwPurpose
from .db.types import utcnow
from .errors import Conflict, Invalid, NotFound
from .perms import Permissions
from .settings import GatewaySettings
from .upstream import AccessCheck, Upstream

logger = logging.getLogger(__name__)

__all__ = ["Registry", "validate_attributes"]

#: 管理端可以改的属性（平台模型名、类型不能改）
EDITABLE = (
    "display_name",
    "description",
    "provider",
    "tags",
    "upstream_model",
    "protocols",
    "input_modalities",
    "capabilities",
    "context_window",
    "max_output_tokens",
    "dimensions",
    "max_batch",
    "data_policy",
    "capacity",
)
_MODALITIES = ("text", "image", "file", "audio", "video")


def validate_attributes(kind: str, attrs: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化属性（JSON 的那几项按 catalog 里的模型校验）。只校验给了的键。"""
    out = dict(attrs)
    try:
        if "capabilities" in out:
            out["capabilities"] = Capabilities.model_validate(
                out["capabilities"] or {}
            ).model_dump()
        if "data_policy" in out:
            out["data_policy"] = DataPolicy.model_validate(out["data_policy"] or {}).model_dump()
        if "capacity" in out:
            out["capacity"] = Capacity.model_validate(out["capacity"] or {}).model_dump()
    except ValidationError as exc:
        raise Invalid(
            f"属性不合法：{exc.errors()[0].get('msg')}", code="INVALID_ATTRIBUTES"
        ) from exc
    if "protocols" in out:
        allowed = protocols_for(kind)  # type: ignore[arg-type]
        protocols = list(dict.fromkeys(out["protocols"] or []))
        bad = [p for p in protocols if p not in allowed]
        if bad:
            raise Invalid(
                f"{kind} 模型不能用这些协议调：{', '.join(bad)}（可选 {', '.join(allowed)}）",
                code="INVALID_PROTOCOL",
            )
        out["protocols"] = protocols
    if "input_modalities" in out:
        modalities = list(dict.fromkeys(out["input_modalities"] or ["text"]))
        bad = [m for m in modalities if m not in _MODALITIES]
        if bad:
            raise Invalid(f"不认识的输入模态：{', '.join(bad)}", code="INVALID_MODALITY")
        out["input_modalities"] = modalities
    if "tags" in out:
        out["tags"] = [str(t)[:32] for t in dict.fromkeys(out["tags"] or [])][:20]
    for key in ("context_window", "max_output_tokens", "dimensions", "max_batch"):
        if out.get(key) is not None and int(out[key]) <= 0:
            raise Invalid(f"{key} 必须是正数", code="INVALID_LIMIT")
    return out


class Registry:
    def __init__(self, session: AsyncSession, settings: GatewaySettings) -> None:
        self.session = session
        self.settings = settings

    # ------------------------------------------------------------------ 查询

    async def list(self, *, kind: str | None = None) -> list[GwModel]:
        query = select(GwModel).order_by(GwModel.name)
        if kind is not None:
            query = query.where(GwModel.kind == kind)
        return list(await self.session.scalars(query))

    async def find(self, name: str) -> GwModel | None:
        return await self.session.scalar(select(GwModel).where(GwModel.name == name))

    async def get(self, name: str) -> GwModel:
        row = await self.find(name)
        if row is None:
            raise NotFound(f"模型 {name} 不存在", model=name)
        return row

    # ------------------------------------------------------------------ 登记 / 修改

    async def check_access(self, row: GwModel) -> AccessCheck:
        """对模型的每个协议各做一次接入检查；任何一个能直连就是不通过。"""
        upstream = Upstream(self.settings)
        checks = [
            await upstream.check_access(row.kind, protocol, row.upstream_model)
            for protocol in (row.protocols or list(protocols_for(row.kind))[:1])  # type: ignore[arg-type]
        ]
        failed = next((c for c in checks if c.ok is False), None)
        unknown = next((c for c in checks if c.ok is None), None)
        result = failed or unknown or checks[0]
        row.access_checked_at = utcnow()
        row.access_check_ok, row.access_check_detail = result.ok, result.detail
        if result.ok is False:
            logger.error("模型 %s 的上游可以不带网关凭据直接调：%s", row.name, result.detail)
        return result

    async def create(
        self, perms: Permissions, *, name: str, kind: str, actor: str, **attrs: Any
    ) -> GwModel:
        if not re.fullmatch(MODEL_NAME_PATTERN, name):
            raise Invalid(
                "平台模型名只能用小写字母、数字、点、短横线、下划线，最长 64", code="INVALID_NAME"
            )
        if kind not in ("chat", "embedding", "rerank"):
            raise Invalid(f"不支持的模型类型 {kind}", code="INVALID_KIND")
        if await self.find(name) is not None:
            raise Conflict(f"模型 {name} 已存在（平台模型名下线后也不能复用）", code="MODEL_EXISTS")
        unknown = set(attrs) - set(EDITABLE)
        if unknown:
            raise Invalid(f"不认识的属性：{', '.join(sorted(unknown))}")
        attrs = validate_attributes(kind, attrs)
        attrs.setdefault("protocols", list(protocols_for(kind))[:1])  # type: ignore[arg-type]
        attrs.setdefault("display_name", name)
        attrs["upstream_model"] = attrs.get("upstream_model") or name
        row = GwModel(name=name, kind=kind, status="onboarding", created_by=actor, updated_by=actor)
        for key, value in attrs.items():
            setattr(row, key, value)
        row.capabilities = row.capabilities or Capabilities().model_dump()
        row.data_policy = row.data_policy or DataPolicy().model_dump()
        row.capacity = row.capacity or Capacity().model_dump()
        check = await self.check_access(row)
        if check.ok is not True:
            raise Invalid(f"接入检查不通过：{check.detail}", code="ACCESS_CHECK_FAILED")
        # 对话模型按人授权：用户中心登记失败则登记失败（异常直接往上抛，库里什么都没写）
        if kind == "chat":
            await perms.register(name, row.display_name, actor)
        self.session.add(row)
        await self.session.flush()
        audit.record(
            self.session,
            actor=actor,
            action="model.create",
            target=name,
            kind=kind,
            upstream=row.upstream_model,
        )
        return row

    async def update(
        self, name: str, changes: dict[str, Any], *, perms: Permissions, actor: str
    ) -> GwModel:
        row = await self.get(name)
        unknown = set(changes) - set(EDITABLE)
        if unknown:
            raise Invalid(f"不能修改：{', '.join(sorted(unknown))}")
        changes = validate_attributes(row.kind, changes)
        reroute = any(
            k in changes and changes[k] != getattr(row, k) for k in ("upstream_model", "protocols")
        )
        renamed = "display_name" in changes and changes["display_name"] != row.display_name
        if renamed and row.kind == "chat":
            await perms.rename(name, changes["display_name"] or name)
        for key, value in changes.items():
            setattr(row, key, value)
        row.updated_by = actor
        if reroute:
            # 换上游 = 换了一条路由：同样要先过接入检查（§9.5）
            check = await self.check_access(row)
            if check.ok is not True:
                raise Invalid(f"接入检查不通过：{check.detail}", code="ACCESS_CHECK_FAILED")
        audit.record(
            self.session,
            actor=actor,
            action="model.update",
            target=name,
            changes={k: v for k, v in changes.items()},
        )
        return row

    # ------------------------------------------------------------------ 生命周期

    async def activate(self, name: str, *, actor: str) -> GwModel:
        """接入中 → 可用。对话模型要有上下文窗口和最大输出；接入检查要通过。"""
        row = await self.get(name)
        if row.status != "onboarding":
            raise Conflict(f"模型 {name} 不在接入中（现在是 {row.status}）", code="NOT_ONBOARDING")
        if row.kind == "chat" and not (row.context_window and row.max_output_tokens):
            raise Invalid("对话模型要先填上下文窗口和最大输出", code="LIMITS_REQUIRED")
        if not row.protocols:
            raise Invalid("还没有确认可用的调用协议（先探测并确认）", code="PROTOCOLS_REQUIRED")
        if row.access_check_ok is not True:
            raise Invalid("接入检查没有通过，不能上线", code="ACCESS_CHECK_FAILED")
        row.status, row.updated_by = "available", actor
        audit.record(self.session, actor=actor, action="model.activate", target=name)
        return row

    async def deprecate(
        self, name: str, *, replacement: str | None, retire_at: datetime | None, actor: str
    ) -> GwModel:
        row = await self.get(name)
        if row.status not in ("available", "deprecated"):
            raise Conflict(f"只有可用的模型能弃用（现在是 {row.status}）", code="NOT_AVAILABLE")
        if replacement is not None:
            target = await self.get(replacement)
            if target.name == name or target.kind != row.kind:
                raise Invalid("替代模型要是另一个同类型的模型", code="INVALID_REPLACEMENT")
        if retire_at is not None and retire_at <= utcnow():
            raise Invalid("计划下线时间要在以后", code="INVALID_RETIRE_AT")
        row.status = "deprecated"
        row.deprecated_at = row.deprecated_at or utcnow()
        row.replacement, row.retire_at, row.updated_by = replacement, retire_at, actor
        audit.record(
            self.session,
            actor=actor,
            action="model.deprecate",
            target=name,
            replacement=replacement,
            retire_at=retire_at.isoformat() if retire_at else None,
        )
        return row

    async def retire(self, name: str, *, actor: str) -> GwModel:
        """下线：网关不再放行；不自动换成替代模型（§5.3）。"""
        row = await self.get(name)
        if row.status == "retired":
            return row
        if row.status == "onboarding":
            raise Conflict("接入中的模型没有上过线，不用下线", code="NOT_AVAILABLE")
        row.status, row.retired_at, row.updated_by = "retired", utcnow(), actor
        audit.record(self.session, actor=actor, action="model.retire", target=name)
        return row

    async def set_disabled(self, name: str, disabled: bool, *, reason: str, actor: str) -> GwModel:
        """停用在网关立即生效，所有路径（含已打开的 ACP 会话）。"""
        row = await self.get(name)
        row.disabled, row.updated_by = disabled, actor
        row.disabled_reason = reason if disabled else ""
        audit.record(
            self.session,
            actor=actor,
            action="model.disable" if disabled else "model.enable",
            target=name,
            reason=reason,
        )
        return row

    async def retire_due(self) -> list[str]:
        """到了计划下线时间的弃用模型（维护任务调用）。"""
        rows = list(
            await self.session.scalars(
                select(GwModel).where(
                    GwModel.status == "deprecated",
                    GwModel.retire_at.is_not(None),
                    GwModel.retire_at <= utcnow(),
                )
            )
        )
        for row in rows:
            await self.retire(row.name, actor="system")
        return [r.name for r in rows]

    # ------------------------------------------------------------------ 探测

    async def probes(self, row: GwModel, *, limit: int = 10) -> list[GwModelProbe]:
        return list(
            await self.session.scalars(
                select(GwModelProbe)
                .where(GwModelProbe.model_uuid == row.uuid)
                .order_by(GwModelProbe.created_at.desc())
                .limit(limit)
            )
        )

    async def apply_probe(self, name: str, probe_id: Any, *, actor: str) -> GwModel:
        """管理员确认探测结果：建议的属性写进目录（§4.4）。"""
        row = await self.get(name)
        probe = await self.session.scalar(
            select(GwModelProbe).where(
                GwModelProbe.uuid == probe_id, GwModelProbe.model_uuid == row.uuid
            )
        )
        if probe is None:
            raise NotFound("探测记录不存在")
        changes = validate_attributes(row.kind, dict(probe.suggested))
        for key, value in changes.items():
            setattr(row, key, value)
        row.updated_by = actor
        probe.applied_by, probe.applied_at = actor, utcnow()
        audit.record(
            self.session,
            actor=actor,
            action="model.probe_apply",
            target=name,
            probe=str(probe.uuid),
            changes=changes,
        )
        return row

    # ------------------------------------------------------------------ 价目

    async def prices(self, row: GwModel) -> list[GwModelPrice]:
        return list(
            await self.session.scalars(
                select(GwModelPrice)
                .where(GwModelPrice.model_uuid == row.uuid)
                .order_by(GwModelPrice.effective_from.desc())
            )
        )

    async def current_price(self, row: GwModel) -> GwModelPrice | None:
        return await self.session.scalar(
            select(GwModelPrice)
            .where(GwModelPrice.model_uuid == row.uuid, GwModelPrice.effective_from <= utcnow())
            .order_by(GwModelPrice.effective_from.desc())
            .limit(1)
        )

    async def add_price(
        self, name: str, *, effective_from: datetime | None, actor: str, **price: Any
    ) -> GwModelPrice:
        row = await self.get(name)
        tiers = []
        for tier in price.pop("tiers", None) or []:
            above = int(tier.get("above_input_tokens") or 0)
            if above <= 0:
                raise Invalid("分段价的阈值必须是正数", code="INVALID_PRICE")
            tiers.append(
                {"above_input_tokens": above}
                | {
                    k: str(Decimal(str(tier[k])))
                    for k in ("input", "output", "cache_read", "cache_write")
                    if tier.get(k) is not None
                }
            )
        for key in ("input", "output", "cache_read", "cache_write", "per_request"):
            if price.get(key) is not None and Decimal(str(price[key])) < 0:
                raise Invalid("价格不能为负", code="INVALID_PRICE")
        record = GwModelPrice(
            model_uuid=row.uuid,
            effective_from=effective_from or utcnow(),
            tiers=tiers,
            created_by=actor,
            **price,
        )
        self.session.add(record)
        await self.session.flush()
        audit.record(
            self.session,
            actor=actor,
            action="model.price",
            target=name,
            effective_from=record.effective_from.isoformat(),
        )
        return record

    # ------------------------------------------------------------------ 平台用途

    async def purposes(self) -> dict[str, GwPurpose]:
        """全部用途的配置。没配过的用途也给一行（默认模型为空）。"""
        rows = {r.code: r for r in await self.session.scalars(select(GwPurpose))}
        return {
            code: rows.get(code) or GwPurpose(code=code, default_model=None, allowed_models=[])
            for code in PURPOSES
        }

    async def set_purpose(
        self, code: str, *, default_model: str | None, allowed_models: list[str], actor: str
    ) -> GwPurpose:
        purpose = PURPOSES.get(code)
        if purpose is None:
            raise NotFound(f"平台用途 {code} 不存在")
        names = list(dict.fromkeys([*([default_model] if default_model else []), *allowed_models]))
        for name in names:
            model = await self.get(name)
            if model.kind != purpose.kind:
                raise Invalid(
                    f"用途「{purpose.name}」要 {purpose.kind} 类型的模型，{name} 是 {model.kind}",
                    code="INVALID_PURPOSE_MODEL",
                )
        row = await self.session.scalar(select(GwPurpose).where(GwPurpose.code == code))
        if row is None:
            row = GwPurpose(code=code)
            self.session.add(row)
        row.default_model, row.allowed_models, row.updated_by = default_model, names, actor
        await self.session.flush()
        audit.record(
            self.session,
            actor=actor,
            action="purpose.set",
            target=code,
            default_model=default_model,
            allowed_models=names,
        )
        return row

    # ------------------------------------------------------------------ 额度

    async def budgets(self) -> list[GwBudget]:
        return list(await self.session.scalars(select(GwBudget).order_by(GwBudget.created_at)))

    async def budget(self, budget_id: Any) -> GwBudget:
        row = await self.session.scalar(select(GwBudget).where(GwBudget.uuid == budget_id))
        if row is None:
            raise NotFound("额度不存在")
        return row

    async def spent(self, budget: GwBudget) -> tuple[Decimal, int]:
        """自 starts_at 以来这个提供方的模型估算花费（同币种），以及没有价格的调用数。"""
        models = select(GwModel.name).where(GwModel.provider == budget.provider)
        since = (GwModelCall.model.in_(models), GwModelCall.created_at >= budget.starts_at)
        total = await self.session.scalar(
            select(func.coalesce(func.sum(GwModelCall.cost), 0)).where(
                *since, GwModelCall.currency == budget.currency
            )
        )
        unpriced = await self.session.scalar(
            select(func.count())
            .select_from(GwModelCall)
            .where(*since, GwModelCall.usage_reported.is_(True), GwModelCall.cost.is_(None))
        )
        return Decimal(str(total or 0)), int(unpriced or 0)
