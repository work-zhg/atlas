"""API Key（方案第 5、6 章）。

    用户 Key     本人申请，一人一把；从「公有 + 授权给本人或所在部门的私有」里选模型
    客户端 Key   应用调接口创建（或管理员在页面上建），按客户端类型 + 客户端编码唯一；
                 创建后「待分配」，管理员分配模型后才在 LiteLLM 生成 Key

    有效模型 = 分配的 ∩ 可选范围 ∩ LiteLLM 里存在的。同步到 LiteLLM 的就是它：
        LiteLLM 里的可用模型 = 有效模型
        LiteLLM 里的停用     = 状态是 blocked，或有效模型为空（空列表在 LiteLLM 里是全部模型）

★ 平台是事实源，LiteLLM 跟随。同步失败记在 sync_error 上，对账任务（reconcile.py）重试。
★ 用户在用户中心被停用（离职）：他的用户 Key 有效模型按空处理，即在 LiteLLM 里停用。
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit
from .db.models import GwApiKey, GwModel
from .db.types import utcnow, uuid7
from .errors import Conflict, GatewayError, Invalid, NotFound
from .litellm_api import LiteLLM
from .perms import SubjectsCache
from .registry import ModelRegistry

logger = logging.getLogger(__name__)

__all__ = ["KeyService", "mask"]

#: 客户端编码的上限（智能体 id、知识库 id 都在这之内）
_CODE_MAX = 128


def mask(secret: str | None) -> str | None:
    return f"sk-…{secret[-4:]}" if secret else None


class KeyService:
    def __init__(
        self,
        session: AsyncSession,
        litellm: LiteLLM,
        registry: ModelRegistry,
        subjects: SubjectsCache,
    ) -> None:
        self.session = session
        self.litellm = litellm
        self.registry = registry
        self.subjects = subjects

    # ── 查询

    async def get(self, uuid: UUID) -> GwApiKey:
        row = await self.session.scalar(select(GwApiKey).where(GwApiKey.uuid == uuid))
        if row is None:
            raise NotFound("Key 不存在", key=str(uuid))
        return row

    async def user_key(self, uid: UUID) -> GwApiKey | None:
        return await self.session.scalar(
            select(GwApiKey).where(GwApiKey.key_type == "user", GwApiKey.user_uuid == uid)
        )

    async def client_key(self, client_type: str, client_code: str) -> GwApiKey | None:
        return await self.session.scalar(
            select(GwApiKey).where(
                GwApiKey.key_type == "client",
                GwApiKey.client_type == client_type,
                GwApiKey.client_code == client_code,
            )
        )

    async def client_keys(
        self, *, client_type: str | None = None, status: str | None = None, q: str | None = None
    ) -> list[GwApiKey]:
        stmt = select(GwApiKey).where(GwApiKey.key_type == "client")
        if client_type:
            stmt = stmt.where(GwApiKey.client_type == client_type)
        if status:
            stmt = stmt.where(GwApiKey.status == status)
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(GwApiKey.client_code.ilike(like) | GwApiKey.name.ilike(like))
        return list(
            await self.session.scalars(stmt.order_by(GwApiKey.client_type, GwApiKey.created_at))
        )

    async def containing(self, model: str) -> list[GwApiKey]:
        """分配了这个模型的 Key（JSON 列表里有它）。"""
        rows = await self.session.scalars(select(GwApiKey))
        return [k for k in rows if model in (k.models or [])]

    # ── 可选范围与有效模型

    async def allowed(
        self, key_type: str, user: UUID | None, *, fresh: bool = False
    ) -> dict[str, GwModel]:
        """能分配给这把 Key 的模型（模型名 → 平台记录）。"""
        callable_models = await self.registry.callable()
        if key_type == "client":
            return callable_models
        assert user is not None
        info = await self.subjects.info(str(user), fresh=fresh)
        if info is None or not info.active:
            return {}
        usable = await self.subjects.usable(str(user), fresh=fresh)
        return {
            name: row
            for name, row in callable_models.items()
            if row.visibility == "public" or usable.allows(name)
        }

    async def effective(self, key: GwApiKey, *, fresh: bool = False) -> list[str]:
        allowed = await self.allowed(key.key_type, key.user_uuid, fresh=fresh)
        return [m for m in key.models or [] if m in allowed]

    # ── 用户 Key

    async def apply(self, uid: UUID, *, models: Sequence[str]) -> tuple[GwApiKey, str]:
        """本人申请用户 Key：一人一把，申请即生成，原文只在这次返回。"""
        if await self.user_key(uid) is not None:
            raise Conflict("你已经有一把用户 Key，需要新的请重置")
        info = await self.subjects.info(str(uid), fresh=True)
        if info is None or not info.active:
            raise Invalid("用户中心里找不到你，或账号已停用")
        key = GwApiKey(
            uuid=uuid7(),
            key_type="user",
            user_uuid=uid,
            name=info.name,
            models=[],
            status="active",
            created_by=str(uid),
        )
        await self._validate_models(key, models)
        key.models = list(dict.fromkeys(models))
        secret = await self._generate(key)
        self.session.add(key)
        await self._flush_or_revoke(key)
        audit.record(
            self.session, actor=str(uid), action="key.apply", target=_target(key), models=key.models
        )
        return key, secret

    # ── 客户端 Key

    async def ensure_client(
        self, *, client_type: str, client_code: str, name: str, actor: str
    ) -> tuple[GwApiKey, bool]:
        """→ (Key, 是否新建)。已存在就返回它（名字不同则改名）：应用重试不会重复创建。"""
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
        audit.record(self.session, actor=actor, action="key.client.create", target=_target(key))
        return key, True

    async def rename_client(self, key: GwApiKey, name: str) -> None:
        key.name = name[:200]

    # ── 共用：分配模型、重置、停用、删除

    async def assign(self, key: GwApiKey, models: Sequence[str], *, actor: str) -> str | None:
        """改分配的模型。客户端 Key 第一次分配时在 LiteLLM 生成 Key。

        → 新生成的原文（没有新生成为 None）。
        """
        await self._validate_models(key, models)
        key.models = list(dict.fromkeys(models))
        secret: str | None = None
        if key.status == "pending":
            secret = await self._generate(key)
            key.status = "active"
            await self._flush_or_revoke(key)
        else:
            await self.sync(key)
        audit.record(
            self.session, actor=actor, action="key.assign", target=_target(key), models=key.models
        )
        return secret

    async def reset(self, key: GwApiKey, *, actor: str) -> str:
        """生成新 Key、删除旧 Key；分配的模型和停用状态不变。旧的删不掉就撤回新的，保持原样。"""
        if key.secret is None:
            raise Invalid("这把 Key 还没有分配模型，没有可重置的 Key")
        old, old_token, old_rotated = key.secret, key.litellm_token, key.rotated_at
        secret = await self._generate(key)
        try:
            await self.litellm.delete_key(old)
        except GatewayError:
            await self._revoke_quietly(secret)
            key.secret, key.litellm_token, key.rotated_at = old, old_token, old_rotated
            raise
        audit.record(self.session, actor=actor, action="key.reset", target=_target(key))
        return secret

    async def block(self, key: GwApiKey, *, actor: str) -> None:
        if key.status == "pending":
            raise Invalid("这把 Key 还没有分配模型，不需要停用")
        key.status = "blocked"
        await self.sync(key)
        audit.record(self.session, actor=actor, action="key.block", target=_target(key))

    async def unblock(self, key: GwApiKey, *, actor: str) -> None:
        if key.status != "blocked":
            return
        key.status = "active"
        await self.sync(key)
        audit.record(self.session, actor=actor, action="key.unblock", target=_target(key))

    async def delete(self, key: GwApiKey, *, actor: str) -> None:
        if key.secret is not None:
            await self.litellm.delete_key(key.secret)
        audit.record(self.session, actor=actor, action="key.delete", target=_target(key))
        await self.session.delete(key)
        await self.session.flush()

    # ── 同步到 LiteLLM

    async def sync(self, key: GwApiKey, *, verify: bool = False, fresh: bool = False) -> bool:
        """把有效模型和停用状态推给 LiteLLM。→ 有没有改动 LiteLLM。失败记 sync_error，不抛。

        verify：先问 LiteLLM 这把 Key 现在是什么样（对账用：发现有人在 LiteLLM 里改过）；
        否则和上一次推过去的比。
        """
        if key.secret is None:
            return False
        try:
            effective = await self.effective(key, fresh=fresh)
            blocked = key.status == "blocked" or not effective
            current_models, current_blocked = list(key.synced_models or []), key.synced_blocked
            if verify:
                info = await self.litellm.key_info(key.secret)
                if info is None:
                    key.sync_error = "LiteLLM 里找不到这把 Key（可能在 LiteLLM 里被删了），请重置"
                    return False
                current_models = list(info.get("models") or [])
                current_blocked = bool(info.get("blocked"))
            changed = False
            # ★ 有效模型为空时不改可用模型（不能推空列表），只停用
            if effective and sorted(effective) != sorted(current_models):
                await self.litellm.update_key(key.secret, models=effective)
                changed = True
            if blocked != current_blocked:
                if blocked:
                    await self.litellm.block_key(key.secret)
                else:
                    await self.litellm.unblock_key(key.secret)
                changed = True
        except GatewayError as exc:
            key.sync_error = exc.message[:500]
            logger.warning("同步 Key %s 到 LiteLLM 失败：%s", _target(key), exc.message)
            return False
        if effective:
            key.synced_models = effective
        key.synced_blocked = blocked
        key.synced_at = utcnow()
        key.sync_error = None
        return changed

    async def resync(self, keys: Sequence[GwApiKey]) -> int:
        """模型的可见性、授权变了，或模型被删了：重新同步受影响的 Key。→ 改动了几把。"""
        self.subjects.forget()
        changed = 0
        for key in keys:
            if await self.sync(key, fresh=True):
                changed += 1
        return changed

    async def drop_model(self, model: str) -> list[GwApiKey]:
        """模型删除后：从所有 Key 的分配里去掉它。→ 受影响的 Key（调用方再 resync）。"""
        affected = await self.containing(model)
        for key in affected:
            key.models = [m for m in key.models if m != model]
        return affected

    # ── 内部

    async def _validate_models(self, key: GwApiKey, models: Sequence[str]) -> None:
        if not models:
            raise Invalid("至少选择一个模型")
        allowed = await self.allowed(key.key_type, key.user_uuid, fresh=True)
        denied = [m for m in dict.fromkeys(models) if m not in allowed]
        if denied:
            why = (
                "不是已登记且可调用的模型"
                if key.key_type == "client"
                else "不是公有模型，也没有授权给你或你所在的部门"
            )
            raise Invalid(f"这些模型{why}：{', '.join(denied)}", models=",".join(denied))

    async def _generate(self, key: GwApiKey) -> str:
        """在 LiteLLM 生成一把新 Key（模型 = 有效模型），记到 key 上。→ 原文。"""
        effective = await self.effective(key, fresh=True)
        generated = await self.litellm.generate_key(
            models=effective or list(key.models),
            alias=_alias(key, utcnow()),
            metadata=_metadata(key),
            user_id=str(key.user_uuid) if key.key_type == "user" else None,
        )
        key.secret = generated.key
        key.litellm_token = generated.token
        key.rotated_at = utcnow()
        key.synced_models = effective or list(key.models)
        key.synced_blocked = False
        key.synced_at = utcnow()
        key.sync_error = None
        if key.status == "blocked" or not effective:
            # 重置一把停用的 Key、或有效模型为空：新 Key 生成后立即停用
            try:
                await self.litellm.block_key(generated.key)
                key.synced_blocked = True
            except GatewayError as exc:
                key.sync_error = exc.message[:500]
        return generated.key

    async def _flush_or_revoke(self, key: GwApiKey) -> None:
        """刚在 LiteLLM 生成了 Key：写库失败就撤回，不留没人管的 Key。"""
        try:
            await self.session.flush()
        except Exception:
            if key.secret is not None:
                await self._revoke_quietly(key.secret)
            raise

    async def _revoke_quietly(self, secret: str) -> None:
        try:
            await self.litellm.delete_key(secret)
        except Exception:
            logger.exception("撤回 LiteLLM 里刚生成的 Key 失败：需要在 LiteLLM 里手工删除")


def _target(key: GwApiKey) -> str:
    if key.key_type == "user":
        return f"key:user:{key.user_uuid}"
    return f"key:client:{key.client_type}:{key.client_code}"


def _alias(key: GwApiKey, at: datetime) -> str:
    """LiteLLM 管理界面里看到的名字。

    ★ LiteLLM 要求别名全局唯一（1.106 实测），重置时新旧两把又同时存在：带上生成时间和一段随机串。
    """
    who = (
        f"user:{key.name or key.user_uuid}"
        if key.key_type == "user"
        else f"{key.client_type}:{key.client_code}"
    )
    return f"{who}@{at:%Y%m%d%H%M%S}-{secrets.token_hex(3)}"


def _metadata(key: GwApiKey) -> dict[str, str]:
    data = {"platform_key": str(key.uuid), "key_type": key.key_type}
    if key.key_type == "user":
        data["user_uuid"] = str(key.user_uuid)
    else:
        data |= {"client_type": str(key.client_type), "client_code": str(key.client_code)}
    return data
