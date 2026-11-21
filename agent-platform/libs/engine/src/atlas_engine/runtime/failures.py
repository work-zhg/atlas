"""模型调用中途失败 → 给用户看的说明（模型管理方案设计 7.5）。纯函数，不碰 IO。

★ 放在单独的模块：runner 属于「纯计算层」（不许依赖 DB / Redis），它只需要这一个函数。

模型网关的拒绝按错误体里的 error.category 分（网关统一的错误类别，
agent-platform/doc/design/model-gateway-design.html 第 9 节；不按报错文字猜）：
    key_invalid       Key 被停用、在本轮进行中被重置或删除 → 本轮停下，重发时取新的 Key
    model_forbidden   模型被移出了 Key、停用 → 本轮停下，找管理员
    key_limit         Key 的预算或每分钟次数到了上限 → 本轮停下，找管理员（换模型没用）
    no_route          这个模型的线路都用不了（供应商余额不足、凭据失效、没配线路）→ 提示换模型
其余（模型繁忙、上游出错、超时）交给 engine 的 classify 按状态码分类。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from atlas_engine.contracts import (
    MODEL_QUOTA,
    EngineError,
    ModelRateLimited,
    ModelUnavailable,
)

__all__ = ["ModelCallRefused", "model_call_failure"]


class ModelCallRefused(EngineError):
    """模型网关拒绝了这一轮里的一次调用：模型被移出了 Key、Key 到了限额。重发也没用，要找人。"""

    kind = "model_not_allowed"


def _category(exc: BaseException) -> str | None:
    """→ 网关错误体里的 error.category。

    Anthropic SDK 的 body 是整个错误体（流式中途出错时是 error 事件的数据），
    OpenAI SDK 已经取出了 error 这一层。
    """
    body: Any = getattr(exc, "body", None)
    error = body.get("error", body) if isinstance(body, Mapping) else None
    if not isinstance(error, Mapping):
        return None
    category = error.get("category")
    return str(category) if category else None


def model_call_failure(exc: BaseException) -> EngineError | None:
    """模型调用的异常 → 给用户看的失败说明。认不出来返回 None，交给 engine 的 classify。

    ★ 走到这里时，5xx 已经由 SDK 退避重试过了：暂时性的原因只需告诉用户稍后重发。
    """
    status = getattr(exc, "status_code", None)
    category = _category(exc)
    if category == "key_invalid":
        # Run 开始时取的是最新的 Key：重发就会换用新的；被停用的话重发会在开始前被拦下
        return ModelUnavailable(
            "模型 Key 在本轮进行中被重置或停用，本轮已停止：重发即可换用新的 Key；"
            "如果 Key 已被停用，请联系模型管理员",
            status_code=status,
            reason=category,
        )
    if category == "model_forbidden":
        return ModelCallRefused(
            "本轮已停止：这个模型已经不在智能体的模型 Key 里，请联系模型管理员重新分配",
            status_code=status,
            reason=category,
        )
    if category == "key_limit":
        return ModelCallRefused(
            "本轮已停止：智能体的模型 Key 到了限额（预算或每分钟次数），请联系模型管理员",
            status_code=status,
            reason=category,
        )
    if category == "no_route":
        # 供应商余额不足、凭据失效：对用户就是这个模型暂时用不了，换一个模型能接着用；
        # 不暴露供应商的细节
        return ModelRateLimited(MODEL_QUOTA, status_code=status, reason=category)
    return None
