"""工作空间：一个人的全部文件（{user}/files/）。

★ 文件本身和会话无关（providers/filesystem/user_files.py）。会话只在两处出现：
    - 会话的工作目录建会话时定下、之后不变（沙箱里的 cwd）。所以有会话在用的目录（它本身或它下面
      有会话的工作目录）不能改名、移动 —— 要换目录就在新目录里新开会话。
    - 删除时，正在运行的会话的工作目录不能删；没在运行的可以删，会话保留，下次运行从空目录开始。
★ 上传的两个上限都读运行期配置（workspace.upload_max_mb、workspace.quota_mb），改了不用重启。
  配额按 files/ 下的全部对象算，智能体写的也在内；只在上传时检查（智能体在沙箱里写拦不住）。
★ 只有本人：路径里有 user 段，接口只按当前主体取前缀，管理员也看不到别人的。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..errors import AppError, Conflict, DependencyUnavailable, NotFound
from ..providers.filesystem import PathEscape, UserFiles, WorkspaceRouting, make_user_files
from ..redisx import make_redis
from ..repositories.thread import ThreadRepository
from ..runtime_config import WorkspaceLimits
from ..sandbox.component import SandboxComponent
from ..sandbox.native import WorkspaceRef
from ..schemas.workspace import (
    WorkspaceDirOut,
    WorkspaceOut,
    WorkspaceRefsOut,
    WorkspaceRefThread,
    WorkspaceSearchOut,
)

__all__ = ["UploadTooLarge", "WorkspaceInUse", "WorkspaceService"]


class UploadTooLarge(AppError):
    """单个文件超过上限，或者超出存储配额。"""

    status_code = 413
    kind = "upload_too_large"


class WorkspaceInUse(Conflict):
    """目录是会话的工作目录（或含有），这次操作不允许。"""

    kind = "workspace_in_use"


class WorkspaceService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._threads = ThreadRepository(session)

    # ------------------------------------------------------------------ 读

    async def overview(self, owner: UUID, limits: WorkspaceLimits) -> WorkspaceOut:
        files = make_user_files(self._settings, owner)
        if files is None:
            return WorkspaceOut(configured=False)
        used, count = await asyncio.to_thread(files.usage)
        return WorkspaceOut(
            configured=True,
            used_bytes=used,
            files=count,
            quota_bytes=limits.quota,
            upload_max_bytes=limits.upload_max,
        )

    async def list_dir(self, owner: UUID, path: str, *, hidden: bool) -> WorkspaceDirOut:
        files = self._files(owner)
        out = await _call(files.list_dir, path, hidden=hidden)
        return WorkspaceDirOut.model_validate(out)

    async def search(self, owner: UUID, q: str, *, hidden: bool) -> WorkspaceSearchOut:
        files = self._files(owner)
        return WorkspaceSearchOut.model_validate(await _call(files.search, q, hidden=hidden))

    async def refs(self, owner: UUID, path: str) -> WorkspaceRefsOut:
        files = self._files(owner)
        rel = await _call(files.rel, path)
        rows = await self._threads.using_workspace(owner, rel)
        return WorkspaceRefsOut(
            threads=[
                WorkspaceRefThread(
                    id=t.thread_uuid, title=t.title, workspace_dir=t.workspace_dir, running=running
                )
                for t, running in rows
            ]
        )

    async def open(self, owner: UUID, path: str) -> tuple[Any, int]:
        files = self._files(owner)
        try:
            return await _call(files.open, path)
        except AppError:
            raise
        except Exception as exc:  # NoSuchKey 等
            raise NotFound(f"工作空间里没有文件 {path}", path=path) from exc

    async def zip(self, owner: UUID, path: str) -> Iterator[bytes]:
        files = self._files(owner)
        rel = await _call(files.rel, path)
        if not await asyncio.to_thread(files.is_dir, rel):
            raise NotFound(f"工作空间里没有文件夹 {path}", path=path)
        return files.zip(rel)

    # ------------------------------------------------------------------ 写

    async def check_upload(self, owner: UUID, size: int, limits: WorkspaceLimits) -> None:
        """上传前检查：单个文件上限、存储配额（覆盖同名文件时多算了一份，宁可严一点）。"""
        if limits.upload_max and size > limits.upload_max:
            raise UploadTooLarge(f"文件超过单个 {_mb(limits.upload_max)} 的上限", size=size)
        if not limits.quota:
            return
        files = make_user_files(self._settings, owner)
        if files is None:
            return
        used, _ = await asyncio.to_thread(files.usage)
        if used + size > limits.quota:
            raise UploadTooLarge(
                f"工作空间存储配额 {_mb(limits.quota)} 已用 {_mb(used)}，放不下这个文件",
                used=used,
                quota=limits.quota,
            )

    async def put(self, owner: UUID, path: str, data: bytes, limits: WorkspaceLimits) -> int:
        await self.check_upload(owner, len(data), limits)
        async with self._routed(owner) as files:
            return await _acall(files.put, path, data)

    async def mkdir(self, owner: UUID, path: str) -> str:
        files = self._files(owner)
        return await _call(files.mkdir, path)

    async def move(self, owner: UUID, src: str, dst: str) -> str:
        files = self._files(owner)
        rel = await _call(files.rel, src)
        if not rel:
            raise AppError("不能移动工作空间的根目录")
        users = await self._threads.using_workspace(owner, rel)
        if users and await asyncio.to_thread(files.is_dir, rel):
            names = "、".join(f"「{t.title or '未命名对话'}」" for t, _ in users[:3])
            more = f" 等 {len(users)} 个" if len(users) > 3 else ""
            raise WorkspaceInUse(
                f"{names}{more}对话以这里为工作目录，工作目录定了不能换，所以不能改名或移动",
                threads=[str(t.thread_uuid) for t, _ in users],
            )
        return await _call(files.move, src, dst)

    async def delete(self, owner: UUID, path: str) -> None:
        files = self._files(owner)
        rel = await _call(files.rel, path)
        if not rel:
            raise AppError("不能删除工作空间的根目录")
        busy = [t for t, running in await self._threads.using_workspace(owner, rel) if running]
        if busy and await asyncio.to_thread(files.is_dir, rel):
            names = "、".join(f"「{t.title or '未命名对话'}」" for t in busy[:3])
            raise WorkspaceInUse(
                f"{names}正在运行，它的工作目录在要删除的范围里，等它结束再删",
                threads=[str(t.thread_uuid) for t in busy],
            )
        async with self._routed(owner) as routed:
            await _acall(routed.delete, path)

    # ------------------------------------------------------------------ 内部

    def _files(self, owner: UUID) -> UserFiles:
        files = make_user_files(self._settings, owner)
        if files is None:
            raise NotFound("没有配置对象存储，这个部署没有文件能力")
        return files

    def _routed(self, owner: UUID) -> _Routed:
        return _Routed(self._settings, owner)


class _Routed:
    """写操作用的 UserFiles：这个人的沙箱在跑时经沙箱写（只有一个写入方）。"""

    def __init__(self, settings: Settings, owner: UUID) -> None:
        self._settings = settings
        self._owner = owner
        self._redis: Any = None

    async def __aenter__(self) -> UserFiles:
        routing = None
        if self._settings.sandbox_enabled:
            self._redis = make_redis(self._settings)
            sandbox = SandboxComponent.build(self._settings, self._redis)
            # ★ 文件区不属于哪个会话：绑定键用人的 id 占位，选路落到这个人任一个 READY 的原生沙箱
            routing = WorkspaceRouting(
                sandbox.native, sandbox.exec, WorkspaceRef(self._owner, self._owner)
            )
        files = make_user_files(self._settings, self._owner, routing=routing)
        if files is None:
            raise NotFound("没有配置对象存储，这个部署没有文件能力")
        return files

    async def __aexit__(self, *exc: object) -> None:
        if self._redis is not None:
            await self._redis.aclose()


async def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """在线程里跑同步的文件操作，把文件层的异常换成接口错误。"""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except Exception as exc:
        raise _translate(exc, args) from exc


async def _acall(fn: Any, *args: Any) -> Any:
    try:
        return await fn(*args)
    except Exception as exc:
        raise _translate(exc, args) from exc


def _translate(exc: Exception, args: tuple[Any, ...]) -> Exception:
    path = str(args[0]) if args else ""
    if isinstance(exc, AppError):
        return exc
    if isinstance(exc, PathEscape):
        return AppError(str(exc), path=path)
    if isinstance(exc, FileExistsError):
        return Conflict(str(exc), path=path)
    if isinstance(exc, FileNotFoundError):
        return NotFound(f"工作空间里没有 {path}", path=path)
    if isinstance(exc, OSError):  # 沙箱在跑却不响应：不悄悄改写对象存储
        return DependencyUnavailable(str(exc), path=path)
    return exc


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.0f} MB" if n < 1024**3 else f"{n / 1024**3:.1f} GB"
