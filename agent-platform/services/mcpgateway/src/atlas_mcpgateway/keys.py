"""API Key（参考模型管理平台）：Key 本身是权限主体。

    用户 Key     本人申请，一人一把；给 Claude Code、Cursor 这类外部客户端用
    智能体 Key   应用调接口创建（Atlas 新建智能体时，客户端类型 Agent、编码 = 智能体 id），
                 按客户端类型 + 编码唯一；管理员也可以在页面上为还没有 Key 的智能体补申请

    申请   在用户中心建主体（数字员工，负责人 = 申请人 / 智能体的负责人）→ 生成原文
    重置   只换原文，主体和授权不变；旧原文立即失效
    停用   网关拒绝这把 Key 的所有调用；授权保留，启用后恢复
    删除   停用用户中心里的主体（主体停用后用户中心对它一律回「无权限」），删掉 Key

★ 申请后马上能调公开服务；私有服务要服务所有者或管理员给 Key 分配权限。所以没有「待分配」状态。
★ 原文存库：Atlas 要取原文去调网关。用户 Key 只在申请、重置时把原文返回一次；
  智能体 Key 不在页面上返回原文，应用经内部接口获取。
"""

from __future__ import annotations

import logging
import secrets
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit
from .db.models import McpApiKey
from .db.types import utcnow, uuid7
from .errors import Conflict, Invalid, NotFound
from .perms import Permissions

logger = logging.getLogger(__name__)

__all__ = ["KeyService", "mask"]

_PREFIX = "mk-"
_CODE_MAX = 128


def _generate() -> str:
    return _PREFIX + secrets.token_urlsafe(32)


def mask(secret: str) -> str:
    return f"{_PREFIX}…{secret[-4:]}"


def _target(key: McpApiKey) -> str:
    if key.key_type == "user":
        return f"key:user:{key.user_uuid}"
    return f"key:client:{key.client_type}:{key.client_code}"


class KeyService:
    def __init__(self, session: AsyncSession, perms: Permissions) -> None:
        self.session = session
        self.perms = perms

    # ── 查询

    async def get(self, uuid: UUID) -> McpApiKey:
        row = await self.session.scalar(select(McpApiKey).where(McpApiKey.uuid == uuid))
        if row is None:
            raise NotFound("API Key 不存在", key=str(uuid))
        return row

    async def user_key(self, uid: UUID) -> McpApiKey | None:
        return await self.session.scalar(
            select(McpApiKey).where(McpApiKey.key_type == "user", McpApiKey.user_uuid == uid)
        )

    async def client_key(self, client_type: str, client_code: str) -> McpApiKey | None:
        return await self.session.scalar(
            select(McpApiKey).where(
                McpApiKey.key_type == "client",
                McpApiKey.client_type == client_type,
                McpApiKey.client_code == client_code.strip(),
            )
        )

    async def authenticate(self, secret: str) -> McpApiKey | None:
        """数据面：原文 → 正常的 Key。停用、重置立即生效（每个请求查库，不缓存）。"""
        return await self.session.scalar(
            select(McpApiKey).where(McpApiKey.secret == secret, McpApiKey.status == "active")
        )

    async def list(
        self, *, key_type: str | None = None, status: str | None = None, q: str | None = None
    ) -> list[McpApiKey]:
        stmt = select(McpApiKey)
        if key_type:
            stmt = stmt.where(McpApiKey.key_type == key_type)
        if status:
            stmt = stmt.where(McpApiKey.status == status)
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(or_(McpApiKey.name.ilike(like), McpApiKey.client_code.ilike(like)))
        return list(
            await self.session.scalars(stmt.order_by(McpApiKey.key_type, McpApiKey.created_at))
        )

    async def by_subjects(self) -> dict[str, McpApiKey]:
        """主体 uid → Key（授权列表里认出哪条授权是哪把 Key 的）。"""
        return {str(k.subject_uid): k for k in await self.session.scalars(select(McpApiKey))}

    # ── 申请

    async def apply(self, uid: UUID, *, actor: str) -> tuple[McpApiKey, str]:
        """本人申请用户 Key：一人一把，原文只在这次返回。"""
        if await self.user_key(uid) is not None:
            raise Conflict("你已经有一把用户 Key，需要新的请重置")
        info = await self.perms.user(str(uid))
        if info is None or info.get("status") != "active":
            raise Invalid("用户中心里找不到你，或账号已停用")
        if info.get("user_type") == "digital":
            raise Invalid("数字员工不能申请用户 Key")
        name = str(info.get("name") or info.get("account") or uid)
        key = McpApiKey(uuid=uuid7(), key_type="user", user_uuid=uid, name=name[:200])
        await self._create(key, owner=str(uid), subject_name=f"MCP Key · {name}", actor=actor)
        return key, key.secret

    async def ensure_client(
        self, *, client_type: str, client_code: str, name: str, owner: str, actor: str
    ) -> tuple[McpApiKey, bool]:
        """→ (Key, 是否新建)。已存在就返回它（名字不同则改名）：应用重试不会重复创建。

        owner：用户中心主体的负责人（必须是人，Atlas 给智能体的负责人）。
        """
        client_code = client_code.strip()
        if not client_code or len(client_code) > _CODE_MAX:
            raise Invalid(f"客户端编码不能为空，且不超过 {_CODE_MAX} 个字符")
        found = await self.client_key(client_type, client_code)
        if found is not None:
            if name and name != found.name:
                found.name = name[:200]
            return found, False
        key = McpApiKey(
            uuid=uuid7(),
            key_type="client",
            client_type=client_type,
            client_code=client_code,
            name=(name or client_code)[:200],
        )
        try:
            await self._create(
                key, owner=owner, subject_name=f"MCP · {key.name}", actor=actor, nested=True
            )
        except IntegrityError:
            # 并发创建了同一个客户端：用先建出来的那把
            found = await self.client_key(client_type, client_code)
            if found is None:
                raise
            return found, False
        return key, True

    async def _create(
        self, key: McpApiKey, *, owner: str, subject_name: str, actor: str, nested: bool = False
    ) -> None:
        # ★ 主体按 Key 的 uuid 幂等创建：写库失败后重试拿到的是同一个主体
        key.subject_uid = UUID(
            await self.perms.create_subject(ref=f"key:{key.uuid}", name=subject_name, owner=owner)
        )
        key.secret, key.status, key.rotated_at = _generate(), "active", utcnow()
        key.created_by = actor
        self.session.add(key)
        if nested:
            async with self.session.begin_nested():
                await self.session.flush()
        else:
            await self.session.flush()
        audit.record(self.session, actor=actor, action="key.create", target=_target(key))

    # ── 重置、停用、删除

    async def reset(self, key: McpApiKey, *, actor: str) -> str:
        key.secret, key.rotated_at = _generate(), utcnow()
        audit.record(self.session, actor=actor, action="key.reset", target=_target(key))
        return key.secret

    async def block(self, key: McpApiKey, *, actor: str) -> None:
        key.status = "blocked"
        audit.record(self.session, actor=actor, action="key.block", target=_target(key))

    async def unblock(self, key: McpApiKey, *, actor: str) -> None:
        key.status = "active"
        audit.record(self.session, actor=actor, action="key.unblock", target=_target(key))

    async def rename(self, key: McpApiKey, name: str) -> None:
        key.name = (name or key.client_code or key.name)[:200]

    async def delete(self, key: McpApiKey, *, actor: str) -> None:
        """停用用户中心里的主体，让它的授权全部失效；再删掉 Key。"""
        await self.perms.set_subject_active(str(key.subject_uid), False)
        audit.record(self.session, actor=actor, action="key.delete", target=_target(key))
        await self.session.delete(key)
        await self.session.flush()
