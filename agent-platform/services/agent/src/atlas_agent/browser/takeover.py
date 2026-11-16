"""浏览器 · 接管（c4-model/agent/browser/code-browser-takeover.html）：等人在浏览器里操作完的事项。

    Takeover        一次接管：审批表里 kind = takeover 的那一行，加上它所在的 run 和会话
    TakeoverPolicy  谁能处理：只有沙箱的主人（会话的创建人），事项必须还在等
    LoginCapture    交还时把这次登录存下来：关窗口、比较接管前后的 cookie，变了的站点存进登录态
    TakeoverDesk    入口：open · hand_back · decline（HTTP 接口）

★ 浏览器在哪由会话定（BrowserHost）：沙箱（原生、CLI 智能体）或使用者自己的电脑（本机 CLI 智能体，
  browser/device.py）。电脑上的浏览器经设备网关请连接器开关窗口；登录态留在电脑上，交还时不存。

★ 接管是审批的第四种：登记、等人、做决定、唤醒、run 结束时作废都借审批；这里只多了「打开」和
  「交还时先存登录态」。原生智能体由引擎的 TakeoverMiddleware 经审批门禁登记，不经过这里。
★ 交还时先存登录态，再做决定、唤醒：续跑可能落在一个新沙箱上（原来的被回收了），先存好，新沙箱拉起
  浏览器时才导得进去。存失败也照样交还，结果里告诉模型「登录态没存下来」。
★ 作废时不在这里关窗口：守护开新接管时会顺手关掉没人管的旧窗口（code-browser-daemon.html）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from atlas_engine.contracts import TakeoverLogin
from atlas_engine.runtime import TakeoverAsk

from ..db.models import Approval, Run, Thread
from ..db.session import fetch_one
from ..device.gateway import GatewayUnavailable
from ..errors import Conflict, Forbidden, NotFound
from ..sandbox.base.lifecycle.runtime import SandboxGone
from .daemon import DaemonClient, DaemonError
from .device import DeviceBrowserHost, on_device
from .host import BrowserHosts, BrowserUnavailable, screen_of, spec_of_thread
from .login import LoginVault

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ..approval import ApprovalService

__all__ = ["LoginCapture", "Takeover", "TakeoverDesk", "TakeoverPolicy", "TakeoverUnavailable"]

logger = logging.getLogger(__name__)


class TakeoverUnavailable(Conflict):
    """这次接管现在打不开（浏览器起不来、沙箱是开浏览器之前建的……）。消息给人看。"""

    kind = "takeover_unavailable"


@dataclass(frozen=True)
class Takeover:
    """一次接管。审批的状态就是它的状态：

    pending 等人，approved 交还，rejected 不处理，expired 作废。
    """

    approval: Approval
    run: Run
    thread: Thread

    @property
    def id(self) -> UUID:
        return self.approval.approval_uuid

    @property
    def ask(self) -> TakeoverAsk:
        return TakeoverAsk.parse(self.approval.args)

    def pending(self) -> bool:
        return self.approval.status == "pending"

    def owned_by(self, user: UUID) -> bool:
        return self.thread.created_by_uuid == user


class TakeoverPolicy:
    """只有沙箱的主人能打开、交还：浏览器里是这个人的登录态，键盘输入的是他的密码。业务应用里一起
    参与会话的同事不能替他操作。"""

    def may_handle(self, takeover: Takeover, user: UUID) -> None:
        if not takeover.owned_by(user):
            raise Forbidden("只有会话的创建人能处理接管：浏览器里是他的登录")
        if not takeover.pending():
            raise Conflict("这次接管已经处理过或已失效", approval_id=str(takeover.id))


class LoginCapture:
    """交还时把这次登录存下来。"""

    def __init__(self, daemon: DaemonClient, vault: LoginVault) -> None:
        self._daemon = daemon
        self._vault = vault

    async def capture(self, takeover: Takeover, hosts: BrowserHosts) -> TakeoverLogin:
        """关窗口、找出这次登录的站点、按站点存好。存不下来返回 saved=False（照样交还）。"""
        thread = takeover.thread
        try:
            ref = await hosts.current(thread)
            if ref is None:
                raise BrowserUnavailable("浏览器已经不在了")
            captured = await self._daemon.takeover_close(ref, str(takeover.id))
            sites = captured.sites()
            if sites:
                await self._vault.save(
                    thread.created_by_uuid,
                    captured.state.only(sites),
                    sites=sites,
                    thread=thread.thread_uuid,
                )
        except Exception:
            logger.warning("接管 %s 交还时存登录态失败", takeover.id, exc_info=True)
            return {"saved": False, "sites": []}
        return {"saved": True, "sites": sorted(s.name for s in sites)}


class TakeoverDesk:
    """接管的入口（一个请求一个）。决定只能由人经接口做出：模型替不了。"""

    def __init__(
        self,
        session: AsyncSession,
        approvals: ApprovalService,
        hosts: BrowserHosts | None,
        daemon: DaemonClient | None,
        vault: LoginVault | None,
        *,
        device: DeviceBrowserHost | None = None,
        policy: TakeoverPolicy | None = None,
    ) -> None:
        """hosts、daemon、vault：沙箱里的浏览器（部署没配浏览器能力时为 None）；
        device：使用者电脑上的浏览器（没部署设备网关时为 None）。"""
        self._session = session
        self._approvals = approvals
        self._hosts = hosts
        self._daemon = daemon
        self._capture = LoginCapture(daemon, vault) if daemon and vault else None
        self._device = device
        self._policy = policy or TakeoverPolicy()

    async def get(self, approval_id: UUID) -> Takeover:
        approval = await self._approvals.get(approval_id)
        if approval is None or approval.kind != "takeover":
            raise NotFound("接管不存在", approval_id=str(approval_id))
        run = await fetch_one(self._session, Run, Run.run_uuid == approval.run_uuid)
        thread = (
            await fetch_one(self._session, Thread, Thread.thread_uuid == run.thread_uuid)
            if run is not None
            else None
        )
        if run is None or thread is None:
            raise NotFound("接管所在的会话不在了", approval_id=str(approval_id))
        return Takeover(approval, run, thread)

    async def open(self, user: UUID, approval_id: UUID) -> Takeover:
        """确保浏览器在（被闲置回收了就重建、导入登录态），开接管窗口打开模型给的地址。

        可以重复点：同一个接管号只开一个窗口（守护只把它置前）。
        """
        takeover = await self.get(approval_id)
        self._policy.may_handle(takeover, user)
        if on_device(takeover.thread):
            await self._open_on_device(takeover)
            return takeover
        if self._hosts is None or self._daemon is None:
            raise TakeoverUnavailable("这个部署没有开浏览器能力", approval_id=str(approval_id))
        spec = await spec_of_thread(self._session, takeover.thread, takeover.run.agent_version_uuid)
        try:
            ref = await self._hosts.ensure(takeover.thread, spec)
            await self._daemon.takeover_open(
                ref, str(takeover.id), takeover.ask.url, screen=screen_of(takeover.thread)
            )
        except (BrowserUnavailable, DaemonError, SandboxGone) as exc:
            raise TakeoverUnavailable(f"打不开接管：{exc}", approval_id=str(approval_id)) from exc
        except Exception as exc:  # 沙箱建不起来、配额满……
            logger.warning("打开接管 %s 失败", approval_id, exc_info=True)
            raise TakeoverUnavailable(
                f"打不开接管：浏览器起不来（{exc}）", approval_id=str(approval_id)
            ) from exc
        return takeover

    async def _open_on_device(self, takeover: Takeover) -> None:
        """电脑上：核对在线、装了组件、本机允许查看画面，请连接器在对话那一块开窗口。"""
        if self._device is None:
            raise TakeoverUnavailable(
                "这个部署没有设备网关，打不开电脑上的浏览器", approval_id=str(takeover.id)
            )
        try:
            await self._device.open_takeover(takeover.thread, str(takeover.id), takeover.ask.url)
        except BrowserUnavailable as exc:
            raise TakeoverUnavailable(str(exc), approval_id=str(takeover.id)) from exc
        except GatewayUnavailable as exc:
            logger.warning("打开接管 %s 时设备网关不可用：%s", takeover.id, exc)
            raise TakeoverUnavailable(
                "打不开接管：连不上设备网关，稍后再试", approval_id=str(takeover.id)
            ) from exc

    async def hand_back(self, user: UUID, approval_id: UUID, note: str | None) -> dict[str, Any]:
        """交还：先关窗口、存这次登录的站点，再做决定（approved）、唤醒 run。

        电脑上的浏览器只关窗口：登录就留在它的 profile 里，不比较、不存（login 为 None）。
        """
        takeover = await self.get(approval_id)
        self._policy.may_handle(takeover, user)
        if on_device(takeover.thread):
            await self._close_on_device(takeover)
            await self._approvals.decide_takeover(approval_id, "approved", by=user, note=note)
            return {"decision": "approved", "login": None}
        if self._capture is None or self._hosts is None:
            raise TakeoverUnavailable("这个部署没有开浏览器能力", approval_id=str(approval_id))
        login = await self._capture.capture(takeover, self._hosts)
        await self._approvals.decide_takeover(
            approval_id, "approved", by=user, note=note, login=dict(login)
        )
        return {"decision": "approved", "login": login}

    async def decline(self, user: UUID, approval_id: UUID, note: str | None) -> dict[str, Any]:
        """不处理：做决定（rejected），再尽力关掉窗口（不存登录态）。"""
        takeover = await self.get(approval_id)
        self._policy.may_handle(takeover, user)
        await self._approvals.decide_takeover(approval_id, "rejected", by=user, note=note)
        if on_device(takeover.thread):
            await self._close_on_device(takeover)
            return {"decision": "rejected"}
        try:
            if self._hosts is not None and self._daemon is not None:
                ref = await self._hosts.current(takeover.thread)
                if ref is not None:
                    await self._daemon.takeover_close(ref, str(takeover.id))
        except Exception:
            logger.info("接管 %s 不处理后关窗口失败（守护下次开接管时清掉）", approval_id)
        return {"decision": "rejected"}

    async def _close_on_device(self, takeover: Takeover) -> None:
        """尽力关掉电脑上的接管窗口：电脑不在线、没开过窗口都不挡住交还。"""
        if self._device is None:
            return
        try:
            await self._device.close_takeover(takeover.thread, str(takeover.id))
        except Exception:
            logger.info("接管 %s 关电脑上的窗口失败（电脑不在线？）", takeover.id, exc_info=True)
