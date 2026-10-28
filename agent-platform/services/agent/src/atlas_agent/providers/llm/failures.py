"""模型调用中途失败 → 给用户看的说明（模型管理方案设计 7.5）。纯函数，不碰 IO。

★ 放在单独的模块：runner 属于「纯计算层」（不许依赖 DB / Redis），它只需要这一个函数。

LiteLLM 的拒绝按错误体里的 error.type 分（不按报错文字猜；LiteLLM 1.106 实测）：
    auth_error / token_not_found_in_db / expired_key
                            Key 被停用、在本轮进行中被重置或删除 → 本轮停下，重发时取新的 Key
    key_model_access_denied 等
                            模型被移出了 Key → 本轮停下，找管理员
    budget_exceeded         预算用完（在 LiteLLM 里按 Key 配的）→ 本轮停下，找管理员
    authentication_error    供应商拒绝了 LiteLLM 的凭据（平台的配置事故）
                            → 本轮失败，不暴露供应商细节
没有 error.type 的：402（供应商余额不足）同上，其余交给 engine 的 classify 按状态码分类。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from atlas_engine.contracts import EngineError, ModelUnavailable

__all__ = ["ModelCallRefused", "model_call_failure"]

#: LiteLLM 的 ProxyErrorTypes。★ 这是和 LiteLLM 之间的协议，升级 LiteLLM 后要复核
_KEY_REJECTED = frozenset({"auth_error", "token_not_found_in_db", "expired_key"})
_MODEL_DENIED = frozenset(
    {"key_model_access_denied", "team_model_access_denied", "user_model_access_denied"}
)
_BUDGET = frozenset({"budget_exceeded"})
#: LiteLLM 把供应商的拒绝按调用协议的格式转回来（Anthropic 格式里是 authentication_error）
_PROVIDER_REJECTED = frozenset({"authentication_error"})
_PLATFORM_FAULT = "模型服务暂时不可用，请稍后重试；持续出现请联系模型管理员"


class ModelCallRefused(EngineError):
    """LiteLLM 拒绝了这一轮里的一次调用：模型被移出了 Key、预算用完。重发也没用，要找人。"""

    kind = "model_not_allowed"


def _error_type(exc: BaseException) -> tuple[str | None, str]:
    """→ (error.type, error.message)。

    Anthropic SDK 的 body 是整个错误体，OpenAI SDK 已经取出了 error 这一层。
    """
    body: Any = getattr(exc, "body", None)
    error = body.get("error", body) if isinstance(body, Mapping) else None
    if not isinstance(error, Mapping):
        return None, ""
    kind = error.get("type")
    return (str(kind) if kind else None), str(error.get("message") or "")


def model_call_failure(exc: BaseException) -> EngineError | None:
    """模型调用的异常 → 给用户看的失败说明。认不出来返回 None，交给 engine 的 classify。

    ★ 走到这里时，5xx 已经由 SDK 退避重试过了：暂时性的原因只需告诉用户稍后重发。
    """
    status = getattr(exc, "status_code", None)
    kind, _ = _error_type(exc)
    if kind in _KEY_REJECTED:
        # Run 开始时取的是最新的 Key：重发就会换用新的；被停用的话重发会在开始前被拦下
        return ModelUnavailable(
            "模型 Key 在本轮进行中被重置或停用，本轮已停止：重发即可换用新的 Key；"
            "如果 Key 已被停用，请联系模型管理员",
            status_code=status,
            reason=kind,
        )
    if kind in _MODEL_DENIED:
        return ModelCallRefused(
            "本轮已停止：这个模型已经不在智能体的模型 Key 里，请联系模型管理员重新分配",
            status_code=status,
            reason=kind,
        )
    if kind in _BUDGET:
        return ModelCallRefused(
            "本轮已停止：模型 Key 的预算已用完，请联系模型管理员", status_code=status, reason=kind
        )
    if kind in _PROVIDER_REJECTED or status == 402:
        # 供应商凭据失效、账户余额不足：平台的配置事故；不对用户暴露供应商的细节
        return ModelUnavailable(_PLATFORM_FAULT, status_code=status, reason=kind)
    return None
