"""这一轮的模型从哪来：模型构造器与解析好的端点（原生运行时的装配和各能力领域共用）。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from atlas_engine.runtime.models import build_chat_model

from atlas_agent.telemetry.model_callback import ModelSpanObserver

from ..providers.llm.model_platform import ModelEndpoint, PlatformModel

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

    ★ 观察者挂在构造器上而不是逐次调用传：调用点分散在好几处，漏一处的表现是「某类模型调用
      在链路图上凭空消失」，而那恰恰最难发现。

    未启用遥测时观察者照常挂着，但它拿到的是 no-op span —— 不写 if 分支。
    """
    from ..config import get_settings

    return build_chat_model(
        model_spec,
        base_url=base_url,
        api_key=api_key,
        request_timeout_s=get_settings().model_timeout_s,
        observers=[ModelSpanObserver()],
        headers=headers,
    )


@dataclass(frozen=True)
class RunModels:
    """这一轮的模型从哪来（with_models 解析好）：主模型、压缩模型都用这个端点。

    endpoint  模型网关 + 智能体的客户端 Key
    models    Key 的可用模型（上下文窗口、最大输出来自它；没登记的上限读 model_catalog 表）
    """

    endpoint: ModelEndpoint
    models: Mapping[str, PlatformModel] = field(default_factory=dict)

    def entry(self, model: str) -> PlatformModel | None:
        return self.models.get(model)

    def sees_images(self, model: str) -> bool:
        """模型平台登记时标注了能看图（Key 的模型条目带着，不用再查平台）。"""
        entry = self.entry(model)
        return entry is not None and entry.supports_vision


def models_of(segment: Segment) -> RunModels:
    """这一段解析好的模型端点（with_models 填的）。"""
    if segment.models is None:
        raise RuntimeError("这一段的模型还没解析：先过 CapabilityAssembly.with_models")
    return segment.models
