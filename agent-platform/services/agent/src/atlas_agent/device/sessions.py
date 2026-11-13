"""设备会话（c4-model/agent/device/code-device-session.html）。

本机 CLI 会话的运行位置：定设备和项目目录、每轮经设备网关登记能力、交给 CLI 运行时一个能连的地址。

    DeviceSessions      CLI 运行时的 CliHost（ensure）；另有列可选设备、列已有项目、释放、吊销联动
    PlacementPolicy     定运行位置：用户选的设备和目录；没选就是默认设备和默认目录
    DevicePlacement     会话跑在哪台电脑、哪个目录（建会话时定下，存在 thread 上）
    CapabilityHandover  把本轮能力交给设备网关登记，CLI 看到的地址换成网关上的、凭证换成会话令牌
    LaunchSpec          开会话流时交给电脑上连接器的启动参数：CLI 类型、目录、权限模式、模型接入

三种情况定运行位置，规则其实是同一条（设计 §2.3 · §2.6 · §2.7）：

    直接新建    用户选的设备（默认选中默认设备）和目录（默认「默认项目地址 / 会话 id」）
    被委派      使用者的默认设备，「默认项目地址 / 根会话 id」：同一个对话里多次委派用同一个目录
    调试预览    编辑者的默认设备，「默认项目地址 / 调试会话 id」

★ 只能跑在本人的电脑上：每次确保都核对设备主人就是会话创建者（§6.1）。
★ 电脑不在线立即失败并说清楚（「你的电脑不在线」），不说「系统忙」；断线重连时 CLI 运行时会接着等。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import update

from ..cli.host import HostAccess, HostUnavailable
from ..db.models import Device, Thread
from ..errors import AppError, NotFound
from ..repositories.device import DeviceRepository
from .files import DeviceFileError
from .gateway import DeviceGateway, DeviceStatus, GatewayUnavailable, SessionGrant, Upstream

if TYPE_CHECKING:
    from atlas_engine.contracts.spec import CliSpec
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ..cli.capabilities import CliCapabilities
    from .registry import DeviceRevoked

__all__ = [
    "CapabilityHandover",
    "DeviceChoice",
    "DeviceOffline",
    "DevicePlacement",
    "DeviceSessions",
    "InvalidPlacement",
    "LaunchSpec",
    "PlacementPolicy",
]

logger = logging.getLogger(__name__)

#: 模型在设备网关上的上游编号（CLI 的 ANTHROPIC_BASE_URL 指向 {网关}/u/model）
MODEL_UPSTREAM = "model"


class DeviceOffline(HostUnavailable):
    def __init__(self, name: str) -> None:
        super().__init__(
            f"你的电脑「{name}」不在线：在电脑上启动连接器（atlas-connect run）后再试",
            kind="device_offline",
        )


class InvalidPlacement(AppError):
    """建会话时选的设备、目录不对（接口层 400）。"""

    status_code = 400
    kind = "invalid_placement"


# ═══════════════════════════════════ 运行位置 ═══════════════════════════════════


@dataclass(frozen=True)
class DevicePlacement:
    device_uuid: UUID
    #: 电脑上的路径，可含 ~（在电脑上展开）
    project_dir: str

    @staticmethod
    def default_dir(cli: CliSpec, root_thread: UUID) -> str:
        return str(PurePosixPath(cli.default_project_dir.strip()) / str(root_thread))

    @staticmethod
    def check_dir(raw: str) -> str:
        """用户选的目录：绝对路径或 ~ 下的路径，不能含 ..。在不在允许范围内由电脑上的连接器判。"""
        path = PurePosixPath(raw.strip() or ".")
        if path == PurePosixPath("."):
            raise InvalidPlacement("没有给项目目录")
        if not (path.is_absolute() or path.parts[0] == "~") or ".." in path.parts:
            raise InvalidPlacement(
                "项目目录要写成绝对路径或 ~/ 下的路径，不能含 ..", project_dir=raw
            )
        return str(path)


class PlacementPolicy:
    def __init__(self, devices: DeviceRepository) -> None:
        self._devices = devices

    async def choose(
        self,
        owner: UUID,
        cli: CliSpec,
        *,
        root_thread: UUID,
        device: UUID | None = None,
        project_dir: str | None = None,
    ) -> DevicePlacement:
        if device is not None:
            found = await self._devices.get(device)
            if found is None or found.owner_uuid != owner or found.status != "active":
                raise InvalidPlacement("只能选你自己配对过的电脑", device_id=str(device))
        else:
            found = await self._devices.default_of(owner)
            if found is None:
                raise InvalidPlacement(
                    "你还没有配对电脑：先在「我的设备」里添加一台，并在电脑上启动连接器"
                )
        directory = (
            DevicePlacement.check_dir(project_dir)
            if project_dir is not None and project_dir.strip()
            else DevicePlacement.default_dir(cli, root_thread)
        )
        return DevicePlacement(found.device_uuid, directory)


# ═══════════════════════════════════ 交接能力 ═══════════════════════════════════


@dataclass(frozen=True)
class LaunchSpec:
    """开会话流时交给连接器的启动参数。连接器按本机策略逐项核对，实际启动什么命令由它按本机配置定。"""

    cli_type: str
    project_dir: str
    #: 权限模式（平台取值）：超过本机上限就拒绝
    mode: str | None
    #: 模型接入：上游编号 + 模型名；None = CLI 用电脑上自己的登录
    model: dict[str, Any] | None = None
    #: 目录不存在就建：默认目录第一次用时还不存在；手输的新目录也照建（都限制在允许的目录里）
    create_dir: bool = True

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "cli_type": self.cli_type,
            "project_dir": self.project_dir,
            "mode": self.mode,
            "create_dir": self.create_dir,
        }
        if self.model is not None:
            out["model"] = self.model
        return out


class CapabilityHandover:
    """本轮能力 → 设备网关登记 → CLI 看到的能力。

    ★ 真实凭据（智能体的模型 Key、MCP Key、知识库令牌）只交给设备网关，CLI 看到的地址都指向网关、
      凭证只有会话令牌。会话令牌整个 CLI 会话不变，真实凭据每轮整份覆盖：换 Key、收回授权
      不用重启 CLI。
    ★ 不注入出站凭据、不投送技能：CLI 用电脑上本人的配置和凭据（设计 §1.3）。
    """

    def __init__(self, gateway: DeviceGateway) -> None:
        self._gateway = gateway

    async def hand_over(
        self,
        thread: Thread,
        placement: DevicePlacement,
        cli: CliSpec,
        caps: CliCapabilities,
        status: DeviceStatus,
    ) -> HostAccess:
        upstreams, model = self._upstreams(caps, cli)
        launch = LaunchSpec(
            cli_type=cli.cli_type,
            project_dir=placement.project_dir,
            mode=caps.permission_mode,
            model=model,
        )
        thread_id = str(thread.thread_uuid)
        grant = await self._gateway.register(
            thread_id,
            device_uuid=str(placement.device_uuid),
            owner_uuid=str(thread.created_by_uuid),
            launch=launch.to_json(),
            upstreams=upstreams,
        )
        url, token = self._gateway.host_url(status, thread_id)
        return HostAccess(url, token, self._device_view(caps, grant))

    @staticmethod
    def _upstreams(
        caps: CliCapabilities, cli: CliSpec
    ) -> tuple[list[Upstream], dict[str, Any] | None]:
        upstreams = [
            Upstream(_mcp_upstream(server), "mcp", str(server["url"]), _headers_of(server))
            for server in caps.mcp_servers
        ]
        if cli.model_source == "local_login":
            return upstreams, None
        if caps.model is None:
            raise HostUnavailable(
                f"平台的模型给不了本机 {cli.cli_type}：请智能体的维护人把模型来源改成「本机登录」",
                kind="model_unavailable",
            )
        m = caps.model
        upstreams.append(
            Upstream(
                MODEL_UPSTREAM,
                "model",
                m.base_url,
                # 用量归属的请求头也由平台写：设备带来的身份头一律被网关去掉
                {"Authorization": f"Bearer {m.key}", **m.headers},
            )
        )
        model = {
            "upstream": MODEL_UPSTREAM,
            "model": m.model,
            "small_model": m.small_model,
            "prompt_cache": m.prompt_cache,
        }
        return upstreams, model

    @staticmethod
    def _device_view(caps: CliCapabilities, grant: SessionGrant) -> CliCapabilities:
        servers = [
            {
                **server,
                "url": grant.endpoints[_mcp_upstream(server)],
                # ★ ACP 的 headers 是数组：凭证只有会话令牌
                "headers": [{"name": "Authorization", "value": f"Bearer {grant.token}"}],
            }
            for server in caps.mcp_servers
        ]
        return replace(caps, mcp_servers=servers, model=None)


def _mcp_upstream(server: dict[str, Any]) -> str:
    """上游编号按服务名算：每轮一样（CLI 只在开会话时读一次地址）。"""
    return "mcp-" + hashlib.sha1(str(server["name"]).encode()).hexdigest()[:12]


def _headers_of(server: dict[str, Any]) -> dict[str, str]:
    return {str(h["name"]): str(h["value"]) for h in server.get("headers") or []}


# ═══════════════════════════════════ 设备会话 ═══════════════════════════════════


@dataclass(frozen=True)
class DeviceChoice:
    """新建会话时可选的一台电脑。"""

    device: Device
    status: DeviceStatus | None = None
    #: 这台电脑装了这种 CLI（连接器上线时报告的）
    has_cli: bool = False

    @property
    def online(self) -> bool:
        return self.status is not None

    @property
    def selectable(self) -> bool:
        return self.online and self.has_cli


@dataclass
class DeviceSessions:
    """进程级构造一次（装配见 main.py）。"""

    sessionmaker: async_sessionmaker[AsyncSession]
    gateway: DeviceGateway
    _handover: CapabilityHandover = field(init=False)

    def __post_init__(self) -> None:
        self._handover = CapabilityHandover(self.gateway)

    # ------------------------------------------------------------------ CliHost

    async def ensure(self, thread: Thread, cli: CliSpec, caps: CliCapabilities) -> HostAccess:
        """确保本机会话：设备在线就登记能力，返回设备网关上这个会话的地址和 CLI 看到的能力。"""
        placement, device = await self._placement_of(thread, cli)
        try:
            status = await self.gateway.presence(str(device.device_uuid))
        except GatewayUnavailable as exc:
            logger.warning("查设备 %s 在不在线时设备网关不可用：%s", device.device_uuid, exc)
            raise
        if status is None:
            raise DeviceOffline(device.name)
        if cli.cli_type not in status.clis:
            raise HostUnavailable(
                f"电脑「{device.name}」上没有装 {cli.cli_type}："
                "在电脑上运行 atlas-connect doctor 查看怎么装，装好后重启连接器",
                kind="cli_not_installed",
            )
        return await self._handover.hand_over(thread, placement, cli, caps, status)

    async def _placement_of(self, thread: Thread, cli: CliSpec) -> tuple[DevicePlacement, Device]:
        """会话定下的设备和目录；还没定（被委派建出的子会话第一轮）就按规则定下、写回会话。"""
        async with self.sessionmaker() as session:
            devices = DeviceRepository(session)
            if thread.device_uuid is not None and thread.device_project_dir:
                placement = DevicePlacement(thread.device_uuid, thread.device_project_dir)
            else:
                try:
                    placement = await PlacementPolicy(devices).choose(
                        thread.created_by_uuid,
                        cli,
                        root_thread=thread.root_thread_uuid or thread.thread_uuid,
                    )
                except InvalidPlacement as exc:
                    raise HostUnavailable(exc.message, kind="device_unavailable") from exc
                await session.execute(
                    update(Thread)
                    .where(Thread.thread_uuid == thread.thread_uuid, Thread.device_uuid.is_(None))
                    .values(
                        device_uuid=placement.device_uuid,
                        device_project_dir=placement.project_dir,
                    )
                )
                await session.commit()
                thread.device_uuid = placement.device_uuid
                thread.device_project_dir = placement.project_dir
            device = await devices.get(placement.device_uuid)
        # ★ 每次都核对：设备主人就是会话创建者，设备没被吊销
        if device is None or device.owner_uuid != thread.created_by_uuid:
            raise HostUnavailable("这个会话绑定的电脑不是你的", kind="device_forbidden")
        if device.status != "active":
            raise HostUnavailable(
                f"这个会话绑定的电脑「{device.name}」已被吊销，文件和 CLI 的上下文都在那台电脑上，"
                "接不上了：请新建一个会话",
                kind="device_revoked",
            )
        return placement, device

    # ------------------------------------------------------------------ 选设备、选项目

    async def choices(self, owner: UUID, cli_type: str) -> list[DeviceChoice]:
        """本人的设备（默认设备在前），带上在不在线、装没装这种 CLI。界面只让选在线且装了的。"""
        async with self.sessionmaker() as session:
            devices = await DeviceRepository(session).list(owner)
        statuses = await self.statuses([str(d.device_uuid) for d in devices])
        return [
            DeviceChoice(
                device=d,
                status=(status := statuses.get(str(d.device_uuid))),
                has_cli=status is not None and cli_type in status.clis,
            )
            for d in devices
        ]

    async def statuses(self, device_ids: list[str]) -> dict[str, DeviceStatus]:
        """设备网关不可用时当作都不在线（列表照常显示）。"""
        if not device_ids or not self.gateway.configured:
            return {}
        try:
            found = await asyncio.gather(*(self.gateway.presence(d) for d in device_ids))
        except GatewayUnavailable as exc:
            logger.warning("查设备在线状态时设备网关不可用：%s", exc)
            return {}
        return {d: s for d, s in zip(device_ids, found, strict=True) if s is not None}

    async def projects(self, owner: UUID, device_uuid: UUID) -> dict[str, Any]:
        """经设备网关请连接器列：每个允许目录下一层的子目录名，标出 git 仓库。"""
        async with self.sessionmaker() as session:
            device = await DeviceRepository(session).get(device_uuid)
        if device is None or device.owner_uuid != owner or device.status != "active":
            raise NotFound(f"设备 {device_uuid} 不存在", device_id=str(device_uuid))
        status = await self.gateway.presence(str(device_uuid))
        if status is None:
            raise DeviceFileError(str(DeviceOffline(device.name)), kind="device_offline")
        resp = await self.gateway.files(status, "roots")
        if resp.status != 200 or not isinstance(resp.body, dict):
            raise DeviceFileError(_message_of(resp.body), status=502, kind="device_error")
        return resp.body

    # ------------------------------------------------------------------ 释放、吊销

    async def release(self, thread_uuid: UUID) -> None:
        """会话删除：作废会话令牌（CLI 再调模型、MCP 一律被拒）。电脑上的 bridge 闲置到点后
        由连接器停掉。

        尽力而为：失败只记日志，登记会自己过期。
        """
        if not self.gateway.configured:
            return
        try:
            await self.gateway.revoke(str(thread_uuid))
        except GatewayUnavailable as exc:
            logger.warning("作废会话 %s 的设备登记失败：%s", thread_uuid, exc)

    async def on_device_revoked(self, event: DeviceRevoked) -> None:
        """请设备网关断开这台设备，作废它上面所有会话的令牌。尽力而为：令牌已在库里失效。"""
        if not self.gateway.configured:
            return
        try:
            await self.gateway.disconnect(str(event.device_uuid), "设备已在网页上吊销")
        except GatewayUnavailable as exc:
            logger.warning("吊销设备 %s 时设备网关不可用：%s", event.device_uuid, exc)


def _message_of(body: Any) -> str:
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if body.get("message"):
            return str(body["message"])
    return str(body or "电脑没有回答")
