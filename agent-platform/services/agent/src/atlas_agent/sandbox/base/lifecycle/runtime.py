"""SandboxRuntime：沙箱实例的入口，以及它的 OpenSandbox 实现 OpenSandboxRuntime（防腐层）。

c4-model/agent/sandbox/code-sandbox-base-lifecycle.html。

    launch(uuid, spec)     先登记（生成两个令牌）→ 带 atlas.sandbox 标签建 → 回填 OpenSandbox id。
                           同一个 uuid 幂等：崩在中途的，下一次按标签找回，不再多建
    get · list             读登记，再向它的 server 刷新状态；OpenSandbox 上已经不在的去掉登记
    await_settled          轮询到离开过渡态
    endpoint               地址 + 必须带的请求头，不含令牌
    service_token          bridge 这类服务的令牌：唯一出基座的令牌
    destroy(…, reason)     先删 OpenSandbox 上的，再删登记；原因必填
    reconcile              OpenSandbox 上有、登记里没有的删掉；登记里有、OpenSandbox 上没有的
                       删登记；
                           没回填 id 的按标签找回
    access                 execd 的地址和令牌，只给基座内的沙箱内执行
    locate                 egress 的地址和鉴权头（实现网络领域的 EgressLocator）

★ 对外只用 Atlas 自己的 sandbox_uuid，由调用方（通用供给）生成：它先落下自己的记录再调 launch，
  崩在中间时用同一个 uuid 重试，这里按 uuid 判重，只建一次。
★ 每次登记、建好、状态变化、删除都追加一条生命周期事件，和实例的变化在同一个事务里提交。
  调 OpenSandbox 在事务外，崩在中途的由对账收尾并记一条 reconciled。
★ 状态以 OpenSandbox 为准：登记里存的只是最近一次观测，用前刷新，变了才记 state_changed。
"""

from __future__ import annotations

import asyncio
import builtins
import logging
import secrets
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID

from atlas_redislock import LockBusy, LockLost, RedisLock
from pydantic import SecretStr

from ....db.models import SandboxEvent, SandboxInstance
from ....repositories.sandbox import SandboxEventLog, SandboxInstanceRepository, TemplateRepository
from .server import Endpoint, LifecycleError, LifecycleServer, RemoteSandbox
from .spec import SANDBOX_LABEL, CreateSpec

if TYPE_CHECKING:
    import redis.asyncio as aioredis
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "EGRESS_PORT",
    "EXECD_PORT",
    "DestroyReason",
    "OpenSandboxRuntime",
    "ReconcileReport",
    "SandboxAccess",
    "SandboxGone",
    "SandboxRuntime",
    "TemplateUnavailable",
]

logger = logging.getLogger(__name__)

EXECD_PORT = 44772
EGRESS_PORT = 18080

#: 删除原因（上层给）：闲置回收、会话删除、孤儿回收、管理员退役、坏的
DestroyReason = Literal["idle", "release", "orphan", "manual", "broken"]


class SandboxGone(LookupError):
    """这个 sandbox_uuid 指的沙箱已经不在（没有登记、还没建好，或 OpenSandbox 上已经没了）。"""


class TemplateUnavailable(LifecycleError):
    """模板不存在、还没发布，或它绑定的 lifecycle server 没有配置。"""


@dataclass(frozen=True)
class SandboxAccess:
    """execd 的地址和令牌（基座内部）。"""

    url: str
    token: SecretStr


@dataclass
class ReconcileReport:
    removed_remote: list[str] = field(default_factory=list)
    removed_registrations: list[UUID] = field(default_factory=list)
    adopted: list[UUID] = field(default_factory=list)


class SandboxRuntime(Protocol):
    async def launch(self, sandbox_uuid: UUID, spec: CreateSpec) -> SandboxInstance: ...
    async def get(self, sandbox_uuid: UUID) -> SandboxInstance | None: ...
    async def list(self) -> list[SandboxInstance]: ...
    async def await_settled(self, sandbox_uuid: UUID) -> SandboxInstance | None: ...
    async def endpoint(self, sandbox_uuid: UUID, port: int) -> Endpoint: ...
    async def service_token(self, sandbox_uuid: UUID) -> SecretStr | None: ...
    async def destroy(self, sandbox_uuid: UUID, *, reason: DestroyReason) -> None: ...
    async def history(self, sandbox_uuid: UUID) -> builtins.list[SandboxEvent]: ...
    async def reconcile(self) -> ReconcileReport: ...
    async def purge_history(self, before: datetime) -> int: ...
    async def access(self, sandbox_uuid: UUID) -> SandboxAccess: ...


def _now() -> datetime:
    return datetime.now(UTC)


def _new_token() -> SecretStr:
    return SecretStr(secrets.token_urlsafe(24))


class OpenSandboxRuntime:
    def __init__(
        self,
        servers: Mapping[str, LifecycleServer],
        sessions: async_sessionmaker[AsyncSession],
        redis: aioredis.Redis,
        *,
        launch_wait_s: float = 200.0,
        settle_timeout_s: float = 200.0,
        settle_poll_s: float = 1.0,
        reconcile_grace_s: float = 600.0,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._servers = dict(servers)
        self._sessions = sessions
        self._redis = redis
        self._launch_wait_s = launch_wait_s
        self._settle_timeout_s = settle_timeout_s
        self._settle_poll_s = settle_poll_s
        self._reconcile_grace_s = reconcile_grace_s
        self._clock = clock

    # ------------------------------------------------------------------ 建

    async def launch(self, sandbox_uuid: UUID, spec: CreateSpec) -> SandboxInstance:
        """建一个沙箱，返回它的登记。同一个 uuid 再调一次返回已有实例，不再建。

        返回时里面的服务未必在监听：就绪由业务类型的种类判断。
        """
        async with self._launching(sandbox_uuid):
            row = await self._row(sandbox_uuid)
            if row is None:
                row = await self._register(sandbox_uuid, spec)
            elif row.external_id is not None:
                refreshed = await self.get(sandbox_uuid)
                if refreshed is None:
                    raise SandboxGone(f"沙箱 {sandbox_uuid} 已经不在，换一个 uuid 重建")
                return refreshed
            server = await self._server_of(row)
            # 崩在「建好」和「回填」之间的：按标签找回，不再多建一个
            remote = await self._adopt(server, sandbox_uuid)
            if remote is None:
                if spec.service_token_env and row.service_token_secret is None:
                    row = await self._with_service_token(row)
                try:
                    remote = await server.create(
                        replace(spec, labels=spec.labels.with_sandbox(str(sandbox_uuid))),
                        access_token=row.access_token_secret,
                        service_token=row.service_token_secret,
                    )
                except LifecycleError as exc:
                    await self._abandon(row, server, error=str(exc))
                    raise
            if not (remote.state.usable or remote.state.settling):
                await server.delete(remote.external_id)
                await self._abandon(row, server, error=remote.message or str(remote.state))
                raise LifecycleError(
                    f"沙箱 {sandbox_uuid} 没有起来：{remote.message or remote.state}"
                )
            return await self._created(row, remote)

    @asynccontextmanager
    async def _launching(self, sandbox_uuid: UUID) -> AsyncIterator[None]:
        """同一个 uuid 同时只建一个。

        OpenSandbox 不能由调用方指定 id，并发地「没查到 → 建」会建出两个。

        ★ 效率锁：锁失效时多建出来的那个带着同一个 atlas.sandbox 标签，下一次 launch 或对账删掉。
        """
        lock = RedisLock(self._redis, f"atlas:sandbox:launch:{sandbox_uuid}", ttl_s=30)
        try:
            async with lock.hold(wait_s=self._launch_wait_s):
                yield
        except LockBusy as exc:
            raise LifecycleError(f"沙箱 {sandbox_uuid} 正在由别处创建，等待超时") from exc
        except LockLost as exc:
            raise LifecycleError(f"沙箱 {sandbox_uuid} 的创建锁中途丢失，请重试") from exc

    async def _register(self, sandbox_uuid: UUID, spec: CreateSpec) -> SandboxInstance:
        async with self._sessions() as session:
            template = await TemplateRepository(session).get(spec.template)
            if template is None:
                raise TemplateUnavailable(f"沙箱模板 {spec.template} 不存在")
            if template.published_version is None:
                raise TemplateUnavailable(f"沙箱模板 {spec.template} 还没有发布")
            if template.server not in self._servers:
                raise TemplateUnavailable(
                    f"沙箱模板 {spec.template} 绑定的 lifecycle server {template.server} 没有配置"
                )
            row = SandboxInstance.register(
                sandbox_uuid,
                template_uuid=template.template_uuid,
                template_version=template.published_version,
                access_token=_new_token(),
                service_token=_new_token() if spec.service_token_env else None,
            )
            SandboxInstanceRepository(session).add(row)
            SandboxEventLog(session).append(
                row.event(
                    "registered",
                    detail={
                        "template": template.code,
                        "template_version": template.published_version,
                        "labels": spec.labels.as_dict(),
                    },
                    at=self._clock(),
                )
            )
            await session.commit()
            return row

    async def _with_service_token(self, row: SandboxInstance) -> SandboxInstance:
        async with self._sessions() as session:
            fresh = await SandboxInstanceRepository(session).get(row.sandbox_uuid)
            if fresh is None:
                raise SandboxGone(f"沙箱 {row.sandbox_uuid} 的登记已经不在")
            fresh.service_token_secret = _new_token()
            await session.commit()
            return fresh

    async def _adopt(self, server: LifecycleServer, sandbox_uuid: UUID) -> RemoteSandbox | None:
        """按 atlas.sandbox 标签找回已经建出来的。多于一个（锁失效）只留一个，其余删掉。"""
        found = [
            sb
            for sb in await server.list({SANDBOX_LABEL: str(sandbox_uuid)})
            if not sb.state.leaving
        ]
        if not found:
            return None
        found.sort(key=lambda sb: (not sb.state.usable, -(sb.created_at or _EPOCH).timestamp()))
        for extra in found[1:]:
            logger.warning(
                "沙箱 %s 在 OpenSandbox 上有多个：留 %s，删 %s",
                sandbox_uuid,
                found[0].external_id,
                extra.external_id,
            )
            await server.delete(extra.external_id)
        return found[0]

    async def _created(self, row: SandboxInstance, remote: RemoteSandbox) -> SandboxInstance:
        async with self._sessions() as session:
            fresh = await SandboxInstanceRepository(session).get(row.sandbox_uuid)
            if fresh is None:  # 建的过程中被对账或 destroy 删了登记
                raise SandboxGone(f"沙箱 {row.sandbox_uuid} 的登记在创建途中被删掉了")
            now = self._clock()
            fresh.external_id = remote.external_id
            fresh.state = remote.state.value
            fresh.observed_at = now
            SandboxEventLog(session).append(fresh.event("created", at=now))
            await session.commit()
            logger.info("建好沙箱 %s（OpenSandbox %s）", fresh.sandbox_uuid, remote.external_id)
            return fresh

    async def _abandon(self, row: SandboxInstance, server: LifecycleServer, *, error: str) -> None:
        """建失败：删掉半成品和登记，记一条 launch_failed。"""
        try:
            for sb in await server.list({SANDBOX_LABEL: str(row.sandbox_uuid)}):
                await server.delete(sb.external_id)
        except LifecycleError:
            logger.warning(
                "沙箱 %s 建失败后清理半成品失败，交给对账", row.sandbox_uuid, exc_info=True
            )
        async with self._sessions() as session:
            repo = SandboxInstanceRepository(session)
            if (fresh := await repo.get(row.sandbox_uuid)) is not None:
                await repo.remove(fresh)
            SandboxEventLog(session).append(
                row.event(
                    "launch_failed",
                    reason="launch_failed",
                    detail={"error": error[:500]},
                    at=self._clock(),
                )
            )
            await session.commit()

    # ------------------------------------------------------------------ 查

    async def get(self, sandbox_uuid: UUID) -> SandboxInstance | None:
        """读登记，再向它的 server 刷新状态。OpenSandbox 上已经不在的，去掉登记、返回 None。"""
        row = await self._row(sandbox_uuid)
        if row is None or row.external_id is None:
            return row
        server = await self._server_of(row)
        return await self._refresh(row, await server.get(row.external_id))

    async def list(self) -> list[SandboxInstance]:
        async with self._sessions() as session:
            rows = await SandboxInstanceRepository(session).list()
        out = []
        for row in rows:
            if row.external_id is None:
                out.append(row)
                continue
            try:
                server = await self._server_of(row)
                refreshed = await self._refresh(row, await server.get(row.external_id))
            except LifecycleError:
                out.append(row)  # 问不到：按上次观测的样子交出去，不当它不在了
                continue
            if refreshed is not None:
                out.append(refreshed)
        return out

    async def await_settled(self, sandbox_uuid: UUID) -> SandboxInstance | None:
        deadline = time.monotonic() + self._settle_timeout_s
        while True:
            row = await self.get(sandbox_uuid)
            if row is None or not row.sandbox_state.settling or time.monotonic() > deadline:
                return row
            await asyncio.sleep(self._settle_poll_s)

    async def _refresh(
        self, row: SandboxInstance, remote: RemoteSandbox | None
    ) -> SandboxInstance | None:
        if remote is not None and remote.state.value == row.state:
            return row
        async with self._sessions() as session:
            repo = SandboxInstanceRepository(session)
            fresh = await repo.get(row.sandbox_uuid)
            if fresh is None:
                return None
            now = self._clock()
            log = SandboxEventLog(session)
            if remote is None:
                await repo.remove(fresh)
                log.append(
                    fresh.event(
                        "reconciled",
                        reason="reconcile",
                        detail={"note": "OpenSandbox 上已经不在，去掉登记"},
                        at=now,
                    )
                )
                await session.commit()
                return None
            if (event := fresh.observe(remote.state, now)) is not None:
                log.append(event)
            await session.commit()
            return fresh

    async def endpoint(self, sandbox_uuid: UUID, port: int) -> Endpoint:
        row = await self._row(sandbox_uuid)
        if row is None or row.external_id is None:
            raise SandboxGone(f"沙箱 {sandbox_uuid} 不在或还没建好")
        server = await self._server_of(row)
        try:
            return await server.endpoint(row.external_id, port)
        except LifecycleError as exc:
            if exc.status == 404:
                raise SandboxGone(f"沙箱 {sandbox_uuid} 已经不在") from exc
            raise

    async def service_token(self, sandbox_uuid: UUID) -> SecretStr | None:
        row = await self._row(sandbox_uuid)
        return row.service_token_secret if row is not None else None

    async def access(self, sandbox_uuid: UUID) -> SandboxAccess:
        """execd 的地址和令牌（基座内部）。沙箱不在或不可用抛 SandboxGone。"""
        row = await self.get(sandbox_uuid)
        if row is None or not row.usable():
            raise SandboxGone(f"沙箱 {sandbox_uuid} 不在或不可用")
        ep = await self.endpoint(sandbox_uuid, EXECD_PORT)
        return SandboxAccess(ep.url, row.access_token_secret)

    async def locate(self, sandbox_uuid: UUID) -> Endpoint:
        """egress 的地址和鉴权头（网络领域的 EgressLocator）。"""
        return await self.endpoint(sandbox_uuid, EGRESS_PORT)

    async def history(self, sandbox_uuid: UUID) -> builtins.list[SandboxEvent]:
        async with self._sessions() as session:
            return await SandboxEventLog(session).history(sandbox_uuid)

    # ------------------------------------------------------------------ 删

    async def destroy(self, sandbox_uuid: UUID, *, reason: DestroyReason) -> None:
        """先删 OpenSandbox 上的（含按标签找到的半成品），再删登记并记一条 destroyed。幂等。"""
        row = await self._row(sandbox_uuid)
        if row is None:
            return
        server = await self._server_of(row)
        if row.external_id is not None:
            await server.delete(row.external_id)
        for sb in await server.list({SANDBOX_LABEL: str(sandbox_uuid)}):
            await server.delete(sb.external_id)
        async with self._sessions() as session:
            repo = SandboxInstanceRepository(session)
            if (fresh := await repo.get(sandbox_uuid)) is None:
                return
            await repo.remove(fresh)
            SandboxEventLog(session).append(
                fresh.event("destroyed", reason=reason, at=self._clock())
            )
            await session.commit()
        logger.info("删了沙箱 %s（%s）", sandbox_uuid, reason)

    # ------------------------------------------------------------------ 对账

    async def reconcile(self) -> ReconcileReport:
        """让登记和 OpenSandbox 收敛。

        ★ 先列 OpenSandbox、再读登记：两步之间新建的沙箱，登记早在它被建出来之前就落下了。
        ★ 还没回填 id、又找不到实例的登记，要等它老过 reconcile_grace_s 才删：可能正在建。
        ★ 存量沙箱（没有 atlas.sandbox 标签、或标签对不上登记）一律删掉（已定决策「存量沙箱
          不管」）。
        ★ 只处理本进程配置了 server 的登记；单个沙箱出错只记日志，不挡住其余的。
        """
        report = ReconcileReport()
        remote: dict[str, tuple[LifecycleServer, RemoteSandbox]] = {}
        listed: dict[str, bool] = {}  # base_url → 这一轮列出来了没有
        for server in self._servers.values():
            if server.base_url in listed:  # 两个名字指向同一个 server（测试、单实例部署）只列一次
                continue
            try:
                found = await server.list({"atlas.managed": "true"})
            except LifecycleError:
                # ★ 列不出来的 server：它名下的登记这一轮一律不动（看不到它上面有什么）
                logger.warning("对账时 lifecycle server %s 不可达，这一轮跳过它", server.name)
                listed[server.base_url] = False
                continue
            listed[server.base_url] = True
            for sb in found:
                remote.setdefault(sb.external_id, (server, sb))
        async with self._sessions() as session:
            rows = await SandboxInstanceRepository(session).list()
            templates = {t.template_uuid: t for t in await TemplateRepository(session).list()}
        registered = {r.external_id for r in rows if r.external_id}
        ours = [
            r
            for r in rows
            if (t := templates.get(r.template_uuid)) is not None
            and t.server in self._servers
            and listed.get(self._servers[t.server].base_url, False)
        ]

        # 登记了、还没回填 id 的：按标签找回；找不到又已经老了的，删登记
        for row in ours:
            if row.external_id is not None:
                continue
            match = [
                sb
                for _, sb in remote.values()
                if sb.labels.get(SANDBOX_LABEL) == str(row.sandbox_uuid) and not sb.state.leaving
            ]
            try:
                if match:
                    await self._created(row, match[0])
                    registered.add(match[0].external_id)
                    report.adopted.append(row.sandbox_uuid)
                elif (self._clock() - row.created_at).total_seconds() > self._reconcile_grace_s:
                    await self._drop_registration(row, "登记了却没有建出来")
                    report.removed_registrations.append(row.sandbox_uuid)
            except (LifecycleError, SandboxGone):
                logger.warning("对账处理沙箱 %s 失败", row.sandbox_uuid, exc_info=True)

        # OpenSandbox 上有、登记里没有的：删掉
        by_uuid = {str(r.sandbox_uuid): r for r in rows}
        for external_id, (server, sb) in remote.items():
            if external_id in registered or sb.state.leaving:
                continue
            try:
                await server.delete(external_id)
            except LifecycleError:
                logger.warning("对账删沙箱 %s 失败", external_id, exc_info=True)
                continue
            await self._log_orphan(sb, by_uuid.get(sb.labels.get(SANDBOX_LABEL, "")))
            report.removed_remote.append(external_id)

        # 登记里有、OpenSandbox 上没有的：删登记（get 刷新时会做）
        for row in ours:
            if row.external_id and row.external_id not in remote:
                try:
                    if await self.get(row.sandbox_uuid) is None:
                        report.removed_registrations.append(row.sandbox_uuid)
                except LifecycleError:
                    logger.warning("对账刷新沙箱 %s 失败", row.sandbox_uuid, exc_info=True)
        return report

    async def _drop_registration(self, row: SandboxInstance, note: str) -> None:
        async with self._sessions() as session:
            repo = SandboxInstanceRepository(session)
            if (fresh := await repo.get(row.sandbox_uuid)) is None:
                return
            await repo.remove(fresh)
            SandboxEventLog(session).append(
                fresh.event(
                    "reconciled", reason="reconcile", detail={"note": note}, at=self._clock()
                )
            )
            await session.commit()

    async def _log_orphan(self, sb: RemoteSandbox, owner: SandboxInstance | None) -> None:
        async with self._sessions() as session:
            uuid = (
                owner.sandbox_uuid
                if owner is not None
                else _uuid_or_none(sb.labels.get(SANDBOX_LABEL))
            )
            SandboxEventLog(session).append(
                SandboxEvent(
                    sandbox_uuid=uuid,
                    external_id=sb.external_id,
                    event_type="reconciled",
                    state=sb.state.value,
                    reason="reconcile",
                    detail={
                        "note": "OpenSandbox 上有、登记里没有，删掉",
                        "labels": dict(sb.labels),
                    },
                    occurred_at=self._clock(),
                )
            )
            await session.commit()
        logger.info("对账删了没有登记的沙箱 %s", sb.external_id)

    async def purge_history(self, before: datetime) -> int:
        async with self._sessions() as session:
            n = await SandboxEventLog(session).purge(before)
            await session.commit()
            return n

    # ------------------------------------------------------------------ 内部

    async def _row(self, sandbox_uuid: UUID) -> SandboxInstance | None:
        async with self._sessions() as session:
            return await SandboxInstanceRepository(session).get(sandbox_uuid)

    async def _server_of(self, row: SandboxInstance) -> LifecycleServer:
        async with self._sessions() as session:
            template = await TemplateRepository(session).by_uuid(row.template_uuid)
        if template is None:
            raise TemplateUnavailable(f"沙箱 {row.sandbox_uuid} 的模板已经不在")
        server = self._servers.get(template.server)
        if server is None:
            raise TemplateUnavailable(
                f"沙箱模板 {template.code} 绑定的 lifecycle server {template.server} 没有配置"
            )
        return server


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _uuid_or_none(raw: str | None) -> UUID | None:
    try:
        return UUID(raw) if raw else None
    except ValueError:
        return None
