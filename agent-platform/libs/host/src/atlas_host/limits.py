"""每一轮的时限策略（Bridge 设计 §6.1 · §7.2）。

数值由 server 按 agent 配置给出：``session.open`` 的 defaults 为整个会话的默认，
``turn.start`` 的 limits 可以逐项覆盖。
bridge 只在 server 什么都没给时使用 ``DEFAULT_LIMITS``（B5）。

★ 截止、审批等待、断线重连窗口三项**默认不限**：
  · CLI 只要在干活就不中断 —— 一轮没有时间上限；
  · 审批只由人决定 —— 不会到点替用户按拒绝处理；
  · 断线只是传话的通道断了 —— 这一轮在 Pod 里照常进行，等 server 重连后 attach 补发。
  唯一的「卡死」判据是静默检测（idle_s / tool_idle_s），所以只有这两项必须有值。
"""

from __future__ import annotations

from pydantic import Field

from ._model import Model

__all__ = ["DEFAULT_LIMITS", "TurnLimits"]

_Positive = Field(default=None, gt=0)


class TurnLimits(Model):
    """全部字段可选：None 表示「沿用上一层的值」；合并到最后仍是 None 表示「不限」。"""

    idle_s: float | None = _Positive  # 静默上限（在等模型）
    tool_idle_s: float | None = _Positive  # 静默上限（在执行工具）
    deadline_s: float | None = _Positive  # 一轮的截止
    permission_wait_s: float | None = _Positive  # 单次权限询问的等待上限
    reconnect_window_s: float | None = _Positive  # 断线后的重连窗口

    def over(self, base: TurnLimits) -> TurnLimits:
        """以本对象覆盖 ``base``：本对象没给的字段取 ``base`` 的值。"""
        merged = base.model_dump()
        merged.update(self.model_dump(exclude_none=True))
        return TurnLimits(**merged)

    def is_complete(self) -> bool:
        """一轮开始前必须有值的只有静默检测的两个阈值；其余三项 None = 不限。"""
        return self.idle_s is not None and self.tool_idle_s is not None


DEFAULT_LIMITS = TurnLimits(idle_s=120, tool_idle_s=600)
