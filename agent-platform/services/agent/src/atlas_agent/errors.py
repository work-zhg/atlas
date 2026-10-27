"""server 侧领域错误 → HTTP 状态码。

统一错误体（文档 §11）：{"error": {"kind", "message", "details"}}
engine 的 EngineError 由 main.py 另有一条处理链路。
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    status_code: int = 400
    kind: str = "bad_request"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFound(AppError):
    status_code = 404
    kind = "not_found"


class Unauthorized(AppError):
    """没有登录 / 会话失效 / 账号停用。原因只写日志，不告诉调用方（权限设计 §7.5）。"""

    status_code = 401
    kind = "unauthorized"


class Forbidden(AppError):
    """操作码不够，或对已可见的对象级别不够。不涉及「对象是否存在」时才用（否则用 404）。"""

    status_code = 403
    kind = "forbidden"


class Conflict(AppError):
    status_code = 409
    kind = "conflict"


class SlugTaken(Conflict):
    kind = "slug_taken"


class ThreadLocked(Conflict):
    kind = "thread_locked"


class InvalidReference(AppError):
    """spec 引用了不存在 / 不可用的技能或 MCP 工具（保存时校验）。"""

    status_code = 400
    kind = "invalid_reference"


class InvalidApprovalMode(AppError):
    """审批模式不在智能体允许的范围里（doc/native-approval-mode-design.html）。"""

    status_code = 400
    kind = "invalid_approval_mode"


class InvalidAnswers(AppError):
    """ask_user_question 的答案和问题对不上（doc/ask-user-question-design.html）。"""

    status_code = 400
    kind = "invalid_answers"


class DependencyUnavailable(AppError):
    """保存时要查的依赖（配置服务）暂时不可用。"""

    status_code = 503
    kind = "dependency_unavailable"


class BadCursor(AppError):
    status_code = 400
    kind = "invalid_cursor"


class CapabilityUnavailable(RuntimeError):
    """装配期前提不满足：勾了某项能力，但它此刻装不上（缺工具、缺凭据、未开启……）。

    ★ 标记类，不是 AppError —— 它不发生在请求里，而是在 run 装配时。
      NativeRuntime 捕获它产出 run.failed，**消息原样给用户看**，所以子类的
      消息必须面向用户、说清原因，且不能带凭据真值。
      其余异常（真正的 bug）不给用户看细节，只报「执行器内部错误」。

    继承 RuntimeError 是为了兼容：子类原本都是 RuntimeError，既有的
    except 行为不变。
    """

    kind = "capability_unavailable"
