"""CLI 的权限模式（c4-model/agent/bridge/code-bridge-session.html · ModeGuard）。

每轮按 server 的要求设上（已经是就不动），记住要求的模式：CLI 每次新建、恢复会话都会把模式重置，
恢复后再设一次。CLI 自己换了模式（降级、退出计划模式）会发更新，这里跟上实际生效的那个。
要求的设不上、而实际的更宽松，这一轮就不跑：宁可失败，也不以超出预期的权限执行。
"""

from __future__ import annotations

import logging
from typing import Any

from atlas_host import ModeState
from atlas_jsonrpc import ChannelClosed

from ..cli.ports import CliPort
from ..errors import AcpRequestTimeout, AgentRequestFailed

__all__ = ["ModeGuard"]

logger = logging.getLogger(__name__)

#: 权限模式的宽松程度（小 = 保守）。要求的模式设不上时，据此判断「能不能以实际的模式跑」：
#: 实际的比要求的更宽松 → 不跑（宁可失败，也不以超出预期的权限执行）。不认识的模式按中间算。
_MODE_RANK = {
    "plan": 0,
    "dontAsk": 0,
    "default": 1,
    "acceptEdits": 2,
    "auto": 3,
    "bypassPermissions": 4,
}


def _rank(mode: str) -> int:
    return _MODE_RANK.get(mode, 2)


class ModeGuard:
    def __init__(self, cli: CliPort, *, request_timeout_s: float) -> None:
        self._cli = cli
        self._request_timeout_s = request_timeout_s
        #: server 要的权限模式（最近一次 turn.start 给的）。None = server 没要求过
        self.desired: str | None = None
        #: CLI 当前实际生效的模式与可选列表。None / () = CLI 不支持模式
        self.current: str | None = None
        self.available: tuple[str, ...] = ()

    def absorb(self, session_id: str) -> None:
        """会话刚打开：以 CLI 报告的模式为准（它在新建 / 恢复时会被重置）。"""
        modes = self._cli.client.modes_of(session_id)
        self.current = modes.current if modes else None
        self.available = modes.available if modes else ()

    def observe(self, raw: dict[str, Any]) -> None:
        """CLI 自己换了模式（降级、退出 plan 等）会发 update：跟上实际生效的那个。"""
        update = raw.get("update")
        if not isinstance(update, dict):
            return
        kind = update.get("sessionUpdate")
        if kind == "current_mode_update" and isinstance(update.get("currentModeId"), str):
            self.current = update["currentModeId"]
        elif kind == "config_option_update":
            for option in update.get("configOptions") or []:
                if (
                    isinstance(option, dict)
                    and option.get("id") == "mode"
                    and isinstance(option.get("currentValue"), str)
                ):
                    self.current = option["currentValue"]

    async def apply(self, session_id: str, requested: str) -> ModeState:
        """把 CLI 的模式设成 requested（已经是就不动）。失败不抛：如实报告实际生效的模式。"""
        self.desired = requested
        if not self.available:
            return ModeState(requested=requested, effective=None, degraded=True)
        before = self.current
        if before != requested:
            if requested not in self.available:
                logger.warning("agent 不支持权限模式 %s（可选：%s）", requested, self.available)
            else:
                try:
                    await self._cli.client.set_mode(
                        session_id, requested, timeout=self._request_timeout_s
                    )
                except (AgentRequestFailed, AcpRequestTimeout, ChannelClosed) as exc:
                    logger.warning("设置权限模式 %s 失败：%s", requested, exc)
                else:
                    # CLI 自己降级时会先发 update（已由 observe 记下）；没发就是照办了
                    if self.current == before:
                        self.current = requested
        return ModeState(
            requested=requested,
            effective=self.current,
            available=list(self.available),
            degraded=self.current != requested,
        )

    async def reapply(self, session_id: str) -> ModeState | None:
        """恢复出来的会话模式被重置了：把 server 要的那个重新设上。server 没要求过就不动。"""
        if self.desired is None:
            return None
        return await self.apply(session_id, self.desired)

    @staticmethod
    def too_permissive(mode: ModeState) -> bool:
        """要求的设不上、而实际的比要求的更宽松 → 不能跑。CLI 不支持模式时无从判断，放行。"""
        if not mode.degraded or mode.requested is None or mode.effective is None:
            return False
        return _rank(mode.effective) > _rank(mode.requested)
