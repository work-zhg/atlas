"""假模型和假工具：主智能体、SubAgent 都按剧本出牌，不需要 API key。

剧本：用户要选一个消息队列。

    主智能体  第 1 波：researcher ×3 调研 Kafka / RabbitMQ / Pulsar（Pulsar 那个会失败）
              第 2 波：researcher 重新调研 Pulsar，外加派给一个不存在的 writer
              然后回复用户
    researcher「调研 Kafka」  自己再派 benchmarker 去压测（SubAgent 派 SubAgent），拿到数字再交结论
    其余的 SubAgent          调一次自己的工具，把结果当结论
"""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from tracing import say

USER_SAYS = "帮我选一个消息队列：Kafka、RabbitMQ、Pulsar"

# 任务书 → 计划：每一波是同一批发出的 task 调用 (SubAgent 名, 任务书)；最后一项是最终答复
# 不在这里的任务书，由 SubAgent 调一次自己的工具完成
PLANS: dict[str, list[list[tuple[str, str]] | str]] = {
    USER_SAYS: [
        [
            ("researcher", "调研 Kafka"),
            ("researcher", "调研 RabbitMQ"),
            ("researcher", "调研 Pulsar"),
        ],
        [("researcher", "重新调研 Pulsar"), ("writer", "写选型报告")],
        "选 Kafka：吞吐最高（单机 100 万条/秒）、生态最成熟；Pulsar 多租户好，但运维复杂。",
    ],
    "调研 Kafka": [
        [("benchmarker", "压测 Kafka")],
        "Kafka：吞吐最高（压测单机 100 万条/秒），生态最成熟",
    ],
}

# SubAgent 的工具：查询 → (要跑多久, 结果)。结果是异常表示工具失败，这个 run 随之失败
TOOL_SCRIPT: dict[str, tuple[float, str | Exception]] = {
    "调研 RabbitMQ": (0.2, "RabbitMQ：路由灵活，吞吐中等"),
    "调研 Pulsar": (0.4, TimeoutError("搜索服务超时")),
    "压测 Kafka": (0.3, "Kafka 单机 100 万条/秒"),
    "重新调研 Pulsar": (0.2, "Pulsar：多租户好，运维复杂"),
}


def check_pairing(messages: list[BaseMessage]) -> None:
    """模拟真实 API 的配对规则：交给模型的每个 tool_call 都要有且只有一条结果，否则 400。"""
    results = Counter(m.tool_call_id for m in messages if isinstance(m, ToolMessage))
    bad = [
        c["id"]
        for m in messages
        if isinstance(m, AIMessage)
        for c in m.tool_calls
        if results[c["id"]] != 1
    ]
    assert not bad, f"tool_call {bad} 的结果不是恰好一条（真实 API 会报 400）"


def first_line(text: object) -> str:
    return str(text).split("\n", 1)[0]


class ScriptedModel(BaseChatModel):
    """一个模型演所有角色：任务书在 PLANS 里就分波派 SubAgent，否则调一次自己的工具。"""

    tool_name: str | None = None  # 自己的工具；主智能体没有

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedModel:
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        check_pairing(messages)
        start = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
        turn = messages[start:]
        plan = PLANS.get(str(turn[0].content))
        if plan is None:
            reply = self._do_it_myself(turn)
        else:
            reply = self._follow_plan(plan, turn, n_calls=_count_calls(messages))
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def _do_it_myself(self, turn: list[BaseMessage]) -> AIMessage:
        last = turn[-1]
        if not isinstance(last, HumanMessage):
            return AIMessage(str(last.content))  # 工具结果就是结论
        assert self.tool_name, "主智能体没有自己的工具，它的每个任务都要有计划"
        call = {"name": self.tool_name, "args": {"query": last.content}, "id": "s1"}
        return AIMessage("", tool_calls=[call])

    def _follow_plan(self, plan: list, turn: list[BaseMessage], n_calls: int) -> AIMessage:
        sent = [m for m in turn if isinstance(m, AIMessage) and m.tool_calls]
        if sent:  # 读上一波的结果
            names = {c["id"]: c["args"]["subagent_type"] for c in sent[-1].tool_calls}
            for m in turn:
                if isinstance(m, ToolMessage) and m.tool_call_id in names:
                    say(f"模型读到 {names[m.tool_call_id]} 的结论：{first_line(m.content)}")
        *waves, answer = plan
        if len(sent) == len(waves):
            say(f"模型给出最终答复：{answer}")
            return AIMessage(str(answer))
        # tool_call id 在整个会话里唯一：接着已有的调用往下编号
        calls = [
            {
                "name": "task",
                "args": {"subagent_type": name, "description": task},
                "id": f"c{n_calls + i}",
            }
            for i, (name, task) in enumerate(waves[len(sent)], 1)
        ]
        briefs = "、".join(
            f"{c['id']} {c['args']['subagent_type']}「{c['args']['description']}」" for c in calls
        )
        say(f"模型同一批发出 {len(calls)} 个 task：{briefs}")
        return AIMessage("", tool_calls=calls)


def _count_calls(messages: list[BaseMessage]) -> int:
    return sum(len(m.tool_calls) for m in messages if isinstance(m, AIMessage))


async def _lookup(query: str) -> str:
    duration, outcome = TOOL_SCRIPT[query]
    await asyncio.sleep(duration)
    if isinstance(outcome, Exception):
        raise outcome
    return outcome


@tool
async def web_search(query: str) -> str:
    """搜索资料。"""
    return await _lookup(query)


@tool
async def run_benchmark(query: str) -> str:
    """跑基准测试。"""
    return await _lookup(query)
