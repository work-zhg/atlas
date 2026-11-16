"""浏览器 · 宿主（c4-model/agent/browser/code-browser-host.html）：这个会话的浏览器在哪个沙箱里，
它怎么起来，怎么给它发命令。

    HostRegistry      browser_host：哪些沙箱是浏览器宿主、属于谁、最近一次导出登录态的时间
    BrowserAddon      通用供给的附加件，也是登录态进出浏览器的唯一一处：建好后导入登录态、拉起守护、
                      登记宿主；checkpoint 导出这个人登录过的站点并存好；退役前 checkpoint 再注销
    BrowserHost       浏览器跑在哪里的扩展点：NativeBrowserHost（工作区的沙箱）、
                      CliBrowserHost（会话的 Pod）
    BrowserHosts      入口：plan · ensure · current · session_env · refresh · forget

★ 一个人一个浏览器：沙箱照旧按人共享（同模板、同镜像），这个人的各个会话用自己的会话名
  （ATLAS_BROWSER_SESSION = 会话 uuid，各开各的窗口），cookie 是同一份。虚拟屏幕按对话分块
  （ATLAS_BROWSER_SCREEN = 根会话 uuid）：每个对话的窗口在自己那一块，画面各看各的。
★ 沙箱建好时由 Atlas 拉起守护：登录态要在打开第一个网页之前导进去，而沙箱自己拿不到登录态。
  守护或 Chromium 后来挂了，由 browser 命令就地重启（profile 还在本地盘上，登录不丢）。
★ 它不管接管事项（接管领域）、不转发画面（画面领域），也不存登录态（登录态领域，这里只在合适的
  时机把它导进导出）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID

from atlas_engine.contracts.spec import AgentSpec, spec_for_subagent
from sqlalchemy import delete, select, update

from ..db.models import BrowserHostRow
from ..sandbox.native import WorkspaceRef
from ..sandbox.provisioning.model import BizRef, BizType, SandboxRef
from .daemon import DaemonClient, ViewKey
from .login import LoginVault, Site
from .plan import BrowserConfig, BrowserPlan

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ..db.models import Thread
    from ..sandbox.base.lifecycle.runtime import DestroyReason
    from ..sandbox.native import NativeSandboxService
    from ..sandbox.provisioning.service import SandboxProvisioning

__all__ = [
    "SCREEN_ENV",
    "SESSION_ENV",
    "BrowserAddon",
    "BrowserHost",
    "BrowserHosts",
    "BrowserUnavailable",
    "CliBrowserHost",
    "HostRecord",
    "HostRegistry",
    "NativeBrowserHost",
    "screen_of",
    "session_env",
    "spec_of_thread",
]

logger = logging.getLogger(__name__)

#: 每条 browser 命令带的会话名（= 会话 uuid）：browser 把它交给 Playwright CLI，各会话各用各的窗口
SESSION_ENV = "ATLAS_BROWSER_SESSION"
#: 对话（= 根会话 uuid）：虚拟屏幕按它分块，子智能体的窗口和父会话在同一块，人看对话的画面都看得到
SCREEN_ENV = "ATLAS_BROWSER_SCREEN"

HostKind = Literal["native", "cli"]


class BrowserUnavailable(RuntimeError):
    """这个会话的浏览器现在用不了（没开浏览器、沙箱是开浏览器之前建的、会话 Pod 不在）。

    消息给人看。
    """


def _now() -> datetime:
    return datetime.now(UTC)


def screen_of(thread: Thread) -> str:
    """这个会话的窗口在屏幕的哪一块：按对话（根会话）分。"""
    return str(thread.sandbox_thread_id)


def session_env(thread: Thread) -> dict[str, str]:
    """命令的会话名（子智能体有自己的会话，也就有自己的窗口）和它所在的对话（屏幕上的那一块）。"""
    return {SESSION_ENV: str(thread.thread_uuid), SCREEN_ENV: screen_of(thread)}


# ---------------------------------------------------------------------------- 登记


@dataclass(frozen=True)
class HostRecord:
    sandbox_uuid: UUID
    user_uuid: UUID
    kind: HostKind
    started_at: datetime
    exported_at: datetime | None

    @property
    def ref(self) -> SandboxRef:
        return SandboxRef(self.sandbox_uuid)


class HostRegistry:
    """browser_host 的读写。每次调用自己开会话、自己提交。"""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def register(self, ref: SandboxRef, user: UUID, kind: HostKind) -> None:
        """登记（已登记的刷新拉起时间：附加件重入时）。"""
        async with self._sessions() as session:
            row = await _row(session, ref.sandbox_uuid)
            if row is None:
                session.add(
                    BrowserHostRow(
                        sandbox_uuid=ref.sandbox_uuid, user_uuid=user, kind=kind, started_at=_now()
                    )
                )
            else:
                row.started_at = _now()
            await session.commit()

    async def get(self, sandbox_uuid: UUID) -> HostRecord | None:
        async with self._sessions() as session:
            row = await _row(session, sandbox_uuid)
            return None if row is None else _record(row)

    async def of_user(self, user: UUID) -> list[HostRecord]:
        async with self._sessions() as session:
            stmt = select(BrowserHostRow).where(BrowserHostRow.user_uuid == user)
            return [_record(r) for r in (await session.execute(stmt)).scalars()]

    async def exported(self, sandbox_uuid: UUID, at: datetime) -> None:
        async with self._sessions() as session:
            await session.execute(
                update(BrowserHostRow)
                .where(BrowserHostRow.sandbox_uuid == sandbox_uuid)
                .values(exported_at=at)
            )
            await session.commit()

    async def prune(self, provisioning: SandboxProvisioning) -> int:
        """去掉通用供给里已经没有的沙箱的登记（沙箱没走退役就没了：被驱逐、对账删掉）。"""
        async with self._sessions() as session:
            rows = list((await session.execute(select(BrowserHostRow))).scalars())
        removed = 0
        for row in rows:
            if await provisioning.describe(SandboxRef(row.sandbox_uuid)) is None:
                await self.remove(row.sandbox_uuid)
                removed += 1
        return removed

    async def remove(self, sandbox_uuid: UUID) -> None:
        async with self._sessions() as session:
            await session.execute(
                delete(BrowserHostRow).where(BrowserHostRow.sandbox_uuid == sandbox_uuid)
            )
            await session.commit()


async def _row(session: AsyncSession, sandbox_uuid: UUID) -> BrowserHostRow | None:
    stmt = select(BrowserHostRow).where(BrowserHostRow.sandbox_uuid == sandbox_uuid)
    return (await session.execute(stmt)).scalar_one_or_none()


def _record(row: BrowserHostRow) -> HostRecord:
    kind: HostKind = "cli" if row.kind == "cli" else "native"
    return HostRecord(row.sandbox_uuid, row.user_uuid, kind, row.started_at, row.exported_at)


# ---------------------------------------------------------------------------- 附加件


class BrowserAddon:
    """通用供给的附加件（SandboxAddon）。

    只管带浏览器的请求：看请求的 browser 属性，不看镜像名。
    """

    def __init__(
        self,
        daemon: DaemonClient,
        vault: LoginVault,
        registry: HostRegistry,
        keys: ViewKey,
        *,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._daemon = daemon
        self._vault = vault
        self._registry = registry
        self._keys = keys
        self._clock = clock

    def applies(self, req: Any) -> bool:
        return getattr(req, "browser", False) is True

    async def on_created(self, ref: SandboxRef, req: Any, *, biz_type: BizType, user: UUID) -> None:
        """读这个人的登录态、拉起守护并导入，再登记宿主。

        失败按建沙箱失败处理（下次 ensure 重来）。
        """
        bundle = await self._vault.load(user)
        await self._daemon.start(ref, self._keys.secret_for(ref.sandbox_uuid), bundle)
        kind: HostKind = "cli" if biz_type is BizType.CLI_AGENT else "native"
        await self._registry.register(ref, user, kind)
        logger.info(
            "沙箱 %s 的浏览器已拉起（导入 %d 个站点）", ref.sandbox_uuid, len(bundle.sites())
        )

    async def checkpoint(self, ref: SandboxRef, *, thread: UUID | None = None) -> list[Site]:
        """导出这个人存过的站点的最新登录态并存好（只写比库里新的）。返回刷新了的站点。

        ★ 只导出存过的站点：模型自己逛过的网站不进库；这个人还没存过任何站点就什么都不做。
        """
        host = await self._registry.get(ref.sandbox_uuid)
        if host is None:
            return []
        sites = await self._vault.stored_sites(host.user_uuid)
        if not sites:
            return []
        at = self._clock()
        origins = await self._vault.stored_origins(host.user_uuid)
        bundle = await self._daemon.export(ref, sites, origins=origins)
        written = await self._vault.save(host.user_uuid, bundle, thread=thread, captured_at=at)
        await self._registry.exported(ref.sandbox_uuid, at)
        return written

    async def before_retire(self, ref: SandboxRef, reason: DestroyReason) -> None:
        """退役前再导出一次，然后注销。导出失败不拦退役，只少一次刷新。"""
        if await self._registry.get(ref.sandbox_uuid) is None:
            return
        try:
            await self.checkpoint(ref)
        except Exception:
            logger.warning(
                "沙箱 %s 退役（%s）前导出登录态失败", ref.sandbox_uuid, reason, exc_info=True
            )
        finally:
            await self._registry.remove(ref.sandbox_uuid)


# ---------------------------------------------------------------------------- 宿主


class BrowserHost(Protocol):
    """浏览器跑在哪里。每种运行时一个实现；以后用户自己电脑上的 Chrome 再加一个。"""

    kind: HostKind

    async def ensure(self, thread: Thread, plan: BrowserPlan, spec: AgentSpec) -> SandboxRef: ...

    async def current(self, thread: Thread) -> SandboxRef | None: ...


class NativeBrowserHost:
    """原生智能体：浏览器在工作区的沙箱里。被闲置回收了，ensure 按同样的规格重建（建好后附加件导入
    登录态）。"""

    kind: HostKind = "native"

    def __init__(self, native: NativeSandboxService) -> None:
        self._native = native

    async def ensure(self, thread: Thread, plan: BrowserPlan, spec: AgentSpec) -> SandboxRef:
        return await self._native.ensure(_workspace(thread), plan.sandbox(spec.sandbox))

    async def current(self, thread: Thread) -> SandboxRef | None:
        return await self._native.current(_workspace(thread))


class CliBrowserHost:
    """CLI 智能体：浏览器在这个会话的 Pod 里。Pod 由 CLI 运行时确保；接管时 run 还在跑，Pod 不会被
    回收，所以 ensure 只取现成的，不新建。"""

    kind: HostKind = "cli"

    def __init__(self, provisioning: SandboxProvisioning) -> None:
        self._prov = provisioning

    async def ensure(self, thread: Thread, plan: BrowserPlan, spec: AgentSpec) -> SandboxRef:
        ref = await self.current(thread)
        if ref is None:
            raise BrowserUnavailable("这个会话的会话 Pod 不在了：等智能体下一轮用到浏览器时再处理")
        return ref

    async def current(self, thread: Thread) -> SandboxRef | None:
        biz = BizRef(BizType.CLI_AGENT, thread.thread_uuid, thread.created_by_uuid)
        return await self._prov.current(biz)


def _workspace(thread: Thread) -> WorkspaceRef:
    return WorkspaceRef(thread.sandbox_thread_id, thread.created_by_uuid, thread.workspace_dir)


# ---------------------------------------------------------------------------- 入口


class BrowserHosts:
    """宿主的入口。构造不做 IO。"""

    def __init__(
        self,
        *,
        native: BrowserHost,
        cli: BrowserHost,
        registry: HostRegistry,
        addon: BrowserAddon,
        daemon: DaemonClient,
        config: Callable[[], Awaitable[BrowserConfig]],
    ) -> None:
        self._hosts: dict[HostKind, BrowserHost] = {"native": native, "cli": cli}
        self._registry = registry
        self._addon = addon
        self._daemon = daemon
        self._config = config

    async def plan(self, spec: AgentSpec) -> BrowserPlan | None:
        """换算浏览器配置：没开返回 None；开了却没有镜像抛 BrowserNotConfigured。"""
        return BrowserPlan.of(spec, await self._config())

    def session_env(self, thread: Thread) -> dict[str, str]:
        return session_env(thread)

    async def ensure(self, thread: Thread, spec: AgentSpec) -> SandboxRef:
        """确保这个会话的浏览器在（原生：被回收了就重建；CLI：取会话现成的 Pod）。"""
        plan = await self.plan(spec)
        if plan is None:
            raise BrowserUnavailable("这个智能体没有开浏览器")
        if plan.runtime == "device":
            # 电脑上的浏览器不在沙箱里：接管、画面按会话走设备宿主（browser/device.py）
            raise BrowserUnavailable("这个会话的浏览器在使用者自己的电脑上，不在沙箱里")
        ref = await self._hosts[plan.runtime].ensure(thread, plan, spec)
        if await self._registry.get(ref.sandbox_uuid) is None:
            # 已有的沙箱原样复用：开浏览器之前建的那个，要等闲置回收后才换成浏览器沙箱
            raise BrowserUnavailable(
                "这个会话的沙箱是开浏览器之前建的，里面没有浏览器：等它闲置回收后再试"
            )
        return ref

    async def current(self, thread: Thread) -> SandboxRef | None:
        """只取已经在跑的浏览器，不新建：先看会话自己的 Pod，再看工作区的沙箱。"""
        for kind in ("cli", "native"):
            ref = await self._hosts[kind].current(thread)  # type: ignore[index]
            if ref is not None and await self._registry.get(ref.sandbox_uuid) is not None:
                return ref
        return None

    async def refresh(self, thread: Thread) -> None:
        """导出这个人存过的站点的最新登录态（段末、轮末）。失败只记日志。"""
        try:
            ref = await self.current(thread)
            if ref is not None:
                await self._addon.checkpoint(ref, thread=thread.thread_uuid)
        except Exception:
            logger.warning("会话 %s 段末刷新登录态失败", thread.thread_uuid, exc_info=True)

    async def forget(self, user: UUID, site: Site) -> None:
        """在这个人每个正在跑的浏览器里删掉这个站点的 cookie 和本地存储。尽力而为。"""
        for host in await self._registry.of_user(user):
            try:
                await self._daemon.forget(host.ref, site)
            except Exception:
                logger.warning(
                    "在沙箱 %s 的浏览器里清站点 %s 失败", host.sandbox_uuid, site, exc_info=True
                )


async def spec_of_thread(session: AsyncSession, thread: Thread, version_uuid: UUID) -> AgentSpec:
    """这个会话这一轮的 AgentSpec（子会话从父 agent 的快照派生）。浏览器配置只看 kind、browser、
    sandbox，名字不重要。"""
    from ..repositories.agent import AgentRepository
    from ..schemas.agent import AgentSpecIn

    version = await AgentRepository(session).version(version_uuid)
    if version is None:
        raise BrowserUnavailable("智能体版本不在了")
    spec = AgentSpecIn.model_validate(version.spec).to_engine(slug="agent", name="agent")
    return spec_for_subagent(spec, thread.subagent_name) if thread.subagent_name else spec
