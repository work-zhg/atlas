"""每次操作选底层（c4-model/agent/code-workspace.html · StoreRouter）。

    技能区、系统区     一律直连对象存储
    工作区区           原生沙箱 READY → 经沙箱；否则直连对象存储

★ 沙箱在跑时只经沙箱写：沙箱用 s3fs 挂着同一个前缀，有目录和属性缓存，这时绕过它直写对象存储，
  沙箱里的进程可能好一阵都看不到，同一个文件还会有两个写入方互相覆盖。
★ 经沙箱失败时先确认沙箱还在不在：确实不在了，这一次改走对象存储重试，模型无感；还在、问不到
  就不悄悄换写入方 —— 写报错；读降级到对象存储（读不会多出一个写入方，比让一次读失败好）。
★ 文件操作不会触发建沙箱：没有运行中的沙箱就走对象存储（多数会话一直是这样）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

import httpx

from ..opensandbox.execd import ExecdFiles
from .stores import FileStore, ObjectStoreFiles, SandboxFiles

if TYPE_CHECKING:
    from ..opensandbox import SandboxEndpoint, SandboxProvisioner

__all__ = ["StoreRouter", "WorkspaceRouting"]

logger = logging.getLogger(__name__)
T = TypeVar("T")


class WorkspaceRouting:
    """原生沙箱这一侧：问工作区现在有没有 READY 的沙箱、确认沙箱是否还在、记活动。"""

    def __init__(
        self,
        provisioner: SandboxProvisioner,
        workspace_thread_id: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._p = provisioner
        self._ws = workspace_thread_id
        self._transport = transport

    async def current(self) -> SandboxEndpoint | None:
        return await self._p.routes.get(self._ws)

    def store(self, ep: SandboxEndpoint) -> SandboxFiles:
        return SandboxFiles(ExecdFiles(ep.url, ep.token, transport=self._transport))

    async def gone(self, ep: SandboxEndpoint) -> bool:
        """请求失败后问一句：沙箱确实不在了就删路由（返回 True）。"""
        self._p.routes.forget_cached(self._ws)
        if await self._p.is_gone(ep):
            await self._p.forget(self._ws, ep)
            return True
        return False

    async def touch(self) -> None:
        await self._p.routes.touch(self._ws)


class StoreRouter:
    def __init__(self, direct: ObjectStoreFiles, sandbox: WorkspaceRouting | None) -> None:
        self.direct = direct
        self._sandbox = sandbox

    async def run(
        self,
        fn: Callable[[FileStore], T],
        *,
        in_sandbox: bool,
        refuse: Callable[[str], T] | None,
    ) -> T:
        """选一个底层，在线程里跑同步的 fn（boto3、execd 的文件 API 都是同步的）。

        in_sandbox：这次操作的路径属于工作区区（只有它会走沙箱）。
        refuse：写操作给出「不可写」的结果；读操作给 None —— 沙箱不响应时降级到对象存储。
        """
        routing = self._sandbox if in_sandbox else None
        ep = await routing.current() if routing is not None else None
        if routing is None or ep is None:
            return await asyncio.to_thread(fn, self.direct)
        store = routing.store(ep)
        try:
            result = await asyncio.to_thread(fn, store)
        except httpx.HTTPError as exc:
            if await routing.gone(ep):
                logger.info("沙箱 %s 已不在，工作区改走对象存储：%s", ep.sandbox_id, exc)
                return await asyncio.to_thread(fn, self.direct)
            if refuse is None:
                logger.warning("沙箱 %s 不响应，这次读降级到对象存储：%s", ep.sandbox_id, exc)
                return await asyncio.to_thread(fn, self.direct)
            return refuse("工作区暂时不可写：沙箱没有响应，请稍后重试")
        finally:
            store.close()
        await routing.touch()
        return result
