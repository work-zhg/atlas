"""本机文件（c4-model/agent/device/code-device-files.html）。

网页上只读查看本机会话的项目目录：改动、按目录浏览、预览和下载。文件留在电脑上，不同步到平台：
打开时经设备网关请电脑上的连接器现读，读完原路返回。

    DeviceFiles       changes · list_dir · fetch（预览站点的每个子资源）· download
    FileAccessPolicy  请求者、会话创建者、设备主人必须是同一个人
    FileLimits        预览、下载的大小上限（运行期配置，改了下一次读就生效）

★ 看文件不依赖会话在跑：读文件由连接器完成，不经过 bridge。电脑在线就能看。
★ 路径不在这里把关：电脑上的连接器只信自己（整理路径、解析符号链接、以点开头的、本机拒绝规则）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ..db.models import Device, Thread
from ..errors import AppError, NotFound
from ..repositories.device import DeviceRepository
from .gateway import DeviceGateway, DeviceStatus, FileResponse

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "DeviceFileError",
    "DeviceFiles",
    "DeviceTarget",
    "FileAccessPolicy",
    "FileFetch",
    "FileLimits",
    "FileTooLarge",
]

_MB = 1024 * 1024


class DeviceFileError(AppError):
    """电脑不在线、拒绝了、文件不在……原因原样给用户。

    ★ 电脑不在线是 409 而不是 503：用户自己能处理（启动连接器），5xx 在前端一律显示「系统忙」。
    """

    status_code = 409
    kind = "device_files_unavailable"

    def __init__(self, message: str, *, status: int = 409, kind: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status
        if kind is not None:
            self.kind = kind


class FileTooLarge(DeviceFileError):
    def __init__(self, size: int, limit_mb: int, what: str) -> None:
        super().__init__(
            f"文件有 {size / _MB:.1f} MB，超过{what}上限 {limit_mb} MB：请到电脑上直接打开",
            status=413,
            kind="file_too_large",
        )


@dataclass(frozen=True)
class FileLimits:
    preview_max_mb: int = 20
    download_max_mb: int = 200


@dataclass
class FileFetch:
    """取文件的结果：200 / 206 带内容流，304 没有内容。"""

    status: int
    headers: dict[str, str]
    body: AsyncIterator[bytes] | None
    _response: FileResponse | None = None

    @property
    def length(self) -> int:
        return int(self.headers.get("content-length") or 0)

    async def aclose(self) -> None:
        if self._response is not None:
            await self._response.aclose()


class FileAccessPolicy:
    """每次看文件都核对：请求者就是会话创建者，设备主人也是他，设备没被吊销（设计 §6.1）。"""

    @staticmethod
    def check(requester: UUID | None, thread: Thread, device: Device | None) -> Device:
        if requester is not None and requester != thread.created_by_uuid:
            raise NotFound("会话不存在")
        if device is None or device.owner_uuid != thread.created_by_uuid:
            raise DeviceFileError("这个会话绑定的电脑不是你的", status=403, kind="device_forbidden")
        if device.status != "active":
            raise DeviceFileError(
                f"电脑「{device.name}」已被吊销，这个会话的文件看不到了，只剩对话记录",
                status=410,
                kind="device_revoked",
            )
        return device


@dataclass(frozen=True)
class DeviceTarget:
    """看哪台电脑上的哪个目录。预览令牌里存的就是它：预览站点的每个子资源不再查库。"""

    owner_uuid: UUID
    device_uuid: UUID
    project_dir: str
    name: str = ""

    def to_json(self) -> dict[str, str]:
        return {
            "owner_id": str(self.owner_uuid),
            "device": str(self.device_uuid),
            "project_dir": self.project_dir,
            "device_name": self.name,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> DeviceTarget:
        return cls(
            UUID(data["owner_id"]),
            UUID(data["device"]),
            str(data["project_dir"]),
            str(data.get("device_name") or ""),
        )


class DeviceFiles:
    """session：要按会话核对归属时给（接口层）；预览站点按令牌里的 DeviceTarget 读，不查库。"""

    def __init__(self, gateway: DeviceGateway, session: AsyncSession | None = None) -> None:
        self._gateway = gateway
        self._devices = DeviceRepository(session) if session is not None else None

    async def target(self, requester: UUID | None, thread: Thread) -> DeviceTarget:
        """会话绑定的电脑和目录，核对过归属（FileAccessPolicy）。"""
        assert self._devices is not None, "核对归属要数据库会话"
        if thread.device_uuid is None or not thread.device_project_dir:
            raise DeviceFileError(
                "这个会话还没开始跑，电脑上还没有项目目录", status=404, kind="no_device_dir"
            )
        device = FileAccessPolicy.check(
            requester, thread, await self._devices.get(thread.device_uuid)
        )
        return DeviceTarget(
            thread.created_by_uuid, device.device_uuid, thread.device_project_dir, device.name
        )

    async def changes(self, target: DeviceTarget) -> dict[str, Any]:
        """git 仓库里还没提交的改动 → {is_repo, entries: [{path, status, size, version…}]}。"""
        status = await self._status(target)
        return await self._json(status, "changes", project_dir=target.project_dir)

    async def list_dir(self, target: DeviceTarget, path: str) -> dict[str, Any]:
        """一层 → {entries: [{name, is_dir, size, modified_at, version}], truncated}。"""
        status = await self._status(target)
        return await self._json(status, "list", project_dir=target.project_dir, path=path)

    async def fetch(
        self,
        target: DeviceTarget,
        path: str,
        *,
        limit_mb: int,
        what: str = "预览",
        byte_range: str | None = None,
        if_none_match: str | None = None,
    ) -> FileFetch:
        """取文件：单段 Range、按版本的条件请求。这次要传的超过上限返回 413（不读完整个文件）。"""
        status = await self._status(target)
        resp = await self._gateway.files(
            status,
            "fetch",
            project_dir=target.project_dir,
            path=path,
            byte_range=byte_range,
            if_none_match=if_none_match,
        )
        if resp.stream is None:
            if resp.status == 304:
                return FileFetch(304, resp.headers, None)
            raise _error_of(resp, status)
        fetched = FileFetch(resp.status, resp.headers, resp.stream, resp)
        if fetched.length > limit_mb * _MB:
            await fetched.aclose()
            raise FileTooLarge(fetched.length, limit_mb, what)
        return fetched

    # ------------------------------------------------------------------ 内部

    async def _status(self, target: DeviceTarget) -> DeviceStatus:
        """设备在线，而且连着的就是这个人的那台（在线表里的主人是核对设备令牌时写下的）。"""
        status = await self._gateway.presence(str(target.device_uuid))
        if status is None:
            raise DeviceFileError(
                f"你的电脑「{target.name}」不在线，文件暂时看不到；启动连接器后自动恢复",
                kind="device_offline",
            )
        if status.owner != str(target.owner_uuid):
            raise DeviceFileError("这台电脑不是你的", status=403, kind="device_forbidden")
        return status

    async def _json(self, status: DeviceStatus, op: str, **params: Any) -> dict[str, Any]:
        resp = await self._gateway.files(status, op, **params)
        if resp.status != 200 or not isinstance(resp.body, dict):
            raise _error_of(resp, status)
        return resp.body


def _error_of(resp: FileResponse, status: DeviceStatus) -> DeviceFileError:
    body = resp.body
    message = ""
    if isinstance(body, dict):
        error = body.get("error")
        message = str(error.get("message") or "") if isinstance(error, dict) else ""
        message = message or str(body.get("message") or "")
    elif isinstance(body, str):
        message = body
    if resp.status == 503:
        return DeviceFileError(
            f"你的电脑「{status.name}」不在线，文件暂时看不到；启动连接器后自动恢复",
            kind="device_offline",
        )
    return DeviceFileError(
        message or f"电脑没有给出文件（{resp.status}）",
        status=resp.status if 400 <= resp.status < 500 else 502,
        kind="device_files_error",
    )
