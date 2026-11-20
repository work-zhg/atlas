"""共享内核：领域之间传递的值对象（c4-model/modelgateway/code-gateway-domains.html）。

★ 只有值对象，不依赖任何领域；领域之间只靠这些互相传话，就不会互相认识。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID

__all__ = [
    "AccountRef",
    "Egress",
    "Entry",
    "ErrorCategory",
    "FailureKind",
    "Format",
    "GatewayError",
    "Intent",
    "ModelCaps",
    "ModelKind",
    "ParamPlan",
    "Price",
    "RequestSummary",
    "Route",
    "ThinkingSupport",
    "UpstreamFailure",
    "Usage",
]


class Format(StrEnum):
    """请求和回答的格式（协议）。"""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE = "google"


class Entry(StrEnum):
    """调用方进的入口：按哪种格式读请求、写回答，线路的账号没有这种格式时能不能转换。"""

    #: /v1/messages：只走登记了 Anthropic 格式地址的线路，不转换
    ANTHROPIC = "anthropic"
    #: /v1/chat/completions、/v1/embeddings、/v1/rerank：只走 OpenAI 格式的线路，不转换
    OPENAI = "openai"
    #: /universal/v1/messages：按 Anthropic 格式传；账号没有 Anthropic 格式地址时转成它有的格式。
    #: 平台里的应用都用它，不用按模型判断该调哪个入口
    UNIVERSAL = "universal"

    @property
    def format(self) -> Format:
        return Format.OPENAI if self is Entry.OPENAI else Format.ANTHROPIC

    @property
    def converts(self) -> bool:
        return self is Entry.UNIVERSAL


class ModelKind(StrEnum):
    CHAT = "chat"
    EMBEDDING = "embedding"
    RERANK = "rerank"


class Egress(StrEnum):
    DIRECT = "direct"
    PROXY = "proxy"


Thinking = Literal["on", "off", "auto"]
ThinkingMode = Literal["adaptive", "budget", "off"]


class ThinkingSupport(StrEnum):
    NONE = "none"
    #: 能思考，也能关
    OPTIONAL = "optional"
    #: 思考关不掉
    ALWAYS_ON = "always_on"


@dataclass(frozen=True)
class ModelCaps:
    """模型对调用方承诺的能力：登记时由预置规则带出，管理员可以改；模型清单原样返回。

    ★ 没有预置、也没人填写的能力一律按「不支持」。
    """

    kind: ModelKind = ModelKind.CHAT
    vision: bool = False
    tools: bool = False
    thinking: ThinkingSupport = ThinkingSupport.NONE
    context_window: int | None = None
    max_output: int | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "vision": self.vision,
            "tools": self.tools,
            "thinking": self.thinking.value,
            "context_window": self.context_window,
            "max_output": self.max_output,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, object] | None) -> ModelCaps:
        raw = raw or {}

        def _opt_int(value: object) -> int | None:
            return int(value) if isinstance(value, int | str) and str(value).isdigit() else None

        return cls(
            kind=ModelKind(str(raw.get("kind") or ModelKind.CHAT.value)),
            vision=bool(raw.get("vision", False)),
            tools=bool(raw.get("tools", False)),
            thinking=ThinkingSupport(str(raw.get("thinking") or ThinkingSupport.NONE.value)),
            context_window=_opt_int(raw.get("context_window")),
            max_output=_opt_int(raw.get("max_output")),
        )


@dataclass(frozen=True)
class Price:
    """模型的对内统一价：元 / 百万 token。缓存写入按输入价算。都是 0 = 不计费。"""

    input: Decimal = Decimal(0)
    cache_hit: Decimal = Decimal(0)
    output: Decimal = Decimal(0)

    @property
    def billable(self) -> bool:
        return bool(self.input or self.cache_hit or self.output)

    def to_json(self) -> dict[str, str]:
        return {
            "input": str(self.input),
            "cache_hit": str(self.cache_hit),
            "output": str(self.output),
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, object] | None) -> Price:
        raw = raw or {}
        return cls(*(Decimal(str(raw.get(k) or 0)) for k in ("input", "cache_hit", "output")))


@dataclass(frozen=True)
class Intent:
    """调用方按统一含义说的意图。None = 没说。"""

    thinking: Thinking | None = None
    thinking_budget: int | None = None
    effort: str | None = None
    temperature: float | None = None
    max_output: int | None = None


@dataclass(frozen=True)
class RequestSummary:
    """读写器从请求里读出的共同信息：后面的共用步骤只看它，不看请求是哪种格式。"""

    model: str
    kind: ModelKind = ModelKind.CHAT
    stream: bool = False
    session_key: str | None = None
    intent: Intent = field(default_factory=Intent)
    has_images: bool = False
    has_tools: bool = False
    approx_input_tokens: int = 0
    #: 历史里思考块的来源标记（ThinkingProvenance 打的）；没有标记的思考块不在这里
    thinking_stamps: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ParamPlan:
    """参数怎么改：能力与参数算出来，读写器照着在请求原来的格式上改。

    每一项都是「最终要发的值」，None = 不传（请求里有也去掉）。
    """

    upstream_model: str
    thinking: ThinkingMode | None = None
    thinking_budget: int | None = None
    effort: str | None = None
    temperature: float | None = None
    max_output: int | None = None
    #: 另外要去掉的字段（比如不收温度的模型也不收 top_p、top_k）
    drop_fields: frozenset[str] = frozenset()
    #: 历史里的思考块要不要去掉（换了上游，或这个上游不收）
    strip_history_thinking: bool = False
    #: 这次回答里思考块的来源标记
    stamp: str = ""
    #: 每一条决定的说明，进调用记录
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class AccountRef:
    """线路上的账号：发请求要用的全部东西。密钥是配置快照里解密后的明文，只在内存里。"""

    uuid: UUID
    provider: str
    #: 按格式登记的地址；可以两种格式都有（同一把密钥）
    endpoints: Mapping[Format, str]
    secret: str = field(repr=False)
    egress: Egress = Egress.DIRECT
    #: 等第一段内容的超时；None = 用网关默认
    first_content_timeout_s: float | None = None
    enabled: bool = True


@dataclass(frozen=True)
class Route:
    """模型 → 某个账号里的某个上游模型。"""

    uuid: UUID
    model: str
    account: AccountRef
    upstream_model: str
    priority: int = 1
    weight: int = 1


@dataclass(frozen=True)
class Usage:
    """统一的四项用量：输入（没命中缓存的部分）、缓存写入、缓存命中、输出（含思考）。"""

    input: int = 0
    cache_write: int = 0
    cache_hit: int = 0
    output: int = 0
    estimated: bool = False

    def merge(self, newer: Usage) -> Usage:
        """流式回答的用量分几段给（开头给输入、结尾给输出），后到的非零值覆盖先到的。"""
        return Usage(
            input=newer.input or self.input,
            cache_write=newer.cache_write or self.cache_write,
            cache_hit=newer.cache_hit or self.cache_hit,
            output=newer.output or self.output,
            estimated=self.estimated or newer.estimated,
        )

    @property
    def empty(self) -> bool:
        return not (self.input or self.cache_write or self.cache_hit or self.output)

    def as_estimated(self) -> Usage:
        return replace(self, estimated=True)


class FailureKind(StrEnum):
    """上游失败的种类：供应商方言按各家的写法归类，线路只看种类、不看状态码。"""

    BAD_REQUEST = "bad_request"
    RATE_LIMITED = "rate_limited"
    #: 过载、服务出错、连不上、等不到第一段内容
    OVERLOADED = "overloaded"
    AUTH_FAILED = "auth_failed"
    NO_BALANCE = "no_balance"


@dataclass(frozen=True)
class UpstreamFailure:
    kind: FailureKind
    message: str
    #: 原始状态码，只记录、不判断；连不上、超时为 None
    status: int | None = None
    retry_after_s: float | None = None
    #: 还没向调用方写出任何内容（提交点之前）
    before_commit: bool = True
    #: 请求有误时原样返回给调用方的错误体
    body: bytes | None = None
    #: 超时（等不到回答、等不到第一段内容）：全部线路都这样时归成 504
    timeout: bool = False


class ErrorCategory(StrEnum):
    """网关统一的错误类别（设计稿第 9 节）。"""

    KEY_INVALID = "key_invalid"
    MODEL_FORBIDDEN = "model_forbidden"
    BEYOND_CAPABILITY = "beyond_capability"
    #: 不转换的入口，模型却没有这种格式的线路
    FORMAT_UNSUPPORTED = "format_unsupported"
    BAD_REQUEST = "bad_request"
    KEY_LIMIT = "key_limit"
    MODEL_BUSY = "model_busy"
    NO_ROUTE = "no_route"
    UPSTREAM_ERROR = "upstream_error"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    INTERRUPTED = "interrupted"


_STATUS = {
    ErrorCategory.KEY_INVALID: 401,
    ErrorCategory.MODEL_FORBIDDEN: 403,
    ErrorCategory.BEYOND_CAPABILITY: 400,
    ErrorCategory.FORMAT_UNSUPPORTED: 400,
    ErrorCategory.BAD_REQUEST: 400,
    ErrorCategory.KEY_LIMIT: 429,
    ErrorCategory.MODEL_BUSY: 429,
    ErrorCategory.NO_ROUTE: 503,
    ErrorCategory.UPSTREAM_ERROR: 502,
    ErrorCategory.UPSTREAM_TIMEOUT: 504,
    ErrorCategory.INTERRUPTED: 502,
}


class GatewayError(Exception):
    """网关给调用方的错误：类别 + 一句能直接给用户看的话。

    no_retry：网关已经换过线路仍然失败，告诉 SDK 不要再自动重试（响应头 x-should-retry: false）。
    """

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        status: int | None = None,
        no_retry: bool = False,
        retry_after_s: float | None = None,
        upstream_body: bytes | None = None,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.status = status or _STATUS[category]
        self.no_retry = no_retry
        self.retry_after_s = retry_after_s
        #: 请求有误时，上游原样的错误体
        self.upstream_body = upstream_body
        #: 只给管理员看的原因（进调用记录，不给调用方）
        self.detail = detail
