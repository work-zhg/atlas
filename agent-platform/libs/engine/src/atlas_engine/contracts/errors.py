"""错误分类学 —— `kind` 与 `run.error_kind` 一一对应（文档 §13.1）。

★ 住在 contracts：错误种类是**事件契约的一部分**（run.failed 携带
  error_kind，前端按它分流展示），抛出方遍布 kernel 中间件（LimitExceeded /
  ContextOverflow）、spec 校验（InvalidSpec）与 runner ——
  典型的「两边都认、谁都不拥有」。
"""

from __future__ import annotations

from collections.abc import Mapping

#: 用户处理不了的模型故障（5xx、网络、请求被上游拒绝）统一这样说 —— 原因只进日志：
#: 原文对用户没有用，还会露出供应商、网关的细节
SYSTEM_BUSY = "系统忙不过来了，请稍后再试"
#: 额度用完、请求太频繁：换一个模型通常就能接着用
MODEL_QUOTA = "当前模型的额度用完了（或请求太频繁），可以在输入框旁换一个模型，或稍后再试"


class EngineError(Exception):
    kind: str = "engine_error"
    retryable: bool = False

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class InvalidSpec(EngineError):
    """AgentSpec 自身不合法 —— 在发起任何模型调用前抛出。"""

    kind = "invalid_spec"


class UnsupportedProvider(EngineError):
    kind = "unsupported_provider"


class ModelRateLimited(EngineError):
    kind = "model_rate_limited"
    retryable = True


class ModelUnavailable(EngineError):
    kind = "model_unavailable"
    retryable = True


class ModelEmptyResponse(EngineError):
    """模型这一轮什么也没给：没有正文、没有工具调用。

    ★ 不能当成功：用户看到的是「发了消息、没有回复」。常见于 Gemini 把工具调用写坏
      （MALFORMED_FUNCTION_CALL）—— LiteLLM 把它转成了普通的 end_turn，原因到这里已经丢了。
    """

    kind = "model_empty_response"
    retryable = True


class ContextOverflow(EngineError):
    """压缩之后仍然超窗口（文档 §7.6）。正常情况用户不该看到。"""

    kind = "context_overflow"


class LimitExceeded(EngineError):
    kind = "limit_exceeded"


class ModelRefused(EngineError):
    """模型拒绝作答（`stop_reason: refusal`）。

    不是故障，重试也没用 —— 原样把模型的说明展示给用户（§13.1）。
    """

    kind = "model_refused"


#: Anthropic 协议的 error.type → HTTP 状态码。流式响应中途出错时 HTTP 已经回了 200，
#: 原因只在 SSE error 事件的 error.type 里（LiteLLM 的 mid-stream error 就是这样）
_ERROR_TYPE_STATUS = {
    "invalid_request_error": 400,
    "authentication_error": 401,
    "permission_error": 403,
    "not_found_error": 404,
    "request_too_large": 413,
    "rate_limit_error": 429,
    "api_error": 500,
    "overloaded_error": 529,
}


def _status_of(exc: BaseException) -> int | None:
    """异常对应的 HTTP 状态：先看异常上的 status_code，不是错误码时看错误体的 error.type。"""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status >= 400:
        return status
    body = getattr(exc, "body", None)
    error = body.get("error") if isinstance(body, Mapping) else None
    if isinstance(error, Mapping):
        return _ERROR_TYPE_STATUS.get(str(error.get("type")))
    return None


def classify(exc: BaseException) -> EngineError:
    """把上游异常映射到 §13.1 的分类。

    ★ 依据是状态码（实测 anthropic SDK 的异常都带它）与协议里的 error.type，
      不是类名匹配 —— 类名会随 SDK 版本变，这两样是协议的一部分。

    ★ 给用户的只有两种说法：额度用完 / 太频繁（429）可以换模型或稍后再试；其余
      （5xx、4xx、超时、连不上）用户都处理不了，一律 SYSTEM_BUSY。原文由调用方记日志。
    """
    if isinstance(exc, EngineError):
        return exc
    status = _status_of(exc)
    if status == 429:
        return ModelRateLimited(MODEL_QUOTA, status_code=429)
    if status is not None:
        return ModelUnavailable(SYSTEM_BUSY, status_code=status)
    return ModelUnavailable(SYSTEM_BUSY)


#: `stop_reason` 取这些值时视为模型拒答
REFUSAL_STOP_REASONS = frozenset({"refusal"})
