"""最小可运行示例：用 state_schema 给智能体的 state 加自定义字段。

运行：  .venv/bin/python demo/state-schema/state_schema.py      （不需要 API key，模型是假的）
说明：  demo/state-schema/README.html（原理、时序图、标注详解、踩坑实验）

场景：用户问北京和上海的天气，模型并行调用两次 get_weather。

演示的东西：
  1. 中间件用 `state_schema = WeatherState` 声明自定义字段，create_agent 把它合并进图的 state
  2. 三种标注控制字段能不能从 invoke 传入、会不会出现在 invoke 的返回值里
       OmitFromInput / OmitFromOutput / PrivateStateAttr
  3. reducer（operator.add）让并行的两个工具都能往同一个字段里追加
  4. 工具通过 ToolRuntime 读 state / context，通过 Command(update=...) 写 state
  5. context（只读、调用方给的）与 state（可读写、在图里流动）的区别
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, NotRequired, TypedDict

from langchain.agents import create_agent
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    OmitFromInput,
    OmitFromOutput,
    PrivateStateAttr,
)
from langchain.tools import ToolRuntime, tool
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.runtime import Runtime
from langgraph.types import Command


# ---------------------------------------------------------------------------
# context：这次调用「是谁、在什么环境」。调用方给，图里只读，不会出现在返回值里
# ---------------------------------------------------------------------------
class Ctx(TypedDict):
    user_id: str


# ---------------------------------------------------------------------------
# state：在图里流动、可以被节点改写的数据。继承 AgentState（自带 messages）
# ---------------------------------------------------------------------------
class WeatherState(AgentState):
    # ① 普通字段：invoke 能传入，图里能读写，invoke 会返回
    unit: NotRequired[str]

    # ② 只进不出：invoke 能传入，但不出现在返回值里
    debug: NotRequired[Annotated[bool, OmitFromOutput]]

    # ③ 只出不进 + reducer：调用方不能传入，由工具写；返回值里有
    #    operator.add 表示「新值追加到旧值后面」，并行的两个工具写入会被合并
    #    ★ reducer 必须是 Annotated 的**最后一个**元数据，写在 OmitFromInput 前面会被忽略
    visited: NotRequired[Annotated[list[str], OmitFromInput, operator.add]]

    # ④ 不进不出：只在图内部用的计数器
    model_calls: NotRequired[Annotated[int, PrivateStateAttr]]


# ---------------------------------------------------------------------------
# 工具：从 ToolRuntime 读 state / context，返回 Command 同时写消息和 state
# ---------------------------------------------------------------------------
TEMPERATURE = {"北京": 18, "上海": 22}


@tool
def get_weather(city: str, runtime: ToolRuntime[Ctx]) -> Command:
    """查询城市当前气温。"""
    # runtime 参数由框架注入，不会出现在给模型看的参数列表里（模型只看到 city）
    unit = runtime.state.get("unit", "C")
    celsius = TEMPERATURE[city]
    text = f"{city} {celsius}°C" if unit == "C" else f"{city} {celsius * 9 / 5 + 32:.0f}°F"
    print(f"    [tool] user={runtime.context['user_id']} 查 {city}（unit={unit}）")
    return Command(
        update={
            # 工具返回 Command 时，tool_result 要自己放进 messages
            "messages": [ToolMessage(text, tool_call_id=runtime.tool_call_id)],
            "visited": [city],  # 经过 reducer：追加，而不是覆盖
        }
    )


# ---------------------------------------------------------------------------
# 假模型：最后一条是用户消息就并行查两个城市，否则汇总工具结果
# ---------------------------------------------------------------------------
class ScriptedModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedModel:
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        if isinstance(messages[-1], HumanMessage):
            calls = [
                {"name": "get_weather", "args": {"city": city}, "id": f"call_{i}"}
                for i, city in enumerate(TEMPERATURE)
            ]
            reply = AIMessage("", tool_calls=calls)
        else:
            results = [m.content for m in messages if isinstance(m, ToolMessage)]
            reply = AIMessage("、".join(results))
        return ChatResult(generations=[ChatGeneration(message=reply)])


# ---------------------------------------------------------------------------
# 中间件：声明 state_schema，在钩子里读写自定义字段
# ---------------------------------------------------------------------------
class WeatherMiddleware(AgentMiddleware):
    state_schema = WeatherState  # 就这一行，让 create_agent 认识上面的字段

    def before_model(self, state: WeatherState, runtime: Runtime[Ctx]) -> dict[str, Any] | None:
        n = state.get("model_calls", 0) + 1
        if state.get("debug"):
            print(f"    [before_model] 第 {n} 次调用模型，state 里的键 = {sorted(state)}")
        return {"model_calls": n}  # 返回 dict 就是「更新这些字段」

    def after_agent(self, state: WeatherState, runtime: Runtime[Ctx]) -> dict[str, Any] | None:
        print(
            f"    [after_agent] user={runtime.context['user_id']} "
            f"model_calls={state.get('model_calls')} visited={state.get('visited')}"
        )
        return None


def main() -> None:
    agent = create_agent(
        model=ScriptedModel(),
        tools=[get_weather],
        middleware=[WeatherMiddleware()],
        context_schema=Ctx,
    )

    print("== create_agent 合并出的入参 / 返回值字段 ==")
    print(f"    invoke 能传入：{sorted(agent.get_input_jsonschema()['properties'])}")
    print(f"    invoke 会返回：{sorted(agent.get_output_jsonschema()['properties'])}")

    print("\n== invoke（故意多传了 visited 和 model_calls） ==")
    result = agent.invoke(
        {
            "messages": [HumanMessage("北京和上海现在多少度？")],
            "unit": "C",
            "debug": True,
            "visited": ["伪造的城市"],  # OmitFromInput → 被丢掉
            "model_calls": 99,  # PrivateStateAttr → 被丢掉
        },
        context={"user_id": "u-42"},
    )

    print("\n== invoke 的返回值 ==")
    for key, value in result.items():
        shown = [f"{m.type}: {m.content!r}" for m in value] if key == "messages" else value
        print(f"    {key}: {shown}")


if __name__ == "__main__":
    main()
