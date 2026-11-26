"""账号：对接一个供应商接口的凭据（地址、密钥、格式、出网方式）。一个供应商可以有多个账号。

    登记、修改    地址只能是这家供应商提供的格式；写死内部 IP 或内网域名的直接拒绝（网关直连前还会按
                  解析出的 IP 绑定建连防重绑定）；密钥只写不读，界面只显示末 4 位
    测试连接      连通 → 拉模型列表 → 按已配线路或模型类型发最小探测请求（对话/向量/重排）；
                  能查余额的顺带查
    状态          正常 / 冷却中（被限流，自动恢复）/ 已停用 /
                  异常（余额不足、密钥失效，修好后自动恢复）
                  冷却、异常在网关状态里（网关写，这里读）
    删除          还有线路在用的不能删（先在模型里移除线路）

★ 改完按租户递增配置版本号（notify.mark），别的租户不受影响。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit, notify
from .crypto import SecretCipher
from .db.models import GwAccount, GwModel, GwRoute
from .errors import Conflict, Invalid, NotFound
from .provider import Op, ProviderCatalog
from .shared.health import IssueRecord, RouteHealth
from .shared.kernel import AccountRef, Egress, Format, ModelKind
from .shared.presets import PresetRules
from .shared.upstream import AddressGuard, UpstreamClient, UpstreamError

__all__ = ["AccountInput", "AccountService", "AccountStatus", "ProbeResult", "account_fields"]

logger = logging.getLogger(__name__)

_PRESETS = PresetRules()


@dataclass(frozen=True)
class AccountInput:
    name: str
    provider: str
    endpoints: dict[str, str]
    #: None = 不改（修改时）；空串 = 清掉（自部署可以没有密钥）
    secret: str | None
    egress: str = "direct"
    first_content_timeout_s: int | None = None
    balance_alert: Decimal | None = None
    remark: str = ""
    enabled: bool = True


@dataclass(frozen=True)
class AccountStatus:
    #: ok / cooling / abnormal / disabled
    state: str
    issue: IssueRecord | None = None
    #: 冷却中的线路数
    cooling: int = 0
    low_balance: bool = False


@dataclass
class ProbeStep:
    name: str
    ok: bool
    message: str = ""


@dataclass
class ProbeResult:
    ok: bool
    steps: list[ProbeStep] = field(default_factory=list)
    upstream_models: list[str] | None = None
    balance: Decimal | None = None


class AccountService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        cipher: SecretCipher,
        catalog: ProviderCatalog,
        guard: AddressGuard,
        health: RouteHealth,
    ) -> None:
        self.session = session
        self.cipher = cipher
        self.catalog = catalog
        self.guard = guard
        self.health = health

    # ── 查 ─────────────────────────────────────────────────────────────

    async def list(self) -> list[GwAccount]:
        return list(await self.session.scalars(select(GwAccount).order_by(GwAccount.created_at)))

    async def get(self, uuid: UUID) -> GwAccount:
        row = await self.session.scalar(select(GwAccount).where(GwAccount.uuid == uuid))
        if row is None:
            raise NotFound("账号不存在")
        return row

    async def route_counts(self) -> dict[UUID, int]:
        rows = await self.session.execute(
            select(GwRoute.account_uuid, func.count()).group_by(GwRoute.account_uuid)
        )
        return {a: n for a, n in rows}

    async def status(self, rows: Sequence[GwAccount]) -> dict[UUID, AccountStatus]:
        ids = {a.uuid for a in rows}
        refs = [r for r in await self.session.scalars(select(GwRoute)) if r.account_uuid in ids]
        cooling, issues = await self.health.lookup([r.uuid for r in refs], list(ids))
        now = datetime.now(UTC).timestamp()
        out: dict[UUID, AccountStatus] = {}
        for a in rows:
            low = bool(
                a.balance_alert is not None
                and a.balance is not None
                and a.balance < a.balance_alert
            )
            n = sum(1 for r in refs if r.account_uuid == a.uuid and cooling.get(r.uuid, 0) > now)
            if not a.enabled:
                out[a.uuid] = AccountStatus("disabled", low_balance=low)
            elif a.uuid in issues:
                out[a.uuid] = AccountStatus("abnormal", issues[a.uuid], n, low)
            elif n:
                out[a.uuid] = AccountStatus("cooling", cooling=n, low_balance=low)
            else:
                out[a.uuid] = AccountStatus("ok", low_balance=low)
        return out

    def ref(self, row: GwAccount, *, secret: str | None = None) -> AccountRef:
        """发请求要用的样子（解密密钥）。secret 不为 None 时用它（保存前测试连接）。"""
        if secret is None:
            secret = self.cipher.decrypt(row.secret_cipher) if row.secret_cipher else ""
        known = {f.value for f in Format}
        return AccountRef(
            uuid=row.uuid,
            provider=row.provider,
            endpoints={Format(k): v for k, v in (row.endpoints or {}).items() if v and k in known},
            secret=secret,
            egress=Egress(row.egress),
            first_content_timeout_s=float(row.first_content_timeout_s)
            if row.first_content_timeout_s
            else None,
            enabled=row.enabled,
        )

    # ── 改 ─────────────────────────────────────────────────────────────

    def _validate(self, data: AccountInput, *, creating: bool) -> dict[str, str]:
        if not data.name.strip():
            raise Invalid("账号名不能为空")
        if not self.catalog.known(data.provider):
            raise Invalid(f"不认识的供应商：{data.provider}")
        spec = self.catalog.spec(data.provider)
        if data.egress not in ("direct", "proxy"):
            raise Invalid("出网方式只能是直连或经出网代理")
        endpoints = {k: v.strip() for k, v in data.endpoints.items() if v and v.strip()}
        if not endpoints:
            raise Invalid("至少登记一个地址")
        for fmt, url in endpoints.items():
            if fmt not in {f.value for f in spec.formats}:
                raise Invalid(f"{spec.display_name} 不提供 {fmt} 格式的接口")
            parts = urlsplit(url.replace("{model}", "m"))
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise Invalid(f"地址不对：{url}")
            reason = self.guard.validate_static(url.replace("{model}", "m"))
            if reason:
                raise Invalid(f"{url}：{reason}")
        if creating and not data.secret and not spec.key_optional:
            raise Invalid("密钥不能为空")
        return endpoints

    async def create(self, data: AccountInput, *, actor: str) -> GwAccount:
        endpoints = self._validate(data, creating=True)
        await self._ensure_name_free(data.name.strip())
        row = GwAccount(
            name=data.name.strip()[:100],
            provider=data.provider,
            endpoints=endpoints,
            egress=data.egress,
            first_content_timeout_s=data.first_content_timeout_s,
            balance_alert=data.balance_alert,
            remark=data.remark,
            enabled=data.enabled,
            created_by=actor,
            updated_by=actor,
        )
        self._set_secret(row, data.secret or "")
        self.session.add(row)
        await self.session.flush()
        audit.record(
            self.session,
            actor=actor,
            action="account.create",
            target=f"account:{row.uuid}",
            name=row.name,
        )
        notify.mark(self.session, tenant=row.tenant_uuid, tenant_changed=True)
        return row

    async def update(self, row: GwAccount, data: AccountInput, *, actor: str) -> GwAccount:
        if data.provider != row.provider:
            raise Invalid("账号建好后不能换供应商，换供应商请新建账号")
        endpoints = self._validate(data, creating=False)
        if data.name.strip() != row.name:
            await self._ensure_name_free(data.name.strip())
        row.name = data.name.strip()[:100]
        row.endpoints = endpoints
        row.egress = data.egress
        row.first_content_timeout_s = data.first_content_timeout_s
        row.balance_alert = data.balance_alert
        row.remark = data.remark
        row.enabled = data.enabled
        row.updated_by = actor
        if data.secret is not None:
            self._set_secret(row, data.secret)
        audit.record(
            self.session,
            actor=actor,
            action="account.update",
            target=f"account:{row.uuid}",
            secret_changed=data.secret is not None,
        )
        notify.mark(self.session, tenant=row.tenant_uuid, tenant_changed=True)
        return row

    async def set_enabled(self, row: GwAccount, enabled: bool, *, actor: str) -> None:
        row.enabled = enabled
        row.updated_by = actor
        audit.record(
            self.session,
            actor=actor,
            action="account.enable" if enabled else "account.disable",
            target=f"account:{row.uuid}",
        )
        notify.mark(self.session, tenant=row.tenant_uuid, tenant_changed=True)

    async def delete(self, row: GwAccount, *, actor: str) -> None:
        used = await self.session.scalar(
            select(func.count()).select_from(GwRoute).where(GwRoute.account_uuid == row.uuid)
        )
        if used:
            raise Conflict(f"还有 {used} 条线路用着「{row.name}」，先在这些模型里移除线路再删除")
        tenant_uuid = row.tenant_uuid
        audit.record(
            self.session,
            actor=actor,
            action="account.delete",
            target=f"account:{row.uuid}",
            name=row.name,
        )
        await self.session.delete(row)
        await self.session.flush()
        notify.mark(self.session, tenant=tenant_uuid, tenant_changed=True)

    def _set_secret(self, row: GwAccount, secret: str) -> None:
        secret = secret.strip()
        row.secret_cipher = self.cipher.encrypt(secret) if secret else None
        row.secret_tail = secret[-4:] if secret else ""

    async def _ensure_name_free(self, name: str) -> None:
        if await self.session.scalar(select(GwAccount.id).where(GwAccount.name == name)):
            raise Conflict(f"已经有叫「{name}」的账号了")

    # ── 测试连接、余额、恢复 ────────────────────────────────────────────

    async def _configured_target(self, account_uuid: UUID) -> tuple[str | None, ModelKind | None]:
        """优先用本账号已挂线路与模型类型来探测（兼容不支持 list_models 或纯向量/重排账号）。"""
        if account_uuid == UUID(int=0):
            return None, None
        row = (
            await self.session.execute(
                select(GwRoute.upstream_model, GwModel.kind)
                .join(GwModel, GwModel.uuid == GwRoute.model_uuid)
                .where(GwRoute.account_uuid == account_uuid)
                .order_by(GwRoute.priority, GwRoute.created_at)
                .limit(1)
            )
        ).first()
        if row is None:
            return None, None
        return str(row[0]), ModelKind(str(row[1]))

    async def probe(
        self,
        ref: AccountRef,
        client: UpstreamClient,
        *,
        upstream_model: str | None = None,
        model_kind: ModelKind | None = None,
    ) -> ProbeResult:
        """连通 → 拉模型列表（验证密钥）→ 按模型类型发最小探测请求 → 余额（能查的话）。"""
        dialect = self.catalog.dialect(ref.provider)
        result = ProbeResult(ok=True)
        http = client.client(ref.egress)
        try:
            models = await dialect.list_models(ref, http)
            result.upstream_models = models
            if models is not None:
                result.steps.append(ProbeStep("拉模型列表", True, f"{len(models)} 个模型"))
        except Exception as exc:
            result.ok = False
            result.steps.append(ProbeStep("拉模型列表", False, _brief(exc)))

        cfg_model, cfg_kind = (
            await self._configured_target(ref.uuid) if not upstream_model else (None, None)
        )
        model = upstream_model or cfg_model or (result.upstream_models or [None])[0]
        kind = model_kind or cfg_kind
        if model:
            if kind is None:
                preset = _PRESETS.caps_for(ref.provider, model)
                kind = preset.kind if preset is not None else ModelKind.CHAT
            step = await self._probe_call(ref, client, model, kind)
            result.steps.append(step)
            result.ok = result.ok and step.ok
        try:
            balance = await dialect.balance(ref, http)
            if balance is not None:
                result.balance = balance
                result.steps.append(ProbeStep("查余额", balance > 0, f"余额 {balance}"))
                if balance <= 0:
                    result.ok = False
        except Exception as exc:
            result.steps.append(ProbeStep("查余额", False, _brief(exc)))

        if not result.steps:
            # 不支持拉模型列表、不支持查余额、又没配线路和上游模型：不能空跑一趟假装恢复成功
            result.ok = False
            result.steps.append(
                ProbeStep(
                    "检查凭据", False, "这家供应商不支持拉模型列表，请指定上游模型或先配线路再测试"
                )
            )
        return result

    async def _probe_call(
        self, ref: AccountRef, client: UpstreamClient, model: str, kind: ModelKind
    ) -> ProbeStep:
        dialect = self.catalog.dialect(ref.provider)
        if kind in (ModelKind.EMBEDDING, ModelKind.RERANK):
            if not ref.endpoints.get(Format.OPENAI):
                return ProbeStep(f"探测 {model}", False, "向量与重排需要登记 OpenAI 格式地址")
            fmt = Format.OPENAI
            if kind is ModelKind.EMBEDDING:
                step_name = f"用 {model} 探测向量接口"
                op, body = Op.EMBEDDINGS, {"model": model, "input": "hi"}
            else:
                step_name = f"用 {model} 探测重排接口"
                op, body = Op.RERANK, {"model": model, "query": "hi", "documents": ["hi"]}
        else:
            fmt = next(
                (
                    f
                    for f in (Format.ANTHROPIC, Format.OPENAI, Format.GOOGLE)
                    if ref.endpoints.get(f)
                ),
                None,
            )
            step_name = f"用 {model} 发 1 token 的请求"
            if fmt is None:
                return ProbeStep("发 1 token 的请求", False, "没有登记地址")
            hello = [{"role": "user", "content": "hi"}]
            match fmt:
                case Format.ANTHROPIC:
                    op, body = Op.MESSAGES, {"model": model, "max_tokens": 1, "messages": hello}
                case Format.OPENAI:
                    op, body = Op.CHAT, {"model": model, "max_tokens": 1, "messages": hello}
                case _:
                    op, body = (
                        Op.GENERATE,
                        {
                            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                            "generationConfig": {"maxOutputTokens": 1},
                        },
                    )
        headers = {"content-type": "application/json", **dialect.auth(fmt, ref.secret)}
        if fmt is Format.ANTHROPIC:
            headers.setdefault("anthropic-version", "2023-06-01")
        try:
            resp = await client.send(
                dialect.url(fmt, ref.endpoints[fmt], op, model),
                headers=headers,
                body=body,
                egress=ref.egress,
                stream=False,
                read_timeout_s=60.0,
            )
        except UpstreamError as exc:
            return ProbeStep(step_name, False, exc.failure.message)
        if resp.status_code >= 400:
            failure = dialect.classify(resp.status_code, resp.headers, resp.content)
            return ProbeStep(step_name, False, f"{failure.kind.value}：{failure.message}")
        return ProbeStep(step_name, True, "正常")

    async def record_check(self, row: GwAccount, result: ProbeResult) -> None:
        row.checked_at = datetime.now(UTC)
        row.check_error = (
            ""
            if result.ok
            else "；".join(f"{s.name}：{s.message}" for s in result.steps if not s.ok)[:1000]
        )
        if result.balance is not None:
            row.balance, row.balance_at = result.balance, row.checked_at
        if result.ok:
            await self.health.clear(row.uuid)


def _brief(exc: Exception) -> str:
    text = str(exc) or type(exc).__name__
    return text[:200]


def account_fields(data: dict[str, Any]) -> AccountInput:
    """接口 / 命令行的字典 → AccountInput。"""
    return AccountInput(
        name=str(data.get("name") or ""),
        provider=str(data.get("provider") or ""),
        endpoints=dict(data.get("endpoints") or {}),
        secret=data.get("secret"),
        egress=str(data.get("egress") or "direct"),
        first_content_timeout_s=data.get("first_content_timeout_s"),
        balance_alert=Decimal(str(data["balance_alert"]))
        if data.get("balance_alert") is not None
        else None,
        remark=str(data.get("remark") or ""),
        enabled=bool(data.get("enabled", True)),
    )
