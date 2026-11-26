"""共享基础层：模型网关（数据面）与管理平台（控制面）、供应商层共用的契约与状态原语。

★ 只放两端真正在库、缓存或连接上对得上的值对象、预置规则、健康状态、版本同步、分区维护和上游连接池。
★ 不依赖 gateway.*、api.* 或控制面领域服务（由 import-linter 机械守卫）。
"""

from .health import AccountIssue, IssueRecord, RouteHealth
from .kernel import (
    AccountRef,
    Egress,
    Entry,
    ErrorCategory,
    FailureKind,
    Format,
    GatewayError,
    Intent,
    ModelCaps,
    ModelKind,
    ParamPlan,
    Price,
    RequestSummary,
    Route,
    ThinkingSupport,
    UpstreamFailure,
    Usage,
    key_digest,
)
from .partitions import PartitionKeeper
from .presets import CONSERVATIVE, EFFORT_LEVELS, PRESETS, Preset, PresetRules, UpstreamRules
from .sync import (
    CONFIG_INVALIDATE_CHANNEL,
    KEY_VERSION_KEY,
    TENANT_VERSION_PREFIX,
    VERSION_KEY,
    ConfigInvalidation,
    VersionWatch,
)
from .upstream import AddressGuard, UpstreamClient, UpstreamError

__all__ = [
    "CONFIG_INVALIDATE_CHANNEL",
    "CONSERVATIVE",
    "EFFORT_LEVELS",
    "KEY_VERSION_KEY",
    "PRESETS",
    "TENANT_VERSION_PREFIX",
    "VERSION_KEY",
    "AccountIssue",
    "AccountRef",
    "AddressGuard",
    "ConfigInvalidation",
    "Egress",
    "Entry",
    "ErrorCategory",
    "FailureKind",
    "Format",
    "GatewayError",
    "Intent",
    "IssueRecord",
    "ModelCaps",
    "ModelKind",
    "ParamPlan",
    "PartitionKeeper",
    "Preset",
    "PresetRules",
    "Price",
    "RequestSummary",
    "Route",
    "RouteHealth",
    "ThinkingSupport",
    "UpstreamClient",
    "UpstreamError",
    "UpstreamFailure",
    "UpstreamRules",
    "Usage",
    "VersionWatch",
    "key_digest",
]
