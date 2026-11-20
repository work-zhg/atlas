"""API Key：调用凭据，决定能用哪些模型、限额多少。

    用户 Key    本人建，一人可以多把（用途名区分）；能选「公有 + 授权给自己或所在部门的私有」。
                只存摘要，原文只在创建、重置时给一次（管理员也看不到、不能重置）
    客户端 Key  应用（Atlas 建智能体时）或管理员建，按「类型 + 编码」幂等；
                管理员分配模型，不看授权。
                新智能体的 Key 建时默认分配全部公有模型（那一刻的快照，
                settings 可改成「等管理员分配」）。
                原文加密保存（应用要取原文）；重置后旧的再保留 10 分钟（正在跑的会话不断）
    限额        月预算（元，软上限）、每分钟请求数、有效期，都可以不设
    有效模型    勾选的 ∩ 此刻允许的（用户 Key 还要看用户中心的授权、持有人是否在职）。网关只认它；
                授权收回、模型删除、改成私有时重算，用户中心那边的变化由定期任务补（jobs.py）

★ 改完把配置版本号加一（notify.mark）。
"""

from __future__ import annotations

import logging
import secrets as pysecrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import or_, select, type_coerce
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit, notify
from .crypto import CipherError, SecretCipher
from .db.models import GwApiKey, GwModel
from .db.types import uuid7
from .errors import Forbidden, Invalid, NotFound
from .gateway.admission import key_digest
from .perms import SubjectsCache
from .registry import ModelRegistry

__all__ = ["KeyLimits", "KeyService", "mask"]

logger = logging.getLogger(__name__)

_CODE_MAX = 128
#: 客户端 Key 重置后旧的再保留多久
RESET_GRACE = timedelta(minutes=10)


def mask(tail: str | None) -> str | None:
    return f"sk-…{tail}" if tail else None


def _new_secret() -> str:
    return f"sk-{pysecrets.token_urlsafe(36)}"


@dataclass(frozen=True)
class KeyLimits:
    monthly_budget: Decimal | None = None
    rpm: int | None = None
    expires_at: datetime | None = None


class KeyService:
    def __init__(
        self,
        session: AsyncSession,
        registry: ModelRegistry,
        subjects: SubjectsCache,
        cipher: SecretCipher,
    ) -> None:
        self.session = session
        self.registry = registry
        self.subjects = subjects
        self.cipher = cipher

    # ── 查 ─────────────────────────────────────────────────────────────

    async def get(self, uuid: UUID) -> GwApiKey:
        key = await self.session.scalar(select(GwApiKey).where(GwApiKey.uuid == uuid))
        if key is None:
            raise NotFound("Key 不存在")
        return key

    async def user_keys(self, uid: UUID) -> list[GwApiKey]:
        return list(
            await self.session.scalars(
                select(GwApiKey)
                .where(GwApiKey.key_type == "user", GwApiKey.user_uuid == uid)
                .order_by(GwApiKey.created_at)
            )
        )

    async def user_key(self, uid: UUID, key_uuid: UUID) -> GwApiKey:
        key = await self.get(key_uuid)
        if key.key_type != "user" or key.user_uuid != uid:
            raise NotFound("Key 不存在")
        return key

    async def client_key(self, client_type: str, client_code: str) -> GwApiKey | None:
        return await self.session.scalar(
            select(GwApiKey).where(
                GwApiKey.key_type == "client",
                GwApiKey.client_type == client_type,
                GwApiKey.client_code == client_code.strip(),
            )
        )

    async def search(
        self,
        *,
        key_type: str | None = None,
        client_type: str | None = None,
        status: str | None = None,
        q: str | None = None,
    ) -> list[GwApiKey]:
        stmt = select(GwApiKey).order_by(GwApiKey.created_at.desc())
        if key_type:
            stmt = stmt.where(GwApiKey.key_type == key_type)
        if client_type:
            stmt = stmt.where(GwApiKey.client_type == client_type)
        if status:
            stmt = stmt.where(GwApiKey.status == status)
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(
                    GwApiKey.name.ilike(like),
                    GwApiKey.label.ilike(like),
                    GwApiKey.client_code.ilike(like),
                )
            )
        return list(await self.session.scalars(stmt))

    async def containing(self, model: str) -> list[GwApiKey]:
        """勾选了这个模型的 Key（JSON 数组包含）。"""
        jsonb = type_coerce(GwApiKey.models, postgresql.JSONB)
        return list(await self.session.scalars(select(GwApiKey).where(jsonb.contains([model]))))

    def reveal(self, key: GwApiKey) -> str | None:
        """客户端 Key 的原文（应用取 Key 时）。旧数据可能还是明文。"""
        if key.key_type != "client" or not key.secret:
            return None
        if not key.secret.startswith("v1:"):
            return key.secret
        try:
            return self.cipher.decrypt(key.secret)
        except CipherError:
            logger.error("客户端 Key %s 的原文解不开", key.uuid)
            return None

    # ── 能选什么、此刻能用什么 ─────────────────────────────────────────

    async def allowed(
        self, key_type: str, user: UUID | None, *, fresh: bool = False
    ) -> dict[str, GwModel]:
        """能分配给这把 Key 的模型（停用的也算：网关会报「模型已停用」，不是「没分配」）。"""
        models = await self.registry.by_names()
        if key_type == "client":
            return models
        assert user is not None
        info = await self.subjects.info(str(user), fresh=fresh)
        if info is None or not info.active:
            return {}
        usable = await self.subjects.usable(str(user), fresh=fresh)
        return {n: m for n, m in models.items() if m.visibility == "public" or usable.allows(n)}

    async def refresh(self, key: GwApiKey, *, fresh: bool = False) -> bool:
        """重算有效模型。→ 变没变。"""
        allowed = await self.allowed(key.key_type, key.user_uuid, fresh=fresh)
        effective = [m for m in key.models or [] if m in allowed]
        if effective == list(key.effective_models or []):
            return False
        key.effective_models = effective
        notify.mark(self.session)
        return True

    async def refresh_many(self, keys: Sequence[GwApiKey], *, fresh: bool = False) -> int:
        changed = 0
        for key in keys:
            try:
                changed += await self.refresh(key, fresh=fresh)
            except Exception:
                logger.warning("重算 Key %s 的有效模型失败", key.uuid, exc_info=True)
        return changed

    async def _validate_models(self, key: GwApiKey, models: Sequence[str]) -> list[str]:
        models = list(dict.fromkeys(m.strip() for m in models if m.strip()))
        allowed = await self.allowed(key.key_type, key.user_uuid, fresh=True)
        bad = [m for m in models if m not in allowed]
        if bad:
            raise Invalid(f"不能选这些模型：{', '.join(bad)}", models=bad)
        return models

    # ── 用户 Key ───────────────────────────────────────────────────────

    async def create_user_key(
        self, uid: UUID, *, label: str, models: Sequence[str], limits: KeyLimits | None = None
    ) -> tuple[GwApiKey, str]:
        """本人建一把用户 Key，原文只在这次返回。"""
        info = await self.subjects.info(str(uid), fresh=True)
        if info is None or not info.active:
            raise Invalid("用户中心里找不到你，或账号已停用")
        key = GwApiKey(
            uuid=uuid7(),
            key_type="user",
            user_uuid=uid,
            name=info.name,
            label=(label or "我的 Key").strip()[:100],
            models=[],
            status="active",
            created_by=str(uid),
        )
        key.models = await self._validate_models(key, models)
        self._apply_limits(key, limits or KeyLimits())
        secret = self._issue(key, keep_original=False)
        self.session.add(key)
        await self.session.flush()
        await self.refresh(key)
        audit.record(
            self.session,
            actor=str(uid),
            action="key.create",
            target=_target(key),
            models=key.models,
        )
        notify.mark(self.session)
        return key, secret

    # ── 客户端 Key ─────────────────────────────────────────────────────

    async def ensure_client(
        self,
        *,
        client_type: str,
        client_code: str,
        name: str,
        actor: str,
        default_public: bool = False,
    ) -> tuple[GwApiKey, bool]:
        """→ (Key, 是否新建)。已存在就返回它（名字不同则改名）：应用重试不会重复创建。

        default_public：建时分配全部公有模型（那一刻的快照），马上能用；否则「待分配」。
        """
        client_code = client_code.strip()
        if not client_code or len(client_code) > _CODE_MAX:
            raise Invalid(f"客户端编码不能为空，且不超过 {_CODE_MAX} 个字符")
        found = await self.client_key(client_type, client_code)
        if found is not None:
            if name and name != found.name:
                found.name = name[:200]
            return found, False
        key = GwApiKey(
            uuid=uuid7(),
            key_type="client",
            client_type=client_type,
            client_code=client_code,
            name=(name or client_code)[:200],
            models=[],
            status="pending",
            created_by=actor,
        )
        if default_public:
            models = await self.registry.by_names()
            key.models = sorted(
                n
                for n, m in models.items()
                if m.visibility == "public" and m.enabled and m.kind == "chat"
            )
            if key.models:
                key.status = "active"
                self._issue(key, keep_original=True)
        self.session.add(key)
        try:
            async with self.session.begin_nested():
                await self.session.flush()
        except IntegrityError:
            # 并发创建了同一个客户端：用先建出来的那把
            found = await self.client_key(client_type, client_code)
            if found is None:
                raise
            return found, False
        await self.refresh(key)
        audit.record(
            self.session,
            actor=actor,
            action="key.client.create",
            target=_target(key),
            models=key.models,
        )
        notify.mark(self.session)
        return key, True

    async def rename_client(self, key: GwApiKey, name: str) -> None:
        key.name = name[:200]

    # ── 共用 ───────────────────────────────────────────────────────────

    async def assign(self, key: GwApiKey, models: Sequence[str], *, actor: str) -> str | None:
        """改勾选的模型。待分配的客户端 Key 第一次分配时生成原文 → 返回它；否则 None。"""
        key.models = await self._validate_models(key, models)
        secret: str | None = None
        if key.status == "pending" and key.models:
            secret = self._issue(key, keep_original=True)
            key.status = "active"
        await self.refresh(key)
        audit.record(
            self.session, actor=actor, action="key.assign", target=_target(key), models=key.models
        )
        notify.mark(self.session)
        return secret

    async def update(
        self,
        key: GwApiKey,
        *,
        label: str | None = None,
        models: Sequence[str] | None = None,
        limits: KeyLimits | None = None,
        actor: str,
    ) -> str | None:
        secret = None
        if label is not None:
            key.label = label.strip()[:100]
        if limits is not None:
            self._apply_limits(key, limits)
        if models is not None:
            secret = await self.assign(key, models, actor=actor)
        else:
            notify.mark(self.session)
        return secret

    async def reset(self, key: GwApiKey, *, actor: str) -> str:
        """生成新原文。客户端 Key 的旧原文再保留 10 分钟；用户 Key 的旧原文立即失效。"""
        if key.status == "pending":
            raise Invalid("这把 Key 还没有分配模型，没有可重置的 Key")
        if key.key_type == "client" and key.secret_digest:
            key.prev_digest, key.prev_until = key.secret_digest, datetime.now(UTC) + RESET_GRACE
        secret = self._issue(key, keep_original=key.key_type == "client")
        audit.record(self.session, actor=actor, action="key.reset", target=_target(key))
        notify.mark(self.session)
        return secret

    async def block(self, key: GwApiKey, *, reason: str = "", actor: str) -> None:
        if key.status == "pending":
            raise Invalid("这把 Key 还没有分配模型，不需要停用")
        key.status = "blocked"
        key.block_reason = reason.strip()[:200]
        key.prev_digest = key.prev_until = None
        audit.record(
            self.session,
            actor=actor,
            action="key.block",
            target=_target(key),
            reason=key.block_reason,
        )
        notify.mark(self.session)

    async def unblock(self, key: GwApiKey, *, actor: str) -> None:
        if key.status != "blocked":
            return
        key.status = "active"
        key.block_reason = ""
        audit.record(self.session, actor=actor, action="key.unblock", target=_target(key))
        notify.mark(self.session)

    async def delete(self, key: GwApiKey, *, actor: str) -> None:
        audit.record(self.session, actor=actor, action="key.delete", target=_target(key))
        await self.session.delete(key)
        await self.session.flush()
        notify.mark(self.session)

    def ensure_owner(self, key: GwApiKey, uid: UUID) -> None:
        if key.key_type != "user" or key.user_uuid != uid:
            raise Forbidden("只能操作自己的 Key")

    # ── 小步骤 ─────────────────────────────────────────────────────────

    def _issue(self, key: GwApiKey, *, keep_original: bool) -> str:
        """生成原文：存摘要和末 4 位；客户端 Key 还加密存原文（应用要取）。"""
        secret = _new_secret()
        key.secret_digest = key_digest(secret)
        key.secret_tail = secret[-4:]
        key.secret = self.cipher.encrypt(secret) if keep_original else None
        key.rotated_at = datetime.now(UTC)
        return secret

    @staticmethod
    def _apply_limits(key: GwApiKey, limits: KeyLimits) -> None:
        if limits.monthly_budget is not None and limits.monthly_budget < 0:
            raise Invalid("月预算不能是负数")
        if limits.rpm is not None and limits.rpm < 1:
            raise Invalid("每分钟请求数至少是 1")
        key.monthly_budget = limits.monthly_budget
        key.rpm = limits.rpm
        key.expires_at = limits.expires_at


def _target(key: GwApiKey) -> str:
    if key.key_type == "user":
        return f"key:user:{key.user_uuid}:{key.uuid}"
    return f"key:client:{key.client_type}:{key.client_code}"
