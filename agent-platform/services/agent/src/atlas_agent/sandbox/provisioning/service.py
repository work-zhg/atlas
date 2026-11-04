"""SandboxProvisioning：通用供给的唯一入口和流程编排；SandboxKind：业务类型的扩展点。

c4-model/agent/sandbox/code-sandbox-provisioning.html。

    ensure(biz, req)        已绑定且 READY → 向基座确认它还能用就交出；CREATING → 接着建；没绑定 →
                            准入、构造规格，经分配策略挑一个或登记新的（判配额），保存后再建、附加、
                            等就绪，标为 READY 才交出
    current(biz)            只认 READY，进程内缓存 2 秒，不会触发新建
    confirm_gone(biz, ref)  向基座确认确实不在了才去掉记录；问不到就当它还在
    describe · of_user      登记的模板、镜像、状态、服务的业务
    retire(ref, reason)     不管有没有业务直接退役：先标 RETIRING 并保存，再交基座删，删完去掉记录
    release(biz, reason)    解绑；沙箱没有业务了，就以这个原因退役（失败只记日志，对账兜底）
    serving(biz_type)       这个业务类型里已绑定、且 READY 的业务和沙箱
    reap_orphans            业务不在名单上的绑定一律释放（reason=orphan）
    reconcile               重试卡在 RETIRING 的；基座里有、供给里没有登记的实例按 orphan 删掉

★ 不用分布式锁：一致性靠账户的版本号 CAS 和 (biz_type, biz_id) 唯一约束；建沙箱这一段不持锁、
  可重入 —— 几个请求同时等同一个沙箱，或进程崩在半路，都按同一个 sandbox_uuid 接着做（基座按
  uuid 幂等）。同一个人并行派出的几个 CLI Agent 不用排队建。
★ 先登记再建：sandbox_uuid 由这里生成，以 CREATING 和绑定一起落库，再交基座建。基座里的每个实例
  在这里都有登记，找不到登记的就是孤儿。
★ 不含任何业务类型特有的逻辑：准入、创建规格、附加配置、就绪判断都由 SandboxKind 给出。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any, Protocol, TypeVar
from uuid import UUID

from ...repositories.sandbox_account import AccountConflict, SandboxAccountRepository
from ..base.lifecycle.runtime import DestroyReason, SandboxRuntime
from ..base.lifecycle.server import LifecycleError
from ..base.lifecycle.spec import CreateSpec
from .model import (
    AllocationPolicy,
    BizRef,
    BizType,
    QuotaPolicy,
    SandboxAccount,
    SandboxRef,
    SandboxStatus,
    UserSandbox,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["SandboxKind", "SandboxProvisioning", "SandboxUnavailableError"]

logger = logging.getLogger(__name__)
T = TypeVar("T")

#: 保存时版本冲突后重读重试的次数
_MAX_ATTEMPTS = 8


class SandboxUnavailableError(RuntimeError):
    """沙箱建不起来、没有就绪，或刚建好就不能用了。消息原样带给模型和用户。"""


class SandboxKind(Protocol):
    """一种业务类型用沙箱时和通用流程不一样的地方。实现必须无状态：装配时登记一次，所有请求共用。"""

    biz_type: BizType

    def admit(self, req: Any) -> None:
        """准入（如镜像白名单）。不通过抛异常。"""

    def build(self, req: Any, biz: BizRef) -> CreateSpec:
        """创建规格：模板 code、镜像、网络策略、挂载……"""

    async def attach(self, ref: SandboxRef, req: Any, *, created: bool) -> None:
        """新建或复用后的附加配置（如每轮整份重写凭据）。"""

    async def ready(self, ref: SandboxRef) -> None:
        """就绪判断（如 execd /ping、bridge /readyz）。时限内没就绪抛异常。"""


class SandboxProvisioning:
    def __init__(
        self,
        runtime: SandboxRuntime,
        sessions: async_sessionmaker[AsyncSession],
        *,
        quota: QuotaPolicy,
        allocation: AllocationPolicy | None = None,
        cache_s: float = 2.0,
    ) -> None:
        self._runtime = runtime
        self._sessions = sessions
        self._quota = quota
        self._allocation = allocation or AllocationPolicy()
        self._cache_s = cache_s
        self._kinds: dict[BizType, SandboxKind] = {}

    def register(self, kind: SandboxKind) -> None:
        """装配时按 biz_type 登记一种业务类型。"""
        self._kinds[kind.biz_type] = kind

    def _kind(self, biz: BizRef) -> SandboxKind:
        try:
            return self._kinds[biz.biz_type]
        except KeyError as exc:
            raise LookupError(f"业务类型 {biz.biz_type} 没有登记") from exc

    # ------------------------------------------------------------------ 确保

    async def ensure(self, biz: BizRef, req: Any) -> SandboxRef:
        kind = self._kind(biz)
        for _ in range(_MAX_ATTEMPTS):
            current = await self._read(lambda repo: repo.by_biz(biz, fresh=True))
            if current is not None and current.status is SandboxStatus.READY:
                if await self._still_usable(current):
                    await kind.attach(current.ref, req, created=False)
                    return current.ref
                # 沙箱坏了（bridge 崩了 Pod 进终态、被驱逐……）：退役它，下一圈建个新的
                await self.retire(current.ref, reason="broken")
                continue
            if current is not None and current.status is SandboxStatus.CREATING:
                return await self._bring_up(current, kind, kind.build(req, biz), req)
            kind.admit(req)
            spec = kind.build(req, biz)
            try:
                allocated = await self._allocate(biz, spec)
            except AccountConflict:
                continue  # 别人先改了这个人的账户：重读再判
            if allocated.status is SandboxStatus.READY:
                continue  # 分配策略挑了一个已就绪的（共享）：走上面的复用分支
            return await self._bring_up(allocated, kind, spec, req)
        raise SandboxUnavailableError("沙箱分配反复冲突，请稍后重试")

    async def _allocate(self, biz: BizRef, spec: CreateSpec) -> UserSandbox:
        async with self._sessions() as session:
            repo = SandboxAccountRepository(session, cache_s=self._cache_s)
            account = await repo.load(biz.user_uuid)
            stale = account.bound(biz)
            if stale is not None and stale.status is SandboxStatus.RETIRING:
                # 绑着一个正在退役的沙箱：解绑，给它分配新的（退役那边删完自己收尾）
                account.unbind(biz)
            sandbox = account.allocate(biz, spec, policy=self._allocation, quota=self._quota)
            await repo.save(account)
            await session.commit()
            return sandbox

    async def _bring_up(
        self, sandbox: UserSandbox, kind: SandboxKind, spec: CreateSpec, req: Any
    ) -> SandboxRef:
        """建、附加、等就绪，标为 READY。可重入：同一个 sandbox_uuid 再来一次只建一个。"""
        ref = sandbox.ref
        try:
            instance = await self._runtime.launch(sandbox.sandbox_uuid, spec)
            if instance.sandbox_state.settling:
                instance = await self._runtime.await_settled(sandbox.sandbox_uuid)  # type: ignore[assignment]
            if instance is None or not instance.usable():
                raise SandboxUnavailableError(f"沙箱 {sandbox.sandbox_uuid} 没有起来")
            await kind.attach(ref, req, created=True)
            await kind.ready(ref)
        except Exception as exc:
            # 建失败、没就绪：这个沙箱不要了（下一次 ensure 换一个 uuid 重建），配额也还回去
            logger.warning("沙箱 %s 没能就绪：%s", sandbox.sandbox_uuid, exc)
            await self.retire(ref, reason="broken")
            if isinstance(exc, LifecycleError | SandboxUnavailableError):
                raise SandboxUnavailableError(str(exc)) from exc
            raise
        await self._mutate(sandbox.user_uuid, lambda acc: _mark_ready(acc, sandbox.sandbox_uuid))
        return ref

    async def _still_usable(self, sandbox: UserSandbox) -> bool:
        """每次确保都向基座确认一遍（bridge 崩了沙箱会变 Failed）。问不到就当它还能用。"""
        try:
            instance = await self._runtime.get(sandbox.sandbox_uuid)
        except LifecycleError:
            return True
        return instance is not None and instance.usable()

    # ------------------------------------------------------------------ 查询

    async def current(self, biz: BizRef) -> SandboxRef | None:
        """只认 READY；进程内缓存（cache_s）；不会触发新建。"""
        sandbox = await self._read(lambda repo: repo.by_biz(biz))
        return sandbox.ref if sandbox is not None and sandbox.status.serving() else None

    async def confirm_gone(self, biz: BizRef, ref: SandboxRef) -> bool:
        """向基座刷新实例状态，确实不在才去掉记录；问不到就当它还在（宁可报错也不悄悄换写入方）。"""
        try:
            instance = await self._runtime.get(ref.sandbox_uuid)
        except LifecycleError:
            return False
        if instance is not None and not (
            instance.sandbox_state.leaving or instance.sandbox_state.broken
        ):
            return False
        await self.retire(ref, reason="broken")
        return True

    async def describe(self, ref: SandboxRef) -> UserSandbox | None:
        return await self._read(lambda repo: repo.describe(ref.sandbox_uuid))

    async def of_user(self, user_uuid: UUID) -> list[UserSandbox]:
        account = await self._read(lambda repo: repo.load(user_uuid))
        return account.sandboxes

    async def serving(self, biz_type: BizType) -> list[tuple[BizRef, SandboxRef]]:
        rows = await self._read(lambda repo: repo.bindings(biz_type))
        return [(biz, sb.ref) for biz, sb in rows if sb.status is SandboxStatus.READY]

    # ------------------------------------------------------------------ 退役、释放

    async def retire(self, ref: SandboxRef, *, reason: DestroyReason) -> None:
        """先标 RETIRING 并保存（current 立刻不再返回它），再交基座删，删完去掉记录和绑定。"""
        owner = await self._read(lambda repo: repo.owner_of(ref.sandbox_uuid))
        if owner is None:
            await self._runtime.destroy(ref.sandbox_uuid, reason=reason)  # 登记已经没了：只管基座
            return
        await self._mutate(owner, lambda acc: _mark_retiring(acc, ref.sandbox_uuid))
        await self._runtime.destroy(ref.sandbox_uuid, reason=reason)
        await self._mutate(owner, lambda acc: acc.remove(ref.sandbox_uuid))

    async def release(self, biz: BizRef, *, reason: DestroyReason) -> None:
        """解绑这个业务；沙箱没有业务了就以这个原因退役。失败只记日志，由对账兜底。"""
        try:
            vacant = await self._mutate(biz.user_uuid, lambda acc: acc.unbind(biz))
            if vacant is not None:
                await self.retire(vacant.ref, reason=reason)
        except Exception:
            logger.warning("释放业务 %s 失败，交给对账", biz, exc_info=True)

    async def reap_orphans(self, biz_type: BizType, live_ids: Iterable[UUID]) -> int:
        """这个业务类型的绑定里，业务不在活名单上的一律释放（reason=orphan）。名单由业务领域给。"""
        live = set(live_ids)
        rows = await self._read(lambda repo: repo.bindings(biz_type))
        orphans = [biz for biz, _ in rows if biz.biz_id not in live]
        for biz in orphans:
            await self.release(biz, reason="orphan")
        return len(orphans)

    async def reconcile(self) -> None:
        """重试删除卡在 RETIRING 的沙箱；基座里有、供给里没有登记的实例按 orphan 删掉。"""
        for sandbox in await self._read(lambda repo: repo.retiring()):
            try:
                await self._runtime.destroy(sandbox.sandbox_uuid, reason="manual")
                await self._mutate(sandbox.user_uuid, _remover(sandbox.sandbox_uuid))
            except Exception:
                logger.warning("重试退役沙箱 %s 失败", sandbox.sandbox_uuid, exc_info=True)
        known = await self._read(lambda repo: repo.sandbox_uuids())
        for instance in await self._runtime.list():
            if instance.sandbox_uuid not in known:
                logger.info("沙箱 %s 在供给里没有登记，按孤儿删掉", instance.sandbox_uuid)
                try:
                    await self._runtime.destroy(instance.sandbox_uuid, reason="orphan")
                except Exception:
                    logger.warning("删孤儿沙箱 %s 失败", instance.sandbox_uuid, exc_info=True)

    # ------------------------------------------------------------------ 内部

    async def _read(self, fn: Callable[[SandboxAccountRepository], Awaitable[T]]) -> T:
        async with self._sessions() as session:
            return await fn(SandboxAccountRepository(session, cache_s=self._cache_s))

    async def _mutate(self, user_uuid: UUID, change: Callable[[SandboxAccount], T]) -> T:
        """读账户 → 改 → CAS 保存；版本冲突时重读再改。"""
        for _ in range(_MAX_ATTEMPTS):
            async with self._sessions() as session:
                repo = SandboxAccountRepository(session, cache_s=self._cache_s)
                account = await repo.load(user_uuid)
                result = change(account)
                try:
                    await repo.save(account)
                    await session.commit()
                except AccountConflict:
                    await session.rollback()
                    continue
                return result
        raise SandboxUnavailableError(f"{user_uuid} 的沙箱账户反复冲突，请稍后重试")


def _mark_ready(account: SandboxAccount, sandbox_uuid: UUID) -> None:
    sandbox = account.get(sandbox_uuid)
    if sandbox is None:
        raise SandboxUnavailableError(f"沙箱 {sandbox_uuid} 在就绪之前被退役了")
    sandbox.mark_ready()


def _remover(sandbox_uuid: UUID) -> Callable[[SandboxAccount], None]:
    return lambda account: account.remove(sandbox_uuid)


def _mark_retiring(account: SandboxAccount, sandbox_uuid: UUID) -> None:
    if account.get(sandbox_uuid) is not None:
        account.retire(sandbox_uuid)
