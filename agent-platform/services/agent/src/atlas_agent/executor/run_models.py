"""这一轮的模型从哪来：模型构造器与解析好的端点（原生运行时的装配和各能力领域共用）。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from atlas_agent.providers.llm.factory import build_chat_model
from atlas_agent.telemetry.model_callback import ModelSpanHandler

from ..config import Settings
from ..providers.llm.model_platform import ModelEndpoint, PlatformModel, direct_endpoint

if TYPE_CHECKING:
    from .segment_data import Segment


class ModelBuilder(Protocol):
    def __call__(
        self,
        model_spec: Any,
        *,
        base_url: str,
        api_key: str,
        headers: Mapping[str, str] | None = None,
    ) -> Any: ...


def default_model_builder(
    model_spec: Any, *, base_url: str, api_key: str, headers: Mapping[str, str] | None = None
) -> Any:
    """★ 遥测回调挂在这里，因为**全部**模型构造都过这一个函数：主对话、
    标题生成、上下文摘要。挂在这里三处一并覆盖，而且不用碰 runner ——
    它是纯计算层，往那里塞回调会让「run 一轮」不再能脱离基础设施单测。

    ★ 用构造器级 callbacks 而不是逐次调用传 config：调用点分散在三处，
      漏一处的表现是「某类模型调用在链路图上凭空消失」，而那恰恰最难发现。

    未启用遥测时回调照常挂着，但它拿到的是 no-op span —— 不写 if 分支。
    """
    return build_chat_model(
        model_spec,
        base_url=base_url,
        api_key=api_key,
        callbacks=[ModelSpanHandler()],
        headers=headers,
    )


@dataclass(frozen=True)
class RunModels:
    """这一轮的模型从哪来（with_models 解析好）：主模型、压缩模型都用这个端点。

    endpoint  经模型管理平台时 = LiteLLM + 智能体的客户端 Key；没接时直连 litellm
    models    经平台时：Key 的可用模型（上下文窗口、最大输出来自它）；
              None = 没接，上限照旧读 model_catalog 表
    """

    endpoint: ModelEndpoint
    models: Mapping[str, PlatformModel] | None = None

    def entry(self, model: str) -> PlatformModel | None:
        return self.models.get(model) if self.models is not None else None


def models_of(segment: Segment, settings: Settings) -> RunModels:
    """这一段解析好的模型端点；还没解析（没接平台）就按直连。"""
    return segment.models or RunModels(direct_endpoint(settings))
