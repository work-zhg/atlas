"""平台审批模式和 CLI 权限模式的对照，只写这一处
（c4-model/agent/cli-runtime/code-cli-runtime-turn.html · ModeMapping）。

每轮开始时（turn.start）按平台模式要求 CLI 用哪种；bridge 回报实际生效的模式时翻成 agent.mode
事件，不认识的原样带出。新增一种模式或 CLI 改了模式名只改这里。
"""

from __future__ import annotations

from typing import Any

from ..domain.spec import ACP_MODE_ID, PLATFORM_MODE, CliSpec, acp_permission_mode

__all__ = ["ModeMapping"]


class ModeMapping:
    @staticmethod
    def platform_mode(cli: CliSpec, approval_mode: str | None) -> str:
        """这一轮按什么权限模式要求 CLI（平台取值）。作为子智能体时与父 run 的审批模式取更严格的
        （native 审批模式设计 D6）。"""
        return acp_permission_mode(cli.permission_mode, approval_mode)

    @classmethod
    def to_cli(cls, cli: CliSpec, approval_mode: str | None) -> str:
        """→ CLI 的 modeId。

        ★ 每一轮都带：CLI 每次新建 / 恢复会话都会把模式重置，bridge 据此按需重设。
        """
        return ACP_MODE_ID[cls.platform_mode(cli, approval_mode)]

    @staticmethod
    def from_cli(mode: dict[str, Any]) -> dict[str, Any]:
        """bridge 回报的模式（CLI 的 modeId）→ agent.mode 事件的 data（平台取值，不认识的原样）。"""

        def platform(mode_id: Any) -> str | None:
            return PLATFORM_MODE.get(mode_id, mode_id) if isinstance(mode_id, str) else None

        return {
            "requested": platform(mode.get("requested")),
            "effective": platform(mode.get("effective")),
            "degraded": bool(mode.get("degraded")),
        }
