"""运行限制的强制执行（文档 §4.4 / §13.2）。

`timeout_s` 与 `max_total_tokens` 在 runner 里直接管；这三项必须拦在
图内部，所以做成中间件：

| 限制 | 拦截点 | 为什么 |
|---|---|---|
| `max_steps` | `abefore_model` | 步 = 一次模型调用。跑飞的 agent 就是在这里空转 |
| `tool_concurrency` | `awrap_tool_call` | 模型一次可以发多个 tool_call，图会并发执行 |
| 工具熔断（§13.2） | `awrap_tool_call` | 同一工具连续失败 3 次说明方案不通，继续试只是烧钱 |

★ 这些控件在编辑器里一直是可填的，但直到 P6 才真正生效。
  「配了不生效」比「没有这个选项」更糟：用户以为设了上限，实际没有。

构造参数刻意全是标量 —— kernel 不认识 AgentSpec，装配值由 server 给。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from atlas_engine.contracts import LimitExceeded

logger = logging.getLogger(__name__)

#: §13.2：同一工具连续失败多少次后熔断
CIRCUIT_THRESHOLD = 3

CIRCUIT_OPEN_RESULT = "该工具已连续失败 {n} 次，已被停用。请改用其他方式完成任务，不要再调用它。"


class StepLimitMiddleware(AgentMiddleware):
    """限制单个 run 内的模型调用次数（`max_steps`）。

    步数是**跑飞的 agent** 唯一可靠的刹车：token 上限只能在调用返回后检查
    （§4.4），而一个陷在「调工具→失败→再调」循环里的 agent，每步 token 不多，
    但会一直转下去直到超时。
    """

    def __init__(self, max_steps: int) -> None:
        super().__init__()
        self._max = max_steps
        self._used = 0

    @property
    def steps_used(self) -> int:
        return self._used

    async def abefore_model(self, state: Any, runtime: Any = None) -> dict[str, Any] | None:
        self._used += 1
        if self._used > self._max:
            raise LimitExceeded(
                f"已达步数上限 {self._max}（每次模型调用算一步），已中断",
                max_steps=self._max,
                steps_used=self._used,
            )
        return None


class ToolGovernorMiddleware(AgentMiddleware):
    """工具并发上限 + 连续失败熔断（§13.2）。

    两件事放在一个中间件里，因为它们都要包住同一个 `awrap_tool_call`，
    拆成两层只是多一次转发。
    """

    def __init__(self, *, concurrency: int, threshold: int = CIRCUIT_THRESHOLD) -> None:
        super().__init__()
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._threshold = threshold
        #: 工具名 → 连续失败次数。成功一次即清零 ——
        #: 熔断针对的是「一直不通」，不是「偶尔抖动」。
        self._failures: dict[str, int] = {}

    def failures_of(self, name: str) -> int:
        return self._failures.get(name, 0)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        call = request.tool_call or {}
        name = str(call.get("name", ""))

        if self._failures.get(name, 0) >= self._threshold:
            # ★ 熔断也是把结果回给 agent，不是抛异常终止 run —— 与 §13.2 的
            #   「单个工具失败不终止 run」一致，agent 可以换方案继续。
            return ToolMessage(
                content=CIRCUIT_OPEN_RESULT.format(n=self._threshold),
                tool_call_id=str(call.get("id", "")),
                name=name,
                status="error",
            )

        async with self._sem:
            try:
                result = await handler(request)
            except LimitExceeded as exc:
                # ★ 工具内部抛出的 LimitExceeded 必然来自嵌套图（子智能体经
                #   task 委派，其 StepLimit 在子图的 abefore_model 里抛）。
                #   在这里落地为工具失败而不是任由其冒泡 —— 委派失败回给
                #   主 agent，run 继续（§13.2「单个工具失败不终止 run」，
                #   与 §4.4 里 max_subagent_depth 超限的语义同款）。
                #   主图自身的 StepLimit 不经过任何工具调用，仍然直接终止 run。
                result = ToolMessage(
                    content=f"委派已中止：{exc.message}",
                    tool_call_id=str(call.get("id", "")),
                    name=name,
                    status="error",
                )

        if _is_failure(result):
            self._failures[name] = self._failures.get(name, 0) + 1
            if self._failures[name] == self._threshold:
                logger.warning("工具 %s 连续失败 %d 次，已熔断", name, self._threshold)
        else:
            self._failures.pop(name, None)

        return result


def _is_failure(result: ToolMessage | Command[Any]) -> bool:
    """只有 ToolMessage 带 status='error' 才算失败。

    Command 是状态更新（如 filesystem 写文件），没有成败语义，
    一律当成功 —— 把它算失败会让正常的写操作把工具熔断掉。
    """
    return isinstance(result, ToolMessage) and getattr(result, "status", None) == "error"
