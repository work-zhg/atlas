"""审批模式与工具的副作用类别（doc/native-approval-mode-design.html）。

「问不问人」由**会话的审批模式**和**工具本身的副作用类别**决定，不再由作者逐个勾选必审工具：

    模式      plan    先想清楚，不动手：只读放行、有副作用的拦下，exit_plan_mode 提交计划等用户批准
              manual  每一步有副作用的动作都请示
              auto    明确安全的直接放行，拿不准的交给判断器（独立配置的快模型）；判断器出错时请示

★ 类别是工具的**属性**：内置工具由 server 的注册表声明，MCP 工具取官方注解 readOnlyHint；
  查不到的按 EXTERNAL 处理（最保守）。判定不按名字前缀或字符串匹配推断。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, NotRequired, TypedDict

from .approval import ApprovalState

__all__ = [
    "APPROVAL_MODES",
    "AnswerSource",
    "ApprovalKind",
    "ApprovalMode",
    "ApprovalOutcome",
    "QuestionAnswer",
    "ToolEffect",
]

ApprovalMode = Literal["plan", "manual", "auto"]

#: 全部模式，按严格程度从高到低（SubAgent 取父子中更严格的那个，设计 D6）
APPROVAL_MODES: tuple[ApprovalMode, ...] = ("plan", "manual", "auto")


class ToolEffect(StrEnum):
    READ = "read"  # 不改变任何状态
    PLAN = "plan"  # 只改智能体自己的计划（write_todos、exit_plan_mode）
    WRITE = "write"  # 改会话工作区里的文件
    EXECUTE = "execute"  # 在沙箱里跑任意命令
    EXTERNAL = "external"  # 可能改动平台之外的系统（非只读的 MCP 工具；查不到属性的工具）
    DELEGATE = "delegate"  # 起一个子 run（task）


#: 一题答案从哪来：用户选 / 用户没逐题作答、按推荐（不带答案的批准）/ 跳过（交给智能体定）
AnswerSource = Literal["user", "recommended", "skipped"]


class QuestionAnswer(TypedDict):
    """ask_user_question 一题的答案（doc/ask-user-question-design.html）。server 校验并补全来源。"""

    #: 选中的选项 label（单选最多一个）
    selected: list[str]
    #: 用户在「其他」里填的话
    other: str | None
    by: AnswerSource


class ApprovalOutcome(TypedDict):
    """一次审批的结果 —— 续跑时 server 从审批表组装，放进 state.decisions。

    ★ 原先是一个字符串（approved / rejected …）。计划审批还要带回「用户选的执行模式」，
      拒绝时要带回「为什么不行」，所以扩成结构体。decisions 不落库（续跑时现读审批表），
      没有存量数据要兼容。
    """

    status: ApprovalState
    #: 计划审批获批时用户选的执行模式
    mode: NotRequired[ApprovalMode]
    #: 拒绝原因 / 计划的修改意见。交给模型，比只说「不行」更容易改对
    feedback: NotRequired[str]
    #: 向用户提的问题（kind=question）的答案，与问题一一对应
    answers: NotRequired[list[QuestionAnswer]]


#: 等人决定的事项的种类：工具调用的确认 / 规划模式提交的计划 / 向用户提的问题。
#: ★ 问题也走审批这条路（登记、挂起、刷新恢复、续跑全部现成），所以「审批」在这里是宽义的
ApprovalKind = Literal["tool", "plan", "question"]
