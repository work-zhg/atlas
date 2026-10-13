"""错误分类学 —— `kind` 与 `run.error_kind` 一一对应（文档 §13.1）。

★ 住在 contracts：错误种类是**事件契约的一部分**（run.failed 携带
  error_kind，前端按它分流展示），抛出方遍布 kernel 中间件（LimitExceeded /
  ContextOverflow）、spec 校验（InvalidSpec）与 runner（RunTimeout）——
  典型的「两边都认、谁都不拥有」。
"""

from __future__ import annotations


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


class ContextOverflow(EngineError):
    """压缩之后仍然超窗口（文档 §7.6）。正常情况用户不该看到。"""

    kind = "context_overflow"


class LimitExceeded(EngineError):
    kind = "limit_exceeded"


class RunTimeout(EngineError):
    kind = "timeout"


class ModelRefused(EngineError):
    """模型拒绝作答（`stop_reason: refusal`）。

    不是故障，重试也没用 —— 原样把模型的说明展示给用户（§13.1）。
    """

    kind = "model_refused"


class RunCancelled(EngineError):
    kind = "cancelled"


def classify(exc: BaseException) -> EngineError:
    """把上游异常映射到 §13.1 的分类。

    ★ 依据是异常实例上的 `status_code`（实测 anthropic SDK 的异常都带它），
      不是类名匹配 —— 类名会随 SDK 版本变，状态码是协议的一部分。

    分对了的价值：前端按 kind 决定文案与"能不能重试"。全都归成
    model_unavailable 的话，用户看到限流会以为服务挂了。
    """
    if isinstance(exc, EngineError):
        return exc

    message = str(exc)[:500]
    status = getattr(exc, "status_code", None)

    if status == 429:
        return ModelRateLimited(f"模型繁忙（429），请稍后重试：{message}", status_code=429)
    if isinstance(status, int) and 500 <= status < 600:
        return ModelUnavailable(f"模型服务异常（{status}）：{message}", status_code=status)
    if isinstance(status, int) and 400 <= status < 500:
        # 4xx 是请求本身的问题，重试无用。最常见的是 key 失效与参数不被接受，
        # 两者都需要人去改配置，所以要把原文透出来而不是笼统说"服务异常"。
        return ModelUnavailable(f"网关拒绝了请求（{status}）：{message}", status_code=status)

    # 超时、连接失败等没有状态码的情况
    return ModelUnavailable(f"模型调用失败：{message}")


#: `stop_reason` 取这些值时视为模型拒答
REFUSAL_STOP_REASONS = frozenset({"refusal"})
