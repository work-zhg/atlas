"""运行限制的强制执行（文档 §4.4 / §13.2）。

这几项必须拦在图内部，所以做成中间件：

| 限制 | 拦截点 | 为什么 |
|---|---|---|
| `max_steps` | `abefore_model` | 步 = 一次模型调用。跑飞的 agent 就是在这里空转 |
| `max_total_tokens` | `abefore_model` | 下一次调用之前就停：调用返回后才查的话，已经做完的一轮会被改判失败 |
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
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from atlas_engine.contracts import LimitExceeded
from atlas_engine.kernel.context._text import emit
from atlas_engine.kernel.context.reminders import Reminders

logger = logging.getLogger(__name__)

#: §13.2：同一工具连续失败多少次后熔断
CIRCUIT_THRESHOLD = 3

CIRCUIT_OPEN_RESULT = "该工具已连续失败 {n} 次，已被停用。请改用其他方式完成任务，不要再调用它。"


#: 步数或 token 用到这个比例，就在每次调用前提醒模型收尾
WARN_RATIO = 0.8

_STEPS_WARNING = (
    "已用 {used}/{limit} 步（每次调用模型算一步），快到上限了，到了会直接中断。请尽快收尾：先把已经"
    "完成的结果整理好告诉用户；没做完的说明卡在哪、需要用户决定什么。不要再做没把握的重复尝试。"
)
_TOKENS_WARNING = (
    "这一轮已用 {used}/{limit} tokens（不含缓存命中），快到上限了，到了会直接中断。请尽快收尾：先把"
    "已经完成的结果整理好告诉用户；没做完的说明卡在哪、需要用户决定什么。不要再做没把握的重复尝试。"
)


def billable_tokens(usage: Any) -> int:
    """一次调用计入 token 上限的量：没命中缓存的输入 + 输出。

    ★ 缓存命中的不算：它便宜，长任务每次调用都把整段上下文重读一遍 —— 算进来的话 1M 的上限
      十几次调用就到了（会话 01a1f830：一共 30M，其中 97% 是缓存命中）。
    """
    if not isinstance(usage, dict):
        return 0
    details = usage.get("input_token_details") or {}
    cached = details.get("cache_read") if isinstance(details, dict) else 0
    total = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
    return max(0, total - int(cached or 0))


class RunBudgetMiddleware(AgentMiddleware):
    """一个 run 的两道刹车：模型调用次数（`max_steps`）、token（`max_total_tokens`）。

    都在**下一次调用之前**拦：一次调用的用量要等它返回才知道，返回后再判的话，模型已经写完的
    回答会被改判失败（会话 01a1f830：13 首歌都下完了，最后一段显示「超过上限」）。
    用到 WARN_RATIO 时，每次调用前追加一条提醒让模型收尾 —— 撞线前它知道还剩多少。

    ★ 步数按段计（每段新装一个）；token 跨段累计：prior_tokens 是之前几段已经计入的量。
    ★ 每一步经 custom 通道报一次 usage.delta（steps=1），runner 记进 usage，落进 run.step_count。
    """

    def __init__(
        self, *, max_steps: int, max_tokens: int | None = None, prior_tokens: int = 0
    ) -> None:
        super().__init__()
        self._max_steps = max_steps
        self._max_tokens = max_tokens
        self._used = 0
        self._tokens = prior_tokens

    @property
    def steps_used(self) -> int:
        return self._used

    @property
    def tokens_used(self) -> int:
        return self._tokens

    async def abefore_model(self, state: Any, runtime: Any = None) -> dict[str, Any] | None:
        self._used += 1
        if self._used > self._max_steps:
            raise LimitExceeded(
                f"已达步数上限 {self._max_steps}（每次模型调用算一步），已中断",
                max_steps=self._max_steps,
                steps_used=self._used,
            )
        if self._max_tokens is not None and self._tokens > self._max_tokens:
            raise LimitExceeded(
                f"已用 {self._tokens} tokens（不含缓存命中），超过上限 {self._max_tokens}，已中断",
                total_tokens=self._tokens,
                max_total_tokens=self._max_tokens,
            )
        emit({"kind": "usage.delta", "steps": 1})
        return None

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        response = await handler(Reminders.attach(request, self._warnings()))
        message = _last_ai(response)
        if message is not None:
            self._tokens += billable_tokens(message.usage_metadata)
        return response

    def _warnings(self) -> list[tuple[str, str]]:
        notes = []
        if self._used >= self._max_steps * WARN_RATIO:
            notes.append(
                ("step_budget", _STEPS_WARNING.format(used=self._used, limit=self._max_steps))
            )
        if self._max_tokens is not None and self._tokens >= self._max_tokens * WARN_RATIO:
            notes.append(
                ("token_budget", _TOKENS_WARNING.format(used=self._tokens, limit=self._max_tokens))
            )
        return notes


def _last_ai(response: Any) -> AIMessage | None:
    if isinstance(response, AIMessage):
        return response
    for message in reversed(list(getattr(response, "result", None) or [])):
        if isinstance(message, AIMessage):
            return message
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
                #   主 agent，run 继续（§13.2「单个工具失败不终止 run」）。
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
