"""ModelSpec → LangChain 聊天模型。真实的 engine 在这里按 provider 建 Anthropic / OpenAI 等客户端。

本例只有 provider="mock"：进程内的模型，按规则发 tool_call，代替真实推理（结果确定）。

    最后一条是用户消息    提到城市且有 get_weather → 每个城市一个 get_weather
                          否则提到"工单"且有 create_ticket → 建一张工单
    上一步查完天气        有城市下雨、用户要求建工单、有 create_ticket → 每个下雨的城市建一张
    其余                  把所有工具结果拼成回复
只能调交进来的工具（按后缀找：服务__get_weather）。
"""

from __future__ import annotations

import re
from itertools import count
from typing import Any

from hops import hop
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from atlas_engine.contracts import ModelSpec

CITIES = r"北京|上海|杭州"
_ids = count(1)


def build_model(spec: ModelSpec) -> BaseChatModel:
    if spec.provider != "mock":
        raise ValueError(f"本例只有 mock 模型，不支持 {spec.provider}")
    return ScriptedModel()


def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "args": args, "id": f"call_{next(_ids)}", "type": "tool_call"}


def _decide(messages: list[BaseMessage], tools: list[str]) -> AIMessage:
    question = next(m.text for m in messages if isinstance(m, HumanMessage))
    weather = next((t for t in tools if t.endswith("__get_weather")), None)
    ticket = next((t for t in tools if t.endswith("__create_ticket")), None)

    if isinstance(messages[-1], HumanMessage):
        cities = re.findall(CITIES, question)
        if weather and cities:
            return AIMessage("", tool_calls=[_call(weather, {"city": c}) for c in cities])
        if ticket and "工单" in question:
            return AIMessage("", tool_calls=[_call(ticket, {"title": question.split("，")[0]})])
        return AIMessage(f"我帮不上忙（可用的工具：{tools}）。")

    calls = {c["id"]: c for m in messages if isinstance(m, AIMessage) for c in m.tool_calls}
    done = [(calls[m.tool_call_id], m.text) for m in messages if isinstance(m, ToolMessage)]
    last = next(m for m in reversed(messages) if isinstance(m, AIMessage))
    rainy = [c["args"]["city"] for c, text in done if c["name"] == weather and "雨" in text]
    if (
        {c["name"] for c in last.tool_calls} == {weather}
        and rainy
        and "工单" in question
        and ticket
    ):
        titles = [f"{c}有雨，提醒带伞" for c in rainy]
        return AIMessage("", tool_calls=[_call(ticket, {"title": t}) for t in titles])
    parts = [f"{c['args']['city']}{text}" if c["name"] == weather else text for c, text in done]
    return AIMessage("；".join(parts) + "。")


class ScriptedModel(BaseChatModel):
    tool_names: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **_: Any) -> Any:
        names = [t.name if hasattr(t, "name") else t["function"]["name"] for t in tools]
        return self.model_copy(update={"tool_names": names})

    def _generate(self, messages: list[BaseMessage], *_: Any, **__: Any) -> ChatResult:
        msg = _decide(messages, self.tool_names)
        if msg.tool_calls:
            shown = "、".join(f"{c['name']}({c['args']})" for c in msg.tool_calls)
            hop("模型（mock）", f"返回 tool_call：{shown}")
        else:
            hop("模型（mock）", f"返回回复：{msg.text}")
        return ChatResult(generations=[ChatGeneration(message=msg)])
