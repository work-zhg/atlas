"""挂起：「这一段到此为止，等一个外部事件，事件到了续跑」。

★ 核心判断：**委派挂起与审批挂起是同一件事**（doc/detail/suspension.html §02）。

    委派   子智能体要跑一小时 —— 父 run 不该在进程里等那么久
    审批   要等人点头 —— 可能等到第二天上班

  两者都是「等外部」，差别只有一处：审批挂起时那个工具**还没执行**，续跑时
  要先把它跑掉；委派挂起时结果已经在等着了，续跑时填进历史即可。
  这条差异由 server 侧的历史重建处理（删掉 marker vs 换掉 marker 的内容），
  kernel 只需要认识「有东西在等」。

★ 为什么用一个字符串而不是异常或状态位。能力方（`task` 工具、审批中间件）
  产出的东西必须是一条合法的 ToolMessage —— Anthropic 要求每个 tool_use 都有
  配对的 tool_result，缺了就是 400。marker 因此既是「在等」的信号，也是那条
  占位的结果。

★ 为什么 marker 自带 token。续跑时要把它换成真话，而「换哪一条」只能靠它。
  把关联关系放在消息序列**自己身上**的回报是不需要另一张表去记
  `(tool_call_id → sub_run_id)` —— marker 所在的 ToolMessage 本来就带着正确
  的 tool_call_id（工具节点生成的）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "RELEASED_MARKER",
    "SUSPENSION_MARKER",
    "Suspension",
    "SuspensionReason",
    "is_released",
    "parse_suspension",
    "released_marker",
    "suspend_marker",
]

SuspensionReason = Literal["delegation", "approval"]

#: 哨兵前缀。
#:
#: ★ 必须是**返回值**，不能是异常。`task` 工具对受理方抛出的异常一律接成错误
#:   文本回给模型（委派失败是局部问题，不该炸掉整个 run）—— marker 走异常
#:   路径会被那个 except 吞掉，变成模型眼里的一句「委派失败」，而它其实正在跑。
#:
#: ★ 模型永远看不到它。图在 before_model 处就跳出了
#:   （kernel/middleware/suspension.py），marker 不进入任何一次模型调用。
SUSPENSION_MARKER = "__ATLAS_SUSPENDED__"

#: 「等到了，去执行那个工具」——续跑时替换 approval 哨兵用的占位。
#:
#: ★ 为什么不直接把哨兵那条**删掉**（那样 tool_call 悬空，正是 jump_to "tools"
#:   的判据）：kernel 的 `PatchToolCallsMiddleware` 在图入口（before_agent）会
#:   给一切悬空的 tool_call 补上一句「was cancelled」。它排在核心栈里、在用户
#:   middleware 之前，顺序无法调整。于是删出来的悬空会被它当场补掉，
#:   SuspensionMiddleware 看到的序列已经配对完整 —— 不跳 tools，流向模型，
#:   模型看到「工具被取消了」就重新发一次调用，于是再挂起一次：**死循环**。
#:   （真实环境验证时撞到的，见 doc/detail/suspension.html §12 修正记录 10。）
#:
#: ★ 所以序列在图入口必须是**配对完整**的：这条占位就是那个「结果」。
#:   制造悬空的时机推到 before_model —— SuspensionMiddleware 在那里把它删掉
#:   并同时 jump_to "tools"，patch 已经跑完，没有第二次插手的机会。
RELEASED_MARKER = "__ATLAS_RELEASED__"

_SEP = ":"


@dataclass(frozen=True)
class Suspension:
    """一次挂起：等什么，以及等的那个东西怎么找。"""

    reason: SuspensionReason
    #: delegation → 子 run 的 id；approval → approval 的 id。
    token: str


def suspend_marker(reason: SuspensionReason, token: str) -> str:
    """造一个哨兵字符串，作为占位 ToolMessage 的内容。"""
    return f"{SUSPENSION_MARKER}{_SEP}{reason}{_SEP}{token}"


def released_marker(token: str) -> str:
    """造一个「已放行」占位，替换掉 approval 哨兵。"""
    return f"{RELEASED_MARKER}{_SEP}{token}"


def is_released(text: object) -> bool:
    """这条 ToolMessage 是不是「已放行，去执行」的占位。"""
    return isinstance(text, str) and text.startswith(RELEASED_MARKER)


def parse_suspension(text: object) -> Suspension | None:
    """从 ToolMessage 的内容里认出哨兵。不是哨兵就返回 None。

    ★ 容错到底：解析失败一律当成「不是挂起」。判错方向要选安全的那一边 ——
      把真 marker 当普通结果的后果是模型看到一串奇怪的字符（可见、可查）；
      把普通结果当 marker 的后果是 run 永久停在 suspended（静默、难查）。
    """
    if not isinstance(text, str) or not text.startswith(SUSPENSION_MARKER):
        return None
    parts = text.split(_SEP)
    if len(parts) != 3:
        return None
    _prefix, reason, token = parts
    if reason not in ("delegation", "approval") or not token:
        return None
    return Suspension(reason=reason, token=token)  # type: ignore[arg-type]
