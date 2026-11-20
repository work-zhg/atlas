"""应用配置（c4-model/infra-platform/code-appconfig.html）：嵌入宿主应用的、带类型、带版本、可回滚的业务参数。

    MAX_PODS = ConfigKey("sandbox.max_pods", int, default=10, description="每人同时几个沙箱")
    config = AppConfig(sessions, [MAX_PODS])
    await config.register()                 # 宿主 lifespan 里一次
    limit = await config.get(MAX_PODS)      # 每次查库，不缓存
    app.include_router(ConfigRoutes(config, guard).router)

两种作用域：系统参数（默认，只有运营中心能改）；租户设置（scope=ConfigScope.TENANT，每个租户一份，
按本租户的值 → 系统值 → 默认值取，读的时候要给 tenant_uuid）。

不管的事：数据库地址、密钥这类启动参数仍放环境变量；值是明文，不能存机密；没有按用户的配置；
不提供变更回调。
"""

from .key import ConfigKey, ConfigScope
from .migrate import downgrade, upgrade
from .model import ConfigItem, ConfigRevision, VersionConflict
from .routes import ConfigGuard, ConfigRoutes, TenantConfigGuard
from .service import AppConfig, ConfigEntry, ScopeMismatch, TypeChanged, UnknownKey
from .types import TypeMismatch, ValueKind, ValueType

__all__ = [
    "AppConfig",
    "ConfigEntry",
    "ConfigGuard",
    "ConfigItem",
    "ConfigKey",
    "ConfigRevision",
    "ConfigRoutes",
    "ConfigScope",
    "ScopeMismatch",
    "TenantConfigGuard",
    "TypeChanged",
    "TypeMismatch",
    "UnknownKey",
    "ValueKind",
    "ValueType",
    "VersionConflict",
    "downgrade",
    "upgrade",
]
