"""沙箱账户的三张表：sandbox_account、user_sandbox、sandbox_binding（通用供给的仓储）。

c4-model/agent/sandbox/code-sandbox-provisioning.html。按人整体加载、整体保存，保存时校验并递增
账户的版本号；并发修改的一方拿到 AccountConflict，重读后再判。

★ by_biz 是热路径（工作区每次读写文件都要问）：只读，进程内缓存 2 秒；本进程的写入立刻作废缓存。
"""

from __future__ import annotations

import builtins
import time
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import ColumnElement, delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import SandboxAccountRow, SandboxBindingRow, UserSandboxRow
from ..db.session import fetch_one
from ..sandbox.provisioning.model import (
    BizRef,
    BizType,
    SandboxAccount,
    SandboxBinding,
    SandboxStatus,
    UserSandbox,
)

__all__ = ["AccountConflict", "SandboxAccountRepository", "forget_cached"]

#: (biz_type, biz_id) → (过期的 monotonic 时刻, 沙箱或 None)
_CACHE: dict[tuple[str, UUID], tuple[float, UserSandbox | None]] = {}


class AccountConflict(RuntimeError):
    """保存时版本号对不上（别人先改了这个人的账户），或一个业务已经绑在别的沙箱上。"""


def forget_cached(*bizs: BizRef) -> None:
    for biz in bizs:
        _CACHE.pop((biz.biz_type.value, biz.biz_id), None)


class SandboxAccountRepository:
    def __init__(self, session: AsyncSession, *, cache_s: float = 2.0) -> None:
        self._session = session
        self._cache_s = cache_s

    # ------------------------------------------------------------------ 聚合

    async def load(self, user_uuid: UUID) -> SandboxAccount:
        row = await fetch_one(
            self._session, SandboxAccountRow, SandboxAccountRow.user_uuid == user_uuid
        )
        sandboxes = await self._sandboxes(UserSandboxRow.user_uuid == user_uuid)
        if row is None:
            return SandboxAccount(user_uuid, version=0, sandboxes=sandboxes, persisted=False)
        return SandboxAccount(user_uuid, version=row.version, sandboxes=sandboxes, persisted=True)

    async def save(self, account: SandboxAccount) -> None:
        """版本号 CAS，再把沙箱和绑定同步成聚合里的样子。

        冲突抛 AccountConflict（调用方回滚、重读）。
        """
        now = datetime.now(UTC)
        if not account.persisted:
            self._session.add(SandboxAccountRow(user_uuid=account.user_uuid, version=1))
            try:
                await self._session.flush()
            except IntegrityError as exc:
                raise AccountConflict(f"{account.user_uuid} 的账户刚被别人建了") from exc
        else:
            result = await self._session.execute(
                update(SandboxAccountRow)
                .where(
                    SandboxAccountRow.user_uuid == account.user_uuid,
                    SandboxAccountRow.version == account.version,
                )
                .values(version=account.version + 1, updated_at=now)
            )
            if getattr(result, "rowcount", 0) != 1:
                raise AccountConflict(f"{account.user_uuid} 的账户被别人改过了")

        rows = {
            r.sandbox_uuid: r
            for r in await self._session.scalars(
                select(UserSandboxRow).where(UserSandboxRow.user_uuid == account.user_uuid)
            )
        }
        desired = {s.sandbox_uuid: s for s in account.sandboxes}
        gone = [u for u in rows if u not in desired]
        if gone:
            await self._session.execute(
                delete(UserSandboxRow).where(UserSandboxRow.sandbox_uuid.in_(gone))
            )
            await self._session.execute(
                delete(SandboxBindingRow).where(SandboxBindingRow.sandbox_uuid.in_(gone))
            )
        for sandbox_uuid, sb in desired.items():
            row = rows.get(sandbox_uuid)
            if row is None:
                self._session.add(
                    UserSandboxRow(
                        sandbox_uuid=sb.sandbox_uuid,
                        user_uuid=sb.user_uuid,
                        template_code=sb.template_code,
                        image=sb.image,
                        status=sb.status.value,
                        ready_at=sb.ready_at,
                        created_at=sb.created_at,
                    )
                )
            elif row.status != sb.status.value or row.ready_at != sb.ready_at:
                row.status, row.ready_at, row.updated_at = sb.status.value, sb.ready_at, now
        if desired:
            bound = {
                (r.sandbox_uuid, r.biz_type, r.biz_id): r
                for r in await self._session.scalars(
                    select(SandboxBindingRow).where(
                        SandboxBindingRow.sandbox_uuid.in_(list(desired))
                    )
                )
            }
            want = {
                (sb.sandbox_uuid, b.biz.biz_type.value, b.biz.biz_id): b
                for sb in desired.values()
                for b in sb.bindings
            }
            for key, r in bound.items():
                if key not in want:
                    await self._session.delete(r)
            await self._session.flush()
            for key, b in want.items():
                if key not in bound:
                    self._session.add(
                        SandboxBindingRow(
                            sandbox_uuid=key[0], biz_type=key[1], biz_id=key[2], bound_at=b.bound_at
                        )
                    )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise AccountConflict("业务已经绑在别的沙箱上了") from exc
        account.version += 1
        account.persisted = True
        forget_cached(*(b.biz for sb in account.sandboxes for b in sb.bindings))

    # ------------------------------------------------------------------ 查询

    async def by_biz(self, biz: BizRef, *, fresh: bool = False) -> UserSandbox | None:
        """这个业务绑着的沙箱。热路径：进程内缓存 cache_s 秒（fresh=True 直接读库）。"""
        key = (biz.biz_type.value, biz.biz_id)
        now = time.monotonic()
        if not fresh and (hit := _CACHE.get(key)) is not None and hit[0] > now:
            return hit[1]
        binding = await fetch_one(
            self._session,
            SandboxBindingRow,
            SandboxBindingRow.biz_type == biz.biz_type.value,
            SandboxBindingRow.biz_id == biz.biz_id,
        )
        found = None
        if binding is not None:
            sandboxes = await self._sandboxes(UserSandboxRow.sandbox_uuid == binding.sandbox_uuid)
            found = sandboxes[0] if sandboxes else None
        _CACHE[key] = (now + self._cache_s, found)
        return found

    async def describe(self, sandbox_uuid: UUID) -> UserSandbox | None:
        sandboxes = await self._sandboxes(UserSandboxRow.sandbox_uuid == sandbox_uuid)
        return sandboxes[0] if sandboxes else None

    async def owner_of(self, sandbox_uuid: UUID) -> UUID | None:
        row = await fetch_one(
            self._session, UserSandboxRow, UserSandboxRow.sandbox_uuid == sandbox_uuid
        )
        return row.user_uuid if row is not None else None

    async def bindings(self, biz_type: BizType) -> builtins.list[tuple[BizRef, UserSandbox]]:
        """这个业务类型的全部绑定和它们的沙箱。直接读库，不经缓存。"""
        rows = await self._session.execute(
            select(SandboxBindingRow, UserSandboxRow)
            .join(UserSandboxRow, UserSandboxRow.sandbox_uuid == SandboxBindingRow.sandbox_uuid)
            .where(SandboxBindingRow.biz_type == biz_type.value)
            .order_by(SandboxBindingRow.id)
        )
        out = []
        for binding, sb in rows.all():
            biz = BizRef(BizType(binding.biz_type), binding.biz_id, sb.user_uuid)
            out.append((biz, _sandbox(sb, [SandboxBinding(biz, binding.bound_at)])))
        return out

    async def retiring(self) -> builtins.list[UserSandbox]:
        return await self._sandboxes(UserSandboxRow.status == SandboxStatus.RETIRING.value)

    async def sandbox_uuids(self) -> set[UUID]:
        return set(await self._session.scalars(select(UserSandboxRow.sandbox_uuid)))

    async def users(self) -> builtins.list[UUID]:
        return builtins.list(await self._session.scalars(select(SandboxAccountRow.user_uuid)))

    # ------------------------------------------------------------------ 内部

    async def _sandboxes(self, *where: ColumnElement[bool]) -> builtins.list[UserSandbox]:
        rows = builtins.list(
            await self._session.scalars(
                select(UserSandboxRow).where(*where).order_by(UserSandboxRow.id)
            )
        )
        if not rows:
            return []
        bindings: dict[UUID, builtins.list[SandboxBinding]] = {r.sandbox_uuid: [] for r in rows}
        owners = {r.sandbox_uuid: r.user_uuid for r in rows}
        for b in await self._session.scalars(
            select(SandboxBindingRow)
            .where(SandboxBindingRow.sandbox_uuid.in_(builtins.list(bindings)))
            .order_by(SandboxBindingRow.id)
        ):
            biz = BizRef(BizType(b.biz_type), b.biz_id, owners[b.sandbox_uuid])
            bindings[b.sandbox_uuid].append(SandboxBinding(biz, b.bound_at))
        return [_sandbox(r, bindings[r.sandbox_uuid]) for r in rows]


def _sandbox(row: UserSandboxRow, bindings: builtins.list[SandboxBinding]) -> UserSandbox:
    return UserSandbox(
        sandbox_uuid=row.sandbox_uuid,
        user_uuid=row.user_uuid,
        template_code=row.template_code,
        image=row.image,
        status=SandboxStatus(row.status),
        bindings=bindings,
        created_at=row.created_at,
        ready_at=row.ready_at,
    )
