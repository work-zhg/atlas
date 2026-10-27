"""模型目录的词汇（模型管理方案设计 §4.2、§5、§6.7）：类型、协议、状态、能力、合规、平台用途。

    Capabilities   能力与参数规则：Atlas 据此决定显示哪些开关、发哪些参数（§5.2）
    DataPolicy     部署方式、区域、数据政策：授权人和使用者据此判断（§4.2）
    Capacity       上游账号的容量参考，不由网关执行
    PURPOSES       平台用途：平台功能按用途取模型，不按人判权（§6.7）

★ 能力存成 JSON、用这里的模型校验：加一项能力不用迁移库，但字段只有这一处定义。
★ 平台模型名同时是请求里的模型名、Agent 配置里的引用、用户中心数据对象的标识：
  不能改、不能删，下线后也不能复用（§4.2）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EFFORTS",
    "KINDS",
    "MODEL_NAME_PATTERN",
    "PROTOCOLS",
    "PURPOSES",
    "STATUSES",
    "Capabilities",
    "Capacity",
    "DataPolicy",
    "Effort",
    "Kind",
    "Protocol",
    "Purpose",
    "Status",
    "protocols_for",
]

#: 平台模型名：小写字母或数字开头，字母、数字、点、短横线、下划线，最长 64
MODEL_NAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"

Kind = Literal["chat", "embedding", "rerank"]
KINDS: tuple[Kind, ...] = ("chat", "embedding", "rerank")

#: 调用协议。对话模型：anthropic（Messages）/ openai（Chat Completions）；
#: 嵌入走 openai（Embeddings）；重排走 rerank（Cohere 兼容的 /v1/rerank）
Protocol = Literal["anthropic", "openai", "rerank"]
PROTOCOLS: tuple[Protocol, ...] = ("anthropic", "openai", "rerank")

#: 生命周期（§5.3）。停用（disabled）不是一个状态：它和生命周期正交，恢复后回到原来的状态
Status = Literal["onboarding", "available", "deprecated", "retired"]
STATUSES: tuple[Status, ...] = ("onboarding", "available", "deprecated", "retired")

Effort = Literal["low", "medium", "high", "xhigh", "max"]
EFFORTS: tuple[Effort, ...] = ("low", "medium", "high", "xhigh", "max")


def protocols_for(kind: Kind) -> tuple[Protocol, ...]:
    """一种类型的模型能用哪些协议调。"""
    return {"chat": ("anthropic", "openai"), "embedding": ("openai",), "rerank": ("rerank",)}[kind]


class Capabilities(BaseModel):
    """能力与参数规则（§5.2）。缺省全是「不支持」：没确认过的能力不能当作有。

    ★ 和 Atlas 原来按模型名写死的规则一一对应：
        thinking / adaptive_thinking / thinking_always_on   有没有思考、能不能用自适应开、能不能关
        effort / thinking_off_max_effort                    接不接受档位；关思考时档位上限
        temperature                                         接不接受温度等采样参数
        prompt_cache / min_cacheable_tokens                 提示词缓存
    """

    model_config = ConfigDict(extra="forbid")

    tools: bool = False
    parallel_tools: bool = False
    structured_output: bool = False
    vision: bool = False
    thinking: bool = False
    adaptive_thinking: bool = False
    thinking_always_on: bool = False
    effort: bool = False
    #: 关闭思考时档位不能超过它（例如 high）。None = 没有这条约束
    thinking_off_max_effort: Effort | None = None
    temperature: bool = False
    prompt_cache: bool = False
    min_cacheable_tokens: int | None = Field(default=None, ge=0)
    #: 流式时会不会回报用量；计量依赖它（§4.4）
    stream_usage: bool = False


class DataPolicy(BaseModel):
    """部署方式、区域、数据政策（§4.2）。只记录和展示，不由网关执行（§1.3）。"""

    model_config = ConfigDict(extra="forbid")

    deployment: Literal["public_cloud", "private"] = "public_cloud"
    region: Literal["domestic", "overseas"] | None = None
    #: 调用数据会不会被拿去训练。None = 不清楚
    trains_on_data: bool | None = None
    #: 供应商保留调用数据多少天。None = 不清楚
    retention_days: int | None = Field(default=None, ge=0)
    #: 能不能要求不留存
    zero_retention: bool | None = None
    #: 备案、开源许可、内容安全等说明
    notes: str = ""


class Capacity(BaseModel):
    """上游账号的容量参考（每分钟请求数、token 数），用于估算和告警，不由网关执行。"""

    model_config = ConfigDict(extra="forbid")

    rpm: int | None = Field(default=None, gt=0)
    tpm: int | None = Field(default=None, gt=0)


@dataclass(frozen=True)
class Purpose:
    code: str
    name: str
    kind: Kind
    description: str
    #: False = 不是调用，只是默认选项（新建 Agent 的默认模型）
    callable: bool = True


#: 平台用途（§6.7）。调用方只说「我是哪个应用、做什么用」，模型由用途配置决定
PURPOSES: dict[str, Purpose] = {
    p.code: p
    for p in (
        Purpose("title", "会话标题", "chat", "给新会话起标题"),
        Purpose("memory_backfill", "记忆补记", "chat", "会话空闲后整理记忆"),
        Purpose("kb_enrich", "知识库入库增强", "chat", "上下文补全、表格摘要"),
        Purpose("kb_query", "知识库查询理解", "chat", "改写、拆分查询"),
        Purpose("kb_embedding", "知识库嵌入", "embedding", "远程向量模型；每个库建库时选定"),
        Purpose("kb_rerank", "知识库重排", "rerank", "远程重排模型"),
        Purpose(
            "agent_default",
            "新建 Agent 的默认模型",
            "chat",
            "编辑器的默认选项；主体有权时才用它",
            callable=False,
        ),
    )
}
