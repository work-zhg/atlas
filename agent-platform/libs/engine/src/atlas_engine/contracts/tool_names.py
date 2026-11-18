"""模型侧几个内置工具的名字：spec 校验、目录、中间件、事件推导都按它认。

定义在契约层，是因为 spec（contracts）和中间件（kernel）都要用它，而 kernel 只许依赖 contracts。
改名只改这里。
"""

from __future__ import annotations

__all__ = ["ASK_USER_QUESTION", "INSPECT_SCREENSHOT", "REQUEST_TAKEOVER", "SUBAGENT_TOOL"]

#: 向用户提问。沿用 Claude Code AskUserQuestion 的约定，取蛇形名（同 exit_plan_mode 的取名规则）
ASK_USER_QUESTION = "ask_user_question"

#: 派子智能体。runner 的事件推导（subagent.started/finished 的判别）与 tool_registry 的目录都引用它
SUBAGENT_TOOL = "task"

#: 请人接管浏览器（c4-model/agent/browser/code-browser-takeover.html）。开了浏览器的智能体才有；
#: 原生由引擎的 TakeoverMiddleware 提供，CLI 由 bridge 本机 MCP 的平台工具提供，名字相同
REQUEST_TAKEOVER = "request_takeover"

#: 看浏览器截图回答问题（kernel/middleware/screen.py）。开了浏览器、主模型能看图的原生智能体才有；
#: 审批按只读放行（permission 的 kernel 工具表）
INSPECT_SCREENSHOT = "inspect_screenshot"
